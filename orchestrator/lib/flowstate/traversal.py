from __future__ import annotations

import json
import os
import subprocess
from dataclasses import dataclass, field
from pathlib import Path

from flowstate.gates import run_gates
from flowstate.merge import merge_branch_scopes
from flowstate.parser import Edge, Flow, FlowGraph, Node, OutputSchema, VALID_SUPERVISION_LEVELS
from flowstate.state import BranchRef, BranchScope, COMPLETED_PHASE_STATUSES, JoinFiring, PhaseState, RunState, branch_is_terminal, branch_status_from_phase, phase_is_terminal, check_var_size, state_lock
from flowstate.time import iso_now


@dataclass
class AdvanceOutcome:
    kind: str  # moved | end | blocked | choice_needed | infra_error | error
    target: str | None = None
    reason: str | None = None
    options: list[dict] | None = None
    # Dynamic-fanout envelope extension (Task 7). Populated only when the
    # advance() call resolved to a dynamic_fanout node (instantiation or
    # subsequent rolling-window tick). ``next_startable_branches`` is the FIFO
    # list of branch_ids the orchestrator should now start (call
    # init_subflow_run on); ``max_concurrent_resolved`` is the resolved cap for
    # orchestrator status display (0 = unbounded). ``None`` on every other
    # advance return path. See spec §4.5.
    next_startable_branches: list[str] | None = None
    max_concurrent_resolved: int | None = None
    # Auto-chase audit trail (see ``advance_autochase``). Populated only when
    # the CLI drove the advance through the chasing wrapper: an ordered list of
    # ``{node, runner, outcome, target}`` dicts recording every programmatic hop
    # taken in this single call. ``None`` on a plain single-step ``advance()``.
    chased: list[dict] | None = None


def _maybe_push_subflow_completion(state: RunState) -> None:
    """Push-driven merge-in (design D9): if this run is a subflow child that
    has just reached its terminal state, notify the parent so it can harvest
    declared outputs and fire its reducer (if configured).

    Called from every site that flips ``state.state`` to ``"completed"``. A
    no-op when ``parent_run`` is unset (top-level run) or the lifecycle did
    not actually reach ``"completed"``.

    Idempotent at the parent level: ``complete_subflow`` skips the harvest
    when the BranchRef is already done; the reducer DOES re-fire per T4
    forensic semantics.

    Errors from the push are caught and recorded as a ``subflow_push_failed``
    event — they must not unwind the child's terminal state. Operators can
    invoke ``flowstate complete-subflow`` manually for recovery.
    """
    if state.state != "completed" or state.parent_run is None:
        return
    # Lock-ordering note: we're inside the CHILD's state_lock (held by
    # cmd_advance). Calling complete_subflow acquires the PARENT's state_lock.
    # See the LOCK ORDERING INVARIANT comment on subflow.complete_subflow for
    # why (child → parent) is the only safe direction.
    from flowstate.subflow import complete_subflow
    try:
        complete_subflow(
            state.parent_run.run_dir,
            state.parent_run.branch_id,
            state.parent_run.fanout_node,
        )
    except (ValueError, FileNotFoundError) as exc:
        state.append_event("subflow_push_failed", {
            "parent_run_dir": state.parent_run.run_dir,
            "branch_id": state.parent_run.branch_id,
            "error": str(exc),
        })
        state.save()
        # I-5: flip the parent's BranchRef.status to "error" so the
        # `cmd_status` dashboard (which checks branch_ref.status == "error"
        # to surface failure_reason) actually sees this push failure. The
        # subflow_push_failed event is the primary forensic signal; the
        # status flip is a best-effort surface. Lock-ordering: we're inside
        # the CHILD's state_lock here and must acquire the PARENT's lock —
        # matches the (child → parent) ordering used by complete_subflow.
        try:
            from flowstate.state import RunState as _ParentRunState
            from flowstate.state import state_lock as _parent_state_lock
            parent_dir = Path(state.parent_run.run_dir)
            with _parent_state_lock(parent_dir):
                parent = _ParentRunState.load(parent_dir)
                target_bid = state.parent_run.branch_id
                target_fanout = state.parent_run.fanout_node
                # Scope to the child's own fanout node so a colliding same-id
                # fork branch elsewhere in the run isn't flipped to error.
                phases_to_scan = (
                    [parent.phases[target_fanout]]
                    if target_fanout and target_fanout in parent.phases
                    else list(parent.phases.values())
                )
                for phase in phases_to_scan:
                    for branch_ref in phase.branches:
                        if branch_ref.branch_id == target_bid:
                            branch_ref.status = "error"
                            branch_ref.failure_reason = str(exc)[:500]
                            break
                parent.save()
        except Exception:
            # Best-effort — the subflow_push_failed event on the child is
            # the primary signal; we don't want a secondary failure (e.g.,
            # parent state file corrupted) to mask the original push error.
            pass


@dataclass
class Decision:
    """Structured intent produced by :func:`decide_next_action`.

    Split out from :func:`advance` per Issue #45f — decide reads state
    and runs validation work (gate scripts) to determine *what should
    happen* without mutating state; advance is then a small effects-only
    function that commits the decision (runs transition scripts, applies
    bypass-vars, marks status, saves).

    Discriminated by ``kind``:

    - ``commit`` — advance to ``target`` via ``chosen_edge``.
    - ``blocked`` — gate failure or current-phase-not-done. ``reason`` set.
    - ``choice_needed`` — fan-out couldn't auto-resolve. ``options`` set.
    - ``end_no_edges`` — current phase has no outgoing edges; run ends.
    - ``error`` — caller error (bad target, etc.). ``reason`` set.
    """
    kind: str  # commit | blocked | choice_needed | end_no_edges | error
    target: str | None = None
    chosen_edge: Edge | None = None
    reason: str | None = None
    options: list[dict] | None = None
    # advance() hint: set on `commit` decisions reached via an auto-
    # resolved fan-out, so the apply step can emit the matching event.
    auto_resolved: bool = False


def _is_done(state: RunState, node: str) -> bool:
    p = state.phases.get(node)
    return p is not None and p.status in COMPLETED_PHASE_STATUSES


def _resolve_current(state: RunState, node: str | None) -> str:
    """Return the explicit node name when provided, else the single active cursor."""
    return node if node is not None else state.sole_cursor()


def _vars_for_node(state: RunState, node: str) -> dict[str, object]:
    """The variable view a node should read/evaluate against.

    A fork branch runs in its own isolated variable scope (seeded as a full
    copy of run-level vars at fork time, then written to independently by the
    branch's own nodes). When ``node`` is a branch cursor — its PhaseState
    carries a ``branch_id`` with a live BranchScope — return that scope so the
    branch's conditional edges and lookups see the branch's own data. Otherwise
    (a normal single-thread cursor) return run-level ``state.variables``.

    This is the single seam that makes branch-local lookups branch-scoped; it
    keys off the ``branch_id`` already stamped on the cursor, so it stays
    correct for every node in a multi-node arm as the branch advances.

    Returns the LIVE branch scope dict — callers that write (validate's
    ``sets_variables`` harvest, output-path population) rely on the reference
    identity. For READ-only evaluation that must also see trunk vars written
    after the fork, use :func:`_eval_vars_for_node`.
    """
    phase = state.phases.get(node)
    bid = phase.branch_id if phase is not None else None
    if bid is not None and bid in state.branch_scopes:
        return state.branch_scopes[bid].variables
    return state.variables


def _eval_vars_for_node(state: RunState, node: str) -> dict[str, object]:
    """Read-only, *layered* variable view for a node: the branch's own writes
    on top of its enclosing (ancestor) scopes, trunk-first.

    A variable the branch never set itself falls back to the enclosing scope —
    most importantly a trunk var written AFTER the fork, which the branch's
    fork-time seed snapshot could not contain. Without this, a fork-arm node
    whose conditional out-edges reference a trunk var set post-fork saw the var
    as undefined and forced a spurious ``choice_needed`` (the 2026-07-09 batch
    e2e: ``intent`` set on trunk, edges out of a B1.1 arm node). The branch's
    own value always wins; the result is a fresh dict and never mutates a
    stored scope, so the at-join delta (measured on the branch's own scope vs
    its seed) is unaffected. Used only where the caller reads and never writes:
    conditional-edge evaluation and gate / transition-script env.
    """
    phase = state.phases.get(node)
    bid = phase.branch_id if phase is not None else None
    if bid is None or bid not in state.branch_scopes:
        return dict(state.variables)
    # Walk the dotted ancestor chain outermost-first (B1, B1.1, B1.1.2, ...)
    # so nearer scopes overlay farther ones. A nearer scope's value wins ONLY
    # when it is non-None: a var declared-but-unset is seeded as None into every
    # scope at fork time, so a branch's None placeholder must NOT clobber a real
    # value the trunk wrote after the fork (that's exactly the e2e `intent`
    # case). A None only fills a key no ancestor has set.
    parts = bid.split(".")
    merged: dict[str, object] = {}
    for i in range(1, len(parts) + 1):
        scope = state.branch_scopes.get(".".join(parts[:i]))
        if scope is None:
            continue
        for k, v in scope.variables.items():
            if v is not None or k not in merged:
                merged[k] = v
    return merged


# Per-failure output truncation cap. Matches the script-NODE failure path
# at `_run_node_script`-caller below (which truncates `stderr[:500]`). Kept
# in sync deliberately so a 500-char ceiling holds across every failure
# surface the orchestrator reads. See Issue #46.
_FAILURE_OUTPUT_CAP = 500


def _format_script_failures(header: str, failures: list) -> str:
    """Render a multi-line `reason` string from a list of ScriptFailure.

    Surfaces each failure's `stderr` (or `stdout` as fallback) — without this,
    gate / transition-script failures arrived at the orchestrator with only
    the script path, losing the actual error output. Matches the script-NODE
    pattern in advance(). See Issue #46.
    """
    lines = [f"{header}:"]
    for f in failures:
        body = (f.stderr or f.stdout or "").strip()[:_FAILURE_OUTPUT_CAP]
        if not body:
            body = "(no output)"
        lines.append(f"- {f.path} (exit {f.exit_code}): {body}")
    return "\n".join(lines)


def _resolve(template: str, state: RunState) -> str:
    """Resolve a path template against the run's variables → absolute path.

    Thin wrapper around `render.resolve_path_template` so traversal callsites
    don't have to repeat the (variables, repo_root) tuple. See Issue #46.
    """
    from flowstate.render import resolve_path_template
    return resolve_path_template(template, state.variables, state.repo_root)


def _populate_output_paths(
    flow: Flow,
    state: RunState,
    schema: OutputSchema,
    *,
    vars_target: dict[str, object] | None = None,
) -> None:
    """Resolve every output file path and write into the corresponding sets_variables slot.

    Also mkdir -p the parent of each output path so the agent / script can write.

    ``vars_target`` lets the caller direct the output-path writes into a
    specific scope (e.g. a branch scope after fork fan-out). Defaults to
    ``state.variables`` for the single-cursor / non-forked path. Path
    templates are resolved using the same ``vars_target`` so per-branch
    variables (incl. branch-local paths) substitute correctly. See Task 24.
    """
    from flowstate.render import resolve_path_template
    vars_view = vars_target if vars_target is not None else state.variables
    name_to_path = {
        sf.name: resolve_path_template(sf.path, vars_view, state.repo_root)
        for sf in schema.files
    }
    for var_name, file_id in schema.sets_variables.items():
        vars_view[var_name] = name_to_path[file_id]
    for path in name_to_path.values():
        Path(path).parent.mkdir(parents=True, exist_ok=True)


def _env_for_scripts(
    state: RunState,
    *,
    vars_override: dict[str, object] | None = None,
    phase_override: str | None = None,
) -> dict[str, str]:
    """Build the env for a gate / transition / node script.

    By default reads variables from ``state.variables`` and the phase name
    from ``state.current_phase``. When fan-out has produced multiple cursors,
    ``state.current_phase`` would raise; callers in that path supply
    ``phase_override`` (the specific branch node being executed) and
    ``vars_override`` (the branch's scope variables). See Task 24 fix:
    fork-branch script-runner successors now execute inline using the branch
    scope as their variable view.
    """
    env = {**os.environ}
    env["FLOWSTATE_RUN_DIR"] = str(state.run_dir)
    env["FLOWSTATE_FLOW_DIR"] = str(state.repo_root / state.flow_dir)
    # Scripts that resolve factory-relative paths (e.g. sync_recency_check.sh /
    # record_sync.sh reading factory/.last_sync_<userid>) read FACTORY_ROOT.
    # A node's working_dir is often the run artefact dir, not the repo root, so
    # export the repo root explicitly rather than relying on cwd.
    env["FACTORY_ROOT"] = str(state.repo_root)
    phase_name = phase_override if phase_override is not None else state.sole_cursor()
    env["FLOWSTATE_PHASE"] = phase_name
    # Branch-internal scripts (script-runner nodes inside a fork branch) carry
    # a non-None branch_id on their PhaseState. Surface it so the script can
    # identify which branch it's running in without flowstate having to do
    # branch-aware variable scoping at the env-var level. See design D5.
    phase = state.phases.get(phase_name)
    if phase is not None and phase.branch_id is not None:
        env["FLOWSTATE_BRANCH_ID"] = phase.branch_id
    vars_view = vars_override if vars_override is not None else state.variables
    for name, value in vars_view.items():
        if value is None or value == "":
            continue
        # Strings pass through verbatim; everything else is JSON-encoded —
        # the same convention _fire_reducer's env builder uses, so a gate
        # script and a reducer see identical encodings of the same variable.
        # (Booleans therefore render as true/false, not True/False.)
        # Spec D8 row 6.
        env[f"FLOWSTATE_VAR_{name}"] = value if isinstance(value, str) else json.dumps(value)
    return env


SCRIPT_TIMEOUT_SECONDS = 120


def _run_node_script(
    flow: Flow,
    state: RunState,
    node: Node,
    *,
    vars_override: dict[str, object] | None = None,
    phase_override: str | None = None,
    env_vars_override: dict[str, object] | None = None,
) -> tuple[int, str, str]:
    """Run a node's script subprocess.

    ``vars_override`` / ``phase_override`` are forwarded to
    :func:`_env_for_scripts` and used for working-dir / output-path
    resolution. Default (both ``None``) preserves the original single-cursor
    behaviour against ``state.variables``. See Task 24 fix.

    ``env_vars_override``, when given, is used for the subprocess ENV build
    only (typically the layered read view from :func:`_eval_vars_for_node`,
    so a fork-arm script sees trunk vars written after the fork). Working-dir
    path-template resolution deliberately stays on ``vars_override`` (the
    live scope) so it matches the scope output paths were populated into.
    """
    script_path = state.repo_root / state.flow_dir / node.script
    vars_view = vars_override if vars_override is not None else state.variables
    from flowstate.render import resolve_path_template
    cwd = Path(resolve_path_template(node.working_dir, vars_view, state.repo_root))
    cwd.mkdir(parents=True, exist_ok=True)
    try:
        proc = subprocess.run(
            [str(script_path)],
            cwd=cwd,
            capture_output=True,
            text=True,
            env=_env_for_scripts(
                state,
                vars_override=(
                    env_vars_override if env_vars_override is not None else vars_override
                ),
                phase_override=phase_override,
            ),
            timeout=SCRIPT_TIMEOUT_SECONDS,
        )
    except subprocess.TimeoutExpired:
        return -1, "", f"script timed out after {SCRIPT_TIMEOUT_SECONDS}s"
    return proc.returncode, proc.stdout, proc.stderr


def _execute_script_node(
    flow: Flow,
    state: RunState,
    target_node: Node,
    *,
    vars_target: dict[str, object] | None = None,
    phase_override: str | None = None,
) -> AdvanceOutcome | None:
    """Run the script for a ``runner=script`` node, validate its outputs,
    mark it ``done``, and record the standard events.

    Centralises the script-execution path so both the single-cursor case
    (``_apply_decision``) and the fork-fan-out case (``_apply_fork_fanout``)
    share one implementation. See Task 24.

    Behaviour:
    - Output-path resolution + write target follow ``vars_target`` (defaults
      to ``state.variables``). For a fork branch, pass the branch scope so
      worker outputs land in the branch's scope and the join merge can fold
      them back.
    - ``phase_override`` is forwarded into the subprocess env so multi-cursor
      callers (post-fork) don't trip ``state.current_phase``'s single-cursor
      guard.
    - On any failure (non-zero exit, missing output, schema violation), saves
      state with the failure event recorded and returns an ``AdvanceOutcome``
      ``kind="error"`` whose ``reason`` matches the existing single-cursor
      messages.
    - On success, marks the phase ``done``, sets each declared
      ``sets_variables`` slot to the resolved output path in the same scope,
      appends a ``validation_passed`` event, saves, and returns ``None`` so
      the caller can continue.
    """
    schema = flow.schemas[target_node.output_schema]
    vars_view = vars_target if vars_target is not None else state.variables
    _populate_output_paths(flow, state, schema, vars_target=vars_view)
    state.save()

    # Env gets the LAYERED read view (own scope over ancestors), computed
    # AFTER _populate_output_paths so the freshly-written output-path vars are
    # included: a fork-arm script must see a trunk var set after the fork,
    # exactly like conditional-edge eval and gate env do. Writes below stay on
    # the live `vars_view`. Same bug class as the 2026-07-09 batch e2e fix.
    env_view = _eval_vars_for_node(state, phase_override or target_node.name)
    rc, stdout, stderr = _run_node_script(
        flow, state, target_node,
        vars_override=vars_view, phase_override=phase_override,
        env_vars_override=env_view,
    )
    state.append_event("script_node_ran", {
        "node": target_node.name, "exit_code": rc,
    })
    if rc != 0:
        state.save()
        return AdvanceOutcome(
            kind="error",
            reason=(
                f"script node {target_node.name!r} exited {rc}: "
                f"{stderr.strip()[:500] or stdout.strip()[:500]}"
            ),
        )

    # Harvest FLOWSTATE_OUTPUT_<var>=<value> lines from stdout. This is the
    # mechanism by which script nodes set graph variables without writing a
    # file (e.g. recency_check sets `should_sync=true|false`; the
    # populate_result_*.sh scripts set `subflow_result=<dict>`). Required
    # variables that don't appear in stdout fail the node so the schema
    # contract is enforced. Mirrors the reducer's parsing convention (see
    # `flowstate.reducer.parse_reducer_stdout`). Without this path, every
    # script node whose schema declares `required_variables` with empty
    # `files: []` silently left its variables null, blocking every
    # downstream conditional edge.
    from flowstate.reducer import parse_reducer_stdout, ReducerParseError
    try:
        harvested = parse_reducer_stdout(stdout)
    except ReducerParseError as exc:
        state.save()
        return AdvanceOutcome(
            kind="error",
            reason=f"script node {target_node.name!r} produced malformed stdout: {exc}",
        )
    for var_name, value in harvested.items():
        vars_view[var_name] = value
    # Required-variable READ check runs on a fresh layered view (built after
    # the harvest writes above, so they're visible): a requirement satisfied
    # by a trunk var written after the fork must pass for a fork-arm script,
    # matching the layered semantics of condition eval / env.
    post_view = _eval_vars_for_node(state, phase_override or target_node.name)
    missing_required = [
        v for v in schema.required_variables if post_view.get(v) is None
    ]
    if missing_required:
        state.save()
        return AdvanceOutcome(
            kind="error",
            reason=(
                f"script node {target_node.name!r} did not set required "
                f"variable(s): {missing_required}. Schema "
                f"{target_node.output_schema!r} declares them as required; "
                f"the script must emit FLOWSTATE_OUTPUT_<name>=<value> on "
                f"stdout for each."
            ),
        )

    # Script nodes have no completion.yml; validate each declared output
    # file against its JSON Schema directly (no _session_id requirement).
    from flowstate.definitions import validate_against_schema, SchemaValidationError
    from flowstate.render import resolve_path_template
    import json as _json
    for sf in schema.files:
        # Path templates resolve against the RAW live scope (vars_view), NOT
        # the layered view — deliberately. Output paths were populated into
        # this same scope at node entry (_populate_output_paths), so resolving
        # the check against the same scope guarantees entry-time and
        # check-time agree on the file location. A layered read could resolve
        # a template through a trunk var and check a different path than the
        # one the entry populated.
        out_path = Path(resolve_path_template(sf.path, vars_view, state.repo_root))
        if not out_path.exists():
            state.save()
            return AdvanceOutcome(
                kind="error",
                reason=f"script node {target_node.name!r} did not produce {sf.path}",
            )
        if sf.definition is None:
            # Existence-only output: verify non-empty, skip the definitions
            # lookup and parse entirely — the artefact's shape is owned by
            # the producing script. Spec 2026-07-07 §3.2.
            if out_path.stat().st_size == 0:
                state.save()
                return AdvanceOutcome(
                    kind="error",
                    reason=f"script node output {sf.path} is empty",
                )
            continue
        # Only run JSON Schema validation when the definition actually IS a
        # JSON Schema (declares `$schema` at top level). Some script outputs
        # are non-JSON (e.g. markdown reports) and their definition file is
        # an informational descriptor with `"format": "markdown"`. For those
        # we only check existence — the schema authors chose this format
        # deliberately and JSON-parsing the file would always fail.
        definition = flow.definitions[sf.definition]
        if isinstance(definition, dict) and "$schema" in definition:
            try:
                data = _json.loads(out_path.read_text())
                validate_against_schema(data, definition)
            except (_json.JSONDecodeError, SchemaValidationError, OSError) as exc:
                # Scope is intentionally narrow: schema-violation, malformed
                # JSON, and read errors are user-recoverable. Anything else
                # (KeyError on flow.definitions, programmer bug) propagates
                # as a traceback so it's visible to the operator. See Issue #44c.
                state.save()
                return AdvanceOutcome(
                    kind="error",
                    reason=f"script node output {sf.path} failed validation: {exc}",
                )
    # All good.
    state.set_phase_status(target_node.name, "done")
    for var_name, file_id in schema.sets_variables.items():
        vars_view[var_name] = resolve_path_template(
            next(f.path for f in schema.files if f.name == file_id),
            vars_view,
            state.repo_root,
        )
    state.append_event("validation_passed", {
        "node": target_node.name, "agent_session_id": None,
    })
    state.save()
    return None


def _choice_payload(graph: FlowGraph, source: str) -> list[dict]:
    out = []
    for edge in graph.edges_from(source):
        out.append({
            "target": edge.target,
            "label": edge.label,
            "condition": edge.condition,
            "gates": list(edge.gates),
        })
    return out


def next_nodes(graph: FlowGraph, state: RunState) -> list[Node]:
    """List target nodes reachable from current_phase, regardless of edge attrs.

    Retained for the `flowstate next` CLI (introspection only).
    """
    return [graph.node(e.target) for e in graph.edges_from(state.sole_cursor())]


def decide_next_action(
    flow: Flow,
    state: RunState,
    *,
    force: bool = False,
    target: str | None = None,
    node: str | None = None,
) -> Decision:
    """Read current state, evaluate conditions, run edge gates, and
    determine the next transition. Pure with respect to flowstate state
    (does not mutate ``state``, does not call ``state.save()``).

    Gates are subprocess calls — they can have filesystem side effects
    of their own (their authors wrote them). decide does not promise
    side-effect-free execution; it promises not to mutate flowstate's
    state file. See Issue #45f.

    When ``node`` is provided it is used as the current node instead of
    reading ``state.current_phase``. Required when multiple cursors are
    active (e.g. after a fork) to avoid the multi-cursor ValueError.

    Returns a :class:`Decision` describing what :func:`advance` should
    commit.
    """
    graph = flow.graph
    current = _resolve_current(state, node)
    is_current_done = _is_done(state, current)

    if not is_current_done and not force:
        return Decision(
            kind="blocked",
            reason=f"current phase {current!r} is not done (validate first or pass --force)",
        )

    edges = graph.edges_from(current)
    if not edges:
        return Decision(
            kind="end_no_edges",
            reason=f"no outgoing edges from {current!r}",
        )

    legal_targets = [e.target for e in edges]

    if target is not None and target not in legal_targets:
        return Decision(
            kind="error",
            reason=(
                f"target {target!r} is not an outgoing edge from {current!r}. "
                f"Legal targets: {legal_targets}"
            ),
        )

    auto_resolved = False
    if len(edges) > 1 and target is None:
        # Auto-resolve when exactly one outgoing edge's condition is satisfied
        # by current state. Saves an LLM round on every fan-out whose conditions
        # are simple equality / inequality / numeric comparison. See Issue #7b.
        from flowstate.conditions import evaluate as _eval_condition
        # Evaluate against the branch's own scope when `current` is a branch
        # cursor — a branch's `if` must see the branch's own variables (e.g. a
        # should_sync a branch script just wrote into its scope), not run-level.
        eval_vars = _eval_vars_for_node(state, current)
        eval_results = [_eval_condition(e.condition, eval_vars) for e in edges]
        true_indices = [i for i, r in enumerate(eval_results) if r is True]
        if len(true_indices) == 1 and all(r is not None for r in eval_results):
            target = edges[true_indices[0]].target
            auto_resolved = True
        else:
            return Decision(
                kind="choice_needed",
                options=_choice_payload(graph, current),
            )

    chosen_edge = next(e for e in edges if (target is None or e.target == target))

    # Gates are validation work — part of "deciding" whether the transition
    # is permitted. Run them here, before advance commits anything.
    if chosen_edge.gates:
        env_extra = _env_for_scripts(state, phase_override=current, vars_override=_eval_vars_for_node(state, current))
        gate_outcome = run_gates(
            chosen_edge.gates,
            flow_dir=state.repo_root / state.flow_dir,
            env_extra=env_extra,
        )
        if not gate_outcome.passed:
            return Decision(
                kind="blocked",
                reason=_format_script_failures("gate(s) failed", gate_outcome.failures),
            )

    return Decision(
        kind="commit",
        target=chosen_edge.target,
        chosen_edge=chosen_edge,
        auto_resolved=auto_resolved,
    )


def advance(
    flow: Flow,
    state: RunState,
    force: bool = False,
    target: str | None = None,
    node: str | None = None,
) -> AdvanceOutcome:
    """One-shot: decide + commit. The orchestrator-facing CLI entrypoint.

    When ``force=True`` and the current phase is not yet terminal, the
    force-mark (``done_forced`` + event + save) happens **first** — before
    calling decide. This preserves the Issue #29 invariant that a forced
    advance survives a downstream gate / script failure (the next CLI
    invocation sees the committed force-mark and doesn't repeat it).

    When ``node`` is provided it is used as the current node instead of
    reading ``state.current_phase``. Required when multiple cursors are
    active (e.g. after a fork) to avoid the multi-cursor ValueError.

    After the optional force-mark, delegates to :func:`decide_next_action`
    and commits the resulting :class:`Decision`. See Issue #45f.
    """
    # Resolve the current node once, using explicit node if provided.
    current = _resolve_current(state, node)
    # Fork fan-out: when the current node is a fork, skip normal decide/apply
    # and immediately fan out all successor nodes as parallel cursors.
    # This path is taken when the orchestrator calls advance(flow, state, node="fork")
    # after landing on the fork (which stamps it in_progress). See Task 7.
    try:
        current_node_obj = flow.graph.node(current)
    except KeyError:
        current_node_obj = None
    if current_node_obj is not None and current_node_obj.runner == "fork":
        # A fork is a structural gateway: no worker, no validation, so nothing
        # ever transitions it in_progress -> done except the fan-out itself.
        # The precondition is therefore "landed and not yet fanned", NOT
        # "done" — the old done-guard was unsatisfiable without --force, and
        # conversely let a second --node call on an already-fanned (done)
        # fork re-enter _apply_fork_fanout and duplicate BranchRefs/scopes.
        # --force is deliberately ignored here: it must never enable a
        # duplicate fan-out. Spec D8 row 7 (addendum 2026-07-03).
        fork_phase = state.phases.get(current)
        if fork_phase is None or fork_phase.status == "pending":
            return AdvanceOutcome(
                kind="blocked",
                reason=(
                    f"fork {current!r} has not been landed on yet; advance "
                    f"onto it first (landing stamps it in_progress)"
                ),
            )
        if fork_phase.branches:
            return AdvanceOutcome(
                kind="blocked",
                reason=(
                    f"fork {current!r} already fanned out (branches: "
                    f"{[b.branch_id for b in fork_phase.branches]})"
                ),
            )
        return _apply_fork_fanout(flow, state, current)
    # Join firing: when the current node is a join in ready_to_fire status,
    # fire it immediately (moves to downstream, marks join done). This path is
    # taken when the orchestrator calls advance(flow, state, node="j") after all
    # arrivals have been recorded. See Task 8.
    if current_node_obj is not None and current_node_obj.runner == "join":
        phase = state.phases.get(current)
        if phase is not None and phase.status == "ready_to_fire":
            return _fire_join(flow, state, current_node_obj)
    # Subflow completion handoff: when the current node is a subflow node that
    # has been marked in_progress (by the structural-runner interception in
    # _apply_decision), check whether its child run has reached terminal state.
    # If so, harvest declared outputs into the parent's variable scope and
    # advance to the single successor. If not, return blocked so the
    # orchestrator can poll again later. See Task 18.
    if current_node_obj is not None and current_node_obj.runner == "subflow":
        return _apply_subflow_completion(flow, state, current_node_obj)
    # Dynamic fanout: when the current node is a dynamic_fanout that has already
    # landed (in_progress), handle two sub-cases in order:
    #   1. Branches already stamped → check if all are terminal (Task 20 completion path).
    #   2. Branches not yet stamped → instantiate one branch per source-variable item (Task 19).
    if current_node_obj is not None and current_node_obj.runner == "dynamic_fanout":
        phase = state.phases.get(current)
        if phase is not None and phase.branches:
            outcome = _apply_dynamic_fanout_completion(flow, state, current_node_obj)
        else:
            outcome = _apply_dynamic_fanout(flow, state, current_node_obj)
        # Task 7: extend the envelope with the rolling-window dispatch hint the
        # orchestrator iterates to call init_subflow_run. flowstate owns the
        # policy (resolution + arithmetic); orchestrator stays dumb. Computed
        # against the post-call PhaseState so newly-stamped branches are
        # visible on the instantiation path and terminal branches are excluded
        # on the completion path. See spec §4.5.
        max_concurrent_resolved = _resolve_max_concurrent(
            fanout_node=current_node_obj,
            flow_stem=state.flow_name,
            prefs_path=state.repo_root / "factory" / "factory-prefs.yml",
        )
        outcome.max_concurrent_resolved = max_concurrent_resolved
        phase_after = state.phases.get(current)
        if phase_after is not None:
            outcome.next_startable_branches = _compute_next_startable_branches(
                phase_after, max_concurrent_resolved,
            )
        else:
            outcome.next_startable_branches = []
        return outcome
    # Persist the force-mark eagerly so a gate failure inside decide()
    # (or any later failure inside _apply_decision) doesn't leave the
    # forced-done flag in memory only. Issue #29.
    if force and not _is_done(state, current):
        state.set_phase_status(current, "done_forced")
        state.append_event("forced_advance", {"node": current})
        state.save()
    decision = decide_next_action(flow, state, target=target, node=node)
    return _apply_decision(flow, state, decision, node=node)


# Guarantees termination on a pathological (cyclic / self-looping) graph so a
# single `advance` can never hang the orchestrator. 50 is far above any real
# programmatic run of scripts/merges/joins between two orchestrator touchpoints.
_CHASE_HOP_LIMIT = 50

# Kinds that end the chase and are surfaced to the orchestrator immediately.
_CHASE_STOP_KINDS = ("choice_needed", "error", "blocked", "infra_error", "end")


def _in_branch_lineage(bid: str, root: str) -> bool:
    """True when ``bid`` is ``root`` itself, a descendant (``root.x...``), or
    an ancestor (a dotted prefix of ``root``). Siblings are NOT in lineage."""
    return bid == root or bid.startswith(root + ".") or root.startswith(bid + ".")


def _cursor_bid(state: RunState, cursor: str) -> str:
    phase = state.phases.get(cursor)
    return (phase.branch_id if phase is not None else None) or "B1"


def _next_pushable_cursor(
    flow: Flow, state: RunState, scope_bid: str | None = None,
) -> str | None:
    """Return one active cursor whose next hop is purely programmatic, or None.

    A cursor is pushable when advancing it needs no orchestrator judgment:

    - any node already in a completed status (``done`` / ``done_forced`` /
      ``done_bypassed``) — a script that ran inline, a bypassed node, or a
      force-marked node: it just needs its outgoing edge taken;
    - a ``fork`` that has landed (``in_progress``) but not yet fanned out;
    - a ``join`` that is ``ready_to_fire`` — but ONLY once no other active
      cursor still rests on one of its input nodes. Readiness is measured by
      input TERMINAL STATUS (a resting done sibling counts as "arrived"), so
      firing while an input's cursor is still parked on the input node would
      strand that cursor (and a later arrival could re-open and double-fire
      the join). Non-join progress is always preferred first for the same
      reason: drain arms into the join, then fire it.

    A cursor is NOT pushable — the chase leaves it resting/parked — when it is
    an ``agent`` / ``orchestrator`` node (orchestrator drives it), a
    ``dynamic_fanout`` / ``subflow`` node (orchestrator dispatches branches),
    or a ``join`` still waiting on arrivals (parked, not an error).

    ``scope_bid`` (set when the caller's advance named an explicit ``--node``)
    restricts non-join pushes to that node's own branch or its descendants
    WHILE any sibling-branch cursor is active — a scoped advance must never
    move a sibling arm's cursor (the envelope would then describe the
    sibling's landing, not the named node's). Once no sibling cursors remain,
    the scope no longer constrains (post-join continuation folds back into
    the parent branch and is safe to chase).

    Deterministic (sorted) so multi-cursor chases are reproducible.
    """
    # Scoped mode is only restrictive while a sibling-branch cursor is active.
    restrict_to_descendants = scope_bid is not None and any(
        not _in_branch_lineage(_cursor_bid(state, c), scope_bid)
        for c in state.current_phases
    )

    ready_joins: list[str] = []
    for c in sorted(state.current_phases):
        phase = state.phases.get(c)
        if phase is None:
            continue
        try:
            node_obj = flow.graph.node(c)
        except KeyError:
            continue
        status = phase.status
        if node_obj.runner == "join":
            if status == "ready_to_fire":
                ready_joins.append(c)
            continue
        if restrict_to_descendants:
            bid = _cursor_bid(state, c)
            if not (bid == scope_bid or bid.startswith(scope_bid + ".")):
                continue
        # A completed cursor just needs its edge taken — regardless of runner
        # (covers script-ran-inline, done_bypassed on any runner, force-marked).
        if status in COMPLETED_PHASE_STATUSES:
            return c
        if node_obj.runner == "fork" and status == "in_progress" and not phase.branches:
            return c  # fan out
    # No non-join progress available: fire a ready join whose inputs have all
    # drained cursor-wise (no active cursor still resting on an input node).
    for j in ready_joins:
        inputs = set(_join_inputs(flow.graph, j))
        if not any(c in inputs for c in state.current_phases):
            return j
    return None


def advance_autochase(
    flow: Flow,
    state: RunState,
    force: bool = False,
    target: str | None = None,
    node: str | None = None,
) -> AdvanceOutcome:
    """Advance, then keep chasing while progress stays purely programmatic.

    A single call moves as far as it can without orchestrator judgment: it
    executes script nodes, fans out forks, fires ready joins, and auto-resolves
    conditional edges (see :func:`decide_next_action`). It stops — returning the
    stopping outcome — at the first thing that needs the orchestrator: an
    ``agent`` / ``orchestrator`` node, a ``dynamic_fanout`` / ``subflow`` node,
    an unresolvable ``choice_needed``, a terminal (``end``), any gate/validation
    error, or a parked (unfired) join.

    The first hop honours the caller's ``force`` / ``target`` / ``node``
    exactly (escape hatches + explicit choice resolution). Every subsequent
    chased hop is a plain programmatic advance of a pushable cursor. The
    returned outcome carries ``chased`` — the ordered audit of hops taken —
    and, when the chase ends on a dynamic_fanout instantiation, preserves that
    outcome's ``next_startable_branches`` / ``max_concurrent_resolved``.

    Scoping rule: an explicit ``node`` scopes the chase to that node's branch
    lineage — while any sibling-branch cursor is active, only the named node's
    own branch (and its descendants) is chased, so the returned envelope
    always describes the NAMED node's progress, never a sibling arm's resting
    cursor being opportunistically pushed. A ready join is fired only once
    every input's cursor has drained into it (see
    :func:`_next_pushable_cursor`); once no sibling cursors remain the chase
    continues unrestricted (post-join, the flow has folded back into the
    parent branch). An unscoped advance (no ``node``) chases all cursors.

    ``advance()`` (single-step) is preserved verbatim as the escape hatch.
    """
    chased: list[dict] = []
    # A dynamic_fanout instantiation carries the dispatch surface the
    # orchestrator needs (next_startable_branches). If it happens mid-chase
    # (e.g. inside a fork arm) a later sibling hop would otherwise become the
    # returned outcome and drop it — so carry the most recent one forward.
    fanout_startable: list[str] | None = None
    fanout_max_concurrent: int | None = None

    def _capture_fanout(o: AdvanceOutcome) -> None:
        nonlocal fanout_startable, fanout_max_concurrent
        if o.next_startable_branches is not None:
            fanout_startable = o.next_startable_branches
            fanout_max_concurrent = o.max_concurrent_resolved

    def _finish(o: AdvanceOutcome) -> AdvanceOutcome:
        o.chased = chased
        if o.next_startable_branches is None and fanout_startable is not None:
            o.next_startable_branches = fanout_startable
            o.max_concurrent_resolved = fanout_max_concurrent
        return o

    first_node = _resolve_current(state, node)
    # Lineage scope for chased hops: captured from the NAMED node's cursor
    # before the first hop moves it. None (unscoped) when no --node was given.
    scope_bid: str | None = None
    if node is not None:
        scope_bid = _cursor_bid(state, node)
    outcome = advance(flow, state, force=force, target=target, node=node)
    _record_chase(chased, flow, first_node, outcome)
    _capture_fanout(outcome)
    if outcome.kind in _CHASE_STOP_KINDS:
        return _finish(outcome)

    hops = 0
    while True:
        pushable = _next_pushable_cursor(flow, state, scope_bid=scope_bid)
        if pushable is None:
            break
        if hops >= _CHASE_HOP_LIMIT:
            return AdvanceOutcome(
                kind="error",
                reason=(
                    f"auto-chase exceeded hop limit ({_CHASE_HOP_LIMIT}); "
                    f"suspected cycle. Visited path: "
                    f"{[c['node'] for c in chased]}"
                ),
                chased=chased,
            )
        hops += 1
        # Chased hops never re-apply force/target — those are the caller's
        # explicit intent for the first hop only.
        outcome = advance(flow, state, node=pushable)
        _record_chase(chased, flow, pushable, outcome)
        _capture_fanout(outcome)
        if outcome.kind in _CHASE_STOP_KINDS:
            return _finish(outcome)

    return _finish(outcome)


def _record_chase(
    chased: list[dict], flow: Flow, from_node: str | None, outcome: AdvanceOutcome
) -> None:
    """Append one hop to the chase audit trail."""
    runner = None
    if from_node:
        try:
            runner = flow.graph.node(from_node).runner
        except KeyError:
            runner = None
    chased.append({
        "node": from_node or None,
        "runner": runner,
        "outcome": outcome.kind,
        "target": outcome.target,
    })


def _apply_fork_fanout(
    flow: Flow,
    state: RunState,
    fork_node: str,
) -> AdvanceOutcome:
    """Fan out all successors of a fork node as parallel cursors.

    Marks the fork node done, removes its cursor, and for each successor
    creates a new cursor with a unique branch_id. See Task 7.

    Script-runner successors execute inline here — matching the single-cursor
    behaviour of ``_apply_decision`` where a ``runner=script`` landing target
    runs its script as part of the same advance. Without this, a fork branch
    that lands on a script node would sit ``in_progress`` forever, and the
    follow-up ``advance(node=succ)`` would block on the "not done" gate
    (the script never had a chance to run). Worker outputs land in the
    branch's BranchScope so the downstream all-mode join's merge can fold
    them back into the run-level variable scope. See Task 24.

    Other runner kinds (``agent``, ``orchestrator``) keep the legacy
    behaviour: stamped ``in_progress`` only, with the orchestrator driving
    execution + a later ``advance`` to transition off.
    """
    graph = flow.graph
    successors = [e.target for e in graph.edges_from(fork_node)]
    # Guard the degenerate 0-successor fork before any state mutation so a
    # bad flow fails cleanly with no partial state change (otherwise the run
    # would be left with zero cursors and no terminal).
    if not successors:
        return AdvanceOutcome(
            kind="error",
            reason=f"fork node {fork_node!r} has no successor edges",
        )
    # Mark fork done and remove its cursor.
    state.set_phase_status(fork_node, "done")
    state.remove_cursor(fork_node)
    fork_phase = state.phases[fork_node]
    # Fan out each successor. Child ids are minted run-unique under the fork's
    # own branch via mint_branch (trunk = "B1"). Spec D2.
    parent_bid = fork_phase.branch_id or "B1"
    branch_ids = []
    script_successors: list[tuple[str, str, Node]] = []
    for succ in successors:
        bid = state.mint_branch(parent_bid)
        branch_ids.append(bid)
        state.set_phase_status(succ, "in_progress")
        state.phases[succ].branch_id = bid
        state.add_cursor(succ)
        # Record the durable runtime audit entry on the fork's PhaseState.
        # These are in-run static branches, so use inline_root_node (not subflow).
        fork_phase.branches.append(
            BranchRef(branch_id=bid, inline_root_node=succ, status="in_progress")
        )
        # Seed a per-branch variable scope with a shallow copy of the parent's
        # variables at fork time. Isolation is in the scope store; workers still
        # read/write state.variables for now (Tasks 11/12 will consume these scopes).
        # ``seed`` is the same snapshot copied independently: the join merge
        # measures this branch's delta against its seed, so a post-fork trunk
        # write is not clobbered by this arm's stale value. See Task 9 / D8 row 8.
        scope = BranchScope(
            branch_id=bid,
            variables=dict(state.variables),
            parent_node=fork_node,
            seed=dict(state.variables),
        )
        state.branch_scopes[bid] = scope
        # Defer script-runner execution until after the fan_out event is
        # recorded — keeps the audit order ("fanned out, then ran each
        # script") deterministic.
        succ_node = graph.node(succ)
        if succ_node.runner == "script":
            script_successors.append((bid, succ, succ_node))
    state.append_event("fork_fanned_out", {
        "node": fork_node,
        "branches": successors,
        "branch_ids": branch_ids,
    })
    state.save()

    # Execute each script-runner branch inline using its branch scope as the
    # variable view. On the first failure, abort and surface the error —
    # matching the single-cursor script-node failure shape in _apply_decision.
    # Successful branches before the failure remain `done` with their outputs
    # recorded in the branch scope.
    for bid, succ, succ_node in script_successors:
        branch_vars = state.branch_scopes[bid].variables
        err = _execute_script_node(
            flow, state, succ_node,
            vars_target=branch_vars, phase_override=succ,
        )
        if err is not None:
            return err
        # Mirror the now-done branch status onto the fork's BranchRef.
        for ref in fork_phase.branches:
            if ref.branch_id == bid:
                ref.status = branch_status_from_phase(state.phases[succ].status)
                break
    if script_successors:
        # _execute_script_node already saved; this final save is redundant
        # but cheap and keeps the contract "fan-out persists everything
        # before returning" obvious to readers.
        state.save()
    return AdvanceOutcome(kind="moved", target=None)


# ---------------------------------------------------------------------------
# Dynamic fanout instantiation (Task 19)
# ---------------------------------------------------------------------------

def _resolve_max_concurrent(
    fanout_node: "Node",
    flow_stem: str,
    prefs_path: Path,
) -> int:
    """Resolve max_concurrent in order: factory-prefs > DOT attribute > default 3.

    Returns 0 to mean "unbounded" (caller treats as no cap).
    """
    import yaml
    if prefs_path.exists():
        try:
            prefs = yaml.safe_load(prefs_path.read_text()) or {}
        except yaml.YAMLError:
            prefs = {}
        flows = prefs.get("flows", {}) or {}
        if flow_stem in flows:
            override = flows[flow_stem].get("max_concurrent")
            if override is not None:
                return int(override)
    if fanout_node.max_concurrent is not None:
        return fanout_node.max_concurrent
    return 3


def _compute_next_startable_branches(
    phase: PhaseState,
    max_concurrent: int,
) -> list[str]:
    """Return the FIFO list of branch_ids ready to transition pending → in_progress.

    Idle branches do not count toward the active total — their slot is freed
    (the orchestrator is fielding a human prompt; no live worker).

    max_concurrent=0 means unbounded; every pending branch is returned.
    """
    active = sum(1 for b in phase.branches if b.status == "in_progress")
    pending = [b.branch_id for b in phase.branches if b.status == "pending"]
    if max_concurrent == 0:
        return pending
    slots = max(0, max_concurrent - active)
    return pending[:slots]


# ---------------------------------------------------------------------------
# Idle transitions for runner=orchestrator + pauses_at_min nodes (Task 8 /
# spec §4.4 of 2026-06-05-batch-factory-pipeline-design)
# ---------------------------------------------------------------------------

# Supervision rank derived from the canonical ``VALID_SUPERVISION_LEVELS``
# tuple in ``parser.py`` so the two never drift. The tuple is ordered
# ``("afk", "low", "medium", "high")`` — increasing strictness — so the
# enumerate index gives the same afk(0) < low(1) < medium(2) < high(3)
# rank that ``pauses_at_min`` comparisons require. If a new level is added,
# fix the order at the source in ``VALID_SUPERVISION_LEVELS`` rather than
# here.
_SUPERVISION_ORDER = {lvl: i for i, lvl in enumerate(VALID_SUPERVISION_LEVELS)}


def _should_enter_idle(
    node: "Node",
    supervision: str,
    bypass_at_match: bool,
) -> bool:
    """Return True iff the orchestrator is about to field a human-input
    prompt at this node, in which case the branch's BranchRef should be
    flipped to ``idle`` so its slot under ``max_concurrent`` frees for
    rolling-window fanout. Spec §4.4 conjunction:

      runner == "orchestrator"
      AND pauses_at_min is set (not None)
      AND current supervision >= pauses_at_min
      AND bypass_at does NOT match current supervision

    ``bypass_at_match=True`` means flowstate's existing bypass logic has
    already short-circuited in ``_apply_decision``; in that case the
    inline orchestrator branch is never entered, so idle never applies.
    """
    if node.runner != "orchestrator":
        return False
    if not node.pauses_at_min:
        return False
    if bypass_at_match:
        return False
    return (
        _SUPERVISION_ORDER.get(supervision, -1)
        >= _SUPERVISION_ORDER.get(node.pauses_at_min, 99)
    )


def _enter_idle(parent_run_dir: Path, fanout_node: str, branch_id: str) -> None:
    """Flip the BranchRef ``status`` from ``in_progress`` to ``idle`` on the
    parent run's dynamic_fanout PhaseState. Appends a ``branch_idle_entered``
    event. No-op if the BranchRef is not currently ``in_progress`` (idempotent
    on re-entry).

    Acquires the PARENT's ``state_lock`` for the read-modify-write cycle.
    Caller is the CHILD's advance path, which already holds the child's
    state_lock — (child → parent) is the only safe acquisition order per
    the LOCK ORDERING INVARIANT (I-6) on ``subflow.complete_subflow``.
    """
    parent_run_dir = Path(parent_run_dir)
    with state_lock(parent_run_dir):
        parent = RunState.load(parent_run_dir)
        phase = parent.phases.get(fanout_node)
        if phase is None:
            return
        for b in phase.branches:
            if b.branch_id == branch_id and b.status == "in_progress":
                b.status = "idle"
                parent.append_event("branch_idle_entered", {
                    "node": fanout_node,
                    "branch_id": branch_id,
                })
                parent.save()
                return


def _exit_idle(parent_run_dir: Path, fanout_node: str, branch_id: str) -> None:
    """Flip the BranchRef ``status`` from ``idle`` back to ``in_progress`` on
    the parent run's dynamic_fanout PhaseState. Appends a ``branch_idle_exited``
    event. No-op if the BranchRef is not currently ``idle``.

    See ``_enter_idle`` for lock-ordering rationale.
    """
    parent_run_dir = Path(parent_run_dir)
    with state_lock(parent_run_dir):
        parent = RunState.load(parent_run_dir)
        phase = parent.phases.get(fanout_node)
        if phase is None:
            return
        for b in phase.branches:
            if b.branch_id == branch_id and b.status == "idle":
                b.status = "in_progress"
                parent.append_event("branch_idle_exited", {
                    "node": fanout_node,
                    "branch_id": branch_id,
                })
                parent.save()
                return


def _apply_dynamic_fanout(
    flow: Flow,
    state: RunState,
    fanout_node: Node,
) -> AdvanceOutcome:
    """Instantiate one branch per item in state.variables[source_var].

    Does NOT call agentctl / init_subflow_run — that is the orchestrator's
    responsibility.  This function only performs flowstate-side bookkeeping:
    - Validates the source variable is a list (error if not). [C3]
    - Errors if the source list is empty. [C3]
    - Respects max_branches; returns AdvanceOutcome(kind='error') if exceeded. [I4]
    - Creates BranchRef (status="pending") + BranchScope per item (1-indexed).
    - Keeps the fanout cursor in place (in_progress; not removed).
    - Emits a "dynamic_fanout" event including the minted branch_ids and saves. [I5]

    On any error path, the fanout phase status is set to "error" before saving.
    See Task 19 (original) and Tasks 14/15/16 (C3, I4, I5).
    """
    name = fanout_node.name
    source_var = fanout_node.source_var or ""
    items = state.variables.get(source_var)

    if not isinstance(items, list):
        state.set_phase_status(name, "error")
        state.save()
        return AdvanceOutcome(
            kind="error",
            reason=(
                f"dynamic_fanout {name!r}: source variable {source_var!r} "
                f"is not a list (got {type(items).__name__})"
            ),
        )

    if len(items) == 0:
        state.set_phase_status(name, "error")
        state.append_event("dynamic_fanout_empty_source", {"node": name, "source": source_var})
        state.save()
        return AdvanceOutcome(
            kind="error",
            reason=(
                f"dynamic_fanout {name!r}: source variable {source_var!r} "
                f"resolved to empty list"
            ),
        )

    if fanout_node.max_branches is not None and len(items) > fanout_node.max_branches:
        state.set_phase_status(name, "error")
        state.save()
        return AdvanceOutcome(
            kind="error",
            reason=(
                f"dynamic_fanout {name!r}: would create {len(items)} branches "
                f"> max_branches={fanout_node.max_branches}"
            ),
        )

    # Ensure the PhaseState exists (it should already be in_progress from the
    # structural-runner landing interception in _apply_decision, but guard anyway).
    fan_phase = state.phases.setdefault(name, PhaseState(status="in_progress"))

    # T22 / I1: pre-check every item against the 16 KB ceiling before any
    # state mutation. Returning AdvanceOutcome.error here (rather than after
    # partial branch-scope writes) keeps the fail-clean contract: a bad item
    # leaves the fanout phase in error with zero stamped branches.
    for i, item in enumerate(items, start=1):
        try:
            check_var_size("item", item)
        except ValueError as exc:
            state.set_phase_status(name, "error")
            state.save()
            return AdvanceOutcome(
                kind="error",
                reason=(
                    f"dynamic_fanout {name!r}: item #{i} exceeds 16 KB ceiling: {exc}"
                ),
            )

    # Child ids are minted run-unique under the fanout's own branch (trunk =
    # "B1") so a fork upstream and this fanout can never collide. Spec D2.
    parent_bid = fan_phase.branch_id or "B1"
    new_branches: list[BranchRef] = []
    for item in items:
        bid = state.mint_branch(parent_bid)
        # Per-branch variable scope: shallow copy of parent vars + item binding.
        # ``seed`` is the same initial content copied independently so the join
        # merge measures this branch's delta against its own seed (including
        # the ``item`` binding). See Task 9 / spec D8 row 8.
        scope_vars = {**state.variables, "item": item}
        state.branch_scopes[bid] = BranchScope(
            branch_id=bid,
            variables=scope_vars,
            parent_node=name,
            seed={**state.variables, "item": item},
        )
        ref = BranchRef(
            branch_id=bid,
            status="pending",
            inline_root_node=None,
            subflow_run_dir=None,
        )
        fan_phase.branches.append(ref)
        new_branches.append(ref)

    # Keep the fanout node in_progress; its cursor stays put.
    fan_phase.status = "in_progress"

    state.append_event("dynamic_fanout", {
        "node": name,
        "count": len(items),
        "source": source_var,
        "branch_ids": [b.branch_id for b in new_branches],
    })
    state.save()
    return AdvanceOutcome(
        kind="moved",
        target=None,
        reason=f"fanned out {len(items)} branches",
    )


# ---------------------------------------------------------------------------
# Dynamic fanout completion (Task 20)
# ---------------------------------------------------------------------------

def _apply_dynamic_fanout_completion(
    flow: Flow,
    state: RunState,
    fanout_node: Node,
) -> AdvanceOutcome:
    """Complete a dynamic_fanout whose branches have all been stamped.

    If all branches are terminal, marks the fanout done, removes its cursor,
    and advances to the single downstream successor.  If any branch is still
    non-terminal, returns blocked (no state mutation).

    Edge-case handling:
    - 0 downstream edges: marks fanout done + returns end.
    - >1 downstream edges: marks fanout done + returns error (the fanout node
      was already in_progress so marking done here is safe; stranding cursors
      would be worse than surfacing the flow-authoring error).

    See Task 20.
    """
    name = fanout_node.name
    phase = state.phases[name]

    # Check all branches for terminal status. BranchRef.status uses the
    # BranchStatus vocabulary; an errored branch is terminal (finished) so it
    # must never block fanout completion — it is surfaced via
    # `errored_branches` instead. idle is NOT terminal. Spec D1.
    non_terminal = [
        b.branch_id for b in phase.branches
        if not branch_is_terminal(b.status)
    ]
    if non_terminal:
        return AdvanceOutcome(
            kind="blocked",
            reason=f"fanout {name!r} awaiting branches: {non_terminal}",
        )

    # All branches are terminal — complete the fanout. Errored branches are
    # surfaced, not blocking: the orchestrator reads `errored_branches` off
    # the event / reason and decides whether to proceed, rewind, or abort.
    errored = [
        {"branch_id": b.branch_id, "failure_reason": b.failure_reason}
        for b in phase.branches
        if b.status == "error"
    ]
    errored_suffix = (
        f"; {len(errored)} branch(es) errored: {[e['branch_id'] for e in errored]}"
        if errored else ""
    )
    state.set_phase_status(name, "done")
    state.remove_cursor(name)

    out_edges = flow.graph.edges_from(name)
    branch_ids = [b.branch_id for b in phase.branches]

    if not out_edges:
        state.append_event("dynamic_fanout_complete", {
            "node": name,
            "branches": branch_ids,
            "errored_branches": errored,
        })
        state.save()
        return AdvanceOutcome(
            kind="end",
            reason=f"fanout {name!r} complete, no downstream{errored_suffix}",
        )

    if len(out_edges) > 1:
        state.append_event("dynamic_fanout_complete", {
            "node": name,
            "branches": branch_ids,
            "errored_branches": errored,
        })
        state.save()
        return AdvanceOutcome(
            kind="error",
            reason=(
                f"fanout {name!r} has {len(out_edges)} downstream edges; "
                f"expected exactly 1"
            ),
        )

    downstream = out_edges[0].target
    ds = state.phases.setdefault(downstream, PhaseState())
    ds.status = "in_progress"
    if ds.started_at is None:
        ds.started_at = iso_now()
    state.add_cursor(downstream)

    state.append_event("dynamic_fanout_complete", {
        "node": name,
        "branches": branch_ids,
        "errored_branches": errored,
    })
    state.save()
    return AdvanceOutcome(
        kind="moved",
        target=downstream,
        reason=f"fanout {name!r} complete; advanced to {downstream!r}{errored_suffix}",
    )


# ---------------------------------------------------------------------------
# Subflow completion (Task 18)
# ---------------------------------------------------------------------------

def _apply_subflow_completion(
    flow: Flow,
    state: RunState,
    subflow_node: Node,
) -> AdvanceOutcome:
    """Handle a landing on a subflow node whose child run may have completed.

    Reads the child run dir from the subflow node's PhaseState.branches[0]
    (stamped by ``init_subflow_run``). If the child has not yet reached
    ``state == "completed"``, returns an explicit ``blocked`` outcome so the
    orchestrator can poll again. On completion, harvests the declared
    ``subflow_outputs`` into the parent's variable scope via ``set_var``
    (size lint enforced), marks the subflow node ``done``, flips the
    BranchRef status to ``done``, and advances to the single successor as a
    normal completed node would.
    """
    name = subflow_node.name

    # A subflow node that is used as a `dynamic_fanout`'s template is NOT a
    # standalone subflow — its BranchRefs live on the fanout node, not on
    # this node. When advance walks the parent's edges and lands here
    # (because the DOT typically has `<fanout> -> <template> -> <successor>`
    # for graph-rendering purposes), there's nothing for THIS node to do.
    # Auto-skip: mark done and advance to the single successor as if the
    # subflow had completed normally. Detected by scanning the flow for any
    # dynamic_fanout whose `template_node` points at this node.
    is_fanout_template = any(
        n.runner == "dynamic_fanout" and n.template_node == name
        for n in flow.graph.nodes
    )

    phase = state.phases.get(name)
    # M3: differentiate the blocked reasons so the orchestrator can tell
    # the three failure shapes apart — no PhaseState at all, PhaseState
    # with no BranchRef yet, BranchRef present but child run dir is None.
    if phase is None:
        return AdvanceOutcome(
            kind="blocked",
            reason=(
                f"subflow {name!r}: PhaseState not registered "
                f"(advance has not landed on this node yet)"
            ),
        )
    if is_fanout_template and not phase.branches:
        # Pass-through case: this is a fanout template, the fanout has done
        # its own dispatch + merge, so just mark the node done and advance
        # to the single successor.
        state.set_phase_status(name, "done")
        state.append_event("subflow_template_passthrough", {"node": name})
        outgoing = flow.graph.edges_from(name)
        if len(outgoing) != 1:
            state.save()
            return AdvanceOutcome(
                kind="error",
                reason=(
                    f"subflow template {name!r}: expected exactly one outgoing edge "
                    f"to auto-advance through; got {[e.target for e in outgoing]}"
                ),
            )
        downstream = outgoing[0].target
        state.current_phases.discard(name)
        state.current_phases.add(downstream)
        state.save()
        return AdvanceOutcome(
            kind="moved",
            target=downstream,
            reason=f"subflow template {name!r} passed through to {downstream!r}",
        )
    if not phase.branches:
        return AdvanceOutcome(
            kind="blocked",
            reason=(
                f"subflow {name!r}: no BranchRef registered "
                f"(was init_subflow_run called?)"
            ),
        )
    branch_ref = phase.branches[0]
    if not branch_ref.subflow_run_dir:
        return AdvanceOutcome(
            kind="blocked",
            reason=(
                f"subflow {name!r}: BranchRef exists but subflow_run_dir is None "
                f"(init incomplete)"
            ),
        )

    from flowstate.subflow import harvest_subflow_outputs
    child_run_dir = Path(branch_ref.subflow_run_dir)

    # Load the child and check its lifecycle state BEFORE harvesting. Doing
    # the load-then-check first eliminates a narrow TOCTOU window where the
    # child transitions to completed between two separate loads (the prior
    # ordering called harvest first and re-loaded the child only when
    # harvested was empty, which could mis-classify a just-completed child
    # with no declared outputs as "still in progress"). harvest_subflow_outputs
    # ALSO checks state internally — defence in depth.
    #
    # Lock-ordering note (I-6): we read the child's state WITHOUT acquiring
    # the child's state_lock. This is correct because we already hold the
    # PARENT's state_lock (via cmd_advance) and acquiring the child's would
    # violate the cross-run lock ordering invariant (only child → parent is
    # safe; see LOCK ORDERING INVARIANT on subflow.complete_subflow). The
    # child's state file is read-only from the parent's perspective — the
    # child's own advance is the only writer.
    child = RunState.load(child_run_dir)
    if child.state != "completed":
        return AdvanceOutcome(
            kind="blocked",
            reason=f"subflow {name!r} child not yet terminal",
        )

    harvested = harvest_subflow_outputs(child_run_dir, subflow_node.subflow_outputs)

    # Child is completed (either with harvested vars or with no declared
    # outputs). Merge harvested values into the parent scope via set_var so
    # the underscore guard + size ceiling fire as appropriate.
    #
    # Guard: when the push-driven hook (D9 / T12) has already run on this
    # branch — signalled by ``branch_ref.status == "done"`` — the harvested
    # vars are already in ``parent.branch_scopes[bid].variables`` and must
    # NOT be written a second time to top-level ``state.variables`` here.
    # Dual-write would silently put the same value in both scopes, creating
    # a real inconsistency for subflows-in-fanouts feeding joins (the join
    # merge would then double-fold the value). The cursor + status mutation
    # below is still required either way.
    if branch_ref.status != "done":
        for parent_var, value in harvested.items():
            state.set_var(parent_var, value)

    # Mark the subflow node done and flip the BranchRef status.
    state.set_phase_status(name, "done")
    branch_ref.status = "done"

    # Advance to the single successor as a normal completed node would.
    out_edges = flow.graph.edges_from(name)
    if not out_edges:
        # No downstream — terminal subflow. Save and end.
        state.completed_at = iso_now()
        state.state = "completed"
        state.append_event("subflow_harvested", {
            "node": name,
            "child_run_dir": str(child_run_dir),
            "outputs_merged": sorted(harvested.keys()),
        })
        state.append_event("run_completed", {})
        state.save()
        _maybe_push_subflow_completion(state)
        return AdvanceOutcome(kind="end", target=None)
    if len(out_edges) > 1:
        # Subflow nodes are single-output by construction. A multi-edge
        # subflow node is a flow-authoring error; surface it explicitly.
        return AdvanceOutcome(
            kind="error",
            reason=(
                f"subflow {name!r} has {len(out_edges)} outgoing edges; "
                f"expected exactly 1"
            ),
        )
    downstream = out_edges[0].target
    ds = state.phases.setdefault(downstream, PhaseState())
    ds.status = "in_progress"
    if ds.started_at is None:
        ds.started_at = iso_now()
    state.add_cursor(downstream)
    state.remove_cursor(name)
    state.append_event("subflow_harvested", {
        "node": name,
        "child_run_dir": str(child_run_dir),
        "outputs_merged": sorted(harvested.keys()),
    })
    state.save()
    return AdvanceOutcome(kind="moved", target=downstream)


# ---------------------------------------------------------------------------
# Join helpers (Task 8)
# ---------------------------------------------------------------------------

def _join_inputs(graph: FlowGraph, join_name: str) -> list[str]:
    """Return the static input set: all nodes with an edge into the join."""
    return [e.source for e in graph.edges if e.target == join_name]


def _arrived_inputs(state: RunState, inputs: list[str]) -> list[str]:
    """Return the subset of inputs that have ARRIVED — i.e. reached a terminal
    status (finished, successfully or not), not merely completed successfully.
    An errored input counts as arrived so it never blocks the join. Spec D1."""
    return [
        n for n in inputs
        if (p := state.phases.get(n)) is not None and phase_is_terminal(p.status)
    ]


def _fire_reducer(
    state: RunState,
    flow: Flow,
    *,
    join_name: str,
    triggering_branch_id: str,
    triggered_from_node: str | None = None,
) -> None:
    """Run the reducer script for one branch arrival; update summary_var
    atomically on success.

    Per design D1 / D4:
    - Fires once per branch arrival.
    - Reducer env carries parent vars + the triggering branch's scope vars
      (branch wins on key conflicts) + FLOWSTATE_BRANCH_ID.
    - On non-zero exit OR parse error: the join's PhaseState status flips
      to ``"error"``, the failure is recorded on a JoinFiring, an event is
      appended, and ``summary_var`` stays at its prior value.
    - On success: parsed FLOWSTATE_OUTPUT_* writes are committed via
      ``state.set_var`` (size lint + underscore guard apply), a JoinFiring
      with ``_outputs`` is appended, and a ``reducer_fired`` event is
      recorded.

    Does not return anything; mutates ``state`` and persists via
    ``state.save()`` exactly once per call.
    """
    from flowstate.reducer import ReducerParseError, parse_reducer_stdout

    join_node = flow.graph.node(join_name)
    script_path = (Path(flow.flow_dir) / join_node.reducer_script).resolve()

    # Find a phase whose branch_id matches the triggering branch — used to
    # capture the branch's current PhaseStatus in the JoinFiring. Defaults to
    # "done" because arrival at a join implies the branch had reached a
    # terminal status.
    branch_status = "done"
    for _name, _phase in state.phases.items():
        if _phase.branch_id == triggering_branch_id:
            branch_status = _phase.status
            break

    # Build env: parent scope + branch scope + branch id. Branch wins on
    # conflicts (set after parent so it overwrites).
    env = {**os.environ}
    env["FLOWSTATE_RUN_DIR"] = str(state.run_dir)
    env["FLOWSTATE_FLOW_DIR"] = str(state.repo_root / state.flow_dir)
    env["FLOWSTATE_PHASE"] = join_name
    env["FLOWSTATE_BRANCH_ID"] = triggering_branch_id

    def _set_var_env(k: str, v: object) -> None:
        if v is None or v == "":
            return
        env[f"FLOWSTATE_VAR_{k}"] = v if isinstance(v, str) else json.dumps(v)

    for k, v in state.variables.items():
        _set_var_env(k, v)
    branch_scope = state.branch_scopes.get(triggering_branch_id)
    if branch_scope is not None:
        for k, v in branch_scope.variables.items():
            _set_var_env(k, v)

    # NOTE: This subprocess runs INSIDE the parent's state_lock (held by
    # cmd_advance via _record_join_arrival, or by complete_subflow's CS2).
    # Spec D9's original "outside the lock" design was amended on 2026-06-04
    # to match the implemented reality — see the "Tradeoff" paragraph in
    # docs/superpowers/specs/2026-06-03-flowstate-multi-branch-fixes-design.md
    # for the N>20 contention story.
    try:
        result = subprocess.run(
            [str(script_path)],
            env=env,
            capture_output=True,
            text=True,
            timeout=SCRIPT_TIMEOUT_SECONDS,
        )
    except subprocess.TimeoutExpired:
        # Treat a timeout like a non-zero exit: record failure, flip to error.
        firing = JoinFiring(
            sequence=len(state.phases[join_name].join_history) + 1,
            triggered_by=triggering_branch_id,
            triggered_by_status=branch_status,
            input_delta={"_reducer_error": f"timed out after {SCRIPT_TIMEOUT_SECONDS}s"},
            fired_at=iso_now(),
            triggered_from_node=triggered_from_node,
        )
        state.phases[join_name].join_history.append(firing)
        state.append_event("reducer_failed", {
            "node": join_name,
            "branch_id": triggering_branch_id,
            "exit_code": -1,
        })
        state.set_phase_status(join_name, "error")
        state.save()
        return
    except (FileNotFoundError, PermissionError, OSError) as exc:
        # OS-level launch failure (missing script, not executable, etc.):
        # route through the same failure path as a non-zero exit so the join
        # never lands in a half-mutated state (join_arrival already saved by
        # _record_join_arrival; we must still append a JoinFiring and flip
        # the phase to error).
        err = f"reducer launch failed: {type(exc).__name__}: {str(exc)[:400]}"
        firing = JoinFiring(
            sequence=len(state.phases[join_name].join_history) + 1,
            triggered_by=triggering_branch_id,
            triggered_by_status=branch_status,
            input_delta={"_reducer_error": err},
            fired_at=iso_now(),
            triggered_from_node=triggered_from_node,
        )
        state.phases[join_name].join_history.append(firing)
        state.append_event("reducer_failed", {
            "node": join_name,
            "branch_id": triggering_branch_id,
            "error": err,
        })
        state.set_phase_status(join_name, "error")
        state.save()
        return

    if result.returncode != 0:
        firing = JoinFiring(
            sequence=len(state.phases[join_name].join_history) + 1,
            triggered_by=triggering_branch_id,
            triggered_by_status=branch_status,
            input_delta={"_reducer_error": (result.stderr or "").strip()[:500]},
            fired_at=iso_now(),
            triggered_from_node=triggered_from_node,
        )
        state.phases[join_name].join_history.append(firing)
        state.append_event("reducer_failed", {
            "node": join_name,
            "branch_id": triggering_branch_id,
            "exit_code": result.returncode,
        })
        state.set_phase_status(join_name, "error")
        state.save()
        return

    try:
        outputs = parse_reducer_stdout(result.stdout)
    except ReducerParseError as exc:
        firing = JoinFiring(
            sequence=len(state.phases[join_name].join_history) + 1,
            triggered_by=triggering_branch_id,
            triggered_by_status=branch_status,
            input_delta={"_reducer_parse_error": str(exc)},
            fired_at=iso_now(),
            triggered_from_node=triggered_from_node,
        )
        state.phases[join_name].join_history.append(firing)
        state.append_event("reducer_parse_error", {
            "node": join_name,
            "branch_id": triggering_branch_id,
            "error": str(exc),
        })
        state.set_phase_status(join_name, "error")
        state.save()
        return

    # Success: commit parsed outputs atomically (within one save() cycle).
    # ``state.set_var`` enforces two invariants the parser does not pre-check:
    # underscore-prefixed keys are rejected (system-reserved) and values
    # serialising to >16 KB are rejected. If a multi-output reducer trips
    # either check on the 2nd+ key, the earlier set_var writes have already
    # mutated state.variables — route to a symmetric failure path so we
    # never leave a JoinFiring un-appended on a half-written state.
    try:
        for k, v in outputs.items():
            state.set_var(k, v)
    except ValueError as exc:
        err = str(exc)[:500]
        firing = JoinFiring(
            sequence=len(state.phases[join_name].join_history) + 1,
            triggered_by=triggering_branch_id,
            triggered_by_status=branch_status,
            input_delta={"_reducer_invalid_output": err},
            fired_at=iso_now(),
            triggered_from_node=triggered_from_node,
        )
        state.phases[join_name].join_history.append(firing)
        state.append_event("reducer_invalid_output", {
            "node": join_name,
            "branch_id": triggering_branch_id,
            "error": err,
        })
        state.set_phase_status(join_name, "error")
        state.save()
        return
    firing = JoinFiring(
        sequence=len(state.phases[join_name].join_history) + 1,
        triggered_by=triggering_branch_id,
        triggered_by_status=branch_status,
        input_delta={"_outputs": sorted(outputs.keys())},
        fired_at=iso_now(),
        triggered_from_node=triggered_from_node,
    )
    state.phases[join_name].join_history.append(firing)
    state.append_event("reducer_fired", {
        "node": join_name,
        "branch_id": triggering_branch_id,
        "output_keys": sorted(outputs.keys()),
    })
    state.save()


def _record_join_arrival(
    flow: Flow,
    state: RunState,
    join_name: str,
    from_node: str,
) -> AdvanceOutcome:
    """Record that a branch node has arrived at a join.

    - Adds the join cursor; removes the branch cursor.
    - Initialises the join's PhaseState to in_progress on first arrival.
    - Flips to ready_to_fire when every input is done (joins are always
      all-mode for downstream per the 2026-06-03 design).
    - Saves and returns an AdvanceOutcome(moved, target=join_name).
    """
    graph = flow.graph
    join_phase = state.phases.setdefault(join_name, PhaseState())

    join_node = graph.node(join_name)

    if join_phase.status == "pending":
        join_phase.status = "in_progress"
        if join_phase.started_at is None:
            join_phase.started_at = iso_now()

    # Cursor bookkeeping: use add/remove, never the setter, so sibling cursors
    # are preserved.
    state.add_cursor(join_name)
    state.remove_cursor(from_node)

    inputs = _join_inputs(graph, join_name)
    arrived = _arrived_inputs(state, inputs)

    if set(arrived) == set(inputs):
        join_phase.status = "ready_to_fire"

    state.append_event("join_arrival", {
        "join": join_name,
        "from": from_node,
        "arrived": arrived,
    })
    state.save()

    # Per-arrival reducer firing (decision D1). Runs for every arrival when
    # the join declares both `reducer_script` and `summary_var`. This is
    # additive to downstream firing in `_fire_join` (Task 4). The audit now
    # carries the REAL branch id (B1.1 / B1.2 / ..., trunk = "B1"), recovered
    # from the arriving node's PhaseState; the human-readable source node is
    # threaded separately via triggered_from_node. Spec D2.
    if join_node.reducer_script and join_node.summary_var:
        _bid = state.phases[from_node].branch_id
        triggering_branch_id = _bid or "B1"
        _fire_reducer(
            state, flow,
            join_name=join_name,
            triggering_branch_id=triggering_branch_id,
            triggered_from_node=from_node,
        )

    return AdvanceOutcome(kind="moved", target=join_name)


def _branch_delta(
    scope_vars: dict,
    seed: dict,
    reducer_owned_keys: set[str],
) -> dict:
    """A branch's merge contribution: the keys it changed relative to its OWN
    seed (the snapshot taken at branch creation), NOT relative to the parent's
    current values. Measuring against the seed is what stops a post-fork trunk
    write from being clobbered by an arm's stale fork-time value at the join.
    See Task 9 / spec D8 row 8.

    Excludes:
      - underscore-prefixed keys — load-bearing: after a first join fires the
        trunk scope carries the reserved ``_branches`` key, which under
        seed-based deltas would enter a later trunk-input delta and trip
        ``merge_branch_scopes``'s reserved-key ValueError. Excluding them
        preserves the pre-change behavior (system keys never entered deltas,
        because they always equalled the parent's current value).
      - reducer-owned keys (the join's ``summary_var``) — the seed rule
        largely subsumes this (the branch's snapshot equals its seed, since the
        per-arrival reducer mutates the PARENT scope, not the branch), but the
        explicit exclusion is kept as a belt-and-braces guard; stage 2
        consolidates.
    """
    return {
        k: v for k, v in scope_vars.items()
        if not k.startswith("_")
        and k not in reducer_owned_keys
        and (k not in seed or seed.get(k) != v)
    }


def _fire_join(
    flow: Flow,
    state: RunState,
    join_node: Node,
) -> AdvanceOutcome:
    """Fire a join: activate its single downstream exactly once.

    Per D1/D12 of the multi-branch fixes design, downstream firing is now
    unconditional "wait for all branches to drain, then fire once" — there
    is no longer a per-arrival downstream-firing path. Reducer firing (T4)
    is independent and continues to run per arrival in
    `_record_join_arrival`.

    Before activating the downstream node, every contributing branch's
    variable delta is merged into state.variables via merge_branch_scopes,
    ordered by completion time.
    """
    jname = join_node.name
    out_edges = flow.graph.edges_from(jname)
    if not out_edges:
        state.set_phase_status(jname, "done")
        state.remove_cursor(jname)
        state.save()
        return AdvanceOutcome(kind="end", reason=f"join {jname!r} has no downstream")
    if len(out_edges) > 1:
        return AdvanceOutcome(
            kind="error",
            reason=f"join {jname!r} has {len(out_edges)} outgoing edges; expected exactly 1",
        )
    downstream = out_edges[0].target

    # Block until every input has arrived. _fire_join verifies the
    # "all-drained" precondition itself rather than trusting status alone.
    inputs = _join_inputs(flow.graph, jname)
    arrived = set(_arrived_inputs(state, inputs))
    missing = [n for n in inputs if n not in arrived]
    if missing:
        return AdvanceOutcome(
            kind="blocked",
            reason=f"join {jname!r} waiting on {sorted(missing)}",
        )

    # Errored inputs ARRIVED (terminal, not successful): they never block the
    # join, but they are surfaced so the orchestrator can react, and their
    # stale cursors — parked at the errored node — are cleaned up. Spec D1.
    errored_inputs = [n for n in inputs if state.phases[n].status == "error"]
    errored: list[dict] = []
    for n in errored_inputs:
        bid = state.phases[n].branch_id or "B1"
        # failure_reason travels on the fork phase's matching BranchRef.
        failure_reason = None
        for p in state.phases.values():
            for ref in p.branches:
                if ref.branch_id == bid:
                    failure_reason = ref.failure_reason
                    break
            if failure_reason is not None:
                break
        errored.append(
            {"branch_id": bid, "node": n, "failure_reason": failure_reason}
        )
        state.remove_cursor(n)
    errored_suffix = (
        f"; {len(errored)} branch(es) errored: {[e['branch_id'] for e in errored]}"
        if errored else ""
    )

    # All inputs drained → fire downstream exactly once.
    # Merge branch scopes into run-level variables before activating
    # downstream. All deltas are measured against the same pre-merge run
    # scope: build every branch tuple first (state.variables is not
    # reassigned inside the loop), then merge once.
    ordered = sorted(inputs, key=lambda n: state.phases[n].completed_at or "")
    # Reducer-owned keys (the join's summary_var) are maintained by per-arrival
    # reducer firings directly in parent scope. The branch scope still holds
    # the stale fork-time snapshot of that key, so if we let it enter the
    # delta, merge_branch_scopes would clobber the reducer's accumulated
    # value with the stale snapshot. Exclude such keys from every branch's
    # delta computation.
    reducer_owned_keys: set[str] = set()
    if join_node.summary_var:
        reducer_owned_keys.add(join_node.summary_var)
    parent_vars = state.variables
    branch_tuples: list[tuple[str, str, str, dict]] = []
    for n in ordered:
        # Scope lookups are ALWAYS keyed by branch id (trunk = B1). The node
        # name is audit-only: it travels in the tuple's second slot and lands
        # in _branches[].source_node. Spec D8 row 4.
        #
        # The delta is measured against the branch's OWN seed (the snapshot at
        # branch creation), NOT the parent's current values — otherwise an
        # arm's stale fork-time value (e.g. a declared var still at its None
        # default when the fork fanned out) would differ from a post-fork
        # trunk write and clobber it at the merge. B1's seed is the run's
        # initial variables, so its delta re-applies its own post-init writes
        # onto itself (harmless no-ops) — one uniform path. Spec D8 row 8.
        bid = state.phases[n].branch_id or "B1"
        scope = state.branch_scopes.get(bid)
        if scope is None:
            delta: dict = {}
        else:
            delta = _branch_delta(scope.variables, scope.seed, reducer_owned_keys)
        branch_tuples.append((bid, n, state.phases[n].status, delta))
    try:
        state.variables = merge_branch_scopes(
            parent_vars, branch_tuples, strategies=flow.merge_strategies,
        )
    except ValueError as exc:
        # T22 / I1: a merged value exceeded the 16 KB ceiling (e.g. two
        # branches each contributed an in-bounds value that list_append
        # concatenated past the limit). Flip the join to error so the
        # operator can intervene; preserve the prior parent_vars view.
        state.set_phase_status(jname, "error")
        state.append_event("join_merge_oversize", {
            "join": jname,
            "error": str(exc)[:500],
        })
        state.save()
        return AdvanceOutcome(
            kind="error",
            reason=f"join {jname!r} merge exceeded 16 KB ceiling: {exc}",
        )

    state.set_phase_status(jname, "done")
    state.remove_cursor(jname)
    ds = state.phases.setdefault(downstream, PhaseState())
    ds.status = "in_progress"
    if ds.started_at is None:
        ds.started_at = iso_now()
    state.add_cursor(downstream)
    state.append_event("join_fired", {
        "join": jname,
        "downstream": downstream,
        "errored_branches": errored,
    })
    state.save()
    # Mirror the script-on-landing behaviour of _apply_decision and
    # _apply_fork_fanout: if the join's single downstream is a
    # ``runner=script`` node, execute its script inline using the
    # post-merge run-level scope. Without this, a join-downstream
    # script node would sit ``in_progress`` and the orchestrator's
    # next ``advance(node=downstream)`` would block on "not done"
    # (the same bug pattern Task 24 fixed for fork branches).
    downstream_node = flow.graph.node(downstream)
    if downstream_node.runner == "script":
        err = _execute_script_node(flow, state, downstream_node)
        if err is not None:
            return err
    return AdvanceOutcome(
        kind="moved",
        target=downstream,
        reason=f"join {jname!r} fired -> {downstream!r}{errored_suffix}",
    )


def _apply_decision(
    flow: Flow,
    state: RunState,
    decision: Decision,
    node: str | None = None,
) -> AdvanceOutcome:
    """Commit the :class:`Decision`: persist force-mark if needed, run
    transition scripts, dispatch on target node shape/runner, save.

    Pulled out of :func:`advance` per Issue #45f. All state mutations
    and ``state.save()`` calls live here. Caller (typically
    :func:`advance`) supplies a Decision produced by
    :func:`decide_next_action`.

    When ``node`` is provided it is used as the current node instead of
    reading ``state.current_phase``. Required when multiple cursors are
    active (e.g. after a fork) to avoid the multi-cursor ValueError.
    """
    graph = flow.graph
    current = _resolve_current(state, node)

    # Non-commit decisions: most are pure reports back to the caller.
    # `choice_needed` and `end_no_edges` still need a state mutation
    # (record the event / mark the run completed); do that here so
    # decide stays mutation-free.
    if decision.kind == "blocked":
        return AdvanceOutcome(kind="blocked", reason=decision.reason)
    if decision.kind == "error":
        return AdvanceOutcome(kind="error", reason=decision.reason)
    if decision.kind == "end_no_edges":
        state.completed_at = iso_now()
        state.state = "completed"
        state.append_event("run_completed", {})
        state.save()
        _maybe_push_subflow_completion(state)
        return AdvanceOutcome(kind="end", reason=decision.reason)
    if decision.kind == "choice_needed":
        legal_targets = [e.target for e in graph.edges_from(current)]
        state.append_event("branch_choice_requested", {
            "node": current, "options": legal_targets,
        })
        state.save()
        return AdvanceOutcome(kind="choice_needed", options=decision.options)

    # decision.kind == "commit": apply the planned transition. The
    # force-mark (if any) was already persisted by `advance()` before
    # decide ran — Issue #29.
    chosen_edge = decision.chosen_edge
    assert chosen_edge is not None  # commit decisions always have one.
    edges = graph.edges_from(current)
    if decision.auto_resolved:
        state.append_event("branch_auto_resolved", {
            "node": current, "chose": chosen_edge.target,
        })
    elif len(edges) > 1:
        # Caller-supplied target on a fan-out.
        state.append_event("branch_choice_made", {
            "node": current, "chose": chosen_edge.target,
        })

    # Transition scripts run here (after the decision has been made, as
    # effects of committing). Gates already ran inside decide.
    if chosen_edge.scripts:
        env_extra = _env_for_scripts(state, phase_override=current, vars_override=_eval_vars_for_node(state, current))
        script_outcome = run_gates(
            chosen_edge.scripts,
            flow_dir=state.repo_root / state.flow_dir,
            env_extra=env_extra,
        )
        if not script_outcome.passed:
            return AdvanceOutcome(
                kind="infra_error",
                reason=_format_script_failures(
                    "transition script(s) failed", script_outcome.failures,
                ),
            )

    target_node = graph.node(chosen_edge.target)

    # Every cursor carries a branch_id (trunk = "B1"). Advance is always
    # branch-aware: add/remove preserves sibling cursors, and the branch_id
    # propagates onto the target node.
    src_phase = state.phases.get(current)
    branch_id = (src_phase.branch_id if src_phase is not None else None) or "B1"
    branch_vars = state.branch_scopes[branch_id].variables if branch_id in state.branch_scopes else None

    # Join arrival: record the arrival via add/remove so sibling cursors survive.
    if target_node.runner == "join":
        return _record_join_arrival(flow, state, target_node.name, current)

    state.remove_cursor(current)
    state.add_cursor(target_node.name)

    if target_node.shape == "Msquare":
        state.phases[target_node.name] = PhaseState(status="done", started_at=iso_now(), completed_at=iso_now(), branch_id=branch_id)
        state.completed_at = iso_now()
        state.state = "completed"
        state.append_event("run_completed", {})
        state.save()
        _maybe_push_subflow_completion(state)
        return AdvanceOutcome(kind="end", target=target_node.name)

    # Bypass branch: if the current supervision is in this node's bypass_at,
    # skip the worker entirely. Apply declared bypass_variables, mark the phase
    # `done_bypassed`, and let the orchestrator's next advance pick the
    # outgoing edge based on the variables we just set.
    current_supervision = str(state.variables.get("_supervision") or state.supervision or "")
    if current_supervision and current_supervision in target_node.bypass_at:
        bypass_vars = flow.bypass_variables.get(target_node.name, {})
        # Bypass writes land in the branch's scope when this is a branch hop,
        # so downstream branch-local edges see them; run-level otherwise.
        bypass_target = branch_vars if branch_vars is not None else state.variables
        for var_name, var_value in bypass_vars.items():
            if var_name.startswith("_"):
                raise RuntimeError(
                    f"bypass_variables for node {target_node.name!r} declares "
                    f"system-reserved variable {var_name!r}; underscore-prefixed "
                    f"names cannot be set via bypass"
                )
            bypass_target[var_name] = var_value
        now = iso_now()
        state.phases[target_node.name] = PhaseState(
            status="done_bypassed",
            started_at=now,
            completed_at=now,
            branch_id=branch_id,
        )
        state.append_event("node_bypassed", {
            "node": target_node.name,
            "supervision": current_supervision,
            "variables_set": list(bypass_vars.keys()),
        })
        state.save()
        return AdvanceOutcome(kind="moved", target=target_node.name)

    # Structural runner nodes (fork, dynamic_fanout, subflow) have no output
    # schema and are handled as routing checkpoints: mark in_progress and
    # return. Join is handled above via _record_join_arrival (Task 8).
    # The orchestrator drives the actual fan-out / join / subflow-completion
    # logic via a subsequent advance call targeting the structural node.
    # See Task 7; subflow handoff added in Task 18.
    if target_node.runner in ("fork", "dynamic_fanout", "subflow"):
        state.set_phase_status(target_node.name, "in_progress")
        state.phases[target_node.name].branch_id = branch_id
        state.append_event("node_entered", {"node": target_node.name})
        state.save()
        return AdvanceOutcome(kind="moved", target=target_node.name)

    # Agent and script nodes both get eager output-path population.
    # Orchestrator-run nodes don't produce output files, so they skip the
    # eager population (the schema's sets_variables are populated by the
    # orchestrator via `flowstate set-var` before calling `flowstate complete`).
    # See Issue #35.
    schema = flow.schemas[target_node.output_schema]
    if target_node.runner != "orchestrator":
        # Entry-time population deliberately uses the RAW live branch scope
        # (not the layered read view): the resolved paths are WRITTEN into
        # this scope, and validate resolves against the same raw scope at
        # completion time (cli.cmd_validate) — using one scope at both ends
        # keeps entry-time and validate-time path resolution identical.
        _populate_output_paths(flow, state, schema, vars_target=branch_vars)
    state.set_phase_status(target_node.name, "in_progress")
    state.phases[target_node.name].branch_id = branch_id
    state.append_event("node_entered", {"node": target_node.name})
    # Persist the in_progress mutation BEFORE running any
    # subprocess. If the script-node subprocess raises (file not found,
    # OOM, etc.) the in_progress state survives so the next CLI call can
    # detect "stuck in_progress" instead of silently rolling back. See
    # Issue #29.
    state.save()

    if target_node.runner == "orchestrator":
        # Orchestrator-run node: flowstate has done its job by recording
        # in_progress + populating the envelope. The orchestrator does the
        # actual work (e.g., AskUserQuestion → set vars) and then calls
        # `flowstate complete <phase>` to mark done. See Issue #35.
        #
        # Idle transition (spec §4.4): when this child run is a branch of a
        # parent dynamic_fanout AND the inline orchestrator node is about to
        # field a human-input prompt (runner=orchestrator + pauses_at_min set
        # + supervision crosses the threshold), flip the parent's BranchRef
        # to ``idle`` so its slot under ``max_concurrent`` frees for
        # rolling-window fanout. The bypass branch above has already
        # returned if bypass_at matched, so ``bypass_at_match=False`` here
        # is invariant. Top-level runs (parent_run is None) have no
        # BranchRef to transition — no-op.
        if (
            state.parent_run is not None
            and state.parent_run.fanout_node
            and state.parent_run.branch_id
            and _should_enter_idle(
                target_node,
                supervision=current_supervision,
                bypass_at_match=False,
            )
        ):
            _enter_idle(
                Path(state.parent_run.run_dir),
                fanout_node=state.parent_run.fanout_node,
                branch_id=state.parent_run.branch_id,
            )
        return AdvanceOutcome(kind="moved", target=target_node.name)

    if target_node.runner == "script":
        # _apply_decision already populated output paths and saved state
        # above (target_node.runner != "orchestrator" branch). Re-running
        # that inside _execute_script_node is idempotent. For a branch hop,
        # thread the branch scope (so the script's outputs land in the branch's
        # scope) and pass phase_override so the subprocess-env builder doesn't
        # trip the single-cursor guard while sibling cursors are live. See
        # Task 24 + fork multi-node-arm support.
        err = _execute_script_node(
            flow, state, target_node,
            vars_target=branch_vars,
            phase_override=target_node.name,
        )
        if err is not None:
            return err
        return AdvanceOutcome(kind="moved", target=target_node.name)

    state.save()
    return AdvanceOutcome(kind="moved", target=target_node.name)

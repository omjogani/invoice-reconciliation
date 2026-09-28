from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

from flowstate.completion import load_and_validate as _load_completion
from flowstate.definitions import (
    extend_with_session_id,
    validate_against_schema,
    SchemaValidationError,
)
from flowstate.parser import Flow, FlowGraph, OutputSchema
from flowstate.repo_root import RepoRootError, to_repo_relative
from flowstate.state import RunState
from flowstate.time import iso_now


def _display_path(out_path: Path, repo_root: Path) -> str:
    """Format a resolved output path for feedback strings. Prefer
    repo-relative form (matches how operators read paths in the
    issues log / git status); fall back to the absolute form if the
    path is outside the repo, which should not happen in practice
    but we still want a useful display. See Issue #15.
    """
    try:
        return to_repo_relative(out_path, repo_root)
    except RepoRootError:
        return str(out_path)


@dataclass
class ValidationOutcome:
    passed: bool
    feedback: str = ""
    agent_session_id: str | None = None


@dataclass
class ValidationResult:
    """Result of `validate_graph`. `errors` are blocking; `warnings` flag
    likely misconfigurations the author may still intend (e.g. T10's
    `pauses_at_min` × non-orchestrator-runner combo)."""
    errors: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)


def _load_output(path: Path) -> dict[str, Any]:
    """Parse an output artefact. Artefacts are YAML or JSON (a YAML subset),
    so a single `yaml.safe_load` handles both. Spec 2026-07-07 §3.2."""
    if not path.exists():
        raise FileNotFoundError(f"output file not found at {path!s}")
    data = yaml.safe_load(path.read_text())
    if not isinstance(data, dict):
        raise ValueError(f"output file at {path!s} is not a mapping")
    return data


def validate_phase(
    state: RunState,
    phase_name: str,
    schema: OutputSchema,
    definitions: dict[str, dict],
    output_paths: dict[str, Path],
    completion_path: Path,
    vars_target: dict[str, Any] | None = None,
    authoritative_sid: str | None = None,
    expected_echo: str | None = None,
) -> ValidationOutcome:
    """Validate completion.yml + every declared output file for `phase_name`.

    Sets phase status to `done` and populates `sets_variables` on pass.
    Records `agent_session_id` on the phase and returns it in the outcome.

    ``vars_target`` directs the sets_variables writes into a specific scope
    (a fork arm's branch scope). Defaults to ``state.variables`` (trunk).
    """
    phase = state.phases.get(phase_name)
    if phase is None:
        return ValidationOutcome(passed=False, feedback=f"unknown phase {phase_name!r}")
    phase.validation_checked_at = iso_now()

    # 1. Completion file shape + session_id presence.
    try:
        completion = _load_completion(completion_path)
    except (FileNotFoundError, SchemaValidationError) as exc:
        phase.validation_passed = False
        phase.validation_feedback = f"completion.yml: {exc}"
        return ValidationOutcome(passed=False, feedback=phase.validation_feedback)

    worker_echo = completion["_session_id"]
    if authoritative_sid is not None:
        # 2026-08-10: the spawn record is the identity; the worker's echo
        # only verifies that this completion.yml came from THIS attempt's
        # worker. Hard fail on a mismatch — but only when both sides exist.
        # `expected_echo` is spawn_token when the harness minted one
        # (opencode: the tok_ nonce the worker was told) else the real sid.
        if expected_echo and worker_echo and worker_echo != expected_echo:
            phase.validation_passed = False
            phase.validation_feedback = (
                f"session-id mismatch: this attempt's worker was told "
                f"{expected_echo!r} but completion.yml carries "
                f"{worker_echo!r}. Either a stale completion.yml from a "
                f"previous attempt, or a different process wrote it — the "
                f"work cannot be attributed; investigate before re-running"
            )
            return ValidationOutcome(
                passed=False, feedback=phase.validation_feedback)
        canonical_sid = authoritative_sid
    else:
        # Legacy trust boundary: no spawn record available (standalone
        # `flowstate validate`, pre-2026-08-10 runs) — the worker's report
        # stands, as it always did.
        canonical_sid = worker_echo

    # Record the worker's session id eagerly — before the per-file checks —
    # so even a *failed* validate captures which worker attempted the phase.
    # `cli.cmd_validate`'s usage-population block keys off
    # `phase.agent_session_id`; without this, failed attempts have their
    # cost silently dropped from the run total. See Issue #6.
    # When the sid changes (re-spawn after a prior attempt), snapshot the
    # previous attempt's usage into `prior_attempts` first.
    if (
        phase.agent_session_id is not None
        and phase.agent_session_id != canonical_sid
    ):
        phase.archive_current_attempt()
    phase.agent_session_id = canonical_sid

    # Capture the worker's optional free-text `notes` BEFORE the status-failed
    # early-return — even failed attempts carry useful caveats (env breaks,
    # intentionally-skipped sub-steps) the orchestrator wants surfaced.
    notes_raw = completion.get("notes")
    if isinstance(notes_raw, str) and notes_raw.strip():
        phase.notes = notes_raw

    # If the worker self-reported failure, honour that signal *before* walking
    # the output files. Without this, a worker that wrote `status: failed,
    # errors: [...]` plus well-formed outputs is marked done. See Issue #39.
    if completion.get("status") == "failed":
        errors = completion.get("errors") or []
        errors_text = "; ".join(str(e) for e in errors) if errors else "no errors listed"
        phase.validation_passed = False
        phase.validation_feedback = f"worker reported status=failed: {errors_text}"
        return ValidationOutcome(passed=False, feedback=phase.validation_feedback)

    # 2. Every declared output file: exists, parses, validates, _session_id matches.
    # Feedback strings carry the *resolved* repo-relative path (not the raw
    # `{_userid}_{_timestamp}` template) so the orchestrator's Bucket A.5
    # amendment path and any human reading the events log can locate the file
    # immediately. See Issue #15.
    missing: list[str] = []
    for sf in schema.files:
        out_path = output_paths[sf.name]
        display = _display_path(out_path, state.repo_root)
        if not out_path.exists():
            missing.append(display)
            continue
        if sf.definition is None:
            # Existence-only output: verify non-empty, skip parse/schema/
            # session-id checks — the artefact is markdown owned by the
            # producing skill. Spec 2026-07-07 §3.2.
            if out_path.stat().st_size == 0:
                phase.validation_passed = False
                phase.validation_feedback = f"{display}: output file is empty"
                return ValidationOutcome(passed=False, feedback=phase.validation_feedback)
            continue
        try:
            data = _load_output(out_path)
        except (yaml.YAMLError, ValueError) as exc:
            phase.validation_passed = False
            phase.validation_feedback = f"{display}: invalid YAML/JSON: {exc}"
            return ValidationOutcome(passed=False, feedback=phase.validation_feedback)
        user_schema = definitions[sf.definition]
        extended = extend_with_session_id(user_schema)
        try:
            validate_against_schema(data, extended)
        except SchemaValidationError as exc:
            phase.validation_passed = False
            phase.validation_feedback = f"{display}: schema violation: {exc}"
            return ValidationOutcome(passed=False, feedback=phase.validation_feedback)
        if data.get("_session_id") != canonical_sid:
            phase.validation_passed = False
            phase.validation_feedback = (
                f"{display}: session_id mismatch — completion.yml has "
                f"{canonical_sid!r}, output has {data.get('_session_id')!r}"
            )
            return ValidationOutcome(passed=False, feedback=phase.validation_feedback)

    if missing:
        phase.validation_passed = False
        phase.validation_feedback = f"missing output(s): {', '.join(missing)}"
        return ValidationOutcome(passed=False, feedback=phase.validation_feedback)

    # 3. All good — mark done and populate sets_variables. `agent_session_id`
    # was set above (before the per-file checks); we just toggle the
    # validation flags and run the post-success bookkeeping.
    phase.validation_passed = True
    phase.validation_feedback = None
    phase.summary = completion.get("summary")
    state.set_phase_status(phase_name, "done")
    target_vars = vars_target if vars_target is not None else state.variables
    for var_name, file_id in schema.sets_variables.items():
        out_path = output_paths[file_id]
        target_vars[var_name] = str(out_path)
    return ValidationOutcome(passed=True, agent_session_id=canonical_sid)


# ---------------------------------------------------------------------------
# Graph-level structural validation
# ---------------------------------------------------------------------------


def _reachable_to_terminal(graph: FlowGraph) -> set[str]:
    """Return the set of node names from which a terminal (Msquare) node is
    reachable via forward edges. Computed by backwards BFS from terminals."""
    terminals = {n.name for n in graph.end_nodes()}
    reachable: set[str] = set(terminals)
    changed = True
    while changed:
        changed = False
        for e in graph.edges:
            if e.target in reachable and e.source not in reachable:
                reachable.add(e.source)
                changed = True
    return reachable


def validate_graph(
    graph: FlowGraph, *, flow: Flow | None = None
) -> ValidationResult:
    """Structural validation of a FlowGraph.  Returns a `ValidationResult`
    with `errors` (blocking) and `warnings` (advisory) lists.

    Errors (graph-only):
      1. Terminal reachability — every non-terminal node must have at least
         one forward path to an Msquare node.
      2. No back-edges across merges — a post-join node must not feed back
         into a pre-join node (for each runner=join node).
      3. Subflow node must declare a flow= attribute — every runner=subflow
         node must have a non-empty subflow_flow.
      4. Dynamic fanout template must be a subflow — every runner=dynamic_fanout
         node's template_node must name an existing node with runner=subflow.
         A missing template_node is also an error.
      5. Join must have >=1 incoming edge (T17/rule 1).
      6. Static fork (runner=fork) must have >=2 outgoing edges (T17/rule 3).
         runner=dynamic_fanout is exempt — its branch count is runtime-determined.
      7. dynamic_fanout source_var must be non-empty (T17/rule 4).
      8. join nodes must co-declare reducer_script and summary_var, or neither
         (T19/rule 6).
     12. max_concurrent must be a non-negative integer (T10). The parser
         already rejects negatives at parse time; this is defense-in-depth for
         programmatically-built graphs that bypass the parser.

    Errors requiring `flow` (skipped when flow is None):
      9. dynamic_fanout source_var must reference a declared variable
         (T17/rule 4 extended).
     10. subflow_outputs declared on a subflow node must be type/enum-compatible
         with the child flow's declared variables (T18/rule 5).
     11. If reducer_script is set on a join, the script must exist relative to
         the flow directory (T19/rule 7).

    Warnings (graph-only):
      W1. `pauses_at_min` set on a non-orchestrator runner (T10). The `idle`
          slot-freeing behaviour only kicks in for runner=orchestrator nodes;
          using pauses_at_min on an agent/script/etc. will still pause but
          won't free the slot. Author may have intended it — surface for
          confirmation rather than block.
    """
    errs: list[str] = []
    warnings: list[str] = []

    # --- Rule 1: Terminal reachability ---
    reachable = _reachable_to_terminal(graph)
    for n in graph.nodes:
        if n.shape == "Msquare":
            continue
        if n.name not in reachable:
            errs.append(
                f"node {n.name!r} cannot reach any terminal node"
            )

    # --- Rule 2: No back-edges across merges ---
    for j in [n for n in graph.nodes if n.runner == "join"]:
        # pre-merge zone: all nodes that can reach j (including j's direct inputs)
        pre: set[str] = set()
        for e in graph.edges:
            if e.target == j.name:
                pre.add(e.source)
        # expand pre backwards (BFS)
        changed = True
        while changed:
            changed = False
            for e in graph.edges:
                if e.target in pre and e.source not in pre:
                    pre.add(e.source)
                    changed = True

        # post-merge zone: all nodes reachable forward from j (not including j itself)
        post: set[str] = set()
        for e in graph.edges:
            if e.source == j.name:
                post.add(e.target)
        changed = True
        while changed:
            changed = False
            for e in graph.edges:
                if e.source in post and e.target not in post:
                    post.add(e.target)
                    changed = True

        # Any edge from post back into pre is a back-edge
        for e in graph.edges:
            if e.source in post and e.target in pre:
                errs.append(
                    f"back-edge {e.source!r}->{e.target!r} re-enters "
                    f"pre-merge zone of join {j.name!r}"
                )

    # --- Rule 3: Subflow node must declare a flow= attribute ---
    for s in [n for n in graph.nodes if n.runner == "subflow"]:
        if not s.subflow_flow:
            errs.append(
                f"subflow node {s.name!r} has no flow= attribute"
            )

    # --- Rule 4: Dynamic fanout template must be a subflow ---
    for f in [n for n in graph.nodes if n.runner == "dynamic_fanout"]:
        if not f.template_node:
            errs.append(
                f"dynamic_fanout {f.name!r} missing template= attribute; "
                f"template must name a subflow node"
            )
            continue
        try:
            tmpl = graph.node(f.template_node)
        except KeyError:
            errs.append(
                f"dynamic_fanout {f.name!r} template {f.template_node!r} "
                f"not found in graph"
            )
            continue
        if tmpl.runner != "subflow":
            errs.append(
                f"dynamic_fanout {f.name!r} template {f.template_node!r} "
                f"must be a subflow node (got runner={tmpl.runner!r})"
            )

    # --- T17/Rule 1: join must have >=1 incoming edge ---
    # A join with no in-edges is silently broken at runtime — the join would
    # never receive any branch arrivals to synchronise. Catch at parse time.
    for j in [n for n in graph.nodes if n.runner == "join"]:
        incoming = [e for e in graph.edges if e.target == j.name]
        if not incoming:
            errs.append(
                f"join {j.name!r}: must have at least 1 incoming edge (none found)"
            )

    # --- T17/Rule 3: static fork must have >=2 outgoing edges ---
    # A single-successor static fork is degenerate — there's no fan-out. The
    # rule is scoped to runner=fork only; runner=dynamic_fanout is exempt
    # because its branch count is determined at runtime from source_var.
    for f in [n for n in graph.nodes if n.runner == "fork"]:
        outgoing = [e for e in graph.edges if e.source == f.name]
        if len(outgoing) < 2:
            errs.append(
                f"fork {f.name!r}: must have at least 2 successors "
                f"(found {len(outgoing)})"
            )

    # --- T17/Rule 4: dynamic_fanout requires source_var ---
    # source_var names the iterable the fanout iterates over. Without it, the
    # runtime has nothing to fan out from. The "must be declared in flow vars"
    # half of the rule is only checkable when `flow` is provided.
    for f in [n for n in graph.nodes if n.runner == "dynamic_fanout"]:
        if not f.source_var:
            errs.append(
                f"dynamic_fanout {f.name!r}: source_var attribute is required"
            )
        elif flow is not None and f.source_var not in flow.variables:
            errs.append(
                f"dynamic_fanout {f.name!r}: source_var {f.source_var!r} "
                f"not declared in flow.variables"
            )

    # --- T19/Rule 6: reducer_script + summary_var co-declared ---
    # The two attributes are paired: the script computes a value, summary_var
    # names where that value lands in parent scope. Either alone is an
    # authoring mistake.
    for j in [n for n in graph.nodes if n.runner == "join"]:
        if j.reducer_script and not j.summary_var:
            errs.append(
                f"join {j.name!r}: reducer_script set but summary_var is not "
                f"— must be co-declared"
            )
        if j.summary_var and not j.reducer_script:
            errs.append(
                f"join {j.name!r}: summary_var set but reducer_script is not "
                f"— must be co-declared"
            )

        # --- T19/Rule 7: reducer_script path must exist (flow-scoped) ---
        if j.reducer_script and flow is not None:
            script_path = flow.flow_dir / j.reducer_script
            if not script_path.exists():
                errs.append(
                    f"join {j.name!r}: reducer_script {j.reducer_script!r} "
                    f"does not exist relative to flow dir {flow.flow_dir!s}"
                )

    # --- T18/Rule 5: cross-flow subflow_outputs compatibility ---
    # Requires the Flow object so we can load the child flow's variables and
    # compare against parent declarations. Skipped silently when flow is None.
    if flow is not None:
        _check_subflow_outputs_compatibility(graph, flow, errs)

    # --- T10/Rule 12: defensive max_concurrent shape check ---
    # The parser already rejects negatives at parse time (`_parse_max_concurrent`),
    # so a parser-loaded flow can never reach this branch. We still emit the
    # error so a programmatically-built FlowGraph (test fixtures, future flow
    # builders) can't smuggle in a negative cap and produce undefined runtime
    # behaviour.
    for n in graph.nodes:
        if n.max_concurrent is not None and n.max_concurrent < 0:
            errs.append(
                f"node {n.name!r}: max_concurrent must be a non-negative "
                f"integer (got {n.max_concurrent!r})"
            )

    # --- T10/Warning W1: pauses_at_min × non-orchestrator runner ---
    # `idle` slot-freeing is only honoured for runner=orchestrator nodes (see
    # the traversal idle-transition logic). Setting pauses_at_min on an agent
    # or script runner will still pause execution at that supervision level
    # but won't free the rolling-window slot — usually not what the author
    # wanted. Warn rather than error because the author may have set it for a
    # different reason (e.g. as documentation, or anticipating a future runner
    # change).
    for n in graph.nodes:
        if n.pauses_at_min and n.runner != "orchestrator":
            warnings.append(
                f"node {n.name!r} has pauses_at_min={n.pauses_at_min!r} but "
                f"runner={n.runner!r}; idle slot-freeing only applies to "
                f"runner=orchestrator nodes. If this is intentional, ignore."
            )

    return ValidationResult(errors=errs, warnings=warnings)


def _check_subflow_outputs_compatibility(
    graph: FlowGraph, flow: Flow, errs: list[str]
) -> None:
    """T18/rule 5: for each runner=subflow node with declared subflow_outputs,
    load the child flow and check parent ↔ child variable compatibility.

    Compatibility rules:
      - If both parent and child declare type=enum, the child's values must be
        a subset of the parent's (otherwise the child can emit a value the
        parent declared as illegal).
      - If both declare a non-empty type and the types differ, that's an
        error.
      - Unknown parent var (not declared in flow.variables) or unknown child
        var (not declared in child.variables) is an error.
      - Failure to load the child flow itself is an error tagged with the
        subflow node name.
    """
    # Local import to avoid a circular import at module load time
    # (parser.py imports from validate.py is not currently the case, but the
    # local import keeps validate.py decoupled from any future re-entry).
    from flowstate.parser import load_flow

    for s in [n for n in graph.nodes if n.runner == "subflow"]:
        if not s.subflow_outputs:
            continue
        if not s.subflow_flow:
            # Rule 3 already errors on this; don't double-report.
            continue
        child_dot = _resolve_child_dot_path(s.subflow_flow, flow.flow_dir)
        try:
            child_flow = load_flow(child_dot)
        except Exception as exc:
            errs.append(
                f"subflow {s.name!r}: cannot load child flow "
                f"{s.subflow_flow!r}: {exc}"
            )
            continue
        for parent_var, child_var in s.subflow_outputs.items():
            parent_spec = flow.variables.get(parent_var)
            child_spec = child_flow.variables.get(child_var)
            if parent_spec is None:
                errs.append(
                    f"subflow {s.name!r}: parent var {parent_var!r} "
                    f"(in subflow_outputs) not declared in parent.variables"
                )
                continue
            if child_spec is None:
                errs.append(
                    f"subflow {s.name!r}: child var {child_var!r} "
                    f"(in subflow_outputs) not declared in child.variables"
                )
                continue
            # Enum subset (only when both sides are enum)
            if parent_spec.type == "enum" and child_spec.type == "enum":
                parent_values = set(parent_spec.values or [])
                child_values = set(child_spec.values or [])
                if not child_values <= parent_values:
                    extras = sorted(child_values - parent_values)
                    errs.append(
                        f"subflow {s.name!r}: child enum values for "
                        f"{child_var!r} ({extras!r}) are not a subset of "
                        f"parent's enum for {parent_var!r} "
                        f"({sorted(parent_values)!r})"
                    )
            # Type mismatch (both non-empty, different)
            elif (
                parent_spec.type
                and child_spec.type
                and parent_spec.type != child_spec.type
            ):
                errs.append(
                    f"subflow {s.name!r}: type mismatch — parent's "
                    f"{parent_var!r} is {parent_spec.type!r}, child's "
                    f"{child_var!r} is {child_spec.type!r}"
                )


def _resolve_child_dot_path(flow_ref: str, parent_flow_dir: Path) -> Path:
    """Resolve a subflow node's flow= attribute to the child flow's .dot path.

    The orchestrator convention is that flows live in `factory/flows/{name}/`
    with `{name}.dot` inside. Given a parent flow_dir, the child flow with
    name `child` is at `parent_flow_dir.parent / child / child.dot`. We try
    that location first; if the caller passed an absolute path, honour it.
    """
    p = Path(flow_ref)
    if p.is_absolute():
        return p if p.suffix == ".dot" else p / f"{p.name}.dot"
    sibling = parent_flow_dir.parent / p.name / f"{p.name}.dot"
    return sibling

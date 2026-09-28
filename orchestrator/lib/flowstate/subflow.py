"""subflow.py — helpers for initialising a child subflow run and harvesting outputs.

``init_subflow_run`` creates a fresh RunState for a child flow, seeds it with
resolved input variables, and stamps ``parent_run`` with a ``ParentRef`` back
to the calling parent run.  It does NOT spawn workers or advance the flow —
that is the orchestrator's responsibility (Task 18+).

``harvest_subflow_outputs`` reads a completed child run's variables and returns
the subset declared in the parent node's ``subflow_outputs`` mapping.  It
returns an empty dict when the child has not yet reached terminal state.
"""
from __future__ import annotations

import re
from pathlib import Path
from typing import Any

from flowstate.state import BranchRef, BranchScope, ParentRef, PhaseState, RunState, branch_is_terminal, check_var_size, state_lock
from flowstate.time import compact_stamp

_SLUG_MAX = 18


def _short_slug(text: str, max_len: int = _SLUG_MAX) -> str:
    """Lowercase word-slug of ``text``, capped at ``max_len`` chars.

    Split on non-alphanumerics; keep words in order while the '-'-joined slug
    stays within ``max_len``. Always keep at least one word — a single overlong
    first word is truncated to ``max_len``. Returns "" for text with no
    alphanumerics.
    """
    words = [w for w in re.split(r"[^A-Za-z0-9]+", text.lower()) if w]
    if not words:
        return ""
    slug = words[0][:max_len]
    for w in words[1:]:
        candidate = f"{slug}-{w}"
        if len(candidate) > max_len:
            break
        slug = candidate
    return slug


def _child_run_descriptor(branch_id: str, inputs: dict[str, Any]) -> str:
    """Derive a child run descriptor as ``<branch_id>-<label>``.

    ``branch_id`` comes first so runs sort/group by branch. Label precedence:
    ``ticket_id`` (preserves the pre-existing contract), else ``jira_ref``
    (optionally suffixed with ``-<short_slug(freeform_input)>``), else the
    ``short_slug`` of ``freeform_input`` alone. With no ticket fields the
    descriptor is ``branch_id`` alone. The whole descriptor is then sanitised
    with the run-dir-name regex (``.`` → ``_``, so ``B1.3`` → ``B1_3``).
    """
    ticket_id = inputs.get("ticket_id")
    jira_ref = inputs.get("jira_ref")
    freeform = inputs.get("freeform_input")
    if ticket_id:
        label = str(ticket_id)
    elif jira_ref:
        label = str(jira_ref)
        if freeform:
            slug = _short_slug(str(freeform))
            if slug:
                label = f"{label}-{slug}"
    elif freeform:
        label = _short_slug(str(freeform))
    else:
        label = ""

    descriptor = f"{branch_id}-{label}" if label else branch_id
    return re.sub(r"[^A-Za-z0-9_\-]", "_", descriptor)


def init_subflow_run(
    parent_run_dir: Path,
    child_flow_name: str,
    child_flow_dir: Path,
    child_flow_dot_path: Path,
    inputs: dict[str, Any],
    branch_id: str,
    fanout_node: str,
    userid: str,
) -> Path:
    """Create and return the path of a newly-initialised child subflow run.

    The child run dir is placed at::

        <repo_root>/factory/graph_runs/<child_flow_name>/<descriptor>_<userid>_<ts>_B<NN>

    where ``descriptor`` is the ticket-labelled ``<branch_id>-<label>`` form
    from ``_child_run_descriptor`` (label = ticket_id / jira_ref[+slug] /
    freeform slug / none), and the ``_B<NN>`` suffix is ``branch_id`` when it
    already matches ``B[\\d.]+`` (dots are path-safe and preserved for
    hierarchical ids like ``B1.2``), otherwise ``B1``.

    After ``RunState.create`` the child's ``parent_run`` field is set and the
    state is saved.  No workers are spawned.

    Args:
        parent_run_dir: Path to the parent run directory (contains graph_run_state.yml).
        child_flow_name: Logical name of the child flow.
        child_flow_dir: Directory on disk that contains the child flow definition.
        child_flow_dot_path: Filesystem path to the child flow's .dot file.
            Stored on the child state as a repo-relative path (matching
            ``cmd_init``'s contract), and read in-process for parser-side
            start-node discovery.
        inputs: Resolved input variables to seed on the child run.
        branch_id: Branch identifier assigned by the parent (e.g. "B01").
        fanout_node: Name of the parent node that initiated the subflow.
        userid: User identifier, forwarded to RunState.create.

    Returns:
        Path to the newly-created child run directory.
    """
    parent_run_dir = Path(parent_run_dir)
    child_flow_dir = Path(child_flow_dir)
    child_flow_dot_path = Path(child_flow_dot_path)

    # Load just enough from the parent to get repo_root and flow_name.
    parent = RunState.load(parent_run_dir)

    # Build the _B<NN> suffix: use branch_id directly when it matches B[\d.]+
    # (dotted hierarchical ids like B1.2 are path-safe and preserved).
    if re.fullmatch(r"B[\d.]+", branch_id):
        b_suffix = branch_id
    else:
        b_suffix = "B1"

    # Derive the ticket-labelled run descriptor: <branch_id>-<label>, branch_id
    # first so runs group by branch. See _child_run_descriptor for the label
    # precedence and sanitisation.
    slug = _child_run_descriptor(branch_id, inputs)

    timestamp = compact_stamp()
    run_dir_name = f"{slug}_{userid}_{timestamp}_{b_suffix}"
    child_run_dir = parent.repo_root / "factory" / "graph_runs" / child_flow_name / run_dir_name

    # parse_dot needs text + flow-level supervision_instructions; RunState.create
    # needs a repo-relative *path* for `flow_dot` (the field downstream code
    # joins onto repo_root and opens with load_flow). Before 2026-06-29 this
    # stored the DOT body in `metadata.flow_dot`, which made every subsequent
    # advance on the child raise OSError [Errno 63] when it tried to open the
    # multi-KB string as a filename.
    from flowstate.parser import load_flow
    from flowstate.repo_root import to_repo_relative

    child_flow = load_flow(child_flow_dot_path)
    start_node = child_flow.graph.start_node().name
    flow_dot_rel = to_repo_relative(child_flow_dot_path, parent.repo_root)
    flow_dir_rel = to_repo_relative(child_flow_dir, parent.repo_root)
    run_dir_rel = to_repo_relative(child_run_dir, parent.repo_root)

    # Stamp the child's own system variables alongside the caller-supplied
    # inputs. Mirrors `_init_state` in cli.py: every run (parent or child)
    # needs `_userid`, `_timestamp`, `_run_descriptor`, `_flow_stem`,
    # `_run_artefact_dir`, `_supervision`, `_supervision_instructions` for
    # prompt templates and downstream nodes to render. Inputs win on collision
    # only for non-system keys — system underscore-prefixed variables are
    # always rebuilt for the child.
    supervision = parent.supervision
    system_vars: dict[str, object] = {
        "_userid": userid,
        "_timestamp": timestamp,
        "_run_descriptor": slug,
        "_flow_stem": child_flow_dot_path.stem,
        "_run_artefact_dir": str(run_dir_rel),
        "_supervision": supervision,
        "_supervision_instructions": child_flow.supervision_instructions.get(supervision, ""),
    }
    # Also seed declared variable defaults for any var the parent didn't pass
    # (matches `_init_state`'s loop over flow.variables).
    declared_defaults: dict[str, object] = {}
    for var_name, spec in child_flow.variables.items():
        if var_name not in inputs:
            declared_defaults[var_name] = spec.default

    seeded = {**declared_defaults, **dict(inputs), **system_vars}

    child_state = RunState.create(
        run_dir=child_run_dir,
        flow_name=child_flow_name,
        flow_dir=Path(flow_dir_rel),
        flow_dot=flow_dot_rel,
        repo_root=parent.repo_root,
        run_descriptor=slug,
        userid=userid,
        timestamp=timestamp,
        start_node=start_node,
        variables=seeded,
        supervision=supervision,
    )

    child_state.parent_run = ParentRef(
        flow=parent.flow_name,
        run_dir=str(parent_run_dir),
        fanout_node=fanout_node,
        branch_id=branch_id,
    )
    child_state.save()

    # Stamp the parent-side relationship so the runtime can find the child run
    # later (subflow completion harvest, Task 18). We re-load the parent under
    # state_lock to avoid clobbering a concurrent orchestrator mutation, then
    # either MUTATE the existing pending BranchRef (stamped earlier by
    # `_apply_dynamic_fanout` with `subflow_run_dir=None`) or APPEND a fresh
    # one if no entry matches the branch_id yet. I-2 (2026-06-04): the prior
    # always-append path silently created a duplicate BranchRef whenever a
    # dynamic_fanout had pre-stamped one, which was dormant in shipped code
    # paths today but would activate as soon as a flow combined programmatic
    # fanout with `init_subflow_run`.
    #
    # When fanout_node is None (a top-level subflow with no parent fanout
    # context) skip the stamp — there's no parent node to record against.
    if fanout_node:
        with state_lock(parent_run_dir):
            parent_state = RunState.load(parent_run_dir)
            phase = parent_state.phases.setdefault(fanout_node, PhaseState())
            existing = next(
                (b for b in phase.branches if b.branch_id == branch_id), None,
            )
            if existing is not None:
                existing.subflow_run_dir = str(child_run_dir)
                existing.status = "in_progress"
            else:
                phase.branches.append(
                    BranchRef(
                        branch_id=branch_id,
                        subflow_run_dir=str(child_run_dir),
                        status="in_progress",
                    )
                )
            parent_state.save()

    return child_run_dir


def harvest_subflow_outputs(
    child_run_dir: Path,
    outputs: dict[str, str],
) -> dict[str, Any]:
    """Return declared subflow outputs from a completed child run.

    Loads the child ``RunState`` from ``child_run_dir``.  If the child has not
    reached ``state == "completed"``, returns ``{}`` immediately — callers
    should poll again on the next orchestrator tick.

    Otherwise, builds and returns a dict ``{parent_var: child_value}`` for each
    entry in ``outputs`` where the mapped ``child_var`` is present in the
    child's ``variables``.  Entries whose ``child_var`` is absent are silently
    omitted so a partial child run never blocks the harvest (back-compat with
    flows that don't populate every declared output).

    Args:
        child_run_dir: Path to the child run directory (contains
            ``graph_run_state.yml``).
        outputs: Mapping of ``{parent_var: child_var}`` taken from the parent
            node's ``subflow_outputs`` attribute.

    Returns:
        Dict of ``{parent_var: value}`` for each harvested variable, or ``{}``
        when the child is not yet in terminal state.
    """
    child_run_dir = Path(child_run_dir)
    child = RunState.load(child_run_dir)
    if child.state != "completed":
        return {}
    result: dict[str, Any] = {}
    for parent_var, child_var in outputs.items():
        if child_var not in child.variables:
            continue
        value = child.variables[child_var]
        # T22 / I1: 16 KB ceiling enforced on every internal write path. The
        # ValueError propagates to the caller (`complete_subflow` or
        # `_apply_subflow_completion`); the push-hook in traversal catches it
        # and records `subflow_push_failed`.
        check_var_size(parent_var, value)
        result[parent_var] = value
    return result


def _find_branch_ref(
    parent: RunState, branch_id: str, fanout_node: str | None = None,
) -> tuple[str, BranchRef]:
    """Locate the (subflow_node_name, BranchRef) for ``branch_id`` in the parent.

    branch_ids are only unique *per parent node* — an upstream ``fork`` and a
    downstream ``dynamic_fanout`` can each mint ``B01``/``B02`` in the same run.
    When ``fanout_node`` is known (the child's ``parent_run.fanout_node``), scope
    the lookup to that node so the fork's stale same-id branch isn't matched.

    Without ``fanout_node`` (legacy / manual CLI recovery), scan all phases but
    prefer a branch that carries a ``subflow_run_dir`` — ``complete_subflow`` is
    only ever for subflow branches, so a colliding inline fork branch (which has
    ``inline_root_node`` and no ``subflow_run_dir``) must not shadow it.

    Raises ValueError if no PhaseState.branches entry matches.
    """
    if fanout_node is not None:
        phase = parent.phases.get(fanout_node)
        if phase is not None:
            for b in phase.branches:
                if b.branch_id == branch_id:
                    return fanout_node, b
        raise ValueError(
            f"branch_id {branch_id!r} not found on fanout node {fanout_node!r} "
            f"in parent run at {parent.run_dir!s}"
        )
    fallback: tuple[str, BranchRef] | None = None
    for name, phase in parent.phases.items():
        for b in phase.branches:
            if b.branch_id == branch_id:
                if b.subflow_run_dir:
                    return name, b
                if fallback is None:
                    fallback = (name, b)
    if fallback is not None:
        return fallback
    raise ValueError(
        f"branch_id {branch_id!r} not found in parent run at {parent.run_dir!s}"
    )


def complete_subflow(
    parent_run_dir: str, branch_id: str, fanout_node: str | None = None,
) -> None:
    """Push-driven merge-in: harvest a completed child's outputs into the parent,
    flip the parent's BranchRef to done, fire the reducer if defined.

    ``fanout_node`` (the child's ``parent_run.fanout_node``) scopes the branch
    lookup to the right parent node — required when an upstream fork and this
    dynamic_fanout mint colliding branch_ids in the same run.

    Invoked by the runtime when a child subflow reaches its terminal state
    (T12 wiring). The orchestrator does NOT call this directly — it is a
    programmatic merge-in hook (also exposed as a CLI subcommand by T11).

    LOCK ORDERING INVARIANT (I-6): this function acquires the PARENT's
    state_lock and is called from the CHILD's advance path (push hook in
    ``_maybe_push_subflow_completion``). Therefore the cross-run lock
    acquisition order is always (child → parent). NEVER acquire a child's
    state_lock while already holding the parent's — read the child without
    a lock (the existing poll path in ``_apply_subflow_completion`` does
    this). Deadlock is not possible today because no path locks
    parent → child, but the invariant must hold for any future code that
    crosses run boundaries.

    Two short critical sections under the parent state_lock:
      1. Harvest declared outputs from the child into
         ``parent.branch_scopes[branch_id].variables`` and flip the
         BranchRef.status to ``"done"``. Skipped when the BranchRef is
         already ``"done"`` (idempotency on the harvest side).
      2. If the subflow node declares ``reducer_script`` + ``summary_var``,
         call ``_fire_reducer`` (which itself runs the subprocess INSIDE
         this critical section, parses stdout, appends a JoinFiring +
         reducer_fired event, and atomically updates ``summary_var``).
         The reducer DOES re-fire on idempotent re-calls — matches T4
         per-arrival semantics so a forensic re-run can be triggered safely.

    Spec D9 was amended on 2026-06-04 (see the design doc) to record that
    the reducer subprocess runs inside CS2 rather than between the two
    critical sections; the implementation never did otherwise.

    Raises ValueError when:
      - ``branch_id`` is not found on any node in the parent's phases
      - the BranchRef has no ``subflow_run_dir`` (not a subflow branch)
      - the child run has ``state != "completed"``
    """
    from flowstate.parser import load_flow

    parent_path = Path(parent_run_dir)

    # --- Critical section 1: harvest + flip status ---------------------
    with state_lock(parent_path):
        parent = RunState.load(parent_path)
        subflow_node_name, branch_ref = _find_branch_ref(parent, branch_id, fanout_node)
        if not branch_ref.subflow_run_dir:
            raise ValueError(
                f"branch {branch_id!r} on node {subflow_node_name!r} is not a "
                f"subflow branch (subflow_run_dir is unset)"
            )

        child = RunState.load(Path(branch_ref.subflow_run_dir))
        if child.state != "completed":
            raise ValueError(
                f"child run at {branch_ref.subflow_run_dir!r} has state "
                f"{child.state!r}; complete_subflow requires state=='completed'"
            )

        flow = load_flow(parent.repo_root / parent.flow_dot)
        node_def = flow.graph.node(subflow_node_name)

        # Resolve the right node from which to read `subflow_outputs` and
        # `reducer_script`/`summary_var`. For a dynamic_fanout, the branch's
        # parent node has runner=dynamic_fanout and points at a TEMPLATE
        # subflow node via `template_node=`. The template carries
        # `subflow_outputs` (what to harvest per child). The reducer lives
        # one hop further on the downstream join. For a plain (non-fanout)
        # subflow node, both live on the node itself.
        outputs_node = node_def
        reducer_node = node_def
        if node_def.runner == "dynamic_fanout" and node_def.template_node:
            try:
                outputs_node = flow.graph.node(node_def.template_node)
            except KeyError:
                outputs_node = node_def
            # The reducer for a dynamic_fanout lives on the downstream join.
            # Walk template's outgoing edges (typically one) to find the join.
            for edge in flow.graph.edges_from(outputs_node.name):
                try:
                    candidate = flow.graph.node(edge.target)
                except KeyError:
                    continue
                if candidate.runner == "join":
                    reducer_node = candidate
                    break
        outputs_map = outputs_node.subflow_outputs or {}

        # Skip harvest if branch is already done (idempotency). The reducer
        # below still re-fires — matches T4 per-arrival semantics.
        if branch_ref.status != "done":
            harvested = harvest_subflow_outputs(
                Path(branch_ref.subflow_run_dir), outputs_map,
            )
            scope = parent.branch_scopes.setdefault(
                branch_id, BranchScope(branch_id=branch_id, parent_node=subflow_node_name),
            )
            for k, v in harvested.items():
                # T22 / I1: enforce 16 KB ceiling at every internal write
                # path. `harvest_subflow_outputs` already pre-checks but
                # checking here too keeps the contract local to the write
                # site (defence in depth — a future caller might bypass
                # harvest and write directly).
                check_var_size(k, v)
                scope.variables[k] = v
            branch_ref.status = "done"
            parent.save()

    # --- Critical section 2: reducer firing (if configured) -------------
    # `flow` and `node_def` are still in scope from CS1 (Python `with` does
    # not create new scope). The reducer subprocess is invoked from inside
    # the lock — mirrors the existing `_record_join_arrival` pattern (Task 4)
    # where _fire_reducer is called serially inside `advance()`. This keeps
    # state mutations atomic relative to other state_lock holders on the
    # same parent run dir.
    if reducer_node.reducer_script and reducer_node.summary_var:
        from flowstate.traversal import _fire_reducer
        with state_lock(parent_path):
            parent = RunState.load(parent_path)
            # _fire_reducer reads/appends to state.phases[join_name].join_history.
            # For the dynamic_fanout push path the parent has not yet advanced
            # into the join (children push via the runtime hook before advance
            # gets there), so the PhaseState needs to be initialised first.
            # Without this setdefault, _fire_reducer raises KeyError on the
            # first arrival from a fanout child. T4's fork/join path doesn't
            # hit this because _record_join_arrival lands on the join via
            # advance, which creates the PhaseState as a side effect.
            parent.phases.setdefault(reducer_node.name, PhaseState(status="in_progress"))
            _fire_reducer(
                parent, flow,
                join_name=reducer_node.name,
                triggering_branch_id=branch_id,
            )
            # If every fanout branch is now in a terminal status, flip the
            # join to ready_to_fire so a later advance can fire it downstream.
            # The push hook is the only place this transition can happen for
            # dynamic_fanout (the fork/join path uses _record_join_arrival,
            # which checks the same condition). Without this, the parent
            # blocks at the join forever even though all children are done.
            fanout_phase = parent.phases.get(subflow_node_name)
            # branch_is_terminal = {done, error}: same predicate the fanout
            # completion path uses. idle is NOT terminal — the branch is
            # fielding a human prompt and will resume. Spec D1.
            if fanout_phase is not None and all(
                branch_is_terminal(b.status) for b in fanout_phase.branches
            ):
                parent.phases[reducer_node.name].status = "ready_to_fire"
                parent.append_event("join_ready_to_fire_after_fanout", {
                    "join": reducer_node.name,
                    "fanout": subflow_node_name,
                })
                parent.save()

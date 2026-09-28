from __future__ import annotations

import argparse
import re
import sys
from dataclasses import asdict
from datetime import datetime, timezone
from pathlib import Path

import yaml

from flowstate.cli_util import emit as _emit, safe_handler
from flowstate.parser import VALID_SUPERVISION_LEVELS, load_flow
from flowstate.render import resolve_path_template
from flowstate.repo_root import RepoRootError, discover_repo_root, to_repo_relative
from flowstate.result import Result
from flowstate.state import RunState, state_lock
from flowstate.time import compact_stamp
from flowstate.traversal import _eval_vars_for_node, _exit_idle, _resolve_max_concurrent, _vars_for_node, advance, advance_autochase, decide_next_action, next_nodes
from flowstate.validate import validate_phase
from flowstate.vcs import VcsError, merge_branch, remove_worktree


def _resolve_userid() -> str:
    import subprocess
    # Timeout: every other subprocess in flowstate (vcs.py, gates.py,
    # wrapup.py) is timed; a hung git here (broken hook, NFS) shouldn't
    # block the whole CLI. See Issue #44g.
    try:
        proc = subprocess.run(
            ["git", "config", "user.email"], capture_output=True, text=True,
            timeout=5,
        )
    except subprocess.TimeoutExpired:
        raise RuntimeError(
            "`git config user.email` did not complete within 5 seconds; "
            "check for broken hooks or hung filesystem"
        )
    email = proc.stdout.strip()
    if not email:
        raise RuntimeError(
            "git user.email is not set; flowstate runs require a configured "
            "git identity for the _userid system variable"
        )
    return email.split("@", 1)[0]


def _init_state(
    *,
    flow,
    flow_dot: Path,
    repo_root: Path,
    run_dir: Path,
    userid: str,
    timestamp: str,
    run_descriptor: str,
    supervision: str,
    orchestrator_session_id: str | None,
    orchestrator_harness: str | None = None,
    orchestrator_identity_source: str = "self_reported",
) -> RunState:
    """Materialise a new ``RunState`` on disk. Shared by ``cmd_init`` and
    ``cmd_bootstrap``; see Issue #46 — both used to duplicate the variable-
    build + ``RunState.create`` call sequence verbatim.

    Idempotently creates ``run_dir`` and seeds the canonical system variables
    (``_userid``, ``_timestamp``, ``_run_descriptor``, ``_flow_stem``,
    ``_run_artefact_dir``, ``_supervision``, ``_supervision_instructions``)
    plus any user-declared variables (each populated with its declared
    default). Caller wraps in ``try/except`` if it needs structured error
    handling — this helper does not catch.
    """
    flow_dot_rel = to_repo_relative(flow_dot, repo_root)
    flow_dir_rel = to_repo_relative(flow.flow_dir, repo_root)
    run_dir.mkdir(parents=True, exist_ok=True)
    run_dir_rel = to_repo_relative(run_dir, repo_root)
    flow_stem = flow_dot.stem

    variables: dict[str, object] = {
        "_userid": userid,
        "_timestamp": timestamp,
        "_run_descriptor": run_descriptor,
        "_flow_stem": flow_stem,
        "_run_artefact_dir": run_dir_rel,
        "_supervision": supervision,
        "_supervision_instructions": flow.supervision_instructions.get(supervision, ""),
    }
    for name, spec in flow.variables.items():
        variables[name] = spec.default

    return RunState.create(
        run_dir=run_dir,
        flow_name=flow_stem,
        flow_dir=Path(flow_dir_rel),
        flow_dot=flow_dot_rel,
        repo_root=repo_root,
        run_descriptor=run_descriptor,
        userid=userid,
        timestamp=timestamp,
        start_node=flow.graph.start_node().name,
        variables=variables,
        supervision=supervision,
        orchestrator_session_id=orchestrator_session_id,
    )
    # Stamped so a reader can tell an attested identity from a self-reported
    # one without inspecting the registry.
    state.orchestrator_harness = orchestrator_harness
    state.orchestrator_identity_source = orchestrator_identity_source
    state.save()


def cmd_init(args: argparse.Namespace) -> int:
    repo_root = discover_repo_root()
    flow_dot = Path(args.flow_dot).resolve()
    flow = load_flow(flow_dot)
    run_dir = Path(args.run_dir).resolve()
    userid = _resolve_userid()
    timestamp = compact_stamp()

    try:
        orch_sid, orch_harness, orch_source = _resolve_orchestrator_identity(args)
    except ValueError as exc:
        return _emit(Result.error(str(exc)))

    state = _init_state(
        flow=flow,
        flow_dot=flow_dot,
        repo_root=repo_root,
        run_dir=run_dir,
        userid=userid,
        timestamp=timestamp,
        run_descriptor=args.run_descriptor,
        supervision=args.supervision,
        orchestrator_session_id=orch_sid,
        orchestrator_harness=orch_harness,
        orchestrator_identity_source=orch_source,
    )
    return _emit(Result.ok(payload={
        "run_id": state.run_id,
        "run_dir": str(run_dir),
        "userid": userid,
        "timestamp": timestamp,
        "run_descriptor": args.run_descriptor,
        "orchestrator_session_id": orch_sid,
        "orchestrator_identity_source": orch_source,
    }))


_BOOTSTRAP_INTENT = "Bootstrap factory graph run"


def _bootstrap_error(failure: str, suggestion: str) -> int:
    """Emit a structured bootstrap error. Every bootstrap failure path goes
    through this helper so the orchestrator gets a uniform recovery contract:
    intent / failure (concrete WHAT) / suggestion (concrete HOW to fix).
    See Issue #37.
    """
    return _emit(Result.error(
        intent=_BOOTSTRAP_INTENT,
        failure=failure,
        suggestion=suggestion,
    ))


def cmd_bootstrap(args: argparse.Namespace) -> int:
    """Collapse prefs read + init + summary + first advance into one CLI call.

    See Issue #37. The orchestrator's startup used to require ~5 round-trips
    (read prefs, compute paths, init, summary, first advance). bootstrap does
    all of them in one CLI invocation and returns a comprehensive envelope
    including the first agent node's `node_config` + `rendered_prompt`.

    Every error path returns `Result.error` with concrete WHAT (`failure`)
    and concrete HOW-to-recover (`suggestion`).
    """
    # -- Step 1: discover repo root --
    try:
        repo_root = discover_repo_root()
    except RepoRootError as e:
        return _bootstrap_error(
            failure=f"could not find a git repo root: {e}",
            suggestion="Run bootstrap from inside a git working tree (the factory repo root or a subdirectory).",
        )

    # -- Step 2: resolve and read prefs --
    prefs_path = Path(args.prefs_path) if args.prefs_path else (repo_root / "factory" / "factory-prefs.yml")
    prefs_example = Path(args.prefs_example) if args.prefs_example else (repo_root / "factory" / "factory-prefs-example.yml")
    if not prefs_path.is_absolute():
        prefs_path = repo_root / prefs_path
    if not prefs_example.is_absolute():
        prefs_example = repo_root / prefs_example

    prefs_created_from_example = False
    if not prefs_path.exists():
        if not prefs_example.exists():
            return _bootstrap_error(
                failure=f"neither {prefs_path} nor {prefs_example} exists",
                suggestion=(
                    f"Create {prefs_example} with at minimum:\n"
                    "  supervision: low\n"
                    "  default_graph: <flow-stem>\n"
                    f"The runtime file ({prefs_path}) will be copied from it on the next bootstrap."
                ),
            )
        try:
            prefs_path.write_text(prefs_example.read_text())
            prefs_created_from_example = True
        except OSError as e:
            return _bootstrap_error(
                failure=f"could not copy {prefs_example} to {prefs_path}: {e}",
                suggestion="Check filesystem permissions on the factory/ directory.",
            )
    try:
        prefs_raw = prefs_path.read_text()
    except OSError as e:
        return _bootstrap_error(
            failure=f"could not read {prefs_path}: {e}",
            suggestion="Check that the file is readable.",
        )
    try:
        prefs = yaml.safe_load(prefs_raw) or {}
    except yaml.YAMLError as e:
        return _bootstrap_error(
            failure=f"could not parse {prefs_path} as YAML: {e}",
            suggestion=f"Fix the YAML syntax in {prefs_path}. Compare against {prefs_example} if needed.",
        )
    if not isinstance(prefs, dict):
        return _bootstrap_error(
            failure=f"{prefs_path} did not parse as a YAML mapping (got {type(prefs).__name__})",
            suggestion=f"Replace contents with a YAML object. Example: {prefs_example}",
        )
    prefs_supervision = prefs.get("supervision")
    prefs_default_graph = prefs.get("default_graph")

    # -- Step 3: resolve supervision (CLI flag > prefs > built-in low) --
    supervision = args.supervision or prefs_supervision or "low"
    if supervision not in VALID_SUPERVISION_LEVELS:
        return _bootstrap_error(
            failure=f"supervision={supervision!r} is not one of {list(VALID_SUPERVISION_LEVELS)}",
            suggestion=(
                "Pass --supervision <afk|low|medium|high> on the bootstrap call, "
                f"or set `supervision: <level>` in {prefs_path}."
            ),
        )

    # -- Step 4: resolve flow_dot (CLI flag > prefs default_graph) --
    if args.flow_dot:
        flow_dot = Path(args.flow_dot)
    elif prefs_default_graph:
        flow_dot = repo_root / "factory" / "flows" / prefs_default_graph / f"{prefs_default_graph}.dot"
    else:
        return _bootstrap_error(
            failure="no flow DOT path resolved (no --flow-dot flag and no `default_graph` in prefs)",
            suggestion=(
                f"Pass --flow-dot <path/to/.dot> on the bootstrap call, "
                f"or set `default_graph: <stem>` in {prefs_path} (resolves to "
                f"factory/flows/<stem>/<stem>.dot)."
            ),
        )
    if not flow_dot.is_absolute():
        flow_dot = (repo_root / flow_dot).resolve()
    else:
        flow_dot = flow_dot.resolve()
    if not flow_dot.exists():
        return _bootstrap_error(
            failure=f"flow DOT file does not exist at {flow_dot}",
            suggestion=(
                "Check the path. The convention is "
                "factory/flows/<stem>/<stem>.dot relative to repo root."
            ),
        )

    # -- Step 5: validate run_descriptor --
    run_descriptor = (args.run_descriptor or "").strip()
    if not run_descriptor:
        return _bootstrap_error(
            failure="--run-descriptor is required and must be non-empty",
            suggestion="Pass --run-descriptor <name> identifying this run (e.g., a Jira ticket slug).",
        )
    if "/" in run_descriptor or run_descriptor.startswith("-"):
        return _bootstrap_error(
            failure=(
                f"run-descriptor {run_descriptor!r} contains invalid characters "
                "(no '/', must not start with '-')"
            ),
            suggestion="Use a path-safe identifier (alphanumerics, hyphens, dots, underscores).",
        )

    # -- Step 6: resolve userid + timestamp + run_dir --
    flow_stem = flow_dot.stem
    try:
        userid = _resolve_userid()
    except RuntimeError as e:
        return _bootstrap_error(
            failure=str(e),
            suggestion="Run `git config user.email <your-email>` in this repo before retrying bootstrap.",
        )
    timestamp = compact_stamp()
    run_dir = repo_root / "factory" / "graph_runs" / flow_stem / f"{run_descriptor}_{userid}_{timestamp}"

    # -- Step 7: load flow + create state (via shared _init_state helper) --
    try:
        _bs_sid, _bs_harness, _bs_source = _resolve_orchestrator_identity(args)
    except ValueError as exc:
        return _bootstrap_error(
            failure=str(exc),
            suggestion=(
                "Pass the session id the spawn TOLD this orchestrator (the "
                "'Your session id' line of its prompt), or drop "
                "--orchestrator-session-id and let the spawn record stand alone."
            ),
        )

    try:
        flow = load_flow(flow_dot)
    except Exception as e:  # parser ValueError etc.
        return _bootstrap_error(
            failure=f"could not load flow from {flow_dot}: {e}",
            suggestion=(
                "Check the flow DOT + .flow.yml + definitions/ for syntax errors. "
                "Try `flowstate init <dot> --run-dir <tmp> ...` directly for a focused error."
            ),
        )

    try:
        state = _init_state(
            flow=flow,
            flow_dot=flow_dot,
            repo_root=repo_root,
            run_dir=run_dir,
            userid=userid,
            timestamp=timestamp,
            run_descriptor=run_descriptor,
            supervision=supervision,
            orchestrator_session_id=_bs_sid,
            orchestrator_harness=_bs_harness,
            orchestrator_identity_source=_bs_source,
        )
    except Exception as e:
        return _bootstrap_error(
            failure=f"could not initialise run state in {run_dir}: {e}",
            suggestion="Check that the run_dir is writable and not already populated.",
        )

    # -- Step 7.5: seed user-supplied variables BEFORE the first advance --
    # Without this, prompt templates referencing `freeform_input` (or any
    # other user-supplied var) fail to render on the bootstrap's own
    # first advance, forcing the orchestrator into a recovery dance
    # (set-var + render-prompt + node-config). With seed-vars, bootstrap
    # is one round-trip from "start a run" to "ready to spawn the first
    # worker". Issue raised in the 2026-06-29 batch-factory-pipeline E2E.
    import json as _json
    seeded_vars: dict[str, object] = {}
    for raw in (args.seed_var or []):
        if "=" not in raw:
            return _bootstrap_error(
                failure=f"--seed-var value {raw!r} is missing '='",
                suggestion="Pass --seed-var NAME=VALUE (e.g. --seed-var freeform_input=\"…\").",
            )
        name, _, val = raw.partition("=")
        name = name.strip()
        if not name:
            return _bootstrap_error(
                failure=f"--seed-var value {raw!r} has an empty variable name",
                suggestion="Pass --seed-var NAME=VALUE with a non-empty NAME.",
            )
        seeded_vars[name] = val
    for raw in (args.seed_var_json or []):
        if "=" not in raw:
            return _bootstrap_error(
                failure=f"--seed-var-json value {raw!r} is missing '='",
                suggestion="Pass --seed-var-json NAME=JSON (e.g. --seed-var-json tickets='[…]').",
            )
        name, _, val = raw.partition("=")
        name = name.strip()
        if not name:
            return _bootstrap_error(
                failure=f"--seed-var-json value {raw!r} has an empty variable name",
                suggestion="Pass --seed-var-json NAME=JSON with a non-empty NAME.",
            )
        try:
            seeded_vars[name] = _json.loads(val)
        except _json.JSONDecodeError as e:
            return _bootstrap_error(
                failure=f"--seed-var-json value for {name!r} is not valid JSON: {e}",
                suggestion=(
                    f"Quote the JSON exactly. For a list of dicts: "
                    f'--seed-var-json {name}=\'[{{"k":"v"}}]\'.'
                ),
            )

    if seeded_vars:
        with state_lock(run_dir):
            state = RunState.load(run_dir)
            for name, val in seeded_vars.items():
                state.set_var(name, val)
            state.save()

    # -- Step 8: summary payload --
    agent_nodes = [n for n in flow.graph.nodes if n.shape == "box"]
    end_nodes = [n for n in flow.graph.nodes if n.shape == "Msquare"]
    summary_payload = {
        "graph": {
            "name": flow.graph.label or flow_stem,
            "description": flow.graph.description,
            "total_agent_nodes": len(agent_nodes),
            "end_node_names": [n.name for n in end_nodes],
            # Full topology digest so the orchestrator never has to read the
            # DOT file to orient: one entry per node (runner + category +
            # description) and per edge (condition / label / gates). Edge
            # descriptions are truncated — `flowstate summary` or the DOT
            # itself carries the full text when a step needs it.
            "nodes": [
                {
                    "name": n.name,
                    "runner": n.runner,
                    **({"category": n.category} if n.category else {}),
                    **({"output_schema": n.output_schema} if n.output_schema else {}),
                    **({"description": n.description} if n.description else {}),
                }
                for n in flow.graph.nodes
            ],
            "edges": [
                {
                    "from": e.source,
                    "to": e.target,
                    **({"condition": e.condition} if e.condition else {}),
                    **({"label": e.label} if e.label else {}),
                    **({"gates": list(e.gates)} if e.gates else {}),
                    **(
                        {"description": (e.description[:160] + "…")
                         if len(e.description) > 160 else e.description}
                        if e.description else {}
                    ),
                }
                for e in flow.graph.edges
            ],
        }
    }

    # -- Step 9: first advance (start -> first agent node), envelope-mode aware --
    with state_lock(run_dir):
        state = RunState.load(run_dir)
        advance_payload = _advance_with_envelope(flow, state)

    return _emit(Result.ok(payload={
        "prefs": {
            "supervision": prefs_supervision,
            "default_graph": prefs_default_graph,
            "created_from_example": prefs_created_from_example,
            "path": str(prefs_path),
        },
        "resolved": {
            "supervision": supervision,
            "flow_dot": str(flow_dot),
            "flow_stem": flow_stem,
            "run_descriptor": run_descriptor,
            "userid": userid,
            "timestamp": timestamp,
            "run_dir": str(run_dir),
        },
        "init": {
            "run_id": state.run_id,
            "orchestrator_session_id": args.orchestrator_session_id,
        },
        "seeded_vars": seeded_vars,
        "summary": summary_payload,
        "advance": advance_payload,
    }))


def cmd_status(args: argparse.Namespace) -> int:
    state = RunState.load(Path(args.run_dir))
    node = getattr(args, "node", None)
    if node is not None:
        # --node given: report just that node's status.
        phase_state = state.phases.get(node)
        payload = {
            "run_id": state.run_id,
            "node": node,
            "current_phase": node,
            "supervision": state.supervision,
            "phases": {node: asdict(phase_state)} if phase_state is not None else {},
        }
    elif len(state.current_phases) > 1:
        # Multi-cursor: report the full cursor set without calling current_phase
        # (which would raise ValueError). The caller can pass --node to narrow down.
        payload = {
            "run_id": state.run_id,
            "current_phases": sorted(state.current_phases),
            "supervision": state.supervision,
            "phases": {name: asdict(p) for name, p in state.phases.items()},
        }
    else:
        payload = {
            "run_id": state.run_id,
            "current_phase": state.sole_cursor(),
            "supervision": state.supervision,
            "phases": {name: asdict(p) for name, p in state.phases.items()},
        }
    payload["branches"] = _build_branches_view(state)
    payload["branch_summaries"] = _build_branch_summaries(state)
    return _emit(Result.ok(payload=payload))


def _build_branch_summaries(state: RunState) -> dict[str, str]:
    """Task 9 / spec §4.7: per-phase one-line dashboard summary for
    dynamic_fanout phases, e.g.

        cap=3 · 2 in_progress · 1 idle · 1 pending · 1 done · 0 error · 5 total

    ``cap=∞`` when the resolved ``max_concurrent`` is 0 (unbounded). Only
    emitted for phases whose node has ``runner=dynamic_fanout`` — other
    branched phases (static fork) don't have a meaningful cap.

    Returns an empty dict when no dynamic_fanout phase has branches yet, so
    orchestrator dashboards can consume the key unconditionally.
    """
    # Don't load the flow at all unless we already see at least one phase
    # with branches; saves a flow parse on every plain `status` call.
    if not any(p.branches for p in state.phases.values()):
        return {}
    try:
        flow = load_flow(state.repo_root / state.flow_dot)
    except Exception:
        # The status CLI must keep working even if the flow file is broken
        # or unreadable — degrade to no summaries rather than failing the
        # whole call. See the broader pattern in `_build_branches_view`.
        return {}
    prefs_path = state.repo_root / "factory" / "factory-prefs.yml"
    summaries: dict[str, str] = {}
    for phase_name, phase in state.phases.items():
        if not phase.branches:
            continue
        try:
            node = flow.graph.node(phase_name)
        except KeyError:
            continue
        if node.runner != "dynamic_fanout":
            continue
        try:
            max_concurrent = _resolve_max_concurrent(node, state.flow_name, prefs_path)
        except Exception:
            max_concurrent = None
        cap_str = "cap=∞" if max_concurrent == 0 else (
            f"cap={max_concurrent}" if max_concurrent is not None else "cap=?"
        )
        counts = {
            "in_progress": 0, "idle": 0, "pending": 0, "done": 0, "error": 0,
        }
        for b in phase.branches:
            if b.status in counts:
                counts[b.status] += 1
        total = len(phase.branches)
        summaries[phase_name] = (
            f"{cap_str} · {counts['in_progress']} in_progress · "
            f"{counts['idle']} idle · {counts['pending']} pending · "
            f"{counts['done']} done · {counts['error']} error · "
            f"{total} total"
        )
    return summaries


def _build_branches_view(state: RunState) -> dict[str, dict[str, object]]:
    """D6: per-branch dashboard view. ALWAYS returned (empty for single-cursor
    runs). Completed branches surface their ``branch_summary[bid]`` entry;
    in-progress subflow branches synthesise a live snapshot from the child run.

    See Task 23. The orchestrator skill consumes this dict unconditionally —
    keep the key present even when no branches exist on this run.
    """
    import json as _json

    # Reducer-output branch_summary may serialise as a JSON string in some
    # transports. Normalise to a dict (or {} if non-dict / non-decodable).
    branch_summary_raw = state.variables.get("branch_summary") or {}
    if isinstance(branch_summary_raw, str):
        try:
            branch_summary_raw = _json.loads(branch_summary_raw)
        except (ValueError, _json.JSONDecodeError):
            branch_summary_raw = {}
    if not isinstance(branch_summary_raw, dict):
        branch_summary_raw = {}

    branches_view: dict[str, dict[str, object]] = {}
    terminal_statuses = ("done", "done_forced", "done_bypassed", "error")

    for _phase_name, phase in state.phases.items():
        for branch_ref in phase.branches:
            entry: dict[str, object] = {"status": branch_ref.status}
            if branch_ref.status in terminal_statuses:
                if branch_ref.branch_id in branch_summary_raw:
                    entry["summary"] = branch_summary_raw[branch_ref.branch_id]
                if branch_ref.failure_reason:
                    entry["error_reason"] = branch_ref.failure_reason
            else:
                # Pending or in-progress: synthesise a live snapshot.
                if branch_ref.subflow_run_dir:
                    entry["subflow_run_dir"] = branch_ref.subflow_run_dir
                    # Try to peek the child's current cursor + last event.
                    # Child may not exist yet or be unreadable — swallow and
                    # continue so a flaky child can't break the dashboard.
                    try:
                        child = RunState.load(Path(branch_ref.subflow_run_dir))
                    except Exception:
                        child = None
                    if child is not None:
                        if child.current_phases:
                            entry["current_node"] = sorted(child.current_phases)[0]
                        if child.events:
                            last = child.events[-1]
                            ts = getattr(last, "t", None)
                            if ts is not None:
                                entry["last_event_at"] = ts
                elif branch_ref.inline_root_node:
                    # Static in-run fork branch — surface its declared root node.
                    entry["root_node"] = branch_ref.inline_root_node
            branches_view[branch_ref.branch_id] = entry

    return branches_view


def cmd_next(args: argparse.Namespace) -> int:
    state = RunState.load(Path(args.run_dir).resolve())
    flow = load_flow(state.repo_root / state.flow_dot)
    nodes = next_nodes(flow.graph, state)
    return _emit(Result.ok(payload={"nodes": [{"name": n.name, "shape": n.shape} for n in nodes]}))


def _seconds_in_phase(phase_state, now: datetime) -> int | None:
    """How long the worker has been (or was) in this phase. Returns None if
    the phase hasn't started yet."""
    if not phase_state.started_at:
        return None
    started = datetime.strptime(phase_state.started_at, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)
    end_iso = phase_state.completed_at if phase_state.status in ("done", "done_forced") else None
    end = datetime.strptime(end_iso, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc) if end_iso else now
    return int((end - started).total_seconds())


def cmd_summary(args: argparse.Namespace) -> int:
    """State-aware current-context payload for orchestrator narration.

    Returns the graph metadata, progress counts, cost rollup, current phase
    info, outgoing edges from current with target metadata, and the last few
    events. Cheap and idempotent — orchestrator may call freely.
    """
    state = RunState.load(Path(args.run_dir).resolve())
    flow = load_flow(state.repo_root / state.flow_dot)

    # Refresh orchestrator usage opportunistically so the totals stay close
    # to live. Cheap (one transcript re-parse) and read-only from the
    # caller's perspective; we save the refreshed state below. See Issue #17.
    state.refresh_orchestrator_usage()
    state.save()

    agent_nodes = [n for n in flow.graph.nodes if n.shape == "box"]
    end_nodes = [n for n in flow.graph.nodes if n.shape == "Msquare"]

    # Progress counts over named (box) phases.
    counts = {"completed": 0, "forced": 0, "in_progress": 0, "pending": 0}
    cumulative_fresh_tokens = 0
    cumulative_cache_read_tokens = 0
    cumulative_turns = 0
    for n in agent_nodes:
        p = state.phases.get(n.name)
        status = p.status if p else "pending"
        if status == "done":
            counts["completed"] += 1
        elif status == "done_forced":
            counts["completed"] += 1
            counts["forced"] += 1
        elif status == "in_progress":
            counts["in_progress"] += 1
        else:
            counts["pending"] += 1
        if p:
            # Include prior_attempts so respawn cost is captured. Issue #6.
            _t = p.total_tokens()
            cumulative_fresh_tokens += _t["fresh"]
            cumulative_cache_read_tokens += _t["cache_read"]
            cumulative_turns += p.total_turns()

    now = datetime.now(timezone.utc)

    def _phase_payload(phase_name: str) -> dict[str, object] | None:
        """Build the current-phase payload for one active cursor. Skips
        start/end nodes (no PhaseState contract) with a minimal payload."""
        node = next((n for n in flow.graph.nodes if n.name == phase_name), None)
        ps = state.phases.get(phase_name)
        if node is None:
            return None
        if node.shape == "box" and ps is not None:
            p: dict[str, object] = {
                "name": node.name,
                "description": node.description,
                "category": node.category,
                "status": ps.status,
                "started_at": ps.started_at,
                "completed_at": ps.completed_at,
                "seconds_in_phase": _seconds_in_phase(ps, now),
                "turns": ps.usage.turns if ps.usage else 0,
                # Tokens, not dollars (2026-08-10): fresh and cache-read are
                # reported separately and never summed — they differ ~28x.
                "tokens": ps.total_tokens(),
                "agent_session_id": ps.agent_session_id,
            }
            if ps.notes:
                p["notes"] = ps.notes
            return p
        # start or end node — minimal payload.
        return {
            "name": node.name,
            "description": node.description,
            "shape": node.shape,
        }

    def _edge_payloads(phase_name: str) -> list[dict[str, object]]:
        """Outgoing edges from one active cursor, with target descriptions."""
        out: list[dict[str, object]] = []
        for edge in flow.graph.edges_from(phase_name):
            target_node = next((n for n in flow.graph.nodes if n.name == edge.target), None)
            out.append({
                "target": edge.target,
                "target_description": target_node.description if target_node else None,
                "target_shape": target_node.shape if target_node else None,
                "label": edge.label,
                "condition": edge.condition,
                "description": edge.description,
                "gates": list(edge.gates),
            })
        return out

    # Multi-cursor-safe current/next reporting. Never read state.current_phase
    # (the singular property) when >1 cursor is active — it raises ValueError.
    # See Task 15 follow-up.
    active = sorted(state.current_phases)
    current_payload: dict[str, object] | None = None
    multi_current_payload: list[dict[str, object]] | None = None
    next_targets: list[dict[str, object]] = []
    if len(active) == 1:
        only = active[0]
        current_payload = _phase_payload(only)
        next_targets = _edge_payloads(only)
    elif len(active) > 1:
        # Forked run: report every active cursor's phase + outgoing edges.
        multi_current_payload = [
            p for p in (_phase_payload(name) for name in active) if p is not None
        ]
        for name in active:
            next_targets.extend(_edge_payloads(name))

    # Last events, slimmed for narration context.
    recent = []
    for e in (state.events or [])[-5:]:
        slim = {"t": e.t, "kind": e.kind}
        for k in ("node", "target", "reason", "feedback"):
            v = e.payload.get(k)
            if v is not None:
                slim[k] = v
        recent.append(slim)

    # Worker free-text notes — only phases with non-empty notes, keyed by
    # node name. Lets the orchestrator surface caveats from past phases
    # without re-reading every phase block.
    notes_by_phase: dict[str, str] = {}
    for name, p in state.phases.items():
        if p.notes:
            notes_by_phase[name] = p.notes

    payload = {
        "graph": {
            "name": flow.graph.label or state.flow_name,
            "description": flow.graph.description,
            "total_agent_nodes": len(agent_nodes),
            "end_node_names": [n.name for n in end_nodes],
        },
        "progress": counts,
        # Renamed from "cost" (2026-08-10): dollar estimates were removed
        # entirely — stale rate tables, an "assume opus" fallback that
        # mispriced unrecognised models, and no way for a reader to tell an
        # estimate from a measurement. Fresh and cache-read are reported
        # separately and never summed; they differ by ~28x.
        "usage": {
            "cumulative_worker_fresh_tokens": cumulative_fresh_tokens,
            "cumulative_worker_cache_read_tokens": cumulative_cache_read_tokens,
            "cumulative_turns": cumulative_turns,
            "orchestrator_turns": (
                int(state.orchestrator_usage.turns)
                if state.orchestrator_usage else 0
            ),
            "total_tokens": state.total_tokens(),
        },
        "current": current_payload,
        "next": next_targets,
        "recent_events": recent,
    }
    if multi_current_payload is not None:
        # Forked run: surface the full active-cursor set and per-cursor blocks.
        # `current` stays None (no single current node); callers branch on
        # `current_phases` being present.
        payload["current_phases"] = active
        payload["current_nodes"] = multi_current_payload
    if notes_by_phase:
        payload["notes_by_phase"] = notes_by_phase
    return _emit(Result.ok(payload=payload))


def cmd_vars(args: argparse.Namespace) -> int:
    state = RunState.load(Path(args.run_dir))
    payload = state.variables if args.name is None else {args.name: state.variables.get(args.name)}
    result = Result.ok(payload=payload)
    if getattr(args, "format", "yaml") == "json":
        sys.stdout.write(result.to_json())
        sys.stdout.write("\n")
        return 0
    return _emit(result)


def cmd_set_var(args: argparse.Namespace) -> int:
    # Serialise load+mutate+save against concurrent CLI invocations. See Issue #27.
    run_dir = Path(args.run_dir)
    value: object = args.value
    if getattr(args, "json", False):
        import json as _json
        try:
            value = _json.loads(args.value)
        except _json.JSONDecodeError as exc:
            return _emit(Result.error(
                intent=f"set-var {args.name!r} --json",
                failure=f"value is not valid JSON: {exc}",
                suggestion=(
                    "Quote the JSON exactly as a shell string. For a list of "
                    "dicts: --json '[{\"jira_ref\":\"DSCO-1\"}]'."
                ),
            ))
    node = getattr(args, "node", None)
    with state_lock(run_dir):
        state = RunState.load(run_dir)
        # When --node names a branch cursor, the write lands in that branch's
        # scope so the branch's own conditional edges (and the merge-at-join
        # delta) see it. Non-branch nodes (branch_id None) fall back to
        # run-level, so passing --node is always safe.
        branch_id = None
        if node is not None:
            phase = state.phases.get(node)
            branch_id = phase.branch_id if phase is not None else None
        state.set_var(args.name, value, branch_id=branch_id)
        # Audit every CLI-driven variable write so post-hoc forensics can see
        # who wrote what (the batch-factory-pipeline E2E had zero trace of the
        # trunk writes a stale-seed join then clobbered). CLI-only: internal
        # set_var callers stay silent, and reducers emit their own events.
        # Spec D8 row 8.
        state.append_event("var_set", {
            "name": args.name,
            "node": node,
            "branch_id": branch_id,
            "value_preview": str(value)[:200],
        })
        state.save()
    return _emit(Result.ok(payload={args.name: value, "branch_id": branch_id}))




def _resolve_orchestrator_identity(args) -> tuple[str | None, str | None, str]:
    """(session_id, harness, source) for the orchestrator driving this run.

    Record when it was spawned by agentctl — which the graph-orchestrator
    skill's launcher mode makes the normal case, precisely so that a
    non-claude orchestrator has an attributable identity at all (self-report
    provably does not work there). Self-report only on the legacy path, and
    the source is stamped into metadata so nobody mistakes one for the other.

    Raises ValueError on a disputed identity — same rule as workers, and only
    when both sides are present.
    """
    rec = _agent_record(getattr(args, "orchestrator_agent_id", None),
                        getattr(args, "orchestrator_registry_dir", None))
    reported = getattr(args, "orchestrator_session_id", None)
    if not rec:
        return reported, None, "self_reported"
    sid = rec.get("agent_session_id") or rec.get("claude_session_id")
    expected_echo = rec.get("spawn_token") or sid
    if expected_echo and reported and reported != expected_echo:
        raise ValueError(
            f"orchestrator session-id mismatch: the spawn record says "
            f"{expected_echo!r} but --orchestrator-session-id is "
            f"{reported!r}. The orchestrator's own usage and transcript "
            f"cannot be attributed under a disputed identity")
    return sid, rec.get("harness"), "record"


def _agent_record(agent_id: str | None, registry_dir) -> dict | None:
    """The agentctl spawn record — source of truth for the worker's session
    identity and harness (2026-08-10). None when no agent id was supplied
    (standalone validate) or the record is unreadable."""
    if not agent_id:
        return None
    import yaml as _yaml
    reg = Path(registry_dir) if registry_dir else Path.home() / ".agentctl"
    path = reg / f"{agent_id}.yml"
    if not path.exists():
        return None
    try:
        return _yaml.safe_load(path.read_text()) or None
    except Exception:
        return None


def _harness_session_usage(harness_name: str | None, session_id: str,
                           registry_dir=None):
    """Usage for one worker session, dispatched on the RECORDED harness.

    None/claude-code -> the claude transcript parse. Anything else -> that
    harness's `session_usage` capability. Never raises: usage is advisory,
    and a zero-turn result must not overwrite recorded usage (Issue #28).
    """
    from flowstate.state import Usage
    try:
        if harness_name in (None, "claude-code"):
            from flowstate.transcripts import parse_session_usage
            return parse_session_usage(session_id)
        from agentctl.harnesses.factory import make_harness
        reg = Path(registry_dir) if registry_dir else Path.home() / ".agentctl"
        d = make_harness(harness_name, reg).session_usage(session_id)
        if d is None:
            return None
        return Usage(
            input_tokens=d["input_tokens"], output_tokens=d["output_tokens"],
            cache_read_input_tokens=d["cache_read_input_tokens"],
            cache_creation_input_tokens=d["cache_creation_input_tokens"],
            reasoning_tokens=d["reasoning_tokens"],
            turns=d["turns"], model=d["model"])
    except Exception:
        return None


def _build_node_config_payload(flow, state: RunState, node_name: str) -> dict[str, object]:
    """Build the node-config payload for the given node. Shared between
    `cmd_node_config` and `cmd_advance`'s envelope mode (Issue #7a).
    """
    from flowstate.temp_layout import phase_temp_dir
    node = flow.graph.node(node_name)
    payload: dict[str, object] = {
        "name": node.name,
        "runner": node.runner,
        "model": node.model,
        # Surfaced alongside `model` so the orchestrator spawns the worker on
        # the harness the graph asked for. Before this, `model` reached the
        # spawn and `harness` did not, so an opencode-model node was launched
        # under claude-code and died on an access error.
        "harness": node.harness,
        "autonomy": node.autonomy,
        "pauses_at_min": node.pauses_at_min,
        "description": node.description,
        "category": node.category,
        # Canonical per-node orchestration temp dir. The orchestrator skill
        # passes this to `agentctl spawn --temp-dir` so agentctl no longer
        # has to reach into flowstate to compute its own layout. See
        # Issue #45b.
        "temp_dir": str(phase_temp_dir(
            state.repo_root,
            state.flow_name,
            state.run_descriptor,
            state.userid,
            state.timestamp,
            node.name,
        )),
    }
    # `working_dir` is optional for runner=orchestrator (no chdir needed).
    if node.working_dir:
        payload["working_dir"] = resolve_path_template(node.working_dir, state.variables, state.repo_root)
    if node.runner in ("agent", "orchestrator"):
        # flow.flow_dir was stored repo-relative; join with repo_root.
        payload["prompt_template"] = str(state.repo_root / state.flow_dir / node.prompt_template)
    elif node.runner == "script":
        payload["script"] = str(state.repo_root / state.flow_dir / node.script)
    # Control runners (fork / join / dynamic_fanout / subflow) carry neither a
    # prompt nor a script — only the common fields above apply. Previously they
    # fell into the script branch and crashed on `flow_dir / None` (seen on
    # `node-config entry_fork` in the 2026-07-09 batch e2e).
    if node.output_schema and node.output_schema in flow.schemas:
        schema = flow.schemas[node.output_schema]
        payload["outputs"] = [
            {
                "name": sf.name,
                "path": resolve_path_template(sf.path, state.variables, state.repo_root),
                # definition omitted for existence-only outputs (sf.definition is None).
                **({"definition": sf.definition} if sf.definition is not None else {}),
            }
            for sf in schema.files
        ]
        payload["sets_variables"] = dict(schema.sets_variables)
        if schema.required_variables:
            payload["required_variables"] = list(schema.required_variables)
    return payload


def _render_node_prompt(
    flow, state: RunState, node_name: str,
    *, variables: dict[str, object] | None = None,
) -> tuple[str | None, str | None]:
    """Render an agent or orchestrator node's prompt template. Returns
    (prompt, error_message). On success, error_message is None. On failure,
    prompt is None. Script nodes return (None, None) — they have no prompt
    by definition.

    ``variables`` overrides the substitution scope. Defaults to run-level
    ``state.variables`` (matching the standalone ``render-prompt`` command).
    Callers rendering a node that lives inside a fork/fanout branch pass the
    branch-scoped layered view (:func:`_eval_vars_for_node`) so branch-local
    placeholders resolve.
    """
    from flowstate.completion import prompt_boilerplate
    from flowstate.render import render_prompt, RenderError
    from flowstate.temp_layout import completion_path
    node = flow.graph.node(node_name)
    if node.runner not in ("agent", "orchestrator"):
        return None, None
    template_path = state.repo_root / state.flow_dir / node.prompt_template
    template = template_path.read_text()
    vars_view = variables if variables is not None else state.variables
    try:
        rendered = render_prompt(template, vars_view)
    except RenderError as exc:
        return None, str(exc)
    if node.runner == "agent":
        # The completion contract is appended HERE, not by the agentctl
        # harness (which now writes prompts verbatim — teams spec §5a).
        # Orchestrator-runner nodes execute inline and never spawn, so they
        # get no contract — same as before, when only spawned prompts
        # passed through the harness's append.
        rendered += prompt_boilerplate(completion_path(
            state.repo_root, state.flow_name, state.run_descriptor,
            state.userid, state.timestamp, node_name,
        ))
    return rendered, None


def cmd_node_config(args: argparse.Namespace) -> int:
    state = RunState.load(Path(args.run_dir).resolve())
    flow = load_flow(state.repo_root / state.flow_dot)
    return _emit(Result.ok(payload=_build_node_config_payload(flow, state, args.node)))


def cmd_render_prompt(args: argparse.Namespace) -> int:
    state = RunState.load(Path(args.run_dir).resolve())
    flow = load_flow(state.repo_root / state.flow_dot)
    node = flow.graph.node(args.node)
    if node.runner not in ("agent", "orchestrator"):
        return _emit(Result.error(
            intent=f"Render prompt for node {args.node!r}",
            failure=f"node has runner={node.runner!r}; only agent and orchestrator nodes have prompts",
            suggestion="render-prompt is intended for agent or orchestrator nodes only.",
        ))
    rendered, error = _render_node_prompt(flow, state, args.node)
    if error is not None:
        return _emit(Result.error(
            intent=f"Render prompt for node {args.node!r}",
            failure=error,
            suggestion="Set missing variables via `flowstate set-var` and retry.",
        ))
    # Payload key is `rendered_prompt` — same as the advance envelope (#7a)
    # so callers don't have to remember two key names for the same content.
    # See post-#37 E2E observation.
    return _emit(Result.ok(payload={"rendered_prompt": rendered, "node": args.node}))


def _advance_with_envelope(
    flow,
    state: RunState,
    *,
    force: bool = False,
    target: str | None = None,
    node: str | None = None,
    single_step: bool = False,
) -> dict[str, object]:
    """Run ``advance()`` and assemble the response payload, folding in
    envelope-mode fields (``node_config``, ``rendered_prompt``) when the
    move lands on an agent or orchestrator node. See Issues #7a + #46 —
    cmd_bootstrap's first-advance block and cmd_advance both used to
    inline this verbatim.

    By default the advance auto-chases through purely programmatic nodes
    (:func:`advance_autochase`): one CLI call moves as far as it can without
    orchestrator judgment and stops at the first agent/orchestrator/
    dynamic_fanout/subflow node, unresolvable choice, terminal, error, or
    parked join. The ``chased`` audit trail is folded into the payload. Pass
    ``single_step=True`` for the legacy one-hop behaviour (debugging / tests).

    When ``node`` is provided it is forwarded as the explicit cursor to advance
    (the first hop), allowing multi-branch orchestration to target a specific
    active phase. See Task 15.

    Caller must hold the run_dir state_lock for the duration of this call.
    """
    if single_step:
        outcome = advance(flow, state, force=force, target=target, node=node)
    else:
        outcome = advance_autochase(flow, state, force=force, target=target, node=node)
    payload: dict[str, object] = {"kind": outcome.kind}
    if outcome.chased is not None:
        payload["chased"] = outcome.chased
    if outcome.target:
        payload["target"] = outcome.target
    if outcome.reason:
        payload["reason"] = outcome.reason
    if outcome.options is not None:
        payload["options"] = outcome.options
        # Scope to the node where the choice actually arose. After an auto-chase
        # that ended in choice_needed the last chased hop names that node — it
        # may differ from the first-hop `node`, and a multi-cursor run would
        # otherwise trip sole_cursor()'s single-cursor guard. Fall back to the
        # explicit node / sole cursor for the single-step path.
        choice_node = None
        if outcome.chased:
            choice_node = outcome.chased[-1].get("node")
        payload["current"] = choice_node or (node if node is not None else state.sole_cursor())
    # Rolling-window dispatch surface for dynamic_fanout nodes
    # (spec §4.5). The orchestrator iterates this list to start subflows
    # one-at-a-time, gated by max_concurrent.
    if outcome.next_startable_branches is not None:
        payload["next_startable_branches"] = outcome.next_startable_branches
    if outcome.max_concurrent_resolved is not None:
        payload["max_concurrent_resolved"] = outcome.max_concurrent_resolved
    if outcome.kind == "moved" and outcome.target:
        target_node = flow.graph.node(outcome.target)
        if target_node.runner in ("agent", "orchestrator"):
            payload["node_config"] = _build_node_config_payload(flow, state, outcome.target)
            rendered, error = _render_node_prompt(flow, state, outcome.target)
            if rendered is not None:
                payload["rendered_prompt"] = rendered
            elif error is not None:
                # Don't fail the advance — surface the rendering error
                # alongside the move so the caller can decide. They can
                # also call `render-prompt` separately for the same error.
                payload["rendered_prompt_error"] = error
    # Multi-cursor surface: when the advance leaves more than one active
    # phase (e.g. a fork just registered its arms), name them and their
    # per-arm node configs inline so the orchestrator doesn't need a
    # follow-up `status` call to learn what started. Agent-arm configs let
    # it spawn workers straight from this envelope. See the 2026-07-09
    # batch e2e (fork advance returned a bare `kind: moved`).
    active = sorted(state.current_phases)
    if len(active) > 1:
        payload["active_phases"] = active
        arm_configs: dict[str, object] = {}
        for phase_name in active:
            try:
                arm_node = flow.graph.node(phase_name)
            except KeyError:
                continue
            if arm_node.runner in ("agent", "orchestrator"):
                cfg = _build_node_config_payload(flow, state, phase_name)
                # Orchestrator-inline arms are executed by the orchestrator
                # itself in-conversation, so inline the rendered prompt here to
                # save the extra `render-prompt` round-trip. Agent arms are NOT
                # inlined — their prompts can be long and the orchestrator
                # fetches them at spawn time. Render against the arm's own
                # branch-scoped layered view so branch-local placeholders
                # resolve. On a missing-VARIABLE render failure, omit the
                # field rather than fail the whole advance — the caller can
                # still call `render-prompt` for the concrete error. (A
                # missing/unreadable template FILE still propagates, same as
                # the single-cursor path.)
                if arm_node.runner == "orchestrator":
                    rendered, _err = _render_node_prompt(
                        flow, state, phase_name,
                        variables=_eval_vars_for_node(state, phase_name),
                    )
                    if rendered is not None:
                        cfg["rendered_prompt"] = rendered
                arm_configs[phase_name] = cfg
        if arm_configs:
            payload["active_phase_configs"] = arm_configs
    return payload


def cmd_advance(args: argparse.Namespace) -> int:
    # Serialise the full advance() call (which loads, mutates, and saves
    # internally several times) against concurrent CLI invocations. See Issue #27.
    run_dir = Path(args.run_dir).resolve()
    node = getattr(args, "node", None)
    admit = getattr(args, "admit", False)
    admit_status = getattr(args, "status", "done_forced") or "done_forced"
    with state_lock(run_dir):
        state = RunState.load(run_dir)
        # Multi-cursor guard: when multiple cursors are active and no --node is
        # given the caller cannot know which branch to advance. Fail early with
        # a clear message rather than letting state.current_phase raise an
        # opaque ValueError inside the traversal layer. See Task 15.
        # The guard applies equally to --admit (you must still name the stuck node).
        if node is None and len(state.current_phases) > 1:
            return _emit(Result.error(
                intent="Advance run",
                failure=(
                    f"multiple active phases {sorted(state.current_phases)!r}; "
                    f"pass --node to choose one"
                ),
                suggestion=(
                    "Pass --node <phase> where <phase> is one of the active cursors "
                    f"{sorted(state.current_phases)!r}."
                ),
            ))
        # --admit: escape hatch for a stuck/failed branch. Force-marks the
        # targeted node to the requested status (default: done_forced) without
        # any validation, appends an auditable admit_forced event, then proceeds
        # with the normal advance so the flow moves past the admitted node.
        # Works on in_progress, failed, or any other non-terminal status. The
        # multi-cursor --node guard above still applies. See Task 16.
        if admit:
            target_node = node if node is not None else state.sole_cursor()
            state.set_phase_status(target_node, admit_status)
            state.append_event("admit_forced", {
                "node": target_node,
                "status": admit_status,
                "manual_override": True,
            })
            state.save()
        flow = load_flow(state.repo_root / state.flow_dot)
        payload = _advance_with_envelope(
            flow, state, force=args.force, target=args.target, node=node,
            single_step=getattr(args, "single_step", False),
        )
        if admit:
            payload["admitted"] = True
            payload["admitted_node"] = target_node
            payload["admitted_status"] = admit_status
    return _emit(Result.ok(payload=payload))


def cmd_decide(args: argparse.Namespace) -> int:
    """Preview the next transition without committing it. See Issue #45f.

    Calls :func:`flowstate.traversal.decide_next_action` and emits the
    Decision as a structured payload. Useful for dry-running ("what
    would advance do right now?") and for diagnostic introspection at
    fan-outs without triggering the side effects in advance.

    Gates ARE executed by decide (they're validation work that informs
    the decision); transition scripts are NOT — those run only at commit
    time inside advance.
    """
    run_dir = Path(args.run_dir).resolve()
    state = RunState.load(run_dir)
    node = getattr(args, "node", None)
    # Multi-cursor guard: when multiple cursors are active and no --node is
    # given the caller cannot know which branch to decide. Fail early with
    # a clear message rather than letting state.current_phase raise an
    # opaque ValueError inside the traversal layer. Mirrors the cmd_advance
    # guard for consistency. See Task 15 / Task 24.
    if node is None and len(state.current_phases) > 1:
        return _emit(Result.error(
            intent="Decide next action",
            failure=(
                f"multiple active phases {sorted(state.current_phases)!r}; "
                f"pass --node to choose one"
            ),
            suggestion=(
                "Pass --node <phase> where <phase> is one of the active cursors "
                f"{sorted(state.current_phases)!r}."
            ),
        ))
    flow = load_flow(state.repo_root / state.flow_dot)
    decision = decide_next_action(
        flow, state, force=args.force, target=args.target, node=node,
    )
    payload: dict[str, object] = {"kind": decision.kind}
    if decision.target:
        payload["target"] = decision.target
    if decision.reason:
        payload["reason"] = decision.reason
    if decision.options is not None:
        payload["options"] = decision.options
        # Scope to the decided node when given — sole_cursor() raises on a
        # multi-cursor run. Matches _advance_with_envelope. Spec D8 row 3.
        payload["current"] = node if node is not None else state.sole_cursor()
    if decision.auto_resolved:
        payload["auto_resolved"] = True
    return _emit(Result.ok(payload=payload))


def cmd_kill_check(args: argparse.Namespace) -> int:
    """Verify whether a node is safe to kill — its status must be in
    (``done``, ``done_forced``). Returns ``Result.ok`` when safe;
    ``Result.error`` with an actionable suggestion otherwise. Used by the
    graph-orchestrator skill as the precondition gate before calling
    ``agentctl kill``. See Issue #45a — this used to live inside
    agentctl, conflating policy (when to kill) with mechanism (how to
    kill).
    """
    run_dir = Path(args.run_dir).resolve()
    node = args.node
    intent = f"Check whether node {node!r} is safe to kill"
    try:
        state = RunState.load(run_dir)
    except RuntimeError as exc:
        return _emit(Result.error(
            intent=intent,
            failure=str(exc),
            suggestion=(
                "Pass --run-dir pointing at a flowstate run directory containing "
                "graph_run_state.yml. If the run-dir is missing, the worker was "
                "spawned without a flowstate run — either reinitialise or use "
                "`agentctl kill` directly to abort the worker without the "
                "precondition gate."
            ),
        ))
    phase_state = state.phases.get(node)
    status = phase_state.status if phase_state is not None else None
    # `done_bypassed` is intentionally excluded — a bypassed node never had
    # a worker to kill, so the kill-check question doesn't apply. Callers
    # who need to abort a stuck worker regardless of node status should
    # use `agentctl kill` directly (it no longer adjudicates).
    if status in ("done", "done_forced"):
        return _emit(Result.ok(payload={"node": node, "status": status}))
    return _emit(Result.error(
        intent=intent,
        failure=(
            f"node {node!r} status is {status!r}, not 'done' or 'done_forced'."
        ),
        suggestion=(
            f"Run `flowstate validate {node}` first to mark the node done after a "
            f"successful worker. To abort a stuck or unwanted worker without "
            f"validating, call `agentctl kill <agent_id>` directly — it no "
            f"longer adjudicates phase status."
        ),
    ))


def cmd_complete(args: argparse.Namespace) -> int:
    """Mark an `runner=orchestrator` node as `done`.

    The orchestrator calls this after handling the node inline (e.g.,
    AskUserQuestion → set vars). Verifies the phase's runner is `orchestrator`
    and that every variable declared in the node's `sets_variables` schema is
    populated, then marks the phase `done` and appends an event. See Issue #35.
    """
    run_dir = Path(args.run_dir).resolve()
    with state_lock(run_dir):
        state = RunState.load(run_dir)
        flow = load_flow(state.repo_root / state.flow_dot)
        try:
            node = flow.graph.node(args.node)
        except Exception as exc:
            return _emit(Result.error(
                intent=f"Complete inline node {args.node!r}",
                failure=str(exc),
                suggestion="Check that the node name matches the DOT graph.",
            ))
        if node.runner != "orchestrator":
            return _emit(Result.error(
                intent=f"Complete inline node {args.node!r}",
                failure=(
                    f"node {args.node!r} has runner={node.runner!r}; "
                    f"`flowstate complete` only applies to runner=orchestrator nodes"
                ),
                suggestion="For agent nodes use `flowstate validate`; script nodes complete inline during `advance`.",
            ))
        # State-machine gates: refuse to mark a non-current node done, and
        # refuse to mark a node done unless it's actually in_progress. Without
        # these, `set_phase_status` would `setdefault` an unseen node into
        # `done`, letting the orchestrator walk past it without running the
        # work. See Issue #43.
        if args.node not in state.current_phases:
            return _emit(Result.error(
                intent=f"complete {args.node!r}",
                failure=(
                    f"node {args.node!r} is not an active cursor "
                    f"(active: {sorted(state.current_phases)!r})"
                ),
                suggestion="Advance to the node before completing it.",
            ))
        existing = state.phases.get(args.node)
        if existing is None or existing.status != "in_progress":
            actual = existing.status if existing else "missing"
            return _emit(Result.error(
                intent=f"Complete inline node {args.node!r}",
                failure=(
                    f"node {args.node!r} has phase status {actual!r}; "
                    f"`flowstate complete` requires status='in_progress'"
                ),
                suggestion="Run `flowstate advance` to enter the node before completing it.",
            ))
        schema = flow.schemas[node.output_schema]
        # "Required" means the orchestrator must have EXPLICITLY set the
        # variable (key present, value not None). Empty string is a valid
        # value — e.g. `modification_feedback=""` is the canonical way the
        # human_approval node says "approved with no modification notes".
        # The earlier check `not state.variables.get(var)` rejected empty
        # strings (and 0, False, []) too, which broke that contract.
        # Read required variables from the executing branch's scope — the
        # orchestrator sets them via `set-var --node <this node>`, which
        # routes into the branch scope for a fork-arm cursor. Spec D8 row 2.
        # Layered (read-only) view so a required var set on trunk after the
        # fork also satisfies the check for a fork-arm node.
        vars_view = _eval_vars_for_node(state, args.node)
        missing = [
            var_name for var_name in schema.required_variables
            if var_name not in vars_view or vars_view.get(var_name) is None
        ]
        if missing:
            return _emit(Result.error(
                intent=f"Complete inline node {args.node!r}",
                failure=(
                    f"node {args.node!r} declares required_variables "
                    f"{sorted(schema.required_variables)} but the following are unset: "
                    f"{missing}"
                ),
                suggestion=(
                    "Set each required variable via `flowstate set-var` before "
                    "calling `complete` (empty string is a valid value)."
                ),
            ))
        state.set_phase_status(args.node, "done")
        state.append_event("node_completed_inline", {"node": args.node})
        state.save()
        # Idle exit (spec §4.4): if this child run is a branch of a parent
        # dynamic_fanout and the parent's BranchRef was flipped to ``idle``
        # by ``_apply_decision`` on advance into this orchestrator+pauses
        # node, flip it back to ``in_progress`` now that the inline work
        # has finished. ``_exit_idle`` is idempotent: no-op when the
        # BranchRef is not currently idle (e.g. the predicate didn't fire
        # on advance, or this is a top-level run).
        if (
            state.parent_run is not None
            and state.parent_run.fanout_node
            and state.parent_run.branch_id
        ):
            _exit_idle(
                Path(state.parent_run.run_dir),
                fanout_node=state.parent_run.fanout_node,
                branch_id=state.parent_run.branch_id,
            )
        return _emit(Result.ok(payload={"node": args.node, "status": "done"}))


def cmd_start_branch(args: argparse.Namespace) -> int:
    """Dispatch one branch of a dynamic_fanout: create the child subflow run.

    The orchestrator's loop after a dynamic_fanout `advance` is:

        for branch_id in payload["next_startable_branches"]:
            flowstate start-branch --run-dir <parent> --node <fanout> \
                --branch <branch_id>

    This subcommand reads everything else from parent state + the flow
    definition, so the orchestrator skill stays graph-agnostic:

    - The fanout node's `template=` attr → the subflow template node.
    - The subflow template's `flow=` attr → the child flow name, resolved on
      disk at `<repo_root>/factory/flows/<flow>/<flow>.dot`.
    - The pre-seeded branch scope (populated by `_apply_dynamic_fanout`) →
      the child run's seed variables. When the subflow template declares
      `inputs="{child_var: parent_var, ...}"`, the mapping is applied;
      otherwise the full branch scope is passed through (after stripping
      system vars `_userid`, `_timestamp`, `_run_descriptor`,
      `_run_artefact_dir`, `_supervision`, `_supervision_instructions`,
      `_flow_stem` — RunState.create stamps fresh ones for the child).

    Idempotency: refuses to re-dispatch a branch whose BranchRef is already
    `in_progress`/`done`/`error`. The orchestrator should treat that as a
    no-op (the branch was started on a previous tick).
    """
    from flowstate.subflow import init_subflow_run

    parent_run_dir = Path(args.run_dir).resolve()
    fanout_name = args.node
    branch_id = args.branch
    intent = (
        f"Start subflow branch {branch_id!r} on fanout {fanout_name!r} "
        f"in parent run at {parent_run_dir!s}"
    )

    if not (parent_run_dir / "graph_run_state.yml").exists():
        return _emit(Result.error(
            intent=intent,
            failure=f"no graph_run_state.yml under {parent_run_dir!s}",
            suggestion="Pass --run-dir pointing at an initialised parent run directory.",
        ))

    # All parent-state reads happen inside this lock to avoid a stale-read
    # window vs. a concurrent `flowstate advance --node <fanout>` that may be
    # mutating phases[fanout].branches or branch_scopes. The lock is released
    # before calling init_subflow_run because init_subflow_run takes its own
    # state_lock on the parent for the BranchRef stamp at the end (lock
    # ordering invariant: we never hold the parent's lock while crossing into
    # a function that re-acquires it).
    with state_lock(parent_run_dir):
        parent = RunState.load(parent_run_dir)
        parent_flow = load_flow(parent.repo_root / parent.flow_dot)

        try:
            fanout_node = parent_flow.graph.node(fanout_name)
        except KeyError:
            return _emit(Result.error(
                intent=intent,
                failure=f"node {fanout_name!r} not found in parent flow {parent.flow_name!r}",
                suggestion="Pass --node matching a dynamic_fanout node in the parent flow.",
            ))

        if fanout_node.runner != "dynamic_fanout":
            return _emit(Result.error(
                intent=intent,
                failure=(
                    f"node {fanout_name!r} has runner={fanout_node.runner!r}; "
                    f"start-branch only applies to runner=dynamic_fanout nodes"
                ),
                suggestion="Linear/agent/orchestrator nodes advance via `flowstate advance`, not start-branch.",
            ))

        template_name = fanout_node.template_node
        if not template_name:
            return _emit(Result.error(
                intent=intent,
                failure=f"fanout {fanout_name!r} has no `template=` attribute",
                suggestion="Add `template=<subflow_node>` to the fanout's DOT attributes.",
            ))

        try:
            subflow_node = parent_flow.graph.node(template_name)
        except KeyError:
            return _emit(Result.error(
                intent=intent,
                failure=(
                    f"fanout {fanout_name!r} names template {template_name!r} "
                    f"which is not defined in the parent flow"
                ),
                suggestion="Fix the parent flow's DOT so the template node is declared.",
            ))

        if subflow_node.runner != "subflow":
            return _emit(Result.error(
                intent=intent,
                failure=(
                    f"template node {template_name!r} has runner={subflow_node.runner!r}; "
                    f"a dynamic_fanout's template must be runner=subflow"
                ),
                suggestion="Change the template node's runner to `subflow`.",
            ))

        child_flow_name = subflow_node.subflow_flow
        if not child_flow_name:
            return _emit(Result.error(
                intent=intent,
                failure=f"subflow template {template_name!r} has no `flow=` attribute",
                suggestion="Add `flow=<child_flow_name>` to the subflow template node.",
            ))

        fanout_phase = parent.phases.get(fanout_name)
        branch_ref = None
        if fanout_phase is not None:
            branch_ref = next(
                (b for b in fanout_phase.branches if b.branch_id == branch_id), None,
            )
        if branch_ref is None:
            return _emit(Result.error(
                intent=intent,
                failure=(
                    f"branch {branch_id!r} not registered on fanout {fanout_name!r}; "
                    f"known branches: "
                    f"{[b.branch_id for b in (fanout_phase.branches if fanout_phase else [])]}"
                ),
                suggestion=(
                    "Call `flowstate advance --node <fanout>` first so the fanout "
                    "stamps its branches, then iterate the returned "
                    "`next_startable_branches` list."
                ),
            ))

        if branch_ref.status != "pending":
            return _emit(Result.ok(payload={
                "branch_id": branch_id,
                "subflow_run_dir": branch_ref.subflow_run_dir,
                "child_flow": child_flow_name,
                "already_started": True,
                "status": branch_ref.status,
            }))

        child_flow_dir = parent.repo_root / "factory" / "flows" / child_flow_name
        child_flow_dot_path = child_flow_dir / f"{child_flow_name}.dot"
        if not child_flow_dot_path.exists():
            return _emit(Result.error(
                intent=intent,
                failure=(
                    f"child flow {child_flow_name!r} not found at "
                    f"{child_flow_dot_path!s}"
                ),
                suggestion=(
                    "Confirm the child flow's DOT lives at "
                    "factory/flows/<flow>/<flow>.dot and the subflow template's "
                    "`flow=` attribute matches that directory name."
                ),
            ))

        scope = parent.branch_scopes.get(branch_id)
        if scope is None:
            return _emit(Result.error(
                intent=intent,
                failure=(
                    f"branch {branch_id!r} has no BranchScope in parent state; "
                    f"_apply_dynamic_fanout should have seeded one"
                ),
                suggestion="Re-run `flowstate advance --node <fanout>` to re-stamp branch scopes.",
            ))

        _SYSTEM_VARS = {
            "_userid", "_timestamp", "_run_descriptor", "_run_artefact_dir",
            "_supervision", "_supervision_instructions", "_flow_stem",
        }
        if subflow_node.subflow_inputs:
            inputs: dict[str, object] = {}
            for child_var, parent_expr in subflow_node.subflow_inputs.items():
                if parent_expr not in scope.variables:
                    return _emit(Result.error(
                        intent=intent,
                        failure=(
                            f"subflow template {template_name!r} maps child var "
                            f"{child_var!r} from parent expression {parent_expr!r}, "
                            f"which is not present in branch {branch_id!r}'s scope"
                        ),
                        suggestion=(
                            "Either declare the variable upstream of the fanout, "
                            "or fix the `inputs=` mapping on the subflow template."
                        ),
                    ))
                inputs[child_var] = scope.variables[parent_expr]
        else:
            # No explicit `inputs=` mapping. Per the batch-factory-pipeline
            # design (spec 2026-06-05 §"Subflow inputs"), each fanned-out item
            # is a dict whose keys/values become the child's initial variables
            # directly. Spread the item dict — do NOT pass the whole branch
            # scope through, which would leak the parent's own variables
            # (e.g. a batch-level `freeform_input`) into the child. A non-dict
            # item (scalar-list fanout) keeps the generic `item` binding.
            item = scope.variables.get("item")
            if isinstance(item, dict):
                inputs = {k: v for k, v in item.items() if k not in _SYSTEM_VARS}
            else:
                inputs = {"item": item}
        userid_for_child = parent.userid

    # Lock released. init_subflow_run re-locks for its own BranchRef stamp.
    try:
        child_run_dir = init_subflow_run(
            parent_run_dir=parent_run_dir,
            child_flow_name=child_flow_name,
            child_flow_dir=child_flow_dir,
            child_flow_dot_path=child_flow_dot_path,
            inputs=inputs,
            branch_id=branch_id,
            fanout_node=fanout_name,
            userid=userid_for_child,
        )
    except (OSError, RuntimeError, ValueError) as exc:
        return _emit(Result.error(
            intent=intent,
            failure=f"init_subflow_run failed: {exc}",
            suggestion=(
                "Inspect the parent's BranchScope and the child flow's DOT for "
                "the root cause. A pre-existing child run directory (RuntimeError) "
                "means a prior start-branch call already created it; check "
                "`flowstate status` for the existing subflow_run_dir before retrying."
            ),
        ))

    return _emit(Result.ok(payload={
        "branch_id": branch_id,
        "subflow_run_dir": str(child_run_dir),
        "child_flow": child_flow_name,
        "already_started": False,
    }))


def cmd_complete_subflow(args: argparse.Namespace) -> int:
    """Push-driven merge-in: harvest a completed child subflow's outputs into
    its parent and flip the parent's BranchRef to done. Runtime hook — the
    orchestrator does NOT call this directly. T12 wires the runtime to invoke
    this automatically when a child reaches terminal state; T11 exposes it as
    a CLI for testability and operator recovery scenarios. See `subflow.py
    :: complete_subflow`.
    """
    from flowstate.subflow import complete_subflow
    parent_run_dir = args.parent_run_dir
    branch_id = args.branch
    intent = f"Complete subflow branch {branch_id!r} on parent run at {parent_run_dir!s}"
    try:
        complete_subflow(parent_run_dir, branch_id)
    except (ValueError, FileNotFoundError) as exc:
        return _emit(Result.error(
            intent=intent,
            failure=str(exc),
            suggestion=(
                "Ensure the child has reached state='completed' and that "
                "--branch matches a BranchRef registered on the parent's "
                "subflow node (see `flowstate status` for current branches)."
            ),
        ))
    return _emit(Result.ok(payload={"branch": branch_id}))


def cmd_validate(args: argparse.Namespace) -> int:
    # Serialise load+mutate+save against concurrent CLI invocations. See Issue #27.
    run_dir = Path(args.run_dir).resolve()
    with state_lock(run_dir):
        return _cmd_validate_locked(args, run_dir)


def _cmd_validate_locked(args: argparse.Namespace, run_dir: Path) -> int:
    state = RunState.load(run_dir)
    flow = load_flow(state.repo_root / state.flow_dot)
    record = _agent_record(getattr(args, "agent_id", None),
                           getattr(args, "registry_dir", None))
    return _emit(_validate_node_locked(
        flow, state, args.node, record=record,
        registry_dir=getattr(args, "registry_dir", None)))


def _validate_node_locked(flow, state: RunState, node_name: str,
                          record: dict | None = None,
                          registry_dir=None) -> Result:
    """Validate ``node_name`` and return the ``Result`` envelope (does not emit).

    Shared by the standalone ``validate`` command and ``finish`` so the two can
    never drift — same session-id / output-schema checks, same phase-done
    marking, same usage bookkeeping and events. Guard failures (wrong runner,
    not an active cursor, wrong status) return ``Result.error``; a completed
    validation returns ``Result.ok`` with ``{passed, feedback,
    agent_session_id}`` (``passed`` may be ``False`` — a failed validation is
    still a successful observation). Mutates and saves ``state``.

    Caller must hold the run_dir state_lock for the duration of this call.
    """
    node = flow.graph.node(node_name)
    # State-machine gates: validate only applies to runner=agent nodes that
    # are currently in_progress. Without these, a misdirected validate call
    # mutates the wrong phase's state and corrupts the audit trail. See
    # Issue #43.
    if node.runner != "agent":
        return Result.error(
            intent=f"Validate node {node_name!r}",
            failure=(
                f"node {node_name!r} has runner={node.runner!r}; "
                f"`flowstate validate` only applies to runner=agent nodes"
            ),
            suggestion=(
                "For orchestrator nodes use `flowstate complete`; "
                "script nodes complete inline during `advance`."
            ),
        )
    if node_name not in state.current_phases:
        return Result.error(
            intent=f"Validate node {node_name!r}",
            failure=(
                f"node {node_name!r} is not an active cursor "
                f"(active: {sorted(state.current_phases)!r})"
            ),
            suggestion="Advance to this node first, or pass the correct --node.",
        )
    existing = state.phases.get(node_name)
    if existing is None or existing.status != "in_progress":
        actual = existing.status if existing else "missing"
        return Result.error(
            intent=f"Validate node {node_name!r}",
            failure=(
                f"node {node_name!r} has phase status {actual!r}; "
                f"`flowstate validate` requires status='in_progress'"
            ),
            suggestion="Run `flowstate advance` to enter the node before validating it.",
        )
    schema = flow.schemas[node.output_schema]

    # Resolve output paths against the EXECUTING branch's scope — a fork-arm
    # node's path templates may reference branch-local variables. Non-branch
    # cursors fall through to state.variables inside _vars_for_node.
    # Spec D8 row 2.
    # Deliberately the RAW live scope, NOT the layered _eval_vars_for_node
    # view: output paths were populated into this same scope at node entry
    # (_apply_decision), and vars_view is also the WRITE target for the
    # sets_variables harvest (validate_phase). Resolving against the same raw
    # scope keeps entry-time and validate-time path resolution identical.
    vars_view = _vars_for_node(state, node_name)
    output_paths: dict[str, Path] = {}
    for sf in schema.files:
        output_paths[sf.name] = Path(resolve_path_template(sf.path, vars_view, state.repo_root))

    # Completion.yml lives in the agent's temp dir. The path convention is shared
    # with agentctl via `flowstate.temp_layout` — both modules MUST go through
    # this helper to stay in sync. See Issue #30.
    from flowstate.temp_layout import completion_path as _completion_path
    completion_path = _completion_path(
        state.repo_root,
        state.flow_name,
        state.run_descriptor,
        state.userid,
        state.timestamp,
        node_name,
    )

    outcome = validate_phase(
        state=state,
        phase_name=node_name,
        schema=schema,
        definitions=flow.definitions,
        output_paths=output_paths,
        completion_path=completion_path,
        vars_target=vars_view,
        # 2026-08-10: identity from the spawn record; the worker's echo is a
        # cross-check. Both None on the legacy path (no --agent-id), which
        # keeps pre-existing behaviour intact.
        authoritative_sid=((record or {}).get("agent_session_id")
                           or (record or {}).get("claude_session_id")),
        expected_echo=((record or {}).get("spawn_token")
                       or (record or {}).get("agent_session_id")
                       or (record or {}).get("claude_session_id")),
    )
    if outcome.passed:
        state.append_event("validation_passed", {
            "node": node_name, "agent_session_id": outcome.agent_session_id,
        })
    else:
        state.append_event("validation_failed", {
            "node": node_name, "feedback": outcome.feedback,
        })

    # Opportunistically populate token usage from the worker's transcript.
    # Never block validation on transcript parsing — the transcript may be
    # missing (transcripts disabled, race during kill) or unreadable; treat
    # those as zero-usage rather than failing validate.
    #
    # CRITICAL: guard against zeroing out previously-recorded usage when the
    # transcript isn't yet flushed (parse_session_usage returns a zero-filled
    # struct in that case). Only overwrite if the parsed usage has signal —
    # `turns > 0` is the cleanest presence check. See Issue #28.
    phase = state.phases.get(node_name)
    if phase is not None and (record or {}).get("harness"):
        phase.harness = record["harness"]
    if phase is not None and phase.agent_session_id:
        usage = _harness_session_usage(
            phase.harness, phase.agent_session_id, registry_dir=registry_dir)
        if usage is not None and usage.turns > 0:
            phase.usage = usage

    # Refresh orchestrator usage too — validate is a natural sync point and
    # keeps the run total live without a separate CLI call. See Issue #17.
    state.refresh_orchestrator_usage()

    state.save()
    return Result.ok(payload={
        "passed": outcome.passed,
        "feedback": outcome.feedback,
        "agent_session_id": outcome.agent_session_id,
    })


def cmd_finish(args: argparse.Namespace) -> int:
    """Collapse per-worker teardown — validate + kill + advance — into one call.

    Replaces the orchestrator's three-call sequence (`flowstate validate`,
    `flowstate kill-check` + `agentctl kill`, `flowstate advance`) with a single
    invocation. Reuses the standalone commands' internals verbatim
    (`_validate_node_locked`, `_advance_with_envelope`, the agentctl harness'
    `kill`) so the collapsed path can never drift from the individual commands.

    Envelope payload: ``{node, validate: {passed, feedback, agent_session_id},
    killed: true|false|null, kill_reason: <str, only when killed is false>,
    advance: {...}|null}``.

    - Validation FAILS → ``killed`` and ``advance`` are null; the worker is left
      in place so it can be resumed to fix its output.
    - Kill runs only when validation passed AND ``--agent-id`` is given; a kill
      that throws (already-dead / unknown id) records ``killed: false`` with a
      reason and still advances — a dead worker must not block graph progress.
    """
    run_dir = Path(args.run_dir).resolve()
    node_name = args.node
    with state_lock(run_dir):
        state = RunState.load(run_dir)
        flow = load_flow(state.repo_root / state.flow_dot)
        _record = _agent_record(getattr(args, "agent_id", None),
                                getattr(args, "registry_dir", None))
        validate_result = _validate_node_locked(
            flow, state, node_name, record=_record,
            registry_dir=getattr(args, "registry_dir", None))
        # Structural guard failure (wrong runner / not active / wrong status):
        # surface it verbatim — there is nothing to kill or advance.
        if validate_result.status == "error":
            return _emit(validate_result)
        validate_payload = dict(validate_result.payload or {})
        if not validate_payload.get("passed"):
            # Validation observed a failure: leave the worker in place (it may
            # be resumed to fix its output). Do NOT kill or advance.
            return _emit(Result.ok(payload={
                "node": node_name,
                "validate": validate_payload,
                "killed": None,
                "advance": None,
            }))

        # Validation passed → the node is now `done`. Kill the worker (if named),
        # then advance from the node.
        killed: bool | None = None
        kill_reason: str | None = None
        if args.agent_id:
            # Mirror the kill-check node-status guard. A passed validation marks
            # the node done, so this normally holds; kept for fidelity with the
            # standalone kill-check gate.
            phase_state = state.phases.get(node_name)
            status = phase_state.status if phase_state is not None else None
            if status not in ("done", "done_forced"):
                killed = False
                kill_reason = (
                    f"node {node_name!r} status is {status!r}, not 'done'/'done_forced'; "
                    f"skipped kill"
                )
            else:
                # Kill through the harness that SPAWNED the worker — the
                # record carries it. Hardcoding ClaudeCodeHarness worked only
                # because kill is largely tmux-generic; it is the same
                # claude-only assumption removed everywhere else here.
                from agentctl.harnesses.factory import make_harness
                registry_dir = (
                    Path(args.registry_dir) if args.registry_dir
                    else Path.home() / ".agentctl"
                )
                _rec = _agent_record(args.agent_id, registry_dir) or {}
                try:
                    make_harness(_rec.get("harness", "claude-code"),
                                 registry_dir).kill(args.agent_id)
                    killed = True
                except Exception as exc:
                    # Already-dead / unknown-id kills must not block progress —
                    # record the reason and fall through to advance.
                    killed = False
                    kill_reason = f"{type(exc).__name__}: {exc}"

        advance_payload = _advance_with_envelope(flow, state, node=node_name)

    payload: dict[str, object] = {
        "node": node_name,
        "validate": validate_payload,
        "killed": killed,
        "advance": advance_payload,
    }
    if killed is False:
        payload["kill_reason"] = kill_reason
    return _emit(Result.ok(payload=payload))


def cmd_event(args: argparse.Namespace) -> int:
    """Append a structured event to `state.events`. Used by the orchestrator
    to record decisions it makes inline that aren't otherwise captured by
    `flowstate validate` / `flowstate advance` — most importantly Bucket A.5
    amendments (targeted Edits the orchestrator applies after a
    `validation_failed` to avoid a worker respawn). See Issue #16.
    """
    import json as _json
    run_dir = Path(args.run_dir).resolve()
    # `t` and `kind` are canonical event fields; allowing them as payload keys
    # destroys the audit timestamp on save (the `**payload` spread overrides
    # the canonical values). Reject at the CLI boundary. See Issue #40.
    RESERVED_EVENT_KEYS = ("t", "kind")
    payload: dict[str, object] = {}
    if args.payload_json:
        try:
            parsed = _json.loads(args.payload_json)
        except _json.JSONDecodeError as exc:
            return _emit(Result.error(
                intent=f"Append event {args.kind!r} to run at {run_dir!s}",
                failure=f"--payload-json is not valid JSON: {exc}",
                suggestion=(
                    "Pass a JSON object literal, e.g. "
                    "--payload-json '{\"removed_field\": \"notes\"}'."
                ),
            ))
        if not isinstance(parsed, dict):
            return _emit(Result.error(
                intent=f"Append event {args.kind!r} to run at {run_dir!s}",
                failure=f"--payload-json must be a JSON object; got {type(parsed).__name__}.",
                suggestion="Wrap your value in an object, e.g. '{\"value\": ...}'.",
            ))
        reserved_hit = [k for k in RESERVED_EVENT_KEYS if k in parsed]
        if reserved_hit:
            return _emit(Result.error(
                intent=f"Append event {args.kind!r} to run at {run_dir!s}",
                failure=(
                    f"--payload-json contains reserved key(s) {reserved_hit!r}; "
                    f"{list(RESERVED_EVENT_KEYS)!r} are canonical event fields "
                    f"and cannot be overridden via payload."
                ),
                suggestion="Rename the offending keys (e.g. `t` → `time`, `kind` → `category`).",
            ))
        payload.update(parsed)
    for kv in args.data or []:
        if "=" not in kv:
            return _emit(Result.error(
                intent=f"Append event {args.kind!r} to run at {run_dir!s}",
                failure=f"-d/--data value {kv!r} is not in key=value form.",
                suggestion="Pass each extra field as -d key=value (repeatable).",
            ))
        key, value = kv.split("=", 1)
        if not key:
            return _emit(Result.error(
                intent=f"Append event {args.kind!r} to run at {run_dir!s}",
                failure=f"-d/--data value {kv!r} has an empty key.",
                suggestion="Pass each extra field as -d key=value (repeatable).",
            ))
        if key in RESERVED_EVENT_KEYS:
            return _emit(Result.error(
                intent=f"Append event {args.kind!r} to run at {run_dir!s}",
                failure=(
                    f"-d/--data uses reserved key {key!r}; "
                    f"{list(RESERVED_EVENT_KEYS)!r} are canonical event fields "
                    f"and cannot be overridden via payload."
                ),
                suggestion=f"Rename it (e.g. `{key}` → `event_{key}`).",
            ))
        payload[key] = value
    if args.node:
        payload["node"] = args.node
    if args.message:
        payload["message"] = args.message
    with state_lock(run_dir):
        state = RunState.load(run_dir)
        state.append_event(args.kind, payload)
        # `append_event` stamped `t` with the canonical `iso_now()` — grab the
        # event we just appended for the echo so callers can round-trip on it.
        appended = state.events[-1]
        state.save()
    return _emit(Result.ok(payload={
        "event": {"t": appended.t, "kind": appended.kind, "payload": dict(appended.payload)},
    }))


def cmd_ancestors(args: argparse.Namespace) -> int:
    from flowstate.traceback import ancestors
    run_dir = Path(args.run_dir).resolve()
    intent = f"Walk ancestor chain for run at {run_dir!s}"
    try:
        chain = ancestors(run_dir)
    except RuntimeError as exc:
        return _emit(Result.error(
            intent=intent,
            failure=str(exc),
            suggestion=(
                "Pass --run-dir pointing at a valid flowstate run directory "
                "containing graph_run_state.yml."
            ),
        ))
    return _emit(Result.ok(payload={"runs": chain}))


def cmd_descendants(args: argparse.Namespace) -> int:
    from flowstate.traceback import descendants
    run_dir = Path(args.run_dir).resolve()
    intent = f"Walk descendant subflows for run at {run_dir!s}"
    try:
        out = descendants(run_dir)
    except RuntimeError as exc:
        return _emit(Result.error(
            intent=intent,
            failure=str(exc),
            suggestion=(
                "Pass --run-dir pointing at a valid flowstate run directory "
                "containing graph_run_state.yml."
            ),
        ))
    return _emit(Result.ok(payload={"runs": out}))


def cmd_merge(args: argparse.Namespace) -> int:
    try:
        merge_branch(
            integration_worktree=Path(args.integration_worktree),
            source_branch=args.source_branch,
            message=args.message,
        )
    except VcsError as exc:
        return _emit(Result.error(
            intent=f"Merge {args.source_branch!r} into worktree at {args.integration_worktree!r}",
            failure=str(exc),
            suggestion=(
                "Resolve conflicts manually or check that the integration worktree exists. "
                "You can run: git -C <integration-worktree> merge --no-ff <source-branch>"
            ),
        ))
    return _emit(Result.ok(payload={"merged": args.source_branch, "into": args.integration_worktree}))


def cmd_remove_worktree(args: argparse.Namespace) -> int:
    try:
        remove_worktree(repo=Path(args.repo), path=Path(args.path), force=args.force)
    except VcsError as exc:
        return _emit(Result.error(
            intent=f"Remove worktree at {args.path!r}",
            failure=str(exc),
            suggestion="Check for uncommitted changes, or pass --force to remove anyway.",
        ))
    return _emit(Result.ok(payload={"removed": args.path}))


def main(argv: list[str]) -> int:
    p = argparse.ArgumentParser(prog="flowstate")
    sub = p.add_subparsers(dest="cmd", required=True)

    s = sub.add_parser("init")
    s.add_argument("flow_dot")
    s.add_argument("--run-dir", required=True)
    s.add_argument("--run-descriptor", required=True)
    s.add_argument("--supervision", default="medium", choices=list(VALID_SUPERVISION_LEVELS))
    s.add_argument(
        "--orchestrator-agent-id", default=None,
        help=(
            "agentctl agent id of the orchestrator (from its own spawn). "
            "When given, the orchestrator's session id and harness are read "
            "from the spawn record and --orchestrator-session-id becomes a "
            "cross-check. The graph-orchestrator skill's launcher mode always "
            "spawns via agentctl, so this is the normal path."),
    )
    s.add_argument("--orchestrator-registry-dir", default=None)
    s.add_argument(
        "--orchestrator-session-id",
        default=None,
        help=(
            "Session id the orchestrator reports for itself. Cross-checked "
            "against the spawn record when --orchestrator-agent-id is given; "
            "the sole source only on the legacy no-record path. Recorded "
            "in metadata for audit. Skill-driven runs must always pass this; "
            "manual CLI use can omit it."
        ),
    )

    s = sub.add_parser(
        "bootstrap",
        help=(
            "One-shot startup: reads factory-prefs, runs init, runs first "
            "advance, returns a comprehensive envelope (prefs + resolved + "
            "init + summary + advance). See Issue #37."
        ),
    )
    s.add_argument(
        "--run-descriptor", required=True,
        help="Path-safe identifier for this run (e.g., a Jira ticket slug).",
    )
    s.add_argument(
        "--orchestrator-session-id", default=None,
        help="Session id the orchestrator reports for itself; cross-checked "
             "against the spawn record when --orchestrator-agent-id is given.",
    )
    s.add_argument(
        "--orchestrator-agent-id", default=None,
        help="agentctl agent id of the orchestrator (from its own spawn). "
             "Source of truth for its session id and harness.",
    )
    s.add_argument("--orchestrator-registry-dir", default=None)
    s.add_argument(
        "--flow-dot", default=None,
        help="Override the prefs `default_graph`. Path to a .dot file.",
    )
    s.add_argument(
        "--supervision", default=None, choices=list(VALID_SUPERVISION_LEVELS),
        help="Override the prefs `supervision` value.",
    )
    s.add_argument(
        "--prefs-path", default=None,
        help="Path to the runtime prefs file. Default: factory/factory-prefs.yml.",
    )
    s.add_argument(
        "--prefs-example", default=None,
        help="Path to the prefs example file (copied to --prefs-path on first run). Default: factory/factory-prefs-example.yml.",
    )
    s.add_argument(
        "--seed-var", action="append", default=[], metavar="NAME=VALUE",
        help=(
            "Seed a string variable before the first advance. Repeatable. "
            "Use for user-supplied inputs like freeform_input that the first "
            "agent node's prompt template references. Without this flag the "
            "first advance fails to render any template referencing the var."
        ),
    )
    s.add_argument(
        "--seed-var-json", action="append", default=[], metavar="NAME=JSON",
        help=(
            "Like --seed-var but parses VALUE as JSON before storing. Use "
            "for list/dict variables (e.g. "
            "--seed-var-json tickets='[{\"jira_ref\":\"DSCO-1\"}]'). "
            "Repeatable; combines with --seed-var."
        ),
    )

    s = sub.add_parser("status")
    s.add_argument("--run-dir", required=True)
    s.add_argument(
        "--node", default=None,
        help=(
            "Filter status to a specific node. Required when multiple cursors are "
            "active and you want a single-node view. Omit to see all active phases."
        ),
    )

    s = sub.add_parser("summary")
    s.add_argument("--run-dir", required=True)

    s = sub.add_parser("next")
    s.add_argument("--run-dir", required=True)

    s = sub.add_parser("vars")
    s.add_argument("name", nargs="?", default=None)
    s.add_argument("--run-dir", required=True)
    s.add_argument(
        "--format",
        choices=["yaml", "json"],
        default="yaml",
        help="Output format. JSON is immune to YAML escape / folding "
             "artifacts and recommended for shell piping. See Issue #12.",
    )

    s = sub.add_parser("set-var")
    s.add_argument("name")
    s.add_argument("value")
    s.add_argument("--run-dir", required=True)
    s.add_argument(
        "--node",
        help=(
            "Node whose scope to write into. When it names an active fork "
            "branch cursor, the variable is stored in that branch's scope "
            "(so the branch's own edges + the merge-at-join see it); "
            "otherwise the write is run-level. Safe to always pass."
        ),
    )
    s.add_argument(
        "--json",
        action="store_true",
        help=(
            "Parse value as JSON before storing. Use for list/dict variables "
            "(e.g. `--json '[{\"jira_ref\":\"DSCO-1\"}]'` populates a list-typed "
            "variable that downstream nodes like dynamic_fanout can iterate). "
            "Without this flag the value is stored verbatim as a string."
        ),
    )

    s = sub.add_parser("node-config")
    s.add_argument("node")
    s.add_argument("--run-dir", required=True)

    s = sub.add_parser("render-prompt")
    s.add_argument("node")
    s.add_argument("--run-dir", required=True)

    s = sub.add_parser("advance")
    s.add_argument("--run-dir", required=True)
    s.add_argument("--force", action="store_true")
    s.add_argument("--target", default=None)
    s.add_argument(
        "--node", default=None,
        help=(
            "Advance a specific node's cursor. Required when multiple cursors "
            "are active (e.g. after a fork fan-out). Omit for single-cursor runs."
        ),
    )
    s.add_argument(
        "--admit", action="store_true",
        help=(
            "Escape hatch: force-mark the targeted node as done (bypassing "
            "validation) so the orchestrator can move past a stuck/failed branch. "
            "Works on any node status, including in_progress. The multi-cursor "
            "--node guard still applies. Appends an auditable admit_forced event. "
            "See Task 16."
        ),
    )
    s.add_argument(
        "--status",
        choices=["done", "done_forced"],
        default="done_forced",
        help=(
            "Status to set when --admit is given. Defaults to done_forced. "
            "Use done when you want the node treated as cleanly completed."
        ),
    )
    s.add_argument(
        "--single-step", dest="single_step", action="store_true",
        help=(
            "Take exactly one hop (legacy behaviour). By default advance "
            "auto-chases through purely programmatic nodes (scripts, forks, "
            "ready joins, auto-resolvable conditional edges) and stops at the "
            "first node needing the orchestrator. Use this to debug a single "
            "transition."
        ),
    )

    s = sub.add_parser(
        "decide",
        help=(
            "Preview the next transition without committing it. Runs "
            "gates (validation work) but not transition scripts. See "
            "Issue #45f."
        ),
    )
    s.add_argument("--run-dir", required=True)
    s.add_argument("--force", action="store_true")
    s.add_argument("--target", default=None)
    s.add_argument(
        "--node", default=None,
        help=(
            "Decide for a specific node's cursor. Required when multiple cursors "
            "are active (e.g. after a fork fan-out). Omit for single-cursor runs."
        ),
    )

    s = sub.add_parser(
        "ancestors",
        help=(
            "Walk parent_run pointers upward from a subflow run and return "
            "the chain of run dirs from the given run to the top-level root."
        ),
    )
    s.add_argument("--run-dir", required=True)

    s = sub.add_parser(
        "descendants",
        help=(
            "Walk BranchRef.subflow_run_dir downward (depth-first) from a run "
            "and return the flat list of all reachable subflow run dirs."
        ),
    )
    s.add_argument("--run-dir", required=True)

    s = sub.add_parser(
        "start-branch",
        help=(
            "Dispatch one branch of a dynamic_fanout: create the child "
            "subflow run, seed it from the parent's branch scope, and "
            "flip the parent's BranchRef to in_progress. Idempotent — "
            "re-calling on an already-started branch returns the existing "
            "subflow_run_dir."
        ),
    )
    s.add_argument("--run-dir", required=True, help="parent run directory")
    s.add_argument(
        "--node", required=True,
        help="dynamic_fanout node name in the parent flow (e.g. 'batch_fanout')",
    )
    s.add_argument(
        "--branch", required=True,
        help="branch_id minted by the fanout (e.g. 'B01')",
    )

    s = sub.add_parser(
        "complete-subflow",
        help=(
            "(runtime hook; not intended for direct invocation) "
            "Push a completed child subflow's outputs into its parent and "
            "flip the parent's BranchRef to done. T12 wires the runtime to "
            "call this automatically; exposed as a CLI for testability and "
            "operator recovery."
        ),
    )
    s.add_argument("parent_run_dir", help="absolute path to the parent run dir")
    s.add_argument(
        "--branch", required=True,
        help="branch_id of the completed child (e.g. 'B01')",
    )

    s = sub.add_parser("validate")
    s.add_argument("node")
    s.add_argument("--run-dir", required=True)
    s.add_argument("--agent-id", default=None,
                   help="agentctl agent id for this node's worker. Enables "
                        "record-sourced session identity (instead of trusting "
                        "the worker's self-report) and harness-aware usage.")
    s.add_argument("--registry-dir", default=None)

    s = sub.add_parser(
        "finish",
        help=(
            "One-shot per-worker teardown: validate the node, kill its agent "
            "(if --agent-id given), and advance — collapsing the three-call "
            "validate / kill-check+kill / advance sequence. On validation "
            "failure the worker is left in place (killed + advance null)."
        ),
    )
    s.add_argument("node")
    s.add_argument("--run-dir", required=True)
    s.add_argument(
        "--agent-id", default=None,
        help=(
            "agentctl agent id to kill after a passed validation. Omit to skip "
            "the kill (killed:null). A kill that fails (already-dead / unknown "
            "worker) records killed:false with a reason and still advances."
        ),
    )
    s.add_argument(
        "--registry-dir", default=None,
        help="agentctl registry dir holding <agent_id>.yml records. Default: ~/.agentctl.",
    )

    s = sub.add_parser(
        "kill-check",
        help=(
            "Verify a node's status is 'done' or 'done_forced' before the "
            "orchestrator calls `agentctl kill`. Replaces the precondition "
            "check that used to live inside agentctl. See Issue #45a."
        ),
    )
    s.add_argument("--run-dir", required=True)
    s.add_argument(
        "--node", required=True,
        help="Node whose status to check.",
    )

    s = sub.add_parser(
        "complete",
        help=(
            "Mark a runner=orchestrator node as done after the orchestrator "
            "session handled it inline (e.g., AskUserQuestion + set-var). "
            "See Issue #35."
        ),
    )
    s.add_argument("node")
    s.add_argument("--run-dir", required=True)

    s = sub.add_parser(
        "event",
        help=(
            "Append a structured event to `state.events`. Used by the "
            "orchestrator to record decisions made inline (e.g., Bucket A.5 "
            "amendments). See Issue #16."
        ),
    )
    s.add_argument("--run-dir", required=True)
    s.add_argument(
        "--kind", required=True,
        help="Event kind (e.g., `node_amended_by_orchestrator`).",
    )
    s.add_argument(
        "--node", default=None,
        help="Node name this event refers to. Stored under payload.node.",
    )
    s.add_argument(
        "--message", default=None,
        help="One-line summary of the event. Stored under payload.message.",
    )
    s.add_argument(
        "-d", "--data", action="append", default=[], metavar="KEY=VALUE",
        help="Extra payload field. Repeatable. Values are stored as strings.",
    )
    s.add_argument(
        "--payload-json", default=None,
        help="Bulk JSON-encoded object merged into the event payload (string "
             "and non-string values supported). Combines with -d.",
    )

    s = sub.add_parser("merge")
    s.add_argument("integration_worktree")
    s.add_argument("source_branch")
    s.add_argument("--message", required=True)

    s = sub.add_parser("remove-worktree")
    s.add_argument("path")
    s.add_argument("--repo", required=True)
    s.add_argument("--force", action="store_true")

    args = p.parse_args(argv)
    handlers = {
        "init": cmd_init,
        "bootstrap": cmd_bootstrap,
        "status": cmd_status,
        "summary": cmd_summary,
        "next": cmd_next,
        "vars": cmd_vars,
        "set-var": cmd_set_var,
        "node-config": cmd_node_config,
        "render-prompt": cmd_render_prompt,
        "advance": cmd_advance,
        "decide": cmd_decide,
        "ancestors": cmd_ancestors,
        "descendants": cmd_descendants,
        "start-branch": cmd_start_branch,
        "complete-subflow": cmd_complete_subflow,
        "validate": cmd_validate,
        "finish": cmd_finish,
        "kill-check": cmd_kill_check,
        "complete": cmd_complete,
        "event": cmd_event,
        "merge": cmd_merge,
        "remove-worktree": cmd_remove_worktree,
    }
    # Wrap dispatch with safe_handler so any exception that escapes a
    # handler becomes a structured Result.error envelope on stdout instead
    # of a Python stacktrace on stderr. Handlers' own bespoke try/except
    # blocks still produce tailored Result.error envelopes for expected
    # failure paths; this wrapper is the backstop for anything else.
    return safe_handler(
        lambda a: f"flowstate {a.cmd}",
        handlers[args.cmd],
    )(args)

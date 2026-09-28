from __future__ import annotations

import contextlib
import fcntl
import os
import uuid
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Literal

import yaml

from flowstate.time import iso_now


# Phase status vocabulary. `pending` = not yet visited, `in_progress` =
# worker is running or orchestrator is handling, `error` = the reducer /
# fanout / join error paths in traversal flipped the phase to a failure
# state (see set_phase_status callers in traversal.py), `done` = completed
# normally, `done_forced` = `--force`d past by the user, `done_bypassed` =
# skipped via `bypass_at` supervision rule. See Issue #46 and I-5 from the
# multi-branch-fixes final review. ``BranchRef.status`` uses the narrower
# ``BranchStatus`` vocabulary defined below (push-hook failure flips the
# parent BranchRef to "error" so cmd_status surfaces the reason; idle is
# branch-only and supports rolling-window fanout).
PhaseStatus = Literal[
    "pending", "in_progress", "ready_to_fire", "error",
    "done", "done_forced", "done_bypassed",
]

# Statuses that signal a phase has reached a terminal/completed state. Used
# by `_is_done` (traversal) and `set_phase_status` (here) to decide when to
# stamp `completed_at`. Centralised so the two callsites can't drift apart
# (the prior asymmetry around `done_bypassed` came from inlining the tuple
# in only one place — see Issue #46).
COMPLETED_PHASE_STATUSES: tuple[PhaseStatus, ...] = (
    "done", "done_forced", "done_bypassed",
)


# Statuses that mean a phase has FINISHED, successfully or not. Distinct from
# COMPLETED_PHASE_STATUSES (the success subset used for completed_at stamping
# and _is_done gate checks): "terminal" answers "is anyone still working
# here?", "completed" answers "did it succeed?". Spec D1.
TERMINAL_PHASE_STATUSES: tuple[PhaseStatus, ...] = (
    "done", "done_forced", "done_bypassed", "error",
)


def phase_is_terminal(status: str) -> bool:
    """True iff the phase has finished (successfully or not). Spec D1."""
    return status in TERMINAL_PHASE_STATUSES

# RunState lifecycle vocabulary. `in_progress` = active, `completed` =
# all terminal nodes reached, `aborted` = wrapup aborted by the user.
RunLifecycleState = Literal["in_progress", "completed", "aborted"]

# Branch status vocabulary for ``BranchRef.status``. Distinct from
# ``PhaseStatus`` because the branch lifecycle is narrower: a branch is
# ``pending`` until ``init_subflow_run`` stamps it ``in_progress``, may be
# paused to ``idle`` at a ``runner=orchestrator + pauses_at_min`` node
# (freeing its slot under ``max_concurrent`` for rolling-window fanout),
# then transitions to ``done`` or ``error``. See the batch-factory-pipeline
# spec (2026-06-05) section 4.2 for the full state diagram.
BranchStatus = Literal["pending", "in_progress", "idle", "done", "error"]

# Branch statuses that mean the branch has finished, successfully or not.
# "idle" is NOT terminal: an idle branch is awaiting human input and still
# blocks join/fanout completion (idle only frees a max_concurrent slot).
# Interim form of spec D1 (2026-07-02-flowstate-cleanup-refactor-design);
# stage 2 promotes this to a branch_is_terminal() predicate.
TERMINAL_BRANCH_STATUSES: tuple[BranchStatus, ...] = ("done", "error")


def branch_is_terminal(status: str) -> bool:
    """True iff the branch has finished (successfully or not). ``idle`` is
    NOT terminal — the branch is awaiting human input and will resume.
    Spec D1."""
    return status in TERMINAL_BRANCH_STATUSES


def branch_status_from_phase(status: str) -> BranchStatus:
    """The only legal way to mirror a PhaseStatus onto a BranchRef.status.
    Success statuses collapse to "done"; "error" maps to "error"; mirroring
    a non-terminal phase status is a programming error. Spec D1."""
    if status in COMPLETED_PHASE_STATUSES:
        return "done"
    if status == "error":
        return "error"
    raise ValueError(
        f"cannot mirror non-terminal phase status {status!r} onto a BranchRef"
    )


# Sibling lock file for serialising read-modify-write cycles. See Issue #27.
_LOCK_FILENAME = ".graph_run_state.lock"

# Maximum serialised size (bytes) for a single variable value. Variables hold
# references (paths/IDs/small metadata), not blobs — this ceiling keeps
# branch-merge operations cheap. See Task 13.
_MAX_VAR_BYTES = 16 * 1024


def check_var_size(name: str, value: Any) -> None:
    """Raise ValueError if ``value`` exceeds the 16 KB variable ceiling.

    Used by every internal write path (set_var, harvest_subflow_outputs,
    merge_branch_scopes, _apply_dynamic_fanout, _fire_reducer) so the ceiling
    is enforced uniformly. Sizing uses ``str(value).encode("utf-8")`` to match
    the historical ``set_var`` behaviour — keeps existing flows / fixtures
    that bumped right up against the limit observably unchanged. See Task 22
    (I1 / D13).
    """
    size = len(str(value).encode("utf-8"))
    if size > _MAX_VAR_BYTES:
        raise ValueError(
            f"variable {name!r} value size {size}B exceeds {_MAX_VAR_BYTES}B ceiling; "
            f"variables hold references (paths/IDs), not blobs"
        )


@contextlib.contextmanager
def state_lock(run_dir: Path):
    """Exclusive file lock for the duration of one read-modify-write cycle.

    Concurrent callers (orchestrator, transition scripts, manual user CLI
    invocations) all need to serialise their RunState mutations. Acquire the
    lock around any sequence that does `load → mutate → save` to prevent
    last-writer-wins data loss.

    Usage:
        with state_lock(run_dir):
            state = RunState.load(run_dir)
            state.variables["foo"] = "bar"
            state.save()

    The lock file lives at `<run_dir>/.graph_run_state.lock` and is created if
    missing. It is intentionally never deleted — a stale empty file is cheap
    and avoids a TOCTOU race between unlink and the next open.
    """
    run_dir = Path(run_dir)
    run_dir.mkdir(parents=True, exist_ok=True)
    lock_path = run_dir / _LOCK_FILENAME
    with open(lock_path, "a+") as lockfd:
        fcntl.flock(lockfd.fileno(), fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(lockfd.fileno(), fcntl.LOCK_UN)


@dataclass
class Usage:
    """Token usage + cost for one session (worker attempt or orchestrator
    session). Single shape reused by ``PhaseState.usage``, ``Attempt.usage``,
    and ``RunState.orchestrator_usage`` — previously the same seven fields
    lived independently in three places and could drift. See Issue #45h.

    ``None`` (as opposed to a zero-filled ``Usage``) is the canonical "we
    haven't observed any usage yet" sentinel — clearer than today's
    ``turns == 0`` heuristic.
    """
    input_tokens: int = 0
    output_tokens: int = 0
    cache_read_input_tokens: int = 0
    cache_creation_input_tokens: int = 0
    # opencode reports reasoning tokens as their own counter; claude has
    # no equivalent. Kept separate — folding into output_tokens would
    # silently skew cross-harness comparisons. 0 for claude sessions.
    reasoning_tokens: int = 0
    turns: int = 0
    model: str | None = None


@dataclass
class Attempt:
    """Snapshot of a single worker attempt for a phase. Created by
    `PhaseState.archive_current_attempt()` when a phase is re-spawned and we
    want to preserve the prior attempt's usage / outcome before overwriting
    the in-place fields. See Issue #6.
    """
    agent_session_id: str | None = None
    started_at: str | None = None
    completed_at: str | None = None
    validation_passed: bool | None = None
    validation_feedback: str | None = None
    summary: str | None = None
    notes: str | None = None
    # Token usage + cost for this specific attempt. `None` until populated
    # by `flowstate validate` from the worker's transcript.
    usage: Usage | None = None


@dataclass
class BranchScope:
    """Per-branch variable scope. Holds a shallow copy of the parent's
    variables taken at fork time, written into exclusively by this branch
    until the join merges them back. The copy is made by the fork handler,
    not by this dataclass.

    ``seed`` is a snapshot of ``variables`` taken at branch creation. The
    join merge measures each branch's delta against its OWN seed (not the
    parent's current values), so a trunk write made AFTER the fork can no
    longer be clobbered by the arm's stale fork-time value. Default {} so a
    legacy state file (no in-flight-run migration — spec Compatibility)
    loads cleanly; the entry-time special case of the stage-3 per-node
    snapshot ring (D4). See Task 9 / spec D8 row 8."""
    branch_id: str
    variables: dict[str, Any] = field(default_factory=dict)
    parent_node: str | None = None   # the fork/fanout node that created it
    seed: dict[str, Any] = field(default_factory=dict)
    # Per-branch monotonic child counter for mint_branch; never resets (a fork
    # after a join keeps counting), so child ids are run-unique. Round-trips
    # via asdict + the _BRANCHSCOPE_FIELDS filter like seed. Spec D2.
    next_child: int = 0


@dataclass
class JoinFiring:
    """One per-arrival reducer firing on a join. Accumulated on the join
    node's PhaseState.join_history across the run."""
    sequence: int
    triggered_by: str                 # branch_id (the real branch id; spec D2)
    triggered_by_status: PhaseStatus  # "done" | "done_forced" | ...
    input_delta: dict[str, Any] = field(default_factory=dict)
    fired_at: str | None = None
    # The node the arrival came from, kept separate from the branch id so the
    # human-readable audit survives without splitting identity. Spec D2.
    triggered_from_node: str | None = None


@dataclass
class BranchRef:
    """A child branch of a fork or dynamic_fanout, recorded on the parent
    node's PhaseState.branches. Either a subflow (subflow_run_dir set) or an
    in-run static branch (inline_root_node set).

    ``status`` uses the branch-specific ``BranchStatus`` vocabulary
    (``pending`` | ``in_progress`` | ``idle`` | ``done`` | ``error``) — see
    the module-level type alias for the lifecycle rationale."""
    branch_id: str
    status: BranchStatus = "pending"
    subflow_run_dir: str | None = None
    inline_root_node: str | None = None
    failure_reason: str | None = None


@dataclass
class ParentRef:
    """Set on a subflow run's RunState to point back at its caller. None on
    top-level runs."""
    flow: str
    run_dir: str
    fanout_node: str | None = None
    branch_id: str | None = None


@dataclass
class PhaseState:
    status: PhaseStatus = "pending"
    agent_id: str | None = None
    agent_session_id: str | None = None
    started_at: str | None = None
    completed_at: str | None = None
    validation_passed: bool | None = None
    validation_checked_at: str | None = None
    validation_feedback: str | None = None
    summary: str | None = None
    # Optional free-text caveats from the worker's `completion.yml` —
    # pre-existing env breaks, intentionally-skipped sub-steps, follow-ups.
    # Surfaced by `flowstate summary` when non-empty.
    notes: str | None = None
    # Token usage + cost for the current attempt — populated by
    # `flowstate validate` from the worker's transcript (see
    # flowstate/transcripts.py). `None` when the transcript is missing or
    # hasn't been parsed yet. See Issue #45h.
    usage: Usage | None = None
    # agentctl harness that ran this phase's worker ("claude-code",
    # "opencode", ...). Stamped by validate/finish from the spawn record;
    # None = legacy/unknown -> downstream treats as claude-code. Usage and
    # transcript retrieval dispatch on this: parsing an opencode session
    # with the claude parser silently yields zero usage.
    harness: str | None = None
    # Snapshots of prior worker attempts on this phase. Populated when a
    # re-spawn happens (validate sees a new `agent_session_id` and archives
    # the previous attempt's usage before overwriting). See Issue #6.
    prior_attempts: list[Attempt] = field(default_factory=list)
    # Multi-branch fields — all default empty/None for back-compat. A
    # pre-multi-branch state file simply omits them (load() filters unknown
    # keys, so newer-binary-reads-older-file is also safe).
    branch_id: str | None = None                          # set on phases inside a branch
    branches: list[BranchRef] = field(default_factory=list)       # set on fork / dynamic_fanout nodes
    join_history: list[JoinFiring] = field(default_factory=list)  # populated on joins with a reducer_script

    def archive_current_attempt(self) -> None:
        """Snapshot the current in-place fields into `prior_attempts` and
        reset them to defaults. Called by validate before recording a new
        worker attempt on the same phase. No-op if nothing meaningful has
        been recorded yet (no session id, no usage observed)."""
        if not self.agent_session_id and self.usage is None:
            return
        self.prior_attempts.append(Attempt(
            agent_session_id=self.agent_session_id,
            started_at=self.started_at,
            completed_at=self.completed_at,
            validation_passed=self.validation_passed,
            validation_feedback=self.validation_feedback,
            summary=self.summary,
            notes=self.notes,
            usage=self.usage,
        ))
        self.agent_session_id = None
        self.validation_passed = None
        self.validation_feedback = None
        self.summary = None
        self.notes = None
        self.usage = None

    def total_tokens(self) -> dict:
        """Token totals across current attempt + all prior attempts.

        Replaces ``total_cost_usd_estimate`` (dollar estimates removed
        2026-08-10: rate tables went stale, the "assume opus" fallback
        mispriced every unrecognised model, and consumers could not tell an
        estimate from a measurement).

        ``fresh`` = input + output + cache_creation. ``cache_read`` is
        reported SEPARATELY and never summed in: it is the context re-read
        on every turn — ~28x fresh on measured corpora — so a combined
        figure makes a chatty phase look like a thorough one.
        """
        def _one(u):
            if not u:
                return (0, 0, 0)
            fresh = (int(u.input_tokens) + int(u.output_tokens)
                     + int(u.cache_creation_input_tokens))
            return (fresh, int(u.cache_read_input_tokens), int(u.turns))
        fresh, cache_read, turns = _one(self.usage)
        for a in self.prior_attempts:
            f, c, t = _one(a.usage)
            fresh += f; cache_read += c; turns += t
        return {"fresh": fresh, "cache_read": cache_read, "turns": turns}

    def total_turns(self) -> int:
        """Sum of ``turns`` across current attempt + all prior attempts."""
        current = int(self.usage.turns) if self.usage else 0
        prior = sum(int(a.usage.turns) for a in self.prior_attempts if a.usage)
        return current + prior


@dataclass
class Event:
    t: str
    kind: str
    payload: dict[str, Any] = field(default_factory=dict)


@dataclass
class RunState:
    run_dir: Path
    run_id: str
    flow_name: str
    flow_dir: Path
    flow_dot: str
    repo_root: Path
    run_descriptor: str
    userid: str
    timestamp: str
    started_at: str
    completed_at: str | None = None
    state: RunLifecycleState = "in_progress"
    current_phases: set[str] = field(default_factory=set)
    phases: dict[str, PhaseState] = field(default_factory=dict)
    supervision: str = "medium"
    events: list[Event] = field(default_factory=list)
    # The claude session id of the orchestrator that called `init`. Set once;
    # never overwritten on save. Identifies the originating orchestrator session
    # for audit / debugging. `None` only when init was invoked without the
    # `--orchestrator-session-id` flag (typically: manual CLI / tests).
    orchestrator_session_id: str | None = None
    # Orchestrator-side usage rollup — populated by `refresh_orchestrator_usage`
    # from the orchestrator's transcript. Cheap to refresh (re-parses the
    # JSONL); callers (cmd_validate, cmd_summary, wrapup) refresh at strategic
    # points rather than on every save. ``None`` until the first parse
    # successfully observes turns > 0. See Issues #17, #45h.
    orchestrator_usage: Usage | None = None
    # agentctl harness the orchestrator itself runs on. Set when the
    # orchestrator was spawned via agentctl (the skill's launcher mode, which
    # is the normal path since 2026-08-10). None = legacy/interactive =>
    # claude-code assumed.
    orchestrator_harness: str | None = None
    # "record" when the orchestrator's identity came from its agentctl spawn
    # record, "self_reported" on the legacy path. Stamped so a reader can tell
    # an attested identity from a claimed one without consulting the registry
    # — self-report provably does not work for non-claude harnesses.
    orchestrator_identity_source: str = "self_reported"
    # Per-branch variable scopes, keyed by branch_id. Seeded at fork time with
    # a shallow copy of the parent's variables. Isolated per branch so Task 11/12
    # merge logic can consume them. Default {} for back-compat with old state files.
    branch_scopes: dict[str, BranchScope] = field(default_factory=dict)
    # Set on subflow child runs to point back at the calling parent run. None on
    # top-level runs. Persisted in metadata for auditability. See Task 17.
    parent_run: ParentRef | None = None

    def __post_init__(self) -> None:
        # Trunk is branch B1. Guaranteeing it on every construction path means
        # the `variables` property and `_vars_for_node` never KeyError.
        # setdefault: never clobber a B1 that create()/load() already populated.
        self.branch_scopes.setdefault("B1", BranchScope(branch_id="B1"))

    @property
    def variables(self) -> dict[str, Any]:
        """Trunk (B1) variable scope. `variables` is an accessor over the
        single store (`branch_scopes`), not a separate field."""
        return self.branch_scopes["B1"].variables

    @variables.setter
    def variables(self, v: dict[str, Any]) -> None:
        self.branch_scopes["B1"].variables = v

    def add_cursor(self, node: str) -> None:
        if not node:
            return
        self.current_phases.add(node)

    def remove_cursor(self, node: str) -> None:
        self.current_phases.discard(node)

    def sole_cursor(self) -> str:
        """The single active cursor. Returns "" when there are none (a
        completed run legitimately has zero cursors); raises only when more
        than one cursor is active (caller must target a specific node)."""
        if len(self.current_phases) == 1:
            return next(iter(self.current_phases))
        if len(self.current_phases) == 0:
            return ""
        raise ValueError(
            f"multiple active phases {sorted(self.current_phases)!r}; "
            f"caller must target a specific node"
        )

    @classmethod
    def create(
        cls,
        run_dir: Path,
        flow_name: str,
        flow_dir: Path,
        flow_dot: str,
        repo_root: Path,
        run_descriptor: str,
        userid: str,
        timestamp: str,
        start_node: str,
        variables: dict[str, Any],
        supervision: str = "medium",
        orchestrator_session_id: str | None = None,
    ) -> RunState:
        run_dir = Path(run_dir)
        run_dir.mkdir(parents=True, exist_ok=True)
        # Refuse to overwrite an existing run. Two concurrent bootstraps in the
        # same wall-clock second otherwise produce the same `run_dir`, both
        # populate state, and the second save silently clobbers the first.
        # See Issue #44a.
        state_path = run_dir / "graph_run_state.yml"
        if state_path.exists():
            raise RuntimeError(
                f"graph_run_state.yml already exists at {state_path!s}; "
                f"refusing to overwrite. Use a distinct run_dir or re-load "
                f"with RunState.load()."
            )
        started = iso_now()
        state = cls(
            run_dir=run_dir,
            run_id=str(uuid.uuid4()),
            flow_name=flow_name,
            flow_dir=Path(flow_dir),
            flow_dot=flow_dot,
            repo_root=Path(repo_root),
            run_descriptor=run_descriptor,
            userid=userid,
            timestamp=timestamp,
            started_at=started,
            current_phases={start_node},
            phases={start_node: PhaseState(status="done", started_at=started, completed_at=started, branch_id="B1")},
            supervision=supervision,
            orchestrator_session_id=orchestrator_session_id,
        )
        state.branch_scopes["B1"].variables = dict(variables)
        # Seed the trunk scope so B1 rides the same uniform delta path at a
        # join: its delta re-applies its own post-init writes onto itself
        # (harmless no-ops). See Task 9 / spec D8 row 8.
        state.branch_scopes["B1"].seed = dict(variables)
        state.append_event("run_started", {"by": userid})
        state.save()
        return state

    @classmethod
    def load(cls, run_dir: Path) -> RunState:
        run_dir = Path(run_dir)
        path = run_dir / "graph_run_state.yml"
        if not path.exists():
            # Back-compat: legacy filename. (No back-compat for content; this
            # only catches a half-renamed deploy.)
            legacy = run_dir / "run-state.yml"
            if legacy.exists():
                raise RuntimeError(
                    f"legacy run-state.yml found at {legacy!s}; this version of "
                    f"flowstate expects graph_run_state.yml. Re-init the run."
                )
            raise RuntimeError(f"graph_run_state.yml not found at {path!s}")
        raw = yaml.safe_load(path.read_text())
        meta = raw["metadata"]
        cur = raw.get("current", {})
        phases = {}
        # Filter unknown kwargs out of PhaseState / Attempt construction so an
        # older binary reading a state file written by a newer one doesn't
        # TypeError. Drift in the field set is expected across deploys (e.g.,
        # the orchestrator_usage block / prior_attempts were added in-place).
        # See Issue #44b.
        _PHASE_FIELDS = {f.name for f in PhaseState.__dataclass_fields__.values()}
        _ATTEMPT_FIELDS = {f.name for f in Attempt.__dataclass_fields__.values()}
        _USAGE_FIELDS = {f.name for f in Usage.__dataclass_fields__.values()}

        def _usage_from_raw(raw_usage: Any) -> Usage | None:
            """Reconstitute a nested ``usage:`` block. ``None`` (absent or
            explicit null) yields ``None`` so the canonical "no usage
            observed yet" signal round-trips cleanly."""
            if not isinstance(raw_usage, dict):
                return None
            return Usage(**{k: v for k, v in raw_usage.items() if k in _USAGE_FIELDS})

        _BRANCHREF_FIELDS = {f.name for f in BranchRef.__dataclass_fields__.values()}
        _JOINFIRING_FIELDS = {f.name for f in JoinFiring.__dataclass_fields__.values()}
        _PARENTREF_FIELDS = {f.name for f in ParentRef.__dataclass_fields__.values()}

        def _alias_legacy_sid(d: dict) -> None:
            """Read-side alias for the 2026-08-10 rename
            (claude_session_id -> agent_session_id). Persisted runs, including
            everything already archived to S3, carry the old key; the field
            filter below would silently DROP it and the run would load with no
            worker identity at all. Emit-side is the new name only."""
            if "claude_session_id" in d and "agent_session_id" not in d:
                d["agent_session_id"] = d.pop("claude_session_id")

        for name, p in (cur.get("nodes") or {}).items():
            _alias_legacy_sid(p)
            attempts_raw = p.pop("prior_attempts", None) or []
            phase_usage_raw = p.pop("usage", None)
            branches_raw = p.pop("branches", None) or []
            join_history_raw = p.pop("join_history", None) or []
            phase_kwargs = {k: v for k, v in p.items() if k in _PHASE_FIELDS}
            phase = PhaseState(**phase_kwargs)
            phase.usage = _usage_from_raw(phase_usage_raw)
            phase.prior_attempts = []
            for a in attempts_raw:
                _alias_legacy_sid(a)
                attempt_usage_raw = a.pop("usage", None)
                attempt_kwargs = {k: v for k, v in a.items() if k in _ATTEMPT_FIELDS}
                attempt = Attempt(**attempt_kwargs)
                attempt.usage = _usage_from_raw(attempt_usage_raw)
                phase.prior_attempts.append(attempt)
            phase.branches = [
                BranchRef(**{k: v for k, v in b.items() if k in _BRANCHREF_FIELDS})
                for b in branches_raw
            ]
            phase.join_history = [
                JoinFiring(**{k: v for k, v in j.items() if k in _JOINFIRING_FIELDS})
                for j in join_history_raw
            ]
            phases[name] = phase
        events = [Event(t=e["t"], kind=e["kind"], payload={k: v for k, v in e.items() if k not in ("t", "kind")}) for e in (raw.get("events") or [])]
        _BRANCHSCOPE_FIELDS = {f.name for f in BranchScope.__dataclass_fields__.values()}
        branch_scopes = {
            bid: BranchScope(**{k: v for k, v in scope_raw.items() if k in _BRANCHSCOPE_FIELDS})
            for bid, scope_raw in (raw.get("branch_scopes") or {}).items()
        }
        # Reconstruct parent_run from metadata if present. Back-compat: old state
        # files omit the key entirely, so we default to None. See Task 17.
        parent_run_raw = meta.get("parent_run")
        parent_run: ParentRef | None = None
        if isinstance(parent_run_raw, dict):
            parent_run = ParentRef(**{k: v for k, v in parent_run_raw.items() if k in _PARENTREF_FIELDS})

        # Honour an explicitly-empty `phases: []` (legitimate when parallel
        # branches have all converged) — only fall back to the legacy `phase`
        # scalar / `start_node` when `phases` is absent (None), never when it
        # is an empty list. A truthiness fallback here would silently
        # resurrect `start_node` after a legitimate empty-cursor reload.
        raw_phases = cur.get("phases")
        if raw_phases is not None:
            current_phases = set(raw_phases)
        elif cur.get("phase"):
            current_phases = {cur["phase"]}
        elif meta.get("start_node"):
            current_phases = {meta["start_node"]}
        else:
            current_phases = set()
        return cls(
            run_dir=run_dir,
            run_id=meta["run_id"],
            flow_name=meta["flow"],
            flow_dir=Path(meta["flow_dir"]),
            flow_dot=meta["flow_dot"],
            repo_root=Path(meta["repo_root"]),
            run_descriptor=meta["run_descriptor"],
            userid=meta["userid"],
            timestamp=meta["timestamp"],
            started_at=meta["started_at"],
            current_phases=current_phases,
            completed_at=meta.get("completed_at"),
            state=meta.get("state", "in_progress"),
            phases=phases,
            supervision=meta.get("supervision", "medium"),
            events=events,
            orchestrator_session_id=meta.get("orchestrator_session_id"),
            orchestrator_harness=meta.get("orchestrator_harness"),
            orchestrator_identity_source=meta.get(
                "orchestrator_identity_source", "self_reported"),
            orchestrator_usage=_usage_from_raw(raw.get("orchestrator_usage")),
            branch_scopes=branch_scopes,
            parent_run=parent_run,
        )

    def save(self) -> None:
        body = {
            "metadata": {
                "run_id": self.run_id,
                "userid": self.userid,
                "orchestrator_session_id": self.orchestrator_session_id,
                "orchestrator_harness": self.orchestrator_harness,
                "orchestrator_identity_source": self.orchestrator_identity_source,
                "flow": self.flow_name,
                "flow_dot": self.flow_dot,
                "flow_dir": str(self.flow_dir),
                "repo_root": str(self.repo_root),
                "supervision": self.supervision,
                "run_descriptor": self.run_descriptor,
                "timestamp": self.timestamp,
                "started_at": self.started_at,
                "completed_at": self.completed_at,
                "state": self.state,
                "parent_run": asdict(self.parent_run) if self.parent_run else None,
            },
            "current": {
                "phase": (sorted(self.current_phases)[0] if len(self.current_phases) == 1 else ""),
                "phases": sorted(self.current_phases),
                "nodes": {name: asdict(p) for name, p in self.phases.items()},
            },
            # asdict on a ``Usage`` dataclass yields the canonical dict; on
            # ``None`` (no usage observed yet) we emit the literal None so
            # the load path's ``isinstance(raw, dict)`` check produces
            # ``orchestrator_usage = None`` symmetrically.
            "orchestrator_usage": (
                asdict(self.orchestrator_usage) if self.orchestrator_usage else None
            ),
            "branch_scopes": {
                bid: asdict(scope) for bid, scope in self.branch_scopes.items()
            },
            "events": [
                {"t": e.t, "kind": e.kind, **e.payload} for e in self.events
            ],
        }
        dest = self.run_dir / "graph_run_state.yml"
        # Unique tmp filename so concurrent saves never collide on the swap
        # target. See Issue #27.
        tmp = dest.with_suffix(f".yml.tmp.{os.getpid()}.{uuid.uuid4().hex[:8]}")
        tmp.write_text(yaml.safe_dump(body, sort_keys=False))
        os.replace(tmp, dest)

    def set_phase_status(self, name: str, status: PhaseStatus) -> None:
        phase = self.phases.setdefault(name, PhaseState())
        phase.status = status
        if status == "in_progress" and phase.started_at is None:
            phase.started_at = iso_now()
        # Stamp `completed_at` for every terminal status, including
        # `done_bypassed`. The inline tuple here used to omit
        # `done_bypassed` — fine while the bypass path constructed
        # PhaseState directly with completed_at set, but a latent bug
        # waiting for the first caller that routed bypass through
        # set_phase_status. See Issue #46.
        if status in COMPLETED_PHASE_STATUSES:
            phase.completed_at = iso_now()

    def set_var(self, name: str, value: Any, *, branch_id: str | None = None) -> None:
        if name.startswith("_"):
            raise ValueError(
                f"variable {name!r} is system-reserved; set-var refuses "
                f"underscore-prefixed names"
            )
        check_var_size(name, value)
        # A branch's variables live in its own scope so the branch's own nodes
        # (and the merge-at-join delta) see them consistently. Fall back to
        # run-level when branch_id is unset or has no live scope.
        if branch_id is not None and branch_id in self.branch_scopes:
            self.branch_scopes[branch_id].variables[name] = value
        else:
            self.variables[name] = value

    def mint_branch(self, parent_branch_id: str) -> str:
        """Mint a run-unique child branch id under ``parent_branch_id``:
        ``{parent}.{n}`` from the parent's monotonic ``next_child`` counter,
        which NEVER resets — sequential forks from the same parent (fork ->
        join -> fork again) keep counting (B1.3, B1.4), so ids are unique for
        the run's lifetime. Does NOT create the child's scope — creation
        sites do that, as before. Spec D2."""
        scope = self.branch_scopes.get(parent_branch_id)
        if scope is None:
            raise ValueError(
                f"cannot mint a child of unknown branch {parent_branch_id!r}"
            )
        scope.next_child += 1
        return f"{parent_branch_id}.{scope.next_child}"

    def append_event(self, kind: str, payload: dict[str, Any] | None = None) -> None:
        self.events.append(Event(t=iso_now(), kind=kind, payload=payload or {}))

    def refresh_orchestrator_usage(self) -> None:
        """Re-parse the orchestrator's transcript and replace
        ``orchestrator_usage`` with the new snapshot. No-op if
        ``orchestrator_session_id`` is unset or the transcript is missing.
        Best-effort — never raises. See Issues #17, #45h.
        """
        if not self.orchestrator_session_id:
            return
        try:
            # Dispatch on the orchestrator's own harness (2026-08-10): an
            # opencode orchestrator parsed with the claude parser silently
            # yields zero usage. metadata carries it when the orchestrator
            # was spawned by agentctl; absent => claude-code, as before.
            harness = self.orchestrator_harness
            if harness in (None, "claude-code"):
                from flowstate.transcripts import parse_session_usage
                usage = parse_session_usage(self.orchestrator_session_id)
            else:
                from agentctl.harnesses.factory import make_harness
                from pathlib import Path as _P
                d = make_harness(
                    harness, _P.home() / ".agentctl"
                ).session_usage(self.orchestrator_session_id)
                if d is None:
                    return
                usage = Usage(
                    input_tokens=d["input_tokens"],
                    output_tokens=d["output_tokens"],
                    cache_read_input_tokens=d["cache_read_input_tokens"],
                    cache_creation_input_tokens=d["cache_creation_input_tokens"],
                    reasoning_tokens=d["reasoning_tokens"],
                    turns=d["turns"], model=d["model"])
        except Exception:
            return
        # Only overwrite when the parse returned signal (matches the validate
        # path's guard against zero-filled struct clobbering prior data).
        if usage.turns == 0:
            return
        self.orchestrator_usage = usage

    def workers_total_tokens(self) -> dict:
        """Token totals across every phase (including prior attempts).
        Excludes orchestrator. See Issue #17."""
        out = {"fresh": 0, "cache_read": 0, "turns": 0}
        for p in self.phases.values():
            t = p.total_tokens()
            for k in out:
                out[k] += t[k]
        return out

    def workers_total_turns(self) -> int:
        return sum(p.total_turns() for p in self.phases.values())

    def total_tokens(self) -> dict:
        """Grand total: workers + orchestrator. See Issue #17."""
        out = self.workers_total_tokens()
        u = self.orchestrator_usage
        if u:
            out["fresh"] += (int(u.input_tokens) + int(u.output_tokens)
                             + int(u.cache_creation_input_tokens))
            out["cache_read"] += int(u.cache_read_input_tokens)
            out["turns"] += int(u.turns)
        return out

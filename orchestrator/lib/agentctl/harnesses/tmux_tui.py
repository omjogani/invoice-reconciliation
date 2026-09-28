"""Shared machinery for tmux-hosted TUI harnesses.

Every supported harness so far (claude-code, opencode) runs the same way: a
persistent TUI launched inside a tmux window by a generated bash wrapper that
records the pid, waits on a completion marker (or process exit), and is
supervised through the window + worker.log + child-process evidence that this
module collects. `TmuxTuiHarness` owns all of that; a concrete harness
subclass supplies only

  - ``name``            — the harness name recorded in the registry,
  - ``AUTONOMY_FLAGS``  — the translation table from agentctl's autonomy
                          vocabulary to the harness's native CLI fragment
                          (validated loudly in :meth:`spawn`), and
  - ``_compose``        — the worker script text plus any harness-specific
                          record fields / session id.

Extracted verbatim from ``claude_code.py`` (2026-08-03) so the opencode
harness could reuse the hardened spawn/kill/probe/wait paths instead of
re-deriving them — the v4 fixes (window-authoritative liveness, caller-owned
completion contract, atomic pid writes) live HERE, once.
"""
from __future__ import annotations

import json
import os
import re
import shlex
import signal
import subprocess
import sys
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path

import yaml

from agentctl import tmux
from agentctl.harnesses.base import AgentConfig, SpawnResult
from agentctl.timeutil import now_iso


# How long to wait for a worker to exit after SIGTERM before escalating to
# SIGKILL. Short — workers don't need to handle SIGTERM gracefully; they're
# being killed because the orchestrator decided to abandon their phase. See
# Issue #44e.
_KILL_SIGTERM_TIMEOUT_S = 2.0


def _read_pid(pid_file: Path, retries: int = 3, delay: float = 0.05) -> int | None:
    """Tolerant pid-file read. The bash producer used to write non-atomically;
    that's been fixed, but a single retry-on-empty guards against any residual
    race or partial filesystem flush. Returns None when no valid pid emerges.
    See Issue #31.
    """
    for _ in range(retries):
        try:
            raw = pid_file.read_text().strip()
        except OSError:
            return None
        if raw:
            try:
                return int(raw)
            except ValueError:
                pass
        time.sleep(delay)
    return None

# The completion contract (and any other prompt preamble) is composed by the
# INVOKER, not here — flowstate appends its own boilerplate at prompt-render
# time, teams prepends its protocol preamble. See teams spec §5a.


# Grace period before declaring a spawn dead. `tmux new-window` returns
# synchronously (or errors immediately), so this no longer needs to cover
# window rendering — it only covers bash + harness startup to the first pid
# write, which is a few seconds even on a busy machine.
SPAWN_GRACE_SECONDS = 5


def _seconds_since(iso: str) -> float:
    try:
        started = datetime.strptime(iso, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)
    except (ValueError, TypeError):
        return 0.0
    return (datetime.now(timezone.utc) - started).total_seconds()


def _epoch_of(iso: str) -> float:
    try:
        dt = datetime.strptime(iso, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)
    except (ValueError, TypeError):
        return datetime.now(timezone.utc).timestamp()
    return dt.timestamp()


# Per-command truncation for descendant command strings. Keeps the probe
# payload compact — the orchestrator only needs a glance at "what's it doing",
# not full argv. See Issue #10 (activity signal) follow-up.
_CHILD_CMD_MAX = 120


def _child_processes(pid: int | None,
                     exclude_subtree_of: str | None = None) -> list[str]:
    """Command strings of all live, transitive descendant processes of ``pid``
    (display form of :func:`_child_snapshot` — see it for pid-level churn)."""
    return _child_snapshot(pid, exclude_subtree_of)[1]


def _child_snapshot(pid: int | None,
                    exclude_subtree_of: str | None = None,
                    ) -> tuple[list[int], list[str]]:
    """(pids, command strings) of all live, transitive descendants of ``pid``.

    The pid list exists for CHURN detection: ``wait`` compares the descendant
    pid SET between polls, because command strings alone cannot distinguish a
    tool respawning identical workers (gradle's javac daemons) from a static
    set of persistent servers (MCP) — same strings, different processes.

    Walks the process tree from a single `ps -A -o pid=,ppid=,command=`
    snapshot (darwin) and collects every transitive child; the worker pid
    itself is not included. Returns an empty list when ``pid`` is None/dead or
    ps is unavailable. Each command string is truncated to ~120 chars. This is
    the second liveness signal used alongside worker.log mtime for stall
    detection.

    ``exclude_subtree_of``: a child whose (untruncated) command contains this
    string is skipped TOGETHER WITH ITS WHOLE SUBTREE. The probe passes the
    spawn script's own path here, because marker-mode spawn scripts fork a
    babysitter loop (``( while ...; do sleep 2; done ) &`` — argv
    ``bash <phase_dir>/spawn.command``) that lives exactly as long as the
    worker does. Counted as a child, that loop makes every worker read as
    permanently "working", which makes the ``stalled`` outcome unreachable —
    the 2026-08-11 oc-spike run sat frozen for 45 minutes while two nested
    watchdogs reported it healthy for precisely this reason. The exclusion is
    deliberately surgical: a bare ``sleep`` elsewhere in the tree still counts
    as work (a real tool invocation can legitimately be sleeping), and the
    match string is a phase-dir-scoped absolute path, which agent workloads
    cannot plausibly collide with.

    ``-A`` is required: without it macOS `ps` lists only processes attached to
    the caller's controlling terminal, and a worker spawned in its own Terminal
    window (a separate session) plus its children would be invisible.
    """
    if pid is None:
        return [], []
    try:
        out = subprocess.run(
            ["ps", "-A", "-o", "pid=,ppid=,command="],
            capture_output=True,
            text=True,
            timeout=5,
        ).stdout
    except (OSError, subprocess.SubprocessError):
        return [], []
    children_by_parent: dict[int, list[tuple[int, str]]] = {}
    for line in out.splitlines():
        parts = line.split(None, 2)
        if len(parts) < 2:
            continue
        try:
            cpid = int(parts[0])
            cppid = int(parts[1])
        except ValueError:
            continue
        cmd = parts[2] if len(parts) > 2 else ""
        children_by_parent.setdefault(cppid, []).append((cpid, cmd))
    pids: list[int] = []
    collected: list[str] = []
    seen: set[int] = set()
    stack = [pid]
    while stack:
        cur = stack.pop()
        for cpid, cmd in children_by_parent.get(cur, []):
            if cpid in seen:
                continue
            seen.add(cpid)
            if exclude_subtree_of and exclude_subtree_of in cmd:
                # The harness's own babysitter: not evidence of work, and
                # neither are its children (its `sleep`) — skip the subtree
                # by not pushing cpid.
                continue
            pids.append(cpid)
            collected.append(cmd[:_CHILD_CMD_MAX])
            stack.append(cpid)
    return pids, collected


def _window_title(branch_token: str, phase: str, max_len: int = 40) -> str:
    """Compose the tmux window name, capped at ``max_len`` chars.

    Top-level workers pass an empty ``branch_token`` and get the bare
    ``phase``. Batch-child workers pass the leading branch id of their run
    descriptor (e.g. ``B1_3``) and get ``<token>-<phase>``. Over the cap, the
    phase is kept WHOLE and the branch token is tail-trimmed to end in a single
    ellipsis; if the phase alone leaves no room (>= max_len - 2), the token is
    dropped entirely. The phase is never trimmed.
    """
    if not branch_token:
        return phase
    title = f"{branch_token}-{phase}"
    if len(title) <= max_len:
        return title
    if len(phase) >= max_len - 2:
        return phase
    budget = max_len - len(phase) - 1  # chars for token + separator
    return branch_token[: budget - 1] + "…-" + phase


def _marker_from_record(body: dict) -> Path | None:
    """Completion-marker path for a registry record. Legacy records (written
    before the marker was caller-supplied) predate the key and default to the
    old fixed location; a recorded None means wait-for-exit (no marker)."""
    if "completion_marker" not in body:
        return Path(body["phase_dir"]) / "completion.yml"
    cm = body["completion_marker"]
    return Path(cm) if cm else None


class SessionResolutionError(RuntimeError):
    """The harness could not produce an authoritative session id.

    2026-08-10 contract: the id is produced BY THE HARNESS — never accepted
    from the worker. An opencode worker once self-reported the OPERATOR'S
    session id (leaked through the environment); 651 turns of the operator's
    own session were harvested as that worker's usage, and the leak tripwire
    would have audited the wrong transcript entirely. A spawn that cannot
    establish its worker's identity must fail, not half-start.
    """


# The ONE documented exception to the verbatim-prompt rule (teams spec §5a).
# At spawn these literals are replaced in the prompt text:
#   __WORKER_SESSION_ID__ -> the value the worker must echo back in
#       completion.yml (claude-code: its real pre-minted session id;
#       harnesses that mint post-launch: a tok_ nonce recorded as
#       spawn_token).
#   __AGENT_ID__          -> the agentctl agent id.
# The worker ECHOES what it was told; it never DISCOVERS an id, because the
# ambient environment carries the operator's id and workers have obediently
# reported it before.
WORKER_SESSION_ID_PLACEHOLDER = "__WORKER_SESSION_ID__"
AGENT_ID_PLACEHOLDER = "__AGENT_ID__"

class TmuxTuiHarness:
    """Base class — see module docstring for the subclass contract."""

    # Subclasses MUST override both. The empty table makes an unported
    # subclass fail the very first spawn (even autonomy=None misses), rather
    # than silently accepting values it never translates.
    name: str = ""
    AUTONOMY_FLAGS: dict[str | None, str] = {}

    def __init__(self, registry_dir: Path):
        self.registry_dir = Path(registry_dir)
        self.registry_dir.mkdir(parents=True, exist_ok=True)

    def _compose(
        self,
        config: AgentConfig,
        *,
        temp_dir: Path,
        prompt_file: Path,
        pid_file: Path,
        completion_marker: Path | None,
    ) -> tuple[str | None, dict, str]:
        """Return ``(session_id, extra_record_fields, script_text)``.

        ``script_text`` is the full bash wrapper written to spawn.command; the
        subclass composes it (typically via :meth:`_pid_write_snippet` and
        :meth:`_wait_and_kill_blocks`) because the launch line, session-id
        handling and any post-wait telemetry are harness-specific.
        ``session_id`` (may be None) lands on the SpawnResult;
        ``extra_record_fields`` are merged into the registry record.
        """
        raise NotImplementedError

    @staticmethod
    def _pid_write_snippet(pid_var: str, pid_file: Path) -> str:
        """Atomic pid write (tmpfile + mv). A naive `echo $pid > file` leaves
        a brief window where a reader sees an empty file between creat() and
        write(). See Issue #31."""
        q = shlex.quote(str(pid_file))
        return f"printf '%s\\n' ${pid_var} > {q}.tmp && mv {q}.tmp {q}\n"

    @staticmethod
    def _wait_and_kill_blocks(
        pid_var: str, completion_marker: Path | None
    ) -> tuple[str, str]:
        """The wrapper's wait loop + kill tail for both lifecycle modes.

        Marker mode: poll for the marker file and end the harness process when
        it appears (flowstate's one-shot worker contract; teams' `leave`
        mechanism). Marker=None: wait-for-exit — the wrapper (and therefore
        the window) lives exactly as long as the agent does, and nothing is
        ever killed. See teams spec §5a.
        """
        if completion_marker is not None:
            q_marker = shlex.quote(str(completion_marker))
            wait_block = (
                f"while kill -0 ${pid_var} 2>/dev/null && "
                f"[ ! -f {q_marker} ]; do sleep 2; done\n"
            )
            kill_block = f"kill ${pid_var} 2>/dev/null || true\n"
        else:
            wait_block = f"while kill -0 ${pid_var} 2>/dev/null; do sleep 2; done\n"
            kill_block = ""
        return wait_block, kill_block

    def _record(self, agent_id: str, body: dict) -> Path:
        path = self.registry_dir / f"{agent_id}.yml"
        path.write_text(yaml.safe_dump(body, sort_keys=False))
        return path

    def _read(self, agent_id: str) -> dict:
        path = self.registry_dir / f"{agent_id}.yml"
        return yaml.safe_load(path.read_text())

    def _resolve_session(
        self, config: AgentConfig, temp_root: Path
    ) -> tuple[str, bool, str | None]:
        """Resolve the run's tmux session label, creating it on first spawn.

        Deterministic per run: ``temp_root`` (the run's temp root, one level
        above the per-phase temp dir) holds a ``tmux-session`` file recording
        the resolved label. If it exists, its content is reused verbatim.
        Otherwise the requested label (``--session`` or the run descriptor) is
        used, suffixed with a compact UTC timestamp if a session of that name
        already exists (a stale or foreign session — this run has written no
        file yet, so it cannot be ours). Returns ``(label, created,
        placeholder_window_id)``; the placeholder id is non-None only when
        this spawn created the session, and the caller must kill that window
        once the worker window is up (see ``ensure_session``).
        """
        session_file = temp_root / "tmux-session"
        if session_file.exists():
            label = session_file.read_text().strip()
            # The recorded session can die (e.g. killed by hand between
            # attempts). Recreate it under the SAME label — the name is free
            # again — and treat this spawn as the creator (console window +
            # viewer re-pop).
            if tmux.has_session(label, socket=config.tmux_socket):
                return label, False, None
            placeholder_id = tmux.ensure_session(label, socket=config.tmux_socket)
            tmux.configure_factory_session(label, socket=config.tmux_socket)
            return label, True, placeholder_id
        label = config.session or config.run_descriptor
        if tmux.has_session(label, socket=config.tmux_socket):
            # An explicitly requested session (`--session`, e.g. a child run
            # joining its batch parent's session) is a JOIN target, never a
            # collision — suffixing applies only to descriptor-derived labels
            # colliding with a stale/foreign session.
            if config.session:
                tmp = session_file.with_suffix(".tmp")
                tmp.write_text(label + "\n")
                tmp.rename(session_file)
                return label, False, None
            label = f"{label}-{datetime.now(timezone.utc).strftime('%H%M%S')}"
        placeholder_id = tmux.ensure_session(label, socket=config.tmux_socket)
        tmux.configure_factory_session(label, socket=config.tmux_socket)
        # Atomic write (tmp + rename), matching the pid-file idiom.
        tmp = session_file.with_suffix(".tmp")
        tmp.write_text(label + "\n")
        tmp.rename(session_file)
        return label, True, placeholder_id

    @staticmethod
    def _write_attach_script(temp_root: Path, session_label: str) -> Path:
        """Write the macOS viewer attach script next to the ``tmux-session``
        file and return its path. Isolated from the ``open`` call so the
        written artefact is testable without launching Terminal."""
        attach_script = temp_root / "attach.command"
        attach_script.write_text(
            "#!/bin/bash\nexec tmux attach -t " + shlex.quote(session_label) + "\n"
        )
        attach_script.chmod(0o755)
        return attach_script

    def _resolve_session_id(
        self,
        config: AgentConfig,
        *,
        spawn_epoch_ms: int,
        composed_session_id: str | None,
    ) -> str:
        """The AUTHORITATIVE session id for the worker just launched.

        Base behaviour: the id was composed at spawn time (claude-code
        pre-assigns a uuid via ``--session-id``), so resolution is the
        identity function. A harness whose CLI mints its own id after launch
        (opencode) overrides this with a poll. Returning the operator's
        ambient id — or anything read back from the worker — is precisely
        the bug this hook exists to prevent.
        """
        if composed_session_id is None:
            raise SessionResolutionError(
                f"{self.name} harness composed no session id and does not "
                f"override _resolve_session_id — every harness must produce "
                f"an authoritative id at spawn"
            )
        return composed_session_id

    @staticmethod
    def _window_command(config: AgentConfig, cmd_path: Path) -> str:
        """The tmux window's command: hooks compose at this OUTER layer so the
        per-harness wrapper scripts stay untouched (opencode's exec-owns-the-
        tty posture in particular). The pre-spawn script is SOURCED (`.`, by
        POSIX sh — keep it sh-compatible) so its exports reach the agent;
        failure aborts before the harness starts. Two failure shapes: a
        script that RETURNS non-zero hits the `||` handler (explicit failed
        line, exit 1); a script that calls `exit N` kills the sourcing shell
        outright — the handler cannot run, which is why the sentinel line is
        echoed BEFORE sourcing: a log that ends right after it says the
        pre-spawn died, with the script's own output as the cause. Both
        shapes abort before the harness starts. The post-kill tail needs the
        outer shell to survive the wrapper, so exec is dropped only when that
        tail exists; a blocked-in-wait sh does not read stdin, so the tty
        still routes to the TUI. Double quotes only inside — the whole
        command travels in single quotes."""
        q_cmd = shlex.quote(str(cmd_path))
        # CLAUDE_CODE_CHILD_SESSION leaking into a nested worker makes it
        # silently disable transcript saving (live finding). Scrubbed HERE,
        # at the layer that owns spawning, so every launcher benefits rather
        # than each caller rediscovering it. Prefixes the INVOCATION, not the
        # script path — `bash env -u ... <path>` would try to run a file
        # called "env".
        run = "env -u CLAUDE_CODE_CHILD_SESSION bash"
        # hooks.log (beside spawn.command / worker.log): the pane's own record
        # of hook outcomes. The watcher folds its lines into terminal
        # lifecycle events, which is how the JOURNAL gets verifiable evidence
        # that the scripts ran — pane-local files die with the team dir.
        q_hooks = shlex.quote(str(cmd_path.parent / "hooks.log"))
        ts = '$(date -u +%Y-%m-%dT%H:%M:%SZ)'
        bits = ["sleep 0.2"]
        if config.pre_spawn_script is not None:
            q_pre = shlex.quote(str(config.pre_spawn_script))
            # cd aborts loudly: spawn() validated the dir exists, but a
            # vanish-between-validation-and-execution race must not silently
            # run the pre-spawn (and agent) in the wrong directory.
            bits.append(
                f"cd {shlex.quote(str(config.working_dir))} || "
                '{ echo "[agentctl] cd to working dir failed"; exit 1; }'
            )
            # Pane-attested cwd (from $(pwd), AFTER the cd) — not a config
            # echo. This is the journal-side proof the spawn dir was entered.
            bits.append(f'echo "spawn_cwd $(pwd) {ts}" >> {q_hooks}')
            # start line BEFORE sourcing: a sourced `exit N` kills the shell,
            # so start-without-ok is the evidence of a pre-spawn death.
            bits.append(f'echo "pre_spawn start {ts}" >> {q_hooks}')
            bits.append(f'echo "[agentctl] pre-spawn: sourcing {q_pre}"')
            bits.append(
                f". {q_pre} || "
                f'{{ rc=$?; echo "pre_spawn fail rc=$rc {ts}" >> {q_hooks}; '
                'echo "[agentctl] pre-spawn script failed (rc=$rc)"; exit 1; }'
            )
            bits.append(f'echo "pre_spawn ok {ts}" >> {q_hooks}')
        if config.post_kill_script is not None:
            bits.append(f"{run} {q_cmd}")
            bits.append(f"bash {shlex.quote(str(config.post_kill_script))}")
            bits.append(f'prc=$?; echo "post_kill rc=$prc {ts}" >> {q_hooks}')
            # if-form, not `[ ... ] &&`: the pane's exit code must stay 0 when
            # the post-kill succeeded.
            bits.append(
                'if [ $prc -ne 0 ]; then '
                'echo "[agentctl] post-kill script failed (rc=$prc)"; fi'
            )
        else:
            bits.append(f"exec {run} {q_cmd}")
        return f"sh -c '{'; '.join(bits)}'"

    def spawn(self, config: AgentConfig) -> SpawnResult:
        # Validate BEFORE any side effect (dirs, files, tmux): a spawn that
        # cannot honour the requested autonomy must not half-start. A silently
        # dropped autonomy setting is the bug class that cost the first
        # 2026-08-03 dry run.
        if config.autonomy not in self.AUTONOMY_FLAGS:
            supported = sorted(k for k in self.AUTONOMY_FLAGS if k)
            raise ValueError(
                f"autonomy {config.autonomy!r} is not supported by the "
                f"{self.name} harness (supported: {supported}, or omit for "
                f"the harness default)"
            )
        # Script hooks fail fast, before any side effect: a spawn whose
        # pre/post script cannot run must not half-start.
        for label, script in (("pre-spawn", config.pre_spawn_script),
                              ("post-kill", config.post_kill_script)):
            if script is not None and not Path(script).is_file():
                raise ValueError(f"{label} script not found: {script}")
        # Same for the working dir: the pane's cd would otherwise fail and
        # (sh -c has no set -e) everything after it would silently run in the
        # wrong directory.
        if not Path(config.working_dir).is_dir():
            raise ValueError(f"working dir not found: {config.working_dir}")
        agent_id = str(uuid.uuid4())
        # Temp dir is supplied by the caller (typically resolved via
        # flowstate's node-config payload, which uses the canonical
        # `flowstate.temp_layout.phase_temp_dir` helper). See Issue #45b
        # — agentctl no longer reaches into flowstate to compute the
        # layout itself.
        # RESOLVED, not taken as given: every path the generated
        # spawn.command references (pid, prompt, completion marker) is
        # rendered into a script whose FIRST act is `cd <working_dir>`. A
        # caller that passes them relative — `agentctl team spawn` forwards
        # `--team-dir`/`--temp-dir` verbatim — silently reinterprets them
        # against the working dir instead of against its own cwd.
        #
        # Live failure (exp01 oc-mixed/DSCO-311, 2026-08-19): the teamlead
        # passed a task WORKTREE as --working-dir, so the script cd'd to
        # `<sandbox>/encore/.worktrees/<task>` and then looked for
        # `factory/execution/teams/...` under it. That path only exists at the
        # sandbox root, so the pid write failed and `$(cat <prompt>)` returned
        # empty — launching a teammate with `--prompt ""`: alive, idle, and
        # doing nothing, with no error anywhere (the script runs `set -uo
        # pipefail`, deliberately without -e). The sibling cell DSCO-200
        # completed fine purely because its teamlead happened to pass the
        # sandbox root instead. Both choices are reasonable; only one worked.
        #
        # Resolving here rather than in each caller matches how this file
        # already handles CLAUDE_CODE_CHILD_SESSION: fix it at the layer that
        # owns spawning so no launcher has to rediscover it. `resolve()` is
        # against agentctl's own cwd, which is where a relative path was
        # always meant to be read — no path is hardcoded.
        temp_dir = Path(config.temp_dir).resolve()
        temp_dir.mkdir(parents=True, exist_ok=True)
        # The run's temp root (one level up from the per-phase temp dir) holds
        # the shared tmux-session file and the viewer attach script.
        temp_root = temp_dir.parent
        prompt_file = temp_dir / "prompt.txt"
        cmd_path = temp_dir / "spawn.command"
        completion_marker = (
            Path(config.completion_marker).resolve()
            if config.completion_marker else None
        )
        pid_file = temp_dir / "pid"

        if completion_marker is not None:
            completion_marker.parent.mkdir(parents=True, exist_ok=True)
            completion_marker.unlink(missing_ok=True)
        pid_file.unlink(missing_ok=True)

        session_id, extra_fields, script_text = self._compose(
            config,
            temp_dir=temp_dir,
            prompt_file=prompt_file,
            pid_file=pid_file,
            completion_marker=completion_marker,
        )

        # The prompt is written VERBATIM (teams spec §5a) except for the two
        # identity placeholders above — the invoker composes everything else.
        # Echo value: the real sid when _compose minted one (claude-code),
        # otherwise a tok_ nonce. The tok_ prefix is deliberate: a bare uuid
        # here would be visually indistinguishable from a claude session id,
        # and any id-shaped value in an id-shaped slot eventually gets
        # consumed as an identity by some code path.
        spawn_token: str | None = None
        echo_value = session_id
        if echo_value is None:
            spawn_token = f"tok_{uuid.uuid4()}"
            echo_value = spawn_token

        # prompt.raw.txt survives for retries: a respawn must re-substitute
        # fresh values, and the substituted prompt carries the PREVIOUS
        # attempt's echo value — an honest retry would otherwise hard-fail
        # its own completion on a stale echo.
        (temp_dir / "prompt.raw.txt").write_text(config.prompt)
        prompt_file.write_text(
            config.prompt
            .replace(WORKER_SESSION_ID_PLACEHOLDER, echo_value)
            .replace(AGENT_ID_PLACEHOLDER, agent_id)
        )

        # Window name: the bare phase for a top-level worker; branch-token
        # prefixed (e.g. `B1_3-frontend_plan_gen`) for a batch-child worker,
        # derived from the leading branch id of the run descriptor.
        m = re.match(r"(B\d+(?:_\d+)*)-", config.run_descriptor)
        window_name = _window_title(m.group(1) if m else "", config.phase)

        cmd_path.write_text(script_text)
        cmd_path.chmod(0o755)

        # Resolve (and, on first spawn of the run, create) the tmux session,
        # then launch the worker in a new window. A TmuxError here propagates —
        # the CLI `_safe` wrapper converts it to a structured Result.error.
        session_label, created_session, placeholder_id = self._resolve_session(
            config, temp_root
        )
        # Truncate any prior attempt's log BEFORE the window starts: a respawn
        # into the same phase must not append onto the previous worker's
        # output, or forensics read two attempts as one transcript.
        worker_log = temp_dir / "worker.log"
        worker_log.unlink(missing_ok=True)
        # Same reasoning for the hook-outcome log: a respawn (new incarnation)
        # must not read the previous attempt's hook lines as its own.
        (temp_dir / "hooks.log").unlink(missing_ok=True)
        # And for wait()'s churn sidecar: the previous incarnation's child
        # pids are not this worker's baseline.
        (temp_dir / "stall_probe.json").unlink(missing_ok=True)
        # The 0.2s head-start lets pipe-pane attach before the worker's first
        # output: pipe-pane only captures from attachment onward, so without
        # it the opening lines (the TUI's banner, a fast script's first echo)
        # race the attach and get lost.
        spawn_epoch_ms = int(time.time() * 1000)
        window_id = tmux.new_window(
            session_label,
            window_name,
            self._window_command(config, cmd_path),
            socket=config.tmux_socket,
        )
        # pipe-pane tees the pane's output to worker.log. The old Terminal.app
        # path never actually produced this file, so the probe's log-mtime
        # activity signal only becomes a real signal with this change.
        tmux.pipe_pane(window_id, str(worker_log), socket=config.tmux_socket)
        # Pin the phase name: otherwise automatic-rename shows the pane's
        # command ("bash /…/spawn.command", then "exit"), and the TUI's own
        # OSC title escapes overwrite it too.
        try:
            tmux.lock_window_name(window_id, socket=config.tmux_socket)
        except tmux.TmuxError:
            pass
        # Resolve the worker's authoritative session id (2026-08-10: produced
        # by the harness, never accepted from the worker). claude-code
        # resolves instantly — it pre-assigned the uuid at compose time;
        # harnesses whose CLI mints post-launch poll here. Failure tears the
        # window down: a worker whose identity is unknown can be neither
        # metered nor audited, so letting it run produces work we must throw
        # away.
        try:
            session_id = self._resolve_session_id(
                config,
                spawn_epoch_ms=spawn_epoch_ms,
                composed_session_id=session_id,
            )
        except SessionResolutionError:
            try:
                tmux.kill_window(window_id, socket=config.tmux_socket)
            except tmux.TmuxError:
                pass
            raise

        # Repurpose the placeholder window new-session created as the run's
        # `console` window. It deliberately outlives workers: without it the
        # session dies whenever zero workers are alive — which is between
        # every pair of phases, since finish kills each worker before the
        # next spawns — churning the viewer with a fresh Terminal per phase
        # (2026-07-10 E2E). Teardown is explicit at run end: the orchestrator
        # runs `tmux kill-session -t <session>` when the flow terminates.
        if placeholder_id is not None:
            try:
                tmux.rename_window(placeholder_id, "console", socket=config.tmux_socket)
                tmux.lock_window_name(placeholder_id, socket=config.tmux_socket)
            except tmux.TmuxError:
                pass

        # macOS viewer (off the critical path): the spawn that created the
        # session opens a Terminal attached to it. Skipped entirely when a
        # named socket is set (tests) or AGENTCTL_NO_VIEWER is set; failure of
        # the viewer must NEVER fail the spawn.
        if (
            created_session
            and config.viewer
            and config.tmux_socket is None
            and sys.platform == "darwin"
            and not os.environ.get("AGENTCTL_NO_VIEWER")
        ):
            try:
                attach_script = self._write_attach_script(temp_root, session_label)
                subprocess.Popen(
                    ["/usr/bin/open", "-a", "Terminal", str(attach_script)],
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                )
            except Exception:
                pass

        body = {
            "agent_id": agent_id,
            "pid_file": str(pid_file),
            "phase_dir": str(temp_dir),      # field name is legacy; semantics = temp dir
            "working_dir": str(config.working_dir),
            "phase": config.phase,
            "harness": self.name,
            "model": config.model,
            "started_at": now_iso(),
            "completion_marker": str(completion_marker) if completion_marker else None,
            "run_dir": str(config.run_dir),
            "tmux_session": session_label,
            "tmux_window_id": window_id,
            # None in production (default server); a named socket only in tests
            # so kill/send route to the disposable server the spawn used.
            "tmux_socket": config.tmux_socket,
            # Uniform across harnesses: the authoritative identity, plus the
            # echo value when it differs from it (opencode's tok_ nonce).
            "agent_session_id": session_id,
            "spawn_token": spawn_token,
            **extra_fields,
        }
        self._record(agent_id, body)

        return SpawnResult(
            agent_id=agent_id,
            session_id=session_id,
            pid_file=pid_file,
            phase_dir=temp_dir,
            started_at=body["started_at"],
            tmux_session=session_label,
            tmux_window_id=window_id,
        )

    def kill(self, agent_id: str) -> None:
        """Terminate the worker process and clean up its completion marker.

        Pure mechanism — does not adjudicate whether the kill is safe. Phase-
        status precondition (was the worker done?) now lives in flowstate as
        ``flowstate kill-check``; callers wanting the gate must call it
        before invoking this. See Issue #45a.
        """
        body = self._read(agent_id)
        # Close the tmux window first — that kills the pane's process tree in
        # the common case. Legacy records (spawned before the tmux migration)
        # have no window id and skip straight to the pid path. A window that's
        # already gone raises TmuxError from kill-window; swallow it and let the
        # pid fallback below catch any reparented/wedged worker.
        window_id = body.get("tmux_window_id")
        if window_id:
            try:
                tmux.kill_window(window_id, socket=body.get("tmux_socket"))
            except tmux.TmuxError:
                pass
        pid_file = Path(body["pid_file"])
        if pid_file.exists():
            pid = _read_pid(pid_file)
            if pid is not None:
                # Send SIGTERM, then poll briefly for exit. If the worker is in
                # uninterruptible sleep or otherwise ignores SIGTERM, escalate
                # to SIGKILL so we don't unlink completion.yml while the
                # `spawn.command` watcher is still writing. See Issue #44e.
                try:
                    os.kill(pid, signal.SIGTERM)
                except ProcessLookupError:
                    pid = None
                if pid is not None:
                    deadline = time.monotonic() + _KILL_SIGTERM_TIMEOUT_S
                    while time.monotonic() < deadline:
                        try:
                            os.kill(pid, 0)  # signal 0 → existence probe
                        except ProcessLookupError:
                            pid = None
                            break
                        time.sleep(0.05)
                if pid is not None:
                    try:
                        os.kill(pid, signal.SIGKILL)
                    except ProcessLookupError:
                        pass
        # Delete the completion marker so a respawn into the same phase has a
        # clean slate. Resolved from the record (spec §5a).
        completion_path = _marker_from_record(body)
        if completion_path is not None:
            completion_path.unlink(missing_ok=True)

    def send(self, agent_id: str, text: str, interrupt: bool = False) -> None:
        """Inject ``text`` into a live worker's tmux window.

        Resolves the worker's window (and the socket it was spawned on) from the
        registry, then types ``text`` literally followed by Enter. Legacy
        records (spawned before the tmux migration) or agents that never spawned
        have no ``tmux_window_id`` — raise a clear ``RuntimeError`` the CLI turns
        into a structured ``Result.error``.

        ``interrupt=True`` sends Escape first (ending the current turn), then a
        short delay, then the text — stop-and-redirect without kill+respawn.
        """
        body = self._read(agent_id)
        window_id = body.get("tmux_window_id")
        if not window_id:
            raise RuntimeError(
                "agent has no tmux window (legacy or never spawned)"
            )
        socket = body.get("tmux_socket")
        if interrupt:
            tmux.send_escape(window_id, socket=socket)
            time.sleep(0.3)
        tmux.send_text(window_id, text, socket=socket)

    def status(self, agent_id: str) -> dict:
        """Point-in-time evidence snapshot for the CLI ``status`` command.

        Thin alias over :meth:`probe`; the two share one evidence-collection
        path so ``status`` also reports ``child_processes`` and ``wait`` polls
        the same signals.
        """
        return self.probe(agent_id)

    def session_last_activity(self, session_id: str) -> float | None:
        """Epoch seconds of the session's last recorded progress, read from
        the harness's OWN session store (claude's JSONL, opencode's sqlite).

        This is the truthful liveness signal. The two process-level signals
        are both fooled in practice: worker.log is a tmux pipe-pane capture,
        and a TUI whose model stream died MID-TURN keeps animating its
        spinner, so the log grows forever (2026-08-11 oc-spike run 4: the
        orchestrator's log grew 27KB/5s for two hours after its last model
        turn); child processes include persistent MCP servers, which outlive
        any turn. The session store only moves when the harness records real
        session progress, and freezes the moment the model stops.

        Returns None when the harness cannot answer (no store, no row, store
        layout changed) — callers must then fall back to the process-level
        signals rather than treating None as "idle forever".
        """
        return None

    def probe(self, agent_id: str) -> dict:
        """Collect the full evidence snapshot for ``agent_id``.

        Sole source of ground truth for both ``status`` (one-shot) and ``wait``
        (polled). Reports process liveness, completion/awaiting-human/spawn-failed
        markers, the activity signal (harness session store when available,
        worker.log mtime otherwise), and the worker's live descendant
        ``child_processes`` (the secondary stall signal).
        """
        body = self._read(agent_id)
        pid_file = Path(body["pid_file"])
        phase_dir = Path(body["phase_dir"])
        # Marker path comes from the spawn record (spec §5a) — it may live
        # outside phase_dir (teams' leave.marker) or be absent entirely
        # (wait-for-exit mode, where there is nothing to poll for).
        completion_path = _marker_from_record(body)
        marker_present = completion_path.exists() if completion_path is not None else False
        worker_log = phase_dir / "worker.log"
        # Worker writes this marker before blocking on AskUserQuestion or
        # similar human-input pauses; clears it on resume. See Issue #21.
        # flowstate's prompt boilerplate derives this path as
        # `completion_path.parent / "awaiting_human.yml"`, which coincides with
        # phase_dir only because flowstate's marker lives in the temp dir — if a
        # caller relocates the marker, awaiting-human detection still watches
        # phase_dir (see flowstate/completion.py).
        awaiting_human_path = phase_dir / "awaiting_human.yml"
        awaiting_human = awaiting_human_path.exists()
        awaiting_human_reason: str | None = None
        if awaiting_human:
            try:
                marker = yaml.safe_load(awaiting_human_path.read_text()) or {}
                awaiting_human_reason = marker.get("question")
            except Exception:
                awaiting_human_reason = None
        # The RECORD is the only identity source (2026-08-10). The worker's
        # own `_session_id` is read purely as a CROSS-CHECK against the value
        # the spawn told it — spawn_token when set (opencode echoes the tok_
        # nonce), else the real sid. The fallback that used to live here
        # ADOPTED the self-report when the record had none: that is how an
        # operator's env-leaked session id became a worker's recorded
        # identity. A mismatch is surfaced, never silently resolved.
        session_id: str | None = (body.get("agent_session_id")
                                  or body.get("claude_session_id"))
        expected_echo: str | None = body.get("spawn_token") or session_id
        worker_reported: str | None = None
        if completion_path is not None and completion_path.exists():
            try:
                comp = yaml.safe_load(completion_path.read_text()) or {}
                worker_reported = comp.get("_session_id")
            except Exception:
                worker_reported = None

        # Liveness signal: track wall-clock since the worker last made real
        # progress. The harness's own session store is authoritative when it
        # can answer (see session_last_activity); worker.log mtime is the
        # fallback — and for TUI harnesses it is a LYING fallback mid-turn,
        # since an animating spinner keeps the pipe-pane capture growing.
        session_epoch = (
            self.session_last_activity(session_id) if session_id else None
        )
        if session_epoch is not None:
            last_activity_epoch = session_epoch
            last_activity_source = "session_store"
        elif worker_log.exists():
            last_activity_epoch = worker_log.stat().st_mtime
            last_activity_source = "worker.log"
        else:
            # Fall back to spawn time when worker.log hasn't been created yet
            # (very early in the spawn, before the TUI wrote its first line).
            last_activity_epoch = _epoch_of(body["started_at"])
            last_activity_source = "started_at"
        seconds_since_last_activity = max(
            0.0, datetime.now(timezone.utc).timestamp() - last_activity_epoch
        )
        last_activity_at = datetime.fromtimestamp(
            last_activity_epoch, tz=timezone.utc
        ).strftime("%Y-%m-%dT%H:%M:%SZ")
        if not pid_file.exists():
            spawn_failed = False
            spawn_failed_reason: str | None = None
            elapsed = _seconds_since(body["started_at"])
            if elapsed > SPAWN_GRACE_SECONDS:
                spawn_failed = True
                spawn_failed_reason = (
                    f"pid file never appeared after {int(elapsed)}s "
                    f"(grace = {SPAWN_GRACE_SECONDS}s). The tmux window's worker "
                    f"script (spawn.command) likely never got the harness running — "
                    f"common causes include the harness CLI not being on PATH in the "
                    f"tmux server's environment or the script erroring before the pid "
                    f"write. Attach with `tmux attach -t <tmux_session>` (or read "
                    f"<phase_dir>/worker.log) for the captured pane output."
                )
                # Persist the failure on disk so orchestrator forensics survives
                # across multiple poll cycles and the run.
                marker = Path(body["phase_dir"]) / "spawn_failed.yml"
                if not marker.exists():
                    marker.write_text(yaml.safe_dump({
                        "intent": f"launch {self.name} worker in a tmux window",
                        "failure": spawn_failed_reason,
                        "suggestion": (
                            "1) Attach to the worker's tmux window "
                            "(`tmux attach -t <tmux_session>`) and read the visible "
                            "error. 2) Confirm the harness CLI is on PATH for the tmux "
                            "server. 3) Re-run the orchestrator to retry this node."
                        ),
                        "phase": body["phase"],
                        "started_at": body["started_at"],
                        "elapsed_seconds": int(elapsed),
                    }, sort_keys=False))
            return {
                "agent_id": agent_id,
                "pid": None,
                "alive": False,
                "completion_marker": marker_present,
                "agent_session_id": session_id,
                "worker_reported_session_id": worker_reported,
                "session_id_mismatch": bool(
                    expected_echo and worker_reported
                    and worker_reported != expected_echo),
                "phase": body["phase"],
                "phase_dir": body["phase_dir"],
                "tmux_session": body.get("tmux_session"),
                "tmux_window_id": body.get("tmux_window_id"),
                "spawn_failed": spawn_failed,
                "spawn_failed_reason": spawn_failed_reason,
                "last_activity_at": last_activity_at,
                "last_activity_source": last_activity_source,
                "seconds_since_last_activity": int(seconds_since_last_activity),
                "awaiting_human": awaiting_human,
                "awaiting_human_reason": awaiting_human_reason,
                "child_processes": [],
                "child_pids": [],
            }
        pid = _read_pid(pid_file)
        if pid is None:
            # File exists but is empty / unparseable; treat as not-alive and
            # let the caller poll again. Cheaper than raising ValueError here
            # and forcing every status caller to handle it.
            return {
                "agent_id": agent_id,
                "pid": None,
                "alive": False,
                "completion_marker": marker_present,
                "agent_session_id": session_id,
                "worker_reported_session_id": worker_reported,
                "session_id_mismatch": bool(
                    expected_echo and worker_reported
                    and worker_reported != expected_echo),
                "phase": body["phase"],
                "phase_dir": body["phase_dir"],
                "tmux_session": body.get("tmux_session"),
                "tmux_window_id": body.get("tmux_window_id"),
                "spawn_failed": False,
                "spawn_failed_reason": None,
                "last_activity_at": last_activity_at,
                "last_activity_source": last_activity_source,
                "seconds_since_last_activity": int(seconds_since_last_activity),
                "awaiting_human": awaiting_human,
                "awaiting_human_reason": awaiting_human_reason,
                "child_processes": [],
                "child_pids": [],
            }
        alive = True
        try:
            os.kill(pid, 0)
        except (ProcessLookupError, PermissionError):
            alive = False
        # The spawn script's own watcher subtree is hidden: it is not
        # evidence of work (see _child_snapshot docstring / the 2026-08-11
        # oc-spike post-mortem).
        child_pids, child_cmds = _child_snapshot(
            pid if alive else None,
            exclude_subtree_of=str(phase_dir / "spawn.command"))
        return {
            "agent_id": agent_id,
            "pid": pid,
            "alive": alive,
            "completion_marker": marker_present,
            "agent_session_id": session_id,
                "worker_reported_session_id": worker_reported,
                "session_id_mismatch": bool(
                    expected_echo and worker_reported
                    and worker_reported != expected_echo),
            "phase": body["phase"],
            "phase_dir": body["phase_dir"],
            "tmux_session": body.get("tmux_session"),
            "tmux_window_id": body.get("tmux_window_id"),
            "spawn_failed": False,
            "spawn_failed_reason": None,
            "last_activity_at": last_activity_at,
            "last_activity_source": last_activity_source,
            "seconds_since_last_activity": int(seconds_since_last_activity),
            "awaiting_human": awaiting_human,
            "awaiting_human_reason": awaiting_human_reason,
            "child_processes": child_cmds,
            "child_pids": child_pids,
        }

    def wait(
        self,
        agent_id: str,
        *,
        max_seconds: float = 570.0,
        stall_after: float = 180.0,
        poll_interval: float = 5.0,
    ) -> dict:
        """Block until the worker reaches a terminal or degenerate state.

        Replaces the orchestrator's hand-rolled `status` poll loop with a single
        blocking call. Pure observation — never kills or writes anything. Probes
        immediately on entry, then every ``poll_interval`` seconds, and returns on
        the FIRST condition met:

          - ``completed``      — completion.yml present
          - ``awaiting_human`` — awaiting_human.yml present
          - ``died``           — pid known but not alive (no completion), or spawn_failed
          - ``stalled``        — alive, but no real progress for the last
                                 ``stall_after`` seconds (signals below)
          - ``timeout``        — ``max_seconds`` elapsed; alive and not stalled

        Progress signals, in order of authority:

        1. **Harness session store** (``session_last_activity``) — when the
           harness can report its session's last recorded progress, that
           timestamp IS the stall clock, supplemented only by child-process
           CHURN (the descendant command set changing between polls — a tool
           starting or finishing). Static children do NOT count: persistent
           MCP servers sit in the tree for the worker's whole life, and a
           mid-turn-dead TUI keeps repainting its spinner, so neither child
           presence nor worker.log mtime is evidence of progress (2026-08-11
           oc-spike run 4: an orchestrator whose model stream died mid-turn
           animated its log for two hours and read "working" throughout).
           Churn state persists across wait() calls in a phase-dir sidecar
           (``stall_probe.json``) because callers wait in slices; a
           first-ever wait grants one ``stall_after`` window of grace before
           a stale session can trip. Known tradeoff: a single silent
           long-running tool (no churn, no session writes) can still trip a
           false ``stalled``; callers' nudge ladders absorb that — a nudge
           lands as a no-op message and the next wait sees the session
           advance.

        2. **Legacy process signals** (session store unavailable) — child
           presence OR worker.log mtime advance, exactly the pre-2026-08-11
           behaviour, seeded from the log mtime (falling back to spawn
           ``started_at``) so pre-existing silence trips ``stalled`` on the
           first probes rather than ``stall_after`` later. See Issue #10.

        Only applies to marker-mode agents: an agent spawned with
        ``completion_marker=None`` can never reach ``completed``, so this raises
        rather than blocking to a misleading ``timeout``.
        """
        body = self._read(agent_id)
        if _marker_from_record(body) is None:
            raise RuntimeError(
                "agent was spawned in wait-for-exit mode "
                "(completion_marker=None); `wait` polls for a completion marker "
                "and cannot apply — poll `status` for liveness instead"
            )
        session_id: str | None = (body.get("agent_session_id")
                                  or body.get("claude_session_id"))
        start = time.monotonic()
        probe = self.probe(agent_id)
        phase_dir = Path(probe["phase_dir"])
        worker_log = phase_dir / "worker.log"

        def _log_mtime() -> float | None:
            try:
                return worker_log.stat().st_mtime
            except OSError:
                return None

        prev_mtime = _log_mtime()
        # Legacy stall clock, seeded from the worker.log mtime so pre-existing
        # silence counts toward the window; fall back to spawn time before any
        # log exists. Only consulted when the session store cannot answer.
        if prev_mtime is not None:
            legacy_last_working_at = prev_mtime
        else:
            legacy_last_working_at = _epoch_of(body["started_at"])
        # Child-process CHURN tracking for session-store mode: only a CHANGE
        # in the descendant pid set counts as movement — pids, not command
        # strings, so identical respawned workers (gradle daemons) still
        # register; a static set (persistent MCP servers) never does.
        #
        # Churn state persists in a phase-dir sidecar because callers wait in
        # SLICES (the runner re-enters every 300s): without continuity every
        # re-entry would re-baseline, and a long silent tool (stale session,
        # static children) would false-stall the moment a fresh slice began.
        # No sidecar = we have never observed this worker before, so absence
        # of churn evidence is not evidence of a stall: the floor is wait
        # entry, giving one full stall_after window to show movement.
        churn_file = phase_dir / "stall_probe.json"
        prev_children: frozenset[int] | None = None
        last_churn_at: float | None = None
        try:
            saved = json.loads(churn_file.read_text())
            prev_children = frozenset(int(p) for p in saved["child_pids"])
            last_churn_at = float(saved["last_churn_at"])
        except (OSError, ValueError, KeyError, TypeError):
            last_churn_at = time.time()  # entry grace — see above

        def _save_churn() -> None:
            tmp = churn_file.with_suffix(".json.part")
            try:
                tmp.write_text(json.dumps({
                    "child_pids": sorted(prev_children or ()),
                    "last_churn_at": last_churn_at,
                }))
                tmp.replace(churn_file)
            except OSError:
                pass  # forensics/continuity aid; never worth failing a poll

        while True:
            if probe["completion_marker"]:
                return self._wait_result("completed", agent_id, probe, start)
            if probe["awaiting_human"]:
                return self._wait_result("awaiting_human", agent_id, probe, start)
            if probe.get("spawn_failed"):
                return self._wait_result("died", agent_id, probe, start)
            if probe["pid"] is not None and not probe["alive"]:
                return self._wait_result("died", agent_id, probe, start)

            children = frozenset(probe.get("child_pids") or [])
            if children != (prev_children if prev_children is not None
                            else children):
                last_churn_at = time.time()
                prev_children = children
                _save_churn()
            elif prev_children is None:
                prev_children = children
                _save_churn()

            cur_mtime = _log_mtime()
            log_advanced = (
                cur_mtime is not None
                and (prev_mtime is None or cur_mtime > prev_mtime)
            )
            if children or log_advanced:
                legacy_last_working_at = time.time()
            if cur_mtime is not None:
                prev_mtime = cur_mtime

            # Authority order (see docstring): session store when it answers,
            # supplemented by child churn; legacy process signals otherwise.
            # Queried per-poll so a store that becomes readable mid-wait
            # upgrades the signal rather than staying on the liar.
            session_ts = (self.session_last_activity(session_id)
                          if session_id else None)
            if session_ts is not None:
                last_working_at = max(session_ts, last_churn_at or 0.0)
            else:
                last_working_at = legacy_last_working_at

            if probe["alive"] and (time.time() - last_working_at) >= stall_after:
                return self._wait_result("stalled", agent_id, probe, start)
            if (time.monotonic() - start) >= max_seconds:
                return self._wait_result("timeout", agent_id, probe, start)

            time.sleep(poll_interval)
            probe = self.probe(agent_id)

    @staticmethod
    def _wait_result(outcome: str, agent_id: str, probe: dict, start: float) -> dict:
        return {
            "outcome": outcome,
            "agent_id": agent_id,
            "waited_seconds": int(time.monotonic() - start),
            "evidence": probe,
        }

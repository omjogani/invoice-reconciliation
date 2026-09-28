"""Shared dataclasses for agentctl harnesses.

The previous `Harness` ABC was removed: only `ClaudeCodeHarness` ever implemented
it, and a second harness would reshape the abstraction with real-world signal
anyway. The two dataclasses below remain because they cross harness/cli/tests
boundaries. See Issue #45j.
"""
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path


@dataclass
class AgentConfig:
    working_dir: Path
    prompt: str
    phase: str
    run_dir: Path
    # Temp dir for orchestration files (prompt.txt, spawn.command, pid,
    # completion.yml, worker.log, agent_session_id).
    # Caller computes this via `flowstate.temp_layout.phase_temp_dir`
    # (or the `temp_dir` field on flowstate's node-config payload) and
    # passes it in — agentctl no longer reaches into flowstate to
    # compute it. See Issue #45b.
    temp_dir: Path
    run_descriptor: str
    repo_root: Path
    harness: str = "claude-code"
    model: str | None = None
    # Autonomy level, in agentctl's OWN vocabulary — never a harness flag
    # value. Callers say what they want ("full": run without permission
    # prompts); each harness translates under the hood (claude-code:
    # `--permission-mode bypassPermissions`; opencode: `--auto`) and MUST
    # raise loudly on a value it cannot honour — a silently dropped autonomy
    # setting cost a full dry run (2026-08-03). None keeps the harness's
    # own default (interactive permission prompts).
    autonomy: str | None = None
    # Script hooks (both optional; validated to exist before any side effect).
    # pre_spawn_script is SOURCED in the pane after cd-ing to working_dir and
    # before the harness launches — its exports reach the agent process; a
    # non-zero exit aborts the spawn (pane closes; teams supervision reports
    # the member dead with the cause in worker.log). post_kill_script runs in
    # the pane after the agent process has ended, before the pane closes —
    # best-effort: failure is echoed, never blocks teardown. Both run with
    # cwd = working_dir, composed at the harness-agnostic window-command
    # layer (TmuxTuiHarness.spawn), never inside the per-harness wrapper.
    pre_spawn_script: Path | None = None
    post_kill_script: Path | None = None
    # Completion marker (teams spec §5a). Path → the spawn wrapper polls for
    # this file and ends the harness process when it appears (flowstate's
    # one-shot worker contract; teams' `leave` mechanism). None → the wrapper
    # waits for the harness process to exit on its own and never kills.
    completion_marker: Path | None = None
    # macOS Terminal viewer for the run's tmux session (harness spawns only
    # open one when they CREATE the session). True for a human-watched
    # flowstate node; teams passes False for teammates — the lead is an
    # agent, so a popped-up Terminal per team is noise. The session is still
    # attachable on demand (`tmux attach -t <label>`).
    viewer: bool = True
    # tmux session label to land the worker's window in. When None the label
    # is derived from ``run_descriptor``; the orchestrator passes the parent
    # run's resolved label for child-run spawns so they share one session.
    session: str | None = None
    # Named tmux socket (``-L``) — tests only, so a disposable server can be
    # targeted. Production omits it and shares the user's default server.
    tmux_socket: str | None = None


@dataclass
class SpawnResult:
    agent_id: str
    session_id: str | None
    pid_file: Path
    phase_dir: Path
    started_at: str
    tmux_session: str
    tmux_window_id: str

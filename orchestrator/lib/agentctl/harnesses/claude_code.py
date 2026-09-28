"""Claude Code harness: the tmux-TUI machinery lives in
:mod:`agentctl.harnesses.tmux_tui`; this subclass supplies only what is
claude-specific — the launch line, the pre-assigned session id, the
transcript hook, and the autonomy translation table.

The helper re-exports at the bottom keep historical import paths
(`from agentctl.harnesses.claude_code import _window_title`) working.
"""
from __future__ import annotations

import shlex
import uuid
from pathlib import Path

from agentctl.harnesses.base import AgentConfig
from agentctl.harnesses.tmux_tui import (  # noqa: F401  (re-exported for callers/tests)
    SPAWN_GRACE_SECONDS,
    TmuxTuiHarness,
    _KILL_SIGTERM_TIMEOUT_S,
    _child_processes,
    _marker_from_record,
    _read_pid,
    _window_title,
)


class ClaudeCodeHarness(TmuxTuiHarness):
    name = "claude-code"

    # Autonomy vocabulary → claude-code CLI fragment (leading space included
    # so absence renders as nothing). Unsupported values fail loudly in the
    # base spawn(); see AgentConfig.autonomy.
    AUTONOMY_FLAGS: dict[str | None, str] = {
        None: "",
        "full": " --permission-mode bypassPermissions",
    }

    def _compose(
        self,
        config: AgentConfig,
        *,
        temp_dir: Path,
        prompt_file: Path,
        pid_file: Path,
        completion_marker: Path | None,
    ) -> tuple[str | None, dict, str]:
        # Pre-assign the claude session id so it's recorded in temp_dir before
        # claude even starts. This makes the session transcript locatable for
        # post-mortem even when the worker hangs and never writes completion.yml.
        agent_session_id = str(uuid.uuid4())
        (temp_dir / "agent_session_id").write_text(agent_session_id + "\n")

        # shlex.quote every path interpolated into the bash script so paths
        # with spaces / metacharacters can't break parsing or be abused.
        q_working_dir = shlex.quote(str(config.working_dir))
        q_prompt_file = shlex.quote(str(prompt_file))
        q_model = shlex.quote(config.model or "sonnet")
        q_session_id = shlex.quote(agent_session_id)
        q_repo_root = shlex.quote(str(config.repo_root))

        wait_block, kill_block = self._wait_and_kill_blocks(
            "claude_pid", completion_marker
        )

        # Autonomy translation (agentctl vocabulary → claude flag). "full"
        # maps to bypassPermissions; None keeps claude's own default (`auto`
        # mode). The flag must actually reach the command line: the auto-mode
        # classifier BLOCKS process-spawning commands and does not consult
        # permission *rules* — a spawned teams lead could not run `agentctl
        # team init` (it starts the watcher daemon) even with `Bash(*)`
        # allowed. Passing the mode through is the only lever. See the
        # 2026-08-03 dry run.
        q_autonomy = self.AUTONOMY_FLAGS[config.autonomy]

        script = (
            "#!/bin/bash\n"
            "set -uo pipefail\n"
            f"cd {q_working_dir}\n"
            # Mark this claude process as a flowstate worker so the SessionStart
            # hook can skip injecting the session-start protocol (which is for
            # human sessions only). See docs/factory-pipeline-e2e-issues.md #22.
            "export FLOWSTATE_WORKER=1\n"
            f"claude --model {q_model} --session-id {q_session_id}{q_autonomy} --setting-sources user,project,local < {q_prompt_file} &\n"
            "claude_pid=$!\n"
            + self._pid_write_snippet("claude_pid", pid_file)
            + wait_block
            + kill_block
        )
        return agent_session_id, {"agent_session_id": agent_session_id}, script

    # ---- uniform harness capabilities (2026-08-10) -----------------------

    def transcript_file(self, session_id: str, cache_dir: Path) -> Path | None:
        """claude writes its own transcript under ~/.claude/projects — there
        is nothing to export. ``cache_dir`` is part of the uniform signature
        and deliberately unused here."""
        from agentctl.transcripts import find_claude_transcript
        return find_claude_transcript(session_id)

    def session_last_activity(self, session_id: str) -> float | None:
        """mtime of the session's own JSONL: claude appends an event per
        message/tool result, so the transcript freezes the moment the
        session stops making progress — unlike the tmux pane capture, which
        a mid-turn-dead TUI keeps animating."""
        from agentctl.transcripts import find_claude_transcript
        path = find_claude_transcript(session_id)
        if path is None:
            return None
        try:
            return path.stat().st_mtime
        except OSError:
            return None

    def session_usage(self, session_id: str,
                      cache_dir: Path | None = None) -> dict | None:
        """None when the transcript is missing (the caller decides how
        serious that is); a parsed dict otherwise, even an all-zero one."""
        from agentctl.transcripts import find_claude_transcript, parse_claude_usage
        if find_claude_transcript(session_id) is None:
            return None
        return parse_claude_usage(session_id)

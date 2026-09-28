from __future__ import annotations

import argparse
from pathlib import Path

from agentctl.harnesses.base import AgentConfig
from agentctl.harnesses.factory import make_harness
from agentctl.harnesses.tmux_tui import TmuxTuiHarness
from agentctl.cli_util import emit as _emit, safe_handler
from agentctl.result import Result


def _harness(args: argparse.Namespace) -> TmuxTuiHarness:
    # Only `spawn` carries --harness; kill/send/status/wait are record-driven
    # (everything they need lives in the registry entry), so any harness
    # instance serves them.
    return make_harness(getattr(args, "harness", "claude-code"),
                        Path(args.registry_dir))


def _serialisable(d: dict) -> dict:
    """Recursively convert Path values to strings for YAML serialisation."""
    return {k: str(v) if isinstance(v, Path) else v for k, v in d.items()}


def cmd_spawn(args: argparse.Namespace) -> int:
    if args.completion_marker and args.completion_marker.strip().lower() == "none":
        marker = None
    elif args.completion_marker:
        marker = Path(args.completion_marker).resolve()
    else:
        # Back-compat default: unchanged behaviour for the graph
        # orchestrator's existing invocations (spec §5a).
        marker = Path(args.temp_dir).resolve() / "completion.yml"
    cfg = AgentConfig(
        working_dir=Path(args.working_dir).resolve(),
        prompt=args.prompt,
        phase=args.phase,
        run_dir=Path(args.run_dir).resolve(),
        temp_dir=Path(args.temp_dir).resolve(),
        run_descriptor=args.run_descriptor,
        repo_root=Path(args.repo_root).resolve(),
        model=args.model,
        autonomy=args.autonomy,
        session=args.session,
        completion_marker=marker,
        pre_spawn_script=Path(args.pre_spawn_script).resolve() if args.pre_spawn_script else None,
        post_kill_script=Path(args.post_kill_script).resolve() if args.post_kill_script else None,
    )
    h = _harness(args)
    result = h.spawn(cfg)
    payload = {
        "agent_id": result.agent_id,
        "pid_file": str(result.pid_file),
        "temp_dir": str(result.phase_dir),
        "started_at": result.started_at,
        "tmux_session": result.tmux_session,
        "tmux_window_id": result.tmux_window_id,
        "attach_hint": f"tmux attach -t {result.tmux_session}",
    }
    return _emit(Result.ok(payload=payload))


def cmd_kill(args: argparse.Namespace) -> int:
    h = _harness(args)
    h.kill(args.agent_id)
    return _emit(Result.ok(payload={"agent_id": args.agent_id, "killed": True}))


def cmd_send(args: argparse.Namespace) -> int:
    h = _harness(args)
    h.send(args.agent_id, args.text, interrupt=args.interrupt)
    return _emit(Result.ok(payload={
        "agent_id": args.agent_id,
        "sent": True,
        "interrupt": args.interrupt,
    }))


def cmd_status(args: argparse.Namespace) -> int:
    h = _harness(args)
    info = h.status(args.agent_id)
    return _emit(Result.ok(payload=info))


def cmd_wait(args: argparse.Namespace) -> int:
    h = _harness(args)
    info = h.wait(
        args.agent_id,
        max_seconds=args.max_seconds,
        stall_after=args.stall_after,
        poll_interval=args.poll_interval,
    )
    return _emit(Result.ok(payload=info))


def _agentctl_default_suggestion(args: argparse.Namespace, exc: Exception) -> str:
    """agentctl-specific default remediation hint, used when a per-command
    suggestion_fn is not provided. See Issue #31."""
    return (
        "Inspect the agentctl registry for this agent_id "
        "(~/.agentctl/<agent_id>.yml) and the per-phase temp dir's "
        "worker.log / spawn_failed.yml for ground truth."
    )


def _safe(intent_fn, handler, suggestion_fn=None):
    """agentctl-flavoured ``safe_handler`` with the agentctl-specific
    default suggestion. Thin wrapper over ``agentctl.cli_util.safe_handler``
    so call sites in this file stay short.
    """
    return safe_handler(intent_fn, handler, suggestion_fn or _agentctl_default_suggestion)


def main(argv: list[str]) -> int:
    p = argparse.ArgumentParser(prog="agentctl")
    p.add_argument("--registry-dir", default=str(Path.home() / ".agentctl"))
    sub = p.add_subparsers(dest="cmd", required=True)

    s = sub.add_parser("spawn")
    s.add_argument("--harness", default="claude-code")
    s.add_argument("--working-dir", required=True)
    s.add_argument("--phase", required=True)
    s.add_argument("--prompt", required=True)
    s.add_argument("--run-dir", required=True)
    s.add_argument(
        "--temp-dir", required=True,
        help=(
            "Absolute path to the per-node orchestration temp dir "
            "(prompt.txt, spawn.command, pid, completion.yml, worker.log "
            "land here). The caller computes this; typically read from "
            "the `temp_dir` field of flowstate's node-config payload. "
            "See Issue #45b."
        ),
    )
    s.add_argument("--run-descriptor", required=True)
    s.add_argument("--repo-root", required=True)
    s.add_argument("--model", default=None)
    s.add_argument(
        "--autonomy", default=None, choices=["full"],
        help=(
            "Autonomy level in agentctl's own vocabulary; each harness "
            "translates it to its native flag (claude-code: --permission-mode "
            "bypassPermissions; opencode: --auto). 'full' = run without "
            "permission prompts. Omit to keep the harness default."
        ),
    )
    s.add_argument(
        "--completion-marker", default=None,
        help=(
            "Completion-marker file the wrapper polls; the worker is ended "
            "when it appears. Omit for the default <temp-dir>/completion.yml; "
            "pass a path to relocate it; pass the literal 'none' for "
            "wait-for-exit mode (persistent worker, never killed)."
        ),
    )
    s.add_argument(
        "--pre-spawn-script", default=None,
        help=(
            "Script SOURCED in the pane (cwd=working-dir) before the harness "
            "launches; its exports reach the agent; non-zero exit aborts the "
            "spawn."
        ),
    )
    s.add_argument(
        "--post-kill-script", default=None,
        help=(
            "Script run in the pane after the agent session ends, before the "
            "pane closes; best-effort (failure is echoed, never blocks "
            "teardown)."
        ),
    )
    s.add_argument(
        "--session", default=None,
        help=(
            "tmux session label to land this worker's window in. Omit for a "
            "top-level run (label derived from --run-descriptor). For child-run "
            "spawns, pass the parent run's resolved `tmux_session` (from the "
            "first spawn envelope) so all workers share one session."
        ),
    )

    s = sub.add_parser("kill")
    s.add_argument("agent_id")

    s = sub.add_parser("send")
    s.add_argument("agent_id")
    s.add_argument("text")
    s.add_argument(
        "--interrupt", action="store_true",
        help=(
            "Send Escape first (end the current turn), then the text. Use to "
            "stop-and-redirect a worker mid-generation without kill+respawn."
        ),
    )

    s = sub.add_parser("status")
    s.add_argument("agent_id")

    s = sub.add_parser("wait")
    s.add_argument("agent_id")
    s.add_argument("--max-seconds", type=float, default=570.0)
    s.add_argument("--stall-after", type=float, default=180.0)
    s.add_argument("--poll-interval", type=float, default=5.0)


    args = p.parse_args(argv)
    handlers = {
        "spawn": _safe(
            lambda a: f"Spawn agent for phase {a.phase!r}",
            cmd_spawn,
            lambda a, exc: (
                "Check that --working-dir exists and is a git repo, --prompt is "
                "non-empty, and tmux is installed (brew install tmux / apt "
                "install tmux). Look in the per-phase temp dir's spawn.command "
                "and worker.log for diagnostics."
            ),
        ),
        "kill": _safe(
            lambda a: f"Kill agent {a.agent_id}",
            cmd_kill,
            lambda a, exc: (
                "Verify the agent_id exists in ~/.agentctl/. If you need to "
                "gate the kill on node status, call `flowstate kill-check "
                "--run-dir <dir> --node <name>` first."
            ),
        ),
        "send": _safe(
            lambda a: f"Send text to agent {a.agent_id}",
            cmd_send,
            lambda a, exc: (
                "Plain text is queued by Claude Code if the worker is mid-"
                "generation; pass --interrupt to send Escape first and end the "
                "current turn. If the send failed, check the agent's window "
                "still exists via `agentctl status <agent_id>`."
            ),
        ),
        "status": _safe(
            lambda a: f"Status of agent {a.agent_id}",
            cmd_status,
            lambda a, exc: (
                "Confirm the agent_id in ~/.agentctl/ matches a spawn from this "
                "machine. A stale registry record from a different machine will "
                "report `alive: false`."
            ),
        ),
        "wait": _safe(
            lambda a: f"Wait on agent {a.agent_id}",
            cmd_wait,
            lambda a, exc: (
                "A `stalled` or `died` outcome should be followed by transcript "
                "forensics: read the worker's session transcript at "
                "~/.claude/projects/*/<agent_session_id>.jsonl (the "
                "agent_session_id is in the wait evidence payload) to see what "
                "the worker was doing when it went quiet."
            ),
        ),
    }
    return handlers[args.cmd](args)

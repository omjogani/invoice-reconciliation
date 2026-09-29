"""spawn-node: spawn the worker for one agent node in a single, checked step.

Replaces the hand-assembled `node-config` + `render-prompt` + `agentctl spawn`
sequence in the orchestrator skill. Why it exists: during exploration an
orchestrator extracted the rendered prompt with a system python that lacked
PyYAML, the extraction failed silently, and a worker was spawned with an
empty prompt. Here the envelopes are parsed with the venv interpreter, an
empty or unrendered prompt is refused, and every spawn is appended to
``<run-dir>/spawn-ledger.jsonl`` so worker ids survive a lost session.

Usage::

    orchestrator/bin/spawn-node <node> --run-dir <dir> [--session <tmux>]
                                [--prompt-file <file>] [--run-descriptor <name>]

``--prompt-file`` respawns from a saved prompt (use the node's
``prompt.raw.txt``, never the substituted ``prompt.txt``).

``--retry-feedback <file>`` is the validation-failure retry. The worker that
wrote a rejected output is already gone (the wrapper ends a worker as soon as
its completion.yml appears), so the retry is a fresh worker: the node's
prompt with a "previous attempt was rejected" section appended. The rejected
completion.yml is moved aside as ``completion.attempt-<n>.yml``; left in
place it would make the wrapper end the new worker immediately.

Output is the agentctl envelope, unchanged, so the orchestrator reads it as
before.
"""
from __future__ import annotations

import argparse
import datetime
import json
import subprocess
import sys
from pathlib import Path

import yaml

ORCH_ROOT = Path(__file__).resolve().parents[1]
FLOWSTATE = ORCH_ROOT / "bin" / "flowstate"
AGENTCTL = ORCH_ROOT / "bin" / "agentctl"


def _envelope(cmd: list[str]) -> dict:
    proc = subprocess.run(cmd, capture_output=True, text=True)
    try:
        env = yaml.safe_load(proc.stdout) or {}
    except yaml.YAMLError as exc:
        raise SystemExit(f"spawn-node: could not parse output of {' '.join(cmd[:3])}: {exc}\n{proc.stdout}")
    if env.get("status") != "ok":
        raise SystemExit(f"spawn-node: {' '.join(cmd[1:3])} failed:\n{proc.stdout}{proc.stderr}")
    return env["payload"]


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="spawn-node")
    ap.add_argument("node")
    ap.add_argument("--run-dir", required=True)
    ap.add_argument("--run-descriptor", help="defaults to the run's own descriptor")
    ap.add_argument("--session", help="tmux session to join (pass the parent's for child-run workers)")
    ap.add_argument("--prompt-file", help="respawn from this prompt instead of re-rendering")
    ap.add_argument("--retry-feedback", help="file with validation feedback for a retry of this node")
    ap.add_argument("--repo-root", default=None)
    args = ap.parse_args(argv)

    run_dir = str(Path(args.run_dir).resolve())
    config = _envelope([str(FLOWSTATE), "node-config", args.node, "--run-dir", run_dir])
    if config.get("runner") not in (None, "agent"):
        raise SystemExit(f"spawn-node: {args.node} is a {config.get('runner')} node, not an agent node")
    if args.prompt_file:
        prompt = Path(args.prompt_file).read_text(encoding="utf-8")
    else:
        rendered = _envelope([str(FLOWSTATE), "render-prompt", args.node, "--run-dir", run_dir])
        prompt = rendered.get("rendered_prompt") or rendered.get("prompt") or ""
    if not prompt.strip():
        raise SystemExit(f"spawn-node: refusing to spawn {args.node}: the prompt is empty")
    temp_dir = Path(config["temp_dir"])
    if args.retry_feedback:
        feedback = Path(args.retry_feedback).read_text(encoding="utf-8").strip()
        prompt = prompt.rstrip() + (
            "\n\n## Previous attempt was rejected\n\n"
            "An earlier worker for this node wrote output that failed validation. Fix every problem below. "
            "Rewrite the output file(s) completely and write a fresh completion.yml.\n\n" + feedback + "\n")
    stale = temp_dir / "completion.yml"
    if stale.exists():
        if not args.retry_feedback and not args.prompt_file:
            raise SystemExit(f"spawn-node: {stale} exists from an earlier attempt; pass --retry-feedback or "
                             f"--prompt-file to respawn deliberately")
        attempt = 1
        while (temp_dir / f"completion.attempt-{attempt}.yml").exists():
            attempt += 1
        stale.rename(temp_dir / f"completion.attempt-{attempt}.yml")

    variables = _envelope([str(FLOWSTATE), "vars", "--run-dir", run_dir])
    descriptor = args.run_descriptor or variables.get("_run_descriptor")
    repo_root = args.repo_root or subprocess.run(["git", "rev-parse", "--show-toplevel"], capture_output=True,
                                                 text=True, check=True).stdout.strip()
    cmd = [str(AGENTCTL), "spawn", "--harness", config.get("harness") or "claude-code",
           "--working-dir", config["working_dir"], "--phase", args.node, "--prompt", prompt,
           "--run-dir", run_dir, "--temp-dir", config["temp_dir"], "--run-descriptor", descriptor,
           "--repo-root", repo_root]
    if config.get("model"):
        cmd += ["--model", config["model"]]
    if config.get("autonomy"):
        cmd += ["--autonomy", config["autonomy"]]
    if args.session:
        cmd += ["--session", args.session]
    proc = subprocess.run(cmd, capture_output=True, text=True)
    sys.stdout.write(proc.stdout)
    sys.stderr.write(proc.stderr)
    try:
        env = yaml.safe_load(proc.stdout) or {}
    except yaml.YAMLError:
        env = {}
    if env.get("status") == "ok":
        payload = env["payload"]
        entry = {"at": datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
                 "node": args.node, "agent_id": payload.get("agent_id"),
                 "tmux_session": payload.get("tmux_session"), "temp_dir": config["temp_dir"],
                 "respawn_from": args.prompt_file, "retry_feedback": args.retry_feedback}
        with open(Path(run_dir) / "spawn-ledger.jsonl", "a", encoding="utf-8") as fh:
            fh.write(json.dumps(entry, sort_keys=True) + "\n")
    return proc.returncode


if __name__ == "__main__":
    sys.exit(main())

"""Claude-code transcript location + usage parsing (harness-owned).

Moved from flowstate/transcripts.py (2026-08-10 harness-session-identity
plan): harness classes need transcript/usage retrieval and agentctl must
never import flowstate. flowstate/transcripts.py remains a thin adapter.

Canonical usage-dict keys, shared by every harness's `session_usage`:

    input_tokens, output_tokens, cache_read_input_tokens,
    cache_creation_input_tokens, reasoning_tokens, turns, model
"""
from __future__ import annotations

import json
from pathlib import Path


def zero_usage() -> dict:
    return {
        "input_tokens": 0, "output_tokens": 0,
        "cache_read_input_tokens": 0, "cache_creation_input_tokens": 0,
        "reasoning_tokens": 0, "turns": 0, "model": None,
    }


def _claude_projects_root() -> Path:
    return Path.home() / ".claude" / "projects"


def _safe_int(v) -> int:
    """Coerce a transcript usage field to int, 0 on any failure. Transcript
    shapes drift (string vs int, nested, null); parsing is best-effort and
    must not abort on one malformed field."""
    if v is None:
        return 0
    try:
        return int(v)
    except (TypeError, ValueError):
        return 0


def find_claude_transcript(
    session_id: str, projects_root: Path | None = None
) -> Path | None:
    """Transcript jsonl path for a session id, or None.

    Searches every project subdir under ~/.claude/projects/. Session ids are
    UUIDs so collisions across projects are vanishingly improbable.
    """
    root = projects_root or _claude_projects_root()
    if not root.exists():
        return None
    for proj_dir in root.iterdir():
        if not proj_dir.is_dir():
            continue
        candidate = proj_dir / f"{session_id}.jsonl"
        if candidate.exists():
            return candidate
    return None


def parse_claude_usage(
    session_id: str, projects_root: Path | None = None
) -> dict:
    """Sum usage across all assistant turns of one claude session.

    Dedupes by requestId — the transcript writes one line per content block
    (thinking/text/tool-use), but they share a requestId and the usage block
    represents the single billable API call. Zero-fills when the transcript
    is missing (best-effort; callers decide whether absence is an error).
    """
    out = zero_usage()
    transcript = find_claude_transcript(session_id, projects_root)
    if transcript is None:
        return out
    seen: set[str] = set()
    for raw in transcript.read_text().splitlines():
        if not raw.strip():
            continue
        try:
            obj = json.loads(raw)
        except json.JSONDecodeError:
            continue
        req_id = obj.get("requestId")
        if not req_id or req_id in seen:
            continue
        message = obj.get("message") or {}
        usage = message.get("usage") or {}
        if not usage:
            continue
        seen.add(req_id)
        out["input_tokens"] += _safe_int(usage.get("input_tokens"))
        out["output_tokens"] += _safe_int(usage.get("output_tokens"))
        out["cache_read_input_tokens"] += _safe_int(
            usage.get("cache_read_input_tokens"))
        out["cache_creation_input_tokens"] += _safe_int(
            usage.get("cache_creation_input_tokens"))
        if message.get("model"):
            out["model"] = message["model"]
    out["turns"] = len(seen)
    return out

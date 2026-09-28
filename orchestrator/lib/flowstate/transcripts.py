"""Flowstate-facing transcript API — a thin adapter over agentctl.transcripts.

The parsing itself moved down to `agentctl/transcripts.py` (2026-08-10
harness-session-identity plan) so harness classes can use it; agentctl must
never import flowstate, so the shared implementation has to live below.

Dollar estimation was REMOVED in the same change: the per-model rate tables
went stale faster than anyone updated them, the "assume opus" fallback
silently mispriced every unrecognised model, and no consumer could tell an
estimate from a measurement. Token counts are the durable number — see
`RunState.total_tokens()`.
"""
from __future__ import annotations

from pathlib import Path

from agentctl.transcripts import find_claude_transcript, parse_claude_usage
from flowstate.state import Usage

# Back-compat alias — TranscriptUsage was the old name before the persistence-
# shared `Usage` dataclass landed (Issue #45h). Existing callers and tests
# still refer to it; keep the alias so we don't churn unrelated code paths.
TranscriptUsage = Usage


def find_transcript(session_id: str, projects_root: Path | None = None) -> Path | None:
    """Adapter over agentctl.transcripts (parsing moved there 2026-08-10 so
    harness classes can use it without importing flowstate)."""
    return find_claude_transcript(session_id, projects_root)


def parse_session_usage(
    session_id: str, projects_root: Path | None = None
) -> TranscriptUsage:
    """Claude-session usage as flowstate's `Usage`.

    Semantics unchanged from the pre-move implementation: a zero-filled
    Usage when the transcript is missing, requestId-deduped sums otherwise.
    """
    d = parse_claude_usage(session_id, projects_root)
    return TranscriptUsage(
        input_tokens=d["input_tokens"],
        output_tokens=d["output_tokens"],
        cache_read_input_tokens=d["cache_read_input_tokens"],
        cache_creation_input_tokens=d["cache_creation_input_tokens"],
        reasoning_tokens=d["reasoning_tokens"],
        turns=d["turns"],
        model=d["model"],
    )

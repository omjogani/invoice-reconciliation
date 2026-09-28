"""Harness registry: name → class. The single place a new harness plugs in."""
from __future__ import annotations

from pathlib import Path

from agentctl.harnesses.claude_code import ClaudeCodeHarness
from agentctl.harnesses.tmux_tui import TmuxTuiHarness

HARNESSES: dict[str, type[TmuxTuiHarness]] = {
    ClaudeCodeHarness.name: ClaudeCodeHarness,
}


def make_harness(name: str, registry_dir: Path) -> TmuxTuiHarness:
    try:
        cls = HARNESSES[name]
    except KeyError:
        raise ValueError(
            f"unknown harness {name!r} (available: {sorted(HARNESSES)})"
        ) from None
    return cls(registry_dir=Path(registry_dir))

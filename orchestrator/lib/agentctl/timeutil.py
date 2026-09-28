"""Timestamp minting for agentctl.

Deliberately mirrors flowstate.time.iso_now — do NOT replace with an import;
agentctl imports nothing from flowstate (spec §2, enforced by
tests/agentctl/test_envelope_contract.py::test_no_flowstate_imports_in_agentctl).
"""
from __future__ import annotations

from datetime import datetime, timezone


def now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")

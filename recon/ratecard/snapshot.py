"""Human-approved rate-card snapshots, keyed by carrier and contract SHA-256.

A snapshot is created only by an explicit approval of a card that a recorded
run produced. A changed contract has a new hash, so it has no snapshot and
the next run pauses for approval rather than reusing an old reading.
"""
from __future__ import annotations

from pathlib import Path
from typing import Any

from recon.model import read_json, write_json
from recon.ratecard import compare


class SnapshotStore:
    def __init__(self, root: Path | str):
        self.root = Path(root)

    def path_for(self, carrier: str, contract_sha256: str) -> Path:
        return self.root / carrier / f"{contract_sha256}.json"

    def load(self, carrier: str, contract_sha256: str) -> dict | None:
        path = self.path_for(carrier, contract_sha256)
        return read_json(path) if path.exists() else None

    def compare(self, card: dict) -> tuple[str, dict | None, list[str]]:
        """Return (status, snapshot card, differences); status is match, differs or missing."""
        entry = self.load(card["carrier"], card["contract_sha256"])
        if entry is None:
            return "missing", None, []
        differences = compare.diff(entry["card"], card)
        return ("match" if not differences else "differs"), entry["card"], differences

    def approve(self, card: dict, *, approved_by: str, approved_at: str, source_run: str,
                replace: bool = False) -> Path:
        from recon.ratecard import strip_metadata

        card = strip_metadata(card)
        path = self.path_for(card["carrier"], card["contract_sha256"])
        if path.exists() and not replace:
            raise FileExistsError(f"{path} already exists; pass replace=True to supersede it")
        entry: dict[str, Any] = {
            "approval": {"approved_by": approved_by, "approved_at": approved_at, "source_run": source_run},
            "card": card,
        }
        write_json(path, entry)
        return path

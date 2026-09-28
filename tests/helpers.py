"""Shared test helpers: fixture rate cards bound to the real contracts."""
from __future__ import annotations

import copy
import json
from pathlib import Path

from recon.ratecard.contract import Contract, load_contract

ROOT = Path(__file__).resolve().parents[1]
CONTRACTS = {
    "alpine": ROOT / "data" / "contracts" / "alpine-express.md",
    "falcon": ROOT / "data" / "contracts" / "falcon-freight.md",
    "sagar": ROOT / "data" / "contracts" / "sagar-roadlines.md",
}


def contract(carrier: str) -> Contract:
    return load_contract(CONTRACTS[carrier])


def fixture_card(carrier: str) -> dict:
    """A test-only card with its contract_sha256 set to the real contract's hash."""
    card = json.loads((ROOT / "tests" / "fixtures" / "ratecards" / f"{carrier}.json").read_text())
    card["contract_sha256"] = contract(carrier).sha256
    return copy.deepcopy(card)


def fixture_cards() -> dict[str, dict]:
    return {c: fixture_card(c) for c in CONTRACTS}

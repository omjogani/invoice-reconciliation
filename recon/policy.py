"""Finding codes and the disposition policy."""
from __future__ import annotations

import json
from functools import lru_cache
from pathlib import Path
from typing import Any, Iterable

RANK = {"accept": 0, "dispute": 1, "escalate": 2}


@lru_cache(maxsize=1)
def codes() -> dict[str, dict[str, Any]]:
    return json.loads(Path(__file__).with_name("policy.json").read_text(encoding="utf-8"))["codes"]


def finding(code: str, detail: str, *, amount=None, clause: str | None = None, **extra: Any) -> dict:
    if code not in codes():
        raise KeyError(f"unknown finding code {code!r}")
    f = {"code": code, "detail": detail, "amount": amount, "clause": clause, "resolved_by": None}
    f.update(extra)
    return f


def disposition_for(code: str) -> str:
    return codes()[code]["disposition"]


def combine(findings: Iterable[dict]) -> str:
    """Most severe disposition among unresolved findings; accept when there are none."""
    worst = "accept"
    for f in findings:
        if f.get("resolved_by"):
            continue
        d = disposition_for(f["code"])
        if RANK[d] > RANK[worst]:
            worst = d
    return worst


def is_monetary(code: str) -> bool:
    return bool(codes()[code]["monetary"])

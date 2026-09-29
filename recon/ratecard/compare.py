"""Semantic comparison of rate cards.

Two cards are the same contract reading when they price everything the same
way. Wording is ignored: quotes, labels, clause citations and the lists that
only exist for coverage (non_pricing, unsupported). Numbers are compared as
decimals (``"18"`` equals ``"18.00"``), and lists whose order carries no
meaning are sorted. Surcharge order is kept: it changes the arithmetic.
"""
from __future__ import annotations

import json
import re
from decimal import Decimal
from typing import Any

_IGNORED_KEYS = {"quote", "label", "summary", "why", "clause", "non_pricing", "unsupported", "contract_file"}
_DECIMAL = re.compile(r"^-?\d+(\.\d+)?$")


def _canon(value: Any) -> Any:
    if isinstance(value, dict):
        return {k: _canon(v) for k, v in sorted(value.items())
                if k not in _IGNORED_KEYS and not k.startswith("_")}
    if isinstance(value, list):
        return [_canon(v) for v in value]
    if isinstance(value, str) and _DECIMAL.match(value):
        return format(Decimal(value).normalize(), "f")
    return value


def _key(value: Any) -> str:
    return json.dumps(value, sort_keys=True)


def canonical(card: dict) -> dict:
    c = _canon(card)
    for comp in c["freight"]["components"]:
        for band in comp["bands"]:
            # An open edge has nothing to include or exclude; its flag carries no meaning.
            if band["min_kg"] is None:
                band["min_inclusive"] = True
            if band["max_kg"] is None:
                band["max_inclusive"] = True
        comp["bands"] = sorted(comp["bands"], key=lambda b: (b["min_kg"] is not None,
                                                             Decimal(b["min_kg"] or 0), b["min_inclusive"]))
    c["accessorials"] = sorted(c["accessorials"], key=_key)
    c["invoice_discounts"] = sorted(c["invoice_discounts"], key=_key)
    c["service_levels"]["allowed"] = sorted(c["service_levels"]["allowed"])
    return c


def diff(a: dict, b: dict) -> list[str]:
    """Human-readable differences between two cards' canonical forms."""
    out: list[str] = []
    _diff(canonical(a), canonical(b), "", out)
    return out


def _diff(a: Any, b: Any, path: str, out: list[str]) -> None:
    if isinstance(a, dict) and isinstance(b, dict):
        for k in sorted(set(a) | set(b)):
            if k not in a:
                out.append(f"{path}/{k}: only in second: {json.dumps(b[k], sort_keys=True)}")
            elif k not in b:
                out.append(f"{path}/{k}: only in first: {json.dumps(a[k], sort_keys=True)}")
            else:
                _diff(a[k], b[k], f"{path}/{k}", out)
    elif isinstance(a, list) and isinstance(b, list):
        if len(a) != len(b):
            out.append(f"{path}: {len(a)} entries vs {len(b)}: "
                       f"{json.dumps(a, sort_keys=True)} vs {json.dumps(b, sort_keys=True)}")
            return
        for i, (x, y) in enumerate(zip(a, b)):
            _diff(x, y, f"{path}/{i}", out)
    elif a != b:
        out.append(f"{path}: {json.dumps(a)} vs {json.dumps(b)}")


def same(a: dict, b: dict) -> bool:
    return canonical(a) == canonical(b)

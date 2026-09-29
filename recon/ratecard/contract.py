"""Reading a prose contract: fingerprint, numbered clauses, normalised text.

Clauses are the numbered paragraphs (``7. Rate revisions require ...``). A
clause runs until the next numbered paragraph or the next markdown heading.
"""
from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass
from decimal import Decimal
from pathlib import Path

_CLAUSE_START = re.compile(r"^(\d+)\.\s+")
_HEADING = re.compile(r"^#{1,6}\s")
_EMPHASIS = re.compile(r"\*\*|__|`")
_NUMBER = re.compile(r"(?<![\d.])\d{1,3}(?:,\d{2,3})+(?:\.\d+)?|(?<![\d.])\d+(?:\.\d+)?")
_ELLIPSIS = re.compile(r"\s*(?:\.\.\.|…)\s*")
_QUOTES = str.maketrans({"‘": "'", "’": "'", "“": '"', "”": '"', "–": "-"})


def normalise(text: str) -> str:
    """Remove markdown emphasis, straighten quotes, collapse whitespace."""
    text = _EMPHASIS.sub("", text).translate(_QUOTES)
    return " ".join(text.split())


def numbers_in(text: str) -> set[Decimal]:
    """Every number written in ``text``: ``2,000`` → 2000, ``₹18.00`` → 18.00."""
    return {Decimal(m.group(0).replace(",", "")) for m in _NUMBER.finditer(normalise(text))}


def quote_segments(quote: str) -> list[str]:
    """A quote may join contiguous excerpts with ``...``; each must appear in order."""
    return [s for s in (normalise(p) for p in _ELLIPSIS.split(quote)) if s]


def contains_quote(text: str, quote: str) -> bool:
    hay = normalise(text)
    at = 0
    for segment in quote_segments(quote):
        found = hay.find(segment, at)
        if found < 0:
            return False
        at = found + len(segment)
    return bool(quote_segments(quote))


@dataclass(frozen=True)
class Contract:
    file_name: str
    sha256: str
    text: str
    clauses: dict[int, str]

    def clause_text(self, numbers: list[int]) -> str:
        return "\n".join(self.clauses[n] for n in numbers if n in self.clauses)


def parse_clause_refs(ref: str) -> list[int]:
    """``"§2, §3"`` → ``[2, 3]``."""
    return [int(n) for n in re.findall(r"§(\d+)", ref)]


def load_contract(path: Path | str) -> Contract:
    path = Path(path)
    raw = path.read_bytes()
    text = raw.decode("utf-8")
    clauses: dict[int, list[str]] = {}
    current: int | None = None
    for line in text.splitlines():
        if _HEADING.match(line):
            current = None
            continue
        m = _CLAUSE_START.match(line)
        if m:
            current = int(m.group(1))
            if current in clauses:
                raise ValueError(f"{path.name}: clause {current} appears twice")
            clauses[current] = [line[m.end():]]
            continue
        if current is not None:
            clauses[current].append(line)
    return Contract(
        file_name=path.name,
        sha256=hashlib.sha256(raw).hexdigest(),
        text=text,
        clauses={n: "\n".join(lines).strip() for n, lines in sorted(clauses.items())},
    )

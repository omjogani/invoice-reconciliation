"""Billing-document parsers.

Each parser module exposes::

    FORMAT: str                         format id recorded on the document
    detect(path, text) -> bool          cheap, side-effect-free recognition
    parse(path, text, sha256) -> (document, [lines])

Adding a carrier format means adding a module here and registering it in
PARSERS. Anything no parser recognises is reported as unknown and routed to
the agent fallback by the flow; it is never skipped.
"""
from __future__ import annotations

from pathlib import Path
from typing import Callable

from recon.parsers import alpine_json, falcon_text, sagar_csv


class ParseError(ValueError):
    """A document looked like a known format but did not parse cleanly."""


PARSERS = (falcon_text, alpine_json, sagar_csv)


def detect(path: Path, text: str):
    """Return the parser module for this document, or None if unknown."""
    matches = [p for p in PARSERS if p.detect(path, text)]
    if len(matches) > 1:
        raise ParseError(f"{path.name}: matched several formats {[m.FORMAT for m in matches]}")
    return matches[0] if matches else None


ParseFn = Callable[[Path, str, str], tuple[dict, list[dict]]]

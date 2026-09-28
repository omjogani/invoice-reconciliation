"""Reducer stdout parser.

Reducer scripts emit `FLOWSTATE_OUTPUT_<var>=<value>` lines on stdout. After
the script exits zero, the runtime parses all such lines and writes the
parsed dict to state atomically. JSON-decodable values are auto-decoded; if
the value can't be parsed as JSON it's preserved as a literal string.

Non-output lines are ignored - scripts may log freely on stdout.
"""

import json
from typing import Any


class ReducerParseError(ValueError):
    """Raised when reducer stdout contains malformed FLOWSTATE_OUTPUT_* lines."""


_PREFIX = "FLOWSTATE_OUTPUT_"


def parse_reducer_stdout(stdout: str) -> dict[str, Any]:
    """Parse a reducer's stdout into a {var: value} dict.

    Lines not starting with FLOWSTATE_OUTPUT_ are ignored. Duplicate keys
    raise ReducerParseError. JSON-decodable values are decoded; otherwise
    the literal string is preserved.
    """
    out: dict[str, Any] = {}
    for raw_line in stdout.splitlines():
        line = raw_line.rstrip("\r")
        if not line.startswith(_PREFIX):
            continue
        rest = line[len(_PREFIX):]
        if "=" not in rest:
            raise ReducerParseError(f"malformed output line (no '='): {line!r}")
        key, value = rest.split("=", 1)
        if not key:
            raise ReducerParseError(f"empty output key in line: {line!r}")
        if key in out:
            raise ReducerParseError(f"duplicate output key {key!r}")
        try:
            out[key] = json.loads(value)
        except (json.JSONDecodeError, ValueError):
            out[key] = value
    return out

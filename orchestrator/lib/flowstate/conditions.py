"""Pure-Python condition evaluator for graph-edge conditions.

`traversal.advance()` calls this to auto-resolve fan-outs whose conditions can
be cleanly evaluated against `state.variables`. When exactly one outgoing edge
satisfies its condition, `advance` returns `kind=moved` directly — saving the
orchestrator an LLM round on every fan-out. See Issue #7b.

Supported grammar (intentionally minimal):
    <condition>  ::= <or_clause>
    <or_clause>  ::= <atom> ('||' <atom>)*
    <atom>       ::= <var> <op> <literal>
    <op>         ::= '==' | '!=' | '<=' | '>=' | '<' | '>'
    <var>        ::= bare identifier (matches a key in state.variables)
    <literal>    ::= bare token (no quotes; whitespace-stripped on both sides)

Anything else (e.g., `&&`, function calls, parentheses, nested expressions)
returns `None` — meaning "I can't evaluate this; fall back to the orchestrator".

`None` is also returned for:
    - referencing an undefined variable (the orchestrator might have just-in-time
      set-var logic the evaluator doesn't know about)
    - numeric comparisons (`<`, `<=`, `>`, `>=`) where the variable or literal
      can't be coerced to int

Empty / None condition returns True (unconditional edges always pass).
"""
from __future__ import annotations

import re
from typing import Mapping


_OPERATORS = ("==", "!=", "<=", ">=", "<", ">")
_NUMERIC_OPS = ("<=", ">=", "<", ">")


def evaluate(condition: str | None, variables: Mapping[str, object]) -> bool | None:
    """Evaluate `condition` against `variables`. See module docstring for grammar.

    Returns True, False, or None (unsupported / unresolvable).
    """
    if condition is None or not condition.strip():
        return True

    # Reject any grammar we don't support.
    if "&&" in condition or "(" in condition or ")" in condition:
        return None

    # OR clauses. If any clause evaluates True, return True. If all clauses
    # return False, return False. If any clause returns None (unsupported),
    # return None — we can't make a clean decision.
    clauses = [c.strip() for c in condition.split("||")]
    results = [_eval_atom(c, variables) for c in clauses]
    if any(r is None for r in results):
        return None
    return any(results)  # type: ignore[arg-type]


def _eval_atom(atom: str, variables: Mapping[str, object]) -> bool | None:
    """Evaluate a single comparison atom: `<var> <op> <literal>`."""
    # Find the operator. Test multi-char operators first so we don't grab the
    # `=` of `==` as a bare `=` (none of our ops are `=` but being defensive
    # against future grammar additions).
    op_used: str | None = None
    op_index: int = -1
    for op in _OPERATORS:
        idx = atom.find(op)
        if idx != -1:
            op_used = op
            op_index = idx
            break
    if op_used is None:
        return None

    var_name = atom[:op_index].strip()
    literal = atom[op_index + len(op_used):].strip()
    # Strip optional surrounding quotes — the DOT parser strips outer quotes
    # but the literal might still carry them on either side.
    if len(literal) >= 2 and literal[0] == literal[-1] and literal[0] in ("'", '"'):
        literal = literal[1:-1]

    if not _is_identifier(var_name):
        return None

    if var_name not in variables:
        # Unset variable: we can't evaluate. Fall through to orchestrator —
        # it may set the var just-in-time before retrying.
        return None
    value = variables[var_name]

    if op_used in _NUMERIC_OPS:
        try:
            lhs = int(str(value))
            rhs = int(literal)
        except (TypeError, ValueError):
            return None
        if op_used == "<": return lhs < rhs
        if op_used == "<=": return lhs <= rhs
        if op_used == ">": return lhs > rhs
        if op_used == ">=": return lhs >= rhs
        return None  # unreachable

    # Python's `str(True)` is `'True'` (capitalised). Flow authors write
    # conditions like `should_sync == true` — lowercase — to match the JSON
    # / shell convention. When a script emits `FLOWSTATE_OUTPUT_x=true` the
    # reducer parser decodes to Python `True`, so without this normalisation
    # `str(True) == 'true'` is False and the conditional edge silently
    # fails to match. Mapping bools to their lowercase JSON form here is
    # the minimal fix and matches what shell, JSON, YAML, and most condition
    # DSLs do.
    rendered_value = (
        "true" if value is True
        else "false" if value is False
        else str(value)
    )
    if op_used == "==":
        return rendered_value == literal
    if op_used == "!=":
        return rendered_value != literal
    return None


_IDENT_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")


def _is_identifier(s: str) -> bool:
    return bool(_IDENT_RE.match(s))

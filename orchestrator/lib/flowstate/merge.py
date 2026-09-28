from __future__ import annotations
from typing import Any

from flowstate.state import check_var_size

_DEFAULT = "append_in_completion_order"


def _apply(strategy: str, existing: Any, incoming: Any, branch_id: str) -> Any:
    # I-3: a declared variable pre-initialised to None (default) must not
    # leak that None into the merged list / concat output. Treat None as
    # "no existing value" so the first branch write seeds the result
    # cleanly. Callers that need to preserve a real None must not declare
    # a merge strategy.
    if existing is None:
        if strategy == "last_wins":
            return incoming
        if strategy == "error_on_conflict":
            return incoming
        if strategy in (_DEFAULT, "concat") and isinstance(incoming, str):
            return incoming
        if strategy in (_DEFAULT, "list_append"):
            return list(incoming) if isinstance(incoming, list) else [incoming]
        # append fallthrough (no existing) — wrap incoming in the
        # branch-tagged list shape.
        return [{"branch_id": branch_id, "value": incoming}]
    if strategy in (_DEFAULT, "concat") and isinstance(existing, str) and isinstance(incoming, str):
        return f"{existing}\n{incoming}"
    if strategy in (_DEFAULT, "list_append"):
        base = existing if isinstance(existing, list) else [existing]
        add = incoming if isinstance(incoming, list) else [incoming]
        return base + add
    if strategy == "last_wins":
        return incoming
    if strategy == "error_on_conflict":
        raise ValueError(f"merge conflict on variable for branch {branch_id}")
    # append default fallthrough: wrap into list of {branch_id, value}
    base = existing if isinstance(existing, list) else [existing]
    return base + [{"branch_id": branch_id, "value": incoming}]


def merge_branch_scopes(
    parent: dict[str, Any],
    branches: list[tuple[str, str, str, dict[str, Any]]],  # (branch_id, source_node, status, delta)
    *,
    strategies: dict[str, str],
) -> dict[str, Any]:
    """Overlay each branch's delta onto a copy of the parent scope in the
    order given (caller passes branches already sorted by completion). For a
    variable written by >1 branch and absent from the parent, apply the
    per-variable strategy (default append_in_completion_order). Injects a
    reserved `_branches` metadata list."""
    merged = dict(parent)
    seen_writes: set[str] = set()
    for branch_id, _src, _status, delta in branches:
        for var, value in delta.items():
            if var.startswith("_"):
                raise ValueError(f"branch {branch_id} wrote reserved var {var!r}")
            # I-3: when a strategy is declared for the variable, the explicit
            # opt-in beats the parent-membership short-circuit. Without this,
            # variables declared in .flow.yml (which get pre-initialised to
            # their default — often None — in state.variables and therefore
            # appear in `parent`) would silently fall through to last-writer-
            # wins regardless of the declared strategy.
            has_strategy = var in strategies
            if not has_strategy and (var not in seen_writes or var in parent):
                merged[var] = value
                seen_writes.add(var)
            elif var not in seen_writes:
                # First branch write for this var. With an explicit strategy
                # declared, seed from the parent's existing value (may be the
                # declared default like None) so the strategy folds the first
                # branch's incoming value on top of it deterministically.
                strat = strategies[var]
                existing = merged.get(var)
                merged[var] = _apply(strat, existing, value, branch_id)
                seen_writes.add(var)
            else:
                strat = strategies.get(var, _DEFAULT)
                merged[var] = _apply(strat, merged[var], value, branch_id)
            # T22 / I1: enforce the 16 KB ceiling on every merge result.
            # `list_append` / `concat` strategies can concatenate two
            # in-bounds branch values into an out-of-bounds merged value;
            # without this check the merged scope would silently exceed the
            # limit that `set_var` defends. ValueError propagates to
            # `_fire_join`, which catches and returns AdvanceOutcome.error.
            check_var_size(var, merged[var])
    merged["_branches"] = [
        {"branch_id": bid, "source_node": src, "status": status}
        for bid, src, status, _ in branches
    ]
    return merged

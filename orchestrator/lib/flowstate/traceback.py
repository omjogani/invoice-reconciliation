from __future__ import annotations

from pathlib import Path

from flowstate.state import RunState


def ancestors(run_dir: str | Path) -> list[str]:
    """Walk parent_run pointers upward. Returns paths starting with the given
    run_dir and ending at the top-level run (whose parent_run is None)."""
    chain = [str(run_dir)]
    cur = RunState.load(run_dir)
    while cur.parent_run:
        chain.append(str(cur.parent_run.run_dir))
        cur = RunState.load(cur.parent_run.run_dir)
    return chain


def descendants(run_dir: str | Path) -> list[str]:
    """Walk BranchRef.subflow_run_dir downward, recursively. Returns the flat
    list of all subflow run dirs reachable from the given run, depth-first."""
    out: list[str] = []
    st = RunState.load(run_dir)
    for ph in st.phases.values():
        for b in ph.branches:
            if b.subflow_run_dir:
                out.append(str(b.subflow_run_dir))
                out.extend(descendants(b.subflow_run_dir))
    return out

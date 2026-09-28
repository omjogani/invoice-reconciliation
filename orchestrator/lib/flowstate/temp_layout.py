"""Canonical per-node temp-directory layout for graph workers.

Both `flowstate` (validate step) and `agentctl` (spawn) need to agree on where
each worker's `completion.yml`, `worker.log`, `prompt.txt`, etc. live. Keeping
the path convention in one place prevents the two modules from silently
drifting — see docs/factory-pipeline-e2e-issues.md #30.
"""
from __future__ import annotations

from pathlib import Path


def phase_temp_dir(
    repo_root: Path,
    flow_name: str,
    run_descriptor: str,
    userid: str,
    timestamp: str,
    phase: str,
) -> Path:
    """Return the per-node temp dir under `factory/execution/temporary/`.

    Layout: `{repo_root}/factory/execution/temporary/{flow_name}/{run_descriptor}_{userid}_{timestamp}/{phase}/`
    """
    return (
        Path(repo_root)
        / "factory"
        / "execution"
        / "temporary"
        / flow_name
        / f"{run_descriptor}_{userid}_{timestamp}"
        / phase
    )


def completion_path(
    repo_root: Path,
    flow_name: str,
    run_descriptor: str,
    userid: str,
    timestamp: str,
    phase: str,
) -> Path:
    """Return the canonical `completion.yml` path for the given phase."""
    return phase_temp_dir(repo_root, flow_name, run_descriptor, userid, timestamp, phase) / "completion.yml"

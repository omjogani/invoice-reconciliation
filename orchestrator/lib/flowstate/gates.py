from __future__ import annotations

import os
import subprocess
from dataclasses import dataclass, field
from pathlib import Path


@dataclass
class ScriptFailure:
    path: str
    exit_code: int
    stdout: str
    stderr: str


@dataclass
class GateOutcome:
    passed: bool
    failures: list[ScriptFailure] = field(default_factory=list)


def _resolve(path: str, flow_dir: Path) -> Path:
    p = Path(path)
    return p if p.is_absolute() else flow_dir / p


GATE_TIMEOUT_SECONDS = 60


def _run_one(path: Path, flow_dir: Path, env_extra: dict[str, str]) -> ScriptFailure | None:
    env = {**os.environ, **env_extra}
    try:
        result = subprocess.run(
            [str(path)],
            capture_output=True,
            text=True,
            cwd=flow_dir,
            env=env,
            timeout=GATE_TIMEOUT_SECONDS,
        )
    except subprocess.TimeoutExpired:
        return ScriptFailure(
            path=str(path),
            exit_code=-1,
            stdout="",
            stderr=f"gate timed out after {GATE_TIMEOUT_SECONDS}s",
        )
    except OSError as exc:
        return ScriptFailure(path=str(path), exit_code=-1, stdout="", stderr=str(exc))
    if result.returncode == 0:
        return None
    return ScriptFailure(
        path=str(path),
        exit_code=result.returncode,
        stdout=result.stdout,
        stderr=result.stderr,
    )


def run_gates(paths: list[str], flow_dir: Path, env_extra: dict[str, str]) -> GateOutcome:
    """Run a list of subprocess paths to completion and collect failures.

    Used for both *edge gates* (blocking pass/fail checks) and *transition
    scripts* (side-effecting setup that runs once an edge has been chosen).
    Both kinds share the same execution + failure-collection contract, so a
    single function backs them. The caller decides what to do with the
    `GateOutcome` — block on `passed=False` (gates) or fold failures into the
    advance `reason` (transition scripts). See Issue #46.
    """
    failures: list[ScriptFailure] = []
    for path in paths:
        resolved = _resolve(path, flow_dir)
        failure = _run_one(resolved, flow_dir, env_extra)
        if failure:
            failures.append(failure)
    return GateOutcome(passed=not failures, failures=failures)

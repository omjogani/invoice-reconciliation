from __future__ import annotations

import subprocess
from pathlib import Path


class VcsError(RuntimeError):
    pass


GIT_TIMEOUT_SECONDS = 120


def _run(args: list[str], cwd: Path) -> subprocess.CompletedProcess[str]:
    try:
        return subprocess.run(
            args, cwd=cwd, capture_output=True, text=True, timeout=GIT_TIMEOUT_SECONDS,
        )
    except subprocess.TimeoutExpired as exc:
        raise VcsError(
            f"git command timed out after {GIT_TIMEOUT_SECONDS}s: {' '.join(args)}"
        ) from exc


def _branch_exists(repo: Path, branch: str) -> bool:
    proc = _run(["git", "rev-parse", "--verify", "--quiet", f"refs/heads/{branch}"], repo)
    return proc.returncode == 0


def create_worktree(repo: Path, path: Path, branch: str, base: str) -> None:
    path = Path(path)
    if path.exists():
        raise VcsError(f"worktree path already exists: {path}")
    if _branch_exists(repo, branch):
        proc = _run(["git", "worktree", "add", str(path), branch], repo)
    else:
        proc = _run(["git", "worktree", "add", "-b", branch, str(path), base], repo)
    if proc.returncode != 0:
        raise VcsError(
            f"git worktree add failed (exit {proc.returncode}): {proc.stderr.strip()}"
        )


def remove_worktree(repo: Path, path: Path, force: bool = False) -> None:
    if not Path(path).exists():
        return
    if not force:
        dirty = _run(["git", "status", "--porcelain"], Path(path))
        if dirty.returncode != 0:
            raise VcsError(f"git status failed in {path}: {dirty.stderr.strip()}")
        if dirty.stdout.strip():
            raise VcsError(f"refusing to remove worktree with uncommitted changes: {path}")
    args = ["git", "worktree", "remove", str(path)]
    if force:
        args.append("--force")
    proc = _run(args, repo)
    if proc.returncode != 0:
        raise VcsError(f"git worktree remove failed: {proc.stderr.strip()}")


def merge_branch(integration_worktree: Path, source_branch: str, message: str) -> None:
    proc = _run(
        ["git", "merge", "--no-ff", "-m", message, source_branch],
        integration_worktree,
    )
    if proc.returncode != 0:
        raise VcsError(
            f"git merge failed (exit {proc.returncode}): {proc.stderr.strip() or proc.stdout.strip()}"
        )

"""Repo-root resolution and path normalisation.

Every path that names a location inside the repo is stored relative to
the repo root and resolved to absolute at use time. This module is the
only place that owns that conversion.
"""
from __future__ import annotations

import subprocess
from pathlib import Path


class RepoRootError(RuntimeError):
    """Raised when repo-root resolution or a relative-path conversion fails."""


def discover_repo_root(cwd: Path | None = None) -> Path:
    """Return the absolute path to the git working tree root containing `cwd`.

    Raises RepoRootError if `cwd` is not inside a git working tree.
    """
    cwd = Path(cwd) if cwd is not None else Path.cwd()
    # Timeout: prevent a hung git (broken hook, hung FUSE / NFS) from
    # blocking the whole CLI. See Issue #44g.
    try:
        proc = subprocess.run(
            ["git", "rev-parse", "--show-toplevel"],
            cwd=cwd,
            capture_output=True,
            text=True,
            timeout=5,
        )
    except subprocess.TimeoutExpired:
        raise RepoRootError(
            f"`git rev-parse --show-toplevel` did not complete within 5 "
            f"seconds in {cwd!s}; check for broken hooks or hung filesystem"
        )
    if proc.returncode != 0:
        raise RepoRootError(
            f"cwd {cwd!s} is not inside a git working tree; "
            f"flowstate runs must be initiated from inside a git repo"
        )
    return Path(proc.stdout.strip()).resolve()


def to_repo_relative(path: str | Path, repo_root: Path) -> str:
    """Express `path` as a forward-slash string relative to `repo_root`.

    Raises RepoRootError if `path` lies outside `repo_root`.
    """
    resolved = Path(path).resolve()
    root = Path(repo_root).resolve()
    try:
        rel = resolved.relative_to(root)
    except ValueError:
        raise RepoRootError(
            f"path {resolved!s} is outside repo root {root!s}; "
            f"all repo-anchored paths must resolve inside the repo"
        )
    return rel.as_posix()


def to_absolute(rel_path: str, repo_root: Path) -> Path:
    """Join `rel_path` with `repo_root` and return an absolute Path.

    Refuses inputs that are absolute or whose resolution escapes `repo_root`.
    """
    if Path(rel_path).is_absolute():
        raise RepoRootError(
            f"path {rel_path!r} is absolute; expected a repo-root-relative path"
        )
    root = Path(repo_root).resolve()
    resolved = (root / rel_path).resolve()
    try:
        resolved.relative_to(root)
    except ValueError:
        raise RepoRootError(
            f"path {rel_path!r} resolves to {resolved!s}, outside repo root {root!s}"
        )
    return resolved

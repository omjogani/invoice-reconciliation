"""How to invoke this agentctl from a spawned agent's shell.

A teammate is told, in its preamble, exactly how to run team commands. Bare
`agentctl` only works when the tool happens to be on PATH — and a spawned
worker's bash is non-interactive, so it never sources a shell rc. Resolve a
command prefix that works regardless of how agentctl was obtained (checked
into a repo, pip-installed, vendored).
"""
from __future__ import annotations

import os
import shutil
import sys
from pathlib import Path


def resolve_cli() -> str:
    """The command prefix that invokes agentctl, in order of portability:

    1. an ``agentctl`` executable on PATH (installed/shared) — shortest and
       stays correct if the install moves;
    2. the sibling ``bin/agentctl`` wrapper next to this package;
    3. ``<this python> -m agentctl`` — always correct for the running
       interpreter, and the only option when neither wrapper nor PATH entry
       exists. Requires the lib dir on the child's PYTHONPATH (spawned
       workers inherit it from the parent that resolved this).
    """
    on_path = shutil.which("agentctl")
    if on_path:
        return "agentctl"
    import agentctl
    wrapper = Path(agentctl.__file__).resolve().parent.parent / "bin" / "agentctl"
    if wrapper.is_file() and os.access(wrapper, os.X_OK):
        return str(wrapper)
    return f"{sys.executable} -m agentctl"

"""Template substitution.

flowstate is the single source of truth for {name} placeholder resolution in
prompts and in path templates. The orchestrator never substitutes variables
itself; it asks for the fully-rendered text via `flowstate render-prompt` or
for resolved output paths via `flowstate node-config`.
"""
from __future__ import annotations

import re
from pathlib import Path
from typing import Any

from flowstate.repo_root import to_absolute


class RenderError(RuntimeError):
    pass


_PLACEHOLDER = re.compile(r"\{([A-Za-z_][A-Za-z0-9_]*)\}")


def render_prompt(template: str, variables: dict[str, Any]) -> str:
    """Substitute every `{name}` placeholder with `variables[name]`.

    Raises RenderError if any placeholder has no entry (or a `None` / empty value)
    in `variables`. This is the single substitution point used by render-prompt
    and by template resolution for output paths.
    """
    missing: list[str] = []
    out_parts: list[str] = []
    last = 0
    for m in _PLACEHOLDER.finditer(template):
        name = m.group(1)
        if name not in variables or variables[name] in (None, ""):
            missing.append(name)
            continue
        out_parts.append(template[last:m.start()])
        out_parts.append(str(variables[name]))
        last = m.end()
    if missing:
        raise RenderError(
            f"template has missing placeholder(s): {', '.join(missing)}"
        )
    out_parts.append(template[last:])
    return "".join(out_parts)


def resolve_path_template(
    template: str, variables: dict[str, Any], repo_root: Path
) -> str:
    """Substitute placeholders, then resolve as a repo-relative path → absolute.

    Layered on `render_prompt`: same placeholder semantics, same `RenderError`
    on missing values. Additionally refuses templates that resolve to an
    absolute path (those would escape `repo_root`) and converts the rendered
    repo-relative form to an absolute string via `to_absolute`.

    Used by both `cli._cmd_validate_locked` and `traversal._populate_output_paths`
    so the path-resolution contract has exactly one definition. See Issue #46.
    """
    out = render_prompt(template, variables)
    if Path(out).is_absolute():
        raise RenderError(
            f"resolved template {template!r} produced absolute path {out!r}"
        )
    return str(to_absolute(out, repo_root))

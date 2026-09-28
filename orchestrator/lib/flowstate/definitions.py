"""JSON Schema validation utilities.

The completion.yml schema itself lives in :mod:`flowstate.completion`
(Issue #45g); this module hosts only the general-purpose helpers that
apply to both built-in and flow-author-declared schemas.
"""
from __future__ import annotations

import copy
from typing import Any

import jsonschema


class SchemaValidationError(RuntimeError):
    pass


def extend_with_session_id(user_schema: dict[str, Any]) -> dict[str, Any]:
    """Return a copy of user_schema with `_session_id: string` required at top level.

    User schemas declared in factory/flows/<flow>/definitions/<name>.json
    describe the data shape only. flowstate adds the `_session_id` metadata
    requirement uniformly so flow authors don't have to repeat it.
    """
    out = copy.deepcopy(user_schema)
    out.setdefault("type", "object")
    out.setdefault("properties", {})
    out["properties"].setdefault("_session_id", {"type": "string", "minLength": 1})
    required = list(out.get("required", []))
    if "_session_id" not in required:
        required.insert(0, "_session_id")
    out["required"] = required
    return out


def validate_against_schema(instance: Any, schema: dict[str, Any]) -> None:
    """Raise SchemaValidationError with a clean message if instance doesn't validate."""
    try:
        jsonschema.validate(instance=instance, schema=schema)
    except jsonschema.ValidationError as exc:
        # Make the error orchestrator-readable.
        path = "/".join(str(p) for p in exc.absolute_path) or "<root>"
        raise SchemaValidationError(f"{path}: {exc.message}")

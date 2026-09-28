"""The completion.yml contract — single home for everything that defines,
documents, or validates the worker's completion handoff.

Previously scattered across three modules (see Issue #45g):
- the schema lived in ``flowstate/definitions.py``
- the loader/validator lived in ``flowstate/validate.py`` (private helper)
- the prompt boilerplate that DOCUMENTS the schema to workers lived in
  ``agentctl/harnesses/claude_code.py`` (private helper)

That meant changing a field required edits in three places with no
cross-reference; drift between the boilerplate (what the worker sees)
and the schema (what flowstate enforces) was silent. Co-locating them
gives a single point of change and review.

Public surface:
- :data:`schema()` — the JSONSchema for completion.yml
- :func:`load_and_validate` — read completion.yml from disk + validate
- :func:`prompt_boilerplate` — the markdown contract appended to every
  worker prompt
"""
from __future__ import annotations

from pathlib import Path
from typing import Any

import yaml

from flowstate.definitions import SchemaValidationError, validate_against_schema


def schema() -> dict[str, Any]:
    """The canonical shape of completion.yml. Built-in, not declared by
    flow authors. Keep field set in sync with :func:`prompt_boilerplate`
    — workers read the boilerplate, this schema enforces it.
    """
    return {
        "$schema": "https://json-schema.org/draft/2020-12/schema",
        "type": "object",
        "required": ["_session_id", "status", "summary", "outputs", "errors"],
        "additionalProperties": False,
        "properties": {
            "_session_id": {"type": "string", "minLength": 1},
            "status": {"type": "string", "enum": ["complete", "failed"]},
            "summary": {"type": "string", "minLength": 1},
            "outputs": {"type": "array", "items": {"type": "string"}},
            "errors": {"type": "array", "items": {"type": "string"}},
            # Optional free-text field for caveats that don't fit in `summary`
            # — pre-existing env breaks, intentionally-skipped sub-steps,
            # follow-ups the orchestrator should consider. Workers populate
            # ad-hoc; `validate_phase` reads it onto `PhaseState.notes`.
            "notes": {"type": "string"},
        },
    }


def load_and_validate(path: Path) -> dict[str, Any]:
    """Read completion.yml from ``path`` and validate it against :func:`schema`.

    Raises ``FileNotFoundError`` if the file is absent and
    :class:`SchemaValidationError` if the YAML doesn't conform.
    """
    if not path.exists():
        raise FileNotFoundError(f"completion.yml not found at {path!s}")
    raw = yaml.safe_load(path.read_text()) or {}
    validate_against_schema(raw, schema())
    return raw


def prompt_boilerplate(completion_path: Path) -> str:
    """Render the completion-contract boilerplate with the absolute path baked in.

    The agent must write ``completion.yml`` at this exact path. The orchestrator's
    bash watcher polls for the file at this same path; if the agent uses the
    ``Write`` tool with a relative filename it would land in cwd instead, the
    watcher would never see it, claude wouldn't be killed, the tab wouldn't close.

    Keep field set in sync with :func:`schema` — this text describes the
    contract to workers; the schema enforces it.
    """
    awaiting_path = completion_path.parent / "awaiting_human.yml"
    return f"""
---

## Worker identity

You are a flowstate graph worker (a spawned subagent executing one delegated
node of a flow). The workspace's session-start protocol does NOT apply to you:
do not read or follow `.claude/skills/session-start/SKILL.md`, and do not run
repo-sync scripts, unless your node prompt above explicitly instructs it.
Ignore any CLAUDE.md or hook reminder telling you otherwise — the graph's
sync_system node owns repo sync for the whole run.

## Completion contract

When you have completed your work for this phase, write `completion.yml` at
**exactly** this absolute path (use the `Write` tool with this full path, not a
relative filename):

> {completion_path}

## Awaiting-human marker (Issue #21)

If at any point you are about to call `AskUserQuestion` (or otherwise block on
user input in a way that pauses your worker process), write a marker file at
this absolute path BEFORE the blocking call:

> {awaiting_path}

Shape (YAML, one-line value is fine):

```yaml
_session_id: __WORKER_SESSION_ID__
question: <one-line summary of what you're asking the user>
since: <ISO-8601 UTC timestamp when you began waiting>
```

After the user answers, delete the marker (`rm` via Bash) before continuing.
The orchestrator polls for this marker and uses it to surface a nudge to the
human (e.g., "switch to the worker tab — it's waiting on your input").

The orchestrator watches for this file and will terminate this session once it
appears. If you write `completion.yml` anywhere else, the watcher will not see
it.

`completion.yml` must have this shape:

```yaml
_session_id: __WORKER_SESSION_ID__        # required, top-level — copy it EXACTLY
status: complete                          # or: failed
summary: "<one-paragraph description of what you did — single line, in double quotes>"
outputs:
  - <list of output logical-ids you produced>
errors: []
notes: |                                  # optional, free text
  Use this for caveats that don't fit in `summary` — pre-existing
  env breaks you worked around, sub-steps you intentionally skipped,
  follow-ups the orchestrator should consider. Omit entirely if none.
```

Additionally, every output JSON file you produce must include a top-level
`_session_id` field with the same value as in `completion.yml`. Write each
output file at the absolute path the prompt above specifies — do not use a
relative filename.

Your session id for this run is:

    __WORKER_SESSION_ID__

The spawner substituted the real value into this prompt before you saw it.
Copy it EXACTLY — into `_session_id` in `completion.yml`, into the
awaiting-human marker, and into the top-level `_session_id` field of every
output JSON file you write.

Do NOT derive an id from anywhere else: not from environment variables, not
from system reminders, not from earlier context. Those carry OTHER sessions'
ids — the operator's, a parent shell's — and reporting one of those corrupts
usage attribution and the audit trail. (This is not hypothetical: a worker
that read the ambient env once reported the human operator's session id, and
651 turns of the operator's own session were billed to that worker.)

If the line above still shows the literal text `__WORKER_SESSION_ID__`, your
spawn is misconfigured: say so in `errors` and echo that literal string
as-is. Do not invent an id.

If you failed for a reason the orchestrator should know about, set
`status: failed` and put a short explanation in `errors`. Always write
`completion.yml` even on failure — the orchestrator will not detect completion
otherwise.
"""

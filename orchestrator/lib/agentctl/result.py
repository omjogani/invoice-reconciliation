"""The agentctl Result envelope.

DUPLICATED from flowstate/result.py by design (spec §2): agentctl is a
standalone substrate and imports nothing from flowstate. The two copies are
pinned to one serialized shape by tests/agentctl/test_envelope_contract.py —
change shape there first or that test will tell you.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Literal

import yaml


# Result envelope status vocabulary. See Issue #46.
ResultStatus = Literal["ok", "error"]


@dataclass
class Result:
    status: ResultStatus
    payload: dict[str, Any] | None = None
    intent: str | None = None
    failure: str | None = None
    suggestion: str | None = None

    @classmethod
    def ok(cls, payload: dict[str, Any] | None = None) -> Result:
        return cls(status="ok", payload=payload or {})

    @classmethod
    def error(cls, intent: str, failure: str, suggestion: str) -> Result:
        return cls(status="error", intent=intent, failure=failure, suggestion=suggestion)

    def _body(self) -> dict[str, Any]:
        body: dict[str, Any] = {"status": self.status}
        if self.status == "ok":
            body["payload"] = self.payload or {}
        else:
            body["intent"] = self.intent
            body["failure"] = self.failure
            body["suggestion"] = self.suggestion
        return body

    def to_yaml(self) -> str:
        # `width=10**9` prevents pyyaml from folding long strings across lines
        # with `\`-continuations. `allow_unicode=True` writes literal unicode
        # bytes instead of `\u`-escapes. Both protect against zsh's `echo`
        # builtin interpreting backslash sequences inside captured payloads
        # and emitting "character not in range" — see Issue #12 (line-fold)
        # and Issue #37 follow-up (`—` em-dash in rendered prompts).
        return yaml.safe_dump(
            self._body(), sort_keys=False, width=10**9, allow_unicode=True
        )

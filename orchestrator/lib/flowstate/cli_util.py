"""Shared CLI plumbing used by both `flowstate` and `agentctl`.

These helpers exist to keep the two CLI entry points consistent — every
command in both tools must:
  - emit exactly one ``Result`` envelope on stdout, and
  - never let a stray exception escape as a stacktrace (the orchestrator
    parses stdout as YAML; bare tracebacks on stderr would break that
    contract).

See Issue #45e — these used to be duplicated (``_emit``) or missing
entirely from one side (``_safe`` lived only in ``agentctl``). Centralising
them here makes the contract enforceable in one place.
"""
from __future__ import annotations

import argparse
import sys
from typing import Callable

from flowstate.result import Result


def emit(result: Result) -> int:
    """Write the YAML form of ``result`` to stdout and return the CLI exit code
    (0 for ``status: ok``, 1 for ``status: error``).
    """
    sys.stdout.write(result.to_yaml())
    return 0 if result.status == "ok" else 1


def safe_handler(
    intent_fn: Callable[[argparse.Namespace], str],
    handler: Callable[[argparse.Namespace], int],
    suggestion_fn: Callable[[argparse.Namespace, Exception], str] | None = None,
) -> Callable[[argparse.Namespace], int]:
    """Wrap a CLI handler so any escaped exception becomes a structured
    ``Result.error`` envelope on stdout instead of a stacktrace on stderr.

    Handlers can — and often do — emit ``Result.error`` themselves with
    tailored ``failure`` / ``suggestion`` text per call site. This wrapper
    is a backstop: it only fires when an exception escapes the handler
    unhandled.

    ``intent_fn(args)`` produces the ``intent`` string; ``suggestion_fn(args, exc)``
    optionally produces a remediation hint. When ``suggestion_fn`` is omitted,
    the suggestion is a generic pointer.
    """
    def _default_suggestion(args: argparse.Namespace, exc: Exception) -> str:
        return (
            "An unexpected error escaped the handler. Re-run with the same "
            "args; if the error reproduces, capture the full traceback by "
            "invoking the underlying Python module directly."
        )

    sf = suggestion_fn or _default_suggestion

    def wrapper(args: argparse.Namespace) -> int:
        try:
            return handler(args)
        except RuntimeError as exc:
            return emit(Result.error(
                intent=intent_fn(args),
                failure=str(exc),
                suggestion=sf(args, exc),
            ))
        except Exception as exc:
            return emit(Result.error(
                intent=intent_fn(args),
                failure=f"{type(exc).__name__}: {exc}",
                suggestion=sf(args, exc),
            ))
    return wrapper

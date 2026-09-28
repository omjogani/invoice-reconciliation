from __future__ import annotations

from datetime import datetime, timezone


def iso_now() -> str:
    """UTC timestamp in ISO 8601 format: ``%Y-%m-%dT%H:%M:%SZ``.

    Used wherever flowstate persists wall-clock instants in YAML
    (``PhaseState.started_at``, ``RunState.events[].t``, etc.).
    """
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def compact_stamp() -> str:
    """UTC timestamp in compact filesystem-friendly format:
    ``%Y%m%dT%H%M%S%fZ`` (microsecond precision).

    Used for run-directory descriptors where colons and dashes are awkward.
    The stamp doubles as the uniqueness discriminator in shared-folder
    artefact filenames (``plan_{userid}_{timestamp}.yml`` etc.), so it must
    be unique across runs AND across subflow branches fanned out within the
    same wall-clock second — second-resolution stamps collided there
    (2026-07-13 batch E2E). Nothing parses the stamp back; it is an opaque
    sortable token.
    """
    return datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")

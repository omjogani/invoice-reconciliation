"""``python -m recon <command>``: the entry points the flow scripts call.

Every command prints a short JSON summary on stdout and exits non-zero with
a one-line reason on stderr when it refuses to proceed.
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path


def _cmd_ingest(args: argparse.Namespace) -> int:
    from recon.ingest import IngestError, ingest, write_outputs

    try:
        documents, lines, inventory = ingest(Path(args.invoices))
    except IngestError as exc:
        print(f"ingest failed: {exc}", file=sys.stderr)
        return 2
    write_outputs(Path(args.out), documents, lines, inventory)
    print(json.dumps({"documents": len(documents), "lines": len(lines),
                      "unknown": inventory["unknown"]}, sort_keys=True))
    return 0


def _cmd_approve_rate_card(args: argparse.Namespace) -> int:
    """Human approval of an agent-compiled card (D1). Refuses cards that fail any gate."""
    from datetime import datetime, timezone

    from recon.model import read_json
    from recon.ratecard import check_card
    from recon.ratecard.contract import load_contract
    from recon.ratecard.snapshot import SnapshotStore

    card = read_json(args.card)
    contract = load_contract(args.contract)
    problems = check_card(card, contract, args.carrier)
    if card.get("unsupported"):
        problems.append("card reports unsupported clauses; it cannot be approved")
    if problems:
        print("refusing to approve:\n  " + "\n  ".join(problems), file=sys.stderr)
        return 2
    store = SnapshotStore(args.store)
    try:
        path = store.approve(card, approved_by=args.approved_by, source_run=args.source_run,
                             approved_at=datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
                             replace=args.replace)
    except FileExistsError as exc:
        print(str(exc), file=sys.stderr)
        return 2
    print(json.dumps({"approved": str(path)}))
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="recon")
    sub = parser.add_subparsers(dest="command", required=True)

    p = sub.add_parser("ingest", help="inventory, parse and tie out billing documents")
    p.add_argument("--invoices", required=True, help="directory of billing documents")
    p.add_argument("--out", required=True, help="directory for documents.json, lines.jsonl, inventory.json")
    p.set_defaults(func=_cmd_ingest)

    p = sub.add_parser("approve-rate-card", help="record a human approval of a compiled rate card")
    p.add_argument("--card", required=True, help="agreed card produced by a run")
    p.add_argument("--contract", required=True, help="the contract markdown it was compiled from")
    p.add_argument("--carrier", required=True, help="carrier id from config/carriers.json")
    p.add_argument("--approved-by", required=True, help="name of the approving person")
    p.add_argument("--source-run", required=True, help="run directory that produced the card")
    p.add_argument("--store", default="rate-cards/approved", help="snapshot directory")
    p.add_argument("--replace", action="store_true", help="supersede an existing snapshot")
    p.set_defaults(func=_cmd_approve_rate_card)

    args = parser.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())

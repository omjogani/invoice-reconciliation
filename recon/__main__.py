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


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="recon")
    sub = parser.add_subparsers(dest="command", required=True)

    p = sub.add_parser("ingest", help="inventory, parse and tie out billing documents")
    p.add_argument("--invoices", required=True, help="directory of billing documents")
    p.add_argument("--out", required=True, help="directory for documents.json, lines.jsonl, inventory.json")
    p.set_defaults(func=_cmd_ingest)

    args = parser.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())

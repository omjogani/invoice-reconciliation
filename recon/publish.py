"""Publishing validated outputs and comparing runs.

``publish`` copies a validated report and memos from a run's artefact
directory to the repository root, replacing the previous memos, and writes a
manifest tying the outputs to the run and its inputs.

``diff_runs`` compares two runs on everything that must be identical (line
and finding membership, amounts, dispositions, clauses, totals, memo set).
Justification and memo wording are allowed to differ; they are agent text.
"""
from __future__ import annotations

import shutil
from pathlib import Path
from typing import Any

from recon.model import read_json, write_json

LINE_FIELDS = ("invoice", "consignment_ref", "shipment_id", "billed_amount", "expected_amount", "delta",
               "disposition", "contract_clause")
FINDING_FIELDS = ("invoice", "amount_impact", "disposition", "contract_clause")
TOTAL_FIELDS = ("invoice", "billed_total", "expected_total")


def publish(source_dir: Path, target_dir: Path, manifest: dict[str, Any]) -> dict[str, Any]:
    source_dir, target_dir = Path(source_dir), Path(target_dir)
    report = source_dir / "reconciliation-report.json"
    memos = source_dir / "memos"
    if not report.exists() or not memos.is_dir():
        raise FileNotFoundError(f"{source_dir} has no validated report and memos to publish")
    shutil.copyfile(report, target_dir / "reconciliation-report.json")
    target_memos = target_dir / "memos"
    if target_memos.exists():
        shutil.rmtree(target_memos)
    shutil.copytree(memos, target_memos)
    manifest = {**manifest, "memo_count": len(list(target_memos.glob("*.md")))}
    write_json(target_dir / "run-manifest.json", manifest)
    return manifest


def _project(report: dict) -> dict[str, Any]:
    return {
        "lines": [{k: l.get(k) for k in LINE_FIELDS} for l in report["lines"]],
        "invoice_findings": [{k: f.get(k) for k in FINDING_FIELDS} for f in report.get("invoice_findings", [])],
        "invoice_totals": [{k: t.get(k) for k in TOTAL_FIELDS} for t in report["invoice_totals"]],
        "summary": report["summary"],
    }


def diff_runs(dir_a: Path, dir_b: Path) -> list[str]:
    dir_a, dir_b = Path(dir_a), Path(dir_b)
    a = _project(read_json(dir_a / "reconciliation-report.json"))
    b = _project(read_json(dir_b / "reconciliation-report.json"))
    out: list[str] = []
    for section in ("lines", "invoice_findings", "invoice_totals"):
        if len(a[section]) != len(b[section]):
            out.append(f"{section}: {len(a[section])} vs {len(b[section])} entries")
            continue
        for i, (x, y) in enumerate(zip(a[section], b[section])):
            for k in x:
                if x[k] != y[k]:
                    out.append(f"{section}[{i}] {x.get('invoice')}/{x.get('consignment_ref', '')}: {k} {x[k]!r} vs {y[k]!r}")
    for k in a["summary"]:
        if a["summary"][k] != b["summary"][k]:
            out.append(f"summary.{k}: {a['summary'][k]!r} vs {b['summary'][k]!r}")
    memos_a = sorted(p.name for p in (dir_a / "memos").glob("*.md"))
    memos_b = sorted(p.name for p in (dir_b / "memos").glob("*.md"))
    if memos_a != memos_b:
        out.append(f"memo files differ: only in first {sorted(set(memos_a) - set(memos_b))}, "
                   f"only in second {sorted(set(memos_b) - set(memos_a))}")
    return out

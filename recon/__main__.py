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


def _cmd_reconcile(args: argparse.Namespace) -> int:
    from recon.model import read_json, read_jsonl, write_json
    from recon.reconcile import ReconcileError, reconcile

    work = Path(args.work)
    try:
        result = reconcile(read_json(work / "documents.json"), read_jsonl(work / "lines.jsonl"),
                           read_json(args.shipments), read_json(args.cards))
    except ReconcileError as exc:
        print(f"reconcile failed: {exc}", file=sys.stderr)
        return 2
    write_json(args.out, result)
    counts: dict[str, int] = {}
    for line in result["lines"]:
        counts[line["disposition"]] = counts.get(line["disposition"], 0) + 1
    print(json.dumps({"lines": len(result["lines"]), "by_disposition": counts,
                      "invoice_findings": len(result["invoice_findings"])}, sort_keys=True))
    return 0


def _contracts(carriers_config: str) -> dict[str, Path]:
    from recon.model import read_json

    root = Path(carriers_config).resolve().parents[1]
    return {c["carrier"]: root / c["contract"] for c in read_json(carriers_config)["carriers"]}


def _flowstate_var(name: str | None, value) -> None:
    if name:
        print(f"FLOWSTATE_OUTPUT_{name}=" + json.dumps(value, sort_keys=True))


def _cmd_plan_review(args: argparse.Namespace) -> int:
    from recon.model import read_json, write_json
    from recon.reconcile import load_reconciled
    from recon.review import plan_review

    reconciled = load_reconciled(read_json(args.reconciled))
    batches = plan_review(reconciled, _contracts(args.carriers), args.as_of, args.batch_size)
    out = Path(args.out_dir)
    jobs = []
    for batch in batches:
        path = out / f"{batch['batch_id']}.json"
        write_json(path, batch)
        jobs.append({"batch_id": batch["batch_id"], "batch_path": str(path.resolve()),
                     "repo_root": str(_repo_root())})
    write_json(out / "jobs.json", jobs)
    _flowstate_var(args.flowstate_var, jobs)
    _flowstate_var(args.flowstate_count_var, "none" if not jobs else "some")
    print(json.dumps({"batches": len(batches), "items": sum(len(b["items"]) for b in batches)}), file=sys.stderr)
    return 0


def _load_batches(batches_dir: str) -> list[dict]:
    from recon.model import read_json

    return [read_json(p) for p in sorted(Path(batches_dir).glob("B*.json"))]


def _load_reviews(reviews_arg: str) -> dict[str, dict]:
    """``--reviews`` is a JSON file mapping any key → review output path; outputs are keyed by batch_id."""
    from recon.model import read_json

    outputs = [read_json(path) for _, path in sorted(read_json(reviews_arg).items())]
    return {o["batch_id"]: o for o in outputs}


def _cmd_review_check(args: argparse.Namespace) -> int:
    from recon.model import write_json
    from recon.review import check_review

    reviews = _load_reviews(args.reviews)
    failed = {}
    for batch in _load_batches(args.batches_dir):
        output = reviews.get(batch["batch_id"])
        if output is None:
            problems = ["no review output for this batch"]
        elif output.get("status") == "failed":
            problems = [f"review child failed after its retries: {json.dumps(output.get('history'))}"]
        else:
            problems = check_review(batch, output)
        if problems:
            failed[batch["batch_id"]] = problems
    write_json(Path(args.feedback), failed)
    if failed:
        for batch_id, problems in failed.items():
            print(f"{batch_id}:\n  " + "\n  ".join(problems), file=sys.stderr)
        return 2
    print(json.dumps({"batches_checked": len(reviews)}))
    return 0


def _cmd_assemble(args: argparse.Namespace) -> int:
    from recon.model import read_json, write_json
    from recon.reconcile import load_reconciled
    from recon.report import assemble, memo_files

    reconciled = load_reconciled(read_json(args.reconciled))
    batches = _load_batches(args.batches_dir)
    items = {i["item_id"]: i for b in batches for i in b["items"]}
    reviews = {}
    for output in _load_reviews(args.reviews).values():
        for r in output["items"]:
            reviews[r["item_id"]] = r
    report = assemble(reconciled, read_json(Path(args.work) / "documents.json"), reviews)
    out = Path(args.out_dir)
    write_json(out / "reconciliation-report.json", report)
    memo_dir = out / "memos"
    memo_dir.mkdir(parents=True, exist_ok=True)
    for old in memo_dir.glob("*.md"):
        old.unlink()
    for name, text in memo_files(reconciled, reviews, items).items():
        (memo_dir / name).write_text(text, encoding="utf-8")
    print(json.dumps(report["summary"], sort_keys=True))
    return 0


def _cmd_validate_report(args: argparse.Namespace) -> int:
    from recon.model import read_json, read_jsonl
    from recon.reconcile import load_reconciled
    from recon.report import validate_report

    run = Path(args.out_dir)
    report = read_json(run / "reconciliation-report.json")
    items = {i["item_id"]: i for b in _load_batches(args.batches_dir) for i in b["items"]}
    memos = {p.name: p.read_text(encoding="utf-8") for p in sorted((run / "memos").glob("*.md"))}
    problems = validate_report(report, parsed_lines=read_jsonl(Path(args.work) / "lines.jsonl"),
                               documents=read_json(Path(args.work) / "documents.json"),
                               reconciled=load_reconciled(read_json(args.reconciled)), items=items, memos=memos)
    if problems:
        print("report failed validation:\n  " + "\n  ".join(problems), file=sys.stderr)
        return 2
    print(json.dumps({"valid": True, "lines": len(report["lines"]), "memos": len(memos)}))
    return 0


def _cmd_publish(args: argparse.Namespace) -> int:
    from recon.model import read_json
    from recon.publish import publish

    manifest = read_json(args.manifest) if args.manifest else {}
    print(json.dumps(publish(Path(args.source), Path(args.target), manifest), sort_keys=True))
    return 0


def _cmd_diff_runs(args: argparse.Namespace) -> int:
    from recon.publish import diff_runs

    differences = diff_runs(Path(args.a), Path(args.b))
    if differences:
        print("runs differ:\n  " + "\n  ".join(differences), file=sys.stderr)
        return 1
    print(json.dumps({"identical": True}))
    return 0


def _repo_root() -> Path:
    return Path(__file__).resolve().parents[1]


def _print_problems(title: str, problems: list[str]) -> None:
    print(title + ":\n  " + "\n  ".join(problems), file=sys.stderr)


def _cmd_check_card(args: argparse.Namespace) -> int:
    """Agent self-check: the same gates rates_check applies, on one card."""
    from recon.model import read_json
    from recon.ratecard import check_card, strip_metadata
    from recon.ratecard.contract import load_contract
    from recon.stages import vocabulary_problems

    card = read_json(args.card)
    problems = check_card(card, load_contract(args.contract), args.carrier)
    if args.vocabulary and not problems:
        problems += vocabulary_problems(strip_metadata(card), read_json(args.vocabulary))
    if problems:
        _print_problems("card fails the rate-card gates", problems)
        return 2
    print(json.dumps({"card_ok": True}))
    return 0


def _cmd_plan_compile(args: argparse.Namespace) -> int:
    from recon.model import read_json, write_json
    from recon.stages import plan_compile, vocabulary

    out = Path(args.out_dir)
    write_json(out / "vocabulary.json", vocabulary(read_json(args.shipments)))
    jobs = plan_compile(read_json(args.carriers)["carriers"], _repo_root(), out / "vocabulary.json")
    write_json(out / "compile-jobs.json", jobs)
    _flowstate_var(args.flowstate_var, jobs)
    print(json.dumps({"compile_jobs": len(jobs)}), file=sys.stderr)
    return 0


def _cmd_compare_cards(args: argparse.Namespace) -> int:
    from recon.model import read_json, write_json
    from recon.stages import compare_cards

    report = compare_cards(Path(args.a), Path(args.b), Path(args.contract), args.carrier,
                           read_json(args.vocabulary) if args.vocabulary else None)
    write_json(args.out, report)
    _flowstate_var(args.flowstate_var, "yes" if report["agree"] else "no")
    print(json.dumps({"agree": report["agree"]}), file=sys.stderr)
    return 0


def _cmd_finalize_compile(args: argparse.Namespace) -> int:
    from recon.model import read_json, write_json

    report = read_json(args.report)
    result = {"carrier": args.carrier, "contract_file": report["contract_file"], "agreed": report["agree"],
              "card_a": str(Path(args.a).resolve()), "card_b": str(Path(args.b).resolve()),
              "compare_report": str(Path(args.report).resolve()), "run_dir": args.run_dir}
    write_json(args.out, result)
    print(json.dumps({"agreed": report["agree"]}), file=sys.stderr)
    return 0


def _cmd_rates_check(args: argparse.Namespace) -> int:
    from recon.model import read_json
    from recon.stages import rates_check

    report = rates_check(read_json(args.results), read_json(args.carriers)["carriers"], _repo_root(),
                         Path(args.store), Path(args.out_dir))
    _flowstate_var(args.flowstate_var, report["verdict"])
    print(json.dumps({"verdict": report["verdict"],
                      "contracts": {c["carrier"]: c["verdict"] for c in report["contracts"]}}), file=sys.stderr)
    return 0


def _cmd_check_review(args: argparse.Namespace) -> int:
    """Agent self-check, and the in-child gate when --feedback/--flowstate-var are given."""
    from recon.model import read_json
    from recon.review import check_review

    batch = read_json(args.batch)
    try:
        output = read_json(args.output)
        problems = check_review(batch, output)
    except (OSError, ValueError) as exc:
        problems = [f"cannot read review output {args.output}: {exc}"]
    if args.feedback:
        Path(args.feedback).parent.mkdir(parents=True, exist_ok=True)
        Path(args.feedback).write_text(
            ("All checks passed.\n" if not problems else
             "The review output was rejected by the reviewer contract check:\n- " + "\n- ".join(problems) + "\n"),
            encoding="utf-8")
    _flowstate_var(args.flowstate_var, "no" if problems else "yes")
    if problems:
        _print_problems("review output fails the reviewer contract", problems)
        return 0 if args.flowstate_var else 2
    print(json.dumps({"review_ok": True}), file=sys.stderr if args.flowstate_var else sys.stdout)
    return 0


def _cmd_finalize_review(args: argparse.Namespace) -> int:
    from recon.stages import finalize_review

    result = finalize_review(Path(args.batch), [Path(p) for p in args.attempts], Path(args.out))
    print(json.dumps({k: result[k] for k in ("status", "batch_id")}), file=sys.stderr)
    return 0


def _cmd_describe_card(args: argparse.Namespace) -> int:
    from recon.model import read_json
    from recon.ratecard import strip_metadata
    from recon.stages import describe_card

    card = read_json(args.card)
    print(describe_card(strip_metadata(card.get("card", card))))
    return 0


def _cmd_merge_fallback(args: argparse.Namespace) -> int:
    from recon.model import read_json
    from recon.stages import merge_fallback

    carriers = [c["carrier"] for c in read_json(args.carriers)["carriers"]]
    problems = merge_fallback(read_json(args.fallback), Path(args.work), carriers)
    if problems:
        _print_problems("fallback parse rejected", problems)
        return 2
    print(json.dumps({"merged": True}))
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

    p = sub.add_parser("reconcile", help="match, price and check every line (deterministic)")
    p.add_argument("--work", required=True, help="directory holding documents.json and lines.jsonl")
    p.add_argument("--shipments", required=True, help="shipments.json (ground truth)")
    p.add_argument("--cards", required=True, help="JSON object {carrier: approved rate card}")
    p.add_argument("--out", required=True, help="reconciled.json to write")
    p.set_defaults(func=_cmd_reconcile)

    p = sub.add_parser("plan-review", help="batch non-accept items for review agents")
    p.add_argument("--reconciled", required=True)
    p.add_argument("--carriers", default="config/carriers.json")
    p.add_argument("--as-of", required=True, help="ISO date memos judge deadlines against")
    p.add_argument("--batch-size", type=int, default=12)
    p.add_argument("--out-dir", required=True)
    p.add_argument("--flowstate-var", help="also print FLOWSTATE_OUTPUT_<name>=<jobs>")
    p.add_argument("--flowstate-count-var", help="also print FLOWSTATE_OUTPUT_<name>=none|some")
    p.set_defaults(func=_cmd_plan_review)

    p = sub.add_parser("review-check", help="enforce the reviewer contract (D7) on every batch")
    p.add_argument("--batches-dir", required=True)
    p.add_argument("--reviews", required=True, help="JSON file mapping batch_id to review output path")
    p.add_argument("--feedback", required=True, help="where to write per-batch problems")
    p.set_defaults(func=_cmd_review_check)

    p = sub.add_parser("assemble", help="build the report and memos from reconciliation and reviews")
    p.add_argument("--reconciled", required=True)
    p.add_argument("--work", required=True, help="directory holding documents.json")
    p.add_argument("--batches-dir", required=True)
    p.add_argument("--reviews", required=True)
    p.add_argument("--out-dir", required=True)
    p.set_defaults(func=_cmd_assemble)

    p = sub.add_parser("validate-report", help="schema and invariant checks on an assembled report")
    p.add_argument("--out-dir", required=True, help="directory holding reconciliation-report.json and memos/")
    p.add_argument("--work", required=True)
    p.add_argument("--reconciled", required=True)
    p.add_argument("--batches-dir", required=True)
    p.set_defaults(func=_cmd_validate_report)

    p = sub.add_parser("publish", help="copy a validated report and memos to the repository root")
    p.add_argument("--source", required=True)
    p.add_argument("--target", default=".")
    p.add_argument("--manifest", help="JSON file with run metadata to record")
    p.set_defaults(func=_cmd_publish)

    p = sub.add_parser("check-card", help="run every rate-card gate on one card (agent self-check)")
    p.add_argument("--card", required=True)
    p.add_argument("--contract", required=True)
    p.add_argument("--carrier", required=True)
    p.add_argument("--vocabulary", help="vocabulary.json of allowed condition values")
    p.set_defaults(func=_cmd_check_card)

    p = sub.add_parser("plan-compile", help="one contract-compilation job per carrier")
    p.add_argument("--carriers", default="config/carriers.json")
    p.add_argument("--shipments", required=True)
    p.add_argument("--out-dir", required=True)
    p.add_argument("--flowstate-var")
    p.set_defaults(func=_cmd_plan_compile)

    p = sub.add_parser("compare-cards", help="in-child agreement gate for two extractions")
    p.add_argument("--a", required=True)
    p.add_argument("--b", required=True)
    p.add_argument("--contract", required=True)
    p.add_argument("--carrier", required=True)
    p.add_argument("--vocabulary")
    p.add_argument("--out", required=True)
    p.add_argument("--flowstate-var")
    p.set_defaults(func=_cmd_compare_cards)

    p = sub.add_parser("finalize-compile", help="record a compile child's final cards")
    for name in ("--carrier", "--a", "--b", "--report", "--out", "--run-dir"):
        p.add_argument(name, required=True)
    p.set_defaults(func=_cmd_finalize_compile)

    p = sub.add_parser("rates-check", help="D1 verdict across contracts; writes the cards to price with")
    p.add_argument("--results", required=True, help="JSON file mapping branch → compile result path")
    p.add_argument("--carriers", default="config/carriers.json")
    p.add_argument("--store", default="rate-cards/approved")
    p.add_argument("--out-dir", required=True)
    p.add_argument("--flowstate-var")
    p.set_defaults(func=_cmd_rates_check)

    p = sub.add_parser("check-review", help="run the reviewer contract check on one batch's output")
    p.add_argument("--batch", required=True)
    p.add_argument("--output", required=True)
    p.add_argument("--feedback", help="write the result for a retry prompt")
    p.add_argument("--flowstate-var", help="print FLOWSTATE_OUTPUT_<name>=yes|no and exit 0")
    p.set_defaults(func=_cmd_check_review)

    p = sub.add_parser("finalize-review", help="settle a review child: latest passing attempt or failure")
    p.add_argument("--batch", required=True)
    p.add_argument("--out", required=True)
    p.add_argument("attempts", nargs="+")
    p.set_defaults(func=_cmd_finalize_review)

    p = sub.add_parser("describe-card", help="plain-text reading of a rate card or approved snapshot")
    p.add_argument("--card", required=True)
    p.set_defaults(func=_cmd_describe_card)

    p = sub.add_parser("merge-fallback", help="check agent-parsed documents and add them to the work files")
    p.add_argument("--fallback", required=True)
    p.add_argument("--work", required=True)
    p.add_argument("--carriers", default="config/carriers.json")
    p.set_defaults(func=_cmd_merge_fallback)

    p = sub.add_parser("diff-runs", help="compare amounts, dispositions and membership of two runs")
    p.add_argument("a")
    p.add_argument("b")
    p.set_defaults(func=_cmd_diff_runs)

    args = parser.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())

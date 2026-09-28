#!/usr/bin/env bash
# Publish the validated report and memos to the repository root, with a manifest tying them to this run.
source "$(dirname "$0")/common.sh"
MANIFEST="${FLOWSTATE_VAR_manifest_path:?}"
"$PY" - "$MANIFEST" <<'PYEOF'
import json, os, subprocess, sys
from pathlib import Path
env = os.environ
inventory = json.loads(Path(env["FLOWSTATE_VAR_inventory_path"]).read_text())
rates_dir = Path(env.get("FLOWSTATE_VAR_rates_report_2") or env["FLOWSTATE_VAR_rates_report"]).parent
cards = json.loads((rates_dir / "rate-cards.json").read_text())
store = Path(env["FLOWSTATE_VAR_approved_store"])
manifest = {
    "flow": "freight-recon",
    "run_dir": env["FLOWSTATE_RUN_DIR"],
    "as_of_date": env["FLOWSTATE_VAR_as_of_date"],
    "git_head": subprocess.run(["git", "rev-parse", "HEAD"], capture_output=True, text=True).stdout.strip(),
    "inputs": {f["file"]: f["sha256"] for f in inventory["files"]},
    "rate_cards": {c: {"contract_file": card["contract_file"], "contract_sha256": card["contract_sha256"],
                       "snapshot": str(store / c / f"{card['contract_sha256']}.json")}
                   for c, card in sorted(cards.items())},
}
Path(sys.argv[1]).write_text(json.dumps(manifest, indent=2, sort_keys=True) + "\n")
PYEOF
recon publish --source "$(dirname "${FLOWSTATE_VAR_report_path:?}")" --target "${FLOWSTATE_VAR_publish_dir:?}" \
  --manifest "$MANIFEST" >&2

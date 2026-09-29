"""The flow definitions load, validate, and stay in sync with the recon package."""
import json
import os
import sys
import unittest
from pathlib import Path

from tests.helpers import ROOT

sys.path.insert(0, str(ROOT / "orchestrator" / "lib"))
from flowstate.parser import load_flow  # noqa: E402
from flowstate.validate import validate_graph  # noqa: E402

FLOWS = ("freight-recon", "recon-compile-rates", "recon-review")


class FlowDefinitionTest(unittest.TestCase):
    def test_flows_load_and_validate(self):
        for name in FLOWS:
            with self.subTest(name):
                flow = load_flow(ROOT / "factory" / "flows" / name / f"{name}.dot")
                result = validate_graph(flow.graph, flow=flow)
                self.assertEqual(result.errors, [])

    def test_scripts_are_executable(self):
        for name in FLOWS:
            for script in (ROOT / "factory" / "flows" / name / "scripts").iterdir():
                if script.name != "common.sh":
                    self.assertTrue(os.access(script, os.X_OK), script)

    def test_definitions_match_recon_schemas(self):
        pairs = [
            (ROOT / "factory/flows/recon-compile-rates/definitions/rate-card.json", ROOT / "recon/ratecard/schema.json"),
            (ROOT / "factory/flows/recon-review/definitions/review-output.json", ROOT / "recon/schemas/review-output.json"),
            (ROOT / "factory/flows/freight-recon/definitions/report.json", ROOT / "report.schema.json"),
        ]
        for flow_copy, source in pairs:
            with self.subTest(flow_copy.name):
                self.assertEqual(json.loads(flow_copy.read_text()), json.loads(source.read_text()),
                                 f"{flow_copy} is out of sync with {source}; copy it again")


if __name__ == "__main__":
    unittest.main()

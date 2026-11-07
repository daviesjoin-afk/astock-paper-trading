import json
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest

from worker import build_fixture_proposal, emit


class ContractTests(unittest.TestCase):
    def test_fixture_is_reproducible(self):
        self.assertEqual(build_fixture_proposal(), build_fixture_proposal())

    def test_proposal_is_versioned_and_does_not_set_order_quantity(self):
        value = build_fixture_proposal()
        self.assertEqual(3, value["schema_version"])
        self.assertEqual("buy", value["signal"]["side"])
        self.assertNotIn("quantity", value["signal"])
        self.assertNotIn("cash", value)
        self.assertNotIn("amount", value["signal"])

    def test_proposal_references_separately_ingested_evidence_without_prices(self):
        value = build_fixture_proposal()
        self.assertNotIn("quote", value)
        self.assertEqual({"source", "quote_id"}, set(value["quote_ref"]))
        fixture = Path(__file__).resolve().parents[1] / "fixtures" / "quote.json"
        quote = json.loads(fixture.read_text(encoding="utf-8"))["quote"]
        self.assertEqual(value["quote_ref"],
                         {key: quote[key] for key in ("source", "quote_id")})

    def test_emit_is_valid_json(self):
        with TemporaryDirectory() as folder:
            target = Path(folder) / "signals" / "fixture.json"
            emit(build_fixture_proposal(), target)
            self.assertEqual(build_fixture_proposal(),
                             json.loads(target.read_text(encoding="utf-8")))


if __name__ == "__main__":
    unittest.main()

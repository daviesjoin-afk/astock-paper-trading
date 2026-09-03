"""Ensure waitlist wording does not claim an automatic next-day retry."""
from pathlib import Path
import unittest


APP = Path(__file__).resolve().parents[1] / "frontend" / "app.js"
INDEX = Path(__file__).resolve().parents[1] / "frontend" / "index.html"


class FrontendCapacityStatusTests(unittest.TestCase):
    def test_deferred_capacity_is_described_as_re_rankable_waitlist(self):
        source = APP.read_text(encoding="utf-8")
        self.assertIn("deferred_capacity:['pending','容量等待重排']", source)
        self.assertNotIn("deferred_capacity:['pending','次日重新筛选']", source)

    def test_cache_key_is_bumped_with_capacity_semantics(self):
        self.assertIn("risk-symbol-name-v6", INDEX.read_text(encoding="utf-8"))


if __name__ == "__main__":
    unittest.main()

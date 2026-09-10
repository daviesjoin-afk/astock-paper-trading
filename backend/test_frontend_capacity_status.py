"""Ensure waitlist wording does not claim an automatic next-day retry."""
import frontend_sources
from pathlib import Path
import unittest

import build_info as BI


SOURCE = frontend_sources.source_text  # PR-55：源已拆到 frontend/src/**
INDEX = Path(__file__).resolve().parents[1] / "frontend" / "index.html"


class FrontendCapacityStatusTests(unittest.TestCase):
    def test_deferred_capacity_is_described_as_re_rankable_waitlist(self):
        source = SOURCE()
        self.assertIn("deferred_capacity:['pending','容量等待重排']", source)
        self.assertNotIn("deferred_capacity:['pending','次日重新筛选']", source)

    def test_cache_key_is_bumped_with_capacity_semantics(self):
        # PR-49：cache-bust 值统一引用规范 build id，不再逐次硬编码日期串。
        index = INDEX.read_text(encoding="utf-8")
        self.assertIn("/app.js?v=" + BI.APP_BUILD_ID, index)
        self.assertIn("/app.css?v=" + BI.APP_BUILD_ID, index)


if __name__ == "__main__":
    unittest.main()

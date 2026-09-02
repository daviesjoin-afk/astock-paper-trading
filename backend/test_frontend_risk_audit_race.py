from pathlib import Path
import unittest


APP = Path(__file__).resolve().parents[1] / "frontend" / "app.js"
INDEX = Path(__file__).resolve().parents[1] / "frontend" / "index.html"


class FrontendRiskAuditRaceTests(unittest.TestCase):
    def test_risk_audit_renderer_accepts_empty_payload(self):
        source = APP.read_text(encoding="utf-8")
        self.assertIn("d=(d&&typeof d==='object')?d:{};", source)

    def test_activity_tab_late_switch_fetches_audit_payload(self):
        source = APP.read_text(encoding="utf-8")
        self.assertIn("auditRequest||api('/api/paper/risk-audit?limit=160')", source)

    def test_frontend_cache_key_includes_risk_audit_fix(self):
        self.assertIn("risk-symbol-name-v6", INDEX.read_text(encoding="utf-8"))


if __name__ == "__main__":
    unittest.main()

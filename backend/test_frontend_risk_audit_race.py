from pathlib import Path
import unittest


APP = Path(__file__).resolve().parents[1] / "frontend" / "app.js"
INDEX = Path(__file__).resolve().parents[1] / "frontend" / "index.html"


class FrontendRiskAuditRaceTests(unittest.TestCase):
    def test_risk_audit_renderer_accepts_empty_payload(self):
        source = APP.read_text(encoding="utf-8")
        self.assertIn("d=(d&&typeof d==='object')?d:{};", source)

    def test_activity_tab_late_switch_fetches_audit_payload(self):
        """Activity 工作区渲染时必须保证能拿到审计数据。

        2026-09-08 起 audit 请求在并行启动之外多了一层 stale-while-revalidate
        缓存：优先用并行请求 / 上次缓存，两者皆无时仍会现场拉取，保证延迟
        切换到 activity 页时审计记录一定出现（而不是静默缺失）。
        """
        source = APP.read_text(encoding="utf-8")
        self.assertIn("auditDashboard=auditRequest||window._paperAuditCache", source)
        self.assertIn("auditDashboard=await api('/api/paper/risk-audit?limit=160')", source)
        self.assertIn("window._paperAuditCache=auditDashboard", source)

    def test_frontend_cache_key_includes_risk_audit_fix(self):
        self.assertIn("20260907-frontend-build-pipeline-v1", INDEX.read_text(encoding="utf-8"))


if __name__ == "__main__":
    unittest.main()

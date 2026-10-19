import frontend_sources
from pathlib import Path
import unittest

import build_info as BI


SOURCE = frontend_sources.source_text  # PR-55：源已拆到 frontend/src/**
INDEX = Path(__file__).resolve().parents[1] / "frontend" / "index.html"


class FrontendRiskAuditRaceTests(unittest.TestCase):
    def test_risk_audit_renderer_accepts_empty_payload(self):
        source = SOURCE()
        self.assertIn("d=(d&&typeof d==='object')?d:{};", source)

    def test_activity_tab_late_switch_fetches_audit_payload(self):
        """Activity 工作区渲染时必须保证能拿到审计数据。

        Activity 渲染委托给等待预取结果的分页加载器；完整交互由
        risk-audit-paging 的 Node 与浏览器回归覆盖，不能要求保留旧的
        “把 Promise 当数据”代码片段。
        """
        source = SOURCE()
        self.assertIn("await loadPaperAudit('current',auditRequest)", source)
        self.assertIn("var initial=prefetched?await prefetched:null", source)
        self.assertNotIn("renderPaperAudit(auditRequest)", source)

    def test_frontend_cache_key_includes_risk_audit_fix(self):
        # PR-49：cache-bust 值统一引用规范 build id，不再逐次硬编码日期串。
        index = INDEX.read_text(encoding="utf-8")
        self.assertIn("/app.js?v=" + BI.APP_BUILD_ID, index)
        self.assertIn("/app.css?v=" + BI.APP_BUILD_ID, index)


if __name__ == "__main__":
    unittest.main()

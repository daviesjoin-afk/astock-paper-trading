from pathlib import Path

PAPER = Path("backend/paper_trading.py")
TEST = Path("backend/test_paper_decision_audit_facade.py")

text = PAPER.read_text(encoding="utf-8")

import_anchor = "import paper_cycle_service as PCS\n"
if text.count(import_anchor) != 1:
    raise SystemExit(f"expected one paper_cycle_service import anchor, got {text.count(import_anchor)}")
if "import paper_decision_audit as PDA\n" not in text:
    text = text.replace(import_anchor, import_anchor + "import paper_decision_audit as PDA\n", 1)

start_marker = "# Decision records are deliberately kept inside the existing JSON payloads so\n"
end_marker = "\n\ndef _order_intent_payload(strategy_id, pick, *, asof_day=None, intended_session=None):\n"
if text.count(start_marker) != 1:
    raise SystemExit(f"expected one decision-audit start marker, got {text.count(start_marker)}")
if text.count(end_marker) != 1:
    raise SystemExit(f"expected one decision-audit end marker, got {text.count(end_marker)}")
start = text.index(start_marker)
end = text.index(end_marker, start)

replacement = '''# Decision records live behind a compatibility facade so existing callers keep
# the historical private helper names while the serializer itself is isolated
# in :mod:`paper_decision_audit`.  Runtime-only state stays explicit here: the
# extracted module performs no database writes and no network I/O.
DECISION_SNAPSHOT_VERSION = PDA.DECISION_SNAPSHOT_VERSION


def _snapshot_safe(value):
    return PDA.snapshot_safe(value)


def _snapshot_date(value):
    return PDA._snapshot_date(value)


def _snapshot_first(mapping, *keys):
    return PDA._snapshot_first(mapping, *keys)


def _snapshot_kline(kline, asof_date=None):
    return PDA._snapshot_kline(kline, asof_date)


def _snapshot_factor_evidence(payload):
    return PDA._snapshot_factor_evidence(payload)


def _decision_snapshot(
    payload=None, *, account_id=None, code=None, side=None, decision=None,
    reason=None, asof_date=None, quote=None, kline=None, news=None,
    final_score=None, decision_at=None,
):
    """Compatibility facade for the extracted point-in-time audit serializer."""
    return PDA.build_decision_snapshot(
        payload,
        account_id=account_id,
        code=code,
        side=side,
        decision=decision,
        reason=reason,
        asof_date=asof_date,
        quote=quote,
        kline=kline,
        news=news,
        final_score=final_score,
        decision_at=decision_at,
        kline_loader=_completed_kline,
        news_scan_meta=_NEWS_SCAN_META,
        risk_version=RISK_VERSION,
        now_fn=_now,
    )


def _with_decision_snapshot(payload=None, **kwargs):
    """Compatibility facade that preserves caller payload and runtime evidence."""
    return PDA.with_decision_snapshot(
        payload,
        **kwargs,
        kline_loader=_completed_kline,
        news_scan_meta=_NEWS_SCAN_META,
        risk_version=RISK_VERSION,
        now_fn=_now,
    )
'''

text = text[:start] + replacement + text[end:]
PAPER.write_text(text, encoding="utf-8")

TEST.write_text('''# -*- coding: utf-8 -*-
import os
import sys
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import paper_decision_audit as audit
import paper_trading as paper


class PaperDecisionAuditFacadeTests(unittest.TestCase):
    def test_snapshot_version_is_owned_by_extracted_module(self):
        self.assertEqual(paper.DECISION_SNAPSHOT_VERSION, audit.DECISION_SNAPSHOT_VERSION)

    def test_simple_helpers_delegate_to_extracted_module(self):
        sentinel = object()
        with mock.patch.object(audit, "snapshot_safe", return_value=sentinel) as fn:
            self.assertIs(paper._snapshot_safe({"x": 1}), sentinel)
            fn.assert_called_once_with({"x": 1})
        with mock.patch.object(audit, "_snapshot_date", return_value=sentinel) as fn:
            self.assertIs(paper._snapshot_date("2026-09-11"), sentinel)
            fn.assert_called_once_with("2026-09-11")
        with mock.patch.object(audit, "_snapshot_first", return_value=sentinel) as fn:
            self.assertIs(paper._snapshot_first({"a": 1}, "a", "b"), sentinel)
            fn.assert_called_once_with({"a": 1}, "a", "b")
        with mock.patch.object(audit, "_snapshot_kline", return_value=sentinel) as fn:
            self.assertIs(paper._snapshot_kline("frame", "2026-09-11"), sentinel)
            fn.assert_called_once_with("frame", "2026-09-11")
        with mock.patch.object(audit, "_snapshot_factor_evidence", return_value=sentinel) as fn:
            self.assertIs(paper._snapshot_factor_evidence({"pick": {}}), sentinel)
            fn.assert_called_once_with({"pick": {}})

    def test_decision_snapshot_injects_runtime_only_dependencies(self):
        payload = {"pick": {"code": "000001"}}
        quote = {"price": 12.3}
        kline = object()
        news = []
        result = {"snapshot": True}
        scan_meta = {"observed_at": "2026-09-11T10:00:00", "stale": False, "error": None}
        with (
            mock.patch.object(audit, "build_decision_snapshot", return_value=result) as build,
            mock.patch.object(paper, "_completed_kline") as loader,
            mock.patch.object(paper, "_now") as clock,
            mock.patch.object(paper, "_NEWS_SCAN_META", scan_meta),
        ):
            actual = paper._decision_snapshot(
                payload,
                account_id="tq_breakout",
                code="000001",
                side="buy",
                decision="approved_signal",
                reason="fixture",
                asof_date="2026-09-11",
                quote=quote,
                kline=kline,
                news=news,
                final_score=0.88,
                decision_at="2026-09-11 10:01:03",
            )
        self.assertIs(actual, result)
        build.assert_called_once_with(
            payload,
            account_id="tq_breakout",
            code="000001",
            side="buy",
            decision="approved_signal",
            reason="fixture",
            asof_date="2026-09-11",
            quote=quote,
            kline=kline,
            news=news,
            final_score=0.88,
            decision_at="2026-09-11 10:01:03",
            kline_loader=loader,
            news_scan_meta=scan_meta,
            risk_version=paper.RISK_VERSION,
            now_fn=clock,
        )

    def test_payload_enrichment_injects_same_runtime_dependencies(self):
        payload = {"decision_name": "fixture"}
        result = {"decision_name": "fixture", "decision_snapshot": {"ok": True}}
        scan_meta = {"observed_at": None, "stale": True, "error": "fixture"}
        with (
            mock.patch.object(audit, "with_decision_snapshot", return_value=result) as enrich,
            mock.patch.object(paper, "_completed_kline") as loader,
            mock.patch.object(paper, "_now") as clock,
            mock.patch.object(paper, "_NEWS_SCAN_META", scan_meta),
        ):
            actual = paper._with_decision_snapshot(
                payload,
                account_id="trend_pullback",
                asof_date="2026-09-11",
            )
        self.assertIs(actual, result)
        enrich.assert_called_once_with(
            payload,
            account_id="trend_pullback",
            asof_date="2026-09-11",
            kline_loader=loader,
            news_scan_meta=scan_meta,
            risk_version=paper.RISK_VERSION,
            now_fn=clock,
        )


if __name__ == "__main__":
    unittest.main()
''', encoding="utf-8")

print("phase2 facade patch prepared")

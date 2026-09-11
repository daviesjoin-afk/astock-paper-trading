# -*- coding: utf-8 -*-
from __future__ import annotations

import ast
import datetime as dt
from pathlib import Path
import unittest
from unittest import mock

try:
    import paper_slot_service as PSS
except ImportError:
    from . import paper_slot_service as PSS


class _Conn:
    def __init__(self, name, events):
        self.name = name
        self.events = events

    def __enter__(self):
        self.events.append(f"db:{self.name}:enter")
        return self

    def __exit__(self, exc_type, exc, tb):
        self.events.append(f"db:{self.name}:exit")
        return False


class SlotContractTests(unittest.TestCase):
    def test_supported_slots_are_exactly_the_legacy_set(self):
        self.assertEqual(PSS.SUPPORTED_SLOTS, frozenset({
            "auction", "open", "risk", "close", "weekly-review", "intraday",
        }))

    def test_invalid_slot_keeps_legacy_error(self):
        with self.assertRaisesRegex(
            ValueError,
            "slot 必须是 auction、open、risk、close、weekly-review 或 intraday",
        ):
            PSS.validate_slot("unknown")


class PreflightTests(unittest.TestCase):
    def _factory(self, events):
        counter = {"n": 0}
        def factory():
            counter["n"] += 1
            return _Conn(str(counter["n"]), events)
        return factory

    def test_lifecycle_cleanup_precedes_dispatch(self):
        events = []
        day = dt.date(2026, 9, 11)
        with mock.patch.object(PSS.ELC, "expire_stale_signals", side_effect=lambda conn, asof_day: events.append("signals")), \
             mock.patch.object(PSS.ELC, "expire_stale_orders", side_effect=lambda conn: events.append("orders")), \
             mock.patch.object(PSS.EPD, "run_execution_dispatch", side_effect=lambda conn: events.append("dispatch")):
            result = PSS.run_preflight(
                db_factory=self._factory(events),
                audit=lambda *args: None,
                resolve_asof_day=lambda: day,
            )
        self.assertEqual(result["lifecycle"], "ok")
        self.assertEqual(result["dispatch"], "ok")
        self.assertEqual(result["asof_day"], "2026-09-11")
        self.assertLess(events.index("signals"), events.index("orders"))
        self.assertLess(events.index("orders"), events.index("dispatch"))

    def test_lifecycle_failure_is_audited_but_dispatch_still_runs(self):
        events = []
        audits = []
        with mock.patch.object(PSS.ELC, "expire_stale_signals", side_effect=RuntimeError("boom")), \
             mock.patch.object(PSS.EPD, "run_execution_dispatch", side_effect=lambda conn: events.append("dispatch")):
            result = PSS.run_preflight(
                db_factory=self._factory(events),
                audit=lambda conn, actor, event, detail: audits.append((actor, event, detail)),
                resolve_asof_day=lambda: dt.date(2026, 9, 11),
            )
        self.assertEqual(result["lifecycle"], "error")
        self.assertEqual(result["dispatch"], "ok")
        self.assertIn("dispatch", events)
        self.assertEqual(audits[0][1], "entry_lifecycle_error")
        self.assertIn("RuntimeError: boom", audits[0][2])

    def test_date_resolution_failure_is_audited_and_dispatch_still_runs(self):
        events = []
        audits = []
        resolver = mock.Mock(side_effect=ValueError("bad date"))
        with mock.patch.object(PSS.ELC, "expire_stale_signals") as signals, \
             mock.patch.object(PSS.EPD, "run_execution_dispatch", side_effect=lambda conn: events.append("dispatch")):
            result = PSS.run_preflight(
                db_factory=self._factory(events),
                audit=lambda conn, actor, event, detail: audits.append((event, detail)),
                resolve_asof_day=resolver,
            )
        self.assertEqual(result["lifecycle"], "error")
        self.assertIn("dispatch", events)
        signals.assert_not_called()
        self.assertEqual(audits[0][0], "entry_lifecycle_error")
        self.assertIn("ValueError: bad date", audits[0][1])

    def test_dispatch_failure_is_audited_and_not_raised(self):
        events = []
        audits = []
        with mock.patch.object(PSS.ELC, "expire_stale_signals", return_value=None), \
             mock.patch.object(PSS.ELC, "expire_stale_orders", return_value=None), \
             mock.patch.object(PSS.EPD, "run_execution_dispatch", side_effect=ValueError("bad dispatch")):
            result = PSS.run_preflight(
                db_factory=self._factory(events),
                audit=lambda conn, actor, event, detail: audits.append((event, detail)),
                resolve_asof_day=lambda: dt.date(2026, 9, 11),
            )
        self.assertEqual(result["dispatch"], "error")
        self.assertEqual(audits[0][0], "execution_dispatch_error")

    def test_audit_failure_is_swallowed(self):
        events = []
        with mock.patch.object(PSS.ELC, "expire_stale_signals", side_effect=RuntimeError("boom")), \
             mock.patch.object(PSS.EPD, "run_execution_dispatch", return_value=None):
            result = PSS.run_preflight(
                db_factory=self._factory(events),
                audit=mock.Mock(side_effect=RuntimeError("audit unavailable")),
                resolve_asof_day=lambda: dt.date(2026, 9, 11),
            )
        self.assertEqual(result["lifecycle"], "error")
        self.assertEqual(result["dispatch"], "ok")


class ArchitectureGuardTests(unittest.TestCase):
    def test_service_never_imports_paper_trading_or_api_layer(self):
        source = Path(PSS.__file__).read_text(encoding="utf-8")
        tree = ast.parse(source)
        imported = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                imported.update(alias.name.split(".", 1)[0] for alias in node.names)
            elif isinstance(node, ast.ImportFrom) and node.module:
                imported.add(node.module.split(".", 1)[0])
        self.assertNotIn("paper_trading", imported)
        self.assertNotIn("api_paper", imported)
        self.assertEqual(imported & {"entry_lifecycle", "execution_dispatch"}, {
            "entry_lifecycle", "execution_dispatch",
        })

    def test_facade_delegates_preflight_without_legacy_cleanup_calls(self):
        source = Path(__file__).with_name("paper_trading.py").read_text(encoding="utf-8")
        start = source.index("def run_slot(slot, asof_date=None, force=False):")
        end = source.index("\ndef ", start + 4) if "\ndef " in source[start + 4:] else len(source)
        body = source[start:end]
        self.assertIn("PSS.validate_slot(slot)", body)
        self.assertIn(
            "PSS.run_preflight(db_factory=_db, audit=_audit, resolve_asof_day=lambda: _date(asof_date))",
            body,
        )
        self.assertLess(body.index("PSS.run_preflight"), body.index("day = _date(asof_date)"))
        self.assertNotIn("ELC.expire_stale_signals", body)
        self.assertNotIn("EPD.run_execution_dispatch", body)


if __name__ == "__main__":
    unittest.main()

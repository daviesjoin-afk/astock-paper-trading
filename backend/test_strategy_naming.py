# -*- coding: utf-8 -*-
"""名号统一的验证：canonical 解析、持久化迁移幂等、以及防回生门禁。"""
import json
import pathlib
import sqlite3
import tempfile
import unittest

import db_migrate
import strategy_naming as NAMING

BACKEND = pathlib.Path(__file__).resolve().parent

#: 门禁白名单：旧名号只允许出现在隔离表本身，以及验证隔离表的这个测试文件。
_GATE_ALLOWED = {"strategy_naming.py", "test_strategy_naming.py"}


class NamingTests(unittest.TestCase):
    def test_legacy_names_resolve_to_canonical_names(self):
        for old, new in NAMING.migration_pairs():
            self.assertEqual(NAMING.canonical_model_family(old), new)
            self.assertTrue(NAMING.is_legacy_model_family(old))
            self.assertTrue(NAMING.is_canonical_model_family(new))

    def test_canonical_names_are_idempotent(self):
        for name in NAMING.MODEL_FAMILIES:
            self.assertEqual(NAMING.canonical_model_family(name), name)
            self.assertFalse(NAMING.is_legacy_model_family(name))

    def test_unknown_values_pass_through_without_guessing(self):
        self.assertEqual(NAMING.canonical_model_family("some_new_model"), "some_new_model")
        self.assertEqual(NAMING.canonical_model_family(""), "")
        self.assertEqual(NAMING.canonical_model_family(None), "")

    def test_the_alias_table_is_exactly_the_three_renamed_families(self):
        self.assertEqual(len(NAMING.migration_pairs()), 3)
        self.assertEqual(set(NAMING.MODEL_FAMILIES),
                         {new for _old, new in NAMING.migration_pairs()}
                         | {NAMING.CANONICAL_TREND_CONTINUATION})


class MigrationTests(unittest.TestCase):
    def setUp(self):
        self._folder = tempfile.TemporaryDirectory()
        self.path = f"{self._folder.name}/paper_trading.sqlite3"
        conn = sqlite3.connect(self.path)
        conn.execute("CREATE TABLE paper_accounts (id TEXT PRIMARY KEY, source_strategy TEXT,"
                     " params TEXT)")
        conn.commit()
        conn.close()

    def tearDown(self):
        self._folder.cleanup()

    def write(self, account_id, source_strategy, params):
        conn = sqlite3.connect(self.path)
        conn.execute("INSERT INTO paper_accounts VALUES(?,?,?)",
                     (account_id, source_strategy, json.dumps(params, ensure_ascii=False)))
        conn.commit()
        conn.close()

    def read(self, account_id):
        conn = sqlite3.connect(self.path)
        conn.row_factory = sqlite3.Row
        try:
            row = conn.execute("SELECT * FROM paper_accounts WHERE id=?",
                               (account_id,)).fetchone()
            return {"source_strategy": row["source_strategy"],
                    "params": json.loads(row["params"])}
        finally:
            conn.close()

    def migrate(self):
        conn = sqlite3.connect(self.path)
        try:
            stats = db_migrate.migrate_legacy_model_family_names(conn)
            conn.commit()
            return stats
        finally:
            conn.close()

    def test_legacy_source_strategy_is_rewritten(self):
        legacy = NAMING.LEGACY_MODEL_FAMILIES[0]
        canonical = NAMING.canonical_model_family(legacy)
        self.write("acc1", legacy, {})
        stats = self.migrate()
        self.assertEqual(stats["migrated_accounts"], 1)
        self.assertEqual(self.read("acc1")["source_strategy"], canonical)

    def test_the_overlay_model_family_is_rewritten_and_nothing_else_changes(self):
        legacy = NAMING.LEGACY_MODEL_FAMILIES[0]
        canonical = NAMING.canonical_model_family(legacy)
        params = {
            "adaptive_selection": {"model_family": legacy,
                                   "conditions": {"vol_surge_min": 1.5},
                                   "entry_score_delta": 0.01},
            "adaptive_selection_meta": {"model_family": legacy, "status": "active"},
            "unrelated": {"keep": "me"},
        }
        self.write("acc1", canonical, params)
        stats = self.migrate()
        self.assertEqual(stats["migrated_params"], 1)
        stored = self.read("acc1")["params"]
        self.assertEqual(stored["adaptive_selection"]["model_family"], canonical)
        self.assertEqual(stored["adaptive_selection_meta"]["model_family"], canonical)
        self.assertEqual(stored["adaptive_selection"]["conditions"], {"vol_surge_min": 1.5})
        self.assertEqual(stored["adaptive_selection"]["entry_score_delta"], 0.01)
        self.assertEqual(stored["unrelated"], {"keep": "me"})

    def test_the_migration_is_idempotent(self):
        legacy = NAMING.LEGACY_MODEL_FAMILIES[0]
        self.write("acc1", legacy, {"adaptive_selection": {"model_family": legacy}})
        first = self.migrate()
        second = self.migrate()
        self.assertEqual(first["migrated_accounts"], 1)
        self.assertEqual(second, {"migrated_accounts": 0, "migrated_params": 0})

    def test_already_canonical_rows_are_untouched(self):
        canonical = NAMING.CANONICAL_FIRST_BOARD_RELAY
        self.write("acc1", canonical, {"adaptive_selection": {"model_family": canonical}})
        self.assertEqual(self.migrate(), {"migrated_accounts": 0, "migrated_params": 0})

    def test_an_unknown_family_is_left_alone(self):
        self.write("acc1", "some_future_model", {"adaptive_selection":
                                                 {"model_family": "some_future_model"}})
        self.assertEqual(self.migrate(), {"migrated_accounts": 0, "migrated_params": 0})
        self.assertEqual(self.read("acc1")["source_strategy"], "some_future_model")

    def test_broken_json_does_not_abort_the_migration(self):
        conn = sqlite3.connect(self.path)
        conn.execute("INSERT INTO paper_accounts VALUES(?,?,?)",
                     ("broken", NAMING.LEGACY_MODEL_FAMILIES[0], "{not json"))
        conn.commit()
        conn.close()
        stats = self.migrate()
        self.assertEqual(stats["migrated_accounts"], 1)
        self.assertEqual(stats["migrated_params"], 0)


class RetroNameGateTests(unittest.TestCase):
    """防回生：旧模型族名不得再渗回任何生产代码。"""

    def _legacy_tokens(self):
        # 从隔离表派生，避免门禁自己成为第二处旧字面量。
        return {old: new for old, new in NAMING.migration_pairs()}

    def test_legacy_names_appear_only_in_the_quarantine_module(self):
        tokens = self._legacy_tokens()
        offenders = []
        for path in sorted(BACKEND.glob("*.py")):
            if path.name in _GATE_ALLOWED:
                continue
            text = path.read_text(encoding="utf-8")
            for old, new in tokens.items():
                if old in text:
                    offenders.append(f"{path.name}: 含有旧名号 {old!r}（应为 {new!r}）")
        self.assertEqual(offenders, [], "旧模型族名重新渗入：\n" + "\n".join(offenders))

    def test_no_production_module_has_drifted_from_the_alias_table(self):
        """每个 canonical 名都能在真实默认表里找到，且默认表里不再有旧名。"""
        import strategies as S

        for canonical in NAMING.MODEL_FAMILIES:
            self.assertIn(canonical, S.PAPER_CONDITION_DEFAULTS)
            self.assertIn(canonical, S.PAPER_WEIGHTS)
        for legacy in NAMING.LEGACY_MODEL_FAMILIES:
            self.assertNotIn(legacy, S.PAPER_CONDITION_DEFAULTS)
            self.assertNotIn(legacy, S.PAPER_WEIGHTS)


class RuntimeCompatibilityTests(unittest.TestCase):
    def test_run_strategy_accepts_a_legacy_family_name(self):
        import pandas as pd

        import strategies as S

        table = pd.DataFrame([{
            "name": "甲", "industry": "行业A", "price": 10.0, "pct": 3.0, "amount": 5e7,
            "turnover": 3.0, "main_pct": 1.0, "super_net_raw": 1e6, "mom5_raw": 0.02,
            "mom20_raw": 0.03, "mom60_raw": 0.05, "mom_short": 0.02, "volsurge": 1.5,
            "vol_surge_raw": 2.0, "rsi14_raw": 55.0, "flow": 1.0, "sentiment": 0.0,
            "above_boll_mid": True, "boll_mid_breakout": True, "ma20": 9.5, "ma60": 9.0,
        }], index=["600001"])
        canonical_name = NAMING.CANONICAL_FIRST_BOARD_RELAY
        legacy_name = next(old for old, new in NAMING.migration_pairs()
                           if new == canonical_name)
        canonical = S.run_strategy(canonical_name, table, topn=1, gate=None,
                                   first_board_codes=["600001"])
        legacy = S.run_strategy(legacy_name, table, topn=1, gate=None,
                                first_board_codes=["600001"])
        self.assertEqual(canonical.get("picks"), legacy.get("picks"))

    def test_selection_model_family_normalizes_a_legacy_persisted_value(self):
        import strategies as S

        legacy = next(old for old, new in NAMING.migration_pairs()
                      if new == NAMING.CANONICAL_FIRST_BOARD_RELAY)
        resolved = S.selection_model_family(
            "tq_breakout", {}, legacy)
        self.assertEqual(resolved, NAMING.CANONICAL_FIRST_BOARD_RELAY)


if __name__ == "__main__":
    unittest.main()

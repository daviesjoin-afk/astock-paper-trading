# -*- coding: utf-8 -*-
"""PR-1.1：持久化选股 overlay 的 legacy 动量单位兼容迁移测试。

与 ``test_factor_unit_contract.py``（PR #107，测**策略比较**的单位）不同，
本文件测的是**持久化状态兼容迁移**：

- Layer A 启动期数据迁移（``db_migrate`` v10）
- Layer B activation / rollback 写 live 之前的归一化
- Layer C 读路径内存级归一化（不写库）

核心不变式：只修**已证实的 legacy sentinel**
（``sentiment_pioneer.conditions.individual_mom5_min == 2.0``）。
其它任何值（``0.025`` / ``2.1`` / ``"2.0"`` / ``True``）一律保持原样。
"""
import ast
import copy
import json
import os
import sqlite3
import tempfile
import unittest

import adaptive_selection as A
import adaptive_selection_compat as C
import db_migrate
import factor_units as FU
import paper_trading as PT
import strategies as S

BACKEND = os.path.dirname(os.path.abspath(__file__))
COMPAT_SRC = os.path.join(BACKEND, "adaptive_selection_compat.py")

CANONICAL = 0.02
LEGACY = 2.0
SENTIMENT_PIONEER = "sentiment_pioneer"
MOM5_MIN_FIELD = "individual_mom5_min"
NOW = "2026-09-11 09:30:00"

_SENTIMENT_WEIGHTS = {"sentiment": 0.40, "flow": 0.25, "mom_short": 0.20, "volsurge": 0.15}


def _now():
    return NOW


def _legacy_conditions(mom5=LEGACY):
    return {
        "sentiment_min": -0.5,
        "individual_pct_min": 3.5,
        "individual_pct_max": 8.5,
        "individual_mom5_min": mom5,
        "enabled": {"sentiment_guard": True, "individual_strong": True},
    }


def _legacy_overlay(model_family="sentiment_pioneer", mom5=LEGACY):
    return {
        "model_family": model_family,
        "weights": dict(_SENTIMENT_WEIGHTS),
        "entry_score_delta": 0.012,
        "entry_paths": {"sector_heat": True, "individual_strong": True},
        "mutation_type": "add_individual_strong_path",
        "structure_reason": "板块热度不足但个股量价、资金、动量同时极强",
        "objectives": ["收益", "最大回撤"],
        "conditions": _legacy_conditions(mom5),
    }


def _legacy_params(model_family="sentiment_pioneer", mom5=LEGACY, meta_status="active"):
    """一份完整的 ``paper_accounts.params``，带若干"必须原样保留"的兄弟键。"""
    return {
        "foo": "keep-me",
        "entry_score_delta": 0.0,
        "adaptive_selection": _legacy_overlay(model_family, mom5),
        "adaptive_selection_meta": {
            "status": meta_status,
            "candidate_id": 4242,
            "version": "select-evo-20260910-4242",
            "effective_date": "2026-09-10",
            "approved_by": "human-ui",
            "source_regime": "momentum",
            "tier": "standard",
        },
        "custom_user_key": {"nested": [1, 2, 3]},
    }


def _expected_after_migration(params):
    expected = copy.deepcopy(params)
    expected["adaptive_selection"]["conditions"]["individual_mom5_min"] = CANONICAL
    return expected


# --------------------------------------------------------------------------
# 夹具
# --------------------------------------------------------------------------
class _PaperDb:
    """最小 paper 库：只含迁移/写入路径真正用到的表。

    ``paper_audit`` 刻意用**四列遗留 schema**，走 ``paper_repository.audit`` 的
    兼容分支，从而不依赖策略注册表种子数据。
    """

    def __init__(self):
        self.dir = tempfile.mkdtemp(prefix="pr11-paper-")
        self.path = os.path.join(self.dir, "paper_trading.sqlite3")
        self.conn = sqlite3.connect(self.path, timeout=30)
        self.conn.row_factory = sqlite3.Row
        self.conn.executescript(
            """
            CREATE TABLE paper_accounts(
                id TEXT PRIMARY KEY, params TEXT, version TEXT, updated_at TEXT,
                cycle_id INTEGER, style TEXT
            );
            CREATE TABLE paper_parameter_versions(
                id INTEGER PRIMARY KEY AUTOINCREMENT, cycle_id INTEGER, account_id TEXT,
                version TEXT, style TEXT, params TEXT, reason TEXT,
                effective_date TEXT, created_at TEXT
            );
            CREATE TABLE paper_audit(
                account_id TEXT, event TEXT, detail TEXT, created_at TEXT
            );
            """
        )
        self.conn.commit()

    def put_account(self, account_id, params, version="v1"):
        self.conn.execute(
            "INSERT OR REPLACE INTO paper_accounts(id,params,version,updated_at,cycle_id,style)"
            " VALUES(?,?,?,?,?,?)",
            (account_id, json.dumps(params, ensure_ascii=False), version, NOW, 1, "adaptive-selection"),
        )
        self.conn.commit()

    def raw_params(self, account_id):
        """未经解析的原始 params 文本（用于证明"没被写过"）。"""
        row = self.conn.execute(
            "SELECT params FROM paper_accounts WHERE id=?", (account_id,)
        ).fetchone()
        return row[0] if row else None

    def params(self, account_id):
        raw = self.raw_params(account_id)
        return json.loads(raw) if raw else None

    def audits(self, event=None):
        if event is None:
            rows = self.conn.execute("SELECT * FROM paper_audit ORDER BY rowid").fetchall()
        else:
            rows = self.conn.execute(
                "SELECT * FROM paper_audit WHERE event=? ORDER BY rowid", (event,)
            ).fetchall()
        return [dict(row) for row in rows]

    def drop_audit_table(self):
        self.conn.execute("DROP TABLE paper_audit")
        self.conn.commit()

    def close(self):
        try:
            self.conn.close()
        except Exception:
            pass


class _AdaptiveDb:
    """最小 adaptive 库（candidates + outbox + events），用生产 ``ensure_schema`` 建表。"""

    def __init__(self):
        self.conn = sqlite3.connect(":memory:", timeout=30)
        self.conn.row_factory = sqlite3.Row
        A.ensure_schema(self.conn)

    def put_candidate(self, account_id, candidate_params, status="eligible_structural_review",
                      regime="momentum", tier="standard"):
        self.conn.execute(
            """INSERT INTO adaptive_selection_candidates(
                   run_date,account_id,regime,model_id,baseline_params,candidate_params,
                   evidence,status,tier,reason,created_at,updated_at)
               VALUES(?,?,?,?,?,?,?,?,?,?,?,?)""",
            ("2026-09-10", account_id, regime, "sentiment_pioneer", "{}",
             json.dumps(candidate_params, ensure_ascii=False), "{}", status, tier,
             "test", NOW, NOW),
        )
        self.conn.commit()
        return int(self.conn.execute(
            "SELECT id FROM adaptive_selection_candidates ORDER BY id DESC LIMIT 1"
        ).fetchone()[0])

    def candidate_params(self, candidate_id):
        row = self.conn.execute(
            "SELECT candidate_params FROM adaptive_selection_candidates WHERE id=?",
            (candidate_id,),
        ).fetchone()
        return json.loads(row[0])

    def outbox_payload(self, candidate_id):
        row = self.conn.execute(
            "SELECT payload FROM adaptive_selection_outbox WHERE candidate_id=?",
            (candidate_id,),
        ).fetchone()
        return json.loads(row[0]) if row else None

    def close(self):
        try:
            self.conn.close()
        except Exception:
            pass


def _account_row(paper: _PaperDb, account_id):
    row = paper.conn.execute("SELECT * FROM paper_accounts WHERE id=?", (account_id,)).fetchone()
    return dict(row)


def _factor_table(mom5_raw=CANONICAL, pct=5.0, flow=1.0, vol_surge=2.0, sentiment=0.0):
    import pandas as pd
    return pd.DataFrame(
        [{
            "name": "测试股", "industry": "测试行业",
            "price": 10.0, "pct": pct,
            "amount": 5e7, "turnover": 3.0,
            "main_pct": 0.0, "super_net_raw": 0.0,
            "mom5_raw": mom5_raw, "mom20_raw": 0.0, "mom60_raw": 0.0,
            "mom_short": 0.0, "volsurge": 0.0, "vol_surge_raw": vol_surge,
            "rsi14_raw": 55.0, "flow": flow, "sentiment": sentiment,
            "above_boll_mid": False, "boll_mid_breakout": False,
            "ma20": 10.0, "ma60": 10.0,
        }],
        index=["T0"],
    )


# --------------------------------------------------------------------------
# A. 纯归一化
# --------------------------------------------------------------------------
class PureNormalizationTests(unittest.TestCase):
    """A1–A8：只有已证实的 legacy sentinel 会被改，且绝不原地修改入参。"""

    def _normalize(self, overlay):
        return C.normalize_legacy_selection_units(overlay)

    def test_a1_legacy_sentinel_is_normalized(self):
        overlay = _legacy_overlay()
        normalized, details = self._normalize(overlay)
        self.assertEqual(normalized["conditions"]["individual_mom5_min"], CANONICAL)
        self.assertEqual(len(details), 1)
        self.assertEqual(details[0]["old_value"], LEGACY)
        self.assertEqual(details[0]["new_value"], CANONICAL)
        self.assertEqual(details[0]["migration_id"], C.MIGRATION_ID)

    def test_a1b_json_integer_two_is_also_the_sentinel(self):
        overlay = _legacy_overlay()
        overlay["conditions"]["individual_mom5_min"] = 2          # JSON int
        normalized, details = self._normalize(overlay)
        self.assertEqual(normalized["conditions"]["individual_mom5_min"], CANONICAL)
        self.assertEqual(len(details), 1)

    def test_a2_input_object_is_not_mutated(self):
        overlay = _legacy_overlay()
        snapshot = json.dumps(overlay, sort_keys=True, ensure_ascii=False)
        normalized, _ = self._normalize(overlay)
        self.assertIsNot(normalized, overlay)
        self.assertIsNot(normalized["conditions"], overlay["conditions"])
        self.assertEqual(json.dumps(overlay, sort_keys=True, ensure_ascii=False), snapshot)

    def test_a3_canonical_value_is_left_alone(self):
        overlay = _legacy_overlay()
        overlay["conditions"]["individual_mom5_min"] = CANONICAL
        normalized, details = self._normalize(overlay)
        self.assertEqual(normalized["conditions"]["individual_mom5_min"], CANONICAL)
        self.assertEqual(details, [])

    def test_a4_user_value_003_is_left_alone(self):
        overlay = _legacy_overlay()
        overlay["conditions"]["individual_mom5_min"] = 0.03
        normalized, details = self._normalize(overlay)
        self.assertEqual(normalized["conditions"]["individual_mom5_min"], 0.03)
        self.assertEqual(details, [])

    def test_a5_two_point_one_is_not_guessed(self):
        overlay = _legacy_overlay()
        overlay["conditions"]["individual_mom5_min"] = 2.1
        normalized, details = self._normalize(overlay)
        self.assertEqual(normalized["conditions"]["individual_mom5_min"], 2.1)
        self.assertEqual(details, [])

    def test_a6_string_two_point_zero_is_not_converted(self):
        overlay = _legacy_overlay()
        overlay["conditions"]["individual_mom5_min"] = "2.0"
        normalized, details = self._normalize(overlay)
        self.assertEqual(normalized["conditions"]["individual_mom5_min"], "2.0")
        self.assertEqual(details, [])

    def test_a7_boolean_is_not_a_number(self):
        overlay = _legacy_overlay()
        overlay["conditions"]["individual_mom5_min"] = True
        normalized, details = self._normalize(overlay)
        self.assertIs(normalized["conditions"]["individual_mom5_min"], True)
        self.assertEqual(details, [])
        self.assertFalse(C.is_exact_legacy_mom5_min(True))

    def test_a8_other_models_are_never_touched(self):
        for model in ("bottom_reversal", "trend_continuation", "one_to_two"):
            with self.subTest(model=model):
                overlay = _legacy_overlay(model_family=model)
                normalized, details = self._normalize(overlay)
                self.assertEqual(normalized["conditions"]["individual_mom5_min"], LEGACY)
                self.assertEqual(details, [])

    def test_a9_missing_model_family_needs_explicit_evidence(self):
        overlay = _legacy_overlay()
        overlay.pop("model_family")
        normalized, details = self._normalize(overlay)
        self.assertEqual(details, [], "model 不明确时不得猜测转换")
        self.assertEqual(normalized["conditions"]["individual_mom5_min"], LEGACY)
        # 调用方若能证明生效模型（运行时读路径），才允许归一化。
        proven, proven_details = C.normalize_legacy_selection_units(
            overlay, effective_model="sentiment_pioneer",
        )
        self.assertEqual(len(proven_details), 1)
        self.assertEqual(proven["conditions"]["individual_mom5_min"], CANONICAL)

    def test_a10_non_dict_input_is_returned_unchanged(self):
        for bad in (None, [], "x", 3):
            with self.subTest(value=bad):
                normalized, details = C.normalize_legacy_selection_units(bad)
                self.assertEqual(details, [])
                self.assertEqual(normalized, bad)

    def test_a11_account_params_preserve_every_other_key(self):
        params = _legacy_params()
        normalized, details = C.normalize_legacy_account_params(params)
        self.assertEqual(len(details), 1)
        self.assertEqual(normalized, _expected_after_migration(params))
        self.assertEqual(params["adaptive_selection"]["conditions"]["individual_mom5_min"], LEGACY)

    def test_a12_canonical_target_comes_from_the_fixed_unit_contract(self):
        # 目标值来自**固定单位契约**（1 pct point == 0.01），而不是"当前策略默认值"：
        # 默认值日后若被改动，这条历史迁移的语义不应随之漂移。
        self.assertEqual(C.LEGACY_MOM5_MIN_PCT_POINTS, LEGACY)
        self.assertEqual(
            FU.pct_points_to_fraction(C.LEGACY_MOM5_MIN_PCT_POINTS), CANONICAL,
        )
        self.assertEqual(C.canonical_mom5_min_fraction(), CANONICAL)

    def test_a13_target_does_not_follow_a_changed_strategy_default(self):
        """即使策略默认值被临时改成 0.03，历史 2.0 仍必须迁成 0.02。

        迁移锚定固定单位契约；一次无关的默认值调整不得绑架历史数据的迁移结果。
        """
        default = S.PAPER_CONDITION_DEFAULTS[SENTIMENT_PIONEER][MOM5_MIN_FIELD]
        try:
            S.PAPER_CONDITION_DEFAULTS[SENTIMENT_PIONEER][MOM5_MIN_FIELD] = 0.03
            normalized, details = self._normalize(_legacy_overlay())
            self.assertEqual(
                normalized["conditions"][MOM5_MIN_FIELD], CANONICAL,
                "历史 2.0 必须仍按单位契约迁成 0.02，而不是跟随被改动的默认值",
            )
            self.assertEqual(len(details), 1)
            self.assertEqual(details[0]["new_value"], CANONICAL)
        finally:
            S.PAPER_CONDITION_DEFAULTS[SENTIMENT_PIONEER][MOM5_MIN_FIELD] = default

    def test_a14_compat_module_does_not_read_live_strategy_defaults(self):
        """窄 AST 守卫：兼容层的迁移目标必须锚定 ``factor_units`` 单位契约。

        只要兼容层还 import ``strategies`` 或引用 ``PAPER_CONDITION_DEFAULTS``，
        一次无关的默认值调整就能改变历史迁移结果 —— 这正是要禁止的耦合。
        """
        with open(COMPAT_SRC, encoding="utf-8") as handle:
            tree = ast.parse(handle.read())

        imported = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                imported.update(alias.name.split(".")[0] for alias in node.names)
            elif isinstance(node, ast.ImportFrom) and node.module:
                imported.add(node.module.split(".")[0])

        self.assertNotIn("strategies", imported, "迁移目标不得取自当前策略默认值")
        self.assertIn("factor_units", imported, "迁移目标必须经单位契约换算")
        referenced = {node.id for node in ast.walk(tree) if isinstance(node, ast.Name)}
        referenced |= {node.attr for node in ast.walk(tree) if isinstance(node, ast.Attribute)}
        self.assertNotIn("PAPER_CONDITION_DEFAULTS", referenced)


# --------------------------------------------------------------------------
# B. live DB 迁移
# --------------------------------------------------------------------------
class LiveMigrationTests(unittest.TestCase):
    def setUp(self):
        self.paper = _PaperDb()
        self.addCleanup(self.paper.close)

    def test_b1_exact_legacy_is_migrated_and_everything_else_is_byte_identical(self):
        original = _legacy_params()
        self.paper.put_account("sector_rotation", original)
        stats = C.migrate_legacy_selection_units(self.paper.conn, now_fn=_now)

        self.assertEqual(stats["migrated"], 1)
        self.assertEqual(stats["scanned"], 1)
        self.assertEqual(self.paper.params("sector_rotation"), _expected_after_migration(original))

    def test_b2_sibling_thresholds_and_metadata_untouched(self):
        self.paper.put_account("sector_rotation", _legacy_params())
        C.migrate_legacy_selection_units(self.paper.conn, now_fn=_now)
        stored = self.paper.params("sector_rotation")
        conditions = stored["adaptive_selection"]["conditions"]
        self.assertEqual(conditions["individual_pct_min"], 3.5)
        self.assertEqual(conditions["individual_pct_max"], 8.5)
        self.assertEqual(conditions["sentiment_min"], -0.5)
        self.assertEqual(conditions["enabled"], {"sentiment_guard": True, "individual_strong": True})
        self.assertEqual(stored["adaptive_selection"]["weights"], _SENTIMENT_WEIGHTS)
        self.assertEqual(stored["adaptive_selection"]["model_family"], "sentiment_pioneer")
        self.assertEqual(stored["adaptive_selection"]["entry_score_delta"], 0.012)
        self.assertEqual(stored["adaptive_selection_meta"]["version"], "select-evo-20260910-4242")
        self.assertEqual(stored["adaptive_selection_meta"]["effective_date"], "2026-09-10")
        self.assertEqual(stored["adaptive_selection_meta"]["candidate_id"], 4242)
        self.assertEqual(stored["custom_user_key"], {"nested": [1, 2, 3]})
        self.assertEqual(stored["foo"], "keep-me")

    def test_b3_audit_row_records_the_correction(self):
        self.paper.put_account("sector_rotation", _legacy_params())
        C.migrate_legacy_selection_units(self.paper.conn, now_fn=_now)
        rows = self.paper.audits(C.AUDIT_EVENT)
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["account_id"], "sector_rotation")
        detail = json.loads(rows[0]["detail"])
        self.assertEqual(detail["migration_id"], C.MIGRATION_ID)
        self.assertEqual(detail["field"], "individual_mom5_min")
        self.assertEqual(detail["old_value"], LEGACY)
        self.assertEqual(detail["new_value"], CANONICAL)
        self.assertEqual(detail["reason"], C.AUDIT_REASON)
        self.assertEqual(rows[0]["created_at"], NOW)

    def test_b4_audit_never_leaks_paths_or_secrets(self):
        self.paper.put_account("sector_rotation", _legacy_params())
        C.migrate_legacy_selection_units(self.paper.conn, now_fn=_now)
        blob = json.dumps(self.paper.audits(C.AUDIT_EVENT), ensure_ascii=False)
        for forbidden in ("/root", "sqlite", ".sqlite3", "password", "token", self.paper.dir):
            self.assertNotIn(forbidden, blob)

    def test_b5_no_usable_table_is_a_clean_noop(self):
        self.paper.conn.execute("DROP TABLE paper_accounts")
        self.paper.conn.commit()
        stats = C.migrate_legacy_selection_units(self.paper.conn, now_fn=_now)
        self.assertEqual(stats, {"scanned": 0, "migrated": 0, "skipped": 0, "unexpected": 0})


# --------------------------------------------------------------------------
# C. 幂等
# --------------------------------------------------------------------------
class IdempotencyTests(unittest.TestCase):
    def setUp(self):
        self.paper = _PaperDb()
        self.addCleanup(self.paper.close)

    def test_c1_second_run_migrates_nothing_and_repeats_no_audit(self):
        self.paper.put_account("sector_rotation", _legacy_params())
        first = C.migrate_legacy_selection_units(self.paper.conn, now_fn=_now)
        after_first = self.paper.raw_params("sector_rotation")
        second = C.migrate_legacy_selection_units(self.paper.conn, now_fn=_now)
        third = C.migrate_legacy_selection_units(self.paper.conn, now_fn=_now)

        self.assertEqual(first["migrated"], 1)
        self.assertEqual(second["migrated"], 0)
        self.assertEqual(third["migrated"], 0)
        self.assertEqual(second["skipped"], 1)
        self.assertEqual(len(self.paper.audits(C.AUDIT_EVENT)), 1, "audit 不得重复")
        self.assertEqual(self.paper.raw_params("sector_rotation"), after_first)
        self.assertEqual(
            self.paper.params("sector_rotation")["adaptive_selection"]["conditions"]["individual_mom5_min"],
            CANONICAL,
        )


# --------------------------------------------------------------------------
# D. 多账户隔离
# --------------------------------------------------------------------------
class MultiAccountIsolationTests(unittest.TestCase):
    def setUp(self):
        self.paper = _PaperDb()
        self.addCleanup(self.paper.close)

    def test_d1_only_the_targeted_legacy_overlay_changes(self):
        self.paper.put_account("sector_rotation", _legacy_params())
        self.paper.put_account("trend_pullback", _legacy_params(model_family="bottom_reversal"))
        self.paper.put_account("tq_breakout", _legacy_params(model_family="one_to_two", mom5=0.0))
        self.paper.put_account("user_custom_01", {
            "adaptive_selection": {"weights": {"value": 1.0}},
            "note": "user strategy params without the field",
        })
        untouched = {
            aid: self.paper.raw_params(aid)
            for aid in ("trend_pullback", "tq_breakout", "user_custom_01")
        }

        stats = C.migrate_legacy_selection_units(self.paper.conn, now_fn=_now)

        self.assertEqual(stats["scanned"], 4)
        self.assertEqual(stats["migrated"], 1)
        self.assertEqual(
            self.paper.params("sector_rotation")["adaptive_selection"]["conditions"]["individual_mom5_min"],
            CANONICAL,
        )
        for aid, raw in untouched.items():
            with self.subTest(account=aid):
                self.assertEqual(self.paper.raw_params(aid), raw, "非目标账户不得被改写")


# --------------------------------------------------------------------------
# E. active / inactive
# --------------------------------------------------------------------------
class ActiveStateTests(unittest.TestCase):
    """§26：任何将来可能重新变成 live 的 overlay 都不能保留错误单位。"""

    def setUp(self):
        self.paper = _PaperDb()
        self.addCleanup(self.paper.close)

    def test_e1_active_overlay_is_migrated(self):
        self.paper.put_account("sector_rotation", _legacy_params(meta_status="active"))
        C.migrate_legacy_selection_units(self.paper.conn, now_fn=_now)
        self.assertEqual(
            self.paper.params("sector_rotation")["adaptive_selection"]["conditions"]["individual_mom5_min"],
            CANONICAL,
        )

    def test_e2_inactive_overlay_is_also_migrated(self):
        params = _legacy_params(meta_status="shadow")
        self.paper.put_account("sector_rotation", params)
        stats = C.migrate_legacy_selection_units(self.paper.conn, now_fn=_now)
        self.assertEqual(stats["migrated"], 1)
        stored = self.paper.params("sector_rotation")
        self.assertEqual(
            stored["adaptive_selection"]["conditions"]["individual_mom5_min"], CANONICAL,
        )
        # meta 状态本身不改写
        self.assertEqual(stored["adaptive_selection_meta"]["status"], "shadow")


# --------------------------------------------------------------------------
# F. activation 边界
# --------------------------------------------------------------------------
class ActivationBoundaryTests(unittest.TestCase):
    def setUp(self):
        self.paper = _PaperDb()
        self.adaptive = _AdaptiveDb()
        self.addCleanup(self.paper.close)
        self.addCleanup(self.adaptive.close)

    def test_f1_legacy_candidate_cannot_pollute_live_state(self):
        self.paper.put_account("sector_rotation", {"adaptive_selection": {"weights": {}}})
        candidate_id = self.adaptive.put_candidate("sector_rotation", _legacy_overlay())

        A.apply_candidate(self.adaptive.conn, self.paper.path, candidate_id, _now,
                          approved_by="human-ui", effective_date="2026-09-11")

        live = self.paper.params("sector_rotation")["adaptive_selection"]
        self.assertEqual(live["conditions"]["individual_mom5_min"], CANONICAL)

    def test_f2_candidate_history_keeps_the_original_value(self):
        self.paper.put_account("sector_rotation", {"adaptive_selection": {"weights": {}}})
        candidate_id = self.adaptive.put_candidate("sector_rotation", _legacy_overlay())
        A.apply_candidate(self.adaptive.conn, self.paper.path, candidate_id, _now,
                          approved_by="human-ui", effective_date="2026-09-11")

        preserved = self.adaptive.candidate_params(candidate_id)
        self.assertEqual(preserved["conditions"]["individual_mom5_min"], LEGACY,
                         "历史 candidate 是审计事实，不得重写")

    def test_f3_outbox_payload_keeps_the_original_value(self):
        self.paper.put_account("sector_rotation", {"adaptive_selection": {"weights": {}}})
        candidate_id = self.adaptive.put_candidate("sector_rotation", _legacy_overlay())
        A.apply_candidate(self.adaptive.conn, self.paper.path, candidate_id, _now,
                          approved_by="human-ui", effective_date="2026-09-11")

        payload = self.adaptive.outbox_payload(candidate_id)
        self.assertEqual(payload["candidate"]["conditions"]["individual_mom5_min"], LEGACY)

    def test_f4_normalization_is_audited_at_activation(self):
        self.paper.put_account("sector_rotation", {"adaptive_selection": {"weights": {}}})
        candidate_id = self.adaptive.put_candidate("sector_rotation", _legacy_overlay())
        A.apply_candidate(self.adaptive.conn, self.paper.path, candidate_id, _now,
                          approved_by="human-ui", effective_date="2026-09-11")

        rows = self.paper.audits(C.NORMALIZED_EVENT)
        self.assertEqual(len(rows), 1)
        detail = json.loads(rows[0]["detail"])
        self.assertEqual(detail["old_value"], LEGACY)
        self.assertEqual(detail["new_value"], CANONICAL)
        self.assertEqual(detail["candidate_id"], candidate_id)

    def test_f5_replayed_pending_outbox_also_normalizes(self):
        """升级前入队、升级后才被消费的 legacy payload 也必须归一化。"""
        self.paper.put_account("sector_rotation", {"adaptive_selection": {"weights": {}}})
        candidate_id = self.adaptive.put_candidate("sector_rotation", _legacy_overlay())
        payload = {
            "candidate": _legacy_overlay(),
            "previous_account_params": json.dumps(
                {"adaptive_selection": {"weights": {}}}, ensure_ascii=False),
            "effective_date": "2026-09-11",
            "approved_by": "human-ui",
            "tier": "standard",
            "regime": "momentum",
            "reason": None,
        }
        self.adaptive.conn.execute(
            """INSERT INTO adaptive_selection_outbox(
                   candidate_id,account_id,operation,version,payload,status,attempts,created_at,updated_at)
               VALUES(?,?,?,?,?,'pending',0,?,?)""",
            (candidate_id, "sector_rotation", "apply", "legacy-pending-v1",
             json.dumps(payload, ensure_ascii=False), NOW, NOW),
        )
        self.adaptive.conn.commit()

        replayed = A.replay_pending_outbox(self.adaptive.conn, self.paper.path, _now)

        self.assertIn(candidate_id, replayed)
        live = self.paper.params("sector_rotation")["adaptive_selection"]
        self.assertEqual(live["conditions"]["individual_mom5_min"], CANONICAL)
        # payload 与 candidate 原始证据保持不变
        self.assertEqual(
            self.adaptive.outbox_payload(candidate_id)["candidate"]["conditions"]["individual_mom5_min"],
            LEGACY,
        )

    def test_f6_rollback_boundary_normalizes_restored_params(self):
        legacy_previous = json.dumps(_legacy_params(), ensure_ascii=False)
        self.paper.put_account("sector_rotation", {
            "adaptive_selection": _legacy_overlay(mom5=CANONICAL),
        }, version="v2")
        candidate_id = self.adaptive.put_candidate(
            "sector_rotation", _legacy_overlay(mom5=CANONICAL), status="eligible_structural_review")
        self.adaptive.conn.execute(
            "UPDATE adaptive_selection_candidates SET status='applied', previous_account_params=?"
            " WHERE id=?", (legacy_previous, candidate_id))
        self.adaptive.conn.commit()

        A.rollback(self.adaptive.conn, self.paper.path, "sector_rotation", _now, reason="测试回滚")

        self.assertEqual(
            self.paper.params("sector_rotation")["adaptive_selection"]["conditions"]["individual_mom5_min"],
            CANONICAL,
        )
        # 回滚点自身仍保留在 candidate 行里（历史不改写）
        row = self.adaptive.conn.execute(
            "SELECT previous_account_params FROM adaptive_selection_candidates WHERE id=?",
            (candidate_id,)).fetchone()
        self.assertEqual(
            json.loads(row[0])["adaptive_selection"]["conditions"]["individual_mom5_min"], LEGACY)


# --------------------------------------------------------------------------
# G. 运行时护栏（读路径，不写库）
# --------------------------------------------------------------------------
class RuntimeGuardTests(unittest.TestCase):
    def setUp(self):
        self.paper = _PaperDb()
        self.addCleanup(self.paper.close)

    def _account(self, params):
        self.paper.put_account("sector_rotation", params)
        return _account_row(self.paper, "sector_rotation")

    def test_g1_runtime_read_normalizes_in_memory(self):
        account = self._account(_legacy_params())
        selected = PT._adaptive_selection(account)
        self.assertEqual(selected["conditions"]["individual_mom5_min"], CANONICAL)

    def test_g2_runtime_read_has_no_database_side_effect(self):
        account = self._account(_legacy_params())
        before = self.paper.raw_params("sector_rotation")
        PT._adaptive_selection(account)
        PT._adaptive_selection(account)
        self.assertEqual(self.paper.raw_params("sector_rotation"), before,
                         "读路径绝不能写库")
        self.assertEqual(
            json.loads(before)["adaptive_selection"]["conditions"]["individual_mom5_min"], LEGACY)

    def test_g3_warning_is_bounded_to_once_per_account(self):
        import io
        from contextlib import redirect_stdout
        account = self._account(_legacy_params())
        PT._LEGACY_UNIT_WARNED.clear()
        buffer = io.StringIO()
        with redirect_stdout(buffer):
            PT._adaptive_selection(account)
            PT._adaptive_selection(account)
            PT._adaptive_selection(account)
        self.assertEqual(buffer.getvalue().count("legacy_selection_unit_normalized"), 1)

    def test_g4_inactive_overlay_is_not_returned_at_all(self):
        account = self._account(_legacy_params(meta_status="shadow"))
        self.assertEqual(PT._adaptive_selection(account), {})

    def test_g5_suspicious_values_never_crash_the_read_path(self):
        params = _legacy_params()
        for value in (2.1, 20.0, "2.0", True, None, float("nan")):
            with self.subTest(value=value):
                candidate = copy.deepcopy(params)
                candidate["adaptive_selection"]["conditions"]["individual_mom5_min"] = value
                account = self._account(candidate)
                selected = PT._adaptive_selection(account)
                self.assertIn("conditions", selected)


# --------------------------------------------------------------------------
# H. 真实业务效果
# --------------------------------------------------------------------------
class RuntimeBehaviourTests(unittest.TestCase):
    def setUp(self):
        self.paper = _PaperDb()
        self.addCleanup(self.paper.close)

    def _run(self, condition_overrides):
        return S.run_strategy(
            "sentiment_pioneer", _factor_table(), topn=5, gate=None,
            first_board_codes=None, condition_overrides=condition_overrides,
        )

    def test_h1_legacy_overlay_would_keep_the_path_dead(self):
        """负向对照：不做兼容处理时，+2% 的个股走不进 individual_strong。"""
        result = self._run(_legacy_conditions())
        self.assertEqual(len(result["picks"]), 1)
        self.assertEqual(result["picks"][0]["entry_path"], "sector_heat")

    def test_h2_after_compatibility_layer_the_path_is_alive(self):
        self.paper.put_account("sector_rotation", _legacy_params())
        account = _account_row(self.paper, "sector_rotation")
        selected = PT._adaptive_selection(account)
        result = self._run(selected["conditions"])
        self.assertEqual(len(result["picks"]), 1)
        self.assertEqual(result["picks"][0]["entry_path"], "individual_strong")

    def test_h3_migration_alone_also_restores_the_behaviour(self):
        self.paper.put_account("sector_rotation", _legacy_params())
        C.migrate_legacy_selection_units(self.paper.conn, now_fn=_now)
        account = _account_row(self.paper, "sector_rotation")
        selected = PT._adaptive_selection(account)
        result = self._run(selected["conditions"])
        self.assertEqual(result["picks"][0]["entry_path"], "individual_strong")


# --------------------------------------------------------------------------
# I. 不误改用户参数
# --------------------------------------------------------------------------
class UserValuePreservationTests(unittest.TestCase):
    def setUp(self):
        self.paper = _PaperDb()
        self.addCleanup(self.paper.close)

    def test_i1_user_value_is_not_reset_to_default(self):
        params = _legacy_params(mom5=0.025)
        self.paper.put_account("sector_rotation", params)
        before = self.paper.raw_params("sector_rotation")
        stats = C.migrate_legacy_selection_units(self.paper.conn, now_fn=_now)
        self.assertEqual(stats["migrated"], 0)
        self.assertEqual(stats["skipped"], 1)
        self.assertEqual(self.paper.raw_params("sector_rotation"), before)
        self.assertEqual(self.paper.audits(C.AUDIT_EVENT), [])

    def test_i2_suspicious_values_are_reported_but_never_converted(self):
        for value, expected in ((2.1, "unexpected"), (20.0, "unexpected"),
                                (1.5, "unexpected"), ("2.0", "unexpected"),
                                (True, "unexpected"), (0.5, "skipped"), (0.0, "skipped")):
            with self.subTest(value=value):
                paper = _PaperDb()
                try:
                    paper.put_account("sector_rotation", _legacy_params(mom5=value))
                    before = paper.raw_params("sector_rotation")
                    stats = C.migrate_legacy_selection_units(paper.conn, now_fn=_now)
                    self.assertEqual(stats["migrated"], 0)
                    self.assertEqual(stats[expected], 1)
                    self.assertEqual(paper.raw_params("sector_rotation"), before)
                finally:
                    paper.close()


# --------------------------------------------------------------------------
# J. 事务安全
# --------------------------------------------------------------------------
class TransactionAtomicityTests(unittest.TestCase):
    def setUp(self):
        self.paper = _PaperDb()
        self.addCleanup(self.paper.close)

    def test_j1_audit_failure_rolls_back_the_params_write(self):
        self.paper.put_account("sector_rotation", _legacy_params())
        before = self.paper.raw_params("sector_rotation")
        self.paper.drop_audit_table()          # 让 audit 写入必然失败

        self.paper.conn.execute("BEGIN")
        with self.assertRaises(sqlite3.OperationalError):
            C.migrate_legacy_selection_units(self.paper.conn, now_fn=_now)
        self.paper.conn.rollback()

        self.assertEqual(self.paper.raw_params("sector_rotation"), before,
                         "audit 失败时 params 不得已经落盘")

    def test_j2_commit_keeps_params_and_audit_together(self):
        self.paper.put_account("sector_rotation", _legacy_params())
        self.paper.conn.execute("BEGIN")
        C.migrate_legacy_selection_units(self.paper.conn, now_fn=_now)
        self.paper.conn.commit()
        self.assertEqual(
            self.paper.params("sector_rotation")["adaptive_selection"]["conditions"]["individual_mom5_min"],
            CANONICAL,
        )
        self.assertEqual(len(self.paper.audits(C.AUDIT_EVENT)), 1)


# --------------------------------------------------------------------------
# 启动路径（§15 / §41）
# --------------------------------------------------------------------------
class StartupPathTests(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp(prefix="pr11-boot-")
        self.path = os.path.join(self.dir, "paper_trading.sqlite3")

    def _fixture_db(self, params):
        conn = sqlite3.connect(self.path, timeout=30)
        conn.row_factory = sqlite3.Row
        conn.executescript(
            """
            CREATE TABLE paper_accounts(
                id TEXT PRIMARY KEY, params TEXT, version TEXT, updated_at TEXT,
                cycle_id INTEGER, style TEXT
            );
            CREATE TABLE paper_audit(
                account_id TEXT, event TEXT, detail TEXT, created_at TEXT
            );
            CREATE TABLE schema_version(
                db_name TEXT PRIMARY KEY, version INTEGER NOT NULL,
                applied_at TEXT NOT NULL, description TEXT
            );
            """
        )
        conn.execute(
            "INSERT INTO paper_accounts(id,params,version,updated_at,cycle_id,style) VALUES(?,?,?,?,?,?)",
            ("sector_rotation", json.dumps(params, ensure_ascii=False), "v1", NOW, 1, "x"),
        )
        # 把 v1–v9 预标记为已应用，只留 v10 待执行（fixture 不建那些表）。
        conn.execute(
            "INSERT INTO schema_version(db_name,version,applied_at,description) VALUES(?,?,?,?)",
            ("paper_trading", 9, NOW, "pre-marked"),
        )
        conn.commit()
        conn.close()

    def _read(self):
        conn = sqlite3.connect(self.path, timeout=30)
        conn.row_factory = sqlite3.Row
        try:
            params = conn.execute("SELECT params FROM paper_accounts WHERE id='sector_rotation'").fetchone()[0]
            audits = [dict(r) for r in conn.execute("SELECT * FROM paper_audit").fetchall()]
            version = conn.execute(
                "SELECT version FROM schema_version WHERE db_name='paper_trading'").fetchone()[0]
            return json.loads(params), audits, version
        finally:
            conn.close()

    def test_startup_path_contains_the_migration(self):
        entries = db_migrate.MIGRATIONS["paper_trading"]
        self.assertEqual(entries[-1][2], C.migrate_legacy_selection_units,
                         "v10 必须接在 db_migrate 这条 canonical 启动路径上")
        self.assertGreater(entries[-1][0], 9)

    def test_startup_path_migrates_then_self_marks(self):
        self._fixture_db(_legacy_params())
        db_migrate.migrate("paper_trading", path=self.path, backup=False)
        params, audits, version = self._read()
        self.assertEqual(params["adaptive_selection"]["conditions"]["individual_mom5_min"], CANONICAL)
        self.assertEqual(len(audits), 1)
        self.assertEqual(version, 10)

    def test_startup_path_second_boot_is_a_noop(self):
        self._fixture_db(_legacy_params())
        db_migrate.migrate("paper_trading", path=self.path, backup=False)
        _params, audits_after_first, _version = self._read()
        db_migrate.migrate("paper_trading", path=self.path, backup=False)
        params, audits_after_second, version = self._read()
        self.assertEqual(params["adaptive_selection"]["conditions"]["individual_mom5_min"], CANONICAL)
        self.assertEqual(audits_after_second, audits_after_first, "重复启动不得新增 audit")
        self.assertEqual(version, 10)

    def test_startup_path_missing_database_is_skipped(self):
        db_migrate.migrate("paper_trading", path=os.path.join(self.dir, "nope.sqlite3"), backup=False)


if __name__ == "__main__":  # pragma: no cover
    unittest.main()

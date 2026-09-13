# -*- coding: utf-8 -*-
"""纸盘账户声明边界契约测试（Extract Paper Account Specs Boundary）。

本文件锁住四件事：

1. **声明内容逐字段等价**：内置 spec / 风格 / 风险画像 / 保守回退与抽取前
   逐字段一致（下面全部是**冻结的 golden 字面量**，不由生产表反推，
   因此不是 `new == new` 的同义反复）；
2. **边界不被复制**：账户声明表的唯一实现在 ``paper_account_specs``；
   ``paper_trading`` 只保留兼容别名与唯一解析口 ``_spec_for``；
3. **口径分离**：声明层不回答执行资格 / 周期所有权 / 注册表生命周期；
   依赖方向单向 ``paper_trading → paper_account_specs → strategy_policies``；
4. **可变隔离**：查询访问器返回独立副本，调用方改动不污染声明真相。
"""
from __future__ import annotations

import ast
import os
import sqlite3
import tempfile
import unittest

import paper_account_specs as ACS
import paper_trading as PT
import strategy_policies as SPOL
import strategy_registry as SR
import strategy_runtime as SRT

BACKEND = os.path.dirname(os.path.abspath(__file__))
SPECS_PATH = os.path.join(BACKEND, "paper_account_specs.py")
PAPER_PATH = os.path.join(BACKEND, "paper_trading.py")

BUILTIN_IDS = (
    "tq_breakout",
    "trend_pullback",
    "sector_rotation",
    "reported_profit_breakout",
    "main_force_top10",
)

# 声明层允许的 import 根（stdlib + 声明式画像模块）。任何新增都要显式评审。
ALLOWED_SPECS_IMPORT_ROOTS = {"__future__", "copy", "strategy_policies"}

# 声明层明确禁止的依赖根：注册表真相、运行时、账本、网络、调度、执行。
FORBIDDEN_SPECS_IMPORT_ROOTS = {
    "paper_trading", "paper_storage", "paper_repository", "paper_schema_migrations",
    "strategy_registry", "strategy_runtime", "strategy_service", "user_strategy_participation",
    "order_intent", "execution_planner", "execution_dispatch", "entry_lifecycle",
    "paper_slot_service", "paper_runner", "main", "api_paper", "dashboard_queries",
    "manual_orders", "sqlite3", "requests", "urllib", "http", "socket", "threading",
    "subprocess", "asyncio", "fastapi", "starlette",
}

# 声明层不得出现的「副作用调用」属性名。
FORBIDDEN_SPECS_CALL_ATTRS = {
    "connect", "execute", "executemany", "executescript", "commit", "rollback",
    "urlopen", "request", "place_order", "submit_order", "send", "start", "run_slot",
    "transition", "create_user_definition", "save_definition", "bind_cycle_versions",
}

# 声明层不得出现的「权威真相」名字：生命周期 / 版本 / 就绪 / 周期所有权 / 执行资格。
FORBIDDEN_AUTHORITY_NAMES = {
    "lifecycle_status", "supports_new_cycle", "runtime_ready", "runtime_readiness",
    "active_ids", "checksum", "dsl_checksum", "cycle_id", "enabled_strategies",
    "execution_eligibility", "participant_ids", "user_participant_ids", "get_context",
}

# 账户 spec 表的「形状签名」：用内置 id 作键、值 dict 含 ≥3 个 spec 标记键。
SPEC_MARKER_KEYS = {"max_positions", "risk_profile", "cycle_days", "default_style"}
SPEC_TABLE_MIN_KEYS = 5
SPEC_TABLE_MIN_VALUES = 3

# ---------------------------------------------------------------------------
# 冻结 golden（抽取前逐字段快照；不由 ACCOUNT_SPECS 反推）
# ---------------------------------------------------------------------------
GOLDEN_ACCOUNT_SPECS = {
    'tq_breakout': {'name': '短线日内做T', 'mode': 'intraday_t', 'source_strategy': 'one_to_two', 'risk_profile': 'breakout', 'entry_model_name': '强势日内候选实时确认', 'max_factor_lag': 1, 'allowed_q': ('Q1', 'Q2'), 'default_style': 'strong', 'cycle_days': 5, 'hold_min': 1, 'hold_max': 8, 'max_positions': 3, 'max_weight': 0.32, 'max_exposure': 0.95, 'hard_stop': -0.05, 'trail_after': 0.04, 'trail_stop': 0.05, 'take_profit': [(0.08, 0.5)], 'min_t_score': 0.76, 'gap_q1': (-0.015, 0.035), 'max_open_runup_pct': 0.05, 'gap_q2': (-0.03, 0.07)},
    'trend_pullback': {'name': '趋势波段优选', 'mode': 'swing', 'source_strategy': 'bottom_reversal', 'risk_profile': 'trend', 'entry_model_name': '趋势回踩结构确认', 'max_factor_lag': 2, 'allowed_q': ('Q1', 'Q2', 'Q3'), 'default_style': 'pullback', 'cycle_days': 10, 'hold_min': 3, 'hold_max': 10, 'max_positions': 3, 'max_weight': 0.34, 'max_exposure': 0.95, 'hard_stop': -0.04, 'trail_after': 0.05, 'trail_stop': 0.06, 'take_profit': [(0.07, 0.3333333333333333), (0.12, 0.3333333333333333)], 'min_t_score': 0.72, 'gap_q1': (-0.015, 0.025), 'max_open_runup_pct': 0.015, 'gap_q2': (-0.025, 0.04)},
    'sector_rotation': {'name': '板块轮动先锋', 'mode': 'swing', 'source_strategy': 'sentiment_pioneer', 'risk_profile': 'sector', 'entry_model_name': '热点板块相对强度', 'max_factor_lag': 1, 'allowed_q': ('Q1', 'Q2'), 'default_style': 'sector', 'cycle_days': 5, 'hold_min': 2, 'hold_max': 7, 'max_positions': 3, 'max_weight': 0.32, 'max_exposure': 0.92, 'hard_stop': -0.045, 'trail_after': 0.045, 'trail_stop': 0.055, 'take_profit': [(0.06, 0.3333333333333333), (0.1, 0.3333333333333333)], 'min_t_score': 0.74, 'gap_q1': (-0.015, 0.03), 'max_open_runup_pct': 0.04, 'gap_q2': (-0.025, 0.06)},
    'reported_profit_breakout': {'name': '三日策略', 'mode': 'swing', 'source_strategy': 'reported_profit_breakout', 'risk_profile': 'core_quality', 'strategy_version': 'reported-profit-breakout-v1', 'entry_model_name': '已披露财报质量与突破确认', 'max_factor_lag': 2, 'allowed_q': ('Q1', 'Q2'), 'default_style': 'quality', 'cycle_days': 12, 'hold_min': 2, 'hold_max': 12, 'max_positions': 3, 'max_weight': 0.32, 'max_exposure': 0.9, 'hard_stop': -0.055, 'trail_after': 0.045, 'trail_stop': 0.06, 'take_profit': [(0.085, 0.4), (0.15, 0.35)], 'min_t_score': 0.74, 'gap_q1': (-0.02, 0.03), 'max_open_runup_pct': 0.02, 'gap_q2': (-0.03, 0.055), 'entry_pct_high': 6.5},
    'main_force_top10': {'name': '超强主力股', 'mode': 'swing', 'source_strategy': 'main_force_top10', 'risk_profile': 'main_force', 'strategy_version': 'main-force-top10-v1', 'entry_model_name': '主力持续性与微观成交确认', 'max_factor_lag': 1, 'allowed_q': ('Q1', 'Q2'), 'default_style': 'main_force', 'cycle_days': 8, 'hold_min': 1, 'hold_max': 8, 'max_positions': 3, 'max_weight': 0.34, 'max_exposure': 0.95, 'hard_stop': -0.05, 'trail_after': 0.05, 'trail_stop': 0.06, 'take_profit': [(0.1, 0.3333333333333333), (0.16, 0.3333333333333333)], 'min_t_score': 0.76, 'gap_q1': (-0.015, 0.04), 'max_open_runup_pct': 0.035, 'gap_q2': (-0.025, 0.07), 'entry_pct_high': 8.8, 'daily_candidate_limit': 10, 'ignition_zone': (3.5, 7.5), 'first_tranche_cap_pct': 0.12},
}

GOLDEN_STYLE_PROFILES = {
    'strong': {'name': '强势接力', 'source_strategy': 'one_to_two'},
    'pullback': {'name': '趋势回踩', 'source_strategy': 'bottom_reversal'},
    'sector': {'name': '板块轮动', 'source_strategy': 'sentiment_pioneer'},
    'quality': {'name': '三日策略', 'source_strategy': 'reported_profit_breakout'},
    'main_force': {'name': '超强主力股', 'source_strategy': 'main_force_top10'},
}

GOLDEN_RISK_PROFILES = {
    'breakout': {'name': '接力快进快出', 'max_weight': 0.32, 'max_exposure': 0.95, 'max_industry': 0.42, 'single_risk': 0.012, 'daily_loss': 0.035, 'drawdown': 0.11, 'cooldown_days': 2, 'min_cost_edge': 0.006},
    'trend': {'name': '趋势集中持有', 'max_weight': 0.34, 'max_exposure': 0.95, 'max_industry': 0.45, 'single_risk': 0.012, 'daily_loss': 0.04, 'drawdown': 0.13, 'cooldown_days': 3, 'min_cost_edge': 0.004},
    'sector': {'name': '热点轮动集中', 'max_weight': 0.32, 'max_exposure': 0.92, 'max_industry': 0.42, 'single_risk': 0.012, 'daily_loss': 0.04, 'drawdown': 0.12, 'cooldown_days': 2, 'min_cost_edge': 0.005},
    'core_quality': {'name': '三日策略独立风控', 'max_weight': 0.32, 'max_exposure': 0.9, 'max_industry': 0.38, 'single_risk': 0.012, 'daily_loss': 0.035, 'drawdown': 0.11, 'cooldown_days': 3, 'min_cost_edge': 0.005},
    'main_force': {'name': '主力持续性独立风控', 'max_weight': 0.34, 'max_exposure': 0.95, 'max_industry': 0.45, 'single_risk': 0.012, 'daily_loss': 0.04, 'drawdown': 0.12, 'cooldown_days': 2, 'min_cost_edge': 0.005},
}

GOLDEN_UNKNOWN_USER_SPEC = {
    'name': '未知策略账户',
    'source_strategy': 'strategy_dsl',
    'selection_mode': 'dsl',
    'mode': 'swing',
    'cycle_days': 8,
    'max_positions': 1,
    'max_weight': 0.1,
    'max_exposure': 0.35,
    'risk_profile': 'trend',
    'strategy_version': 'v0',
    'default_style': 'pullback',
    'entry_model_name': '未知策略账户',
    'max_factor_lag': 1,
    'entry_pct_high': 6.5,
    'gap_q2': (-0.025, 0.07),
    'hold_min': 1,
    'hold_max': 8,
    'hard_stop': -0.05,
    'trail_after': 0.05,
    'trail_stop': 0.06,
    'take_profit': [(0.1, 0.3333333333333333), (0.16, 0.3333333333333333)],
    'candidate_topn': 10,
    'lifecycle_stage': 'quarantined',
}

USER_RULE = {
    "op": "and",
    "args": [
        {"op": "gt", "left": {"op": "field", "name": "close"},
         "right": {"op": "indicator", "name": "ma", "window": 20}},
        {"op": "gt", "left": {"op": "field", "name": "volume"},
         "right": {"op": "mul", "left": {"op": "indicator", "name": "volume_mean", "window": 5},
                   "right": {"op": "const", "value": 1.2}}},
    ],
}


# ---------------------------------------------------------------------------
# AST 辅助
# ---------------------------------------------------------------------------
def _parse(path):
    with open(path, encoding="utf-8") as handle:
        return ast.parse(handle.read())


def _import_roots(tree):
    roots = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            roots.update(alias.name.split(".")[0] for alias in node.names)
        elif isinstance(node, ast.ImportFrom):
            if node.level == 0 and node.module:
                roots.add(node.module.split(".")[0])
    return roots


def _module_level_statements(tree):
    return [node for node in tree.body]


def _find_assign(tree, name):
    for node in ast.walk(tree):
        if isinstance(node, ast.Assign):
            for target in node.targets:
                if isinstance(target, ast.Name) and target.id == name:
                    return node
    return None


def _find_function(tree, name):
    for node in ast.walk(tree):
        if isinstance(node, ast.FunctionDef) and node.name == name:
            return node
    return None


def _dict_key_names(node):
    """dict 字面量的键名集合：字符串常量取原值，Name 取标识符。"""
    keys = set()
    for key in node.keys:
        if isinstance(key, ast.Constant) and isinstance(key.value, str):
            keys.add(key.value)
        elif isinstance(key, ast.Name):
            keys.add(key.id)
    return keys


def _find_account_spec_tables(tree):
    """找出所有「账户 spec 表形状」的 dict 字面量，返回 (lineno, key_count, spec_like)。"""
    found = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Dict):
            continue
        keys = _dict_key_names(node)
        if len(keys) < SPEC_TABLE_MIN_KEYS:
            continue
        if not {"tq_breakout", "trend_pullback", "sector_rotation"}.issubset(keys):
            continue
        spec_like = 0
        for value in node.values:
            if isinstance(value, ast.Dict) and len(SPEC_MARKER_KEYS & _dict_key_names(value)) >= 3:
                spec_like += 1
        if spec_like >= SPEC_TABLE_MIN_VALUES:
            found.append((node.lineno, len(keys), spec_like))
    return found


def _iter_backend_sources():
    for name in sorted(os.listdir(BACKEND)):
        if not name.endswith(".py") or name.startswith("test_"):
            continue
        yield name, os.path.join(BACKEND, name)


# ---------------------------------------------------------------------------
# 1) 内置 spec 逐字段契约
# ---------------------------------------------------------------------------
class BuiltinSpecContractTests(unittest.TestCase):
    def test_account_set_matches_golden(self):
        self.assertEqual(set(GOLDEN_ACCOUNT_SPECS), set(ACS.ACCOUNT_SPECS))

    def test_builtin_specs_match_golden_field_by_field(self):
        for account_id, want in GOLDEN_ACCOUNT_SPECS.items():
            got = ACS.ACCOUNT_SPECS.get(account_id)
            self.assertIsInstance(got, dict, f"{account_id} 缺失")
            self.assertEqual(set(want), set(got), f"{account_id}: 字段集合漂移")
            for field, expected in want.items():
                self.assertEqual(expected, got[field], f"{account_id}.{field}")

    def test_declaration_order_is_frozen(self):
        """键序是分配层 account_order 与仪表盘行序的契约。"""
        self.assertEqual(BUILTIN_IDS, tuple(ACS.ACCOUNT_SPECS))
        self.assertEqual(BUILTIN_IDS, ACS.builtin_account_ids())

    def test_builtin_ids_follow_strategy_policies(self):
        self.assertEqual(SPOL.NEW_STRATEGY_ID, ACS.NEW_STRATEGY_ID)
        self.assertEqual(SPOL.MAIN_FORCE_STRATEGY_ID, ACS.MAIN_FORCE_STRATEGY_ID)
        self.assertEqual("reported_profit_breakout", ACS.NEW_STRATEGY_ID)
        self.assertEqual("main_force_top10", ACS.MAIN_FORCE_STRATEGY_ID)

    def test_builtin_lookup_returns_none_for_undeclared_account(self):
        self.assertIsNone(ACS.builtin_spec("not_a_declared_account"))

    def test_every_builtin_declaration_is_internally_consistent(self):
        for account_id, spec in GOLDEN_ACCOUNT_SPECS.items():
            with self.subTest(account=account_id):
                self.assertIn(spec["risk_profile"], ACS.RISK_PROFILES)
                self.assertIn(spec["default_style"], ACS.STYLE_PROFILES)
                self.assertGreaterEqual(spec["max_positions"], 1)
                self.assertLessEqual(spec["hold_min"], spec["hold_max"])
                self.assertTrue(spec["name"])


# ---------------------------------------------------------------------------
# 2) 风格 / 风险画像契约
# ---------------------------------------------------------------------------
class StyleAndRiskProfileContractTests(unittest.TestCase):
    def test_style_profiles_match_golden(self):
        self.assertEqual(GOLDEN_STYLE_PROFILES, ACS.STYLE_PROFILES)

    def test_risk_profiles_match_golden(self):
        self.assertEqual(GOLDEN_RISK_PROFILES, ACS.RISK_PROFILES)

    def test_has_style(self):
        for style in GOLDEN_STYLE_PROFILES:
            self.assertTrue(ACS.has_style(style), style)
        for missing in ("", None, "not_a_style"):
            self.assertFalse(ACS.has_style(missing), missing)

    def test_style_profile_accessor_matches_table(self):
        for style, want in GOLDEN_STYLE_PROFILES.items():
            self.assertEqual(want, ACS.style_profile(style, default_style="strong"), style)

    def test_style_profile_falls_back_to_default_style(self):
        self.assertEqual(GOLDEN_STYLE_PROFILES["pullback"],
                         ACS.style_profile("unknown_style", default_style="pullback"))

    def test_style_profile_raises_when_default_style_is_undeclared(self):
        """与抽取前逐字一致：default 参数是 eager 求值，未声明即 KeyError。"""
        with self.assertRaises(KeyError):
            ACS.style_profile("unknown_style", default_style="not_a_style")

    def test_style_name_contract(self):
        for style, want in GOLDEN_STYLE_PROFILES.items():
            self.assertEqual(want["name"], ACS.style_name(style))
        self.assertEqual("fallback-name", ACS.style_name("not_a_style", "fallback-name"))
        self.assertEqual("not_a_style", ACS.style_name("not_a_style"))
        self.assertIsNone(ACS.style_name(None, None))

    def test_risk_profile_accessor_matches_table(self):
        for key, want in GOLDEN_RISK_PROFILES.items():
            self.assertEqual(want, ACS.risk_profile(key, default_key="trend"), key)

    def test_risk_profile_falls_back_to_default_key(self):
        self.assertEqual(GOLDEN_RISK_PROFILES["sector"],
                         ACS.risk_profile("not_a_profile", default_key="sector"))

    def test_risk_profile_raises_when_default_key_is_undeclared(self):
        with self.assertRaises(KeyError):
            ACS.risk_profile("not_a_profile", default_key="not_a_profile")

    def test_default_risk_profile_key(self):
        self.assertEqual("breakout", ACS.default_risk_profile_key("tq_breakout"))
        self.assertEqual("main_force", ACS.default_risk_profile_key("main_force_top10"))
        self.assertEqual("trend", ACS.default_risk_profile_key("not_a_declared_account"))
        self.assertEqual("custom", ACS.default_risk_profile_key("not_a_declared_account", "custom"))


# ---------------------------------------------------------------------------
# 3) 解析口径：内置 / 用户 / 未知
# ---------------------------------------------------------------------------
class SpecResolutionContractTests(unittest.TestCase):
    def test_unknown_account_gets_exact_conservative_fallback(self):
        self.assertEqual(GOLDEN_UNKNOWN_USER_SPEC, PT._spec_for("definitely-not-a-known-account"))

    def test_unknown_account_never_maps_to_a_builtin_identity(self):
        fallback = PT._spec_for("definitely-not-a-known-account")
        self.assertEqual("未知策略账户", fallback["name"])
        self.assertEqual("strategy_dsl", fallback["source_strategy"])
        self.assertEqual("quarantined", fallback["lifecycle_stage"])
        for account_id, spec in GOLDEN_ACCOUNT_SPECS.items():
            self.assertNotEqual(spec["name"], fallback["name"], account_id)
            self.assertNotEqual(spec["source_strategy"], fallback["source_strategy"], account_id)

    def test_builtin_resolution_matches_declaration(self):
        for account_id, want in GOLDEN_ACCOUNT_SPECS.items():
            self.assertEqual(want, PT._spec_for(account_id), account_id)

    def test_fallback_spec_helper_matches_unknown_contract(self):
        self.assertEqual(GOLDEN_UNKNOWN_USER_SPEC, ACS.fallback_spec())

    def test_spec_selection_mode_is_none_for_builtin(self):
        self.assertIsNone(PT.spec_selection_mode("tq_breakout"))


class UserStrategyResolutionTests(unittest.TestCase):
    """用户策略必须在没有内置声明的前提下可解析，且不得映射到内置身份。"""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self._original_db_path = PT.DB_PATH
        PT.DB_PATH = os.path.join(self._tmp.name, "paper_trading.sqlite3")
        self.addCleanup(setattr, PT, "DB_PATH", self._original_db_path)
        SRT.clear_cache()
        self.addCleanup(SRT.clear_cache)
        PT.init_db()

    def _conn(self):
        conn = sqlite3.connect(PT.DB_PATH, timeout=30)
        conn.row_factory = sqlite3.Row
        return conn

    def _activate_user_strategy(self, strategy_id, *, name="用户声明式策略", metadata=None):
        conn = self._conn()
        try:
            with conn:
                SR.ensure_schema(conn)
                SR.create_user_definition(
                    conn, strategy_id, name, dsl_ast=USER_RULE,
                    metadata=metadata or {}, actor="paper-account-specs-test",
                )
                SR.transition(conn, strategy_id, "validated", expected_status="draft",
                              reason="validate", actor="paper-account-specs-test")
                SR.transition(conn, strategy_id, "active", expected_status="validated",
                              reason="activate", actor="paper-account-specs-test")
        finally:
            conn.close()

    def test_user_strategy_has_no_builtin_declaration(self):
        strategy_id = "user_specs_alpha"
        self._activate_user_strategy(strategy_id)
        self.assertNotIn(strategy_id, ACS.ACCOUNT_SPECS)
        self.assertIsNone(ACS.builtin_spec(strategy_id))

    def test_user_strategy_resolves_from_registry_context(self):
        strategy_id = "user_specs_beta"
        self._activate_user_strategy(strategy_id, name="用户策略 Beta")
        conn = self._conn()
        try:
            spec = PT._spec_for(strategy_id, conn=conn)
        finally:
            conn.close()
        self.assertEqual("用户策略 Beta", spec["name"])
        self.assertEqual("strategy_dsl", spec["source_strategy"])
        self.assertEqual("dsl", spec["selection_mode"])
        self.assertNotIn(spec["name"], {s["name"] for s in GOLDEN_ACCOUNT_SPECS.values()})
        self.assertIn(spec["risk_profile"], ACS.RISK_PROFILES)

    def test_unknown_user_strategy_falls_back_without_keyerror(self):
        conn = self._conn()
        try:
            spec = PT._spec_for("never_created_strategy", conn=conn)
        finally:
            conn.close()
        self.assertEqual(GOLDEN_UNKNOWN_USER_SPEC, spec)


# ---------------------------------------------------------------------------
# 4) 与注册表口径的「一致但不复制」
# ---------------------------------------------------------------------------
class RegistryAgreementTests(unittest.TestCase):
    def test_active_account_ids_are_registry_active_intersection(self):
        self.assertEqual(
            set(SR.active_ids()) & set(ACS.ACCOUNT_SPECS),
            set(PT.ACTIVE_ACCOUNT_IDS),
        )

    def test_active_account_specs_project_the_declaration(self):
        self.assertEqual(set(PT.ACTIVE_ACCOUNT_IDS), set(PT.ACTIVE_ACCOUNT_SPECS))
        for account_id in PT.ACTIVE_ACCOUNT_IDS:
            self.assertEqual(GOLDEN_ACCOUNT_SPECS[account_id], PT.ACTIVE_ACCOUNT_SPECS[account_id])
        # 是投影，不是第二个真相：整体不是同一对象，但取值必须落在声明表里。
        self.assertIsNot(PT.ACTIVE_ACCOUNT_SPECS, ACS.ACCOUNT_SPECS)
        self.assertTrue(set(PT.ACTIVE_ACCOUNT_SPECS).issubset(set(ACS.ACCOUNT_SPECS)))

    def test_registry_derived_scope_stays_in_the_authoritative_layer(self):
        """`ACTIVE_ACCOUNT_IDS` 是注册表投影，定义点必须留在 paper_trading。"""
        assign = _find_assign(_parse(PAPER_PATH), "ACTIVE_ACCOUNT_IDS")
        self.assertIsNotNone(assign, "paper_trading 必须自己定义 ACTIVE_ACCOUNT_IDS")
        source = ast.unparse(assign)
        self.assertIn("SR.active_ids", source)
        self.assertIn("ACCOUNT_SPECS", source)

    def test_specs_module_holds_no_registry_scope(self):
        tree = _parse(SPECS_PATH)
        assigned = {
            target.id
            for node in ast.walk(tree) if isinstance(node, ast.Assign)
            for target in node.targets if isinstance(target, ast.Name)
        }
        self.assertNotIn("ACTIVE_ACCOUNT_IDS", assigned)
        self.assertNotIn("ACTIVE_ACCOUNT_SPECS", assigned)

    def test_registry_change_is_visible_in_registry_not_frozen_in_specs(self):
        """注册表真相随时可变；声明层不得缓存它的 active 结论。"""
        conn = sqlite3.connect(":memory:")
        try:
            SR.ensure_schema(conn)
            before = set(SR.active_ids(conn=conn))
            self.assertTrue(before, "注册表应至少有一个 active 内置策略")
            conn.execute(
                "UPDATE strategy_definitions SET lifecycle_status='paused', supports_new_cycle=0 "
                "WHERE id='tq_breakout'"
            )
            after = set(SR.active_ids(conn=conn))
            self.assertNotEqual(before, after, "注册表口径必须随 lifecycle 变化")
            self.assertIn("tq_breakout", before)
            self.assertNotIn("tq_breakout", after)
        finally:
            conn.close()
        # 声明层与注册表无关：暂停不改变声明内容。
        self.assertEqual(GOLDEN_ACCOUNT_SPECS["tq_breakout"], ACS.ACCOUNT_SPECS["tq_breakout"])


# ---------------------------------------------------------------------------
# 5) 归档 / 回放可解析性（生命周期无关）
# ---------------------------------------------------------------------------
class ArchiveReplayResolvabilityTests(unittest.TestCase):
    def test_known_account_universe_survives_for_archive_replay(self):
        known = set(PT.ACCOUNT_SPECS)
        for account_id in BUILTIN_IDS:
            self.assertIn(account_id, known)

    def test_inactive_or_archived_builtin_still_resolves(self):
        """归档账本解码不依赖注册表生命周期：声明表始终能解析。"""
        for account_id in BUILTIN_IDS:
            self.assertEqual(GOLDEN_ACCOUNT_SPECS[account_id], PT._spec_for(account_id))

    def test_archived_user_strategy_still_resolves_without_keyerror(self):
        spec = PT._spec_for("archived_user_strategy_from_history")
        self.assertEqual(GOLDEN_UNKNOWN_USER_SPEC, spec)

    def test_orphan_account_lookup_contract_used_by_dashboard(self):
        """仪表盘/持仓的孤儿账户兜底路径必须拿到可读 spec。"""
        self.assertIsNone(PT.ACCOUNT_SPECS.get("orphan_account_id"))
        self.assertIsNotNone(ACS.fallback_spec()["entry_model_name"])


# ---------------------------------------------------------------------------
# 6) 可变隔离
# ---------------------------------------------------------------------------
class MutableIsolationTests(unittest.TestCase):
    def test_builtin_spec_lookup_returns_independent_copy(self):
        first = ACS.builtin_spec("tq_breakout")
        first["max_positions"] = 99
        first["take_profit"].append((9.9, 9.9))
        second = ACS.builtin_spec("tq_breakout")
        self.assertEqual(GOLDEN_ACCOUNT_SPECS["tq_breakout"], second)
        self.assertEqual(3, second["max_positions"])
        self.assertEqual([(0.08, 0.5)], second["take_profit"])
        self.assertEqual(GOLDEN_ACCOUNT_SPECS["tq_breakout"], ACS.ACCOUNT_SPECS["tq_breakout"])

    def test_fallback_spec_lookup_returns_independent_copy(self):
        first = ACS.fallback_spec()
        first["max_positions"] = 42
        first["take_profit"].append((7.7, 7.7))
        self.assertEqual(GOLDEN_UNKNOWN_USER_SPEC, ACS.fallback_spec())
        self.assertEqual(GOLDEN_UNKNOWN_USER_SPEC, ACS.UNKNOWN_USER_SPEC)

    def test_spec_for_builtin_returns_independent_copy(self):
        first = PT._spec_for("trend_pullback")
        first["max_weight"] = 0.99
        self.assertEqual(GOLDEN_ACCOUNT_SPECS["trend_pullback"], PT._spec_for("trend_pullback"))
        self.assertEqual(GOLDEN_ACCOUNT_SPECS["trend_pullback"], ACS.ACCOUNT_SPECS["trend_pullback"])

    def test_style_profile_lookup_returns_independent_copy(self):
        first = ACS.style_profile("strong", default_style="strong")
        first["name"] = "被污染"
        self.assertEqual(GOLDEN_STYLE_PROFILES["strong"],
                         ACS.style_profile("strong", default_style="strong"))

    def test_risk_profile_lookup_returns_independent_copy(self):
        first = ACS.risk_profile("breakout", default_key="trend")
        first["max_exposure"] = 0.01
        self.assertEqual(GOLDEN_RISK_PROFILES["breakout"],
                         ACS.risk_profile("breakout", default_key="trend"))


# ---------------------------------------------------------------------------
# 7) 兼容 facade：只允许别名 / 委托
# ---------------------------------------------------------------------------
class CompatibilityFacadeTests(unittest.TestCase):
    def test_paper_trading_tables_are_the_same_objects(self):
        self.assertIs(ACS.ACCOUNT_SPECS, PT.ACCOUNT_SPECS)
        self.assertIs(ACS.STYLE_PROFILES, PT.STYLE_PROFILES)
        self.assertIs(ACS.RISK_PROFILES, PT.RISK_PROFILES)
        self.assertIs(ACS.UNKNOWN_USER_SPEC, PT._UNKNOWN_USER_SPEC)

    def test_version_constants_are_aliases(self):
        self.assertEqual(ACS.NEW_STRATEGY_VERSION, PT.NEW_STRATEGY_VERSION)
        self.assertEqual(ACS.MAIN_FORCE_STRATEGY_VERSION, PT.MAIN_FORCE_STRATEGY_VERSION)
        self.assertEqual("reported-profit-breakout-v1", PT.NEW_STRATEGY_VERSION)
        self.assertEqual("main-force-top10-v1", PT.MAIN_FORCE_STRATEGY_VERSION)

    def test_paper_trading_aliases_are_plain_assignments_not_dict_literals(self):
        tree = _parse(PAPER_PATH)
        for name in ("ACCOUNT_SPECS", "STYLE_PROFILES", "RISK_PROFILES", "_UNKNOWN_USER_SPEC"):
            with self.subTest(name=name):
                assign = _find_assign(tree, name)
                self.assertIsNotNone(assign, f"{name} 兼容别名缺失")
                self.assertIsInstance(assign.value, ast.Attribute, f"{name} 必须是别名而非字面量表")
                self.assertEqual("ACS", ast.unparse(assign.value).split(".")[0])

    def test_spec_for_delegates_to_the_specs_module(self):
        func = _find_function(_parse(PAPER_PATH), "_spec_for")
        self.assertIsNotNone(func)
        body = ast.unparse(func)
        self.assertIn("ACS.builtin_spec", body)
        self.assertIn("ACS.fallback_spec", body)
        self.assertIn("USP.user_spec_for", body)
        # 不得在内联重写第二份解析/声明。
        self.assertNotIn("max_positions", body)
        self.assertNotIn("default_style", body)


# ---------------------------------------------------------------------------
# 8) 架构守卫：依赖方向 / 无副作用 / 单一实现
# ---------------------------------------------------------------------------
class SpecsArchitectureGuardTests(unittest.TestCase):
    def test_specs_module_never_imports_paper_trading(self):
        roots = _import_roots(_parse(SPECS_PATH))
        self.assertNotIn("paper_trading", roots)

    def test_specs_module_imports_only_allowlisted_roots(self):
        roots = _import_roots(_parse(SPECS_PATH))
        self.assertTrue(roots, "守卫非空性：至少应有 import")
        self.assertEqual(set(), roots - ALLOWED_SPECS_IMPORT_ROOTS)

    def test_specs_module_declares_no_forbidden_dependency(self):
        roots = _import_roots(_parse(SPECS_PATH))
        self.assertEqual(set(), roots & FORBIDDEN_SPECS_IMPORT_ROOTS)

    def test_specs_module_performs_no_database_network_or_order_io(self):
        offenders = []
        for node in ast.walk(_parse(SPECS_PATH)):
            if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute):
                if node.func.attr in FORBIDDEN_SPECS_CALL_ATTRS:
                    offenders.append(f"{node.lineno}: .{node.func.attr}(")
        self.assertEqual([], offenders, "声明层不得有数据库/网络/订单副作用调用")

    def test_specs_module_carries_no_authoritative_truth_names(self):
        offenders = []
        for node in ast.walk(_parse(SPECS_PATH)):
            if isinstance(node, ast.Name) and node.id in FORBIDDEN_AUTHORITY_NAMES:
                offenders.append(f"{node.lineno}: {node.id}")
            elif isinstance(node, ast.Attribute) and node.attr in FORBIDDEN_AUTHORITY_NAMES:
                offenders.append(f"{node.lineno}: .{node.attr}")
            elif isinstance(node, ast.Constant) and isinstance(node.value, str):
                if node.value in FORBIDDEN_AUTHORITY_NAMES:
                    offenders.append(f"{node.lineno}: {node.value!r}")
        self.assertEqual([], offenders, "声明层不得携带生命周期/版本/周期所有权/执行资格真相")

    def test_specs_module_has_no_import_time_side_effects(self):
        """模块顶层只允许 import / 赋值 / 函数定义 / docstring。"""
        offenders = []
        for node in _module_level_statements(_parse(SPECS_PATH)):
            if isinstance(node, (ast.Import, ast.ImportFrom, ast.Assign, ast.AnnAssign,
                                 ast.FunctionDef, ast.ClassDef)):
                continue
            if isinstance(node, ast.Expr) and isinstance(node.value, ast.Constant):
                continue  # docstring
            offenders.append(f"{node.lineno}: {type(node).__name__}")
        self.assertEqual([], offenders)

    def test_module_level_assignments_contain_no_calls(self):
        offenders = []
        for node in _module_level_statements(_parse(SPECS_PATH)):
            if not isinstance(node, ast.Assign):
                continue
            for child in ast.walk(node.value):
                if isinstance(child, ast.Call):
                    offenders.append(f"{node.lineno}: {ast.unparse(child)}")
        self.assertEqual([], offenders, "import 本模块不得执行任何调用（不改变运行时状态）")

    # ---- 单一实现守卫（非空性先证存在，再证别处没有） ---------------------
    def test_detector_finds_the_real_table_in_the_specs_module(self):
        found = _find_account_spec_tables(_parse(SPECS_PATH))
        self.assertEqual(1, len(found), f"声明层应有且只有一张账户 spec 表：{found}")
        _, key_count, spec_like = found[0]
        self.assertEqual(len(BUILTIN_IDS), key_count)
        self.assertEqual(len(BUILTIN_IDS), spec_like)

    def test_account_spec_table_lives_only_in_the_specs_module(self):
        offenders = []
        for name, path in _iter_backend_sources():
            if os.path.abspath(path) == os.path.abspath(SPECS_PATH):
                continue
            for lineno, key_count, spec_like in _find_account_spec_tables(_parse(path)):
                offenders.append(f"{name}:{lineno} keys={key_count} spec_like={spec_like}")
        self.assertEqual([], offenders, "账户声明表只允许存在于 paper_account_specs.py")

    def test_paper_trading_holds_no_duplicated_spec_values(self):
        """别名之外的任何一份内置 spec 字段副本都必须被守卫抓到。"""
        source = open(PAPER_PATH, encoding="utf-8").read()
        # entry_model_name 是账户 spec 独有文案；它们只允许出现在声明模块。
        for unique_field_value in (
            "强势日内候选实时确认",
            "热点板块相对强度",
            "已披露财报质量与突破确认",
            "主力持续性与微观成交确认",
        ):
            self.assertNotIn(unique_field_value, source, unique_field_value)


if __name__ == "__main__":
    unittest.main()

# -*- coding: utf-8 -*-
"""PR-9 第三批：组装期依赖容器的契约。

`ManualOrderDependencies` / `DashboardDependencies` / `ExecutionDependencies` 以前
用**私有字段名**承载 65 条跨模块私有依赖（``_now`` / ``_num`` / ``_audit`` …），
再由组装根 ``globals()[field.name]`` 按名字回填。现在：

* 容器字段是公开名（``now`` / ``num`` / ``audit`` …）；
* 组装根用 ``_dependency_implementation(name)`` 解析（先同名、再回退到 ``_name``），
  查不到立刻抛错；
* 本文件机械校验"容器的每个字段都能解析、且容器里不再有私有字段"，
  以后谁再往容器里加私有字段或改名，测试会先红。
"""
from __future__ import annotations

import dataclasses
import os
import sys
import unittest
from unittest import mock

BACKEND_DIR = os.path.dirname(os.path.abspath(__file__))
if BACKEND_DIR not in sys.path:
    sys.path.insert(0, BACKEND_DIR)

import dashboard_queries as DQ  # noqa: E402
import execution_planner as EP  # noqa: E402
import manual_orders as MO  # noqa: E402
import paper_trading as PT  # noqa: E402

CONTAINERS = (
    ("manual_orders", MO.ManualOrderDependencies),
    ("dashboard_queries", DQ.DashboardDependencies),
    ("execution_planner", EP.ExecutionDependencies),
)


class DependencyContainerContractTests(unittest.TestCase):
    def test_containers_declare_no_private_fields(self):
        for label, container in CONTAINERS:
            with self.subTest(container=label):
                private = [f.name for f in dataclasses.fields(container)
                           if f.name.startswith("_") and not f.name.startswith("__")]
                self.assertEqual(private, [], f"{label} 仍有私有依赖字段")

    def test_every_field_resolves_to_an_implementation(self):
        """每个字段都必须解析出实现：由别的 owner 提供，或从组装根解析。

        组装根解析（``_dependency_implementation``）覆盖本模块提供的实现；由其他
        owner 直接绑定的字段（lot / provenance）在实例上体现，所以这里以**绑定结果**
        为准，未知字段仍必须大声失败。
        """
        accessors = {
            "manual_orders": MO._dependencies,
            "dashboard_queries": DQ._dependencies,
            "execution_planner": EP._pt,
        }
        for label, container in CONTAINERS:
            instance = accessors[label]()
            for field in dataclasses.fields(container):
                with self.subTest(container=label, field=field.name):
                    self.assertIsNotNone(getattr(instance, field.name))
                    if field.name in ("record_lot", "consume_available_lots",
                                      "sync_positions", "position_rows",
                                      "link_source_fill", "order_cycle_provenance_for_order",
                                      "require_order_execution_cycle",
                                      # PR-10 工作包 A：订单行 owner 由组装根直绑，
                                      # 不再从本模块解析（避免 other 域 → paper 域的新边）。
                                      "cancel_order", "insert_order",
                                      "record_execution_block", "record_fill_result",
                                      "accumulate_realized_pnl", "write_signal_payload",
                                      "defer_signal", "reopen_pending",
                                      "mark_signal_filled", "update_order_outcome",
                                      "record_signal_outcome"):
                        continue
                    self.assertIsNotNone(PT._dependency_implementation(field.name))

    def test_unknown_field_fails_loudly(self):
        with self.assertRaisesRegex(RuntimeError, "未登记的依赖实现"):
            PT._dependency_implementation("definitely_not_a_dependency")

    def test_binding_supplies_every_declared_field(self):
        # 组装根在 import 时已经跑过一遍；这里验证容器实例上真的拿得到实现。
        instances = (
            ("manual_orders", MO._dependencies()),
            ("dashboard_queries", DQ._dependencies()),
            ("execution_planner", EP._pt()),
        )
        for label, instance in instances:
            for field in dataclasses.fields(type(instance)):
                with self.subTest(container=label, field=field.name):
                    self.assertIsNotNone(getattr(instance, field.name))


if __name__ == "__main__":
    unittest.main()

class LegacyEntryPointCompatTests(unittest.TestCase):
    """PR-9 依赖字段公开化时删掉的旧入口必须还在，且不得另立一套判定。"""

    def test_legacy_lease_name_is_importable_and_callable(self):
        self.assertTrue(callable(PT._assert_active_lease))
        self.assertTrue(callable(PT._require_active_lease))

    def test_legacy_lease_name_forwards_to_the_owner(self):
        sentinel = object()
        with mock.patch.object(PT, "_require_active_lease", return_value=sentinel) as owner:
            self.assertIs(PT._assert_active_lease("conn", "phase"), sentinel)
        owner.assert_called_once_with("conn", "phase")

    def test_legacy_lease_name_still_fails_closed_when_lease_is_lost(self):
        with mock.patch.object(PT, "_require_active_lease",
                               side_effect=RuntimeError("paper lease lost: fixture")):
            with self.assertRaisesRegex(RuntimeError, "paper lease lost"):
                PT._assert_active_lease(object(), "phase")
            with self.assertRaisesRegex(RuntimeError, "paper lease lost"):
                PT._require_active_lease(object(), "phase")

    def test_legacy_cycle_name_is_importable_and_forwards(self):
        import paper_order_execution_authority as POEA
        self.assertTrue(callable(POEA.assert_order_execution_cycle))
        self.assertTrue(callable(POEA.require_order_execution_cycle))
        sentinel = object()
        with mock.patch.object(POEA, "require_order_execution_cycle",
                               return_value=sentinel) as owner:
            self.assertIs(POEA.assert_order_execution_cycle("c", 9, account_id="a"), sentinel)
        owner.assert_called_once_with("c", 9, account_id="a")

    def test_legacy_cycle_name_cannot_bypass_validation(self):
        import paper_order_execution_authority as POEA
        with mock.patch.object(POEA, "require_order_execution_cycle",
                               side_effect=RuntimeError("cycle mismatch fixture")):
            with self.assertRaisesRegex(RuntimeError, "cycle mismatch"):
                POEA.assert_order_execution_cycle(object(), 9)
            with self.assertRaisesRegex(RuntimeError, "cycle mismatch"):
                POEA.require_order_execution_cycle(object(), 9)

    def test_containers_keep_public_require_fields(self):
        names = {f.name for _, container in CONTAINERS for f in dataclasses.fields(container)}
        self.assertIn("require_active_lease", names)
        self.assertIn("require_order_execution_cycle", names)

    def test_no_assert_prefixed_dependency_fields_are_reintroduced(self):
        """`assert_*` 会被 Mock 当成断言方法，依赖字段必须继续用 require_*。"""
        for label, container in CONTAINERS:
            for field in dataclasses.fields(container):
                with self.subTest(container=label, field=field.name):
                    self.assertFalse(field.name.startswith("assert_"),
                                     "依赖字段不得以 assert_ 开头（Mock 会拦截）")


class DependencyResolverBoundaryTests(unittest.TestCase):
    """`_dependency_implementation` 的解析边界。"""

    def test_legacy_private_alias_wins_over_an_unrelated_public_name(self):
        with mock.patch.object(PT, "collision_probe", "public", create=True),                 mock.patch.object(PT, "_collision_probe", "legacy", create=True):
            self.assertEqual(PT._dependency_implementation("collision_probe"), "legacy")

    def test_public_name_is_used_when_no_legacy_alias_exists(self):
        with mock.patch.object(PT, "only_public_probe", "public", create=True):
            self.assertEqual(PT._dependency_implementation("only_public_probe"), "public")

    def test_falsy_implementations_are_not_treated_as_missing(self):
        with mock.patch.object(PT, "_falsy_probe", 0, create=True):
            self.assertEqual(PT._dependency_implementation("falsy_probe"), 0)

    def test_unknown_field_fails_immediately(self):
        with self.assertRaisesRegex(RuntimeError, "未登记的依赖实现"):
            PT._dependency_implementation("no_such_dependency")

    def test_resolution_is_runtime_not_frozen_at_import(self):
        sentinel = object()
        with mock.patch.object(PT, "_audit", sentinel):
            self.assertIs(PT._dependency_implementation("audit"), sentinel)
            # 绑定工厂是惰性的：模块导入之后替换实现，下一次取依赖就能看到。
            self.assertIs(MO._dependencies().audit, sentinel)

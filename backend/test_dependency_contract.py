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
        for label, container in CONTAINERS:
            for field in dataclasses.fields(container):
                with self.subTest(container=label, field=field.name):
                    value = PT._dependency_implementation(field.name)
                    self.assertIsNotNone(value)

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

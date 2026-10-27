# -*- coding: utf-8 -*-
"""研究运行时连接边界的验证：配置只走 owner、依赖方向单向、事实走只读账本。

这一层存在的理由是**一处真实缺陷**：研究运行时曾经把「配置」和「研究队列」混成一条连接，
于是配置库要么被复制进研究库，要么连"禁用的槽位"都照发请求。现在：

* 配置连接只能由宿主注册的 owner 工厂提供，本模块不 import ``adaptive_engine``；
* 未注册时是明确的 ``ConfigurationConnectionUnbound``，不会偷偷换库；
* 正式绩效事实走独立只读账本，绝不借用研究队列连接。
"""
import ast
import contextlib
import os
import unittest
from unittest import mock

import research_runtime_config as RRC


def _bound_names(path):
    """源文件里所有静态 import 到的顶层模块名（含函数体内的延迟 import）。"""
    source = open(path, encoding="utf-8").read()
    names = set()
    for node in ast.walk(ast.parse(source)):
        if isinstance(node, ast.Import):
            names.update(alias.name.split(".")[0] for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.level == 0 and node.module:
            names.add(node.module.split(".")[0])
    return names


class ModuleBoundaryTests(unittest.TestCase):
    def test_the_runtime_boundary_never_imports_the_config_owner(self):
        """方向必须是 AE → RRC 单向；反向 import 会把编排/学习引擎拖进一个环。"""
        names = _bound_names(RRC.__file__)
        self.assertNotIn("adaptive_engine", names)
        self.assertNotIn("agent_task_queue", names)

    def test_orchestration_reaches_the_config_only_through_this_boundary(self):
        import research_orchestration as RO

        names = _bound_names(RO.__file__)
        self.assertNotIn("adaptive_engine", names)
        with mock.patch.object(RRC, "configuration_connection") as connection:
            self.assertIs(RO.config_connection(), connection.return_value)
        connection.assert_called_once_with()

    def test_performance_facts_use_the_readonly_ledger_not_a_queue_connection(self):
        import strategy_performance as SP

        request = {"strategy_id": "tq_breakout"}
        with mock.patch.object(SP, "collect_performance_fact",
                               return_value={"nav": 1.0}) as collect:
            self.assertEqual(
                RRC.read_performance_fact(request, db_path="ledger.sqlite3"),
                {"nav": 1.0})
        collect.assert_called_once_with(request, db_path="ledger.sqlite3")


class ConfigurationConnectionTests(unittest.TestCase):
    def setUp(self):
        self._previous = RRC.config_connection_factory()

    def tearDown(self):
        RRC.bind_config_connection(self._previous)

    def test_an_injected_connection_is_used_as_is(self):
        sentinel = object()
        with RRC.configuration_connection(sentinel) as conn:
            self.assertIs(conn, sentinel)

    def test_an_unbound_owner_is_an_explicit_error_not_a_silent_fallback(self):
        RRC.bind_config_connection(None)
        self.assertFalse(RRC.config_connection_bound())
        with self.assertRaises(RRC.ConfigurationConnectionUnbound) as caught:
            with RRC.configuration_connection():
                self.fail("未绑定时不该打开任何连接")
        self.assertIn("bind_config_connection", str(caught.exception))

    def test_the_bound_factory_is_entered_and_closed(self):
        opened = []

        @contextlib.contextmanager
        def owner():
            opened.append("enter")
            try:
                yield "config-conn"
            finally:
                opened.append("exit")

        RRC.bind_config_connection(owner)
        self.assertTrue(RRC.config_connection_bound())
        with RRC.configuration_connection() as conn:
            self.assertEqual(conn, "config-conn")
        self.assertEqual(opened, ["enter", "exit"])

    def test_binding_is_a_single_owner_slot(self):
        RRC.bind_config_connection(lambda: "first")
        self.assertEqual(RRC.config_connection_factory()(), "first")
        RRC.bind_config_connection(lambda: "second")
        self.assertEqual(RRC.config_connection_factory()(), "second")

    def test_a_non_callable_factory_is_refused(self):
        with self.assertRaises(TypeError):
            RRC.bind_config_connection("adaptive_engine._connect")


class CompositionRootTests(unittest.TestCase):
    def test_the_engine_registers_itself_as_the_config_owner(self):
        import adaptive_engine as AE

        self.assertIs(RRC.config_connection_factory(), AE._connect)
        self.assertIs(AE.CONFIG_OWNER_BOUND, AE._connect)

    def test_the_owner_connection_reads_the_configured_slot_row(self):
        import tempfile

        import adaptive_engine as AE
        import ai_review_service as AIR

        with tempfile.TemporaryDirectory() as folder:
            with mock.patch.multiple(AE, DB_PATH=os.path.join(folder, "config.sqlite3"),
                                     CACHE_DIR=folder):
                with AE._connect() as config:
                    AIR.update_slot(config, "ai1", api_key="fixture-key",
                                    base_url="https://fixture.invalid/v1",
                                    model="fixture-model", enabled=True)
                with RRC.configuration_connection() as config:
                    stored = AIR.get_slot_config(config, "ai1")
        self.assertEqual(stored["model"], "fixture-model")
        self.assertEqual(stored["source"], "database")


if __name__ == "__main__":
    unittest.main()

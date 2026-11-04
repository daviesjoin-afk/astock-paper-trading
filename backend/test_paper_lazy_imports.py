# -*- coding: utf-8 -*-
"""按入口拆分依赖：热路径 lane 不得为此付出 pandas 的启动成本。

背景：`paper_trading` 是每个 slot 的 cron 入口，每个交易日 231 次冷启动
（risk / execution-resume / intraday 各 77 次，3 分钟一跳）。旧实现在模块体
里直接 `import pandas`，于是连完全不碰 DataFrame 的风险 lane 也要先付约
0.5 秒的 pandas/numpy/OpenBLAS。

这里锁住三件事：
1. 惰性代理在真正使用前不加载目标模块，之后缓存解析结果；
2. 代理对真实模块的 API 完全透明（含 DataFrame/Series 等真实对象）；
3. 代理拒绝属性写入，避免出现与真实模块分叉的本地副本。
"""
import sys
import time
import unittest

import paper_lazy_imports as LI


class LazyModuleTests(unittest.TestCase):
    def test_target_is_not_imported_until_first_attribute_access(self):
        sys.modules.pop("cmath", None)
        proxy = LI.lazy("cmath")
        self.assertNotIn("cmath", sys.modules)
        self.assertEqual(proxy.sqrt(4.0), 2.0)
        self.assertIn("cmath", sys.modules)

    def test_resolution_is_cached_not_repeated(self):
        proxy = LI.lazy("cmath")
        proxy.sqrt(4.0)
        started = time.perf_counter()
        for _ in range(1000):
            proxy.sqrt(9.0)
        self.assertLess(time.perf_counter() - started, 0.05)

    def test_proxy_is_transparent_for_real_module_attributes(self):
        proxy = LI.lazy("json")
        self.assertEqual(proxy.dumps({"a": 1}), '{"a": 1}')
        self.assertEqual(proxy.loads('{"a": 1}')["a"], 1)
        self.assertIn("dumps", dir(proxy))

    def test_repr_reports_lazy_then_resolved(self):
        proxy = LI.lazy("cmath")
        self.assertIn("lazy", repr(proxy))
        self.assertAlmostEqual(proxy.pi, 3.14159265, places=6)
        self.assertIn("resolved", repr(proxy))

    def test_writes_forward_to_the_real_module_for_mock_patch(self):
        # mock.patch.object(module, "name", ...) 是仓库里的既有用法：patch 会写入
        # 再还原。代理必须转发，否则测试套件会静默失效。
        from unittest import mock

        proxy = LI.lazy("cmath")
        with mock.patch.object(proxy, "sqrt", return_value=99):
            self.assertEqual(proxy.sqrt(4), 99)
        self.assertEqual(proxy.sqrt(4), 2 + 0j)

    def test_write_before_resolution_lands_on_the_real_module(self):
        # 用本用例专用的临时模块，绝不能污染标准库：一旦把 fractions.Fraction
        # 换成函数，同分片内 statistics.mean 会连锁失败（实测 super() 报错）。
        import types

        name = "_lazy_probe_module"
        fake = types.ModuleType(name)
        fake.marker = "original"
        sys.modules[name] = fake
        self.addCleanup(sys.modules.pop, name, None)

        proxy = LI.lazy(name)
        proxy.marker = "overridden"
        self.assertEqual(proxy.marker, "overridden")
        self.assertEqual(sys.modules[name].marker, "overridden",
                         "解析时要把预写值补到真实模块")


class HotPathImportTests(unittest.TestCase):
    """真实入口：Risk lane 不应在导入期拉起 pandas。"""

    def test_paper_trading_import_does_not_load_pandas(self):
        import subprocess
        from pathlib import Path

        backend = Path(__file__).resolve().parent
        code = "import sys; sys.path.insert(0, '.'); import paper_trading; " \
               "print('pandas' in sys.modules)"
        result = subprocess.run([sys.executable, "-c", code], cwd=str(backend),
                                capture_output=True, text=True, timeout=300)
        self.assertEqual(result.returncode, 0, result.stderr[-2000:])
        self.assertEqual(result.stdout.strip(), "False",
                         "paper_trading must not import pandas at module load; "
                         "risk/execution-resume lanes never touch a DataFrame")


if __name__ == "__main__":
    unittest.main()

# -*- coding: utf-8 -*-
"""并行回归跑批的分片数必须跟当前可用内存相容。

并行分片的提速来自"每个进程各自 import 一次 pandas/numpy/OpenBLAS"。这些库
每个进程要占约 1 GiB 常驻内存；在 16 GiB 且只剩 3.6 GiB 可用的机器上开 8 个
分片会直接 OOM——现场表现是 ``MemoryError``、``OpenBLAS: Memory allocation
still failed`` 或退出码 0xC0000409，看起来像测试失败，实际是机器扛不住。

这里锁住两件事：分片数会按可用内存下调（并说明原因），以及这类崩溃被识别成
内存问题而不是断言失败。
"""
import importlib.util
import unittest
from pathlib import Path


def _load_runner():
    path = Path(__file__).resolve().parents[1] / "scripts" / "run_tests_parallel.py"
    spec = importlib.util.spec_from_file_location("run_tests_parallel", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class ShardMemoryBudgetTests(unittest.TestCase):
    def setUp(self):
        self.runner = _load_runner()

    def test_shards_are_cut_when_memory_cannot_feed_them(self):
        # 16 GiB 机器只剩 3.6 GiB：8 个分片必然 OOM，应降到 3 个（(3686-512)/900）。
        shards, note = self.runner.memory_limited_shards(8, avail_mib=3686)
        self.assertEqual(shards, 3)
        self.assertIn("下调", note)
        self.assertIn("pandas", note)

    def test_four_shards_still_fit_the_machine_that_passed(self):
        # 4 分片在可用 3.6 GiB 的宿主上实测通过，不能因为探测抖动被压到 1 个。
        shards, _ = self.runner.memory_limited_shards(4, avail_mib=3686)
        self.assertGreaterEqual(shards, 3)

    def test_shards_are_left_alone_when_memory_is_ample(self):
        shards, note = self.runner.memory_limited_shards(4, avail_mib=64 * 1024)
        self.assertEqual(shards, 4)
        self.assertEqual(note, "")

    def test_never_drops_below_one_shard(self):
        # 内存再紧也必须跑，不能算出 0 个分片而静默跳过整个回归。
        shards, note = self.runner.memory_limited_shards(8, avail_mib=256)
        self.assertEqual(shards, 1)
        self.assertIn("下调", note)

    def test_unknown_memory_does_not_change_the_request(self):
        # 读不到可用内存时宁可照常跑，也不要瞎猜成 1 个分片拖慢全量。
        shards, note = self.runner.memory_limited_shards(8, probe=False)
        self.assertEqual(shards, 8)
        self.assertEqual(note, "")

    def test_oom_crash_is_reported_as_memory_not_as_a_failure(self):
        hint = self.runner._oom_hint(
            3221225773, ["AssertionError: 1 != 0", "OpenBLAS error: Memory allocation still failed"])
        self.assertIn("内存耗尽", hint)
        self.assertIn("非测试断言失败", hint)

    def test_plain_memory_error_is_detected(self):
        hint = self.runner._oom_hint(1, ["MemoryError"])
        self.assertIn("内存耗尽", hint)

    def test_ordinary_failure_gets_no_memory_hint(self):
        self.assertEqual(self.runner._oom_hint(1, ["AssertionError: 3 != 4"]), "")


if __name__ == "__main__":
    unittest.main()

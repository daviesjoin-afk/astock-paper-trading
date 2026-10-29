"""The paper runner and fast-entry runner share one durable timing state."""
import datetime as dt
import importlib.util
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest


class EntryTimingProcessStateTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.path = Path(self.tmp.name) / 'entry.json'
        self.now = dt.datetime.combine(dt.date.today(), dt.time(9, 40))

    def worker(self):
        spec = importlib.util.spec_from_file_location('isolated_entry_timing', Path(__file__).with_name('entry_timing.py'))
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        module.STATE_PATH = str(self.path)
        return module

    def test_another_runner_does_not_drop_new_candidates_or_confirmations(self):
        paper, fast = self.worker(), self.worker()
        paper.evaluate('tq_breakout', '600001', 10, 1, now=self.now)
        fast.evaluate('trend_pullback', '600002', 10, 1, now=self.now)
        allowed, _ = paper.evaluate('tq_breakout', '600001', 10, 1,
                                    now=self.now + dt.timedelta(minutes=3))
        self.assertTrue(allowed)
        fast.evaluate('trend_pullback', '600003', 10, 1, now=self.now)
        state = json.loads(self.path.read_text())
        self.assertEqual(state['tq_breakout']['600001']['state'], 'confirmed')
        self.assertEqual(set(state['trend_pullback']), {'600002', '600003'})

    def test_completed_entry_cannot_be_reopened_by_a_stale_runner(self):
        paper, fast = self.worker(), self.worker()
        paper.evaluate('tq_breakout', '600001', 10, 1, now=self.now)
        fast.evaluate('trend_pullback', '600002', 10, 1, now=self.now)
        paper.mark_entered('tq_breakout', '600001', 10)
        fast.evaluate('trend_pullback', '600003', 10, 1, now=self.now)
        allowed, info = fast.evaluate('tq_breakout', '600001', 10, 1,
                                     now=self.now + dt.timedelta(minutes=3))
        self.assertFalse(allowed)
        self.assertEqual(info['state'], 'entered')

    def test_parallel_processes_keep_all_independent_symbols(self):
        backend = str(Path(__file__).parent)
        child = """import datetime as dt,sys
sys.path.insert(0,sys.argv[1])
import entry_timing as et
et.STATE_PATH=sys.argv[2]
now=dt.datetime.combine(dt.date.today(),dt.time(9,40))
for i in range(12):
 et.evaluate('tq_breakout',str(int(sys.argv[3])+i),10,1,now=now)
"""
        processes = [subprocess.Popen([sys.executable, '-B', '-c', child, backend,
                     str(self.path), str(start)], stdout=subprocess.PIPE,
                     stderr=subprocess.PIPE, env={**os.environ, 'PYTHONDONTWRITEBYTECODE': '1'})
                     for start in (600001, 601001)]
        for process in processes:
            out, err = process.communicate(timeout=30)
            self.assertEqual(process.returncode, 0, (out, err))
        state = json.loads(self.path.read_text())
        self.assertEqual(len(state['tq_breakout']), 24)

    def test_a_contended_lock_never_drops_the_symbol(self):
        """拿不到文件锁时必须**重试**，不能 fail-open 丢状态。

        Windows 的 ``msvcrt.locking`` 只尝试 10 次就抛 OSError；POSIX 的
        ``os.lockf`` 是阻塞的。两种实现在"拿不到锁"时都可能让异常冒到
        ``evaluate``，而它的兜底是 fail-open 放行**且不落盘** —— 那个标的
        就被静默丢掉了（实测表现为 24 个标的只落盘 23 个）。

        这里把锁调用打成"前两次失败、第三次成功"，断言状态仍然写进去了。
        """
        module = self.worker()
        module.STATE_PATH = str(self.path)
        module._state = None
        real_locking = module.msvcrt.locking if hasattr(module, 'msvcrt') else None
        if real_locking is None:  # pragma: no cover - POSIX path.
            self.skipTest("POSIX 的 os.lockf 是阻塞的，不会在这里抛错")
        attempts = []

        def flaky(fd, mode, length):
            attempts.append(mode)
            if len(attempts) < 3:
                raise OSError(36, "Resource deadlock avoided")
            return real_locking(fd, mode, length)

        module.msvcrt.locking = flaky
        try:
            allowed, info = module.evaluate('tq_breakout', '600001', 10, 1, now=self.now)
        finally:
            module.msvcrt.locking = real_locking
        self.assertNotEqual(info.get('state'), 'error', info)
        self.assertIsInstance(allowed, bool)
        state = json.loads(self.path.read_text())
        self.assertIn('600001', state['tq_breakout'],
                      '拿不到锁必须重试到成功，不能 fail-open 丢掉这个标的')


if __name__ == '__main__':
    unittest.main()

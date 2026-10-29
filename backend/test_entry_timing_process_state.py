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


if __name__ == '__main__':
    unittest.main()

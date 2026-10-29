# -*- coding: utf-8 -*-
"""``ASTOCK_DATA_DIR`` 必须移动**全部**运行时状态，而不是只移动账本。

为什么要有这个测试：``data_paths`` 的契约写的是"把全部运行时状态指向别的
目录"，但历史上只有一部分消费者遵守它 —— ``data_fetcher.CACHE_DIR`` 仍固定
在源码根目录，于是 K 线、行情快照、清单、健康文件，以及所有从
``dfc.CACHE_DIR`` 派生的路径（``selection_tracking.DB_PATH``、
``selection_service`` 的因子缓存、``universe.UNIVERSE_PATH`` …）都会被写回
原工作树。这类缺陷在单进程、无隔离检查的情况下完全不可见：路径都存在、
写入都成功、测试都通过，只是写到了错误的位置。

因此本文件用**新进程**验证：显式设置 ``ASTOCK_DATA_DIR`` 后

1. 每个已知运行时路径常量都落在指定目录内；
2. 源码根目录的 ``data_cache/`` **没有新增文件**。
"""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

BACKEND = Path(__file__).resolve().parent
REPO_CACHE = BACKEND.parent / "data_cache"

#: 在子进程里导入的消费者。覆盖"缓存根 / 账本 / 状态 JSON / 锁 / 报告"五类
#: 运行时写入方；任何一类漏掉环境变量都会被下面的断言抓住。
PROBE = r"""
import json
import adaptive_learning_dispatch as ALD
import alt_data as AD
import data_fetcher as dfc
import data_paths
import main as MAIN
import paper_trading as PT
import resource_guard as RG
import risk_center as RC
import security_state_point_in_time as SSP
import selection_alpha_report as SAR
import selection_tracking as ST
import strategy_registry as SR
import strategy_trace as TRC
import tracker as TK
import trade_attribution as TA
import universe as U

print(json.dumps({
    "data_dir": data_paths.data_dir(),
    "finance_cache": dfc.FINANCE_CACHE_PATH,
    "cache_dir": dfc.CACHE_DIR,
    "kline_dir": dfc.KLINE_DIR,
    "kline_manifest": dfc.KLINE_MANIFEST_PATH,
    "full_snapshot": dfc.MARKET_SNAPSHOT_FULL_CACHE_PATH,
    "sample_snapshot": dfc.MARKET_SNAPSHOT_SAMPLE_CACHE_PATH,
    "data_source_health": dfc.DATA_SOURCE_HEALTH_PATH,
    "tracking_db": ST.DB_PATH,
    "selection_factors": MAIN.SEL._SELECTION_FACTORS_PATH,
    "selection_results": MAIN.SEL._SELECTION_RESULT_DIR,
    "super_flow": MAIN.SEL._SUPER_FLOW_PATH,
    "universe": U.UNIVERSE_PATH,
    "paper_db": PT.DB_PATH,
    "paper_report_dir": PT.REPORT_DIR,
    "alt_data": AD.CACHE_DIR,
    "attribution_db": TA.DB_PATH,
    "risk_snapshot": RC.SNAPSHOT_PATH,
    "risk_history": RC.HISTORY_PATH,
    "track_path": TK.TRACK_PATH,
    "registry_db": SR.DEFAULT_DB_PATH,
    "guard_lock": str(RG.LOCK_PATH),
    "job_dir": str(ALD.JOB_DIR),
    "security_archive": SSP.default_archive_path(),
    "alpha_data_dir": SAR.DATA_DIR,
    "alpha_report": SAR.REPORT_PATH,
    "trace_dir": str(TRC._default_trace_dir()),
}, ensure_ascii=False))
"""


def _cache_snapshot(root: Path) -> set:
    """源码根目录 ``data_cache/`` 里所有文件的相对路径。"""
    if not root.exists():
        return set()
    found = set()
    for dirpath, _dirnames, filenames in os.walk(root):
        for name in filenames:
            found.add(os.path.relpath(os.path.join(dirpath, name), root))
    return found


def _cache_signature(root: Path) -> dict:
    """相对路径 → (大小, mtime_ns)：抓"改了已有文件"而不只是"新增文件"。

    2026-10-29 实测：全量跑批期间源码根 ``data_cache/paper_trading.sqlite3``
    的 mtime 被更新，而仅比较文件名的断言完全看不见——"没新增文件"和"没被写过"
    是两件事。
    """
    if not root.exists():
        return {}
    signature = {}
    for dirpath, _dirnames, filenames in os.walk(root):
        for name in filenames:
            path = Path(dirpath) / name
            try:
                stat = path.stat()
            except OSError:
                continue
            signature[os.path.relpath(path, root)] = (stat.st_size, stat.st_mtime_ns)
    return signature


class DataDirIsolationTests(unittest.TestCase):
    def setUp(self):
        self.isolated = Path(tempfile.mkdtemp(prefix="astock-data-dir-"))
        self.addCleanup(shutil.rmtree, self.isolated, True)

    def _probe(self) -> dict:
        env = {k: v for k, v in os.environ.items() if not k.startswith("ASTOCK_")}
        env["ASTOCK_DATA_DIR"] = str(self.isolated)
        result = subprocess.run(
            [sys.executable, "-B", "-c", PROBE],
            cwd=str(BACKEND), env=env, capture_output=True, text=True, timeout=180,
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        return json.loads(result.stdout.strip().splitlines()[-1])

    def test_every_runtime_path_follows_the_isolated_directory(self):
        paths = self._probe()
        root = str(self.isolated)
        self.assertEqual(os.path.abspath(paths.pop("data_dir")), root)
        outside = {
            name: value for name, value in paths.items()
            if not str(value).replace("\\", "/").startswith(root.replace("\\", "/"))
        }
        self.assertEqual(outside, {}, f"以下运行时路径仍指向源码目录：{outside}")

    def test_tracking_ledger_and_market_cache_are_isolated(self):
        paths = self._probe()
        root = Path(paths["data_dir"])
        expected = {
            "tracking_db": root / "selection_tracking.db",
            "kline_dir": root / "klines",
            "data_source_health": root / "data_source_health.json",
        }
        for name, want in expected.items():
            self.assertEqual(os.path.abspath(paths[name]), os.path.abspath(want), name)
        # 导入消费者就会建 K 线目录：隔离目录真的被使用，而不是只改字符串。
        self.assertTrue((root / "klines").is_dir(), "隔离目录内的 K 线缓存目录必须已建立")

    def test_source_tree_cache_stays_untouched(self):
        before = _cache_snapshot(REPO_CACHE)
        self._probe()
        after = _cache_snapshot(REPO_CACHE)
        self.assertEqual(after - before, set(), "设置 ASTOCK_DATA_DIR 后不得在源码目录新增运行数据")

    def test_source_tree_cache_content_stays_untouched(self):
        """已有文件也不得被改写：只看"新增"会漏掉就地更新。"""
        before = _cache_signature(REPO_CACHE)
        self._probe()
        after = _cache_signature(REPO_CACHE)
        changed = sorted(
            name for name, meta in after.items()
            if name in before and before[name] != meta)
        self.assertEqual(changed, [], "设置 ASTOCK_DATA_DIR 后不得改写源码目录的运行数据")
        self.assertEqual(sorted(after), sorted(before), "文件集合本身也不得变化")


if __name__ == "__main__":
    unittest.main()

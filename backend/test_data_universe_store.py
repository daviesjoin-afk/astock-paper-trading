# -*- coding: utf-8 -*-
"""``data_universe_store`` 读取边界：正式清单判据、文件身份缓存与路径 seam。

这层是从 ``universe.load_universe`` 下沉出来的，行情备用报价与基础库构建都要向它
取同一份代码集合。本模块钉住三件事：什么文件才算"正式全 A 股清单"、缓存身份必须
包含路径（否则两个路径会互相串用）、以及 ``universe.UNIVERSE_PATH`` 仍是部署/测试
的重定向 seam。
"""
import json
import os
import shutil
import tempfile
import unittest
from unittest import mock

import data_universe_store as USTORE


def _write(path, codes, **payload):
    body = {
        "built_at": "2026-10-28 08:00:00",
        "scope": "all_a_shares",
        "requested_limit": None,
        "stocks": [{"code": code} for code in codes],
    }
    body.update(payload)
    with open(path, "w", encoding="utf-8") as handle:
        json.dump(body, handle, ensure_ascii=False)
    return path


class FormalUniverseStoreTests(unittest.TestCase):
    def setUp(self):
        self._dir = tempfile.mkdtemp(prefix="universe-store-")
        self.addCleanup(shutil.rmtree, self._dir, True)
        # 部署规模阈值是常量而非逻辑：小样本注入下放宽，判据本身不变。
        patcher = mock.patch.object(USTORE, "FORMAL_UNIVERSE_MIN_ROWS", 2)
        patcher.start()
        self.addCleanup(patcher.stop)

    def _path(self, name):
        return os.path.join(self._dir, name)

    def test_formal_file_supplies_the_code_rows(self):
        path = _write(self._path("formal.json"), ["600000", "600001"])
        rows = USTORE.load_formal_universe(path)
        self.assertEqual(["600000", "600001"], [row["code"] for row in rows])

    def test_legacy_or_debug_files_are_not_a_formal_universe(self):
        cases = {
            "demo.json": {"scope": "demo_synthetic"},
            "limited.json": {"requested_limit": 10},
        }
        for name, payload in cases.items():
            with self.subTest(case=name):
                path = _write(self._path(name), ["600000", "600001"], **payload)
                self.assertEqual([], USTORE.load_formal_universe(path))
        short = _write(self._path("short.json"), ["600000"])
        self.assertEqual([], USTORE.load_formal_universe(short), "低于正式清单门槛不得当正式输入")

    def test_missing_file_is_an_empty_universe(self):
        self.assertEqual([], USTORE.load_formal_universe(self._path("absent.json")))

    def test_cache_identity_includes_the_path(self):
        first = _write(self._path("a.json"), ["600000", "600001"])
        second = _write(self._path("b.json"), ["000001", "000002"])
        self.assertEqual(["600000", "600001"],
                         [row["code"] for row in USTORE.load_formal_universe(first)])
        self.assertEqual(["000001", "000002"],
                         [row["code"] for row in USTORE.load_formal_universe(second)])
        # 回到第一个路径必须是它自己的行，不能命中第二个路径的缓存。
        self.assertEqual(["600000", "600001"],
                         [row["code"] for row in USTORE.load_formal_universe(first)])

    def test_rebuild_invalidates_through_the_file_signature(self):
        path = _write(self._path("rebuilt.json"), ["600000", "600001"])
        self.assertEqual(2, len(USTORE.load_formal_universe(path)))
        _write(path, ["600000", "600001", "600002"])
        self.assertEqual(3, len(USTORE.load_formal_universe(path)),
                         "文件重建后必须重新读取，不得沿用过期的轮内缓存")

    def test_failed_read_is_not_memoized_as_success(self):
        path = self._path("broken.json")
        with open(path, "w", encoding="utf-8") as handle:
            handle.write("{not json")
        self.assertEqual([], USTORE.load_formal_universe(path))
        _write(path, ["600000", "600001"])
        self.assertEqual(2, len(USTORE.load_formal_universe(path)))


class UniverseDelegationTests(unittest.TestCase):
    """``universe`` 保留公开名与部署 seam，读取实现只有一份。"""

    def setUp(self):
        self._dir = tempfile.mkdtemp(prefix="universe-delegation-")
        self.addCleanup(shutil.rmtree, self._dir, True)
        patcher = mock.patch.object(USTORE, "FORMAL_UNIVERSE_MIN_ROWS", 2)
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_universe_path_seam_still_redirects_the_read(self):
        import universe as U

        path = _write(os.path.join(self._dir, "seam.json"), ["600000", "600001"])
        with mock.patch.object(U, "UNIVERSE_PATH", path):
            self.assertEqual(["600000", "600001"], [row["code"] for row in U.load_universe()])
            self.assertEqual(
                (os.path.getmtime(path), os.path.getsize(path)),
                U._universe_cache_signature(),
                "覆盖度缓存签名必须跟着同一条路径",
            )

    def test_universe_and_store_share_one_implementation_and_cache(self):
        import universe as U

        path = _write(os.path.join(self._dir, "shared.json"), ["600000", "600001"])
        with mock.patch.object(U, "UNIVERSE_PATH", path):
            via_universe = U.load_universe()
            via_store = USTORE.load_formal_universe(path)
        self.assertEqual(via_store, via_universe)
        # 只有一份缓存：两条入口都命中同一份文件身份。
        self.assertEqual(path, USTORE._cache_identity[0])


if __name__ == "__main__":
    unittest.main()

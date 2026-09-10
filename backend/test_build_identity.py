# -*- coding: utf-8 -*-
"""PR-49 regression：发布标识（build id）必须三处一致。

历史上这三处是手工维护、彼此独立：

1. ``frontend/src/app.js``（PR-55 起的入口）第 2 行 ``window.__ASTOCK_ADAPTIVE_UI_BUILD__``；
2. ``frontend/index.html`` 中 ``/app.css?v=`` / ``/app.js?v=`` 的 cache-bust 值；
3. 后端 ``/api/version`` 返回的 ``build``（``backend/build_info.py``）。

任何一处漏改，都会出现「后端已是新版、浏览器却仍在跑缓存旧脚本」的隐性
错配。发布收口时用本测试把它们锁在一起；同时锁住 dist 产物确实包含新
build id（避免有人忘了重新构建）。
"""
from __future__ import annotations

import os
import re
import unittest

BACKEND = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(BACKEND)
FRONTEND = os.path.join(ROOT, "frontend")

import build_info as BI  # noqa: E402
import main  # noqa: E402


def _load(path):
    with open(path, "r", encoding="utf-8") as handle:
        return handle.read()


class BuildIdentityTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.index_source = _load(os.path.join(FRONTEND, "index.html"))
        cls.app_source = _load(os.path.join(FRONTEND, "src", "app.js"))

    def test_build_id_is_non_empty_and_dated(self):
        self.assertTrue(BI.APP_BUILD_ID)
        self.assertRegex(BI.APP_BUILD_ID, r"^\d{8}-[a-z0-9][a-z0-9-]*$")

    def test_version_endpoint_registered(self):
        paths = main.app.openapi().get("paths") or {}
        self.assertIn("/api/version", paths)
        self.assertIn("get", paths["/api/version"])

    def test_version_endpoint_returns_canonical_build(self):
        self.assertEqual(BI.build_payload()["build"], BI.APP_BUILD_ID)
        self.assertEqual(main.version()["build"], BI.APP_BUILD_ID)

    def test_frontend_script_declares_same_build(self):
        match = re.search(r"__ASTOCK_ADAPTIVE_UI_BUILD__='([^']+)'", self.app_source)
        self.assertIsNotNone(match, "app.js 缺少 __ASTOCK_ADAPTIVE_UI_BUILD__ 声明")
        self.assertEqual(match.group(1), BI.APP_BUILD_ID)

    def test_index_html_cache_bust_matches_build(self):
        versions = re.findall(r"/(?:app\.css|app\.js)\?v=([^\"']+)", self.index_source)
        self.assertEqual(len(versions), 2, f"index.html 应有 2 处 cache-bust，实际 {versions}")
        for value in versions:
            self.assertEqual(value, BI.APP_BUILD_ID)

    def test_dist_bundle_carries_build_id(self):
        dist_path = os.path.join(FRONTEND, "dist", "app.js")
        self.assertTrue(os.path.exists(dist_path), "dist/app.js 未构建")
        self.assertIn(BI.APP_BUILD_ID, _load(dist_path), "dist 产物与源 build id 不一致，忘了重新构建？")


if __name__ == "__main__":
    unittest.main()

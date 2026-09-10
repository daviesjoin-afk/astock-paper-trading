# -*- coding: utf-8 -*-
"""PR-49：发布标识（build id）的单一来源。

三处必须使用同一个 build id：

1. 本模块的 ``APP_BUILD_ID``（后端 ``/api/version`` 返回）；
2. ``frontend/app.js`` 第 2 行的 ``window.__ASTOCK_ADAPTIVE_UI_BUILD__``；
3. ``frontend/index.html`` 中 ``/app.css?v=`` 与 ``/app.js?v=`` 的 cache-bust 值。

``test_build_identity.py`` 会逐处断言一致——历史上这三者是手工维护的，
一旦有人只改了其中一处，就会出现"界面已经换了新版、浏览器却仍在跑旧脚
本"的隐性错配，排查成本极高。发布收口时把它们锁在一起。
"""
from __future__ import annotations

APP_NAME = "astock-paper-trading"
APP_BUILD_ID = "20260910-strategy-console-v1"
APP_BUILD_LABEL = "Custom Strategy Web 产品线收口（PR-45 → PR-49）"


def build_payload() -> dict:
    """只读、零网络的发布标识，供 ``/api/version`` 与运维核验使用。"""
    return {
        "app": APP_NAME,
        "build": APP_BUILD_ID,
        "label": APP_BUILD_LABEL,
    }

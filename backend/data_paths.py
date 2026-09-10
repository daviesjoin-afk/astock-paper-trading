"""运行时数据目录解析（唯一入口）。

默认仍是仓库内的 ``data_cache/``；设置 ``ASTOCK_DATA_DIR`` 可把**全部运行时状态**
（SQLite 账本、选股因子、快照与状态 JSON、报告）指向别的目录。

用途：浏览器 E2E 测试（PR-56）为每个测试 worker 建独立临时目录，
既不写开发者本地 ``data_cache/``，也不碰服务器生产库。

只影响"会写"的运行时数据；``frontend/`` 等源码与产物仍在仓库内。
"""
from __future__ import annotations

import os

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DEFAULT_DATA_DIR = os.path.join(BASE, "data_cache")

_ENV = "ASTOCK_DATA_DIR"


def data_dir() -> str:
    """运行时数据目录（绝对路径，不自动创建）。"""
    override = os.environ.get(_ENV)
    if override and override.strip():
        return os.path.abspath(override.strip())
    return DEFAULT_DATA_DIR


def data_path(*parts: str) -> str:
    """运行时数据目录下的路径。"""
    return os.path.join(data_dir(), *parts)


def report_dir() -> str:
    """报告输出目录；E2E 下与数据目录一起隔离到临时位置。"""
    override = os.environ.get("ASTOCK_REPORT_DIR")
    if override and override.strip():
        return os.path.abspath(override.strip())
    if os.environ.get(_ENV, "").strip():
        return os.path.join(data_dir(), "reports")
    return os.path.join(BASE, "reports")

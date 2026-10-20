# -*- coding: utf-8 -*-
"""轻量可观测性模块：Prometheus 文本格式 /metrics（零第三方依赖）。

提供：
- 进程/系统指标：内存 RSS、磁盘使用率、数据库文件大小
- 业务指标：模拟盘周期状态、任务执行成功率、最近任务耗时
- 健康检查辅助

设计原则：任何指标采集失败都必须静默降级（返回 None/0），绝不影响主业务。
"""
import datetime as dt
import json
import os
import sqlite3
import time

import data_fetcher as dfc

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
import data_paths
CACHE_DIR = data_paths.data_dir()
REPORT_DIR = os.path.join(BASE, "reports")

_METRICS_CACHE = {"data": None, "ts": 0.0}
_METRICS_TTL_SECONDS = 10.0


def _safe_float(value, default=0.0):
    """有限数值原样返回，NaN/无法解析才退回默认值。

    旧实现写成 ``return num if num != num else default``，把 NaN 判断写反，
    于是每个有限输入都拿到默认值 —— 线上 ``astock_process_rss_kb`` 因此
    长期报 0。指标采集失败必须静默降级，但不能把有效读数静默抹成 0。
    """
    try:
        num = float(value)
    except (TypeError, ValueError):
        return default
    return num if num == num else default  # num != num 才是 NaN


def _collect_system_metrics():
    """进程与系统级指标（尽力而为，失败返回空 dict）。"""
    metrics = {}
    # 优先当前进程 /proc/self/status；容器内受限时退回 /proc/1 或 resource
    for proc_path in ("/proc/self/status", "/proc/1/status"):
        try:
            with open(proc_path, encoding="utf-8") as handle:
                for line in handle:
                    if line.startswith("VmRSS:"):
                        metrics["process_rss_kb"] = _safe_float(line.split()[1])
                        break
            if "process_rss_kb" in metrics:
                break
        except OSError:
            continue
    if "process_rss_kb" not in metrics:
        try:
            import resource
            metrics["process_rss_kb"] = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
        except (ImportError, ValueError):
            pass
    try:
        stat = os.statvfs("/")
        total = stat.f_blocks * stat.f_frsize
        free = stat.f_bavail * stat.f_frsize
        metrics["disk_total_bytes"] = total
        metrics["disk_free_bytes"] = free
        metrics["disk_used_ratio"] = round((total - free) / total, 4) if total else 0.0
    except OSError:
        pass
    return metrics


def _collect_db_metrics():
    """三个 SQLite 数据库文件大小。"""
    metrics = {}
    for name in ("paper_trading.sqlite3", "adaptive_learning.sqlite3", "paper_research.sqlite3"):
        path = os.path.join(CACHE_DIR, name)
        try:
            metrics[f"db_size_bytes_{name.replace('.sqlite3', '')}"] = os.path.getsize(path)
        except OSError:
            pass
    return metrics


def _collect_business_metrics():
    """业务指标：读取健康检查与模拟盘概览缓存，不触发网络/重计算。"""
    metrics = {}
    try:
        health = dfc.load_source_health() or {}
        metrics["data_source_healthy"] = 1 if health.get("healthy") else 0
    except Exception:
        pass
    return metrics


def _parse_stamp(value):
    try:
        return dt.datetime.strptime(str(value)[:19], "%Y-%m-%d %H:%M:%S")
    except (TypeError, ValueError):
        return None


def _collect_paper_metrics():
    """任务新鲜度、最近一轮耗时与市场快照状态。

    只做一条索引查询加一条审计取最新行：指标端点每十秒被采集一次，任何
    全表扫描或大 payload 解析都会变成采集方的负载。失败一律静默降级。
    """
    metrics = {}
    path = os.path.join(CACHE_DIR, "paper_trading.sqlite3")
    if not os.path.exists(path):
        return metrics
    try:
        con = sqlite3.connect(path, timeout=2.0)
    except sqlite3.Error:
        return metrics
    try:
        con.row_factory = sqlite3.Row
        run = con.execute(
            "SELECT status, started_at, finished_at FROM paper_job_runs "
            "ORDER BY started_at DESC LIMIT 1"
        ).fetchone()
        if run is not None:
            started = _parse_stamp(run["started_at"])
            if started is not None:
                metrics["paper_slot_last_age_seconds"] = round(
                    (dt.datetime.now() - started).total_seconds(), 1
                )
            metrics["paper_slot_last_failed"] = 1 if run["status"] == "failed" else 0
            finished = _parse_stamp(run["finished_at"])
            if started is not None and finished is not None:
                metrics["paper_slot_last_duration_seconds"] = round(
                    (finished - started).total_seconds(), 1
                )
        gate = con.execute(
            "SELECT detail FROM paper_audit WHERE event = 'live_scan_gate' "
            "ORDER BY id DESC LIMIT 1"
        ).fetchone()
        if gate is not None and gate["detail"]:
            snapshot = (json.loads(gate["detail"]) or {}).get("snapshot") or {}
            metrics["market_snapshot_rows"] = _safe_float(snapshot.get("rows"))
            metrics["market_snapshot_price_only"] = 1 if snapshot.get("price_only") else 0
            metrics["market_snapshot_flow_field_coverage"] = _safe_float(
                snapshot.get("flow_field_coverage")
            )
    except (sqlite3.Error, ValueError, TypeError):
        pass
    finally:
        try:
            con.close()
        except sqlite3.Error:
            pass
    return metrics


def _collect_runtime_version_metrics():
    """运行时 SQLite 版本，便于确认镜像真的换成了修复版本。"""
    try:
        parts = [int(piece) for piece in sqlite3.sqlite_version.split(".")[:3]]
        while len(parts) < 3:
            parts.append(0)
        return {"sqlite_version_number": parts[0] * 10000 + parts[1] * 100 + parts[2]}
    except (AttributeError, TypeError, ValueError):
        return {}


def _format_prometheus(metrics, prefix="astock"):
    """将 dict 转成 Prometheus 文本格式（gauge 类型）。"""
    lines = []
    lines.append("# HELP astock_process_rss_kb Process resident memory (KB)")
    lines.append("# TYPE astock_process_rss_kb gauge")
    for key, value in metrics.items():
        if not isinstance(value, (int, float)):
            continue
        safe_key = key.replace("-", "_").replace(".", "_")
        lines.append(f"{prefix}_{safe_key} {value}")
    lines.append(f"# HELP {prefix}_scrape_ts Scrape timestamp unix seconds")
    lines.append(f"# TYPE {prefix}_scrape_ts gauge")
    lines.append(f"{prefix}_scrape_ts {int(time.time())}")
    return "\n".join(lines) + "\n"


def metrics_payload(force=False):
    """返回 Prometheus 文本（带 10s 缓存，避免每次请求重复 stat）。"""
    now = time.time()
    if not force and _METRICS_CACHE["data"] and now - _METRICS_CACHE["ts"] < _METRICS_TTL_SECONDS:
        return _METRICS_CACHE["data"]
    merged = {}
    for collector in (_collect_system_metrics, _collect_db_metrics, _collect_business_metrics,
                      _collect_paper_metrics, _collect_runtime_version_metrics):
        try:
            merged.update(collector())
        except Exception:
            continue
    payload = _format_prometheus(merged)
    _METRICS_CACHE["data"] = payload
    _METRICS_CACHE["ts"] = now
    return payload

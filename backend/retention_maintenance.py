#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""自适应学习库保留窗口维护：归档 + 裁剪过期样本。

可再生的中间样本（alpha_samples / intraday_samples）只保留最近 N 天，
删除前先归档为 gzip（append 到归档文件），保证可恢复。
已兑现收益 / 审计证据链 / 新闻事件按更长窗口保留。

用法：python retention_maintenance.py [--days-alpha 30] [--dry-run]
"""
import gzip
import json
import os
import sqlite3
import sys
import time

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
CACHE_DIR = os.path.join(BASE, "data_cache")
DB_PATH = os.path.join(CACHE_DIR, "adaptive_learning.sqlite3")
ARCHIVE_DIR = os.path.join(CACHE_DIR, "retention_archive")
os.makedirs(ARCHIVE_DIR, exist_ok=True)

# 保留策略：表 -> (时间列, 保留天数, 是否归档)
RETENTION_POLICY = {
    "adaptive_alpha_samples": ("profile_date", 30, True),
    "adaptive_intraday_samples": ("profile_date", 30, True),
    "adaptive_alpha_returns": ("created_at", 60, False),
    "adaptive_evidence_chains": ("created_at", 90, False),
    "news_events": ("first_seen_at", 60, False),
    "news_event_outcomes": ("matured_at", 90, False),
    "adaptive_intraday_profiles": ("created_at", 30, True),
    "adaptive_market_profiles": ("created_at", 90, False),
    "adaptive_risk_daily_outcomes": ("created_at", 120, False),
    # 此前三张高频写入的表不在策略里，是 adaptive_learning.sqlite3 膨胀到
    # 数百 MB 的主因。逐订单归因与逐次运行记录只有近期审计价值；
    # adaptive_rewards 是证据门槛输入，窗口放长但仍有界。
    "adaptive_order_risk_attribution": ("order_date", 180, False),
    "adaptive_downside_events": ("event_date", 180, False),
    "adaptive_runs": ("started_at", 180, False),
    "adaptive_rewards": ("end_date", 365, False),
}

DAYS_ALPHA = 30  # 默认 alpha 保留天数（可参数化）


def _parse_date(value):
    """解析 ISO 日期/时间戳，返回 date 对象或 None。"""
    if not value:
        return None
    text = str(value)[:10]
    try:
        import datetime as dt
        return dt.date.fromisoformat(text)
    except ValueError:
        return None


def _loads(value):
    try:
        return json.loads(value)
    except (TypeError, ValueError):
        return None


def clean_orphan_klines(dry_run=True):
    """清理 klines 目录中不在 manifest 内的临时/孤儿文件。"""
    manifest_path = os.path.join(CACHE_DIR, "kline_manifest.json")
    kline_dir = os.path.join(CACHE_DIR, "klines")
    try:
        with open(manifest_path, encoding="utf-8") as handle:
            manifest = json.load(handle)
        stocks = manifest.get("stocks", {}) if isinstance(manifest, dict) else {}
        manifest_codes = set(str(k) for k in stocks.keys())
    except (OSError, ValueError):
        print("kline_manifest 读取失败，跳过孤儿清理")
        return 0
    removed = 0
    for name in os.listdir(kline_dir):
        base = os.path.splitext(os.path.basename(name))[0]
        # 正常文件为 6 位代码（如 000001.csv）或带前缀基准（BENCH_000300.csv）
        if "." in base and base.split(".")[0] not in manifest_codes:
            continue  # 跳过 xxx.csv.<random> 临时文件（base 含点）
        code = base.split(".")[0]
        if base in manifest_codes or code in manifest_codes or base.startswith("BENCH_"):
            continue
        path = os.path.join(kline_dir, name)
        if not dry_run:
            try:
                os.remove(path)
                removed += 1
            except OSError:
                pass
        else:
            removed += 1
    print(f"klines 孤儿清理: {'DRY-RUN 检出' if dry_run else '删除'} {removed} 个")
    return removed


def run(dry_run=True, alpha_days=DAYS_ALPHA):
    cutoff = time.strftime("%Y%m%d")
    archive_path = os.path.join(ARCHIVE_DIR, f"adaptive-retention-{cutoff}.jsonl.gz")
    conn = sqlite3.connect(DB_PATH, timeout=30)
    total_purged = 0
    archived_rows = 0
    for table, (col, keep_days, do_archive) in RETENTION_POLICY.items():
        try:
            cols = [r[1] for r in conn.execute(f"PRAGMA table_info({table})")]
        except sqlite3.OperationalError:
            continue
        if col not in cols:
            continue
        # 计算阈值：保留窗口基于"最新日期"，避免数据稀疏时误删
        row = conn.execute(f"SELECT MAX({col}) FROM {table}").fetchone()
        latest = _parse_date(row[0] if row else None)
        if not latest:
            continue
        import datetime as dt
        threshold = latest - dt.timedelta(days=keep_days)
        threshold_str = threshold.isoformat()
        before = conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
        # 归档过期行（带 payload 的表完整导出）
        if do_archive and not dry_run:
            try:
                rows = conn.execute(
                    f"SELECT * FROM {table} WHERE {col} < ? ORDER BY {col}",
                    (threshold_str,),
                ).fetchall()
                if rows:
                    with gzip.open(archive_path, "at", encoding="utf-8") as handle:
                        for r in rows:
                            handle.write(json.dumps(dict(zip(cols, r)), ensure_ascii=False, default=str) + "\n")
                    archived_rows += len(rows)
            except sqlite3.OperationalError:
                pass
        # 删除过期行
        if not dry_run:
            cur = conn.execute(f"DELETE FROM {table} WHERE {col} < ?", (threshold_str,))
            purged = cur.rowcount
        else:
            cur = conn.execute(f"SELECT COUNT(*) FROM {table} WHERE {col} < ?", (threshold_str,))
            purged = cur.fetchone()[0]
        total_purged += purged
        after = before - purged
        print(f"{table}: keep {keep_days}d (<= {threshold}) purged={purged} rows {before}->{after}")
    conn.commit()
    # DELETE 不回收文件页；裁剪后执行 WAL 收缩与 VACUUM，让磁盘占用真正
    # 回落。VACUUM 需要约等于库大小的临时空间且耗时数分钟，因此只在
    # 实际删除了数据时执行（本脚本设计为低频维护任务，非交易时段运行）。
    if not dry_run and total_purged > 0:
        try:
            conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
            print("wal_checkpoint(TRUNCATE) 完成")
        except sqlite3.OperationalError as exc:
            print(f"wal_checkpoint 跳过: {exc}")
        try:
            conn.execute("VACUUM")
            print("VACUUM 完成")
        except sqlite3.OperationalError as exc:
            print(f"VACUUM 跳过（可能磁盘空间不足）: {exc}")
    conn.close()
    size_mb = os.path.getsize(DB_PATH) / (1024 * 1024) if os.path.exists(DB_PATH) else 0
    print(f"adaptive_learning.sqlite3 当前大小: {size_mb:.1f} MB")
    print(f"[{'DRY-RUN' if dry_run else 'DONE'}] total_purged={total_purged} archived_rows={archived_rows}")
    clean_orphan_klines(dry_run=dry_run)
    if dry_run:
        print("提示：加 --apply 执行实际裁剪")


if __name__ == "__main__":
    dry = "--apply" not in sys.argv
    alpha = DAYS_ALPHA
    if "--days-alpha" in sys.argv:
        try:
            alpha = int(sys.argv[sys.argv.index("--days-alpha") + 1])
        except (ValueError, IndexError):
            pass
    run(dry_run=dry, alpha_days=alpha)

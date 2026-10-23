# -*- coding: utf-8 -*-
"""用可验证的原始报价证据修正被盘中写入的收盘观测。

B2 的修复只阻止**新的**盘中写入；已经落库的行仍带着盘中价。修正它们需要原始
报价证据，不能靠"看起来像收盘"的猜测，也不能把重复记录改成无条件覆盖。
本工具的判定与线上写库共用同一条政策（``market_data_contract
.close_snapshot_rows``）：

* 证据必须是该交易日的原始报价：归档的全市场快照 payload（authority 自己
  持久化的那份 artifact），或至少 ``{code, price, quote_at}`` 的行；
* 证据的观测时点必须 ≥ 15:00 且属于同一个交易日 —— 盘中归档一律拒绝；
* 只改**被证据证明有误**的行；没有证据的行原样保留并如实报告；
* 默认 dry-run，``--apply`` 才写库，且写入前比对旧值防止覆盖他人改动。

用法::

    # 只看会改什么（不写库）
    python backend/selection_tracking_repair.py --evidence /backup/market_snapshot_full.json

    # 确认后执行
    python backend/selection_tracking_repair.py --evidence ... --apply

证据文件支持三种形态：完整快照 payload、``{"asof": "...", "quotes": [...]}``、
以及裸的报价行列表（此时必须显式给 ``--asof``）。
"""
from __future__ import annotations

import argparse
import datetime as dt
import json
import os
import sqlite3
import sys
from contextlib import closing
from typing import Any, Mapping, Sequence

import market_data_contract as MDC
import selection_tracking as ST

#: 价格比较容差：报价保留两位小数，任何更大的差异都说明不是同一笔事实。
PRICE_TOLERANCE = 1e-6

_OBSERVATION_QUERY = """
    SELECT o.id, o.pick_id, o.observed_date, o.price, o.benchmark_price,
           o.holding_days, o.return_pct, o.benchmark_return_pct, o.excess_return_pct,
           p.code, p.entry_price, r.benchmark_entry_price
    FROM selection_observations o
    JOIN selection_picks p ON p.id = o.pick_id
    JOIN selection_runs r ON r.id = p.run_id
    {where}
    ORDER BY o.observed_date, p.code
"""


class RepairError(RuntimeError):
    """证据不可用（不是收盘报价、或无法解析）。"""


def _as_instant(value: Any) -> dt.datetime | None:
    if isinstance(value, dt.datetime):
        return value if value.tzinfo else value.replace(tzinfo=ST.CHINA_TZ)
    if isinstance(value, (int, float)):
        return dt.datetime.fromtimestamp(float(value), tz=dt.timezone.utc)
    text = str(value or "").strip()
    if not text:
        return None
    try:
        parsed = dt.datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=ST.CHINA_TZ)


def _evidence_parts(payload: Any, asof: Any) -> tuple[dt.datetime | None, Sequence[Mapping]]:
    """把三种证据形态归一成 ``(asof, rows)``。"""
    if isinstance(payload, Mapping):
        rows = payload.get("rows")
        stamp = payload.get("saved_at") or payload.get("observed_at") or payload.get("asof") or asof
        if rows is None and isinstance(payload.get("quotes"), (list, tuple)):
            rows = payload["quotes"]
            stamp = payload.get("asof") or payload.get("saved_at") or asof
    elif isinstance(payload, (list, tuple)):
        rows, stamp = payload, asof
    else:
        raise RepairError(f"无法识别的证据格式: {type(payload).__name__}")
    if not isinstance(rows, (list, tuple)):
        raise RepairError("证据里没有报价行（rows/quotes）")
    return _as_instant(stamp), [row for row in rows if isinstance(row, Mapping)]


def verified_close_prices(payload: Any, asof: Any = None) -> tuple[dt.datetime, str, dict]:
    """按收盘政策校验证据，返回 ``(asof, business_day, {code: price})``。

    证据时点缺失、不在 15:00 之后、或没有任何行落在该交易日的收盘窗口内时
    **直接失败** —— 宁可拒绝，也不能拿盘中/隔日报价改写历史。
    """
    instant, rows = _evidence_parts(payload, asof)
    if instant is None:
        raise RepairError("证据缺少可解析的观测时点（saved_at / asof）")
    business_day = instant.astimezone(ST.CHINA_TZ).date().isoformat()
    if MDC.close_snapshot_window(instant, business_day) is None:
        raise RepairError(f"证据时点 {instant.isoformat()} 未过该交易日 15:00，不是收盘事实")
    verified = MDC.close_snapshot_rows(rows, instant, business_day)
    if not verified:
        raise RepairError(
            f"证据中没有落在 {business_day} 收盘窗口内的报价（{len(rows)} 行全部不合格）"
        )
    prices = {}
    for row in verified:
        price = row.get("price")
        code = str(row.get("code") or "")
        if not code or not isinstance(price, (int, float)) or price <= 0:
            continue
        prices[code] = float(price)
    if not prices:
        raise RepairError("证据中的收盘报价没有可用价格")
    return instant, business_day, prices


def _benchmark_price(day: str) -> float | None:
    try:
        return ST._benchmark_price_on_or_before(dt.date.fromisoformat(day))
    except Exception:
        return None


def build_plan(
    evidence: Any,
    *,
    db_path: str | None = None,
    since: str | None = None,
    asof: Any = None,
) -> dict:
    """生成修正计划（**只读**，不写库）。"""
    instant, business_day, evidence_prices = verified_close_prices(evidence, asof)
    target = db_path or ST.DB_PATH
    where, args = "", ()
    if since:
        dt.date.fromisoformat(str(since)[:10])  # 早失败，避免静默忽略错误日期
        where, args = "WHERE o.observed_date >= ?", (str(since)[:10],)
    plan = {
        "db": target,
        "evidence": {
            "asof": instant.isoformat(timespec="seconds"),
            "business_date": business_day,
            "verified_quotes": len(evidence_prices),
        },
        "scanned_rows": 0,
        "updates": [],
        "unchanged": 0,
        "no_evidence": 0,
        "skipped": [],
        "applied": False,
    }
    if not os.path.exists(target):
        raise RepairError(f"跟踪库不存在: {target}")
    with closing(sqlite3.connect(f"file:{target}?mode=ro", uri=True, timeout=20)) as conn:
        conn.row_factory = sqlite3.Row
        try:
            rows = conn.execute(_OBSERVATION_QUERY.format(where=where), args).fetchall()
        except sqlite3.OperationalError as exc:
            raise RepairError(f"读取跟踪库失败: {exc}") from exc
    for row in rows:
        plan["scanned_rows"] += 1
        day = str(row["observed_date"])[:10]
        verified = evidence_prices.get(str(row["code"] or ""))
        if verified is None:
            plan["no_evidence"] += 1
            continue
        if day != business_day:
            # 该行属于另一交易日，本次证据不能证明它，原样保留。
            plan["no_evidence"] += 1
            continue
        stored = row["price"]
        if isinstance(stored, (int, float)) and abs(float(stored) - verified) <= PRICE_TOLERANCE:
            plan["unchanged"] += 1
            continue
        entry = row["entry_price"]
        if not isinstance(entry, (int, float)) or entry <= 0:
            plan["skipped"].append({
                "id": row["id"], "code": row["code"], "reason": "missing_entry_price",
            })
            continue
        benchmark = _benchmark_price(day)
        if benchmark is None:
            benchmark = row["benchmark_price"]
        bench_entry = row["benchmark_entry_price"]
        ret = (verified / float(entry) - 1) * 100
        bench_ret = (
            (benchmark / float(bench_entry) - 1) * 100
            if isinstance(benchmark, (int, float)) and isinstance(bench_entry, (int, float)) and bench_entry
            else None
        )
        plan["updates"].append({
            "id": int(row["id"]),
            "pick_id": int(row["pick_id"]),
            "code": str(row["code"] or ""),
            "observed_date": day,
            "old_price": stored,
            "new_price": verified,
            "old_return_pct": row["return_pct"],
            "new_return_pct": ret,
            "benchmark_price": benchmark,
            "benchmark_return_pct": bench_ret,
            "excess_return_pct": (ret - bench_ret) if bench_ret is not None else None,
        })
    return plan


def apply_plan(plan: dict) -> dict:
    """执行计划。每行都带旧值比对，避免覆盖期间发生的其它写入。"""
    updates = plan.get("updates") or []
    if not updates:
        plan["applied"] = True
        plan["applied_rows"] = 0
        return plan
    target = plan["db"]
    written = 0
    with closing(sqlite3.connect(target, timeout=30)) as conn, conn:
        for item in updates:
            cursor = conn.execute(
                """
                UPDATE selection_observations
                   SET price = ?, benchmark_price = ?, return_pct = ?,
                       benchmark_return_pct = ?, excess_return_pct = ?
                 WHERE id = ? AND pick_id = ? AND observed_date = ? AND price = ?
                """,
                (item["new_price"], item["benchmark_price"], item["new_return_pct"],
                 item["benchmark_return_pct"], item["excess_return_pct"],
                 item["id"], item["pick_id"], item["observed_date"], item["old_price"]),
            )
            written += int(cursor.rowcount or 0)
    plan["applied"] = True
    plan["applied_rows"] = written
    if written != len(updates):
        plan["note"] = "部分行在比对时已变化（未被改写），请重新生成计划"
    return plan


def _load_evidence(path: str) -> Any:
    with open(path, encoding="utf-8") as handle:
        return json.load(handle)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="用可验证的收盘报价证据修正跟踪观测（默认 dry-run）",
    )
    parser.add_argument("--evidence", required=True, help="证据 JSON（快照 payload 或报价行列表）")
    parser.add_argument("--asof", default=None, help="证据观测时点（裸行列表时必需）")
    parser.add_argument("--db", default=None, help="跟踪库路径（默认 selection_tracking.DB_PATH）")
    parser.add_argument("--since", default=None, help="只处理该日期及之后的观测（YYYY-MM-DD）")
    parser.add_argument("--apply", action="store_true", help="真正写库；缺省只打印计划")
    args = parser.parse_args(argv)

    try:
        plan = build_plan(
            _load_evidence(args.evidence), db_path=args.db, since=args.since, asof=args.asof,
        )
        if args.apply:
            apply_plan(plan)
    except (RepairError, OSError, ValueError) as exc:
        print(json.dumps({"error": f"{type(exc).__name__}: {exc}"}, ensure_ascii=False, indent=2))
        return 2
    print(json.dumps(plan, ensure_ascii=False, indent=2, default=str))
    return 0


if __name__ == "__main__":
    sys.exit(main())

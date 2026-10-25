"""09:15 起的集合竞价观测。只保存当时个股报价，不写模拟盘账本。

2026-10-26：竞价阶段没有连续成交，"竞价成交额"只能由上游给的累计撮合额
（东财 f6，单位元）得到，因此这里做两件事：
1. 每轮把**真实可用的量额**汇总进 status.json（缺失不填零），页面直接显示；
2. 09:25 收尾时把**当天逐股快照归档**成一天一个文件，作为后续"竞价因子"
   的唯一时点数据源（在此之前只有 current.json，跨天即丢，无法回溯）。
"""
from __future__ import annotations

import datetime as dt
import json
import math
from pathlib import Path
import tempfile

import data_fetcher as dfc
import data_paths
import market_data_contract as MDC
import market_data_service as market
import universe

CN = dt.timezone(dt.timedelta(hours=8))
# bid1/ask1 从未有任何 provider 产出（东财 clist 不给盘口档位），保留只会让
# 下游误以为有盘口数据；量、额、换手、量比、主力净额是真实存在的字段。
FIELDS = ("code", "name", "price", "pct", "volume", "amount", "turnover",
          "quote_at", "source", "main_net", "vol_ratio")
ARCHIVE_KEEP_DAYS = 60
MAX_LARGEST = 5


def _number(value):
    if isinstance(value, bool):
        return None
    try:
        value = float(value)
    except (TypeError, ValueError, OverflowError):
        return None
    return value if math.isfinite(value) else None


def summarize(rows, *, top=MAX_LARGEST):
    """竞价金额汇总：只用时点上真实存在的量额，缺失一律不计入、不填零。

    ``amount_total`` 是**观测到的**竞价成交额合计，不是全市场竞价成交额；
    ``amount_coverage_pct`` 明确覆盖率，避免把部分覆盖读成全市场。
    """
    rows = list(rows or ())
    amounts = [(row, _number(row.get("amount"))) for row in rows]
    volumes = [(row, _number(row.get("volume"))) for row in rows]
    with_amount = [(row, value) for row, value in amounts if value is not None and value > 0]
    with_volume = [(row, value) for row, value in volumes if value is not None and value > 0]
    pcts = [value for value in (_number(row.get("pct")) for row in rows) if value is not None]
    largest = sorted(with_amount, key=lambda item: -item[1])[:max(int(top), 0)]
    return {
        "tracked": len(rows),
        "with_amount": len(with_amount),
        "with_volume": len(with_volume),
        "amount_total": round(sum(value for _row, value in with_amount), 2),
        "volume_total": round(sum(value for _row, value in with_volume), 2),
        "amount_coverage_pct": (
            round(100.0 * len(with_amount) / len(rows), 2) if rows else 0.0),
        "up": sum(1 for value in pcts if value > 0),
        "down": sum(1 for value in pcts if value < 0),
        "flat": sum(1 for value in pcts if value == 0),
        "mean_pct": round(sum(pcts) / len(pcts), 3) if pcts else None,
        "largest": [
            {"code": str(row.get("code") or ""), "name": row.get("name"),
             "pct": _number(row.get("pct")), "amount": round(value, 2)}
            for row, value in largest
        ],
        "unit": {"amount": "元", "volume": "手"},
        "boundary": "只统计本次观测中真实带金额的个股；未覆盖部分不计入，也不代表全市场竞价成交额。",
    }


def _write(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", dir=path.parent,
                                     prefix=".auction-", suffix=".tmp", delete=False) as handle:
        json.dump(value, handle, ensure_ascii=False, allow_nan=False)
        temporary = Path(handle.name)
    try:
        temporary.replace(path)
    finally:
        temporary.unlink(missing_ok=True)


def _read(path):
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
        return value if isinstance(value, dict) else {}
    except (OSError, ValueError):
        return {}


def status():
    """只读小型状态文件，页面查询不读取全市场历史、更不联网。"""
    return _read(Path(data_paths.data_path("auction_tracking", "status.json"))) or {"status": "not_run"}


def _observed_at(value):
    try:
        stamp = dt.datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        return stamp.replace(tzinfo=CN) if stamp.tzinfo is None else stamp.astimezone(CN)
    except (TypeError, ValueError):
        return None


def run(now=None):
    current = (now or dt.datetime.now(CN)).astimezone(CN)
    if not universe.is_trade_day(current.date()) or not dt.time(9, 15) <= current.time() < dt.time(9, 25):
        return {"slot": "auction-track", "status": "skipped", "reason": "outside_auction_tracking_window"}
    # 探活异常不能阻止独立观测；观测结果也不能签发买入许可。
    try:
        health = dfc.check_data_source_health(force=False)
    except Exception:
        health = {}
    try:
        reading = market.refresh_snapshot(MDC.AUCTION_TRACKING_POLICY, now=current, force=True)
        fresh = reading.available and reading.freshness == MDC.FRESHNESS_FRESH
        rows = []
        for raw in reading.rows() if fresh else ():
            observed = _observed_at(raw.get("quote_at"))
            try:
                valid_price = math.isfinite(float(raw.get("price"))) and float(raw["price"]) > 0
            except (ValueError, TypeError, OverflowError):
                valid_price = False
            if (not raw.get("code") or not valid_price or observed is None
                    or observed.date() != current.date()
                    or not dt.time(9, 15) <= observed.time() < dt.time(9, 25)
                    or not -120 <= (current - observed).total_seconds() <= 90):
                continue
            row = {key: raw.get(key) for key in FIELDS if key in raw}
            for key, value in list(row.items()):
                if isinstance(value, float) and not math.isfinite(value):
                    row[key] = None
            rows.append(row)
        rows = list({str(row["code"]): row for row in rows}.values())
        reason = reading.reason
    except Exception as exc:
        rows, reason = [], type(exc).__name__
    return _publish(rows, current, reason, health)


def _publish(rows, current, reason, health, *, final=False):
    root = Path(data_paths.data_path("auction_tracking"))
    previous = _read(root / "current.json")
    observations = previous.get("observations", []) if previous.get("trade_date") == current.date().isoformat() else []
    tick = "09:25" if final else f"09:{15 + ((current.minute - 15) // 3) * 3:02d}"
    observations = [item for item in observations if item.get("tick") != tick]
    observations.append({"tick": tick, "captured_at": current.isoformat(), "rows": rows,
                         "reason": reason, "phase": "final_snapshot" if final else ("cancellable" if current.minute < 20 else "non_cancellable")})
    observations.sort(key=lambda item: item["tick"])
    # 四轮跟踪及最终预选快照仅保留当天，避免文件无限增长。
    observations = observations[-5:]
    summary = summarize(rows)
    report = {"slot": "auction-track", "status": "completed" if rows else "blocked",
              "trade_date": current.date().isoformat(), "captured_at": current.isoformat(),
              "observations": len(observations), "tracked_stocks": len(rows),
              "reason": reason, "source_healthy": bool(health.get("healthy")),
              "observation_only": True, "summary": summary}
    _write(root / "current.json", {"trade_date": report["trade_date"], "observations": observations})
    _write(root / "status.json", report)
    if final and rows:
        _archive_day(root, current, rows, summary)
    return report


def _valid_day(trade_date):
    try:
        return dt.date.fromisoformat(str(trade_date)[:10]).isoformat()
    except (TypeError, ValueError):
        return None


def archive_path(trade_date):
    """某一天的竞价归档文件路径；日期非法时抛 ValueError（不拼接任意字符串）。

    日期会被拼进文件名，任何非 ISO 日期的输入都可能是目录穿越，一律拒绝。
    """
    day = _valid_day(trade_date)
    if day is None:
        raise ValueError("trade_date must be an ISO date")
    return Path(data_paths.data_path("auction_tracking", f"{day}.json"))


def _archive_day(root, current, rows, summary):
    """09:25 收尾：一天一个文件，作为竞价因子的唯一时点数据源。"""
    day = current.date().isoformat()
    _write(root / f"{day}.json", {
        "trade_date": day,
        "captured_at": current.isoformat(),
        "final_tick": "09:25",
        "phase": "final_snapshot",
        "summary": summary,
        "rows": rows,
        "unit": {"amount": "元", "volume": "手"},
        "boundary": "竞价阶段无连续成交；amount 是上游给出的当日累计撮合额，缺失即 None。",
    })
    _prune_archive(root)


def _prune_archive(root):
    try:
        days = sorted(path for path in root.glob("*.json")
                      if len(path.stem) == 10 and path.stem[4] == "-")
    except OSError:
        return
    for path in days[:-ARCHIVE_KEEP_DAYS]:
        try:
            path.unlink()
        except OSError:
            continue


def archived_days(limit=ARCHIVE_KEEP_DAYS):
    """已归档的竞价日（升序），供竞价因子回看；只读，不建目录。"""
    root = Path(data_paths.data_path("auction_tracking"))
    try:
        days = sorted(path for path in root.glob("*.json")
                      if len(path.stem) == 10 and path.stem[4] == "-")
    except OSError:
        return []
    return days[-int(limit):] if limit else days


def load_archived_day(trade_date):
    try:
        path = archive_path(trade_date)
    except ValueError:
        return {}
    return _read(path)


def current_snapshot():
    """当天的 current.json（可能为空表），只读；供 API 在归档前展示。"""
    return _read(Path(data_paths.data_path("auction_tracking", "current.json")))


def record_final(rows, now, health):
    """复用09:25预选已取得的行情；不额外联网，不允许盘后补造竞价。

    收尾窗口放宽到 09:25–09:32：竞价预选在 09:25/09:27/09:29 三次尝试，最后一次
    落在 09:28 之后；窗口太窄会让"最后一次重试"永远归档不了当天竞价。只放宽收尾
    时刻，行本身仍要求 09:24 之后的时点数据且不超过 90 秒。
    """
    current = now.replace(tzinfo=CN) if now.tzinfo is None else now.astimezone(CN)
    if not dt.time(9, 25) <= current.time() < dt.time(9, 32):
        return False
    usable = []
    for row in rows:
        stamp = _observed_at(row.get('quote_at'))
        if (stamp and stamp.date() == current.date() and dt.time(9, 24) <= stamp.time() < dt.time(9, 32)
                and 0 <= (current-stamp).total_seconds() <= 90):
            usable.append({key:row.get(key) for key in FIELDS if key in row})
    if not usable:
        return False
    _publish(usable,current,None,health,final=True)
    return True


def record_archive_error(exc):
    """把归档失败写进 status.json：页面和自检能看出"今天没归档"是因为报错。"""
    report = dict(status() or {})
    report["archive_error"] = f"{type(exc).__name__}: {exc}"[:200]
    report["archive_error_at"] = dt.datetime.now(CN).isoformat()
    _write(Path(data_paths.data_path("auction_tracking", "status.json")), report)
    return report

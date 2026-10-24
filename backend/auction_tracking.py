"""09:15 起的集合竞价观测。只保存当时个股报价，不写模拟盘账本。"""
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
FIELDS = ("code", "name", "price", "pct", "volume", "amount", "turnover",
          "bid1", "ask1", "quote_at", "source", "main_net", "vol_ratio")


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
    report = {"slot": "auction-track", "status": "completed" if rows else "blocked",
              "trade_date": current.date().isoformat(), "captured_at": current.isoformat(),
              "observations": len(observations), "tracked_stocks": len(rows),
              "reason": reason, "source_healthy": bool(health.get("healthy")), "observation_only": True}
    _write(root / "current.json", {"trade_date": report["trade_date"], "observations": observations})
    _write(root / "status.json", report)
    return report


def record_final(rows, now, health):
    """复用09:25预选已取得的行情；不额外联网，不允许盘后补造竞价。"""
    current = now.replace(tzinfo=CN) if now.tzinfo is None else now.astimezone(CN)
    if not dt.time(9, 25) <= current.time() < dt.time(9, 28):
        return False
    usable = []
    for row in rows:
        stamp = _observed_at(row.get('quote_at'))
        if (stamp and stamp.date() == current.date() and dt.time(9, 24) <= stamp.time() < dt.time(9, 28)
                and 0 <= (current-stamp).total_seconds() <= 90):
            usable.append({key:row.get(key) for key in FIELDS if key in row})
    if not usable:
        return False
    _publish(usable,current,None,health,final=True)
    return True

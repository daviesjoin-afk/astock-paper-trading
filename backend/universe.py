# -*- coding: utf-8 -*-
"""全 A 股基础库与历史数据初始化。

基础库不按市值截断，也不删除科创板、北交所或 ST；风险和流动性应在策略层筛选，
否则数据层会制造幸存者偏差并让“全市场”名不副实。
"""
import datetime as dt
import os, json, time, threading, uuid
from concurrent.futures import ThreadPoolExecutor, as_completed
import data_fetcher as dfc

try:
    import chinese_calendar as _cn_calendar
except ImportError:  # pragma: no cover - requirements installs the provider.
    _cn_calendar = None

UNIVERSE_PATH = os.path.join(dfc.CACHE_DIR, "universe.json")
INIT_STATE = {
    "status": "idle", "phase": "idle", "done": 0, "total": 0, "started_at": None,
    "errors": 0, "retried": 0, "recovered": 0,
    "last_error": None,
}
_state_lock = threading.Lock()

# 基础库是低频变更的静态文件，但每轮盘中扫描会通过
# _latest_price_map / _candidate_rows / _live_scan_gate 等路径重复读取
# 数十次（1.86MB JSON）。按 (mtime, size) 缓存，文件被重建时自动失效。
_universe_cache = None
_universe_cache_sig = None
_universe_cache_lock = threading.Lock()
FORMAL_UNIVERSE_MIN_ROWS = 4000
FORMAL_UNIVERSE_MIN_COVERAGE = 0.90
COVERAGE_DISPLAY_CACHE_TTL_SECONDS = 10.0
_coverage_cache_lock = threading.Lock()
_coverage_cache = {"key": None, "at": 0.0, "data": None}


def _coverage_file_signature(path):
    try:
        stat = os.stat(path)
        return (stat.st_mtime_ns, stat.st_size)
    except OSError:
        return None


def _coverage_cache_signature():
    """Return cheap file signatures for the read-only coverage view."""
    return (
        _universe_cache_signature(),
        _coverage_file_signature(dfc.KLINE_MANIFEST_PATH),
        _coverage_file_signature(dfc.KLINE_DIR),
    )


def invalidate_coverage_cache():
    """Invalidate the optional display cache after a data/universe rebuild."""
    with _coverage_cache_lock:
        _coverage_cache.update({"key": None, "at": 0.0, "data": None})


def _universe_cache_signature():
    try:
        return (os.path.getmtime(UNIVERSE_PATH), os.path.getsize(UNIVERSE_PATH))
    except OSError:
        return None

def _board(code):
    code = str(code)
    if code.startswith(("8", "4", "92")):
        return "北交所"
    if code.startswith(("688", "689")):
        return "科创板"
    if code.startswith(("300", "301", "302")):
        return "创业板"
    return "主板"


def build_universe(size=0):
    """构建完整 A 股基础库；size>0 仅用于显式的小样本调试。"""
    # ``size`` is a research/debug view only.  It must never truncate the
    # persisted formal universe used by the paper trader and selector.
    debug_limit = int(size or 0)
    try:
        snap = dfc.fetch_market_snapshot_full(max_age=300)
    except Exception:
        snap = []
    existing = load_universe()
    if not isinstance(snap, list) or not snap:
        return existing
    rows = []
    for row in snap:
        code = str(row.get("code") or "")
        name = str(row.get("name") or "")
        if len(code) != 6 or not code.isdigit() or not name:
            continue
        item = dict(row)
        item["code"] = code
        item["board"] = _board(code)
        item["risk_flag"] = "ST" in name.upper() or "退" in name
        item["snapshot_tradable"] = isinstance(row.get("price"), (int, float))
        item["listing_status"] = (
            "active"
            if any(
                isinstance(row.get(field), (int, float))
                for field in ("price", "mktcap", "float_cap")
            )
            else "pending"
        )
        rows.append(item)
    # 北交所快照会混入“定转”等非股票品种和已迁板的旧代码。保留停牌股票与
    # 尚未开盘的新股，但不把这些非股票/失效代码计入“全市场股票数”。
    cleaned = []
    for item in rows:
        code, name = item["code"], item["name"]
        if name.endswith("定转"):
            continue
        no_market_data = not any(
            isinstance(item.get(field), (int, float))
            for field in ("price", "mktcap", "float_cap")
        )
        possible_new_listing = code.startswith(("920", "688", "301")) and "退" not in name
        if no_market_data and not possible_new_listing:
            continue
        cleaned.append(item)
    rows = cleaned
    rows.sort(
        key=lambda row: row.get("float_cap")
        if isinstance(row.get("float_cap"), (int, float))
        else -1,
        reverse=True,
    )
    unique_codes = {str(row.get("code") or "") for row in rows if row.get("code")}
    baseline = len({str(row.get("code") or "") for row in existing if row.get("code")})
    coverage = len(unique_codes) / max(baseline, len(unique_codes), 1)
    formal_ok = (
        debug_limit == 0
        and len(unique_codes) >= FORMAL_UNIVERSE_MIN_ROWS
        and coverage >= FORMAL_UNIVERSE_MIN_COVERAGE
    )
    if debug_limit > 0:
        return rows[:debug_limit]
    if not formal_ok:
        # A partial/empty source is a diagnostic result, not a new formal
        # universe.  Keep the previous full list intact; on first bootstrap,
        # return the in-memory rows so the downloader can make progress, but
        # never publish them as ``universe.json``.
        return existing or rows
    uni = rows
    # Atomic replace: both containers read this file concurrently, a plain
    # truncate-write made readers parse half-written JSON as an empty universe.
    tmp_uni = f"{UNIVERSE_PATH}.{os.getpid()}.{uuid.uuid4().hex}.tmp"
    with open(tmp_uni, "w", encoding="utf-8") as f:
        json.dump(
            {
                "built_at": time.strftime("%Y-%m-%d %H:%M:%S"),
                "scope": "all_a_shares",
                "requested_limit": size or None,
                "stocks": uni,
            },
            f,
            ensure_ascii=False,
        )
    os.replace(tmp_uni, UNIVERSE_PATH)
    invalidate_coverage_cache()
    return uni

def load_universe():
    global _universe_cache, _universe_cache_sig
    sig = _universe_cache_signature()
    if sig is None:
        return []
    if _universe_cache is not None and sig == _universe_cache_sig:
        return _universe_cache
    with _universe_cache_lock:
        # 双检：锁内再确认一次，避免并发下重复读盘。
        if _universe_cache is not None and sig == _universe_cache_sig:
            return _universe_cache
        rows = []
        if os.path.exists(UNIVERSE_PATH):
            try:
                with open(UNIVERSE_PATH, "r", encoding="utf-8") as f:
                    payload = json.load(f)
                candidate = payload.get("stocks", []) if isinstance(payload, dict) else []
                codes = {str(row.get("code") or "") for row in candidate
                         if isinstance(row, dict) and row.get("code")}
                # Legacy/debug files must not become a formal input merely
                # because the JSON itself is parseable.  The writer records
                # requested_limit=None for the only publishable all-market
                # form; a formal file also needs the same 4,000-row floor.
                if (
                    not isinstance(candidate, list)
                    or not isinstance(payload, dict)
                    or payload.get("scope") != "all_a_shares"
                    or payload.get("requested_limit") is not None
                    or len(codes) < FORMAL_UNIVERSE_MIN_ROWS
                ):
                    rows = []
                else:
                    rows = candidate
            except (OSError, ValueError, TypeError):
                rows = []
        _universe_cache = rows
        _universe_cache_sig = sig
    return rows

def get_init_state():
    with _state_lock:
        return dict(INIT_STATE)

def _as_date(value=None):
    if isinstance(value, dt.datetime):
        return value.date()
    if isinstance(value, dt.date):
        return value
    return dt.date.fromisoformat(str(value)[:10]) if value else dt.date.today()


def is_trade_day(value=None):
    """Return whether Shanghai is open on this calendar date.

    Weekday-only treatment is wrong around Spring Festival, National Day and
    other statutory closures.  ``chinese-calendar`` covers the current A-share
    operating years; if it cannot classify an out-of-range date we fail closed
    rather than unlock T+1 inventory on an assumed session.
    """
    day = _as_date(value)
    if day.weekday() >= 5:
        return False
    if _cn_calendar is None:
        return False
    try:
        return not _cn_calendar.is_holiday(day)
    except (NotImplementedError, ValueError, TypeError):
        return False


def previous_trade_day(day=None):
    value = _as_date(day) - dt.timedelta(days=1)
    for _ in range(21):
        if is_trade_day(value):
            return value
        value -= dt.timedelta(days=1)
    raise RuntimeError("交易日历无法确定上一交易日")


def next_trade_day(day=None):
    value = _as_date(day) + dt.timedelta(days=1)
    for _ in range(21):
        if is_trade_day(value):
            return value
        value += dt.timedelta(days=1)
    raise RuntimeError("交易日历无法确定下一交易日")


def _previous_trade_weekday(day=None):
    """Backward-compatible name; it now uses the statutory trading calendar."""
    return previous_trade_day(day)


def latest_complete_trade_date(asof_day=None, now=None):
    """Return the latest date that is safe to use as a complete daily bar.

    A calendar date is not automatically a completed trading date: before
    15:05 the current day's bar can still be partial, and after a weekend the
    previous Friday is the only valid reference.  The old refresh job passed
    ``today`` unconditionally, so providers correctly returned yesterday and
    the updater classified every response as stale.  Keep this decision in
    the shared data layer so selection, paper trading and scheduled refreshes
    cannot disagree about the cutoff.
    """
    clock = now or dt.datetime.now()
    requested = asof_day
    if isinstance(requested, dt.datetime):
        requested = requested.date()
    elif requested is not None and not isinstance(requested, dt.date):
        requested = dt.date.fromisoformat(str(requested)[:10])
    day = requested or clock.date()
    if not is_trade_day(day):
        return _previous_trade_weekday(day)
    # Only the current local date is subject to the intraday cutoff.  A
    # caller explicitly backfilling an earlier date must be allowed to use it.
    if day == clock.date() and clock.time() < dt.time(15, 5):
        return _previous_trade_weekday(day)
    return day


def _history_is_fresh(last_date, asof_day=None):
    """Accept today's or the prior weekday's complete daily bar."""
    import datetime

    if not last_date:
        return False
    try:
        value = datetime.date.fromisoformat(str(last_date)[:10])
    except (TypeError, ValueError):
        return False
    today = asof_day or datetime.date.today()
    return value in {today, _previous_trade_weekday(today)}


def _download_one(code, beg, tries=3, required_date=None):
    """下载单只历史K线；东财接口偶发空响应，失败重试（避免股票池出现空洞）"""
    import pandas as pd

    cached = dfc.load_cached_kline(code)
    cached_meta = dfc.get_kline_manifest().get(str(code), {})
    fallback_source = cached_meta.get("source") == "sina"

    def _anchor_unadjusted_increment(frame):
        """将新浪未复权增量按共同收盘价锚定为连续前复权数据。"""
        if frame is None or frame.empty or cached is None or cached.empty:
            return None
        if str(cached_meta.get("adjustment") or "").lower() != "qfq":
            return frame
        if str(getattr(frame, "attrs", {}).get("adjustment") or "").lower() != "none":
            return frame
        common = cached.index.intersection(frame.index)
        if len(common) == 0:
            return None
        try:
            anchor = common[-1]
            raw_close = float(frame.loc[anchor, "close"])
            adjusted_close = float(cached.loc[anchor, "close"])
            ratio = adjusted_close / raw_close if raw_close > 0 else 0.0
        except (KeyError, TypeError, ValueError, ZeroDivisionError):
            return None
        if not 0.5 <= ratio <= 2.0:
            return None
        out = frame.copy()
        for column in ("open", "close", "high", "low", "amount"):
            if column in out.columns:
                out[column] = out[column].astype(float) * ratio
        out.attrs.update({"source": "sina_anchor", "adjustment": "qfq", "anchor_ratio": ratio})
        return out
    if cached is not None and len(cached) > 50 and not fallback_source:
        latest = cached.index[-1].date()
        if required_date is not None:
            if latest >= required_date:
                # 文件可能已经由另一轮/进程写入最新K线，但清单仍停留在旧日期。
                # 不能因为“缓存已满足”直接返回而永远不修复清单，
                # 否则覆盖率页和盘后任务会持续误报数据缺失。
                manifest_last = str(cached_meta.get("last_date") or "")[:10]
                if manifest_last < latest.isoformat():
                    try:
                        dfc.save_kline(code, cached)
                    except Exception:
                        pass
                return True
        elif _history_is_fresh(latest):
            return True
    if fallback_source:
        cached = None
    for attempt in range(tries):
        if attempt > 0:
            time.sleep(min(0.5 * (2 ** (attempt - 1)), 3.0))
        try:
            if cached is not None and len(cached) > 50:
                last = str(cached.index[-1].date()).replace("-", "")
                df_new = dfc.fetch_kline(
                    code,
                    beg=last,
                    end=(required_date.strftime("%Y%m%d") if required_date is not None else "20500101"),
                )
                if df_new is not None and not df_new.empty:
                    newest = pd.to_datetime(df_new.index, errors="coerce").max()
                    if required_date is not None and (
                        pd.isna(newest) or newest.date() < required_date
                    ):
                        # A provider can return a valid but stale response.  Do not
                        # overwrite the cache or report success in that case.
                        try:
                            dfc.reset_data_source("历史行情源返回旧日期")
                        except Exception:
                            pass
                        time.sleep(0.3)
                        continue
                    if (
                        cached_meta.get("adjustment") == "qfq"
                        and df_new.attrs.get("adjustment") != "qfq"
                    ):
                        df_new = _anchor_unadjusted_increment(df_new)
                        if df_new is None:
                            # 宁可保留稍旧的前复权历史，也不能把两个不同价格口径拼接。
                            time.sleep(0.3)
                            continue
                    attrs = dict(df_new.attrs)
                    merged = pd.concat([cached[cached.index < df_new.index[0]], df_new])
                    merged.attrs.update(attrs)
                    dfc.save_kline(code, merged)
                    return True
            else:
                df = dfc.fetch_kline(
                    code,
                    beg=beg,
                    end=(required_date.strftime("%Y%m%d") if required_date is not None else "20500101"),
                )
                if df is not None and not df.empty:
                    newest = pd.to_datetime(df.index, errors="coerce").max()
                    if required_date is not None and (
                        pd.isna(newest) or newest.date() < required_date
                    ):
                        try:
                            dfc.reset_data_source("历史行情源返回旧日期")
                        except Exception:
                            pass
                        time.sleep(0.3)
                        continue
                    dfc.save_kline(code, df)
                    return True
        except Exception:
            try:
                dfc.reset_data_source("历史行情请求异常")
            except Exception:
                pass
        try:
            dfc.reset_data_source("历史行情源未返回当日数据")
        except Exception:
            pass
        time.sleep(0.3 * (attempt + 1))
    return False


MIN_STRATEGY_HISTORY_ROWS = 120
FULL_HISTORY_BACKFILL_BEG = "20230101"


def refresh_history(asof_day=None, workers=8, max_seconds=240):
    """Synchronously bring daily K-lines up to a completed market date.

    The old post-close job only rebuilt factors from the existing cache; it did
    not call ``init_history``.  This left the factor file one trading day behind
    while the job was still marked completed.  This bounded refresh is used by
    the close task before factors are rebuilt.  A stale provider response is
    treated as a failure rather than silently being saved.
    """
    import datetime as dt

    requested_date = None
    if asof_day is None:
        requested_date = dt.date.today()
    elif isinstance(asof_day, dt.datetime):
        requested_date = asof_day.date()
    elif isinstance(asof_day, dt.date):
        requested_date = asof_day
    else:
        requested_date = dt.date.fromisoformat(str(asof_day)[:10])
    target = latest_complete_trade_date(requested_date)
    existing = load_universe()
    if not existing:
        existing = build_universe(0)
    # 清单和 CSV 是两个持久化文件。容器重启、任务中断或旧版本写入时，
    # CSV 可能已更新而清单仍旧；每个完整交易日最多做一次本地重建，避免
    # 无意义的网络补下载和“数据不足”误报。
    recovery_path = os.path.join(dfc.CACHE_DIR, "kline_refresh_state.json")
    recovery_state = {}
    try:
        with open(recovery_path, "r", encoding="utf-8") as handle:
            recovery_state = json.load(handle) or {}
    except (OSError, ValueError, TypeError):
        recovery_state = {}
    cursor_key = target.isoformat()
    manifest_reconciled = False
    if recovery_state.get("manifest_reconciled_target") != cursor_key:
        try:
            dfc.rebuild_kline_manifest()
            manifest_reconciled = True
        except Exception:
            # 重建只是本地索引优化；失败不能阻断真实数据下载。
            pass
    manifest = dfc.get_kline_manifest()
    codes = []
    for row in existing:
        code = str(row.get("code") or "")
        if not code:
            continue
        last = str((manifest.get(code) or {}).get("last_date") or "")[:10]
        try:
            current = dt.date.fromisoformat(last)
        except ValueError:
            current = None
        meta = manifest.get(code) or {}
        # 日期已齐但仍是新浪不复权兜底的股票也必须进入升级队列；
        # 否则 refresh_history 会误认为 up_to_date，3435 只永远不会
        # 重试腾讯/东财前复权源。
        needs_adjustment_upgrade = (
            str(meta.get("source") or "").lower() == "sina"
            and str(meta.get("adjustment") or "").lower() != "qfq"
        )
        # A cache with only one or a few rows is not an incrementally updated
        # history.  The previous recovery path passed ``target`` as its begin
        # date for these files, repeatedly downloading one bar and overwriting
        # the cache forever.  Queue short files for a full, adjusted backfill.
        # Recent listings may legitimately remain below 120 bars; retrying a
        # small set at the bounded close job is harmless and lets them grow
        # into the normal strategy path without a separate migration.
        try:
            cached_rows = int(meta.get("rows") or 0)
        except (TypeError, ValueError):
            cached_rows = 0
        needs_history_depth_repair = 0 < cached_rows < MIN_STRATEGY_HISTORY_ROWS
        if current is None or current < target or needs_adjustment_upgrade or needs_history_depth_repair:
            codes.append(code)
    if not codes:
        return {"status": "up_to_date", "target_date": target.isoformat(), "total": 0,
                "updated": 0, "failed": [], "elapsed_seconds": 0.0}

    # A bounded close job must not restart from the first stale symbol every
    # time.  Public history endpoints can throttle for several minutes; the old
    # implementation consequently retried the same prefix of the universe on
    # every 15:15/15:25 run and thousands of later symbols starved forever.
    # Persist only a recovery cursor (not market data) and rotate the stale list
    # before submitting work.  A restart therefore resumes at the next symbol.
    start_cursor = int(recovery_state.get("cursor", 0) or 0) if recovery_state.get("target_date") == cursor_key else 0
    start_cursor %= len(codes)
    rotated_codes = codes[start_cursor:] + codes[:start_cursor]

    started = time.monotonic()
    failed = []
    submitted_codes = []
    updated = 0
    # 分批提交并严格遵守截止时间，避免一次性创建 5000 个线程任务把
    # 15:05 收盘任务拖到系统超时；未完成代码会保留在失败队列，下一次
    # 15:25/15:40/16:00 增量重试。
    deadline = started + max(30, int(max_seconds))
    cursor = 0
    # Four concurrent full-market refreshes repeatedly tripped the public
    # history endpoints' rate limiter.  Two/three workers are faster in
    # aggregate because they avoid a circuit-breaker storm and leave the
    # retry pass useful instead of returning thousands of false failures.
    pool_size = max(1, min(int(workers), 4))
    batch_size = max(pool_size * 4, 16)

    def _parse_retry_at(value):
        try:
            return dt.datetime.fromisoformat(str(value))
        except (TypeError, ValueError):
            return None

    # 失败项不能等完整五千只轮完才再试。保留一个有退避的、小型队列：
    # 每次恢复任务先给近期失败的代码机会，剩余容量继续推进全市场游标。
    now = dt.datetime.now()
    stale_set = set(codes)
    retry_by_code = {}
    for item in recovery_state.get("retry_queue") or []:
        if not isinstance(item, dict):
            continue
        code = str(item.get("code") or "")
        if code in stale_set:
            retry_by_code[code] = {
                "code": code,
                "attempts": max(0, int(item.get("attempts") or 0)),
                "next_retry_at": item.get("next_retry_at"),
            }
    due_retries = [
        item for item in retry_by_code.values()
        if (_parse_retry_at(item.get("next_retry_at")) or now) <= now
    ]
    due_retries.sort(key=lambda item: (item.get("next_retry_at") or "", item["code"]))
    retry_budget = min(len(due_retries), max(1, pool_size * 2))

    # Reuse one bounded executor for all batches.  Recreating it for each
    # 16/24-code batch caused hundreds of thread start/stop cycles during a
    # full-market refresh without increasing concurrency.
    executor = ThreadPoolExecutor(max_workers=pool_size)
    try:
        def _run_batch(batch_codes):
            nonlocal updated
            if not batch_codes or time.monotonic() >= deadline:
                return
            # _download_one performs a cheap incremental merge for normal caches.
            # For short/corrupt caches it must receive a true historical begin
            # date, never the current target date, otherwise it can only ever
            # rebuild a single daily row.
            futures = {executor.submit(_download_one, code, FULL_HISTORY_BACKFILL_BEG, 3, target): code
                       for code in batch_codes}
            submitted_codes.extend(batch_codes)
            try:
                remaining = max(1.0, deadline - time.monotonic())
                for future in as_completed(futures, timeout=remaining):
                    code = futures[future]
                    try:
                        if future.result():
                            updated += 1
                        else:
                            failed.append(code)
                    except Exception:
                        failed.append(code)
            except TimeoutError:
                for future in futures:
                    if not future.done():
                        failed.append(futures[future])
                        future.cancel()

        retry_attempted = [item["code"] for item in due_retries[:retry_budget]]
        _run_batch(retry_attempted)
        retry_attempted_set = set(retry_attempted)
        rotated_codes = [code for code in rotated_codes if code not in retry_attempted_set]
        while cursor < len(rotated_codes) and time.monotonic() < deadline:
            batch_codes = rotated_codes[cursor:cursor + batch_size]
            cursor += len(batch_codes)
            _run_batch(batch_codes)
            try:
                tmp_rp = f"{recovery_path}.{os.getpid()}.{uuid.uuid4().hex}.progress"
                with open(tmp_rp, "w", encoding="utf-8") as hp:
                    json.dump({"target_date": cursor_key, "cursor": (start_cursor + cursor) % len(codes),
                               "updated_so_far": updated, "saved_at": dt.datetime.now().isoformat()}, hp)
                os.replace(tmp_rp, recovery_path)
            except Exception:
                pass
            if time.monotonic() >= deadline:
                break
    finally:
        # ``wait=False`` lets in-flight downloader threads keep writing CSV
        # files after refresh_history() returns.  The post-close runner can
        # then rebuild selection factors from a half-finished batch.  A
        # timed-out batch is therefore still joined here; cancellation only
        # applies to work that has not started yet.
        executor.shutdown(wait=True, cancel_futures=True)
    # Advance by submitted symbols, even when the provider rejected them.  The
    # next scheduled pass explores the rest of the universe instead of looping
    # forever on a throttled prefix.  Once it wraps, still-stale symbols are
    # naturally retried on the following pass.
    # ``cursor`` is only advanced by fresh rotation codes. Retried failures do
    # not consume the global cursor, otherwise a busy failure queue could skip
    # large parts of the market indefinitely.
    next_cursor = (start_cursor + cursor) % len(codes)
    succeeded = set(submitted_codes) - set(failed)
    for code in succeeded:
        retry_by_code.pop(code, None)
    for code in set(failed):
        item = retry_by_code.get(code) or {"code": code, "attempts": 0}
        attempts = min(8, int(item.get("attempts") or 0) + 1)
        delay_minutes = min(60, 5 * (2 ** max(0, attempts - 1)))
        retry_by_code[code] = {
            "code": code,
            "attempts": attempts,
            "next_retry_at": (now + dt.timedelta(minutes=delay_minutes)).isoformat(timespec="seconds"),
        }
    retry_queue = sorted(
        retry_by_code.values(),
        key=lambda item: (item.get("next_retry_at") or "", item["code"]),
    )[:600]
    unprocessed = max(0, len(rotated_codes) - cursor)
    recovery_state = {
        "target_date": cursor_key,
        "cursor": next_cursor,
        "stale_total": len(codes),
        "submitted": len(submitted_codes),
        "updated": updated,
        "failed_submitted": len(set(failed)),
        "unprocessed": unprocessed,
        "retry_due": len(due_retries),
        "retry_attempted": len(retry_attempted),
        "retry_queue": retry_queue,
        # Only remember a successful reconciliation.  A transient filesystem
        # error must be retried on the next recovery pass rather than leaving
        # a stale manifest permanently trusted.
        "manifest_reconciled_target": cursor_key if manifest_reconciled else recovery_state.get("manifest_reconciled_target"),
        "manifest_reconciled": manifest_reconciled,
        "updated_at": dt.datetime.now().isoformat(timespec="seconds"),
    }
    try:
        tmp_path = f"{recovery_path}.{os.getpid()}.{uuid.uuid4().hex}.tmp"
        with open(tmp_path, "w", encoding="utf-8") as handle:
            json.dump(recovery_state, handle, ensure_ascii=False, indent=2)
        os.replace(tmp_path, recovery_path)
    except OSError:
        pass
    try:
        bench = dfc.load_cached_kline("BENCH_000300")
        if bench is None or bench.empty or bench.index[-1].date() < target:
            frame = dfc.fetch_kline(None, beg=target.strftime("%Y%m%d"), secid="1.000300")
            if frame is not None and not frame.empty:
                dfc.save_kline("BENCH_000300", frame)
    except Exception:
        pass
    dfc.flush_kline_manifest()
    invalidate_coverage_cache()
    return {
        "status": "ok" if not failed and not unprocessed else "partial",
        "requested_date": requested_date.isoformat() if requested_date else None,
        "target_date": target.isoformat(),
        "total": len(codes),
        "updated": updated,
        "failed_count": len(failed),
        "failed": sorted(set(failed))[:100],
        "unprocessed_count": unprocessed,
        "retry_queue_count": len(retry_queue),
        "recovery": recovery_state,
        "elapsed_seconds": round(time.monotonic() - started, 2),
    }

def init_history(beg="20230101", size=0, workers=6):
    """后台线程批量下载股票池历史K线"""
    with _state_lock:
        if INIT_STATE["status"] == "running":
            return False
        INIT_STATE.update({"status": "running", "phase": "preparing",
                           "done": 0, "errors": 0,
                           "retried": 0, "recovered": 0,
                           "last_error": None,
                           "started_at": time.strftime("%H:%M:%S")})
    def _run():
        lease_error = (None, None, None)
        try:
            # Hold the shared worker lease for the whole asynchronous job.
            # Acquiring it in the API handler would release it immediately
            # after this thread is created and would not protect the actual
            # download from the data-worker recovery queue.
            from resource_guard import heavy_job_lease
            admission_ctx = heavy_job_lease("init-history")
            admission = admission_ctx.__enter__()
            if not admission.get("allowed"):
                with _state_lock:
                    INIT_STATE.update({"status": "deferred", "phase": "deferred",
                                       "last_error": admission.get("reason")})
                admission_ctx.__exit__(None, None, None)
                return
        except Exception as exc:
            lease_error = (type(exc), exc, exc.__traceback__)
            with _state_lock:
                INIT_STATE.update({"status": "error", "phase": "error",
                                   "last_error": f"{type(exc).__name__}: {exc}"})
            return
        try:
            # 已有完整基础库时直接复用，避免每次增量更新都先等待一次全市场快照。
            existing = load_universe()
            if size == 0 and len(existing) >= 1000:
                uni = existing
            else:
                with _state_lock:
                    INIT_STATE["phase"] = "building_universe"
                uni = build_universe(size)
            codes = [
                row["code"]
                for row in uni
                if row.get("snapshot_tradable")
                or os.path.exists(dfc.kline_cache_path(row["code"]))
            ]
            with _state_lock:
                INIT_STATE["total"] = len(codes)
                INIT_STATE["phase"] = "downloading"
            failed = []
            with ThreadPoolExecutor(max_workers=workers) as ex:
                futs = {ex.submit(_download_one, c, beg): c for c in codes}
                for fut in as_completed(futs):
                    ok = fut.result()
                    with _state_lock:
                        INIT_STATE["done"] += 1
                        if not ok:
                            failed.append(futs[fut])
            # 对失败队列降低并发再补两轮；接口限流时这比整池重跑更可靠。
            initial_failed = len(failed)
            for retry_workers in (max(1, workers // 2), 1):
                if not failed:
                    break
                with _state_lock:
                    INIT_STATE["phase"] = "retrying"
                retry_codes = failed
                failed = []
                with _state_lock:
                    INIT_STATE["retried"] += len(retry_codes)
                with ThreadPoolExecutor(max_workers=retry_workers) as ex:
                    futs = {ex.submit(_download_one, c, beg, 2): c for c in retry_codes}
                    for fut in as_completed(futs):
                        if not fut.result():
                            failed.append(futs[fut])
            with _state_lock:
                INIT_STATE["errors"] = len(failed)
                INIT_STATE["recovered"] = initial_failed - len(failed)
            # 同时下载基准指数与海外指数
            with _state_lock:
                INIT_STATE["phase"] = "finalizing"
            try:
                bench = dfc.fetch_kline(None, beg=beg, secid="1.000300")
                if bench is not None and not bench.empty:
                    dfc.save_kline("BENCH_000300", bench)
            except Exception:
                pass
            # 海外免费源偶发长时间不可达，不应让 A 股全市场下载卡在“收尾”。
            threading.Thread(
                target=dfc.fetch_overseas_history,
                kwargs={"beg": beg},
                daemon=True,
            ).start()
            # K线文件已经变化，清除进程内回测矩阵，下一次请求重新加载。
            try:
                import backtest
                backtest.clear_matrix_cache()
            except Exception:
                pass
            dfc.flush_kline_manifest()
            with _state_lock:
                INIT_STATE["status"] = "done" if not failed else "partial"
                INIT_STATE["phase"] = "done" if not failed else "partial"
        except Exception as exc:
            lease_error = (type(exc), exc, exc.__traceback__)
            with _state_lock:
                INIT_STATE["status"] = "error"
                INIT_STATE["phase"] = "error"
                INIT_STATE["last_error"] = f"{type(exc).__name__}: {exc}"
        finally:
            try:
                admission_ctx.__exit__(*lease_error)
            except Exception:
                pass
    # The API only starts this worker; the lease must be held inside the
    # worker for the complete download, not around the thread creation.
    threading.Thread(target=_run, daemon=True).start()
    return True

def data_ready():
    return coverage_report()["ready"]


def coverage_report(cache_ttl=0):
    """按当前股票池计算文件、选股样本、回测样本和陈旧数据覆盖率。

    Formal selection/data gates use the default ``cache_ttl=0`` and therefore
    always calculate a fresh report.  Read-only dashboard callers may opt into
    the short TTL to avoid repeating a 5,000-file scan on every page poll.
    """
    try:
        cache_ttl = max(0.0, float(cache_ttl or 0))
    except (TypeError, ValueError):
        cache_ttl = 0.0
    cache_key = _coverage_cache_signature() if cache_ttl else None
    if cache_ttl:
        now = time.monotonic()
        with _coverage_cache_lock:
            cached = _coverage_cache.get("data")
            if (
                cached is not None
                and _coverage_cache.get("key") == cache_key
                and now - float(_coverage_cache.get("at") or 0.0) < cache_ttl
            ):
                return dict(cached)
    universe = load_universe()
    expected = {str(row["code"]) for row in universe}
    cached = {
        filename[:-4]
        for filename in os.listdir(dfc.KLINE_DIR)
        if filename.endswith(".csv") and not filename.startswith("BENCH")
    }
    # 无实时价格且从未产生过日线的证券是待上市新股；已有历史的停牌股仍属于必需覆盖。
    pending = {
        str(row["code"])
        for row in universe
        if not row.get("snapshot_tradable") and str(row["code"]) not in cached
    }
    required = expected - pending
    covered = required & cached
    missing = required - cached
    manifest = dfc.get_kline_manifest()
    usable_selection = {
        code for code in covered if int((manifest.get(code) or {}).get("rows") or 0) >= 65
    }
    usable_backtest = {
        code for code in covered if int((manifest.get(code) or {}).get("rows") or 0) >= 120
    }
    stale = set()
    for code in covered:
        last = (manifest.get(code) or {}).get("last_date")
        try:
            if not _history_is_fresh(last):
                stale.add(code)
        except ValueError:
            stale.add(code)
    # 长期停牌股永远等不到新 K 线：把它们的陈旧行继续留在分母里，会让
    # fresh_selection_pct 存在结构性上限（几只长期停牌的大盘股即可让
    # ready 永久为假）。仅排除“落后参考交易日超过 14 个自然日”的标的——
    # 常规停牌（数日内复牌）仍计入覆盖要求，保持对恢复任务的约束；
    # 数据源故障造成的大面积滞后不会被此规则掩盖（那种情况滞后天数相近
    # 且远小于 14 天）。
    reference = _previous_trade_weekday()
    long_suspended = set()
    for code in stale:
        last = str((manifest.get(code) or {}).get("last_date") or "")[:10]
        try:
            last_day = dt.date.fromisoformat(last)
        except (TypeError, ValueError):
            continue
        if (reference - last_day).days > 14:
            long_suspended.add(code)
    denominator_set = required - long_suspended
    denominator = len(denominator_set)
    selection_pct = round(len(usable_selection) / denominator * 100, 1) if denominator else 0.0
    fresh = covered - stale
    fresh_selection = usable_selection - stale
    fresh_pct = round(len(fresh) / denominator * 100, 1) if denominator else 0.0
    fresh_selection_pct = round(len(fresh_selection) / denominator * 100, 1) if denominator else 0.0
    fallback_unadjusted = {
        code
        for code in covered
        if (manifest.get(code) or {}).get("source") == "sina"
        and (manifest.get(code) or {}).get("adjustment") == "none"
    }
    # --- #6 行业级覆盖率 ---
    industry_groups = {}
    for row in universe:
        code = str(row.get("code") or "")
        if code not in denominator_set:
            continue
        industry = str(row.get("industry") or "未知").strip() or "未知"
        industry_groups.setdefault(industry, {"total": 0, "fresh": 0})
        industry_groups[industry]["total"] += 1
        if code in fresh:
            industry_groups[industry]["fresh"] += 1
    industry_gaps = []
    for ind, stats in sorted(industry_groups.items(), key=lambda x: x[1]["total"], reverse=True):
        pct = round(stats["fresh"] / max(stats["total"], 1) * 100, 1)
        if pct < 80.0 and stats["total"] >= 5:
            industry_gaps.append({"industry": ind, "fresh_pct": pct, "total": stats["total"], "stale": stats["total"] - stats["fresh"]})

    # --- #12 分层覆盖率（按流通市值） ---
    tier_def = {"large": (200e8, float("inf")), "mid": (50e8, 200e8), "small": (0, 50e8)}
    tier_stats = {t: {"total": 0, "fresh": 0} for t in tier_def}
    for row in universe:
        code = str(row.get("code") or "")
        if code not in denominator_set:
            continue
        try:
            cap = float(row.get("float_cap") or 0)
        except (TypeError, ValueError):
            cap = 0
        tier = "large" if cap >= 200e8 else ("mid" if cap >= 50e8 else "small")
        tier_stats[tier]["total"] += 1
        if code in fresh:
            tier_stats[tier]["fresh"] += 1
    tier_coverage = {}
    for t, stats in tier_stats.items():
        tier_coverage[t] = {
            "total": stats["total"],
            "fresh": stats["fresh"],
            "fresh_pct": round(stats["fresh"] / max(stats["total"], 1) * 100, 1),
        }

    # Ready condition: overall + per-tier minimums
    tier_ready = all(
        tc["fresh_pct"] >= {"large": 95.0, "mid": 90.0, "small": 85.0}.get(t, 85.0)
        for t, tc in tier_coverage.items()
        if tc["total"] > 0
    )
    ready = denominator >= 1000 and fresh_selection_pct >= 90.0 and tier_ready

    result = {
        "universe_size": len(expected),
        "history_required": denominator,
        "required_total": len(required),
        "long_suspended": len(long_suspended),
        "pending_listing": len(pending),
        "covered": len(covered),
        "missing": len(missing),
        "coverage_pct": round(len(covered) / denominator * 100, 1) if denominator else 0.0,
        "usable_selection": len(usable_selection),
        "usable_selection_pct": selection_pct,
        "usable_backtest": len(usable_backtest),
        "usable_backtest_pct": (
            round(len(usable_backtest) / denominator * 100, 1) if denominator else 0.0
        ),
        "fresh": len(fresh),
        "fresh_pct": fresh_pct,
        "fresh_selection": len(fresh_selection),
        "fresh_selection_pct": fresh_selection_pct,
        "expected_reference_date": _previous_trade_weekday().isoformat(),
        "stale": len(stale),
        "fallback_unadjusted": len(fallback_unadjusted),
        "ready": ready,
        "missing_codes": sorted(missing),
        "orphan_cache_files": len(cached - expected),
        "industry_gaps": industry_gaps,
        "tier_coverage": tier_coverage,
    }
    if cache_ttl:
        with _coverage_cache_lock:
            _coverage_cache.update({"key": cache_key, "at": time.monotonic(), "data": dict(result)})
    return result

# ---------- 科创板强势信号 → 映射所属板块的主板/创业板龙头 ----------
def star_leader_mapping(min_pct=5.0, top_star=10, leaders_per_sector=3):
    """旧客户端兼容的科创板行业联动观察。

    科创板和北交所已经直接进入全市场基础库；本函数只观察科创强势股的同行联动，
    不再把主板/创业板股票当成它们的替代品。
    龙头判定 = 同行业内 涨幅 + 主力净流入占比 + 流通市值 综合排序。"""
    snap = dfc.fetch_market_snapshot()
    star = [s for s in snap
            if str(s.get("code", "")).startswith(("688", "689"))
            and isinstance(s.get("pct"), (int, float)) and s["pct"] >= min_pct
            and s.get("name") and "ST" not in s["name"]]
    star.sort(key=lambda x: x["pct"], reverse=True)
    star = star[:top_star]
    if not star:
        return {"star_signals": [], "note": f"当前无涨幅≥{min_pct}%的科创板强势股"}
    # 主板/创业板候选（已剔ST）
    main_cx = [s for s in snap
               if str(s.get("code", "")).startswith(("60", "00", "30"))
               and s.get("name") and "ST" not in s["name"] and "退" not in s["name"]
               and isinstance(s.get("pct"), (int, float))]
    by_industry = {}
    for s in main_cx:
        ind = s.get("industry")
        if ind:
            by_industry.setdefault(ind, []).append(s)

    def _leader_score(s):
        pct = s.get("pct") or 0
        mp = s.get("main_pct") if isinstance(s.get("main_pct"), (int, float)) else 0
        cap = s.get("float_cap") or 0
        cap_score = min(cap / 5e10, 1.0)  # 500亿流通市值封顶
        return pct * 0.5 + mp * 0.3 + cap_score * 2.0

    out = []
    for st in star:
        ind = st.get("industry")
        cands = by_industry.get(ind, [])
        cands.sort(key=_leader_score, reverse=True)
        leaders = [{
            "code": c["code"], "name": c["name"],
            "pct": round(c["pct"], 2) if isinstance(c.get("pct"), (int, float)) else None,
            "main_pct": c.get("main_pct"),
            "float_cap_yi": round((c.get("float_cap") or 0) / 1e8, 1),
            "board": "创业板" if str(c["code"]).startswith("30") else "主板",
        } for c in cands[:leaders_per_sector]]
        out.append({
            "star_code": st["code"], "star_name": st["name"],
            "star_pct": round(st["pct"], 2), "industry": ind,
            "leaders": leaders,
            "hint": f"科创股 {st['name']} 大涨 {st['pct']:.1f}%，可关注同板块「{ind}」主板/创业板龙头替代",
        })
    return {
        "star_signals": out,
        "note": "科创板和北交所均已直接进入全市场基础库；同行联动仅供研究参考，不构成投资建议。",
    }

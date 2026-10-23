# -*- coding: utf-8 -*-
"""共享选股流程、结果缓存及后台上下文刷新；不创建网页应用。"""
import datetime
import json
import os
import threading
import time
import uuid
from contextlib import contextmanager

import pandas as pd

import data_fetcher as dfc
import decision_engine as DE
import factors as F
import market_data_contract as MDC
import market_data_service as MDSvc
import paper_trading as P
import selection_factor_store as SFS
import strategies as S
import universe as U

try:
    import fcntl as _selection_fcntl
except ImportError:  # pragma: no cover - Windows development only.
    _selection_fcntl = None

_SELECT_RESULT_CACHE = {}
_SELECT_RESULT_LOCK = threading.RLock()
_SELECT_RESULT_TTL_SECONDS = 60
_SUPER_FLOW_PATH = os.path.join(dfc.CACHE_DIR, "super_flow_cache.json")
_SUPER_FLOW_CACHE = {"data": None, "ts": 0.0, "refreshing": False, "retry_at": 0.0,
                     "file_signature": None}
_SUPER_FLOW_LOCK = threading.Lock()
_SUPER_FLOW_TTL_SECONDS = 300
_SUPER_FLOW_RETRY_SECONDS = 30
_SUPER_FLOW_FILE_LOCK_PATH = _SUPER_FLOW_PATH + ".lock"
_SELECTION_FACTORS_PATH = os.path.join(dfc.CACHE_DIR, "selection_factors.csv")
_SELECTION_META_PATH = os.path.join(dfc.CACHE_DIR, "selection_cache.json")
_SELECTION_RESULT_DIR = os.path.join(dfc.CACHE_DIR, "selection_results")
_SELECTION_CACHE = {
    "signature": None,
    "price_factors": None,
    "first_board_codes": None,
    "status": None,
}
_SELECTION_REQUIRED_COLUMNS = {
    "three_up",
    "boll_mid_breakout",
    "above_ma5_5d",
    "above_ma10_5d",
    "above_boll_mid",
    "above_ma60",
    "above_all_ma",
    "weekly_oversold",
    "monthly_oversold",
}
_SELECTION_LOCK = threading.Lock()
_LIVE_CACHE = {
    "finance": {"data": {}, "report_dates": []},
    "sentiment": {},
    "gate": None,
    "sector_flow": [],
    "news_hits": [],
    "indices": [],
}
_SELECTION_CONTEXT_LOCK = threading.Lock()
_SELECTION_CONTEXT_STATE = {
    key: {"refreshing": False, "retry_at": 0.0}
    for key in ("finance", "sentiment")
}
_SELECTION_CONTEXT_VERSION = 0
_SELECTION_CONTEXT_RETRY_SECONDS = 30
_SELECTION_CONTEXT_TTL = {"finance": 6 * 3600, "sentiment": 300}
_SELECTION_CONTEXT_PROVIDER_KEY = {"finance": "finance", "sentiment": "hot_100"}


def _health_file_signature(path):
    try:
        stat = os.stat(path)
        return (stat.st_mtime_ns, stat.st_size)
    except OSError:
        return None


def _selection_signature():
    return (
        _health_file_signature(dfc.KLINE_MANIFEST_PATH),
        _health_file_signature(U.UNIVERSE_PATH),
        dfc.SHARED_KLINE_SOURCE_VERSION,
        _health_file_signature(_SELECTION_FACTORS_PATH),
        _health_file_signature(_SELECTION_META_PATH),
    )


def _selection_result_path(strategy: str, topn: int) -> str:
    """Stable per-strategy result path so the UI can reopen the last run."""
    safe_strategy = "".join(ch for ch in str(strategy) if ch.isalnum() or ch in "_-") or "unknown"
    return os.path.join(_SELECTION_RESULT_DIR, f"{safe_strategy}_{int(topn)}.json")


def _save_selection_result(strategy: str, topn: int, result: dict) -> None:
    """Persist only completed selection results; failed/partial runs are never shown as latest."""
    if (not isinstance(result, dict) or result.get("need_init") or result.get("error")
            or result.get("context_pending")):
        return
    try:
        os.makedirs(_SELECTION_RESULT_DIR, exist_ok=True)
        payload = {
            "saved_at": datetime.datetime.now().isoformat(timespec="seconds"),
            "strategy": strategy,
            "topn": int(topn),
            "data": result,
        }
        path = _selection_result_path(strategy, topn)
        tmp = path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as handle:
            json.dump(payload, handle, ensure_ascii=False, separators=(",", ":"))
        os.replace(tmp, path)
    except Exception:
        # Persistence must never make a successful selection request fail.
        return


def _load_selection_result(strategy: str, topn: int):
    path = _selection_result_path(strategy, topn)
    try:
        with open(path, "r", encoding="utf-8") as handle:
            payload = json.load(handle)
        data = payload.get("data") if isinstance(payload, dict) else None
        if not isinstance(data, dict) or not data.get("picks"):
            return None
        data = dict(data)
        data["latest_saved_at"] = payload.get("saved_at")
        data["latest_strategy"] = payload.get("strategy", strategy)
        data["latest_topn"] = payload.get("topn", topn)
        return data
    except (OSError, ValueError, TypeError):
        return None


def _selection_result_staleness(data: dict):
    """Check whether a persisted result is still the current trading-day view.

    ``/api/select/latest`` is intentionally read-only, but showing a week-old
    result as if it were today's selection is worse than showing a short
    refresh state.  A result is current only when its run day and live quote
    day match the current trading day (or the latest Friday on a weekend),
    and its factor reference is not older than the shared coverage cutoff.
    """
    today = datetime.date.today()
    run_day = U.latest_complete_trade_date(today)
    saved_day = str(data.get("latest_saved_at") or "")[:10]
    if saved_day != run_day.isoformat():
        return True, f"结果保存于 {saved_day or '未知日期'}，当前交易日为 {run_day.isoformat()}"
    quality = data.get("data_quality") or {}
    expected = str((U.coverage_report() or {}).get("expected_reference_date") or "")[:10]
    reference = str(quality.get("reference_date") or "")[:10]
    if expected and (not reference or reference < expected):
        return True, f"因子仅更新到 {reference or '未知'}，当前至少需要 {expected}"
    live_day = str(quality.get("live_quote_at") or "")[:10]
    if live_day and live_day != run_day.isoformat():
        return True, f"实时行情停留在 {live_day}，尚未覆盖 {run_day.isoformat()}"
    return False, None


def _complete_daily_cutoff():
    """Return the latest complete daily bar (never today's partial bar)."""
    # Keep the selector's upper bound identical to the shared refresh/coverage
    # calendar.  A weekday is not necessarily a trading day (holiday), and a
    # provider can legitimately return a bar newer than the last completed
    # session while the current session is still in progress.
    return U.latest_complete_trade_date(datetime.date.today())


def _refresh_super_flow(topn):
    try:
        rows = dfc.fetch_fund_flow_rank(topn=topn)
        if not isinstance(rows, list) or not rows or any(not isinstance(row, dict) for row in rows):
            return
        payload = {"updated_at": time.time(), "data": rows}
        with _super_flow_file_lock():
            tmp_path = f"{_SUPER_FLOW_PATH}.{os.getpid()}.{uuid.uuid4().hex}.tmp"
            with open(tmp_path, "w", encoding="utf-8") as handle:
                json.dump(payload, handle, ensure_ascii=False)
            os.replace(tmp_path, _SUPER_FLOW_PATH)
            signature = _health_file_signature(_SUPER_FLOW_PATH)
            with _SUPER_FLOW_LOCK:
                _SUPER_FLOW_CACHE.update(data=rows, ts=payload["updated_at"], file_signature=signature)
        with _SELECT_RESULT_LOCK:
            _SELECT_RESULT_CACHE.clear()
    except Exception:
        # A failed background attempt must retain the last known facts/time.
        # The loader's retry deadline limits failures without blocking callers.
        return
    finally:
        with _SUPER_FLOW_LOCK:
            _SUPER_FLOW_CACHE.update(refreshing=False,
                                     retry_at=time.monotonic() + _SUPER_FLOW_RETRY_SECONDS)


@contextmanager
def _super_flow_file_lock():
    """Protect super-order flow cache writes across API worker processes."""
    handle = None
    try:
        handle = open(_SUPER_FLOW_FILE_LOCK_PATH, "a+", encoding="utf-8")
        if _selection_fcntl is not None:
            _selection_fcntl.flock(handle.fileno(), _selection_fcntl.LOCK_EX)
        yield
    finally:
        if handle is not None:
            try:
                if _selection_fcntl is not None:
                    _selection_fcntl.flock(handle.fileno(), _selection_fcntl.LOCK_UN)
            except OSError:
                pass
            handle.close()


def _schedule_super_flow_refresh(topn):
    """Coalesce refresh attempts in this process, including cold-cache failures."""
    now = time.monotonic()
    with _SUPER_FLOW_LOCK:
        if _SUPER_FLOW_CACHE["refreshing"] or now < _SUPER_FLOW_CACHE.get("retry_at", 0.0):
            return
        _SUPER_FLOW_CACHE.update(refreshing=True, retry_at=now + _SUPER_FLOW_RETRY_SECONDS)
    try:
        threading.Thread(
            target=_refresh_super_flow, args=(topn,),
            name="super-flow-refresh", daemon=True,
        ).start()
    except RuntimeError:
        with _SUPER_FLOW_LOCK:
            _SUPER_FLOW_CACHE["refreshing"] = False


def _load_super_flow(topn):
    """Read known facts immediately; a cache miss also refreshes in the background."""
    signature = _health_file_signature(_SUPER_FLOW_PATH)
    with _SUPER_FLOW_LOCK:
        if (_SUPER_FLOW_CACHE["data"] is None
                or _SUPER_FLOW_CACHE.get("file_signature") != signature):
            _SUPER_FLOW_CACHE["file_signature"] = signature
            try:
                with open(_SUPER_FLOW_PATH, "r", encoding="utf-8") as handle:
                    payload = json.load(handle)
                if (not isinstance(payload, dict) or not isinstance(payload.get("data"), list)
                        or any(not isinstance(row, dict) for row in payload["data"])):
                    raise ValueError("invalid fund flow cache")
                _SUPER_FLOW_CACHE.update(
                    data=payload.get("data") or [],
                    ts=float(payload.get("updated_at") or 0),
                )
            except (OSError, ValueError, TypeError):
                if _SUPER_FLOW_CACHE["data"] is None:
                    _SUPER_FLOW_CACHE["data"] = []
        age = time.time() - _SUPER_FLOW_CACHE["ts"]
        cached = _SUPER_FLOW_CACHE["data"]
    if not cached or age >= _SUPER_FLOW_TTL_SECONDS:
        _schedule_super_flow_refresh(topn)
    return cached or []


def _load_selection_base(*, with_status=False):
    """Read one validated factor snapshot, retaining its readiness diagnosis."""
    signature = (_selection_signature(), _complete_daily_cutoff(),
                 int(time.monotonic() // 60))
    with _SELECTION_LOCK:
        if (_SELECTION_CACHE["price_factors"] is not None
                and _SELECTION_CACHE["signature"] == signature):
            result = (_SELECTION_CACHE["price_factors"], _SELECTION_CACHE["first_board_codes"])
            return (*result, dict(_SELECTION_CACHE.get("status") or {})) if with_status else result
        status = {"stage": "factors", "state": "blocked", "target_date": str(signature[1])}
        price_factors, first_board_codes = pd.DataFrame(), set()
        try:
            candidate, meta = SFS.read(_SELECTION_FACTORS_PATH, _SELECTION_META_PATH)
            if not _SELECTION_REQUIRED_COLUMNS.issubset(candidate.columns):
                status.update(reason="选股因子缓存版本过旧，等待后台重建", reason_code="factor_schema_outdated")
            else:
                freshness = P._selection_factor_freshness(
                    candidate, U.load_universe(), datetime.date.today(), meta=meta)
                status.update(freshness)
                if freshness["passed"]:
                    price_factors = candidate
                    first_board_codes = set(meta.get("first_board_codes", []))
                    status.update(state="ready", reason_code=None)
                else:
                    status["reason_code"] = "factor_snapshot_not_ready"
        except FileNotFoundError:
            status.update(reason="共享选股因子尚未生成，等待后台更新", reason_code="factor_cache_missing")
        except (OSError, ValueError, TypeError, KeyError):
            status.update(reason="共享选股因子缓存不可用，等待后台重建", reason_code="factor_cache_invalid")
        # Cache negative readiness too; new manifest/universe/factor files
        # invalidate the signature immediately. Requests never rebuild history.
        _SELECTION_CACHE.update(signature=signature, price_factors=price_factors,
                                first_board_codes=first_board_codes, status=status)
        result = (price_factors, first_board_codes)
        return (*result, dict(status)) if with_status else result


def _context_has_data(key, value):
    rows = value.get("data") if key == "finance" and isinstance(value, dict) else value
    return isinstance(rows, dict) and bool(rows) and all(isinstance(row, dict) for row in rows.values())


def _selection_context_fresh(key, now):
    state = _SELECTION_CONTEXT_STATE[key]
    return (state.get("value_id") == id(_LIVE_CACHE[key])
            and state.get("day") == datetime.date.today().isoformat()
            and now < state.get("expires_at", 0.0))


def _selection_context_signature():
    with _SELECTION_CONTEXT_LOCK:
        now = time.monotonic()
        return (datetime.date.today().isoformat(),
                *(_selection_context_fresh(key, now) for key in _SELECTION_CONTEXT_TTL))


def _publish_selection_context(key, value, *, started_at=None):
    global _SELECTION_CONTEXT_VERSION
    now = time.monotonic()
    provider_age = dfc.cached_age_seconds(_SELECTION_CONTEXT_PROVIDER_KEY[key])
    age = provider_age if provider_age is not None else now - (started_at if started_at is not None else now)
    with _SELECTION_CONTEXT_LOCK:
        _LIVE_CACHE[key] = value
        _SELECTION_CONTEXT_STATE[key].update(
            expires_at=now + max(0.0, _SELECTION_CONTEXT_TTL[key] - age),
            day=datetime.date.today().isoformat(), value_id=id(value))
        _SELECTION_CONTEXT_VERSION += 1
    # The generation also protects a result still being computed while
    # this worker is waiting for the result lock.
    with _SELECT_RESULT_LOCK:
        _SELECT_RESULT_CACHE.clear()


def _refresh_selection_context(key, loader):
    started_at = time.monotonic()
    try:
        value = loader()
        if not _context_has_data(key, value):
            return
        _publish_selection_context(key, value, started_at=started_at)
    except Exception:
        return
    finally:
        with _SELECTION_CONTEXT_LOCK:
            _SELECTION_CONTEXT_STATE[key].update(
                refreshing=False, retry_at=time.monotonic() + _SELECTION_CONTEXT_RETRY_SECONDS)


def _load_selection_context(key, loader, *, background=True):
    """Only HTTP cold reads skip waiting; short-lived jobs retain sync reads."""
    should_refresh = False
    with _SELECTION_CONTEXT_LOCK:
        value = _LIVE_CACHE[key]
        state = _SELECTION_CONTEXT_STATE[key]
        now = time.monotonic()
        if _selection_context_fresh(key, now):
            return value
        if background and not state["refreshing"] and now >= state["retry_at"]:
            state.update(refreshing=True, retry_at=now + _SELECTION_CONTEXT_RETRY_SECONDS)
            should_refresh = True
    if not background:
        started_at = time.monotonic()
        try:
            value = loader()
            if _context_has_data(key, value):
                _publish_selection_context(key, value, started_at=started_at)
                with _SELECTION_CONTEXT_LOCK:
                    if _selection_context_fresh(key, time.monotonic()):
                        return value
        except Exception:
            pass
    if should_refresh:
        try:
            threading.Thread(target=_refresh_selection_context, args=(key, loader),
                             name=f"selection-{key}-refresh", daemon=True).start()
        except RuntimeError:
            with _SELECTION_CONTEXT_LOCK:
                _SELECTION_CONTEXT_STATE[key]["refreshing"] = False
    return {"data": {}, "report_dates": []} if key == "finance" else {}


def _warm_finance():
    _load_selection_context("finance", dfc.fetch_finance_latest)


def _buy_scope(code, name=None, risk_flag=None, instrument_type=None):
    """Single read-side eligibility rule for all buy suggestions.

    Execution has an equivalent hard gate in ``paper_trading``.  The public
    scanner/detail APIs must not describe a restricted board, ST stock or a
    non-equity instrument as a buyable idea simply because it has six digits.
    """
    code = str(code or "").strip()
    label = str(name or "").strip()
    upper = label.upper()
    kind = str(instrument_type or "").strip().lower()
    risk = str(risk_flag or "").strip().lower() in {"1", "true", "yes", "y", "risk", "st"}
    if len(code) != 6 or not code.isdigit():
        return {"allowed": False, "reason": "证券代码格式无效"}
    if risk or "ST" in upper or "退" in label:
        return {"allowed": False, "reason": "ST/退市风险证券不提供买入建议"}
    if kind in {"etf", "fund", "index", "bond", "convertible_bond", "unknown"}:
        return {"allowed": False, "reason": "当前系统仅研究可交易的沪深主板与创业板普通股"}
    if any(token in upper for token in ("ETF", "LOF", "基金", "指数")):
        return {"allowed": False, "reason": "基金或指数不属于当前股票买入范围"}
    if code.startswith(("688", "689")):
        return {"allowed": False, "reason": "科创板仅作产业共振参考，不生成买入建议"}
    if code.startswith(("4", "8", "92")):
        return {"allowed": False, "reason": "北交所股票不在当前交易权限范围"}
    if not code.startswith(("000", "001", "002", "003", "600", "601", "603", "605", "300", "301", "302")):
        return {"allowed": False, "reason": "不属于当前允许的沪深主板或创业板普通股范围"}
    return {"allowed": True, "reason": "沪深主板/创业板普通股"}


def _completed_daily_kline(frame):
    """Never pass an unfinished intraday daily bar into a buy decision."""
    if frame is None or frame.empty:
        return frame
    return frame.loc[frame.index.date <= _complete_daily_cutoff()]


def _select_uncached(
    strategy: str = "three_day",
    topn: int = 10,
    *, background_context: bool = False,
):
    # 「策略选股」页复用本流水线跑模拟盘模型族（paper_selection.STRATEGY_MODEL
    # → strategies.PAPER_WEIGHTS），run_strategy 会把这些 id 分派给
    # _run_paper_strategy，因此白名单必须同时放行两套 id 体系。
    if strategy not in S.STRATEGIES and strategy not in S.PAPER_WEIGHTS:
        return {"error": "未知策略"}
    # 选股和回测的完整度要求不同。回测需要近乎全市场连续历史；选股只要
    # 使用“最近完整交易日”已有的因子行即可。不要因少量停牌/更新失败股票
    # 把整个选股页面锁死，更不能让过期行进入本次筛选。
    coverage = U.coverage_report()
    # Public selection and the paper scanner must share the same complete-
    # universe bar.  Running at 80% while the health page reports 90% ready
    # creates two different answers for the same market snapshot.
    if coverage["fresh_selection"] < 1000 or coverage["fresh_selection_pct"] < 90.0:
        return {
            "need_init": True,
            "message": "服务器当前可用的最近交易日选股数据不足，请等待增量更新完成",
            "coverage": coverage,
        }
    uni = U.load_universe() or U.build_universe()
    price_f, first_board_codes, factor_status = _load_selection_base(with_status=True)
    if price_f.empty:
        message = factor_status.get("reason") or "共享选股因子未就绪，等待后台更新"
        cached_day, target_day = factor_status.get("factor_date"), factor_status.get("target_date")
        if cached_day and target_day and cached_day != target_day:
            message = f"选股因子仍为 {cached_day}，目标 {target_day}；等待后台重建"
        return {"need_init": True, "message": message, "data_status": factor_status}
    reference_date = str(coverage["expected_reference_date"])
    complete_cutoff = U.latest_complete_trade_date(datetime.date.today()).isoformat()
    factor_dates = price_f["last_date"].astype(str).str[:10]
    # Public selection uses the same exact-date rule as paper trading: the
    # retained historical factor rows must all be from the latest complete
    # daily bar, never a mixture of a few fresh rows and old cache rows.
    price_f = price_f.loc[factor_dates.eq(complete_cutoff)].copy()
    first_board_codes = set(first_board_codes) & set(price_f.index)
    if price_f.empty:
        return {
            "need_init": True,
            "message": "最近完整交易日的选股因子尚未生成，请等待增量更新完成",
            "coverage": coverage,
        }
    # 初始化时保存的全市场快照提供横截面数据；慢速实时/财务/海外接口只在后台刷新，
    # 不再阻塞每次选股。最终入选股票仍逐只经过执行风控。
    # Public selection and paper execution share the same tradable universe.
    # Keep STAR as context only (the strategy layer can turn its sector
    # strength into a bounded bonus for an allowed peer), never as a pick.
    universe_by_code = {str(row.get("code")): row for row in uni if row.get("code")}
    eligible_codes = {
        code for code, row in universe_by_code.items()
        if _buy_scope(
            code,
            row.get("name"),
            row.get("risk_flag"),
            row.get("instrument_type") or row.get("security_type"),
        )["allowed"]
    }
    # Keep STAR rows in the factor table as context only.  The strategy layer
    # still hard-filters them from candidates, but needs same-snapshot STAR
    # rows to compute the bounded industry impulse for permitted main/ChiNext
    # peers.  Filtering them before ``run_strategy`` silently made that bonus
    # always zero in the public selector.
    star_context_codes = {
        code for code in universe_by_code
        if str(code).startswith(("688", "689"))
    }
    context_codes = eligible_codes | star_context_codes
    price_f = price_f.loc[price_f.index.astype(str).isin(context_codes)].copy()
    first_board_codes = set(first_board_codes) & eligible_codes
    covered_codes = set(price_f.index.astype(str))
    eligible_factor_rows = len(covered_codes & eligible_codes)
    required_factor_rows = max(4000, int(len(eligible_codes) * 0.90 + 0.9999))
    if eligible_factor_rows < required_factor_rows:
        return {
            "need_init": True,
            "message": f"最近完整交易日历史因子覆盖不足 {eligible_factor_rows}/{len(eligible_codes)}，需至少 {required_factor_rows}，已停止使用旧因子",
            "factor_date": complete_cutoff,
            "factor_rows": len(price_f),
            "eligible_factor_rows": eligible_factor_rows,
            "required_factor_rows": required_factor_rows,
        }
    if price_f.empty:
        return {
            "need_init": True,
            "message": "最近完整交易日没有可用于主板/创业板的合规选股因子",
        }
    # 历史因子只取到最近完整交易日；但页面上的价格、涨跌幅、量比和资金流
    # 必须来自盘中实时快照，不能把初始化时写入 universe.json 的旧值标为“今日”。
    # 保留基础库的风险/板块字段，再由实时字段覆盖可变行情字段。
    today = datetime.date.today()
    quote_now = datetime.datetime.now().astimezone()
    now_clock = quote_now.time()
    is_open_session = U.is_trade_day(today) and datetime.time(9, 15) <= now_clock < datetime.time(15)
    if U.is_trade_day(today) and now_clock >= datetime.time(9, 15):
        expected_live_date = today.isoformat()
    else:
        expected_live_date = U.previous_trade_day(today).isoformat()
    def _quote_age_ok(value, max_minutes=20):
        text = str(value or "").strip()
        if not text:
            return False
        try:
            parsed = datetime.datetime.fromisoformat(text.replace("Z", "+00:00"))
            if parsed.tzinfo is None:
                parsed = parsed.replace(tzinfo=datetime.timezone(datetime.timedelta(hours=8)))
            age = (datetime.datetime.now(datetime.timezone.utc) - parsed.astimezone(datetime.timezone.utc)).total_seconds()
            return -120 <= age <= max_minutes * 60
        except (TypeError, ValueError, OverflowError):
            return False

    required_live = max(4000, int(len(eligible_codes) * 0.90 + 0.9999))

    # During the session require a genuinely recent quote.  After the close,
    # overnight, or on weekends/holidays before open, the last complete closing
    # snapshot remains valid for research/selection. Allow up to 14 days for holidays.
    quote_age_limit = 20 if expected_live_date == datetime.date.today().isoformat() and is_open_session else 14 * 24 * 60

    def _valid_live_rows(rows):
        return [
            row for row in (rows or [])
            if str(row.get("quote_at") or "")[:10] == expected_live_date
            and _quote_age_ok(row.get("quote_at"), quote_age_limit)
            and row.get("code")
            and isinstance(row.get("price"), (int, float)) and row.get("price") > 0
            and isinstance(row.get("pct"), (int, float))
        ]

    def _safe_live_snapshot(*, force=False):
        """Fetch live quotes without allowing a source exception to leak.

        Selection must fail closed on insufficient coverage, not fail open (or
        turn a transient provider exception into a 500 response).

        R24：selection 是决策路径（显式允许联网），取数与 freshness 都归
        Market Data Authority；它已经把 provider 异常收敛成"返回空"。
        """
        if is_open_session:
            return MDSvc.refresh_rows(force=force)
        # Off-session selection consumes a dated closing fact. A historical
        # closing request must never refresh today's prices into that date.
        reading = MDSvc.read_snapshot(
            MDC.CLOSE_SNAPSHOT_POLICY, now=quote_now, asof_day=expected_live_date)
        if reading.available and reading.freshness == MDC.FRESHNESS_FRESH and not force:
            return [dict(row) for row in MDC.close_snapshot_rows(reading.rows(), quote_now, expected_live_date)]
        if expected_live_date != today.isoformat():
            return []
        return MDSvc.refresh_rows(force=force, now=quote_now,
                                 policy=MDC.CLOSE_SNAPSHOT_POLICY, asof_day=expected_live_date)

    live_rows = _safe_live_snapshot()
    # A disk fallback can be complete in row count but several days old.  Do
    # not let that cache masquerade as today's real-time quote; force a source
    # health probe and one full refresh before failing closed.
    live_rows = _valid_live_rows(live_rows)
    if len(set(str(row.get("code")) for row in live_rows) & eligible_codes) < required_live:
        try:
            dfc.check_data_source_health(force=True)
        except Exception:
            pass
        live_rows = _safe_live_snapshot(force=True)
        live_rows = _valid_live_rows(live_rows)
    live_map = {
        str(row.get("code")): row
        for row in live_rows
        if row.get("code")
        and isinstance(row.get("price"), (int, float))
        and row.get("price") > 0
        and isinstance(row.get("pct"), (int, float))
    }
    eligible_live_codes = {
        code for code, row in universe_by_code.items()
        if _buy_scope(code, row.get("name"), row.get("risk_flag"),
                      row.get("instrument_type") or row.get("security_type"))["allowed"]
    }
    live_eligible = eligible_live_codes & set(live_map)
    live_coverage = len(live_eligible) / max(len(eligible_live_codes), 1)
    required_live = max(required_live, max(4000, int(len(eligible_live_codes) * 0.90 + 0.9999)))
    if len(live_eligible) < required_live or live_coverage < 0.90:
        return {
            "need_init": True,
            "message": f"实时行情覆盖不足 {len(live_eligible)}/{len(eligible_live_codes)}（{live_coverage * 100:.1f}%），已停止使用旧快照；请稍后重试",
        }
    snap_f = [
        {**row, **live_map[row["code"]]}
        for row in universe_by_code.values()
        if row["code"] in covered_codes and row["code"] in live_map
    ]
    if len(snap_f) < required_live:
        return {
            "need_init": True,
            "message": f"实时行情覆盖不足 {len(snap_f)}/{required_live}，已停止使用旧快照进行今日选股",
        }
    finance = _load_selection_context("finance", dfc.fetch_finance_latest,
                                      background=background_context)
    # 两套 cutoff 必须分开：``complete_cutoff`` 是"最近完整交易日"（盘中即昨天），
    # 只用于已收盘日线与财务披露；当日实时截面（PE/PB/换手/资金/行业）的决策时点
    # 是**现在**。混用会把盘中每一行都判成 future 并清空这些列。
    fund_f = F.compute_fundamental_factors(
        snap_f, finance, asof=complete_cutoff, snapshot_asof=F.live_snapshot_asof())
    # Technical factors remain anchored to downloaded complete daily K-lines;
    # mutable same-day fields must come from this validated live snapshot.
    price_f = price_f.copy()
    live_field_map = {
        "price": "price", "pct": "pct", "amount": "amount",
        "turnover": "turnover", "main_pct": "main_pct",
        "super_net_raw": "super_net", "main_net": "main_net",
    }
    factor_codes = set(price_f.index)
    for target, source in live_field_map.items():
        updates = {
            code: row[source] for code, row in live_map.items()
            if code in factor_codes and row.get(source) is not None
            and str(row[source]).strip() not in {"", "--", "-"}
        }
        if updates:
            values = pd.Series(updates)
            price_f.loc[values.index, target] = values.to_numpy()
    sentiment = _load_selection_context(
        "sentiment", lambda: F.compute_sentiment_factors(frozenset(eligible_codes)),
        background=background_context)
    # 实时资金流直接从同一份行情取（含 f62/f184），省去一次全市场排行抓取
    realtime_flow = {
        s["code"]: (
            s.get("main_pct")
            if isinstance(s.get("main_pct"), (int, float))
            else None
        )
        for s in snap_f
    }
    try:
        super_rows = _load_super_flow(topn=max(len(snap_f), 100))
    except Exception:
        super_rows = []
    has_expected_super = any(
        str(r.get("quote_at") or "")[:10] == expected_live_date
        and _quote_age_ok(r.get("quote_at"), quote_age_limit)
        for r in (super_rows or [])
    )
    if not has_expected_super:
        _schedule_super_flow_refresh(topn=max(len(snap_f), 100))
    # The flow rank endpoint is cached independently from the quote snapshot.
    # Never inject a row with an absent/old timestamp into today's cross-section;
    # such rows silently turn stale capital-flow data into a live factor.
    realtime_super_flow = {
        str(row.get("code")): row.get("super_net")
        for row in super_rows
        if row.get("code")
        and str(row.get("quote_at") or "")[:10] == expected_live_date
        and _quote_age_ok(row.get("quote_at"), quote_age_limit)
        and isinstance(row.get("super_net"), (int, float))
    }
    news_hits = _LIVE_CACHE["news_hits"]
    gate = _LIVE_CACHE["gate"] or {
        "light": "unknown",
        "detail": [],
        "advice": "海外历史源暂不可用，执行层按未知风险保守处理",
    }
    table = S.build_factor_table(
        price_f,
        fund_f,
        sentiment,
        realtime_flow,
        realtime_super_flow=realtime_super_flow,
    )
    result = S.run_strategy(
        strategy,
        table,
        topn=topn,
        news_hits=news_hits,
        gate=gate,
        first_board_codes=first_board_codes,
        replay_data_date=complete_cutoff,
        replay_required=True,
    )
    for pick in result.get("picks", []) or []:
        live_row = live_map.get(str(pick.get("code")))
        if live_row:
            pick["quote_at"] = live_row.get("quote_at")
            pick["quote_source"] = live_row.get("source") or "live_snapshot"
            pick["quote_data_scope"] = ("当日实时行情" if is_open_session else "最近完整交易日收盘价格") + "；技术指标使用最近完整交易日下载K线"
            pick["historical_factor_date"] = complete_cutoff
    result["universe_size"] = len(price_f)
    result["total_universe"] = len(uni)
    result["data_quality"] = {
        "reference_date": reference_date,
        "complete_cutoff": complete_cutoff,
        "eligible_factor_rows": len(price_f),
        "excluded_stale_rows": max(0, len(uni) - len(price_f)),
        "coverage_pct": coverage["fresh_selection_pct"],
        "live_quote_source": " / ".join(sorted({str(row.get("source") or "全市场快照（未逐行声明来源）") for row in snap_f})),
        "quote_policy": MDC.LIVE_MARKET_POLICY.name if is_open_session else MDC.CLOSE_SNAPSHOT_POLICY.name,
        "live_quote_at": max((str(row.get("quote_at") or "") for row in snap_f), default=None),
        "live_quote_coverage": len(snap_f),
        "live_eligible_coverage_pct": round(live_coverage * 100, 2),
        "kline_source": "与模拟盘共享：data_cache/klines（前复权日线）",
        "kline_source_version": dfc.SHARED_KLINE_SOURCE_VERSION,
        "kline_cache_path": dfc.KLINE_DIR,
        "note": "历史因子使用最近完整交易日；价格按盘中实时或指定交易日收盘口径读取，缺失字段保持未知。选股与模拟盘读取同一份前复权日线缓存。",
    }
    if background_context:
        result["context_pending"] = [key for key, value in (("finance", finance), ("sentiment", sentiment))
                                     if not _context_has_data(key, value)]
    result["finance_report_dates"] = finance.get("report_dates")
    result["latest_finance_report_date"] = (
        (finance.get("report_dates") or [None])[0]
    )
    result["annual_report_date"] = finance.get("annual_report_date")
    # 买入执行风控：每只策略选出的股必须经过六维核查 + T1-T5 时机判定
    snap_map = {s["code"]: s for s in snap_f}
    kline_map = {
        pick["code"]: _completed_daily_kline(dfc.load_shared_kline(pick["code"]))
        for pick in result.get("picks", [])
    }
    sector_flow = _LIVE_CACHE["sector_flow"]
    for p in result.get("picks", []):
        p["buy_decision"] = DE.buy_decision(
            p["code"], name=p.get("name"),
            kline=kline_map.get(p["code"]),
            snap=snap_map.get(p["code"]),
            sector_flow=sector_flow,
            overseas_gate=gate,
            news_hits=news_hits,
        )
    result["executable_count"] = sum(1 for p in result["picks"] if p["buy_decision"]["executable"])
    result["watchlist_count"] = sum(1 for p in result["picks"] if p["buy_decision"]["watchlist"])
    result["board_filter"] = "净利润优先按最新一季报/半年报/三季报累计进度判断，缺失时回退最近年报；公开研究策略仅允许沪深主板和创业板，统一排除ST/退市风险、科创板及北交所；科创板只作同产业实时共振加分"
    result["disclaimer"] = "选股结果经舆情否决+买入执行风控模型过滤：负面舆情一票否决，仅 T1/T2 为建议买入，T3 进入观察清单，T4/T5 不执行。仅供研究参考，不构成投资建议。"
    return result


def select(
    strategy: str = "three_day",
    topn: int = 10,
):
    """Coalesce identical selection requests on small servers."""
    key = (strategy, topn, _selection_signature(), _health_file_signature(_SUPER_FLOW_PATH),
           _SELECTION_CONTEXT_VERSION, _selection_context_signature())
    now = time.time()
    cached = _SELECT_RESULT_CACHE.get(key)
    if cached and now - cached["ts"] < _SELECT_RESULT_TTL_SECONDS:
        return cached["data"]
    with _SELECT_RESULT_LOCK:
        now = time.time()
        cached = _SELECT_RESULT_CACHE.get(key)
        if cached and now - cached["ts"] < _SELECT_RESULT_TTL_SECONDS:
            return cached["data"]
        result = _select_uncached(strategy=strategy, topn=topn, background_context=True)
        _SELECT_RESULT_CACHE.clear()
        _SELECT_RESULT_CACHE[key] = {"ts": time.time(), "data": result}
        _save_selection_result(strategy, topn, result)
        return result

# -*- coding: utf-8 -*-
"""业绩公告的**市场级摄取**：从公开公告镜像抓窗口内的公告，落进 owner ledger。

它只做"把外部世界的公告搬进 durable 事实"这一件事：

```text
市场级公告列表（按发布时间倒序，可按页翻）
    → 标题分类（业绩预告 / 业绩快报；英文镜像、子公司主体、说明会一律拒绝）
    → 逐条取原文
    → owner ledger 复算 + 落库（含 first_seen_at = 本轮观测时刻）
```

──────────────────────── 三条刻意的边界 ────────────────────────

1. **拿不到原文就不落库。** 原文取失败（超时 / 正文为空）时**不写行**，只计数，
   下一轮会自动重试。理由：ledger 对同一 ``announcement_id`` 的内容冲突是 fail closed
   的，先用空正文占位会让真实正文永远进不来 —— "暂时读不到"被永久固化成"读不懂"。
2. **一行多码就跳过。** 一条公告若关联多个证券代码（常见于控股股东的权益分派），
   "这条业绩公告属于哪家公司"本身就不确定，记到任一代码上都是编造。
3. **观测时刻由本模块给出，且只有一次。** 一行的 ``first_seen_at`` 是本轮**开始抓取**
   的时刻 —— 它不是来源说的发布时间，也不是逐行的本地时间。PIT 只要有一点含糊，
   历史可见性就会漂移。

本模块不 import 任何 research / 研究层模块：它是 owner 侧的摄取 writer，方向单向。
"""

from __future__ import annotations

import datetime as dt
import json
import re
import threading
import time
from typing import Any, Callable, Iterable, Mapping
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import Request, urlopen

import earnings_disclosure_facts as EDF
import earnings_disclosure_ledger as LEDGER

__all__ = (
    "MARKET_ANNOUNCEMENT_URL",
    "ANNOUNCEMENT_BODY_URL",
    "SOURCE_NAME",
    "SOURCE_TYPE",
    "DEFAULT_LOOKBACK_DAYS",
    "DEFAULT_PAGE_SIZE",
    "DEFAULT_MAX_PAGES",
    "DEFAULT_MAX_BODIES",
    "fetch_window",
    "fetch_body",
    "ingest_window",
)

#: 公开公告镜像（列表 / 正文）。刻意与 ``disclosure_timeline`` 指向同一族端点：
#: 该项目对"公告"只有一个外部来源，另找一家会引入第二套时间戳语义。
MARKET_ANNOUNCEMENT_URL = "https://np-anotice-stock.eastmoney.com/api/security/ann"
ANNOUNCEMENT_BODY_URL = "https://np-cnotice-stock.eastmoney.com/api/content/ann"

SOURCE_NAME = "eastmoney-public-notice"
#: 聚合站，不是交易所签发的原始接口 —— 这个标签会一路进 evidence 的 detail。
SOURCE_TYPE = "public_aggregator"

DEFAULT_LOOKBACK_DAYS = 2
DEFAULT_PAGE_SIZE = 100
#: 40 页 × 100 条 = 4000 条/轮，够覆盖 2 个交易日的全市场公告（实测约 1400 条/日）。
DEFAULT_MAX_PAGES = 40
#: 单轮最多取多少条原文。抓正文是串行且有间隔的，这个上限把单轮时长钉在分钟级。
DEFAULT_MAX_BODIES = 120
DEFAULT_TIMEOUT_SECONDS = 15.0
MIN_REQUEST_INTERVAL_SECONDS = 0.35

_RATE_LOCK = threading.Lock()
_LAST_REQUEST_AT = 0.0

_CODE_RE = re.compile(r"^\d{6}$")


class IngestionError(RuntimeError):
    """整轮摄取无法继续（列表都取不到）。逐条失败不算错误，只计数。"""


def _throttle() -> None:
    global _LAST_REQUEST_AT
    with _RATE_LOCK:
        wait = MIN_REQUEST_INTERVAL_SECONDS - (time.monotonic() - _LAST_REQUEST_AT)
        if wait > 0:
            time.sleep(wait)
        _LAST_REQUEST_AT = time.monotonic()


def _request_json(url: str, params: Mapping[str, Any],
                  timeout: float = DEFAULT_TIMEOUT_SECONDS) -> Mapping[str, Any]:
    """一次有界请求，无隐藏重试（重试属于调度层，不属于单次抓取）。"""
    _throttle()
    query = urlencode({key: value for key, value in params.items() if value is not None})
    request = Request(
        f"{url}?{query}",
        headers={
            "User-Agent": "Mozilla/5.0 (compatible; astock-quant/1.0)",
            "Referer": "https://data.eastmoney.com/",
            "Accept": "application/json,text/plain,*/*",
        },
    )
    with urlopen(request, timeout=timeout) as response:
        payload = response.read()
    decoded = json.loads(payload.decode("utf-8", errors="replace"))
    return decoded if isinstance(decoded, Mapping) else {}


def _rows(payload: Mapping[str, Any]) -> list[dict]:
    data = payload.get("data") if isinstance(payload, Mapping) else None
    rows = data.get("list") if isinstance(data, Mapping) else []
    return [dict(row) for row in rows if isinstance(row, Mapping)]


def _total_hits(payload: Mapping[str, Any]) -> int | None:
    data = payload.get("data") if isinstance(payload, Mapping) else None
    total = data.get("total_hits") if isinstance(data, Mapping) else None
    try:
        return int(total)
    except (TypeError, ValueError):
        return None


def _single_code(row: Mapping[str, Any]) -> str | None:
    """这条公告唯一对应的证券代码；零个或多个都返回 ``None``（不猜）。"""
    values = row.get("codes") or []
    if isinstance(values, Mapping):
        values = [values]
    codes: list[str] = []
    for item in values if isinstance(values, Iterable) and not isinstance(values, (str, bytes)) else []:
        value = item.get("stock_code") if isinstance(item, Mapping) else item
        text = str(value or "").strip()
        if _CODE_RE.match(text) and text not in codes:
            codes.append(text)
    return codes[0] if len(codes) == 1 else None


def fetch_window(
    *,
    begin_day: dt.date,
    end_day: dt.date,
    transport: Callable[..., Mapping[str, Any]] | None = None,
    page_size: int = DEFAULT_PAGE_SIZE,
    max_pages: int = DEFAULT_MAX_PAGES,
) -> tuple[list[dict], dict]:
    """抓 ``[begin_day, end_day]`` 窗口内的市场级公告列表。

    返回 ``(rows, stats)``。列表本身取不到就抛 :class:`IngestionError` —— 那是
    "这一轮没跑"，不是"今天没有业绩公告"，两者绝不能混成同一个结果。
    """
    fetch = transport or _request_json
    rows: list[dict] = []
    pages = 0
    total_hits: int | None = None
    truncated = False
    last_error: str | None = None
    for page in range(1, max(1, int(max_pages)) + 1):
        try:
            payload = fetch(MARKET_ANNOUNCEMENT_URL, {
                "sr": -1, "st": "ann_date", "ann_type": "SHA,SZA,BJA",
                "page_size": max(1, min(int(page_size), 100)),
                "page_index": page,
                "begin_time": begin_day.isoformat(),
                "end_time": end_day.isoformat(),
            })
        except (HTTPError, URLError, TimeoutError, OSError, ValueError, TypeError) as exc:
            last_error = f"{type(exc).__name__}: {exc}"
            break
        page_rows = _rows(payload or {})
        pages = page
        if total_hits is None:
            total_hits = _total_hits(payload or {})
        rows.extend(page_rows)
        if len(page_rows) < page_size:
            break
    else:
        truncated = True

    if pages == 0:
        raise IngestionError(f"公告列表不可用：{last_error or '源未返回'}")
    stats = {
        "pages": pages,
        "rows": len(rows),
        "total_hits": total_hits,
        "truncated_pages": truncated,
        "list_error": last_error,
    }
    return rows, stats


def fetch_body(art_code: str, *, transport: Callable[..., Mapping[str, Any]] | None = None) -> str | None:
    """取一条公告的原文；取不到返回 ``None``（调用方据此跳过落库）。"""
    fetch = transport or _request_json
    text = str(art_code or "").strip()
    if not text:
        return None
    try:
        payload = fetch(ANNOUNCEMENT_BODY_URL, {
            "art_code": text, "client_source": "web", "page_index": 1,
        })
    except (HTTPError, URLError, TimeoutError, OSError, ValueError, TypeError):
        return None
    data = payload.get("data") if isinstance(payload, Mapping) else None
    body = data.get("notice_content") if isinstance(data, Mapping) else None
    return body if isinstance(body, str) and body.strip() else None


def ingest_window(
    *,
    conn,
    days: int = DEFAULT_LOOKBACK_DAYS,
    observed_at: Any = None,
    transport: Callable[..., Mapping[str, Any]] | None = None,
    body_transport: Callable[..., Mapping[str, Any]] | None = None,
    page_size: int = DEFAULT_PAGE_SIZE,
    max_pages: int = DEFAULT_MAX_PAGES,
    max_bodies: int = DEFAULT_MAX_BODIES,
) -> dict:
    """抓一个窗口并落库。返回可 JSON 序列化的摘要（供 cron 日志与 ``--status`` 对照）。"""
    started = time.monotonic()
    stamped = observed_at if observed_at is not None else dt.datetime.now(dt.timezone.utc)
    observed = LEDGER.owner_instant(stamped)  # 与 owner 同一套时区归一
    end_day = observed.date()
    lookback = max(1, int(days))
    begin_day = end_day - dt.timedelta(days=lookback - 1)

    rows, stats = fetch_window(
        begin_day=begin_day, end_day=end_day, transport=transport,
        page_size=page_size, max_pages=max_pages,
    )

    rejected: dict[str, int] = {}
    counters = {
        "candidates": 0,
        "recorded": 0,
        "duplicates": 0,
        "unparsable": 0,
        "conflicts": 0,
        "ambiguous_code": 0,
        "missing_id": 0,
        "body_failures": 0,
        "errors": 0,
    }
    truncated_bodies = False
    bodies = 0
    for row in rows:
        title = row.get("title") or row.get("title_ch")
        meta = EDF.classify_announcement(title)
        if not meta.supported:
            reason = str(meta.unsupported_reason)
            rejected[reason] = rejected.get(reason, 0) + 1
            continue
        counters["candidates"] += 1
        code = _single_code(row)
        if code is None:
            counters["ambiguous_code"] += 1
            continue
        art_code = str(row.get("art_code") or "").strip()
        if not art_code:
            counters["missing_id"] += 1
            continue
        if bodies >= max(1, int(max_bodies)):
            truncated_bodies = True
            break
        bodies += 1
        body = fetch_body(art_code, transport=body_transport or transport)
        if body is None:
            # 拿不到原文就不落库：先占位会让真实正文永远进不来（内容冲突 fail closed）。
            counters["body_failures"] += 1
            continue
        try:
            outcome = LEDGER.record_announcement(
                conn,
                code=code,
                title=title,
                announcement_id=art_code,
                source_name=SOURCE_NAME,
                source_type=SOURCE_TYPE,
                source_url=row.get("attach_url") or None,
                published_at=row.get("display_time") or row.get("eiTime"),
                body=body,
                observed_at=observed,
            )
        except LEDGER.EarningsLedgerError as exc:
            if exc.reason == "announcement_content_conflict":
                counters["conflicts"] += 1
            else:
                counters["errors"] += 1
            continue
        except Exception:  # noqa: BLE001 - 单条异常不得中断整轮
            counters["errors"] += 1
            continue
        if outcome.status == LEDGER.INGEST_RECORDED:
            counters["recorded"] += 1
            if outcome.verification_status == LEDGER.VERIFICATION_UNPARSABLE:
                counters["unparsable"] += 1
        elif outcome.status == LEDGER.INGEST_DUPLICATE:
            counters["duplicates"] += 1
        elif outcome.status == LEDGER.INGEST_REJECTED:
            reason = str(outcome.reason)
            rejected[reason] = rejected.get(reason, 0) + 1

    return {
        "observed_at": observed.isoformat(timespec="seconds"),
        "window": {"begin_day": begin_day.isoformat(), "end_day": end_day.isoformat()},
        "list": stats,
        "bodies_fetched": bodies,
        "truncated_bodies": truncated_bodies,
        "counters": counters,
        "rejected": dict(sorted(rejected.items())),
        "elapsed_s": round(time.monotonic() - started, 3),
    }

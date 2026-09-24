# -*- coding: utf-8 -*-
"""DeepSeek evidence reviewer and bounded paper-trading tuner.

The deterministic checks in this module establish the evidence.  The language
model may classify, explain and propose bounded paper-account patches.  It is
never used as a source of market truth, never emits orders, and never bypasses
the deterministic data-quality and evolution gates.

──────────────── R27-B2B：本模块里哪一部分已经迁移 ────────────────

``run_review``（``purpose='data_quality'`` 的市场数据质量研究运行）已经**迁出** legacy
路径，改由 typed research orchestration 承担：

    run_review
        → 从 R24 Market Data Authority 取 typed MarketDataReading
        → 建成 typed InformationEvent
        → ai_research_service（provider + canonical append）
        → ai_research_runs

因此 ``run_review`` **不再是** ``adaptive_advisor_runs`` 的 writer。该 purpose 的旧行继续
留在旧表（legacy rows stay legacy，不迁移、不回填）。

**本模块里没有迁移的是 tuner。** ``run_realtime_tuning`` / ``_tuning_*`` 仍然是有界调参
的 legacy writer，仍然走 ``call_json``。它写的是 `adaptive_selection_candidates`
（``status='shadow_proposal'``），属于 **proposal** 边界而不是 research —— 顺手在
research 迁移里改掉它，会同时改变它的业务 authority，因此刻意留给后续单独一轮。
这也意味着本模块**仍然**持有 legacy provider 网络调用（``call_json``），它不是 R27
provider 链路的一部分。
"""
from __future__ import annotations

import datetime as dt
import hashlib
import json
import math
import os
import sqlite3
import time
import urllib.error
import urllib.request
from collections import Counter
from zoneinfo import ZoneInfo


TZ = ZoneInfo("Asia/Shanghai")
from adaptive_common import _now  # C3: _loads 保留本地（空值语义与规范版不同）

import ai_research_contract as ARC
import ai_research_repository
import ai_research_service

PROVIDER = "DeepSeek"  # legacy display label; runtime selection is provider_name()
DEFAULT_MODEL = "deepseek-v4-flash"

#: 迁移后的 market-data-quality 研究运行在 canonical ledger 里使用的 purpose 标签。
#: 刻意保留 legacy 的字符串，因为**读侧**（``overview`` / ``deepseek_research``）要问
#: "这一类研究最近一次运行是什么"，两套标签会让同一种研究出现两个名字。
RESEARCH_PURPOSE_DATA_QUALITY = "data_quality"

#: 研究问题。它是 caller intent，不是 prompt：prompt 由 ``ai_research_provider`` 独占。
RESEARCH_QUESTION_DATA_QUALITY = "当前全市场行情快照自身是否完整、可解析、可用于研究？"

#: 把一条 typed 市场事实送进来的来源标签（审计用，不参与判定）。
RESEARCH_EVIDENCE_SOURCE = "market_data_service.read_snapshot_with_meta"

# Provider-neutral catalog: only DeepSeek is active today; Kimi/MIMO are
# deliberately capability placeholders until their credentials are configured.
PROVIDER_CATALOG = {
    "deepseek": {"label": "DeepSeek", "status": "active", "default_model": DEFAULT_MODEL, "base_url_env": "DEEPSEEK_BASE_URL", "api_key_env": "DEEPSEEK_API_KEY"},
    "kimi": {"label": "Kimi", "status": "reserved", "default_model": "moonshot-v1-8k", "base_url_env": "KIMI_BASE_URL", "api_key_env": "KIMI_API_KEY"},
    "mimo": {"label": "MIMO", "status": "active", "default_model": "mimo-v1", "base_url_env": "MIMO_BASE_URL", "api_key_env": "MIMO_API_KEY"},
}

def provider_catalog():
    return {key: dict(value) for key, value in PROVIDER_CATALOG.items()}

def provider_name():
    value = str(os.getenv("LLM_PROVIDER") or "deepseek").strip().lower()
    return value if value in PROVIDER_CATALOG else "deepseek"

SEVERITIES = {"critical", "high", "medium", "low", "info"}


def _loads(value, default=None):
    try:
        return json.loads(value)
    except (TypeError, ValueError):
        return default


def _finite(value):
    try:
        return math.isfinite(float(value))
    except (TypeError, ValueError):
        return False


def _truthy(value):
    return str(value or "").strip().lower() in {"1", "true", "yes", "on", "enabled"}


def enabled(config=None):
    return _truthy(os.getenv("LLM_ADVISOR_ENABLED")) or bool((config or {}).get("llm_advisor_enabled", False))


def configured():
    spec = PROVIDER_CATALOG[provider_name()]
    return bool(str(os.getenv(spec["api_key_env"]) or "").strip())


def model_name():
    provider = provider_name()
    env_name = PROVIDER_CATALOG[provider]["default_model"]
    return str(os.getenv(f"{provider.upper()}_MODEL") or env_name).strip() or env_name


def ensure_schema(conn):
    conn.executescript(
        """
        CREATE TABLE IF NOT EXISTS adaptive_advisor_runs(
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            purpose TEXT NOT NULL,
            trigger TEXT NOT NULL,
            status TEXT NOT NULL,
            provider TEXT NOT NULL,
            model TEXT NOT NULL,
            evidence_hash TEXT NOT NULL,
            evidence TEXT NOT NULL,
            report TEXT,
            error_code TEXT,
            latency_ms INTEGER,
            input_tokens INTEGER,
            output_tokens INTEGER,
            created_at TEXT NOT NULL,
            finished_at TEXT NOT NULL
        );
        CREATE INDEX IF NOT EXISTS idx_advisor_runs_recent
            ON adaptive_advisor_runs(id DESC);
        CREATE TABLE IF NOT EXISTS adaptive_ai_tuning_runs(
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            purpose TEXT NOT NULL,
            trigger TEXT NOT NULL,
            mode TEXT NOT NULL,
            status TEXT NOT NULL,
            profile_date TEXT,
            market_regime TEXT,
            provider TEXT NOT NULL,
            model TEXT NOT NULL,
            evidence_hash TEXT NOT NULL,
            evidence TEXT NOT NULL,
            response TEXT,
            proposals TEXT,
            applied_ids TEXT,
            reason TEXT,
            latency_ms INTEGER,
            created_at TEXT NOT NULL,
            finished_at TEXT NOT NULL
        );
        CREATE INDEX IF NOT EXISTS idx_ai_tuning_runs_recent
            ON adaptive_ai_tuning_runs(id DESC);
        """
    )


def _parse_time(value):
    text = str(value or "").strip()
    if not text:
        return None
    try:
        parsed = dt.datetime.fromisoformat(text.replace("Z", "+00:00"))
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=TZ)
        return parsed.astimezone(TZ)
    except ValueError:
        return None


def _close_cutoff(profile_date):
    """A 股收盘口径固定为北京时间 15:00；采集完成时间另行记录。"""
    try:
        return dt.datetime.fromisoformat(f"{str(profile_date)[:10]}T15:00:00+08:00")
    except ValueError:
        return None


def _market_reading():
    """取一份 R24 authority 的 typed 全市场事实；失败返回 ``(None, None)``。

    只经 Market Data Authority：迁移前遍历 ``snapshot_paths`` 裸读 JSON，第二个路径是
    20 页风险样本 —— 把它当全市场快照会让 coverage / 有效性统计系统性偏差，且绕过完整性
    校验。现在只认 authority 校验过的事实。

    刻意把**typed reading 本身**返回给调用方（而不只是 dict）：R27 的 typed evidence
    必须从 reading 派生，一个被 flatten 成 dict 的快照已经没有办法再被证明"这是哪一条
    事实"。
    """
    try:
        import market_data_service as MDSvc
        return MDSvc.read_snapshot_with_meta(now=dt.datetime.now(dt.timezone.utc))
    except Exception:
        return None, None


def _read_snapshot(snapshot_paths):
    """``_market_reading`` 的 legacy dict 视图（shape 保持不变，供 ``collect_evidence``）。

    ``collect_evidence`` 还要读 ``saved_at``，所以这里保留 ``payload`` dict + 来源标签的
    返回形状；typed 调用方一律走 :func:`_market_reading` 拿 reading。
    """
    reading, payload = _market_reading()
    if reading is None:
        return {}, None
    try:
        rows = [dict(row) for row in reading.rows()]
    except Exception:
        return {}, None
    if not rows:
        return {}, None
    merged = dict(payload) if isinstance(payload, dict) else {}
    merged["rows"] = rows
    return merged, "market_snapshot_full"


def _finding(code, severity, title, evidence, action):
    return {
        "code": code,
        "severity": severity if severity in SEVERITIES else "info",
        "title": title,
        "evidence": evidence,
        "recommended_action": action,
    }


def _secondary_quote_check(rows, important_codes):
    """Reconcile a deterministic sample against Tencent public quotes."""
    if not _truthy(os.getenv("SECONDARY_QUOTE_ENABLED")):
        return {"enabled": False, "status": "not_configured", "source": "tencent_public_quote",
                "requested_rows": 0, "returned_rows": 0, "compared_rows": 0, "passed_rows": 0,
                "coverage_pct": 0.0, "agreement_pct": 0.0, "mismatches": []}
    sample_size = max(20, min(120, int(os.getenv("SECONDARY_QUOTE_SAMPLE_SIZE") or 60)))
    primary = {str(row.get("code") or ""): row for row in rows if isinstance(row, dict) and str(row.get("code") or "")}
    ordered = sorted(primary)
    selected = [code for code in sorted(set(important_codes or [])) if code in primary]
    remaining = max(0, sample_size - len(selected))
    if ordered and remaining:
        step = max(len(ordered) / remaining, 1)
        selected.extend(ordered[min(int(index * step), len(ordered) - 1)] for index in range(remaining))
    selected = list(dict.fromkeys(selected))[:sample_size]
    try:
        import data_fetcher
        secondary_rows = data_fetcher.fetch_tencent_realtime_for_codes(selected)
    except Exception:
        secondary_rows = []
    secondary = {str(row.get("code") or ""): row for row in secondary_rows if isinstance(row, dict)}
    passed = 0
    compared = 0
    mismatches = []
    for code in selected:
        left, right = primary.get(code), secondary.get(code)
        if not left or not right or not _finite(left.get("price")) or not _finite(right.get("price")):
            continue
        compared += 1
        left_price, right_price = float(left["price"]), float(right["price"])
        price_diff = abs(left_price - right_price)
        price_ok = price_diff <= max(0.02, abs(left_price) * 0.003)
        pct_ok = True
        if _finite(left.get("pct")) and _finite(right.get("pct")):
            pct_ok = abs(float(left["pct"]) - float(right["pct"])) <= 0.35
        left_date = str(left.get("quote_at") or "")[:10].replace("-", "")
        right_digits = "".join(ch for ch in str(right.get("quote_at") or "") if ch.isdigit())
        right_date = right_digits[:8]
        date_ok = bool(left_date and right_date and left_date == right_date)
        if price_ok and pct_ok and date_ok:
            passed += 1
        elif len(mismatches) < 8:
            mismatches.append({
                "code": code, "primary_price": round(left_price, 4), "secondary_price": round(right_price, 4),
                "price_diff_pct": round(price_diff / max(abs(left_price), 0.01) * 100, 3),
                "primary_pct": left.get("pct"), "secondary_pct": right.get("pct"),
                "primary_date": left_date or None, "secondary_date": right_date or None,
            })
    coverage = 100.0 * len(secondary) / max(len(selected), 1)
    agreement = 100.0 * passed / max(compared, 1)
    if compared >= min(20, len(selected)) and coverage >= 80 and agreement >= 95:
        status = "verified"
    elif compared and coverage >= 50 and agreement >= 90:
        status = "partial"
    elif secondary:
        status = "failed"
    else:
        status = "unavailable"
    return {
        "enabled": True, "status": status, "primary_source": "eastmoney_public_quote",
        "source": "tencent_public_quote", "requested_rows": len(selected),
        "returned_rows": len(secondary), "compared_rows": compared, "passed_rows": passed,
        "coverage_pct": round(coverage, 2), "agreement_pct": round(agreement, 2),
        "price_tolerance": "max(0.02元,0.3%)", "pct_tolerance": "0.35个百分点",
        "date_must_match": True, "mismatches": mismatches,
    }


def collect_evidence(adaptive_conn, paper_db_path, snapshot_paths):
    """Return aggregate evidence only; no raw records or free-form user text."""
    now = dt.datetime.now(TZ)
    payload, source_file = _read_snapshot(snapshot_paths)
    rows = payload.get("rows") if isinstance(payload.get("rows"), list) else []
    codes = [str(row.get("code") or "").strip() for row in rows if isinstance(row, dict)]
    code_counts = Counter(code for code in codes if code)
    valid_prices = sum(1 for row in rows if isinstance(row, dict) and _finite(row.get("price")) and float(row["price"]) > 0)
    valid_pct = sum(1 for row in rows if isinstance(row, dict) and _finite(row.get("pct")) and abs(float(row["pct"])) <= 100)
    quote_times = [_parse_time(row.get("quote_at")) for row in rows if isinstance(row, dict)]
    quote_times = [item for item in quote_times if item]
    source_time = _parse_time(payload.get("saved_at")) or (max(quote_times) if quote_times else None)
    profile_dates = [item.date().isoformat() for item in quote_times]
    profile_date = Counter(profile_dates).most_common(1)[0][0] if profile_dates else (now.date().isoformat())
    cutoff = _close_cutoff(profile_date)
    closed = bool(cutoff and now >= cutoff)
    market_asof = cutoff if closed else source_time
    source_lag_minutes = round(max((cutoff - source_time).total_seconds(), 0) / 60, 1) if cutoff and source_time and source_time.date() == cutoff.date() else None
    freshness_minutes = round(max((now - source_time).total_seconds(), 0) / 60, 1) if source_time else None

    findings = []
    if not rows:
        findings.append(_finding("snapshot_empty", "critical", "行情快照为空", "未读取到任何行情行", "停止依赖该快照生成新信号并检查采集任务"))
    else:
        if len(rows) < 1000:
            findings.append(_finding("snapshot_low_coverage", "high", "行情覆盖不足", f"仅 {len(rows)} 行", "核对全市场采集范围和分页是否完整"))
        if valid_prices / max(len(rows), 1) < 0.95:
            findings.append(_finding("invalid_prices", "high", "有效价格比例偏低", f"有效 {valid_prices}/{len(rows)}", "隔离无效行并检查字段映射"))
        if valid_pct / max(len(rows), 1) < 0.98:
            findings.append(_finding("invalid_pct", "medium", "涨跌幅字段存在异常", f"有效 {valid_pct}/{len(rows)}", "复核单位、停牌值和异常极值"))
        duplicate_rows = sum(count - 1 for count in code_counts.values() if count > 1)
        if duplicate_rows:
            findings.append(_finding("duplicate_codes", "medium", "行情代码重复", f"重复行 {duplicate_rows}", "按证券代码与最新时间去重"))
        if sum(1 for code in codes if not code):
            findings.append(_finding("missing_codes", "high", "行情代码缺失", f"缺失 {sum(1 for code in codes if not code)} 行", "拒绝无代码记录进入选股链路"))
        if freshness_minutes is None:
            findings.append(_finding("timestamp_missing", "high", "行情时间不可判定", "快照和报价均缺少可解析时间", "补齐数据源时间戳并统一时区"))
        elif freshness_minutes > 24 * 60:
            findings.append(_finding("snapshot_stale", "high", "行情快照已过期", f"距最新时间约 {freshness_minutes:.0f} 分钟", "仅用于历史复盘，禁止标记为实时证据"))
        if closed and source_lag_minutes is not None and source_lag_minutes > 10:
            findings.append(_finding("close_snapshot_lag", "medium", "收盘快照早于15:00", f"源行情最后时间早于收盘口径 {source_lag_minutes:.1f} 分钟", "收盘后强制刷新全市场快照，并把源到达时间与收盘口径分开显示"))
    ledger = {
        "database_present": os.path.exists(paper_db_path),
        "accounts": 0,
        "orders": 0,
        "fills": 0,
        "positions": 0,
        "nav_rows": 0,
        "orphan_fills": 0,
        "filled_orders_without_fill": 0,
        "invalid_positions": 0,
        "negative_cash_accounts": 0,
        "fill_order_mismatches": 0,
    }
    important_codes = set()
    if not ledger["database_present"]:
        findings.append(_finding("ledger_missing", "critical", "模拟盘账本不存在", "未找到 paper_trading.sqlite3", "恢复账本后再运行学习与审查"))
    else:
        paper = sqlite3.connect(paper_db_path, timeout=10)
        try:
            queries = {
                "accounts": "SELECT COUNT(*) FROM paper_accounts",
                "orders": "SELECT COUNT(*) FROM paper_orders",
                "fills": "SELECT COUNT(*) FROM paper_fills",
                "positions": "SELECT COUNT(*) FROM paper_positions",
                "nav_rows": "SELECT COUNT(*) FROM paper_nav",
                "orphan_fills": "SELECT COUNT(*) FROM paper_fills f LEFT JOIN paper_orders o ON o.id=f.order_id WHERE o.id IS NULL",
                "filled_orders_without_fill": "SELECT COUNT(*) FROM paper_orders o LEFT JOIN paper_fills f ON f.order_id=o.id WHERE o.status='filled' AND f.id IS NULL",
                "invalid_positions": "SELECT COUNT(*) FROM paper_positions WHERE qty<0 OR cost<0",
                "negative_cash_accounts": "SELECT COUNT(*) FROM paper_accounts WHERE cash<0",
                "fill_order_mismatches": "SELECT COUNT(*) FROM paper_fills f JOIN paper_orders o ON o.id=f.order_id WHERE f.account_id<>o.account_id OR f.code<>o.code OR f.side<>o.side OR f.qty<>o.qty",
            }
            for key, query in queries.items():
                ledger[key] = int(paper.execute(query).fetchone()[0])
            important_codes.update(str(row[0]) for row in paper.execute("SELECT DISTINCT code FROM paper_positions"))
            important_codes.update(str(row[0]) for row in paper.execute(
                "SELECT DISTINCT code FROM paper_fills ORDER BY id DESC LIMIT 80"
            ))
        finally:
            paper.close()
        for key, title in (
            ("orphan_fills", "存在无对应委托的成交"),
            ("filled_orders_without_fill", "存在无成交记录的已成交委托"),
            ("invalid_positions", "存在负数持仓或成本"),
            ("negative_cash_accounts", "存在负现金账户"),
            ("fill_order_mismatches", "委托与成交关键字段不一致"),
        ):
            if ledger[key]:
                findings.append(_finding(key, "critical" if key in {"orphan_fills", "fill_order_mismatches"} else "high", title, f"发现 {ledger[key]} 条", "冻结相关样本进入进化奖励并执行账本对账"))
        if ledger["accounts"] and ledger["nav_rows"] < ledger["accounts"]:
            findings.append(_finding("nav_incomplete", "high", "账户净值记录不完整", f"账户 {ledger['accounts']}，净值记录 {ledger['nav_rows']}", "完成全部账户收盘估值后再评估收益"))

    cross_source = _secondary_quote_check(rows, important_codes)
    if cross_source["status"] != "verified":
        if cross_source["status"] == "partial":
            findings.append(_finding(
                "cross_source_partial", "medium", "跨源行情仅部分通过",
                f"覆盖 {cross_source['coverage_pct']:.1f}%，一致 {cross_source['agreement_pct']:.1f}%",
                "隔离不一致代码并复核时间差、停牌和复权口径",
            ))
        elif cross_source["status"] == "failed":
            findings.append(_finding(
                "cross_source_failed", "high", "跨源行情校验失败",
                f"覆盖 {cross_source['coverage_pct']:.1f}%，一致 {cross_source['agreement_pct']:.1f}%",
                "暂停使用不一致报价进入新信号并执行逐项对账",
            ))
        else:
            findings.append(_finding(
                "independent_source_missing", "medium", "尚未完成跨源真实性校验",
                "腾讯独立行情源未配置或本次不可用", "恢复第二行情源后再标记为跨源验证",
            ))

    adaptive_stats = {
        "profiles": int(adaptive_conn.execute("SELECT COUNT(*) FROM adaptive_market_profiles").fetchone()[0]),
        "rewards": int(adaptive_conn.execute("SELECT COUNT(*) FROM adaptive_rewards").fetchone()[0]),
        "runs_failed": int(adaptive_conn.execute("SELECT COUNT(*) FROM adaptive_runs WHERE status='failed'").fetchone()[0]),
    }
    max_severity = "info"
    rank = {"info": 0, "low": 1, "medium": 2, "high": 3, "critical": 4}
    if findings:
        max_severity = max((item["severity"] for item in findings), key=lambda value: rank[value])
    evidence = {
        "scope": "paper_trading_only",
        "generated_at": _now(),
        "truth_boundary": "跨源对账只能提高行情可信度，不能由语言模型单独证明数据真实；结论仍受覆盖率、时间与价格容差约束。",
        "market_snapshot": {
            "source_file": source_file,
            "source_count": (1 if source_file else 0) + (1 if cross_source.get("returned_rows") else 0),
            "rows": len(rows),
            "unique_codes": len(code_counts),
            "valid_price_rows": valid_prices,
            "valid_pct_rows": valid_pct,
            "latest_source_at": source_time.isoformat(timespec="seconds") if source_time else None,
            "market_asof_at": market_asof.isoformat(timespec="seconds") if market_asof else None,
            "session_status": "closed" if closed else "trading",
            "close_cutoff_at": cutoff.isoformat(timespec="seconds") if cutoff else None,
            "source_lag_minutes_to_close": source_lag_minutes,
            "freshness_minutes": freshness_minutes,
            "cross_source": cross_source,
        },
        "paper_ledger": ledger,
        "adaptive_learning": adaptive_stats,
        "deterministic_findings": findings,
        "deterministic_max_severity": max_severity,
    }
    canonical = json.dumps(evidence, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return evidence, hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def call_json(system, user, max_tokens=1800):
    provider = provider_name()
    spec = PROVIDER_CATALOG[provider]
    api_key = str(os.getenv(spec["api_key_env"]) or "").strip()
    if not api_key:
        raise RuntimeError("api_key_missing")
    defaults = {"deepseek": "https://api.deepseek.com", "kimi": "https://api.moonshot.cn/v1", "mimo": "https://api.mimo.ai/v1"}
    base_url = str(os.getenv(spec["base_url_env"]) or defaults[provider]).rstrip("/")
    model = model_name()
    body = {
        "model": model,
        "messages": [
            {"role": "system", "content": system},
            {"role": "user", "content": user},
        ],
        "thinking": {"type": "disabled"},
        "response_format": {"type": "json_object"},
        "max_tokens": max(400, min(int(max_tokens), 4000)),
        "stream": False,
    }
    request = urllib.request.Request(
        base_url + "/chat/completions",
        data=json.dumps(body, ensure_ascii=False).encode("utf-8"),
        headers={"Authorization": "Bearer " + api_key, "Content-Type": "application/json"},
        method="POST",
    )
    with urllib.request.urlopen(request, timeout=float(os.getenv("DEEPSEEK_TIMEOUT_SECONDS") or 35)) as response:
        payload = json.loads(response.read().decode("utf-8"))
    content = payload["choices"][0]["message"]["content"]
    usage = payload.get("usage") or {}
    return json.loads(content), int(usage.get("prompt_tokens") or 0), int(usage.get("completion_tokens") or 0)


# ─────────────────────────────────────────────────────────────────────────────
# R27-B2B：typed market evidence 与读投影
# ─────────────────────────────────────────────────────────────────────────────


def _market_observation(reading):
    """这条事实**自身内容**的可观测描述。

    刻意只放"快照里有什么"（行数、唯一代码数、有价行数、最新报价时间），**不放**任何判据
    结论（覆盖是否达标、能不能相信、要不要修数据）。判据是**研究结论**：必须由 provider
    提出、由 R27-A 的 ``status`` 表达。把本层的裁决塞进 payload 会让研究台账声称一个它
    没有做过的判断，也会把"确定性门禁结论"与"模型的研究推理"混成同一种东西。
    """
    try:
        rows = [dict(row) for row in reading.rows()]
    except Exception:
        rows = []
    codes = {str(row.get("code") or "").strip() for row in rows if isinstance(row, dict)}
    codes.discard("")
    priced = [
        row for row in rows
        if isinstance(row, dict) and _finite(row.get("price")) and float(row["price"]) > 0
    ]
    quotes = [_parse_time(row.get("quote_at")) for row in rows if isinstance(row, dict)]
    quotes = [stamp for stamp in quotes if stamp]
    snapshot = getattr(reading, "snapshot", None)
    return {
        "kind": str(getattr(snapshot, "kind", "") or ""),
        "observed_at": str(getattr(snapshot, "observed_at", "") or ""),
        "row_count": len(rows),
        "unique_code_count": len(codes),
        "priced_row_count": len(priced),
        "latest_quote_at": max(quotes).isoformat() if quotes else None,
    }


def market_research_events(snapshot_paths):
    """R24 Market Data Authority → typed R27 evidence（研究链路的**唯一**事实入口）。

    返回 ``()`` 表示这一次拿不到可证明的市场事实。那种情况下刻意**不**拼一条看起来像
    事实的对象：一个没有 authority 来源的 payload 会让研究台账伪造 provenance，而那正是
    R27 契约要根除的东西。调用方对空证据的处置见 :func:`run_review`。

    三件刻意不做的事：

    * **不**声明 ``verification`` / ``verification_method`` / ``source_id`` —— 它们由
      ``evidence_ref_from_market_reading`` 从 reading 投影派生，本层不参与；
    * **不**把跨源结论搬进 payload —— ``cross_source_verified`` 是 reading 自己的核验
      维度，由 R24 拥有；本层连比较都不做（比较 ``verification == "verified"`` 会把
      ``coverage_integrity`` 误读成逐票双源）；
    * **不**构造 reading。
    """
    reading, _payload = _market_reading()
    if reading is None:
        return ()
    try:
        ref = ARC.evidence_ref_from_market_reading(reading)
        observation = _market_observation(reading)
    except Exception:
        return ()
    return (
        ARC.InformationEvent(
            as_of=ref.as_of,
            source=RESEARCH_EVIDENCE_SOURCE,
            evidence_ref=ref,
            payload=observation,
        ),
    )


def _research_provider_config(connect_factory):
    """解析 canonical 槽位配置 —— provider 凭据的**唯一**来源。

    本轮不新增 provider 配置模型，也没有第三套 API Key：槽位词表与解析都在
    ``ai_review_service``。legacy 的 provider 选择（``LLM_PROVIDER``）按既有别名表映射到
    槽位，因此"这次研究由哪个厂商执行"不会被迁移顺手改掉。

    映射不存在时显式失败，而不是回落到某个槽位：静默换 provider 等于换掉这次研究的实际
    执行者，而那正是审计必须看得见的东西。
    """
    import ai_review_service
    try:
        slot = ai_review_service.resolve_slot(provider_name())
    except ValueError as exc:
        raise RuntimeError("research_provider_slot_unavailable") from exc
    with connect_factory() as conn:
        return ai_review_service.get_slot_config(conn, slot)


def _research_report_view(*, run_id, hypothesis, narrative, counter_arguments):
    """canonical research 投影 → 读侧稳定形状。

    同时接受"刚跑完的 typed hypothesis 投影"与"读出来的 persisted hypothesis 投影"：
    这两个本来就是**同一个形状**（``ResearchHypothesis.projection()`` 就是被持久化的那一
    份），因此不需要两套渲染逻辑，也就不会漂移。

    保留 legacy ``report`` 的键名，让既有读侧不必同时改接线；但语义来源变了：
    ``verdict`` 现在是 R27-A 派生的研究 ``status``（不再是模型自述的严重度），
    ``confidence`` 是 R27-A 的 ``[0, 1]``（不再是 0–100）。``cross_source_status`` 读的是
    契约的 ``cross_source_verified``（它再委托 R24），而不是比较 ``verification`` 字符串。

    ``authority`` / ``is_authoritative`` 是**常量**：读一条研究记录永远不会让调用方获得
    新的写权限，也因此 ``status == supported`` 不会在这里变成任何形式的批准。
    """
    evidence = hypothesis.get("evidence") or []
    cross_source_verified = any(
        bool(item.get("cross_source_verified"))
        for item in evidence if isinstance(item, dict)
    )
    thesis = str(hypothesis.get("thesis") or "")
    return {
        "verdict": hypothesis.get("status"),
        "reason": hypothesis.get("reason"),
        "thesis": thesis,
        "confidence": float(hypothesis.get("confidence") or 0.0),
        "summary": str(narrative or thesis),
        "counter_arguments": list(counter_arguments or ()),
        "cross_source_status": "verified" if cross_source_verified else "not_verified",
        "authority": "research",
        "is_authoritative": False,
        "research_run_id": run_id,
        "as_of": hypothesis.get("as_of"),
        "truth_claim": "evidence_review_not_truth_proof",
    }


def research_report_view(run):
    """一次刚完成的 typed 运行（``ResearchRunResult``）→ 读侧稳定形状。"""
    return _research_report_view(
        run_id=run.run_id,
        hypothesis=run.hypothesis.projection(),
        narrative=run.narrative,
        counter_arguments=run.counter_arguments,
    )


def research_report_view_from_row(row):
    """canonical **读投影**（``recent_runs`` / ``get_run`` 的输出）→ 读侧稳定形状。"""
    return _research_report_view(
        run_id=row["id"],
        hypothesis=row["hypothesis"],
        narrative=row["narrative"],
        counter_arguments=row["counter_arguments"],
    )


def latest_data_quality_research(conn):
    """canonical ledger 里最近一次 ``data_quality`` 研究运行；没有则 ``None``。

    **唯一**的"最新市场数据质量研究"读入口。``overview`` 与
    ``deepseek_research._latest_data_quality`` 都用它，避免两个读侧各自实现一遍"最近一条"
    而出现漂移。

    刻意先 ``ensure_schema``：读路径必须在**首次研究运行成功之前**也能工作。canonical 台账
    只有在一次 append 之后才存在，而 ``overview`` 是每次刷新概览都会走的路径 —— 少了这一步，
    全新库（或尚未跑过研究的库）上会直接 ``no such table: ai_research_runs``。这与
    ``ai_review_service.get_slot_config`` 的处理方式一致：读函数自己保证 schema 已建，
    而不是要求调用方先做一次写。

    刻意不吞 :class:`ai_research_repository.ResearchPersistenceError`：``recent_runs`` 对
    损坏行 fail closed，所以一条读不出来的台账记录必须表现为**损坏**，而不是"这里没有研究
    结论"。
    """
    ai_research_repository.ensure_schema(conn)
    rows = ai_research_repository.recent_runs(
        conn, limit=1, purpose=RESEARCH_PURPOSE_DATA_QUALITY,
    )
    return rows[0] if rows else None


def _canonical_research_display(row):
    """canonical 研究行 → legacy 展示形状的**兼容投影**（有明确删除条件）。

    存在的唯一理由：writer 迁到 canonical ledger 之后，``/ai/overview`` 的读路径不必同时
    在同一个 PR 里改接线。它**不**写入任何东西，也**不**把 canonical 行转回 legacy 表的
    形状去存储 —— 那会变成 dual-write。

    **删除条件**：R27-B3 提供 canonical research API / UI 的那一轮，前端直接读
    ``ai_research_runs`` 时，本函数与 ``overview`` 里的调用点一起删除。
    """
    return {
        "id": row["id"],
        "purpose": row["purpose"],
        "trigger": row["trigger"],
        "status": row["status"],
        "provider": row["provider_model"] or None,
        "model": row["provider_model"] or None,
        "evidence": {},
        "report": research_report_view_from_row(row),
        "created_at": row["created_at"],
        "finished_at": row["created_at"],
        "as_of": row["as_of"],
        "reason": row["reason"],
        "authority": "research",
        "is_authoritative": False,
        "research_run_id": row["id"],
        "source": "canonical_research_ledger",
    }


def run_review(connect_factory, paper_db_path, snapshot_paths, config=None, trigger="manual"):
    """Run one market-data-quality research run on the typed R27 path.

    R27-B2B 迁移：本函数从"legacy ``call_json`` + 写 ``adaptive_advisor_runs``"改成 typed
    research orchestration（``ai_research_service`` → ``ai_research_provider`` →
    ``ai_research_runs``）。返回 dict 的键**保持不变**，因此四个既有调用方与 ``overview``
    的接线不需要跟着改；``best-effort`` 语义也保持不变（只有 ``advisor_disabled`` /
    ``api_key_missing`` 会抛错，其余失败以明确的 ``status='failed'`` + ``error_code`` 返回）。

    ``paper_db_path`` / ``snapshot_paths`` 刻意保留在签名里（调用方契约不变），但本函数不再
    读它们 —— typed 路径只接受 authority 签发的事实。**这是本轮一处能力收窄**：legacy 的
    data_quality 证据里还混着模拟盘账本对账等**未类型化**的事实，把账本事实塞进市场事实的
    payload 会伪造 provenance，因此它们不进入这次研究。``collect_evidence`` 仍然保留这些
    确定性检查（tuner 门禁与 ``/ai/overview`` 仍在使用）。

    没有可证明的 typed 事实时，本函数**不调用 provider、不写 canonical 行**，直接返回明确
    失败。原因：一条没有证据的研究运行连 ``as_of``（业务日）都无法从事实派生，用墙上时钟
    补一个就伪造了 PIT 声明。
    """
    if not enabled(config):
        raise RuntimeError("advisor_disabled")
    if not configured():
        raise RuntimeError("api_key_missing")
    events = market_research_events(snapshot_paths)
    if not events:
        return {"id": None, "status": "failed", "report": None,
                "error_code": "market_evidence_unavailable", "latency_ms": 0}
    as_of = events[0].as_of
    try:
        run = ai_research_service.run_research_run(
            connect_factory,
            purpose=RESEARCH_PURPOSE_DATA_QUALITY,
            trigger=str(trigger or "manual"),
            hypothesis_id="market_data_quality@" + as_of,
            as_of=as_of,
            subject=events[0].evidence_id,
            question=RESEARCH_QUESTION_DATA_QUALITY,
            events=events,
            provider_config=_research_provider_config(connect_factory),
        )
    except ai_research_service.ResearchServiceError as exc:
        # 明确失败：**不**回落 legacy provider、**不**写 legacy 表、**不**假装"AI 没意见"。
        # provider 或持久化任一段失败都不留下 canonical 行，调用方必须看到失败。
        return {"id": None, "status": "failed", "report": None,
                "error_code": ("%s_%s" % (exc.stage, exc.reason))[:80], "latency_ms": 0}
    return {
        "id": run.run_id,
        "status": run.hypothesis.status,
        "report": research_report_view(run),
        "error_code": None,
        "latency_ms": run.latency_ms,
    }


def _tuning_accounts(paper_db_path):
    """Read only the small, auditable account state sent to the model."""
    try:
        paper = sqlite3.connect(paper_db_path, timeout=10)
        paper.row_factory = sqlite3.Row
        rows = paper.execute("SELECT id,version,style,params FROM paper_accounts ORDER BY id").fetchall()
    except (OSError, sqlite3.Error):
        return []
    finally:
        try:
            paper.close()
        except Exception:
            pass
    accounts = []
    for row in rows:
        params = _loads(row["params"], {}) or {}
        overlay = params.get("adaptive_selection") or {}
        accounts.append({
            "account_id": str(row["id"]),
            "version": str(row["version"] or ""),
            "style": str(row["style"] or ""),
            "weights": overlay.get("weights") or {},
            "entry_score_delta": overlay.get("entry_score_delta", params.get("entry_score_delta", params.get("min_t_score_delta", 0.0))),
            "conditions": overlay.get("conditions") or {},
            # Thresholds and conditions are intentionally not exposed as
            # writable AI fields.  They remain deterministic/manual-review
            # state even when legacy params contain them.
            "automatic_scope": "existing_factor_weights_only",
        })
    return accounts


def _tuning_prompt(evidence, accounts, mode):
    example = {
        "decision": "propose|hold",
        "confidence": 0,
        "market_regime": "momentum|rotation|risk_off|high_volatility|balanced|unclassified",
        "summary": "中文说明",
        "proposals": [{
            "account_id": "tq_breakout|trend_pullback|sector_rotation",
            "reason": "只说明证据和预期改善",
            "weights": {"仅使用当前账户已有因子": 0.25},
        }],
    }
    system = (
        "你是A股模拟盘的受约束调参器，不是交易员。只输出严格JSON。"
        "你不能下单、不能修改公共选股、不能新增未知因子、不能修改风控上限。"
        "只有在证据充分且置信度>=70时提出很小的模拟盘内部补丁；证据不足就hold。"
        "weights必须是该账户已有因子并且总和约等于1。"
        "自动层只允许调整已有因子权重，单个因子最多移动3个百分点；"
        "entry_score_delta、conditions、enabled、model_family和entry_paths必须保持不变，"
        "不得修改硬门槛、仓位、止损、板块权限或订单逻辑。不要把行情推测写成事实。"
    )
    user = (
        "调参模式=" + str(mode) + "\n格式示例=" + json.dumps(example, ensure_ascii=False) +
        "\n证据=" + json.dumps(evidence, ensure_ascii=False, separators=(",", ":")) +
        "\n账户状态=" + json.dumps(accounts, ensure_ascii=False, separators=(",", ":"))
    )
    return system, user


def _bounded_tuning_patch(account, proposal, model_id, base_weights, base_conditions, mode="intraday"):
    """Turn an AI suggestion into a factor-only, deterministic tiny patch.

    ``conditions`` and ``entry_score_delta`` used to be clipped here and then
    applied by the realtime path.  A clipped threshold is still a threshold
    change, so the automatic AI lane must not carry those fields at all.  The
    normal self-evolution/manual-review lane remains responsible for structural
    proposals.
    """
    import adaptive_selection as selection

    requested = proposal.get("weights") if isinstance(proposal, dict) else {}
    if not isinstance(requested, dict):
        requested = {}
    requested = {key: requested.get(key, value) for key, value in base_weights.items()}
    try:
        normalized = selection._normalize(requested)
    except Exception:
        normalized = dict(base_weights)
    # Every automatic mode has the same hard limit.  ``mode`` is retained for
    # API compatibility, but must never widen the permission boundary.
    max_weight_step = 0.03
    # Interpolate toward the normalized request as one simplex-preserving
    # move.  Clipping each component independently and normalizing afterwards
    # can make the normalization step push one factor beyond ±3%.
    max_requested_move = max(
        (abs(float(normalized[key]) - float(base_weights[key])) for key in base_weights),
        default=0.0,
    )
    blend = min(1.0, max_weight_step / max_requested_move) if max_requested_move else 0.0
    weights = {
        key: round(float(base_weights[key]) + (float(normalized[key]) - float(base_weights[key])) * blend, 6)
        for key in base_weights
    }
    # Keep the serialized simplex exact after decimal rounding.
    if weights:
        last_key = next(reversed(weights))
        weights[last_key] = round(weights[last_key] + (1.0 - sum(weights.values())), 6)
    # Do not echo threshold/condition values into the candidate.  Omitting them
    # is intentional: the applier rejects any automatic candidate that carries
    # such fields, and the existing account values remain untouched.
    return {"weights": weights} if weights != {
        key: round(float(value), 6) for key, value in base_weights.items()
    } else None


def _tuning_failure_row(conn, trigger, mode, profile, evidence, evidence_hash, status, reason, response=None, proposals=None, applied=None, started=None):
    finished = _now()
    cursor = conn.execute(
        """INSERT INTO adaptive_ai_tuning_runs(
           purpose,trigger,mode,status,profile_date,market_regime,provider,model,evidence_hash,
           evidence,response,proposals,applied_ids,reason,latency_ms,created_at,finished_at)
           VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
        ("bounded_tuning", str(trigger or "manual")[:80], str(mode or "intraday")[:30], status,
         (profile or {}).get("profile_date"), (profile or {}).get("regime"), PROVIDER, model_name(), evidence_hash,
         json.dumps(evidence or {}, ensure_ascii=False, separators=(",", ":")),
         json.dumps(response, ensure_ascii=False, separators=(",", ":")) if response is not None else None,
         json.dumps(proposals or [], ensure_ascii=False, separators=(",", ":")),
         json.dumps(applied or [], ensure_ascii=False, separators=(",", ":")),
         str(reason or "")[:500], None, started or finished, finished),
    )
    return cursor.lastrowid


def run_realtime_tuning(connect_factory, paper_db_path, snapshot_paths, config=None, profile=None,
                        trigger="scheduled-midday-ai", mode="intraday"):
    """Ask DeepSeek for bounded account patches and optionally apply them today.

    This is intentionally a paper-only control loop.  The model creates a
    shadow candidate; a human confirmation boundary is required before any
    account write.  No order path is imported or called here.  The historical
    realtime auto-apply flag is ignored deliberately so a legacy caller cannot
    bypass the confirmation boundary.
    """
    cfg = config or {}
    profile = profile or {}
    started_clock = time.monotonic()
    mode = str(mode or "intraday")[:30]
    with connect_factory() as conn:
        ensure_schema(conn)
        evidence, evidence_hash = collect_evidence(conn, paper_db_path, snapshot_paths)
        evidence["market_profile"] = {
            "profile_date": profile.get("profile_date"), "regime": profile.get("regime"),
            "quality": profile.get("quality"), "valid_rows": profile.get("valid_rows"),
            "source_at": profile.get("source_at"),
        }
        accounts = _tuning_accounts(paper_db_path)
        min_interval = max(5, int(cfg.get("llm_realtime_min_interval_minutes", 15) or 15))
        last = conn.execute(
            "SELECT finished_at FROM adaptive_ai_tuning_runs ORDER BY id DESC LIMIT 1"
        ).fetchone()
        last_at = _parse_time(last[0]) if last else None
        elapsed = (dt.datetime.now(TZ) - last_at).total_seconds() / 60 if last_at else None
        if elapsed is not None and elapsed < min_interval:
            run_id = _tuning_failure_row(conn, trigger, mode, profile, evidence, evidence_hash,
                                         "cooldown", f"距上次AI调参仅{elapsed:.1f}分钟，冷却{min_interval}分钟")
            return {"id": run_id, "status": "cooldown", "applied_ids": [], "reason": "cooldown"}
        if not enabled(cfg):
            run_id = _tuning_failure_row(conn, trigger, mode, profile, evidence, evidence_hash,
                                         "disabled", "DeepSeek调参开关未开启")
            return {"id": run_id, "status": "disabled", "applied_ids": [], "reason": "disabled"}
        if not configured():
            run_id = _tuning_failure_row(conn, trigger, mode, profile, evidence, evidence_hash,
                                         "blocked", "DeepSeek API密钥未配置")
            return {"id": run_id, "status": "blocked", "applied_ids": [], "reason": "api_key_missing"}
        quality = str(profile.get("quality") or "")
        cross_source = ((evidence.get("market_snapshot") or {}).get("cross_source") or {}).get("status")
        min_rows = max(100, int(cfg.get("llm_realtime_min_valid_rows", 1000) or 1000))
        require_cross = bool(cfg.get("llm_realtime_require_cross_source", True))
        if quality not in {"valid_close", "valid_intraday", "fresh"} or int(profile.get("valid_rows") or 0) < min_rows:
            reason = f"行情质量未达门槛:{quality or 'unknown'} / 有效行{profile.get('valid_rows') or 0}"
            run_id = _tuning_failure_row(conn, trigger, mode, profile, evidence, evidence_hash,
                                         "blocked_quality", reason)
            return {"id": run_id, "status": "blocked_quality", "applied_ids": [], "reason": reason}
        if require_cross and cross_source != "verified":
            reason = f"跨源校验未通过:{cross_source or 'unknown'}"
            run_id = _tuning_failure_row(conn, trigger, mode, profile, evidence, evidence_hash,
                                         "blocked_cross_source", reason)
            # A4：门禁连续拦截可观测——统计最近连续 blocked 次数，超阈值直接告警（空转不再安静）
            try:
                _recent = [r[0] for r in conn.execute(
                    "SELECT status FROM adaptive_ai_tuning_runs ORDER BY id DESC LIMIT 40"
                ).fetchall()]
                _consec = sum(1 for _s in _recent if isinstance(_s, str) and _s.startswith("blocked"))
                if _consec >= 5:
                    import json as _json_mod
                    print(_json_mod.dumps({"alarm": "ai_tuning_gate_blocked", "consecutive_blocked": _consec,
                                      "last_status": "blocked_cross_source",
                                      "note": "AI 调参门禁连续拦截 %d 次，第二数据源(cross_source)长期未 verified，请排查或放宽 llm_realtime_require_cross_source" % _consec},
                                     ensure_ascii=False), flush=True)
            except Exception:
                pass
            return {"id": run_id, "status": "blocked_cross_source", "applied_ids": [], "reason": reason}

        system, user = _tuning_prompt(evidence, accounts, mode)
        response = None
        proposals = []
        applied_ids = []
        try:
            response, input_tokens, output_tokens = call_json(system, user, 1800)
            if not isinstance(response, dict):
                raise ValueError("response_not_object")
            confidence = float(response.get("confidence") or 0)
            # Models sometimes express confidence as 0–1 despite the prompt's
            # 0–100 contract; normalize that harmless representation before
            # applying the deterministic 70-point gate.
            if 0 < confidence <= 1:
                confidence *= 100
            if str(response.get("decision") or "hold").lower() != "propose" or confidence < 70:
                run_id = _tuning_failure_row(conn, trigger, mode, profile, evidence, evidence_hash,
                                             "hold", f"AI建议保持不变，置信度{confidence:.0f}", response=response)
                return {"id": run_id, "status": "hold", "applied_ids": [], "response": response}
            import adaptive_selection as selection
            # ACCOUNT_NAMES is a mapping; use explicit keys to make unknown
            # model output impossible to apply.  (The previous dead
            # comprehension iterated the mapping and subscripted the string
            # keys, raising TypeError before this correct line ever ran.)
            account_map = {key: key for key in selection.ACCOUNT_NAMES}
            paper_accounts = {str(item["account_id"]): item for item in accounts}
            for raw in (response.get("proposals") or [])[:3]:
                if not isinstance(raw, dict):
                    continue
                account_id = str(raw.get("account_id") or "")
                # 三日策略是披露/均线硬规则账户，没有可自动微调的
                # PAPER_WEIGHTS 模型；它只进入影子复盘，不能因模型输出
                # 误把 KeyError 扩散为整轮调参失败。
                if account_id not in account_map or account_id not in paper_accounts \
                        or account_id not in selection.ACCOUNT_MODELS:
                    continue
                model_id = selection.ACCOUNT_MODELS[account_id]
                base_weights = dict(selection.BASE_WEIGHTS[model_id])
                current_overlay = paper_accounts[account_id].get("weights") or {}
                if set(current_overlay) == set(base_weights):
                    base_weights = selection._normalize(current_overlay)
                base_conditions = selection._conditions(model_id, paper_accounts[account_id].get("conditions") or {})
                patch = _bounded_tuning_patch(paper_accounts[account_id], raw, model_id,
                                               base_weights, base_conditions, mode=mode)
                if not patch:
                    continue
                proposals.append({"account_id": account_id, "reason": str(raw.get("reason") or "AI bounded patch")[:300],
                                  "candidate_params": patch, "confidence": round(confidence, 1)})
            if not proposals:
                run_id = _tuning_failure_row(conn, trigger, mode, profile, evidence, evidence_hash,
                                             "no_change", "未生成通过白名单和幅度限制的参数变更", response=response)
                return {"id": run_id, "status": "no_change", "applied_ids": [], "response": response}
            now = _now()
            for item in proposals:
                account_id = item["account_id"]
                model_id = selection.ACCOUNT_MODELS[account_id]
                regime = f"{str(profile.get('regime') or 'unclassified')}:ai:{now[11:16].replace(':', '')}"
                # Preserve the actual effective overlay sent to the model.  A
                # static base would make the candidate appear to start from
                # the wrong weights after a previous bounded adjustment.
                effective_weights = paper_accounts[account_id].get("weights") or {}
                if set(effective_weights) != set(selection.BASE_WEIGHTS[model_id]):
                    effective_weights = dict(selection.BASE_WEIGHTS[model_id])
                baseline = {"weights": selection._normalize(effective_weights),
                            "conditions": selection._conditions(model_id),
                            "entry_score_delta": float(paper_accounts[account_id].get("entry_score_delta") or 0)}
                candidate = item["candidate_params"]
                # Fail closed permanently: DeepSeek cannot write an account,
                # even when a stale/forged config explicitly asks for it.
                # Apply is available only through the human UI boundary.
                auto_apply = False
                candidate_status = "shadow_proposal"
                cursor = conn.execute(
                    """INSERT INTO adaptive_selection_candidates(
                       run_date,account_id,regime,model_id,baseline_params,candidate_params,evidence,status,tier,reason,created_at,updated_at)
                       VALUES(?,?,?,?,?,?,?,?,?,?,?,?)""",
                    (str(profile.get("profile_date") or dt.datetime.now(TZ).date().isoformat())[:10], account_id, regime, model_id,
                     json.dumps(baseline, ensure_ascii=False, separators=(",", ":")),
                     json.dumps(candidate, ensure_ascii=False, separators=(",", ":")),
                     json.dumps({"source": "DeepSeek", "confidence": item["confidence"], "evidence_hash": evidence_hash}, ensure_ascii=False),
                     candidate_status, "ai_realtime", item["reason"], now, now),
                )
            status = "applied" if applied_ids else ("shadow_proposal" if mode == "shadow" else "proposal_only")
            reason = (f"通过确定性门禁；应用{len(applied_ids)}/{len(proposals)}个模拟盘候选"
                      if auto_apply else "通过确定性门禁；仅保存候选，未同日应用")
            latency = round((time.monotonic() - started_clock) * 1000)
            cursor = conn.execute(
                """INSERT INTO adaptive_ai_tuning_runs(
                   purpose,trigger,mode,status,profile_date,market_regime,provider,model,evidence_hash,evidence,response,proposals,applied_ids,reason,latency_ms,created_at,finished_at)
                   VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                ("bounded_tuning", str(trigger or "manual")[:80], mode, status, profile.get("profile_date"), profile.get("regime"),
                 PROVIDER, model_name(), evidence_hash, json.dumps(evidence, ensure_ascii=False, separators=(",", ":")),
                 json.dumps(response, ensure_ascii=False, separators=(",", ":")), json.dumps(proposals, ensure_ascii=False, separators=(",", ":")),
                 json.dumps(applied_ids), reason, latency, now, _now()),
            )
            return {"id": int(cursor.lastrowid), "status": status, "applied_ids": applied_ids,
                    "proposals": proposals, "response": response, "reason": reason}
        except Exception as exc:
            run_id = _tuning_failure_row(conn, trigger, mode, profile, evidence, evidence_hash,
                                         "failed", f"{type(exc).__name__}: {exc}", response=response,
                                         proposals=proposals, applied=applied_ids,
                                         started=_now())
            return {"id": run_id, "status": "failed", "applied_ids": applied_ids,
                    "reason": f"{type(exc).__name__}: {exc}"}


def overview(conn, config=None):
    ensure_schema(conn)
    latest_by_purpose = {}
    for row in conn.execute("SELECT * FROM adaptive_advisor_runs ORDER BY id DESC LIMIT 100"):
        if row["purpose"] in latest_by_purpose:
            continue
        item = dict(row)
        item["evidence"] = _loads(item.get("evidence"), {})
        item["report"] = _loads(item.get("report"), None)
        item.pop("evidence_hash", None)
        latest_by_purpose[row["purpose"]] = item
    # R27-B2B：`data_quality` 这个 purpose 的 writer 已经迁到 canonical research
    # ledger，旧表只剩历史行。读侧因此以 canonical 为准；旧行仍然可见（legacy rows
    # stay legacy），但不再被当成"最新的市场数据质量研究"。
    latest = latest_by_purpose.get(RESEARCH_PURPOSE_DATA_QUALITY)
    if latest is not None:
        latest["source"] = "legacy_adaptive_advisor_runs"
    canonical = latest_data_quality_research(conn)
    if canonical is not None:
        latest = _canonical_research_display(canonical)
        latest_by_purpose[RESEARCH_PURPOSE_DATA_QUALITY] = latest
    tuning_row = conn.execute(
        "SELECT * FROM adaptive_ai_tuning_runs ORDER BY id DESC LIMIT 1"
    ).fetchone()
    latest_tuning = dict(tuning_row) if tuning_row else None
    if latest_tuning:
        for field in ("evidence", "response", "proposals", "applied_ids"):
            latest_tuning[field] = _loads(latest_tuning.get(field), [] if field in {"proposals", "applied_ids"} else {})
    return {
        "enabled": enabled(config),
        "configured": configured(),
        "provider": PROVIDER,
        "model": model_name(),
        "mode": "evidence+bounded_tuning",
        "scope": "paper_trading_only",
        "truth_boundary": "DeepSeek负责复核与解释证据，不能单独证明行情真实；真实门禁仍由时效、约束对账和独立数据源决定。",
        "can": ["审阅数据质量证据", "解释账本矛盾", "提出待验证原因", "生成收盘复盘摘要", "在模拟盘内提出并小步应用白名单参数"],
        "cannot": ["直接下单", "修改公共选股", "修改风控上限", "绕过门禁", "把单一来源包装成真实"],
        "latest": latest,
        "latest_by_purpose": latest_by_purpose,
        "realtime_tuning": {
            "enabled": bool((config or {}).get("llm_realtime_tuning_enabled", False)) and enabled(config),
            "auto_apply": False,
            "min_interval_minutes": int((config or {}).get("llm_realtime_min_interval_minutes", 15) or 15),
            "scope": "仅当前模拟账户；只生成影子候选；应用必须人工确认并重新校验",
            "latest": latest_tuning,
        },
    }

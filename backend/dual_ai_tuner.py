# -*- coding: utf-8 -*-
"""双AI共识调参器：MiMo + DeepSeek 并行调参，双方通过才生效。

设计原则：
- 两个AI模型独立分析同一份市场证据，各自生成调参提案
- 只有两个模型的提案方向一致（同向调整）且幅度差异不超过阈值时，才合并为最终提案
- 任一模型失败或意见分歧时，记录分歧原因，不执行调整
- 所有调参记录完整审计，可追溯每个AI的独立判断
"""
from __future__ import annotations

import json
import os
import time
import urllib.error
import urllib.request
from concurrent.futures import ThreadPoolExecutor, as_completed
from zoneinfo import ZoneInfo

TZ = ZoneInfo("Asia/Shanghai")
from adaptive_common import _now, _json, _loads  # C3: 收敛重复工具函数

# ─── 双AI配置 ───
DUAL_AI_VERSION = "dual-consensus-v1"

# MiMo API 配置
MIMO_DEFAULTS = {
    "base_url": "https://api.mimo.ai/v1",
    "model": "mimo-v1",
    "api_key_env": "MIMO_API_KEY",
    "base_url_env": "MIMO_BASE_URL",
    "model_env": "MIMO_MODEL",
    "timeout_env": "MIMO_TIMEOUT_SECONDS",
    "default_timeout": 40,
}

# DeepSeek API 配置
DEEPSEEK_DEFAULTS = {
    "base_url": "https://api.deepseek.com",
    "model": "deepseek-v4-flash",
    "api_key_env": "DEEPSEEK_API_KEY",
    "base_url_env": "DEEPSEEK_BASE_URL",
    "model_env": "DEEPSEEK_MODEL",
    "timeout_env": "DEEPSEEK_TIMEOUT_SECONDS",
    "default_timeout": 35,
}

# 共识阈值：两个AI的权重调整方向必须一致，幅度差异不超过此值
CONSENSUS_WEIGHT_DIRECTION_THRESHOLD = 0.005  # 权重方向一致性阈值
CONSENSUS_WEIGHT_MAGNITUDE_RATIO = 0.60       # 幅度比：较小/较大 >= 此值视为一致
CONSENSUS_DELTA_DIRECTION_THRESHOLD = 0.001   # 入场阈值方向一致性
CONSENSUS_CONDITION_MAGNITUDE_RATIO = 0.50    # 条件参数幅度一致性
# 共识采纳的置信度门禁与有界调整幅度：与单AI路径
# （deepseek_advisor._bounded_tuning_patch）保持一致。
CONSENSUS_MIN_CONFIDENCE = 70.0
CONSENSUS_MAX_WEIGHT_STEP = 0.03
CONSENSUS_MAX_DELTA_STEP = 0.005


def _num(value, default=None):
    try:
        v = float(value)
        return v if abs(v) < 1e15 else default
    except (TypeError, ValueError):
        return default


def ensure_schema(conn):
    """创建双AI调参相关的数据库表。"""
    conn.executescript("""
        CREATE TABLE IF NOT EXISTS dual_ai_tuning_runs(
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            trigger TEXT NOT NULL,
            mode TEXT NOT NULL,
            status TEXT NOT NULL,
            profile_date TEXT,
            market_regime TEXT,
            -- MiMo 侧
            mimo_status TEXT,
            mimo_model TEXT,
            mimo_response TEXT,
            mimo_proposals TEXT,
            mimo_latency_ms INTEGER,
            mimo_error TEXT,
            -- DeepSeek 侧
            deepseek_status TEXT,
            deepseek_model TEXT,
            deepseek_response TEXT,
            deepseek_proposals TEXT,
            deepseek_latency_ms INTEGER,
            deepseek_error TEXT,
            -- 共识结果
            consensus_result TEXT,
            consensus_reason TEXT,
            merged_proposals TEXT,
            applied_ids TEXT,
            -- 元数据
            evidence_hash TEXT NOT NULL,
            evidence TEXT NOT NULL,
            total_latency_ms INTEGER,
            created_at TEXT NOT NULL,
            finished_at TEXT NOT NULL
        );
        CREATE INDEX IF NOT EXISTS idx_dual_ai_runs_recent
            ON dual_ai_tuning_runs(id DESC);

        CREATE TABLE IF NOT EXISTS dual_ai_api_keys(
            provider TEXT PRIMARY KEY,
            api_key TEXT NOT NULL,
            base_url TEXT,
            model TEXT,
            enabled INTEGER NOT NULL DEFAULT 1,
            updated_at TEXT NOT NULL
        );
    """)


def get_api_keys(conn):
    """读取已配置的API Key状态（不返回明文key，只返回是否已配置）。"""
    rows = conn.execute(
        "SELECT provider, api_key, base_url, model, enabled, updated_at FROM dual_ai_api_keys"
    ).fetchall()
    result = {}
    for row in rows:
        key_value = row[1] or ""
        result[row[0]] = {
            "configured": bool(key_value.strip()),
            "key_preview": (key_value[:8] + "****" + key_value[-4:]) if len(key_value) > 12 else ("****" if key_value else ""),
            "base_url": row[2] or "",
            "model": row[3] or "",
            "enabled": bool(row[4]),
            "updated_at": row[5],
        }
    # 补充默认值
    for provider, defaults in [("mimo", MIMO_DEFAULTS), ("deepseek", DEEPSEEK_DEFAULTS)]:
        if provider not in result:
            result[provider] = {
                "configured": False,
                "key_preview": "",
                "base_url": defaults["base_url"],
                "model": defaults["model"],
                "enabled": True,
                "updated_at": None,
            }
    return result


def update_api_key(conn, provider, api_key=None, base_url=None, model=None, enabled=None):
    """更新某个AI供应商的API配置。"""
    if provider not in ("mimo", "deepseek"):
        raise ValueError("provider必须是 mimo 或 deepseek")
    now = _now()
    existing = conn.execute(
        "SELECT api_key, base_url, model, enabled FROM dual_ai_api_keys WHERE provider=?",
        (provider,)
    ).fetchone()
    if existing:
        new_key = api_key if api_key is not None else existing[0]
        new_url = base_url if base_url is not None else existing[1]
        new_model = model if model is not None else existing[2]
        new_enabled = enabled if enabled is not None else bool(existing[3])
    else:
        defaults = MIMO_DEFAULTS if provider == "mimo" else DEEPSEEK_DEFAULTS
        new_key = api_key or ""
        new_url = base_url or defaults["base_url"]
        new_model = model or defaults["model"]
        new_enabled = enabled if enabled is not None else True
    conn.execute(
        "INSERT INTO dual_ai_api_keys(provider,api_key,base_url,model,enabled,updated_at) "
        "VALUES(?,?,?,?,?,?) ON CONFLICT(provider) DO UPDATE SET "
        "api_key=excluded.api_key, base_url=excluded.base_url, model=excluded.model, "
        "enabled=excluded.enabled, updated_at=excluded.updated_at",
        (provider, new_key, new_url, new_model, int(new_enabled), now)
    )
    return get_api_keys(conn)


def _get_provider_config(conn, provider):
    """获取某供应商的完整调用配置。"""
    defaults = MIMO_DEFAULTS if provider == "mimo" else DEEPSEEK_DEFAULTS
    row = conn.execute(
        "SELECT api_key, base_url, model, enabled FROM dual_ai_api_keys WHERE provider=?",
        (provider,)
    ).fetchone()
    if row:
        api_key = row[0] or ""
        base_url = row[1] or defaults["base_url"]
        model = row[2] or defaults["model"]
        enabled = bool(row[3])
    else:
        api_key = str(os.getenv(defaults["api_key_env"]) or "").strip()
        base_url = str(os.getenv(defaults["base_url_env"]) or defaults["base_url"]).rstrip("/")
        model = str(os.getenv(defaults["model_env"]) or defaults["model"]).strip()
        enabled = bool(api_key)
    return {
        "provider": provider,
        "api_key": api_key,
        "base_url": base_url.rstrip("/"),
        "model": model,
        "enabled": enabled,
        "timeout": float(os.getenv(defaults["timeout_env"]) or defaults["default_timeout"]),
    }


def _call_single_ai(provider_config, system_prompt, user_prompt, max_tokens=1800):
    """调用单个AI模型，返回 (parsed_json, input_tokens, output_tokens, latency_ms)。"""
    api_key = provider_config["api_key"]
    if not api_key:
        raise RuntimeError(f"{provider_config['provider']}_api_key_missing")
    base_url = provider_config["base_url"]
    model = provider_config["model"]
    timeout = provider_config["timeout"]

    body = {
        "model": model,
        "messages": [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_prompt},
        ],
        "response_format": {"type": "json_object"},
        "max_tokens": max(400, min(int(max_tokens), 4000)),
        "stream": False,
    }
    # MiMo 可能不支持 thinking 参数，DeepSeek 支持
    if provider_config["provider"] == "deepseek":
        body["thinking"] = {"type": "disabled"}

    request = urllib.request.Request(
        base_url + "/chat/completions",
        data=json.dumps(body, ensure_ascii=False).encode("utf-8"),
        headers={"Authorization": "Bearer " + api_key, "Content-Type": "application/json"},
        method="POST",
    )
    started = time.monotonic()
    with urllib.request.urlopen(request, timeout=timeout) as response:
        payload = json.loads(response.read().decode("utf-8"))
    latency_ms = round((time.monotonic() - started) * 1000)
    content = payload["choices"][0]["message"]["content"]
    usage = payload.get("usage") or {}
    parsed = json.loads(content)
    return parsed, int(usage.get("prompt_tokens") or 0), int(usage.get("completion_tokens") or 0), latency_ms


def _build_tuning_system_prompt():
    """构建调参系统提示词。"""
    return (
        "你是A股模拟盘的受约束调参器，不是交易员。只输出严格JSON。\n"
        "你不能下单、不能修改公共选股、不能新增未知因子、不能修改风控上限。\n"
        "只有在证据充分且置信度>=70时提出很小的模拟盘内部补丁；证据不足就hold。\n"
        "weights必须是该账户已有因子并且总和约等于1，conditions只能使用已有条件名。\n"
        "本次是盘中小步调参，单个权重最多移动3个百分点，入场阈值最多移动0.005，\n"
        "不得切换条件enabled。不要把行情推测写成事实。"
    )


def _build_tuning_user_prompt(evidence, accounts, mode):
    """构建调参用户提示词。"""
    example = {
        "decision": "propose|hold",
        "confidence": 0,
        "market_regime": "momentum|rotation|risk_off|high_volatility|balanced|unclassified",
        "summary": "中文说明",
        "proposals": [{
            "account_id": "tq_breakout|trend_pullback|sector_rotation",
            "reason": "只说明证据和预期改善",
            "weights": {"仅使用当前账户已有因子": 0.25},
            "entry_score_delta": 0.0,
            "conditions": {"仅使用已有条件名": 0.0},
        }],
    }
    return (
        "调参模式=" + str(mode) + "\n格式示例=" + json.dumps(example, ensure_ascii=False) +
        "\n证据=" + json.dumps(evidence, ensure_ascii=False, separators=(",", ":")) +
        "\n账户状态=" + json.dumps(accounts, ensure_ascii=False, separators=(",", ":"))
    )


def _check_consensus(mimo_proposals, deepseek_proposals, accounts_map):
    """检查两个AI的提案是否达成共识。

    共识条件：
    1. 两个AI的 decision 都是 "propose"
    2. 对同一 account_id 的权重调整方向一致（同增/同减）
    3. 调整幅度比 >= CONSENSUS_WEIGHT_MAGNITUDE_RATIO
    4. 入场阈值调整方向一致

    返回 (consensus: bool, reason: str, merged: list)
    """
    if not mimo_proposals or not deepseek_proposals:
        return False, "至少一个AI未提出有效提案", []

    # 按 account_id 建立映射
    mimo_map = {}
    for p in mimo_proposals:
        aid = str(p.get("account_id", ""))
        if aid:
            mimo_map[aid] = p

    ds_map = {}
    for p in deepseek_proposals:
        aid = str(p.get("account_id", ""))
        if aid:
            ds_map[aid] = p

    # 必须覆盖相同的账户
    common_accounts = set(mimo_map.keys()) & set(ds_map.keys())
    if not common_accounts:
        return False, f"两个AI针对不同账户提出提案（MiMo:{list(mimo_map.keys())}, DeepSeek:{list(ds_map.keys())}）", []

    merged = []
    disagreements = []

    for account_id in sorted(common_accounts):
        mp = mimo_map[account_id]
        dp = ds_map[account_id]
        base = accounts_map.get(account_id, {})

        # 置信度门禁：单AI路径要求确定性 ≥70 才允许 propose；共识路径此前
        # 完全不读置信度，两个低置信提案方向一致即可通过。先归一化 0-1
        # 表示（与 advisor 相同规则），再执行同样的 70 分门槛。
        def _norm_confidence(value):
            v = _num(value, 0.0)
            if 0 < v <= 1:
                v *= 100
            return v

        m_conf = _norm_confidence(mp.get("confidence"))
        d_conf = _norm_confidence(dp.get("confidence"))
        if m_conf < CONSENSUS_MIN_CONFIDENCE or d_conf < CONSENSUS_MIN_CONFIDENCE:
            disagreements.append(
                f"[{account_id}] 置信度不足：MiMo={m_conf:.0f}，DeepSeek={d_conf:.0f}"
                f"（要求均≥{CONSENSUS_MIN_CONFIDENCE:.0f}）"
            )
            continue

        # 检查权重方向一致性
        mw = mp.get("weights") or {}
        dw = dp.get("weights") or {}
        base_weights = base.get("weights") or {}
        if not isinstance(mw, dict) or not isinstance(dw, dict) or not isinstance(base_weights, dict) or not base_weights:
            disagreements.append(f"[{account_id}] 权重格式无效")
            continue
        unknown_mimo = sorted(set(mw) - set(base_weights))
        unknown_deepseek = sorted(set(dw) - set(base_weights))
        if unknown_mimo or unknown_deepseek:
            # Unknown factors must never be averaged into the candidate.  A
            # zero-valued default would otherwise make a typo look like a
            # valid new factor and leak it into the shadow ledger.
            disagreements.append(
                f"[{account_id}] 未知因子拒绝：MiMo={unknown_mimo}, DeepSeek={unknown_deepseek}"
            )
            continue
        all_factors = set(list(base_weights.keys()) + list(mw.keys()) + list(dw.keys()))

        weight_consensus = True
        weight_details = {}
        for factor in all_factors:
            base_val = _num(base_weights.get(factor), 0.0)
            m_val = _num(mw.get(factor), base_val)
            d_val = _num(dw.get(factor), base_val)
            m_delta = m_val - base_val
            d_delta = d_val - base_val

            # 方向检查
            if m_delta * d_delta < -CONSENSUS_WEIGHT_DIRECTION_THRESHOLD ** 2:
                weight_consensus = False
                disagreements.append(f"[{account_id}] {factor}: MiMo={m_delta:+.4f} vs DeepSeek={d_delta:+.4f} 方向相反")
                continue

            # 幅度比检查（仅当两者都有显著调整时）
            if abs(m_delta) > 0.005 and abs(d_delta) > 0.005:
                ratio = min(abs(m_delta), abs(d_delta)) / max(abs(m_delta), abs(d_delta))
                if ratio < CONSENSUS_WEIGHT_MAGNITUDE_RATIO:
                    weight_consensus = False
                    disagreements.append(f"[{account_id}] {factor}: 幅度比={ratio:.2f} < {CONSENSUS_WEIGHT_MAGNITUDE_RATIO}")
                    continue

            # 取两者平均值作为共识值
            weight_details[factor] = round((m_val + d_val) / 2, 6)

        # 检查入场阈值方向一致性
        m_delta = _num(mp.get("entry_score_delta"), 0.0)
        d_delta = _num(dp.get("entry_score_delta"), 0.0)
        delta_consensus = True
        if abs(m_delta) > CONSENSUS_DELTA_DIRECTION_THRESHOLD and abs(d_delta) > CONSENSUS_DELTA_DIRECTION_THRESHOLD:
            if m_delta * d_delta < 0:
                delta_consensus = False
                disagreements.append(f"[{account_id}] 入场阈值: MiMo={m_delta:+.4f} vs DeepSeek={d_delta:+.4f} 方向相反")
        merged_delta = round((m_delta + d_delta) / 2, 6)

        # 检查条件参数一致性
        mc = mp.get("conditions") or {}
        dc = dp.get("conditions") or {}
        base_conditions = base.get("conditions") or {}
        condition_consensus = True
        merged_conditions = {}
        for key in set(list(base_conditions.keys()) + list(mc.keys()) + list(dc.keys())):
            if key == "enabled":
                merged_conditions["enabled"] = base_conditions.get("enabled", {})
                continue
            base_val = _num(base_conditions.get(key), 0.0)
            m_val = _num(mc.get(key), base_val)
            d_val = _num(dc.get(key), base_val)
            m_delta_c = m_val - base_val
            d_delta_c = d_val - base_val
            if abs(m_delta_c) > 0.01 and abs(d_delta_c) > 0.01:
                if m_delta_c * d_delta_c < 0:
                    condition_consensus = False
                    disagreements.append(f"[{account_id}] 条件 {key}: 方向相反")
                    continue
                ratio = min(abs(m_delta_c), abs(d_delta_c)) / max(abs(m_delta_c), abs(d_delta_c))
                if ratio < CONSENSUS_CONDITION_MAGNITUDE_RATIO:
                    condition_consensus = False
                    disagreements.append(f"[{account_id}] 条件 {key}: 幅度比={ratio:.2f}")
                    continue
            merged_conditions[key] = round((m_val + d_val) / 2, 6)

        if weight_consensus and delta_consensus and condition_consensus:
            # 有界调整：与单AI路径一致，共识合并值同样不允许一次跨越
            # 单因子±0.03、入场阈值±0.005、条件参数20%局部移动的边界。
            bounded_weights = {}
            for factor, value in weight_details.items():
                base_val = _num(base_weights.get(factor), value)
                bounded = min(max(value, base_val - CONSENSUS_MAX_WEIGHT_STEP),
                              base_val + CONSENSUS_MAX_WEIGHT_STEP)
                bounded_weights[factor] = min(max(bounded, 0.0), 1.0)
            try:
                import adaptive_selection as _selection
                if bounded_weights:
                    bounded_weights = dict(_selection._normalize(bounded_weights))
            except Exception:
                pass
            if set(bounded_weights) != set(base_weights) or any(
                    abs(_num(bounded_weights.get(key), 0.0) - _num(base_weights.get(key), 0.0))
                    > CONSENSUS_MAX_WEIGHT_STEP + 1e-6
                    for key in base_weights):
                disagreements.append(f"[{account_id}] 归一化后单因子权重变化超过±3%，拒绝共识")
                continue
            current_entry = _num(base.get("entry_score_delta"), merged_delta)
            merged_delta = round(
                current_entry + max(-CONSENSUS_MAX_DELTA_STEP,
                                    min(CONSENSUS_MAX_DELTA_STEP, merged_delta - current_entry)),
                6,
            )
            for key, value in list(merged_conditions.items()):
                current = _num(base_conditions.get(key), value)
                step = max(abs(_num(current, 0.0)) * 0.20, 0.05)
                merged_conditions[key] = round(current + max(-step, min(step, value - current)), 6)
            merged.append({
                "account_id": account_id,
                "reason": f"双AI共识：MiMo→{mp.get('reason','')[:60]} | DeepSeek→{dp.get('reason','')[:60]}",
                "weights": bounded_weights,
                "entry_score_delta": merged_delta,
                "conditions": merged_conditions,
                "consensus_source": "dual_ai_agreement",
                "mimo_confidence": m_conf,
                "deepseek_confidence": d_conf,
            })

    if disagreements:
        return False, "分歧：" + "; ".join(disagreements[:5]), []

    if not merged:
        return False, "无有效共识提案", []

    return True, f"双AI对 {len(merged)} 个账户达成共识", merged


def run_dual_ai_tuning(connect_factory, paper_db_path, snapshot_paths, evidence_collector,
                        tuning_accounts_fn, config=None, profile=None, trigger="scheduled", mode="intraday"):
    """执行一次双AI并行调参。

    流程：
    1. 收集市场证据
    2. 并行调用 MiMo 和 DeepSeek
    3. 解析双方提案
    4. 检查共识
    5. 合并共识提案并返回

    返回 dict 包含完整的审计信息。
    """
    profile = profile or {}
    started = time.monotonic()
    started_at = _now()
    mode = str(mode or "intraday")[:30]

    with connect_factory() as conn:
        ensure_schema(conn)
        # 收集证据
        evidence, evidence_hash = evidence_collector(conn, paper_db_path, snapshot_paths)
        evidence["market_profile"] = {
            "profile_date": profile.get("profile_date"),
            "regime": profile.get("regime"),
            "quality": profile.get("quality"),
            "valid_rows": profile.get("valid_rows"),
            "source_at": profile.get("source_at"),
        }
        accounts = tuning_accounts_fn(paper_db_path)
        accounts_map = {str(a.get("account_id", "")): a for a in accounts}

        # 获取双AI配置
        mimo_config = _get_provider_config(conn, "mimo")
        ds_config = _get_provider_config(conn, "deepseek")

    # 检查两个AI是否都已配置
    if not mimo_config["api_key"]:
        return _save_failure_row(connect_factory, trigger, mode, profile, evidence, evidence_hash,
                                 "mimo_not_configured", "MiMo API Key 未配置", started_at, started)
    if not ds_config["api_key"]:
        return _save_failure_row(connect_factory, trigger, mode, profile, evidence, evidence_hash,
                                 "deepseek_not_configured", "DeepSeek API Key 未配置", started_at, started)
    if not mimo_config["enabled"]:
        return _save_failure_row(connect_factory, trigger, mode, profile, evidence, evidence_hash,
                                 "mimo_disabled", "MiMo 已禁用", started_at, started)
    if not ds_config["enabled"]:
        return _save_failure_row(connect_factory, trigger, mode, profile, evidence, evidence_hash,
                                 "deepseek_disabled", "DeepSeek 已禁用", started_at, started)

    # 构建提示词
    system_prompt = _build_tuning_system_prompt()
    user_prompt = _build_tuning_user_prompt(evidence, accounts, mode)

    # 并行调用两个AI
    mimo_result = {"status": "pending", "response": None, "proposals": None, "latency_ms": None, "error": None, "model": mimo_config["model"]}
    ds_result = {"status": "pending", "response": None, "proposals": None, "latency_ms": None, "error": None, "model": ds_config["model"]}

    def call_mimo():
        try:
            parsed, in_tok, out_tok, lat = _call_single_ai(mimo_config, system_prompt, user_prompt)
            return {"status": "completed", "response": parsed, "proposals": parsed.get("proposals", []),
                    "latency_ms": lat, "error": None, "input_tokens": in_tok, "output_tokens": out_tok,
                    "decision": parsed.get("decision", "hold"), "confidence": parsed.get("confidence", 0),
                    "market_regime": parsed.get("market_regime", "unclassified"), "summary": parsed.get("summary", "")}
        except Exception as e:
            return {"status": "failed", "response": None, "proposals": None, "latency_ms": None,
                    "error": f"{type(e).__name__}: {str(e)[:200]}"}

    def call_deepseek():
        try:
            parsed, in_tok, out_tok, lat = _call_single_ai(ds_config, system_prompt, user_prompt)
            return {"status": "completed", "response": parsed, "proposals": parsed.get("proposals", []),
                    "latency_ms": lat, "error": None, "input_tokens": in_tok, "output_tokens": out_tok,
                    "decision": parsed.get("decision", "hold"), "confidence": parsed.get("confidence", 0),
                    "market_regime": parsed.get("market_regime", "unclassified"), "summary": parsed.get("summary", "")}
        except Exception as e:
            return {"status": "failed", "response": None, "proposals": None, "latency_ms": None,
                    "error": f"{type(e).__name__}: {str(e)[:200]}"}

    with ThreadPoolExecutor(max_workers=2, thread_name_prefix="dual-ai") as pool:
        futures = {
            pool.submit(call_mimo): "mimo",
            pool.submit(call_deepseek): "deepseek",
        }
        results = {}
        for future in as_completed(futures):
            key = futures[future]
            try:
                results[key] = future.result()
            except Exception as e:
                results[key] = {"status": "failed", "error": str(e)[:200], "proposals": None, "latency_ms": None}

    mimo_r = results.get("mimo", {})
    ds_r = results.get("deepseek", {})
    total_latency = round((time.monotonic() - started) * 1000)

    # 检查双方是否都成功
    both_ok = mimo_r.get("status") == "completed" and ds_r.get("status") == "completed"

    consensus = False
    consensus_reason = ""
    merged_proposals = []

    if both_ok:
        mimo_decision = mimo_r.get("decision", "hold")
        ds_decision = ds_r.get("decision", "hold")

        if mimo_decision == "hold" and ds_decision == "hold":
            consensus_reason = "双AI一致认为当前证据不足，保持现状"
        elif mimo_decision == "propose" and ds_decision == "propose":
            consensus, consensus_reason, merged_proposals = _check_consensus(
                mimo_r.get("proposals") or [],
                ds_r.get("proposals") or [],
                accounts_map
            )
        else:
            consensus_reason = f"决策分歧：MiMo={mimo_decision}, DeepSeek={ds_decision}"
    else:
        errors = []
        if mimo_r.get("status") == "failed":
            errors.append(f"MiMo失败: {mimo_r.get('error', 'unknown')}")
        if ds_r.get("status") == "failed":
            errors.append(f"DeepSeek失败: {ds_r.get('error', 'unknown')}")
        consensus_reason = "; ".join(errors)

    # 保存审计记录
    finished_at = _now()
    with connect_factory() as conn:
        ensure_schema(conn)
        cursor = conn.execute(
            """INSERT INTO dual_ai_tuning_runs(
                trigger, mode, status, profile_date, market_regime,
                mimo_status, mimo_model, mimo_response, mimo_proposals, mimo_latency_ms, mimo_error,
                deepseek_status, deepseek_model, deepseek_response, deepseek_proposals, deepseek_latency_ms, deepseek_error,
                consensus_result, consensus_reason, merged_proposals, applied_ids,
                evidence_hash, evidence, total_latency_ms, created_at, finished_at
            ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (
                str(trigger)[:80], mode,
                "consensus" if consensus else ("both_hold" if both_ok and not merged_proposals else "no_consensus"),
                profile.get("profile_date"), profile.get("regime"),
                mimo_r.get("status"), mimo_config["model"],
                _json(mimo_r.get("response")) if mimo_r.get("response") else None,
                _json(mimo_r.get("proposals")) if mimo_r.get("proposals") else None,
                mimo_r.get("latency_ms"), mimo_r.get("error"),
                ds_r.get("status"), ds_config["model"],
                _json(ds_r.get("response")) if ds_r.get("response") else None,
                _json(ds_r.get("proposals")) if ds_r.get("proposals") else None,
                ds_r.get("latency_ms"), ds_r.get("error"),
                "consensus" if consensus else "no_consensus",
                consensus_reason[:500],
                _json(merged_proposals) if merged_proposals else None,
                None,  # applied_ids 填充在实际应用后
                evidence_hash,
                _json(evidence),
                total_latency, started_at, finished_at
            )
        )
        run_id = cursor.lastrowid

        # D1：接线"调参结果 → 进化追踪"。仅记录真正完成的双AI决策
        # （consensus / both_hold / no_consensus）；缺 key / 禁用等 blocked
        # 情况走 _save_failure_row，不污染 evolution_tracking 的失败率统计。
        try:
            import self_evolution as _SE
            _SE.ensure_schema(conn)
            _SE.init_params(conn)  # 幂等：仅首次写入默认参数版本
            _track_status = ("consensus" if consensus
                             else ("both_hold" if both_ok and not merged_proposals else "no_consensus"))
            _SE.track_run(
                conn, run_id,
                trigger=str(trigger)[:80], mode=mode,
                status=_track_status,
                market_regime=profile.get("regime"),
                applied=False, applied_count=len(merged_proposals or []),
                mimo_latency_ms=mimo_r.get("latency_ms"),
                deepseek_latency_ms=ds_r.get("latency_ms"),
                total_latency_ms=total_latency,
                mimo_confidence=mimo_r.get("confidence"),
                deepseek_confidence=ds_r.get("confidence"),
            )
        except Exception:
            pass  # 追踪失败绝不阻塞调参主流程

    return {
        "id": run_id,
        "version": DUAL_AI_VERSION,
        "status": "consensus" if consensus else ("both_hold" if both_ok and not merged_proposals else "no_consensus"),
        "consensus": consensus,
        "consensus_reason": consensus_reason,
        "merged_proposals": merged_proposals,
        "mimo": {
            "status": mimo_r.get("status"),
            "model": mimo_config["model"],
            "decision": mimo_r.get("decision") if both_ok else None,
            "confidence": mimo_r.get("confidence") if both_ok else None,
            "market_regime": mimo_r.get("market_regime") if both_ok else None,
            "summary": mimo_r.get("summary") if both_ok else None,
            "proposals_count": len(mimo_r.get("proposals") or []) if both_ok else 0,
            "latency_ms": mimo_r.get("latency_ms"),
            "error": mimo_r.get("error"),
        },
        "deepseek": {
            "status": ds_r.get("status"),
            "model": ds_config["model"],
            "decision": ds_r.get("decision") if both_ok else None,
            "confidence": ds_r.get("confidence") if both_ok else None,
            "market_regime": ds_r.get("market_regime") if both_ok else None,
            "summary": ds_r.get("summary") if both_ok else None,
            "proposals_count": len(ds_r.get("proposals") or []) if both_ok else 0,
            "latency_ms": ds_r.get("latency_ms"),
            "error": ds_r.get("error"),
        },
        "total_latency_ms": total_latency,
        "evidence_hash": evidence_hash,
    }


def _save_failure_row(connect_factory, trigger, mode, profile, evidence, evidence_hash, status, reason, started_at, started):
    """保存失败记录。"""
    finished_at = _now()
    total_latency = round((time.monotonic() - started) * 1000)
    with connect_factory() as conn:
        ensure_schema(conn)
        cursor = conn.execute(
            """INSERT INTO dual_ai_tuning_runs(
                trigger, mode, status, profile_date, market_regime,
                mimo_status, mimo_model, mimo_response, mimo_proposals, mimo_latency_ms, mimo_error,
                deepseek_status, deepseek_model, deepseek_response, deepseek_proposals, deepseek_latency_ms, deepseek_error,
                consensus_result, consensus_reason, merged_proposals, applied_ids,
                evidence_hash, evidence, total_latency_ms, created_at, finished_at
            ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (
                str(trigger)[:80], mode, status,
                profile.get("profile_date"), profile.get("regime"),
                None, None, None, None, None, None,
                None, None, None, None, None, None,
                "blocked", reason[:500], None, None,
                evidence_hash, _json(evidence), total_latency, started_at, finished_at
            )
        )
        run_id = cursor.lastrowid
    return {
        "id": run_id, "version": DUAL_AI_VERSION, "status": status,
        "consensus": False, "consensus_reason": reason,
        "merged_proposals": [],
        "mimo": {"status": "not_run"}, "deepseek": {"status": "not_run"},
        "total_latency_ms": total_latency, "evidence_hash": evidence_hash,
    }


def recent_runs(conn, limit=20):
    """读取最近的双AI调参记录。"""
    rows = conn.execute(
        """SELECT id, trigger, mode, status, profile_date, market_regime,
                  mimo_status, mimo_model, mimo_latency_ms, mimo_error,
                  deepseek_status, deepseek_model, deepseek_latency_ms, deepseek_error,
                  consensus_result, consensus_reason, merged_proposals,
                  total_latency_ms, created_at, finished_at
           FROM dual_ai_tuning_runs ORDER BY id DESC LIMIT ?""",
        (limit,)
    ).fetchall()
    result = []
    for row in rows:
        result.append({
            "id": row[0], "trigger": row[1], "mode": row[2], "status": row[3],
            "profile_date": row[4], "market_regime": row[5],
            "mimo": {"status": row[6], "model": row[7], "latency_ms": row[8], "error": row[9]},
            "deepseek": {"status": row[10], "model": row[11], "latency_ms": row[12], "error": row[13]},
            "consensus_result": row[14], "consensus_reason": row[15],
            "merged_proposals": _loads(row[16], []),
            "total_latency_ms": row[17], "created_at": row[18], "finished_at": row[19],
        })
    return result


def dual_ai_status(conn):
    """返回双AI系统的整体状态。"""
    keys = get_api_keys(conn)
    mimo_ok = keys.get("mimo", {}).get("configured", False) and keys.get("mimo", {}).get("enabled", True)
    ds_ok = keys.get("deepseek", {}).get("configured", False) and keys.get("deepseek", {}).get("enabled", True)

    recent = recent_runs(conn, 5)
    last_consensus = next((r for r in recent if r.get("consensus_result") == "consensus"), None)

    return {
        "version": DUAL_AI_VERSION,
        "providers": keys,
        "mimo_ready": mimo_ok,
        "deepseek_ready": ds_ok,
        "dual_ready": mimo_ok and ds_ok,
        "recent_runs": recent,
        "last_consensus": last_consensus,
        "consensus_rules": {
            "weight_direction_threshold": CONSENSUS_WEIGHT_DIRECTION_THRESHOLD,
            "weight_magnitude_ratio": CONSENSUS_WEIGHT_MAGNITUDE_RATIO,
            "delta_direction_threshold": CONSENSUS_DELTA_DIRECTION_THRESHOLD,
            "condition_magnitude_ratio": CONSENSUS_CONDITION_MAGNITUDE_RATIO,
            "rule": "两个AI必须同时propose且方向一致、幅度接近，才合并为最终提案",
        },
    }

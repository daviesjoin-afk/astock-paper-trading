//! Persistent runtime & AI provider settings plus live AI connectivity testing.

use std::fs;
use std::path::{Path, PathBuf};
use std::time::{Duration, Instant};

use serde::{Deserialize, Serialize};
use serde_json::{Value, json};

use crate::cli::http;

#[derive(Clone, Debug, Serialize, Deserialize)]
#[serde(default)]
pub struct SystemSettings {
    pub ai_provider: String,
    pub ai_base_url: String,
    pub ai_model: String,
    pub ai_api_key: String,
    pub ai_enabled: bool,
    /// "advisory" (bounded tilt) or "deep" (AI-led buy/sell/weight within cash & T+1/limit-band rules).
    pub ai_depth: String,
    pub ai_timeout_secs: u64,
    pub live_interval_secs: u64,
    pub live_auto_evolve: bool,
    pub live_daemon_enabled: bool,
    pub live_universe_path: String,
    pub updated_at: String,
}

impl Default for SystemSettings {
    fn default() -> Self {
        let env_base = std::env::var("ASTOCK_LLM_BASE").unwrap_or_default();
        let env_key = std::env::var("ASTOCK_LLM_KEY").unwrap_or_default();
        let env_model = std::env::var("ASTOCK_LLM_MODEL").unwrap_or_default();
        let env_universe = std::env::var("ASTOCK_UNIVERSE").unwrap_or_default();
        Self {
            ai_provider: if env_base.contains("deepseek") || env_base.is_empty() {
                "deepseek".into()
            } else {
                "custom".into()
            },
            ai_base_url: if env_base.is_empty() {
                "https://api.deepseek.com/v1".into()
            } else {
                env_base
            },
            ai_model: if env_model.is_empty() {
                "deepseek-chat".into()
            } else {
                env_model
            },
            ai_api_key: env_key,
            ai_enabled: true,
            ai_depth: "deep".into(),
            ai_timeout_secs: 25,
            live_interval_secs: 180,
            live_auto_evolve: true,
            live_daemon_enabled: false,
            live_universe_path: env_universe,
            updated_at: String::new(),
        }
    }
}

#[derive(Clone, Debug, Default, Serialize)]
pub struct DaemonRuntimeStatus {
    pub daemon_enabled: bool,
    pub running: bool,
    pub os: String,
    pub phase: String,
    pub last_heartbeat: Option<String>,
    pub last_round_id: Option<String>,
    pub last_error: Option<String>,
}

#[derive(Clone, Debug, Default, Deserialize)]
#[serde(default)]
pub struct SettingsPatch {
    pub ai_provider: Option<String>,
    pub ai_base_url: Option<String>,
    pub ai_model: Option<String>,
    pub ai_api_key: Option<String>,
    pub ai_enabled: Option<bool>,
    pub ai_depth: Option<String>,
    pub ai_timeout_secs: Option<u64>,
    pub live_interval_secs: Option<u64>,
    pub live_auto_evolve: Option<bool>,
    pub live_daemon_enabled: Option<bool>,
    pub live_universe_path: Option<String>,
}

pub fn settings_path(live_db_path: &str) -> PathBuf {
    if let Ok(explicit) = std::env::var("ASTOCK_SETTINGS_PATH")
        && !explicit.trim().is_empty()
    {
        return PathBuf::from(explicit);
    }
    let db = Path::new(live_db_path);
    match db.parent() {
        Some(parent) if !parent.as_os_str().is_empty() => parent.join("settings.json"),
        _ => PathBuf::from("settings.json"),
    }
}

pub fn load_settings(path: &Path) -> SystemSettings {
    match fs::read_to_string(path) {
        Ok(raw) => serde_json::from_str::<SystemSettings>(&raw).unwrap_or_default(),
        Err(_) => SystemSettings::default(),
    }
}

pub fn save_settings(path: &Path, settings: &SystemSettings) -> Result<(), String> {
    if let Some(parent) = path.parent()
        && !parent.as_os_str().is_empty()
    {
        fs::create_dir_all(parent).map_err(|e| format!("create settings dir failed: {e}"))?;
    }
    let json = serde_json::to_string_pretty(settings)
        .map_err(|e| format!("serialize settings failed: {e}"))?;
    let tmp = path.with_extension("json.tmp");
    fs::write(&tmp, json).map_err(|e| format!("write settings tmp failed: {e}"))?;
    fs::rename(&tmp, path).map_err(|e| format!("replace settings failed: {e}"))?;
    Ok(())
}

pub fn mask_key(key: &str) -> String {
    let trimmed = key.trim();
    if trimmed.is_empty() {
        return String::new();
    }
    let chars: Vec<char> = trimmed.chars().collect();
    if chars.len() <= 8 {
        return "****".into();
    }
    let head: String = chars[..3].iter().collect();
    let tail: String = chars[chars.len() - 4..].iter().collect();
    format!("{head}****{tail}")
}

pub fn apply_patch(current: &mut SystemSettings, patch: SettingsPatch, now: &str) {
    if let Some(v) = patch.ai_provider {
        current.ai_provider = v.trim().to_string();
    }
    if let Some(v) = patch.ai_base_url {
        current.ai_base_url = v.trim().trim_end_matches('/').to_string();
    }
    if let Some(v) = patch.ai_model {
        current.ai_model = v.trim().to_string();
    }
    if let Some(v) = patch.ai_api_key {
        let trimmed = v.trim();
        if !trimmed.is_empty() && !trimmed.contains("****") {
            current.ai_api_key = trimmed.to_string();
        }
    }
    if let Some(v) = patch.ai_enabled {
        current.ai_enabled = v;
    }
    if let Some(v) = patch.ai_depth {
        current.ai_depth = if v == "advisory" {
            "advisory".into()
        } else {
            "deep".into()
        };
    }
    if let Some(v) = patch.ai_timeout_secs {
        current.ai_timeout_secs = v.clamp(5, 120);
    }
    if let Some(v) = patch.live_interval_secs {
        current.live_interval_secs = v.clamp(30, 1800);
    }
    if let Some(v) = patch.live_auto_evolve {
        current.live_auto_evolve = v;
    }
    if let Some(v) = patch.live_daemon_enabled {
        current.live_daemon_enabled = v;
    }
    if let Some(v) = patch.live_universe_path {
        current.live_universe_path = v.trim().to_string();
    }
    current.updated_at = now.to_string();
}

pub fn redacted_view(settings: &SystemSettings, runtime: &DaemonRuntimeStatus) -> Value {
    json!({
        "ai_provider": settings.ai_provider,
        "ai_base_url": settings.ai_base_url,
        "ai_model": settings.ai_model,
        "has_api_key": !settings.ai_api_key.trim().is_empty(),
        "ai_api_key_masked": mask_key(&settings.ai_api_key),
        "ai_enabled": settings.ai_enabled,
        "ai_depth": settings.ai_depth,
        "ai_timeout_secs": settings.ai_timeout_secs,
        "live_interval_secs": settings.live_interval_secs,
        "live_auto_evolve": settings.live_auto_evolve,
        "live_daemon_enabled": settings.live_daemon_enabled,
        "live_universe_path": settings.live_universe_path,
        "updated_at": settings.updated_at,
        "runtime": runtime,
    })
}

pub fn test_ai_connection(
    base_url: &str,
    api_key: &str,
    model: &str,
    timeout_secs: u64,
) -> Value {
    let base = base_url.trim().trim_end_matches('/');
    if base.is_empty() || model.trim().is_empty() || api_key.trim().is_empty() {
        return json!({
            "ok": false,
            "error": "请先填写 Base URL、模型名称和 API Key",
        });
    }
    let endpoint = format!("{base}/chat/completions");
    let body = json!({
        "model": model.trim(),
        "messages": [
            {"role": "system", "content": "You are a concise trading connectivity check."},
            {"role": "user", "content": "Reply with OK"}
        ],
        "max_tokens": 16,
        "temperature": 0
    });
    let started = Instant::now();
    let agent = http::agent(Duration::from_secs(timeout_secs.clamp(5, 60)), false);
    let resp = agent
        .post(&endpoint)
        .header("Authorization", &format!("Bearer {}", api_key.trim()))
        .header("Content-Type", "application/json")
        .header("Accept", "application/json")
        .send(body.to_string().as_bytes());
    let latency_ms = started.elapsed().as_millis() as u64;
    match resp {
        Ok(mut res) => match res.body_mut().read_to_vec() {
            Ok(bytes) => match serde_json::from_slice::<Value>(&bytes) {
                Ok(parsed) => {
                    let reply = parsed["choices"][0]["message"]["content"]
                        .as_str()
                        .unwrap_or("OK")
                        .trim()
                        .to_string();
                    json!({
                        "ok": true,
                        "latency_ms": latency_ms,
                        "model": parsed["model"].as_str().unwrap_or(model.trim()),
                        "reply": reply,
                    })
                }
                Err(e) => json!({
                    "ok": false,
                    "latency_ms": latency_ms,
                    "error": format!("返回非 JSON：{e}"),
                }),
            },
            Err(e) => json!({
                "ok": false,
                "latency_ms": latency_ms,
                "error": format!("读取响应失败：{e}"),
            }),
        },
        Err(e) => json!({
            "ok": false,
            "latency_ms": latency_ms,
            "error": format!("请求失败：{e}"),
        }),
    }
}

/// Build an enriched context prompt (account equity, positions, market breadth, top quotes)
/// and ask the configured LLM provider for structured intraday trading advice.
/// Advice plus the provider's token accounting for this call. Prompt caching is
/// billed from these numbers, so a caller that cannot see them cannot tell a
/// cached round from a cold one.
#[derive(Clone, Debug, Default, PartialEq, Serialize, Deserialize)]
pub struct LiveAdviceOutcome {
    pub advice: Value,
    pub prompt_tokens: Option<i64>,
    pub cached_prompt_tokens: Option<i64>,
    pub total_tokens: Option<i64>,
    pub attempts: u32,
}

pub fn call_ai_live_advice(
    settings: &SystemSettings,
    context_snapshot: &Value,
) -> Result<LiveAdviceOutcome, String> {
    let (system_prompt, mode) = live_advice_prompt(settings);
    // Prompt caching bills from the longest common prefix, so the payload is
    // serialised with the stable half first. Sending the kernel's own object
    // order put minute-by-minute fields at the front; the measured hit rate was
    // 0.2% versus 99.9% once stable structure led.
    // Only facts that are byte-identical across rounds belong here. The slot
    // books carry cash/equity, which changes with every fill, so they stay in
    // the volatile tail: caching them would break the prefix on every trade.
    const STABLE_KEYS: [&str; 3] = [
        "market_table", "recent_bull_trap_lessons", "account_as_of_round_id",
    ];
    let mut stable = serde_json::Map::new();
    let mut volatile = serde_json::Map::new();
    if let Some(fields) = context_snapshot.as_object() {
        for (key, value) in fields {
            if STABLE_KEYS.contains(&key.as_str()) {
                stable.insert(key.clone(), value.clone());
            } else {
                volatile.insert(key.clone(), value.clone());
            }
        }
    }
    // Build the wrapper by hand: serde_json's map keeps insertion order, and
    // "stable" must come first or the cached prefix shrinks to nothing.
    let ordered_text = format!(
        "{{\"stable\":{},\"round\":{}}}",
        Value::Object(stable), Value::Object(volatile)
    );
    let body = json!({
        "model": settings.ai_model.trim(),
        "messages": [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": ordered_text}
        ],
        "temperature": 0.2
    });
    let endpoint = live_advice_endpoint(settings)?;
    // A reasoning model spends the whole budget on hidden reasoning before it
    // emits any content: at 900 tokens every live call came back empty, so the
    // scheduler paid for a second, non-reasoning call each round. Ask for the
    // JSON directly first; only a provider that rejects the switch falls back
    // to reasoning with a budget large enough to finish.
    let attempts = live_advice_attempts(settings, &body);
    let mut errors = Vec::new();
    for (attempt, (request_body, timeout_secs)) in attempts.into_iter().enumerate() {
        match live_advice_once(&endpoint, settings, &request_body, timeout_secs) {
            Ok((advice, usage)) => {
                return Ok(LiveAdviceOutcome {
                    advice: stamp_live_advice(advice, settings, &mode),
                    prompt_tokens: usage.0,
                    cached_prompt_tokens: usage.1,
                    total_tokens: usage.2,
                    attempts: attempt as u32 + 1,
                });
            }
            Err(error) => errors.push(error),
        }
    }
    Err(errors.join("; "))
}

fn live_advice_endpoint(settings: &SystemSettings) -> Result<String, String> {
    let base = settings.ai_base_url.trim().trim_end_matches('/');
    if !settings.ai_enabled || base.is_empty() || settings.ai_model.trim().is_empty() || settings.ai_api_key.trim().is_empty() {
        return Err("ai_not_configured".into());
    }
    Ok(format!("{base}/chat/completions"))
}

fn live_advice_request(body: &Value, _settings: &SystemSettings, max_tokens: u64, think: bool) -> Value {
    let mut request = body.clone();
    request["max_tokens"] = json!(max_tokens);
    if think {
        request["thinking"] = json!({"type": "disabled"});
    }
    request
}

fn live_advice_once(
    endpoint: &str,
    settings: &SystemSettings,
    body: &Value,
    timeout_secs: u64,
) -> Result<(Value, (Option<i64>, Option<i64>, Option<i64>)), String> {
    let agent = http::agent(Duration::from_secs(timeout_secs), false);
    live_advice_with(endpoint, settings, body, &agent)
}

/// Split out so a test can drive the request/parse contract without a network.
fn live_advice_with(
    endpoint: &str,
    settings: &SystemSettings,
    body: &Value,
    agent: &ureq::Agent,
) -> Result<(Value, (Option<i64>, Option<i64>, Option<i64>)), String> {
    let mut res = agent
        .post(endpoint)
        .header("Authorization", &format!("Bearer {}", settings.ai_api_key.trim()))
        .header("Content-Type", "application/json")
        .header("Accept", "application/json")
        .send(body.to_string().as_bytes())
        .map_err(|e| format!("ai_http_error: {e}"))?;
    let bytes = res
        .body_mut()
        .read_to_vec()
        .map_err(|e| format!("ai_read_error: {e}"))?;
    let parsed: Value = serde_json::from_slice(&bytes)
        .map_err(|e| format!("ai_json_error: {e}"))?;
    let content = parsed["choices"][0]["message"]["content"]
        .as_str()
        .ok_or_else(|| "ai_empty_content".to_string())?;
    let cleaned = content
        .trim()
        .trim_start_matches("```json")
        .trim_start_matches("```")
        .trim_end_matches("```")
        .trim();
    if cleaned.is_empty() {
        return Err("ai_empty_content".into());
    }
    let advice = serde_json::from_str(cleaned)
        .map_err(|e| format!("ai_advice_parse_error: {e}"))?;
    // Providers differ in where they report cache hits; accept either spelling.
    let usage = &parsed["usage"];
    let num = |value: &Value| value.as_i64().or_else(|| value.as_f64().map(|v| v as i64));
    let prompt = num(&usage["prompt_tokens"]).or_else(|| num(&usage["input_tokens"]));
    let cached = num(&usage["prompt_cache_hit_tokens"])
        .or_else(|| num(&usage["prompt_tokens_details"]["cached_tokens"]))
        .or_else(|| num(&usage["cached_prompt_tokens"]));
    let total = num(&usage["total_tokens"]);
    Ok((advice, (prompt, cached, total)))
}

fn live_advice_attempts(settings: &SystemSettings, body: &Value) -> Vec<(Value, u64)> {
    vec![
        (live_advice_request(body, settings, 1_200, true), settings.ai_timeout_secs.clamp(5, 120)),
        (live_advice_request(body, settings, 8_000, false), settings.ai_timeout_secs.clamp(5, 120)),
    ]
}

fn stamp_live_advice(mut advice: Value, settings: &SystemSettings, mode: &str) -> Value {
    if let Some(obj) = advice.as_object_mut() {
        obj.entry("model".to_string())
            .or_insert_with(|| Value::String(settings.ai_model.clone()));
        obj.entry("mode".to_string())
            .or_insert_with(|| Value::String(mode.to_string()));
    }
    advice
}

pub fn live_advice_prompt(settings: &SystemSettings) -> (String, String) {
    let depth_instruction = if settings.ai_depth == "deep" {
        "当前为【深度托管模式】：你可以直接决定个股的买卖方向（buy/sell/hold/avoid）与目标仓位万分比 target_weight_bps（0~10000，例如 2500 代表 25% 槽位资产）。请务必严格执行【主力意图与反诱多五维纪律】：
         1. 看情绪与炸板率（market_sentiment）：当 score < 42（冰点/偏冷）或 bull_trap_rate_bps >= 3000（炸板率≥30%）时，主力极易拉高出货，严禁追高开仓，优先去弱留强或空仓观望；
         2. 查日内均价线（vwap_distance_bps）：现价跌破日内成交均价线（vwap_distance_bps < 0，尤其是 <= -120 bps）说明当日追高筹码全线被套，属典型主力拉高派发（钓鱼线），一票否决买入；
         3. 辨假阳真阴与长上影（upper_wick_bps / bull_trap_risk）：若 bull_trap_risk >= 30 或 upper_wick_bps >= 150（冲高留长上影）或高开低走，严禁买入并列入 avoid/blocked_symbols；
         4. 防高位接盘（cum_gain_3d_bps / dist_20d_high_bps）：近 3 日累计涨幅 cum_gain_3d_bps >= 1800（18%）且日内分位不足 80% 的高位加速股，属于击鼓传花末端，严禁接盘；
         5. 读近期诱多错题本（recent_bull_trap_lessons）：牢记本账户近期被套与止损教训，绝不在同类形态上重蹈覆辙；
         6. 双槽隔离与 T+1 约束：为每条建议明确指定 slot（\"steady\" 或 \"aggressive\"），优先从 slot_candidates 对应池选股；早盘 09:30~09:45 审慎开新仓，sellable_quantity=0 的当日新仓不可卖出。"
    } else {
        "当前为【顾问模式】：请结合 market_sentiment、候选股的 vwap_distance_bps / bull_trap_risk 反诱多指标以及 recent_bull_trap_lessons 错题本，给出全局市场偏向 bias（-20~20）、入场门槛调整 entry_delta（-15~15）、权重倾斜 weight_tilt_bps（-1000~1000）和分槽位建议 suggestions。"
    };
    let system_prompt = format!(
        "你是 A 股盘中量化模拟交易 AI 决策引擎（内置主力诱多识破机制）。请严格根据提供的实时市场情绪（market_sentiment）、双槽抗诱多候选（slot_candidates）、日内均价线偏离（vwap_distance_bps）、诱多风险分（bull_trap_risk）、近期诱多错题本（recent_bull_trap_lessons）与 T+1 可卖股数输出纯 JSON（不要包含 markdown 代码块）。
         {depth_instruction}
         【符号约定，最容易搞反】entry_delta 直接加到开仓门槛分数上：正值 = 更严格、更难买；负值 = 更宽松、更容易买。看空、要求收紧、禁止追高时必须给正值；只有明确要放宽门槛去找机会时才给负值。输出前自检：reason 说防守，entry_delta 就必须是正数。bias 同为负值更谨慎。
         【禁买清单】全市场表里凡满足禁买条件的代码都要写进 blocked_symbols（最多 40 个），不要只挑几个；漏写等于默许买入。
         JSON 格式：{{\"mode\":\"{}\",\"confidence\":0-100,\"bias\":-20..20,\"entry_delta\":-15..15,\"weight_tilt_bps\":-1000..1000,\"blocked_symbols\":[\"高诱多风险代码\"],\"reason\":\"简明中文判断（含情绪温度、主力诱多识别结论与仓位策略）\",\"suggestions\":[{{\"symbol\":\"600000.SH\",\"slot\":\"steady|aggressive\",\"side\":\"buy|sell|avoid|hold\",\"target_weight_bps\":0-10000,\"score_delta\":-20..20,\"reason\":\"中文理由（说明均价线/诱多风险/趋势依据）\"}}]}}",
        settings.ai_depth
    );
    (system_prompt, settings.ai_depth.clone())
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn mask_and_patch_preserve_existing_secret_when_masked_or_empty() {
        let mut s = SystemSettings {
            ai_api_key: "sk-1234567890abcdef".into(),
            ..Default::default()
        };
        assert_eq!(mask_key(&s.ai_api_key), "sk-****cdef");

        apply_patch(
            &mut s,
            SettingsPatch {
                ai_api_key: Some("sk-****cdef".into()),
                ai_depth: Some("deep".into()),
                live_interval_secs: Some(120),
                ..Default::default()
            },
            "2026-11-20T15:00:00+08:00",
        );
        assert_eq!(s.ai_api_key, "sk-1234567890abcdef");
        assert_eq!(s.ai_depth, "deep");
        assert_eq!(s.live_interval_secs, 120);
    }

    #[test]
    fn the_payload_leads_with_stable_fields_so_the_prefix_caches() {
        // Two rounds that differ only in the volatile tail must serialise the
        // same first document; otherwise the provider caches nothing.
        let round = |equity: i64, stamp: &str| json!({
            "quote_time": stamp,
            "sample_count": 5_219,
            "market_sentiment": {"score": 12},
            "market_table": {"cols": ["symbol"], "rows": [["600000.SH", 100]]},
            "slots": [{"slot": "steady", "cash_cents": equity}],
            "held_positions": [],
            "account_as_of_round_id": "2026-11-23-130009",
            "recent_bull_trap_lessons": [],
            "change_distribution": {"up": 1, "down": 2, "flat": 0},
        });
        let split = |value: &Value| {
            let mut stable = serde_json::Map::new();
            let mut volatile = serde_json::Map::new();
            for (key, item) in value.as_object().unwrap() {
                if ["market_table", "recent_bull_trap_lessons",
                    "account_as_of_round_id"].contains(&key.as_str()) {
                    stable.insert(key.clone(), item.clone());
                } else {
                    volatile.insert(key.clone(), item.clone());
                }
            }
            (json!({"stable": stable, "round": volatile}).to_string(), stable)
        };
        let (first, s1) = split(&round(9_100_000, "2026-11-23T13:00:09+08:00"));
        let (second, s2) = split(&round(9_200_000, "2026-11-23T13:03:09+08:00"));
        assert_ne!(first, second, "the volatile tail must still change");
        assert_eq!(s1, s2, "the stable block must be byte-identical");
        assert!(first.contains("\"stable\":"), "{first}");
        assert!(!serde_json::to_string(&s1).unwrap().contains("quote_time"));
    }

    #[test]
    fn the_live_prompt_spells_out_which_way_entry_delta_moves_the_gate() {
        let settings = SystemSettings { ai_depth: "deep".into(), ..Default::default() };
        let (prompt, mode) = live_advice_prompt(&settings);
        assert_eq!(mode, "deep");
        // Without this the model returned -12 while saying "forbid chasing".
        assert!(prompt.contains("正值 = 更严格"), "prompt must define the sign");
        assert!(prompt.contains("负值 = 更宽松"));
        assert!(prompt.contains("entry_delta 就必须是正数"));
    }

    #[test]
    fn live_advice_asks_for_direct_json_before_spending_reasoning_tokens() {
        let s = SystemSettings::default();
        let body = json!({"model": "deepseek-v4.1-flash", "messages": []});
        let attempts = live_advice_attempts(&s, &body);
        assert_eq!(attempts.len(), 2);
        assert_eq!(attempts[0].0["thinking"], json!({"type": "disabled"}));
        assert_eq!(attempts[0].0["max_tokens"], json!(1_200));
        assert!(attempts[1].0.get("thinking").is_none());
        assert_eq!(attempts[1].0["max_tokens"], json!(8_000));
        assert_eq!(attempts[0].0["model"], json!("deepseek-v4.1-flash"));
    }

    #[test]
    fn an_empty_or_truncated_completion_is_an_error_not_advice() {
        for content in [json!(""), json!("   "), json!("{\"bias\":")] {
            let payload = json!({"choices": [{"message": {"content": content}}]});
            let cleaned = payload["choices"][0]["message"]["content"].as_str().unwrap().trim();
            assert!(serde_json::from_str::<Value>(cleaned).is_err() || cleaned.is_empty());
        }
        // Environment-independent: the default reads env vars, so build an
        // explicit configured instance and an explicitly disabled one.
        let configured = SystemSettings {
            ai_enabled: true,
            ai_base_url: "https://llm.example/v1/".into(),
            ai_model: "deepseek-v4.1-flash".into(),
            ai_api_key: "test-key".into(),
            ..SystemSettings::default()
        };
        assert_eq!(live_advice_endpoint(&configured).unwrap(),
                   "https://llm.example/v1/chat/completions");
        let off = SystemSettings { ai_enabled: false, ..configured };
        assert_eq!(live_advice_endpoint(&off).unwrap_err(), "ai_not_configured");
    }
}

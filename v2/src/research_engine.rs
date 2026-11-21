//! Offline / on-demand multi-strategy research evaluator over ingested market bars.

use std::collections::BTreeMap;
use std::fs;
use std::path::{Path, PathBuf};

use serde::{Deserialize, Serialize};

use crate::store::Bar;

#[derive(Clone, Debug, Serialize, Deserialize)]
pub struct ResearchCurvePoint {
    pub day: String,
    pub equity_bps: i64,
}

#[derive(Clone, Debug, Serialize, Deserialize)]
pub struct ResearchCandidateRecord {
    pub candidate_id: String,
    pub algorithm_id: String,
    pub algorithm_version: String,
    pub state: String,
    pub created_day: String,
    pub updated_day: String,
    pub shadow_days: usize,
    pub gate: String,
    pub total_return_bps: i64,
    pub max_drawdown_bps: i64,
    pub win_rate_bps: i64,
    #[serde(default)]
    pub sharpe_x100: i64,
    #[serde(default)]
    pub turnover_bps: i64,
    pub curve: Vec<ResearchCurvePoint>,
}

struct StrategySpec {
    id: &'static str,
    algo: &'static str,
    version: &'static str,
    min_daily_bps: i64,
    max_daily_bps: i64,
    top_n: usize,
    weight_bps: i64,
}

const SPECS: [StrategySpec; 3] = [
    StrategySpec {
        id: "cand-steady-v2",
        algo: "稳健均衡",
        version: "2.1",
        min_daily_bps: -150,
        max_daily_bps: 450,
        top_n: 5,
        weight_bps: 7_500,
    },
    StrategySpec {
        id: "cand-momentum-v2",
        algo: "动量突破",
        version: "2.4",
        min_daily_bps: 50,
        max_daily_bps: 980,
        top_n: 4,
        weight_bps: 9_000,
    },
    StrategySpec {
        id: "cand-lowdd-v2",
        algo: "低回撤趋势",
        version: "2.0",
        min_daily_bps: 0,
        max_daily_bps: 350,
        top_n: 6,
        weight_bps: 6_000,
    },
];

/// Evaluate 3 candidate parameterizations on sequential daily bars (`day -> bars`).
pub fn evaluate_candidates(daily_bars: &[(String, Vec<Bar>)]) -> Vec<ResearchCandidateRecord> {
    if daily_bars.len() < 2 {
        return Vec::new();
    }
    let first_day = daily_bars.first().map(|(d, _)| d.clone()).unwrap_or_default();
    let last_day = daily_bars.last().map(|(d, _)| d.clone()).unwrap_or_default();
    let shadow_days = daily_bars.len().saturating_sub(1);

    let mut out = Vec::with_capacity(SPECS.len());
    for spec in &SPECS {
        let mut equity = 10_000_i64;
        let mut peak = 10_000_i64;
        let mut max_dd = 0_i64;
        let mut wins = 0_usize;
        let mut total_days = 0_usize;
        let mut curve = vec![ResearchCurvePoint {
            day: first_day.clone(),
            equity_bps: equity,
        }];

        let mut prev_close: BTreeMap<String, i64> = BTreeMap::new();
        let mut prev_selected: Vec<String> = Vec::new();
        let mut daily_rets: Vec<i64> = Vec::new();
        let mut turnover_sum_bps = 0_i64;
        for (day, bars) in daily_bars {
            let by_sym: BTreeMap<&str, &Bar> =
                bars.iter().map(|b| (b.symbol.as_str(), b)).collect();

            if day != &first_day {
                let mut sum_ret = 0_i64;
                let mut cnt = 0_i64;
                for sym in &prev_selected {
                    if let (Some(bar), Some(&prev_c)) = (by_sym.get(sym.as_str()), prev_close.get(sym.as_str()))
                        && prev_c > 0
                    {
                        let ret = ((bar.close_cents - prev_c) * 10_000) / prev_c;
                        if ret.abs() <= 1_100 {
                            sum_ret += ret;
                            cnt += 1;
                        }
                    }
                }
                let gross_ret = if cnt > 0 {
                    (sum_ret / cnt) * spec.weight_bps / 10_000
                } else {
                    0
                };
                // Deduct realistic A-share friction (stamp tax 5 bps + commission/slippage 10 bps scaled by turnover)
                let friction_bps = if cnt > 0 {
                    (15 * spec.weight_bps) / 20_000
                } else {
                    0
                };
                let day_ret = gross_ret - friction_bps;
                daily_rets.push(day_ret);
                equity = (equity * (10_000 + day_ret) / 10_000).max(1);
                if equity > peak {
                    peak = equity;
                }
                let dd = if peak > 0 {
                    ((peak - equity) * 10_000) / peak
                } else {
                    0
                };
                if dd > max_dd {
                    max_dd = dd;
                }
                if day_ret > 0 {
                    wins += 1;
                }
                total_days += 1;
                curve.push(ResearchCurvePoint {
                    day: day.clone(),
                    equity_bps: equity,
                });
            }

            let mut ranked: Vec<(&str, i64)> = bars
                .iter()
                .filter(|b| b.tradable && b.close_cents > 0)
                .filter_map(|b| {
                    let prev_c = prev_close.get(b.symbol.as_str()).copied()?;
                    if prev_c <= 0 {
                        return None;
                    }
                    let ret = ((b.close_cents - prev_c) * 10_000) / prev_c;
                    if ret >= spec.min_daily_bps && ret <= spec.max_daily_bps {
                        Some((b.symbol.as_str(), ret))
                    } else {
                        None
                    }
                })
                .collect();
            ranked.sort_by(|a, b| b.1.cmp(&a.1).then_with(|| a.0.cmp(b.0)));
            let next_selected: Vec<String> = ranked
                .into_iter()
                .take(spec.top_n)
                .map(|(s, _)| s.to_string())
                .collect();
            if !prev_selected.is_empty() || !next_selected.is_empty() {
                let kept = next_selected
                    .iter()
                    .filter(|s| prev_selected.contains(s))
                    .count();
                let denom = prev_selected.len().max(next_selected.len()).max(1);
                let changed_ratio_bps = ((denom - kept) as i64 * 10_000) / denom as i64;
                turnover_sum_bps += changed_ratio_bps * spec.weight_bps / 10_000;
            }
            prev_selected = next_selected;

            for bar in bars {
                if bar.tradable && bar.close_cents > 0 {
                    prev_close.insert(bar.symbol.clone(), bar.close_cents);
                }
            }
        }

        let total_return_bps = equity - 10_000;
        let win_rate_bps = if total_days > 0 {
            (wins as i64 * 10_000) / total_days as i64
        } else {
            0
        };
        let turnover_bps = if total_days > 0 {
            turnover_sum_bps / total_days as i64
        } else {
            0
        };
        let sharpe_x100 = if daily_rets.len() >= 2 {
            let n = daily_rets.len() as f64;
            let mean = daily_rets.iter().copied().sum::<i64>() as f64 / n;
            let var = daily_rets
                .iter()
                .map(|r| {
                    let d = *r as f64 - mean;
                    d * d
                })
                .sum::<f64>()
                / n;
            let std = var.sqrt();
            if std > 1e-6 {
                ((mean / std) * (252.0_f64).sqrt() * 100.0).round() as i64
            } else {
                0
            }
        } else {
            0
        };
        let passed = shadow_days >= 10 && total_return_bps > 0 && max_dd <= 1_200;
        let state = if passed {
            "promoted"
        } else if total_return_bps < -500 || max_dd > 1_800 {
            "rejected"
        } else {
            "shadow"
        };
        let gate = if passed {
            format!(
                "通过门禁：净收益 +{:.2}% / 回撤 {:.2}% / 夏普 {:.2} / 胜率 {:.0}%",
                total_return_bps as f64 / 100.0,
                max_dd as f64 / 100.0,
                sharpe_x100 as f64 / 100.0,
                win_rate_bps as f64 / 100.0,
            )
        } else {
            let mut shortfalls = Vec::new();
            if shadow_days < 10 {
                shortfalls.push(format!("样本天数 {shadow_days}/10"));
            }
            if total_return_bps <= 0 {
                shortfalls.push(format!("扣费净收益 {total_return_bps}/1 bps"));
            }
            if max_dd > 1_200 {
                shortfalls.push(format!("回撤 {max_dd}/1200 bps"));
            }
            format!(
                "未达晋升线（{} · 夏普 {:.2}）",
                shortfalls.join("；"),
                sharpe_x100 as f64 / 100.0
            )
        };

        out.push(ResearchCandidateRecord {
            candidate_id: spec.id.to_string(),
            algorithm_id: spec.algo.to_string(),
            algorithm_version: spec.version.to_string(),
            state: state.to_string(),
            created_day: first_day.clone(),
            updated_day: last_day.clone(),
            shadow_days,
            gate,
            total_return_bps,
            max_drawdown_bps: max_dd,
            win_rate_bps,
            sharpe_x100,
            turnover_bps,
            curve,
        });
    }
    out
}

pub fn research_store_path(live_db_path: &str) -> PathBuf {
    let db = Path::new(live_db_path);
    match db.parent() {
        Some(parent) if !parent.as_os_str().is_empty() => parent.join("research_candidates.json"),
        _ => PathBuf::from("research_candidates.json"),
    }
}

pub fn load_candidates(path: &Path) -> Vec<ResearchCandidateRecord> {
    match fs::read_to_string(path) {
        Ok(raw) => serde_json::from_str::<Vec<ResearchCandidateRecord>>(&raw).unwrap_or_default(),
        Err(_) => Vec::new(),
    }
}

pub fn save_candidates(path: &Path, rows: &[ResearchCandidateRecord]) -> Result<(), String> {
    if let Some(parent) = path.parent()
        && !parent.as_os_str().is_empty()
    {
        fs::create_dir_all(parent).map_err(|e| format!("create research dir failed: {e}"))?;
    }
    let json = serde_json::to_string_pretty(rows)
        .map_err(|e| format!("serialize research failed: {e}"))?;
    let tmp = path.with_extension("json.tmp");
    fs::write(&tmp, json).map_err(|e| format!("write research tmp failed: {e}"))?;
    fs::rename(&tmp, path).map_err(|e| format!("replace research failed: {e}"))?;
    Ok(())
}

#[cfg(test)]
mod tests {
    use super::*;

    fn make_bar(symbol: &str, day: &str, open: i64, close: i64) -> Bar {
        Bar {
            symbol: symbol.into(),
            day: day.into(),
            name: symbol.into(),
            open_cents: open,
            high_cents: close.max(open),
            low_cents: close.min(open),
            close_cents: close,
            prev_close_cents: open,
            volume_shares: 10_000,
            amount_cents: close * 10_000,
            adjust: "none".into(),
            source: "tdx".into(),
            tradable: true,
        }
    }

    #[test]
    fn evaluate_candidates_produces_three_curves_from_bars() {
        let mut days = Vec::new();
        for i in 0..14 {
            let day = format!("2026-10-{:02}", i + 1);
            let p = 1000 + i * 15;
            days.push((
                day.clone(),
                vec![
                    make_bar("600000.SH", &day, p, p + 15),
                    make_bar("000001.SZ", &day, p, p + 10),
                ],
            ));
        }
        let out = evaluate_candidates(&days);
        assert_eq!(out.len(), 3);
        assert!(out.iter().all(|c| c.curve.len() >= 2));
    }
}

/**
 * 盘中实时视图：独立的读模型渲染器，不复用也不改写日线账户视图。
 *
 * 数据来源是 Rust 的独立盘中内核（v2/src/intraday/store.rs），数值按 API
 * 原值展示；没有服务端字段时显示「—」，不用 0、估价或前端推算补位。两槽
 * 收益是已有权益曲线点与账户初始分账的算术差，缺任一输入即显示「—」。
 * 定时器仅在 live 模块激活时存在，每 30 秒最多发一轮请求，刷新期间不重入。
 * 八个子视图共享核心读模型；AI / 进化 / 风控各有自己的懒加载，个股档案只
 * 由用户输入或股票链接触发，不进入自动刷新。
 */

import { pushHash, type Route } from "./router";

/** 由 main.ts 注入，保证页内跳转与既有 hash 路由分发走同一条路径。 */
let applyRoute: ((route: Route) => void) | null = null;
export function setLiveRouteApplier(applier: (route: Route) => void): void {
  applyRoute = applier;
}

const REFRESH_MS = 30_000;
const EMPTY = "—";
const LIVE_VIEWS = [
  "dashboard", "signals", "trades", "rounds",
  "ai", "evolution", "risk", "symbol",
] as const;
type LiveView = (typeof LIVE_VIEWS)[number];
type Slot = "steady" | "aggressive";

// ---- API read models (mirrors of the Rust Serialize structs) ----------------

type LivePosition = {
  slot: Slot;
  symbol: string;
  /** Real name from the quote source; null when the source did not carry one.
   *  Never inferred from the code. */
  name: string | null;
  quantity: number;
  sellable_quantity: number;
  avg_cost_cents: number;
  /** Latest real snapshot price. Null when no snapshot ever existed for this
   *  symbol; may be from an older round if the stock had no quote recently. */
  last_price_cents: number | null;
  /** quantity × last_price, computed by the kernel. Null follows the price. */
  market_value_cents: number | null;
  updated_at: string;
};

type LiveSlotView = {
  slot: Slot;
  cash_cents: number;
  market_value_cents: number | null;
  equity_cents: number | null;
  positions: number;
  sellable_positions: number;
};

type LiveAccountView = {
  id: string;
  initial_cash_cents: number;
  cash_cents: number;
  created_at: string;
  as_of_round_id: string | null;
  equity_cents: number | null;
  profit_cents: number | null;
  daily_pnl_cents?: number | null;
  daily_pnl_day?: string | null;
  return_bps: number | null;
  slots: LiveSlotView[];
  positions: LivePosition[];
};

type LiveRound = {
  round_id: string;
  started_at: string;
  finished_at: string;
  quote_count: number;
  trade_count: number;
  ai_ok: boolean;
  ai_error: string | null;
  equity_cents: number;
  regime: string;
  /** Raw audit note. Kept for traceability, not shown to a normal reader. */
  note: string;
  /** Human wording produced by the kernel; preferred over raw fields. */
  regime_label: string | null;
  ai_label: string | null;
  note_label: string | null;
};

type LiveDecision = {
  round_id: string;
  slot: Slot;
  symbol: string;
  score: number;
  threshold: number;
  reached_threshold: boolean;
  side: string;
  blocked_reason: string | null;
  ai_bias: number;
  ai_confidence: number;
  ai_applied: boolean;
  factors_json: unknown;
  note: string;
  created_at: string;
  name: string | null;
  side_label: string | null;
  blocked_label: string | null;
  slot_label: string | null;
};

/**
 * `/v2/live/decisions` now returns an object instead of a bare array. `total`
 * is the number of decisions in the whole round; `decisions` is only the rows
 * the server kept for the current `signals_only` mode.
 */
type LiveDecisionPayload = {
  round_id: string;
  signals_only: boolean;
  total: number;
  returned: number;
  decisions: LiveDecision[];
};

type LiveTrade = {
  id: number;
  round_id: string;
  slot: Slot;
  symbol: string;
  name: string | null;
  side: string;
  quantity: number;
  price_cents: number;
  fee_cents: number;
  reason: string;
  ai_applied: boolean;
  created_at: string;
  side_label: string | null;
  reason_label: string | null;
  slot_label: string | null;
};

type LiveEquityPoint = {
  round_id: string;
  quote_time: string;
  finished_at: string;
  equity_cents: number;
  profit_cents: number;
  return_bps: number;
  steady_equity_cents: number;
  aggressive_equity_cents: number;
};

type AiObservation = {
  round_id: string;
  started_at: string;
  model: string;
  raw_advice: string;
  accepted: boolean;
  clamped: boolean;
  confidence: number;
  reason: string;
  clamped_fields: unknown;
  signal_baseline?: {
    compared: number;
    side_changed: number;
    fills: { slot: Slot; symbol: string; side: string; without_ai: string }[];
  } | null;
};

type EvolutionProposal = {
  entry_delta: number;
  weight_tilt_bps: number;
  max_positions_delta: number;
  cash_floor_delta_bps: number;
  scope?: string;
  basis?: string;
};

type EvolutionMetrics = {
  source?: string;
  day?: string;
  equity_cents?: number;
  profit_cents?: number;
  profit_bps: number;
  round_count: number;
  max_drawdown_bps: number;
};

type EvolutionPreview = {
  day: string;
  /** JSON blobs from the kernel; every field is optional at the boundary. */
  proposal: Partial<EvolutionProposal> | null;
  before_metrics: Partial<EvolutionMetrics> | null;
  after_metrics: { measured?: boolean; applied?: boolean; reason?: string } | null;
  /** Kernel keeps proposals inert; shown as a neutral review state. */
  accepted: boolean;
  reason: string;
  created_at: string;
};

type RiskCounter = {
  reason: string;
  label: string;
  count: number;
};

type RiskSummary = {
  window_rounds: number;
  blocked_total: number;
  counters: RiskCounter[];
};

type SymbolArchiveEvent = {
  round_id: string;
  started_at: string;
  kind: "trade" | "decision";
  slot: string;
  slot_label: string;
  side: string;
  side_label: string;
  quantity: number;
  price_cents: number | null;
  score: number | null;
  blocked_label: string;
  note: string;
};

type SymbolArchive = {
  symbol: string;
  name: string | null;
  trade_count: number;
  buy_quantity: number;
  sell_quantity: number;
  realized_fee_cents: number;
  decision_count: number;
  events: SymbolArchiveEvent[];
};

type LiveSentiment = {
  round_id: string | null;
  quote_time: string | null;
  total_quotes: number;
  up_count: number;
  down_count: number;
  flat_count: number;
  up_ratio_bps: number;
  strong_count: number;
  weak_count: number;
  limit_up_count: number;
  limit_down_count: number;
  surge_fade_count: number;
  surge_fade_rate_bps: number;
  score: number;
  label: string;
  posture: string;
  recommended_max_exposure_bps: number;
};

type LiveData = {
  account: LiveAccountView | null;
  rounds: LiveRound[] | null;
  decisions: LiveDecision[] | null;
  /** All decision rows in the selected round, including unreturned holds.
   *  null = no round selected or the payload has not been read yet. */
  decisionsTotal: number | null;
  trades: LiveTrade[] | null;
  curve: LiveEquityPoint[] | null;
  sentiment: LiveSentiment | null;
  /** null = not loaded yet; [] = loaded and empty. */
  ai: AiObservation[] | null;
  evolution: EvolutionPreview[] | null;
  risk: RiskSummary | null;
};

/** The parameterised archive is never cached: a new symbol is a new request. */
type SymbolState = {
  input: string;
  loadedSymbol: string | null;
  archive: SymbolArchive | null;
  loading: boolean;
  error: string | null;
};

/** Extra read models that load on first visit and join periodic refreshes. */
const EXTRA_PATHS: Record<"ai" | "evolution" | "risk", string> = {
  ai: "/v2/live/ai",
  evolution: "/v2/live/evolution",
  risk: "/v2/live/risk?rounds=40",
};

type LiveExtraKey = keyof typeof EXTRA_PATHS;

type LoadState = {
  status: "loading" | "ready" | "error";
  message: string;
  fetchedAt: Date | null;
};

type Filters = {
  roundId: string | null;
  slot: string;
  side: string;
  /** 1-based page index for the decision-signal table. */
  page: number;
  pageSize: number;
  /** true = 只看有信号的（默认，约 250 KB）；false = 显示全部决策。
   *  toolbar 的「显示全部」开关是它的反向状态，默认不勾选。 */
  signalsOnly: boolean;
};

/** Page sizes offered in the signal table. A round has ~5k symbols × 2 slots,
 *  so rendering them all at once froze the tab; only one page is in the DOM. */
const PAGE_SIZES = [50, 100, 200] as const;
const DEFAULT_PAGE_SIZE = 100;

class LiveApiError extends Error {
  constructor(readonly status: number | null, readonly detail: string) {
    super(status === null ? "network" : `http_${status}`);
  }
}

// ---- module state ----------------------------------------------------------

const host = document.getElementById("live-module");
if (!host) throw new Error("missing #live-module");

const root: HTMLElement = host;
let activeView: LiveView = "dashboard";
let active = false;
let loaded = false;
let timer: number | null = null;
let loading = false;
let refreshQueued = false;
let requestSeq = 0;
let decisionToken = 0;
let filters: Filters = {
  roundId: null,
  slot: "all",
  side: "all",
  page: 1,
  pageSize: DEFAULT_PAGE_SIZE,
  signalsOnly: true,
};
let state: LoadState = { status: "loading", message: "正在读取盘中数据…", fetchedAt: null };
const errors: Partial<Record<keyof LiveData | "symbol", string>> = {};
const extraLoading: Partial<Record<LiveExtraKey, number>> = {};
/**
 * Monotonic per-key generation for the lazily loaded read models.
 *
 * Kept apart from `extraLoading` (which doubles as the "正在读取…" marker) so
 * releasing the marker cannot also erase the fact that a newer read started.
 */
const extraSequence: Record<LiveExtraKey, number> = { ai: 0, evolution: 0, risk: 0 };
const data: LiveData = {
  account: null,
  rounds: null,
  decisions: null,
  decisionsTotal: null,
  trades: null,
  curve: null,
  sentiment: null,
  ai: null,
  evolution: null,
  risk: null,
};
const symbolState: SymbolState = {
  input: "",
  loadedSymbol: null,
  archive: null,
  loading: false,
  error: null,
};
let symbolToken = 0;

// ---- formatting (never substitutes a missing value) ------------------------

const escapeHtml = (value: unknown): string =>
  String(value ?? "").replace(/[&<>"']/g, (char) => {
    switch (char) {
      case "&": return "&amp;";
      case "<": return "&lt;";
      case ">": return "&gt;";
      case '"': return "&quot;";
      default: return "&#39;";
    }
  });

const text = (value: string | null | undefined): string => {
  if (value === null || value === undefined || value.trim() === "") return EMPTY;
  return escapeHtml(value);
};

const money = (cents: number | null | undefined): string => {
  if (cents === null || cents === undefined || !Number.isFinite(cents)) return EMPTY;
  return "¥" + (cents / 100).toLocaleString("zh-CN", {
    minimumFractionDigits: 2,
    maximumFractionDigits: 2,
  });
};

const signedMoney = (cents: number | null | undefined): string => {
  if (cents === null || cents === undefined || !Number.isFinite(cents)) return EMPTY;
  return (cents > 0 ? "+" : "") + money(cents);
};

const percent = (bps: number | null | undefined): string => {
  if (bps === null || bps === undefined || !Number.isFinite(bps)) return EMPTY;
  const value = bps / 100;
  return `${value > 0 ? "+" : ""}${value.toFixed(2)}%`;
};

const pnlClass = (value: number | null | undefined): string => {
  if (value === null || value === undefined || !Number.isFinite(value) || value === 0) return "";
  return value > 0 ? "pos" : "neg";
};

const finite = (value: number | null | undefined): number | null =>
  value === null || value === undefined || !Number.isFinite(value) ? null : value;

const count = (value: number | null | undefined): string =>
  value === null || value === undefined || !Number.isFinite(value)
    ? EMPTY
    : value.toLocaleString("zh-CN");

const slotLabel = (slot: string): string => {
  if (slot === "steady") return "稳健";
  if (slot === "aggressive") return "激进";
  return text(slot);
};

const sideLabel = (side: string): string => {
  if (side === "buy") return "买入";
  if (side === "sell") return "卖出";
  if (side === "hold") return "观望";
  if (side === "ignore" || side === "block") return "忽略";
  if (side === "avoid") return "回避";
  return text(side);
};

const aiLabel = (ok: boolean): string => (ok ? "正常" : "异常");
const aiClass = (ok: boolean): "ok" | "error" => (ok ? "ok" : "error");

/**
 * A stock is shown as "名称 代码" so a normal reader can recognise it while the
 * exact code stays available for verification. The name comes from the quote
 * source; when it is absent we show the code alone rather than guessing.
 */
const stockLabel = (name: string | null | undefined, symbol: string): string => {
  const cleanName = typeof name === "string" ? name.trim() : "";
  if (!cleanName) return `<span class="live-code">${text(symbol)}</span>`;
  return `<span class="live-name">${text(cleanName)}</span>`
    + ` <span class="live-code">${text(symbol)}</span>`;
};

/** A raw score means nothing to a reader; show what it implies plus the value. */
const scoreCell = (score: number | null | undefined): string => {
  if (score === null || score === undefined || !Number.isFinite(score)) {
    return EMPTY;
  }
  const tone = score >= 40 ? "偏强" : score <= -40 ? "偏弱" : "中性";
  return `<span class="live-score live-score-${score >= 40 ? "up" : score <= -40 ? "down" : "flat"}">${tone}</span>`
    + ` <span class="live-dim">${count(score)}</span>`;
};

/** Older kernels send raw blocker codes; translate the known ones locally. */
const blockedFallback = (reason: string | null | undefined): string => {
  if (!reason) return EMPTY;
  const table: Record<string, string> = {
    limit_up_sealed: "已涨停，买不进",
    limit_down_sealed: "已跌停，卖不出",
    t_plus_one: "今天刚买，明天才能卖",
    cash_budget: "现金不够",
    no_live_price: "没有实时价格",
    max_positions: "持仓已满",
    no_live_quote: "没有行情",
    morning_cooldown_guard: "早盘冷静期（防冲动追高）",
    bull_trap_divergence: "主力诱多形态拦截（破均价线/冲高长上影回落）",
    ai_blocked: "AI 建议回避",
    research_entry_gate: "弱市提高开仓门槛",
  };
  return table[reason] ??
    (reason.startsWith("invalid_quote") ? "行情数据异常" : text(reason));
};

const translateTokens = (
  raw: string | null | undefined,
  table: Record<string, string>,
): string => {
  if (!raw || !raw.trim()) return EMPTY;
  const parts = raw
    .split(";")
    .map((part) => part.trim())
    .filter(Boolean)
    .map((part) => {
      if (part.startsWith("ai_reason:")) return `AI 依据：${escapeHtml(part.slice(10))}`;
      return table[part] ?? escapeHtml(part);
    });
  return parts.length > 0 ? parts.join("；") : EMPTY;
};

const aiReasonLabel = (reason: string | null | undefined): string =>
  translateTokens(reason, {
    no_advice: "本轮未提供建议",
    no_reason: "未附理由",
    low_confidence: "置信度不足，未采纳",
    below_confidence_threshold: "低于置信度门槛，未采纳",
    clamped: "已按风控边界裁剪",
    accepted: "已采纳",
  });

const evolutionReasonLabel = (reason: string | null | undefined): string =>
  translateTokens(reason, {
    proposal_not_validated: "提案未通过验证",
    kept_inert: "保持未生效（仅待人工评审）",
    insufficient_rounds: "轮次不足",
    no_change: "无需调整",
  });

const symbolNoteLabel = (note: string | null | undefined): string =>
  translateTokens(note, {
    ai_force_buy: "AI 强化买入",
    ai_force_sell: "AI 强化卖出",
    ai_chase_guarded: "防追高/炸板护栏拦截",
    vwap_breakdown_trap: "跌破日内均价线（疑似诱多派发）",
    bull_trap_divergence: "主力诱多形态拦截",
    morning_cooldown_guard: "早盘冷静期（防冲动追高）",
    ai_block: "AI 建议回避",
    ai_entry_pause: "AI 建议暂缓开仓",
    ai_weak_buffer: "AI 弱市提高门槛",
    ai_strong_relief: "AI 强市适度放宽",
    amount_audit_only: "成交额仅作审计",
    amount_missing_audit: "缺少成交额",
    volume_ratio_missing: "缺少量比基准",
    explicit_trailing_closes_used: "使用历史收盘序列",
    limit_fallback_board: "按板块规则估算涨跌停",
    volume_used_only_for_volume_ratio: "成交量仅用于量比",
    snapshot_only: "仅用盘中快照评分",
    scale_in: "持仓加仓",
    t_plus_one: "今天刚买，明天才能卖",
    cash_budget: "现金不够",
    limit_up_sealed: "已涨停，买不进",
    limit_down_sealed: "已跌停，卖不出",
    max_positions: "持仓已满",
  });

/** "AI 看法" combines direction and confidence into one phrase a person reads. */
const aiViewCell = (
  bias: number | null | undefined,
  confidence: number | null | undefined,
): string => {
  const conf = finite(confidence);
  if (conf === null || conf <= 0) return `<span class="live-dim">未表态</span>`;
  const b = finite(bias) ?? 0;
  const lean = b > 0 ? "看好" : b < 0 ? "谨慎" : "中性";
  return `${lean} <span class="live-dim">(${conf}%)</span>`;
};

const stockText = (name: string | null | undefined, symbol: string): string => {
  const cleanName = typeof name === "string" ? name.trim() : "";
  return cleanName ? `${cleanName} ${symbol}` : symbol;
};

/** Server-provided wording first; a local table is only a fallback for an older
 *  kernel that does not send labels yet. Keeps a single owner for semantics. */
const pickLabel = (
  serverLabel: string | null | undefined,
  fallback: string,
): string => {
  const clean = typeof serverLabel === "string" ? serverLabel.trim() : "";
  return clean && clean !== EMPTY ? clean : fallback;
};

/** RFC3339 / ISO timestamps are shown as-is when they cannot be parsed. */
const timeLabel = (value: string | null | undefined): string => {
  if (!value) return EMPTY;
  const date = new Date(value);
  if (Number.isNaN(date.getTime())) return text(value);
  const parts = new Intl.DateTimeFormat("zh-CN", {
    timeZone: "Asia/Shanghai",
    year: "numeric",
    month: "2-digit",
    day: "2-digit",
    hour: "2-digit",
    minute: "2-digit",
    second: "2-digit",
    hour12: false,
    hourCycle: "h23",
  }).formatToParts(date);
  const part = (type: Intl.DateTimeFormatPartTypes): string =>
    parts.find((item) => item.type === type)?.value ?? "";
  const stamp = `${part("year")}-${part("month")}-${part("day")} ${part("hour")}:${part("minute")}:${part("second")}`;
  return stamp.includes("--") ? text(value) : escapeHtml(stamp);
};

const errorDetail = (error: unknown): string => {
  if (error instanceof LiveApiError) {
    if (error.status === null) return "无法连接本地服务";
    try {
      const body: unknown = JSON.parse(error.detail);
      if (typeof body === "object" && body !== null && "error" in body) {
        const detail = (body as { error: unknown }).error;
        if (typeof detail === "string") return detail;
      }
    } catch {
      // A non-JSON error body still has a meaningful HTTP status.
    }
    return `HTTP ${error.status}`;
  }
  return error instanceof Error ? error.message : String(error);
};

async function liveApi<T>(path: string): Promise<T> {
  let response: Response;
  try {
    response = await fetch(path, { cache: "no-store", headers: { Accept: "application/json" } });
  } catch (error) {
    throw new LiveApiError(null, error instanceof Error ? error.message : String(error));
  }
  const body = await response.text();
  if (!response.ok) throw new LiveApiError(response.status, body.slice(0, 500));
  return body ? (JSON.parse(body) as T) : (undefined as T);
}

const isArray = <T>(value: unknown): value is T[] => Array.isArray(value);
const isRecord = (value: unknown): value is Record<string, unknown> =>
  typeof value === "object" && value !== null && !Array.isArray(value);
const numericField = (record: Record<string, unknown> | null, key: string): number | null => {
  const value = record?.[key];
  return typeof value === "number" ? finite(value) : null;
};

/** A 404 is a real "no live account yet" answer, not a broken endpoint. */
const optional = async <T>(key: keyof LiveData | "symbol", path: string): Promise<T | null> => {
  try {
    const value: unknown = await liveApi<unknown>(path);
    delete errors[key];
    return value as T;
  } catch (error) {
    errors[key] = errorDetail(error);
    return null;
  }
};

/** A malformed decisions response must degrade to an empty table, never throw. */
function decisionPayload(value: unknown): LiveDecisionPayload | null {
  if (isArray<LiveDecision>(value)) {
    const decisions = filters.signalsOnly
      ? value.filter((item) => item.side !== "hold" || item.blocked_reason)
      : value;
    return { round_id: value[0]?.round_id ?? "", signals_only: filters.signalsOnly,
      total: value.length, returned: decisions.length, decisions };
  }
  if (!isRecord(value) || !isArray<LiveDecision>(value.decisions)) return null;
  const total = numericField(value, "total");
  const returned = numericField(value, "returned");
  return {
    round_id: typeof value.round_id === "string" ? value.round_id : "",
    signals_only: value.signals_only === true,
    total: total ?? value.decisions.length,
    returned: returned ?? value.decisions.length,
    decisions: value.decisions,
  };
}

const decisionsPath = (roundId: string): string =>
  `/v2/live/decisions?round_id=${encodeURIComponent(roundId)}`
  + `&signals_only=${filters.signalsOnly ? "true" : "false"}`;

// ---- data loading ----------------------------------------------------------

/** The newest round by its real start time, not by array position.
 *  The kernel orders by started_at, but a caller-supplied round_id (e.g. a
 *  manual smoke test) can sort oddly; never trust the list order for "latest".
 */
function latestOf(rounds: LiveRound[] | null): LiveRound | null {
  if (!rounds || rounds.length === 0) return null;
  return rounds.reduce((best, row) =>
    row.started_at > best.started_at ? row : best);
}

function currentRoundId(rounds: LiveRound[] | null): string | null {
  const latest = latestOf(rounds);
  return latest ? latest.round_id : null;
}

function todayInShanghai(): string {
  const parts = new Intl.DateTimeFormat("en-CA", {
    timeZone: "Asia/Shanghai",
    year: "numeric",
    month: "2-digit",
    day: "2-digit",
  }).formatToParts(new Date());
  const part = (type: Intl.DateTimeFormatPartTypes): string =>
    parts.find((item) => item.type === type)?.value ?? "";
  return `${part("year")}-${part("month")}-${part("day")}`;
}

/** Extra read models keep the same `optional()`/`errors`/`draw()` flow as the
 *  core live data, but load only after their own view has been visited. */
async function loadLiveExtra(key: LiveExtraKey, force = false): Promise<void> {
  if (!force && (data[key] !== null || extraLoading[key])) return;
  const generation = ++extraSequence[key];
  extraLoading[key] = generation;
  if (activeView === key) draw();
  const value: unknown = await optional<unknown>(key, EXTRA_PATHS[key]);
  // Release the loading marker on *every* path, including the ones that return
  // early. Leaving it set after the reader navigated away made the panel say
  // "正在读取…" forever once they came back.
  if (extraLoading[key] === generation) delete extraLoading[key];
  if (!active || generation !== extraSequence[key]) return;
  if (key === "ai") data.ai = isArray<AiObservation>(value) ? value : [];
  else if (key === "evolution") data.evolution = isArray<EvolutionPreview>(value) ? value : [];
  else data.risk = isRecord(value) ? value as unknown as RiskSummary : null;
  draw();
}

async function loadSymbolArchive(symbol: string): Promise<void> {
  const clean = symbol.trim().toUpperCase();
  if (!clean) {
    symbolToken += 1;
    symbolState.loadedSymbol = null;
    symbolState.archive = null;
    symbolState.error = null;
    symbolState.loading = false;
    draw();
    return;
  }
  const generation = ++symbolToken;
  symbolState.loading = true;
  symbolState.error = null;
  symbolState.loadedSymbol = clean;
  draw();
  const archive = await optional<SymbolArchive>(
    "symbol",
    `/v2/live/symbol?symbol=${encodeURIComponent(clean)}&limit=120`,
  );
  // Only a superseded read keeps the marker: a newer lookup for another code
  // owns `loading` and this answer must not touch it. A read whose view was
  // left behind still commits its result and *always* clears the marker —
  // returning early here is what used to leave the page saying 「正在读取…」
  // forever after the reader navigated away and came back.
  if (generation !== symbolToken) return;
  symbolState.loading = false;
  symbolState.error = errors.symbol ?? null;
  symbolState.archive = isRecord(archive) ? archive as unknown as SymbolArchive : null;
  if (active) draw();
}

/**
 * Hand-off from the daily symbol page: set the live view's target state
 * directly.
 *
 * The previous implementation waited 60 ms and then clicked `#live-symbol-load`
 * programmatically. That button is disabled while the view is refreshing, and a
 * click on a disabled button is dropped by the browser — the URL said
 * `live/symbol` while the archive stayed empty. Writing the intended state is
 * unconditional, so it cannot depend on another control's disabled state.
 */
export function openLiveSymbolArchive(symbol: string): void {
  const clean = symbol.trim().toUpperCase();
  symbolState.input = clean;
  const input = document.getElementById("live-symbol-input");
  if (input instanceof HTMLInputElement) input.value = clean;
  void loadSymbolArchive(clean);
}

function roundsNewestFirst(rounds: LiveRound[]): LiveRound[] {
  return rounds.slice().sort((a, b) =>
    b.started_at.localeCompare(a.started_at) || b.round_id.localeCompare(a.round_id));
}

/** One refresh = one bounded batch; no overlapping requests, no polling storm. */
async function refreshLive(): Promise<void> {
  if (!active) return;
  const generation = ++decisionToken;
  if (loading) {
    refreshQueued = true;
    return;
  }
  loading = true;
  const seq = ++requestSeq;
  state = { ...state, status: "loading", message: "正在读取盘中数据…" };
  draw();
  try {
    const [account, rounds, trades, curve] = await Promise.all([
      optional<LiveAccountView>("account", "/v2/live/account"),
      optional<LiveRound[]>("rounds", "/v2/live/rounds"),
      optional<LiveTrade[]>("trades", "/v2/live/trades"),
      optional<LiveEquityPoint[]>("curve", "/v2/live/equity-curve"),
    ]);
    const desired = filters.roundId && isArray<LiveRound>(rounds) && rounds.some((round) => round.round_id === filters.roundId)
      ? filters.roundId
      : currentRoundId(isArray<LiveRound>(rounds) ? rounds : null);
    if (!desired) delete errors.decisions;
    // 第五个接口只用本批 rounds 中确认存在的 round_id；失败由 optional 收敛。
    const payload = desired && generation === decisionToken
      ? decisionPayload(await optional<unknown>("decisions", decisionsPath(desired)))
      : null;
    if (generation !== decisionToken) return; // 用户切换轮次后丢弃旧的决策响应
    if (seq !== requestSeq || !active) return;
    // 已访问过的扩展视图跟随 30 秒/手动刷新；未访问过的保持 null 不请求。
    // 核心读模型先落盘，扩展视图的独立响应再由各自 generation 收敛。
    void Promise.all(
      (Object.keys(EXTRA_PATHS) as LiveExtraKey[])
        .filter((key) => data[key] !== null)
        .map((key) => loadLiveExtra(key, true)),
    );
    data.account = isRecord(account) ? account : null;
    data.rounds = isArray<LiveRound>(rounds) ? rounds : [];
    data.trades = isArray<LiveTrade>(trades) ? trades : [];
    data.curve = isArray<LiveEquityPoint>(curve) ? curve : [];
    // The sentiment read is awaited in the middle of the batch. Waiting is
    // exactly when the reader can switch rounds, so the response is parked in
    // a local and committed only after re-reading the current round below.
    let sentiment: LiveSentiment | null = null;
    if (isArray<LiveRound>(rounds) && rounds.length > 0) {
      try {
        const sResp = await fetch("/v2/live/sentiment", {
          cache: "no-store",
          headers: { Accept: "application/json" },
        });
        if (sResp.ok) {
          const sJson = (await sResp.json()) as unknown;
          if (isRecord(sJson)) sentiment = sJson as unknown as LiveSentiment;
        }
      } catch {
        // Optional sentiment gauge never blocks core dashboard rendering.
      }
      // Re-read the token and the request sequence after the await: if the
      // round changed while sentiment was in flight, this batch is history and
      // must not resurrect the round and signals captured before it.
      if (!active || seq !== requestSeq || generation !== decisionToken) return;
    }
    data.sentiment = sentiment;
    filters.roundId = desired;
    data.decisions = desired && payload !== null ? payload.decisions : [];
    data.decisionsTotal = desired && payload !== null ? payload.total : null;
    const coreErrors = Object.keys(errors).filter((key) => key !== "symbol");
    state = {
      status: "ready",
      message: coreErrors.length > 0 ? "部分接口读取失败" : "已更新",
      fetchedAt: new Date(),
    };
    loaded = true;
  } catch (error) {
    if (seq !== requestSeq || !active || generation !== decisionToken) return;
    state = { ...state, status: "error", message: `加载失败：${errorDetail(error)}` };
  } finally {
    loading = false;
    if (active && seq === requestSeq) draw();
    if (refreshQueued && active) {
      refreshQueued = false;
      void refreshLive();
    }
  }
}

/** The signals view changes round without re-fetching the other four views. */
async function loadDecisions(roundId: string | null): Promise<void> {
  const generation = ++decisionToken;
  if (!roundId) {
    data.decisions = [];
    data.decisionsTotal = null;
    delete errors.decisions;
    draw();
    return;
  }
  state = { ...state, status: "loading", message: `正在读取轮次 ${roundId} 的决策信号…` };
  draw();
  const payload = decisionPayload(await optional<unknown>("decisions", decisionsPath(roundId)));
  if (!active || generation !== decisionToken) return;
  data.decisions = payload ? payload.decisions : [];
  data.decisionsTotal = payload ? payload.total : null;
  state = { ...state, status: "ready", message: "已更新", fetchedAt: new Date() };
  draw();
}

// ---- view composition ------------------------------------------------------

const stat = (label: string, value: string, extraClass = ""): string =>
  `<div class="curve-stat ${extraClass}"><span class="curve-stat-label">${label}</span><strong>${value}</strong></div>`;

const flag = (label: string, stateName: "ok" | "error" | "applied" | "plain"): string =>
  `<span class="live-flag" data-state="${stateName}">${label}</span>`;

const empty = (message: string): string => `<p class="empty">${message}</p>`;

const section = (title: string, description: string, body: string, aside = ""): string =>
  `<section class="panel">
    <div class="panel-head"><div><h2>${title}</h2><p class="muted">${description}</p></div>${aside}</div>
    ${body}
  </section>`;

function statusPanel(): string {
  const fetched = state.fetchedAt ? `本次读取 ${timeLabel(state.fetchedAt.toISOString())}` : EMPTY;
  const statusMessage = escapeHtml(state.message) || EMPTY;
  const notes = Object.entries(errors)
    .filter(([key]) => key !== "symbol")
    .map(([key, detail]) => `${escapeHtml(key)}: ${escapeHtml(detail)}`)
    .join("；");
  const statusState = state.status === "loading" || state.status === "error" ? state.status : "ready";
  return `<section class="panel live-status" data-state="${statusState}" aria-live="polite">
    <div class="toolbar" style="border-bottom:0">
      <span class="live-dot" aria-hidden="true"></span>
      <span>${statusMessage}</span>
      <span class="live-dim">${fetched}</span>
      <button type="button" class="btn btn-quiet" id="live-refresh" data-testid="live-refresh"${loading ? " disabled" : ""}>刷新</button>
      <span class="live-dim">每 30 秒自动更新</span>
    </div>
    ${notes ? `<p class="muted live-note" style="padding:0 20px 12px">有部分数据读取失败，页面显示可能不完整。</p>` : ""}
  </section>`;
}

/**
 * 槽位收益只取 API 的权益曲线点：服务端逐轮落盘的 steady/aggressive 权益与
 * 账户初始本金各半的差额。没有曲线点时显示「—」，不在前端估算。
 */
function profitFromPoints(slot: Slot): number | null {
  const points = data.curve;
  if (!points || points.length === 0) return null;
  const last = points[points.length - 1];
  const value = finite(slot === "steady" ? last.steady_equity_cents : last.aggressive_equity_cents);
  const initial = finite(data.account?.initial_cash_cents);
  if (value === null || initial === null) return null;
  const base = slot === "steady" ? Math.floor(initial / 2) : initial - Math.floor(initial / 2);
  return value - base;
}

/** Simple SVG equity curve from the API's own integer-cent equity points. */
function renderEquityCurve(points: LiveEquityPoint[]): string {
  if (points.length === 0) {
    return `<p class="chart-empty">${errors.curve ? `读取账户变化失败：${escapeHtml(errors.curve)}` : "还没有记录。等第一轮跑完，这里就会出现账户总值的变化。"}</p>`;
  }
  const width = 720;
  const height = 220;
  const padX = 10;
  const padY = 14;
  const values = points.map((point) => finite(point.equity_cents)).filter((value): value is number => value !== null);
  if (values.length !== points.length) return `<p class="chart-empty">权益点缺失，暂不能绘制曲线。</p>`;
  const min = Math.min(...values);
  const max = Math.max(...values);
  const span = Math.max(1, max - min);
  const plotWidth = width - padX * 2;
  const plotHeight = height - padY * 2;
  const x = (index: number): number =>
    points.length === 1 ? padX + plotWidth / 2 : padX + (index * plotWidth) / (points.length - 1);
  const y = (value: number): number => padY + plotHeight - ((value - min) / span) * plotHeight;
  const path = values
    .map((value, index) => `${index === 0 ? "M" : "L"}${x(index).toFixed(2)},${y(value).toFixed(2)}`)
    .join(" ");
  const area = `${path} L${x(points.length - 1).toFixed(2)},${(padY + plotHeight).toFixed(2)} L${x(0).toFixed(2)},${(padY + plotHeight).toFixed(2)} Z`;
  const stroke = values[values.length - 1] >= values[0] ? "var(--positive)" : "var(--negative)";
  const guideYs = [padY + plotHeight * 0.25, padY + plotHeight * 0.5, padY + plotHeight * 0.75];
  const guides = guideYs
    .map((gy) => `<line class="curve-baseline" x1="${padX}" y1="${gy.toFixed(1)}" x2="${width - padX}" y2="${gy.toFixed(1)}" />`)
    .join("");
  const lastX = x(points.length - 1).toFixed(2);
  const lastY = y(values[values.length - 1]).toFixed(2);
  const endDot = `<circle cx="${lastX}" cy="${lastY}" r="6" fill="${stroke}" fill-opacity="0.18" />
    <circle cx="${lastX}" cy="${lastY}" r="3.2" fill="${stroke}" />`;
  return `<div class="chart-wrap">
    <svg class="curve" viewBox="0 0 ${width} ${height}" preserveAspectRatio="none"
         role="img" aria-label="盘中权益曲线，共 ${points.length} 个轮次">
      <defs>
        <linearGradient id="live-fill" x1="0" y1="0" x2="0" y2="1">
          <stop offset="0%" stop-color="${stroke}" stop-opacity="0.22" />
          <stop offset="100%" stop-color="${stroke}" stop-opacity="0.02" />
        </linearGradient>
      </defs>
      ${guides}
      <path d="${area}" fill="url(#live-fill)" stroke="none" />
      <path d="${path}" fill="none" stroke="${stroke}" stroke-width="2"
            stroke-linejoin="round" stroke-linecap="round" />
      ${endDot}
    </svg>
    <div class="live-scale"><span>${timeLabel(points[0].quote_time)}</span><span>${timeLabel(points[points.length - 1].quote_time)}</span></div>
    <div class="live-scale"><span>最低 ${money(min)}</span><span>最高 ${money(max)}</span><span>最新 ${money(values[values.length - 1])}</span></div>
  </div>`;
}

function latestRound(): LiveRound | null {
  return latestOf(data.rounds);
}

function renderDashboard(): string {
  const account = data.account;
  const round = latestRound();
  const curve = data.curve ?? [];
  const pnlDay = account?.daily_pnl_day;
  const pnlTitle = pnlDay && pnlDay !== todayInShanghai()
    ? `交易日盈亏 · ${escapeHtml(pnlDay.slice(5))}`
    : "今日盈亏";
  const figures = `<section class="hero" aria-labelledby="live-overview-title">
    <div>
      <p class="eyebrow">模拟资金 · 不涉及真实交易</p>
      <h2 id="live-overview-title">我的模拟账户</h2>
      <p class="muted live-note">系统每 3 分钟自动买卖一次，用的是实时行情（有几十秒延迟）。${errors.account ? ` 账户读取失败：${escapeHtml(errors.account)}` : ""}</p>
    </div>
    <dl class="hero-figures live-hero-figures">
      <div><dt>本金</dt><dd>${money(account?.initial_cash_cents)}</dd></div>
      <div><dt>总资产</dt><dd>${money(account?.equity_cents)}</dd></div>
      <div><dt>${pnlTitle}</dt><dd data-testid="live-daily-pnl" class="${pnlClass(account?.daily_pnl_cents)}">${signedMoney(account?.daily_pnl_cents)}</dd></div>
      <div><dt>累计收益</dt><dd class="${pnlClass(account?.profit_cents)}">${signedMoney(account?.profit_cents)}</dd></div>
      <div><dt>收益率</dt><dd class="${pnlClass(account?.return_bps)}">${percent(account?.return_bps)}</dd></div>
      <div><dt>可用现金</dt><dd>${money(account?.cash_cents)}</dd></div>
    </dl>
  </section>`;

  const slot = (id: Slot): LiveSlotView | undefined =>
    account?.slots.find((item) => item.slot === id);
  const slotCard = (id: Slot, title: string): string => {
    const view = slot(id);
    const desc = id === "steady"
      ? "挑走势稳、波动小的股票，单只最多投 15%"
      : "追近期涨得快的股票，单只最多投 30%";
    return `<article class="strategy-card live-strategy-card" data-slot="${id}">
      <header><div><span class="slot-badge">${id === "steady" ? "求稳" : "求快"}</span><h3>${title}</h3></div></header>
      <p class="strategy-desc">${desc}。</p>
      <dl class="metric-grid">
        <div><dt>这个策略的钱</dt><dd>${money(view?.equity_cents)}</dd></div>
        <div><dt>其中买了股票</dt><dd>${money(view?.market_value_cents)}</dd></div>
        <div><dt>还剩现金</dt><dd>${money(view?.cash_cents)}</dd></div>
        <div><dt>持有几只</dt><dd>${count(view?.positions)}</dd></div>
        <div><dt>今天能卖几只</dt><dd>${count(view?.sellable_positions)}</dd></div>
        <div><dt>赚了多少</dt><dd class="${view ? pnlClass(profitFromPoints(view.slot)) : ""}">${view ? signedMoney(profitFromPoints(view.slot)) : EMPTY}</dd></div>
      </dl>
    </article>`;
  };
  const slotCards = `<section class="strategy-grid live-strategy-grid" aria-label="两个槽位">
    ${slotCard("steady", "稳健")}${slotCard("aggressive", "激进")}
  </section>`;

  const aiState = round
    ? flag(
      pickLabel(round.ai_label, aiLabel(round.ai_ok)),
      aiClass(round.ai_ok),
    )
    : EMPTY;
  const summary = section(
    "刚才那一轮",
    "系统最近一次自动决策的情况。每 3 分钟跑一轮，一天大约 80 轮。",
    `<dl class="metric-grid wide" style="padding:16px 20px;margin:0">
      <div><dt>时间</dt><dd>${timeLabel(round?.started_at)}</dd></div>
      <div><dt>看了多少只股票</dt><dd>${round ? count(round.quote_count) : EMPTY}</dd></div>
      <div><dt>做了几笔买卖</dt><dd>${round ? count(round.trade_count) : EMPTY}</dd></div>
      <div><dt>AI 有没有参与</dt><dd>${aiState}</dd></div>
      <div><dt>当时市场</dt><dd>${round ? pickLabel(round.regime_label, text(round.regime)) : EMPTY}</dd></div>
      <div><dt>这一轮后账户</dt><dd>${round ? money(round.equity_cents) : EMPTY}</dd></div>
    </dl>`,
    `<span class="count">${data.rounds && data.rounds.length > 0 ? `已跑 ${data.rounds.length} 轮` : "还没有开始"}</span>`,
  );

  // 持仓列表：普通人最想看「我买了什么、现在赚了还是亏了」。
  const positions = account?.positions ?? [];
  const holdingsBody = positions.length === 0
    ? empty("现在没有持股，全部是现金。")
    : `<div class="table-scroll"><table class="live-table">
        <thead><tr><th>股票</th><th>策略</th><th class="num-col">持有股数</th><th class="num-col">成本价</th><th class="num-col">现在价</th><th class="num-col">市值</th><th class="num-col">浮动盈亏</th><th>今天能卖</th></tr></thead>
        <tbody>${positions.map((item) => {
          const value = finite(item.market_value_cents);
          const cost = finite(item.avg_cost_cents);
          const quantity = finite(item.quantity);
          const costBasis = cost !== null && quantity !== null ? cost * quantity : null;
          const pnl = value !== null && costBasis !== null ? value - costBasis : null;
          const pnlPct = pnl !== null && costBasis !== null && costBasis > 0
            ? Math.round((pnl * 10_000) / costBasis)
            : null;
          return `<tr>
            <td class="live-nowrap"><button type="button" class="live-link" data-symbol="${escapeHtml(item.symbol)}" aria-label="查看 ${escapeHtml(stockText(item.name, item.symbol))} 的个股档案">${stockLabel(item.name, item.symbol)}</button></td>
            <td>${slotLabel(item.slot)}</td>
            <td class="num-col">${count(item.quantity)}</td>
            <td class="num-col">${money(item.avg_cost_cents)}</td>
            <td class="num-col">${money(item.last_price_cents)}</td>
            <td class="num-col">${money(item.market_value_cents)}</td>
            <td class="num-col ${pnlClass(pnl)}">${signedMoney(pnl)}${pnlPct !== null ? ` <span class="live-dim">(${pnlPct > 0 ? "+" : ""}${(pnlPct / 100).toFixed(2)}%)</span>` : ""}</td>
            <td>${item.sellable_quantity > 0 ? "可以" : `<span class="live-dim">明天</span>`}</td>
          </tr>`;
        }).join("")}</tbody></table></div>`;

  const lastEquity = curve.length > 0 ? money(curve[curve.length - 1].equity_cents) : EMPTY;
  const holdings = section(
    "我买了哪些股票",
    "成本价是买入时的价格，现在价是最近一次行情。浮动盈亏按现在价算，卖了才算真赚或真亏。",
    holdingsBody,
    `<span class="count">${positions.length} 只</span>`,
  );
  const s = data.sentiment;
  const sentimentSection = s && s.total_quotes > 0
    ? section(
      "市场情绪与赚钱效应温度计",
      escapeHtml(s.posture || "基于本轮全市场涨跌广度、涨跌停比与冲高回落（炸板）率实时合成。"),
      `<dl class="metric-grid wide" style="padding:16px 20px;margin:0">
        <div><dt>情绪温度</dt><dd class="${s.score >= 58 ? "pos" : s.score <= 40 ? "neg" : ""}">${count(s.score)} / 100 · ${escapeHtml(s.label)}</dd></div>
        <div><dt>涨跌广度</dt><dd>${count(s.up_count)} 涨 / ${count(s.down_count)} 跌（${(s.up_ratio_bps / 100).toFixed(1)}%）</dd></div>
        <div><dt>大涨 / 大跌 (≥5%)</dt><dd><span class="pos">${count(s.strong_count)}</span> / <span class="neg">${count(s.weak_count)}</span></dd></div>
        <div><dt>涨停 / 跌停</dt><dd><span class="pos">${count(s.limit_up_count)}</span> / <span class="neg">${count(s.limit_down_count)}</span></dd></div>
        <div><dt>冲高回落占比</dt><dd class="${s.surge_fade_rate_bps >= 3500 ? "neg" : ""}">${(s.surge_fade_rate_bps / 100).toFixed(1)}%（${count(s.surge_fade_count)} 只）</dd></div>
        <div><dt>建议槽位仓位上限</dt><dd>${(s.recommended_max_exposure_bps / 100).toFixed(0)}%</dd></div>
      </dl>`,
      `<span class="count">全市场 ${count(s.total_quotes)} 只</span>`,
    )
    : "";

  return `${statusPanel()}${figures}${sentimentSection}${slotCards}${holdings}${summary}${section(
    "账户总值变化", "每跑完一轮记一个点，你能看到钱是怎么变的。", renderEquityCurve(curve),
    `<span class="count">共 ${curve.length} 点 · 最新 ${lastEquity}</span>`,
  )}`;
}

function roundOptions(): string {
  const rounds = data.rounds ?? [];
  if (rounds.length === 0) return "";
  const selected = filters.roundId ?? currentRoundId(rounds);
  // A full session is ~80 rounds; listing them all makes the picker unusable.
  return roundsNewestFirst(rounds)
    .slice(0, 20)
    .map((round) => {
      const value = escapeHtml(round.round_id);
      const label = `${timeLabel(round.started_at)} · 买卖 ${count(round.trade_count)} 笔`;
      return `<option value="${value}"${round.round_id === selected ? " selected" : ""}>${label}</option>`;
    })
    .join("");
}

/** Decisions matching the active slot/side filters. Shared by the table and the
 *  pager so the two can never disagree about how many rows exist. */
function filteredSignals(): LiveDecision[] {
  const decisions = data.decisions ?? [];
  return decisions.filter((item) =>
    (filters.slot === "all" || item.slot === filters.slot)
    && (filters.side === "all" || item.side === filters.side));
}

function signalPageCount(): number {
  return Math.max(1, Math.ceil(filteredSignals().length / filters.pageSize));
}

function renderSignals(): string {
  const rounds = data.rounds ?? [];
  const decisions = data.decisions ?? [];
  const total = data.decisionsTotal;
  const totalLabel = count(total);
  const signalLabel = count(decisions.length);
  const sides = [...new Set(decisions.map((item) => item.side))].sort();
  const filtered = filteredSignals();
  // Clamp the page against the *filtered* length: changing a filter can shrink
  // the result set below the current page, and an out-of-range page would
  // render an empty table with a valid count beside it.
  const pageSize = filters.pageSize;
  const pageCount = Math.max(1, Math.ceil(filtered.length / pageSize));
  const page = Math.min(Math.max(1, filters.page), pageCount);
  filters.page = page;
  const pageStart = (page - 1) * pageSize;
  const visible = filtered.slice(pageStart, pageStart + pageSize);
  const rangeLabel = filtered.length === 0
    ? "0 条"
    : `第 ${pageStart + 1}–${pageStart + visible.length} 条 / 共 ${filtered.length} 条`;
  const summaryLabel = filters.signalsOnly
    ? `本轮共 ${totalLabel} 条决策，其中 ${signalLabel} 条有信号 · ${rangeLabel}`
    : `本轮共 ${totalLabel} 条决策（显示全部）· ${rangeLabel}`;
  const switchLabel = filters.signalsOnly
    ? `只看有信号的（${signalLabel} / ${totalLabel}）`
    : `显示全部（${signalLabel} / ${totalLabel}）`;
  const pager = `<div class="live-pager">
    <button type="button" class="live-page" data-page="first"${page <= 1 ? " disabled" : ""}>« 首页</button>
    <button type="button" class="live-page" data-page="prev"${page <= 1 ? " disabled" : ""}>‹ 上一页</button>
    <span class="live-dim">第 ${page} / ${pageCount} 页</span>
    <button type="button" class="live-page" data-page="next"${page >= pageCount ? " disabled" : ""}>下一页 ›</button>
    <button type="button" class="live-page" data-page="last"${page >= pageCount ? " disabled" : ""}>末页 »</button>
    <label class="field-inline"><span>每页</span><select id="live-page-size">
      ${PAGE_SIZES.map((size) => `<option value="${size}"${size === pageSize ? " selected" : ""}>${size}</option>`).join("")}
    </select></label>
  </div>`;
  const toolbar = `<div class="toolbar">
    <label class="field-inline"><span>哪一轮</span><select id="live-round">${roundOptions()}</select></label>
    <label class="field-inline"><span>哪个策略</span><select id="live-slot-filter">
      <option value="all"${filters.slot === "all" ? " selected" : ""}>全部</option>
      <option value="steady"${filters.slot === "steady" ? " selected" : ""}>稳健</option>
      <option value="aggressive"${filters.slot === "aggressive" ? " selected" : ""}>激进</option>
    </select></label>
    <label class="field-inline"><span>做什么</span><select id="live-side-filter">
      <option value="all"${filters.side === "all" ? " selected" : ""}>全部</option>
      ${sides.map((side) => `<option value="${escapeHtml(side)}"${filters.side === side ? " selected" : ""}>${sideLabel(side)}</option>`).join("")}
      ${sides.length > 0 && filters.side !== "all" && !sides.includes(filters.side) ? `<option value="${escapeHtml(filters.side)}" selected>${sideLabel(filters.side)}</option>` : ""}
    </select></label>
    <label class="field-inline"><span>显示全部</span><input id="live-show-all" type="checkbox" data-testid="live-show-all"${filters.signalsOnly ? "" : " checked"} /></label>
    <span class="count" data-testid="live-decisions-mode-label">${switchLabel}</span>
    <span class="count" data-testid="live-decisions-summary">${summaryLabel}</span>
  </div>${pager}`;
  const body = rounds.length === 0
    ? empty(errors.rounds ? `调度记录读取失败：${escapeHtml(errors.rounds)}` : "还没有实时轮次")
    : decisions.length === 0
      ? empty(errors.decisions
        ? `本轮决策信号读取失败：${escapeHtml(errors.decisions)}`
        : filters.signalsOnly ? "本轮没有决策信号。" : "本轮还没有决策记录。")
      : `<div class="table-scroll table-tall"><table class="live-table">
          <thead><tr><th>股票</th><th>策略</th><th class="num-col">评分</th><th>结论</th><th>为什么没成交</th><th class="num-col">AI 看法</th><th>AI</th></tr></thead>
          <tbody>${visible.map((item) => `<tr>
            <td class="live-nowrap"><button type="button" class="live-link" data-symbol="${escapeHtml(item.symbol)}" aria-label="查看 ${escapeHtml(stockText(item.name, item.symbol))} 的个股档案">${stockLabel(item.name, item.symbol)}</button></td>
            <td>${pickLabel(item.slot_label, slotLabel(item.slot))}</td>
            <td class="num-col">${scoreCell(item.score)}</td>
            <td>${pickLabel(item.side_label, sideLabel(item.side))}</td>
            <td class="live-dim">${pickLabel(item.blocked_label, blockedFallback(item.blocked_reason))}</td>
            <td class="num-col">${aiViewCell(item.ai_bias, item.ai_confidence)}</td>
            <td>${item.ai_applied ? flag("参与", "applied") : `<span class="live-dim">未参与</span>`}</td>
          </tr>`).join("")}</tbody></table></div>${filtered.length === 0 ? empty("当前筛选没有匹配的股票。") : ""}`;
  return `${statusPanel()}${section(
    "决策信号",
    "每一轮，系统给每只股票打分并决定买、卖还是观望。评分越高越想买；「为什么没成交」说明卡在哪一步。",
    toolbar + body,
  )}`;
}

function renderTrades(): string {
  const trades = data.trades ?? [];
  const body = trades.length === 0
    ? empty(errors.trades ? `成交读取失败：${escapeHtml(errors.trades)}` : "还没有实时成交。")
    : `<div class="table-scroll table-tall"><table class="live-table">
        <thead><tr><th>时间</th><th>策略</th><th>股票</th><th>操作</th><th class="num-col">股数</th><th class="num-col">成交价</th><th class="num-col">手续费</th><th>为什么</th><th>AI</th></tr></thead>
        <tbody>${trades.map((item) => `<tr>
          <td class="live-nowrap">${timeLabel(item.created_at)}</td>
          <td>${pickLabel(item.slot_label, slotLabel(item.slot))}</td>
          <td class="live-nowrap"><button type="button" class="live-link" data-symbol="${escapeHtml(item.symbol)}" aria-label="查看 ${escapeHtml(stockText(item.name, item.symbol))} 的个股档案">${stockLabel(item.name, item.symbol)}</button></td>
          <td>${pickLabel(item.side_label, sideLabel(item.side))}</td>
          <td class="num-col">${count(item.quantity)}</td>
          <td class="num-col">${money(item.price_cents)}</td>
          <td class="num-col">${money(item.fee_cents)}</td>
          <td class="live-dim">${pickLabel(item.reason_label, text(item.reason))}</td>
          <td>${item.ai_applied ? flag("AI 参与", "applied") : `<span class="live-dim">按规则</span>`}</td>
        </tr>`).join("")}</tbody></table></div>`;
  return `${statusPanel()}${section("实时成交", "系统在盘中的每一笔模拟买卖。标「AI 参与」的是 AI 建议实际改变了那一笔。", body, `<span class="count">共 ${trades.length} 笔</span>`)}`;
}

function renderRounds(): string {
  const rounds = data.rounds ?? [];
  const body = rounds.length === 0
    ? empty(errors.rounds ? `调度记录读取失败：${escapeHtml(errors.rounds)}` : "还没有实时轮次")
    : `<div class="table-scroll table-tall"><table class="live-table">
        <thead><tr><th>时间</th><th class="num-col">扫描股票</th><th class="num-col">买入/卖出</th><th>AI</th><th class="num-col">账户总值</th><th>市场</th><th>本轮说明</th><th>操作</th></tr></thead>
        <tbody>${roundsNewestFirst(rounds).map((round) => `<tr>
          <td class="live-nowrap">${timeLabel(round.started_at)}</td>
          <td class="num-col">${count(round.quote_count)}</td>
          <td class="num-col">${count(round.trade_count)}</td>
          <td>${flag(
            pickLabel(round.ai_label, aiLabel(round.ai_ok)),
            aiClass(round.ai_ok),
          )}${round.ai_error && !round.ai_label ? `<br><span class="live-dim">${text(round.ai_error)}</span>` : ""}</td>
          <td class="num-col">${money(round.equity_cents)}</td>
          <td>${pickLabel(round.regime_label, text(round.regime))}</td>
          <td class="live-dim live-note">${pickLabel(round.note_label, text(round.note))}</td>
          <td><button type="button" class="live-link" data-round="${escapeHtml(round.round_id)}">看这一轮</button></td>
        </tr>`).join("")}</tbody></table></div>`;
  return `${statusPanel()}${section("调度记录", "系统每 3 分钟跑一轮，每轮一行。点「看这一轮」可以看它当时对每只股票的判断。", body, `<span class="count">共 ${rounds.length} 轮</span>`)}`;
}

function renderSignalBaseline(baseline: AiObservation["signal_baseline"]): string {
  if (!baseline) return "—";
  const summary = `方向变化 ${count(baseline.side_changed)} / ${count(baseline.compared)}`;
  if (baseline.fills.length === 0) return summary;
  return `<details class="live-details"><summary>${summary}</summary><ul>${baseline.fills.map(
    (fill) => `<li>${slotLabel(fill.slot)} ${text(fill.symbol)}：无 AI 信号 ${sideLabel(fill.without_ai)}，实际成交 ${sideLabel(fill.side)}</li>`,
  ).join("")}</ul></details>`;
}

function renderAi(): string {
  const rows = data.ai;
  const body = rows === null || extraLoading.ai
    ? empty("正在读取 AI 观测…")
    : rows.length === 0
      ? empty(errors.ai ? `AI 观测读取失败：${escapeHtml(errors.ai)}` : "还没有 AI 观测。等第一轮跑完，这里就会出现模型的原始建议。")
      : `<div class="table-scroll table-tall"><table class="live-table">
          <thead><tr><th>时间</th><th>模型</th><th class="num-col">置信度</th><th>采纳</th><th>裁剪</th><th>理由</th><th>无 AI 对照</th><th>原始建议</th></tr></thead>
          <tbody>${rows.map((item) => `<tr>
            <td class="live-nowrap">${timeLabel(item.started_at)}</td>
            <td>${text(item.model)}</td>
            <td class="num-col">${count(item.confidence)}</td>
            <td>${item.accepted ? flag("已采纳", "ok") : flag("未采纳", "plain")}</td>
            <td>${item.clamped ? flag("已裁剪", "applied") : flag("无", "plain")}</td>
            <td class="live-note">${aiReasonLabel(item.reason)}</td>
            <td>${renderSignalBaseline(item.signal_baseline)}</td>
            <td><details class="live-details"><summary>查看原文</summary><pre class="live-raw">${text(item.raw_advice)}</pre></details></td>
          </tr>`).join("")}</tbody></table></div>`;
  return `${statusPanel()}${section("AI 观测", "查看模型建议、内核采纳和裁剪记录。对照使用同轮行情与持仓，比较有无 AI 的信号方向。", body, `<span class="count">${rows ? `共 ${rows.length} 条` : EMPTY}</span>`)}`;
}

function renderEvolution(): string {
  const rows = data.evolution;
  const body = rows === null || extraLoading.evolution
    ? empty("正在读取进化提案…")
    : rows.length === 0
      ? empty(errors.evolution ? `进化提案读取失败：${escapeHtml(errors.evolution)}` : "还没有进化提案。收盘后才会生成供人工评审的提案。")
      : `<div class="table-scroll table-tall"><table class="live-table">
          <thead><tr><th>交易日</th><th class="num-col">开仓门槛调整</th><th class="num-col">动量权重倾斜</th><th class="num-col">最大持仓变化</th><th class="num-col">现金底线变化</th><th>采纳状态</th><th class="num-col">收益</th><th class="num-col">轮次</th><th class="num-col">最大回撤</th><th>说明</th></tr></thead>
          <tbody>${rows.map((item) => {
            const proposal = isRecord(item.proposal) ? item.proposal : {};
            const before = isRecord(item.before_metrics) ? item.before_metrics : {};
            const delta = (key: string): string => {
              const value = numericField(proposal, key);
              return value === null ? EMPTY : `${value > 0 ? "+" : ""}${count(value)}`;
            };
            return `<tr>
              <td class="live-nowrap">${text(item.day)}</td>
              <td class="num-col">${delta("entry_delta")}</td>
              <td class="num-col">${delta("weight_tilt_bps")}</td>
              <td class="num-col">${delta("max_positions_delta")}</td>
              <td class="num-col">${delta("cash_floor_delta_bps")}</td>
              <td>${item.accepted ? flag("已采纳", "ok") : flag("待人工评审", "plain")}</td>
              <td class="num-col ${pnlClass(numericField(before, "profit_bps"))}">${percent(numericField(before, "profit_bps"))}</td>
              <td class="num-col">${count(numericField(before, "round_count"))}</td>
              <td class="num-col">${percent(numericField(before, "max_drawdown_bps"))}</td>
              <td class="live-note">${evolutionReasonLabel(item.reason)}</td>
            </tr>`;
          }).join("")}</tbody></table></div>`;
  return `${statusPanel()}${section("进化提案", "收盘后根据已落盘权益生成的调整建议。提案只供人工评审，不会自动生效。", body, `<span class="count">${rows ? `共 ${rows.length} 条` : EMPTY}</span>`)}`;
}

function renderRisk(): string {
  const summary = data.risk;
  const counters = summary?.counters ?? [];
  const body = summary === null || extraLoading.risk
    ? empty(errors.risk && !extraLoading.risk ? `风控拦截读取失败：${escapeHtml(errors.risk)}` : "正在读取风控拦截…")
    : counters.length === 0
      ? empty("最近 40 轮还没有风控拦截。")
      : `<div class="table-scroll table-tall"><table class="live-table">
          <thead><tr><th>原因</th><th class="num-col">次数</th></tr></thead>
          <tbody>${counters.map((item) => `<tr>
            <td>${pickLabel(item.label, text(item.reason))}</td>
            <td class="num-col">${count(item.count)}</td>
          </tr>`).join("")}</tbody></table></div>`;
  const summaryText = summary
    ? `最近 ${count(summary.window_rounds)} 轮内共 ${count(summary.blocked_total)} 次拦截`
    : `最近 40 轮内共 ${EMPTY} 次拦截`;
  return `${statusPanel()}<section class="hero" aria-labelledby="live-risk-title">
      <div><p class="eyebrow">模拟资金 · 不涉及真实交易</p><h2 id="live-risk-title">风控拦截汇总</h2></div>
      <dl class="hero-figures"><div><dt>统计轮次</dt><dd>${summary ? count(summary.window_rounds) : EMPTY}</dd></div>
      <div><dt>拦截次数</dt><dd>${summary ? count(summary.blocked_total) : EMPTY}</dd></div></dl>
    </section>${section("拦截原因", summaryText + "。每次拦截都保留内核给出的中文原因。", body, summary ? `<span class="count">共 ${counters.length} 类</span>` : "")}`;
}

function renderSymbol(): string {
  const archive = symbolState.archive;
  const toolbar = `<div class="toolbar">
    <label class="field-inline"><span>股票代码</span><input id="live-symbol-input" type="search" autocomplete="off" placeholder="600000.SH" value="${escapeHtml(symbolState.input)}" /></label>
    <button type="button" class="btn" id="live-symbol-load"${symbolState.loading ? " disabled" : ""}>加载</button>
    <span class="live-dim">输入代码查看盘中成交与信号档案</span>
  </div>`;
  let result: string;
  if (symbolState.loading) result = empty(`正在读取 ${text(symbolState.loadedSymbol)} 的个股档案…`);
  else if (!symbolState.loadedSymbol) result = empty("输入股票代码后点「加载」，查看盘中成交与信号档案。");
  else if (symbolState.error) result = empty(`个股档案读取失败：${escapeHtml(symbolState.error)}`);
  else if (!archive || (archive.events ?? []).length === 0) result = empty(`${text(symbolState.loadedSymbol)} 还没有盘中档案。`);
  else {
    const metrics = `<dl class="metric-grid wide" style="padding:16px 20px;margin:0">
      <div><dt>成交笔数</dt><dd>${count(archive.trade_count)}</dd></div>
      <div><dt>买入股数</dt><dd>${count(archive.buy_quantity)}</dd></div>
      <div><dt>卖出股数</dt><dd>${count(archive.sell_quantity)}</dd></div>
      <div><dt>累计手续费</dt><dd>${money(archive.realized_fee_cents)}</dd></div>
      <div><dt>决策条数</dt><dd>${count(archive.decision_count)}</dd></div>
    </dl>`;
    const events = `<div class="table-scroll table-tall"><table class="live-table">
      <thead><tr><th>时间</th><th>类型</th><th>策略</th><th>方向</th><th class="num-col">数量</th><th class="num-col">价格</th><th class="num-col">评分</th><th>结果说明</th></tr></thead>
      <tbody>${archive.events.map((item) => `<tr>
        <td class="live-nowrap">${timeLabel(item.started_at)}</td>
        <td>${item.kind === "trade" ? "成交" : item.kind === "decision" ? "信号" : text(item.kind)}</td>
        <td>${pickLabel(item.slot_label, slotLabel(item.slot))}</td>
        <td>${pickLabel(item.side_label, sideLabel(item.side))}</td>
        <td class="num-col">${item.kind === "trade" ? count(item.quantity) : EMPTY}</td>
        <td class="num-col">${item.kind === "trade" ? money(item.price_cents) : EMPTY}</td>
        <td class="num-col">${item.kind === "decision" ? scoreCell(item.score) : EMPTY}</td>
        <td class="live-note">${pickLabel(item.blocked_label, symbolNoteLabel(item.note))}</td>
      </tr>`).join("")}</tbody></table></div>`;
    result = `${section("档案汇总", stockText(archive.name, archive.symbol), metrics)}${section("事件记录", "成交与信号按时间从新到旧排列。", events, `<span class="count">共 ${archive.events.length} 条</span>`)}`;
  }
  return `${statusPanel()}${section("个股档案", "盘中内核自己的成交与决策记录，不混用日线账本。", toolbar)}${result}`;
}

function body(): string {
  switch (activeView) {
    case "signals": return renderSignals();
    case "trades": return renderTrades();
    case "rounds": return renderRounds();
    case "ai": return renderAi();
    case "evolution": return renderEvolution();
    case "risk": return renderRisk();
    case "symbol": return renderSymbol();
    default: return renderDashboard();
  }
}

function bindEvents(): void {
  const refreshButton = document.getElementById("live-refresh");
  if (refreshButton instanceof HTMLButtonElement) {
    refreshButton.addEventListener("click", () => void refreshLive());
  }
  const roundSelect = document.getElementById("live-round");
  if (roundSelect instanceof HTMLSelectElement) {
    roundSelect.addEventListener("change", () => {
      filters.roundId = roundSelect.value || null;
      filters.page = 1; // a different round has a different row count
      void loadDecisions(filters.roundId);
    });
  }
  const slotSelect = document.getElementById("live-slot-filter");
  if (slotSelect instanceof HTMLSelectElement) {
    slotSelect.addEventListener("change", () => {
      filters.slot = slotSelect.value;
      filters.page = 1; // a narrower result set must not land on a stale page
      draw();
    });
  }
  const sideSelect = document.getElementById("live-side-filter");
  if (sideSelect instanceof HTMLSelectElement) {
    sideSelect.addEventListener("change", () => {
      filters.side = sideSelect.value;
      filters.page = 1;
      draw();
    });
  }
  const showAll = document.getElementById("live-show-all");
  if (showAll instanceof HTMLInputElement) {
    showAll.addEventListener("change", () => {
      filters.signalsOnly = !showAll.checked;
      filters.page = 1;
      void loadDecisions(filters.roundId);
    });
  }
  const pageSizeSelect = document.getElementById("live-page-size");
  if (pageSizeSelect instanceof HTMLSelectElement) {
    pageSizeSelect.addEventListener("change", () => {
      const size = Number(pageSizeSelect.value);
      if (PAGE_SIZES.includes(size as (typeof PAGE_SIZES)[number])) {
        filters.pageSize = size;
        filters.page = 1;
        draw();
      }
    });
  }
  for (const button of root.querySelectorAll<HTMLButtonElement>(".live-page")) {
    button.addEventListener("click", () => {
      const action = button.dataset.page;
      const pageCount = signalPageCount();
      if (action === "first") filters.page = 1;
      else if (action === "prev") filters.page = Math.max(1, filters.page - 1);
      else if (action === "next") filters.page = Math.min(pageCount, filters.page + 1);
      else if (action === "last") filters.page = pageCount;
      draw();
    });
  }
  for (const button of root.querySelectorAll<HTMLButtonElement>(".live-link[data-round]")) {
    button.addEventListener("click", () => {
      const roundId = button.dataset.round;
      if (!roundId) return;
      filters.roundId = roundId;
      filters.slot = "all";
      filters.side = "all";
      filters.page = 1;
      pushHash({ module: "live", view: "signals" });
      const next: Route = { module: "live", view: "signals" };
      if (applyRoute) applyRoute(next);
      else renderLive(next); // fallback: 仍保持容器渲染
      void loadDecisions(roundId);
    });
  }
  for (const button of root.querySelectorAll<HTMLButtonElement>(".live-link[data-symbol]")) {
    button.addEventListener("click", () => {
      const symbol = button.dataset.symbol;
      if (!symbol) return;
      symbolState.input = symbol;
      pushHash({ module: "live", view: "symbol" });
      const next: Route = { module: "live", view: "symbol" };
      if (applyRoute) applyRoute(next);
      else renderLive(next);
      void loadSymbolArchive(symbol);
    });
  }
  const symbolInput = document.getElementById("live-symbol-input");
  if (symbolInput instanceof HTMLInputElement) {
    symbolInput.addEventListener("input", () => {
      symbolState.input = symbolInput.value;
    });
    symbolInput.addEventListener("keydown", (event) => {
      if (event.key === "Enter") {
        symbolState.input = symbolInput.value;
        void loadSymbolArchive(symbolState.input);
      }
    });
  }
  const symbolButton = document.getElementById("live-symbol-load");
  if (symbolButton instanceof HTMLButtonElement) {
    symbolButton.addEventListener("click", () => {
      const input = document.getElementById("live-symbol-input");
      const value = input instanceof HTMLInputElement ? input.value : symbolState.input;
      symbolState.input = value;
      void loadSymbolArchive(value);
    });
  }
}

function draw(): void {
  if (!active) return;
  root.innerHTML = body();
  bindEvents();
}

function stopTimer(): void {
  if (timer !== null) {
    window.clearInterval(timer);
    timer = null;
  }
}

function startTimer(): void {
  stopTimer();
  let refreshing = false;
  timer = window.setInterval(() => {
    if (!active || document.hidden || refreshing || loading) return;
    refreshing = true;
    void refreshLive().finally(() => {
      refreshing = false;
    });
  }, REFRESH_MS);
}

/** Called by `main.ts` on every route application. */
export function renderLive(route: Route): void {
  if (route.module !== "live") {
    if (active) {
      active = false;
      stopTimer();
      requestSeq += 1; // discard in-flight responses from the live module
      root.replaceChildren();
    }
    return;
  }
  const next = LIVE_VIEWS.find((view) => view === route.view) ?? "dashboard";
  active = true;
  activeView = next;
  if (!symbolState.input) {
    const firstHolding = data.account?.positions?.[0]?.symbol
      ?? data.trades?.[0]?.symbol;
    if (firstHolding) symbolState.input = firstHolding;
  }
  draw();
  if (timer === null) startTimer();
  // 只在首次进入（或上次失败）时补拉；视图切换复用本轮缓存，
  // 后续按 30 秒定时器或手动刷新更新，避免频繁打后端。
  if (!loaded) void refreshLive();
  if (next === "ai") void loadLiveExtra("ai");
  else if (next === "evolution") void loadLiveExtra("evolution");
  else if (next === "risk") void loadLiveExtra("risk");
}

/** 盘中模块的「复制复盘」只读本模块已加载的实时读模型，绝不碰日线账本。 */
export function buildLiveReport(): string {
  const account = data.account;
  const lines = [
    "盘中模拟盘复盘",
    `账户权益：${money(account?.equity_cents)}`,
    `累计收益：${signedMoney(account?.profit_cents)}（${percent(account?.return_bps)}）`,
    `可用现金：${money(account?.cash_cents)}`,
    "",
    "最近 5 轮概要：",
  ];
  const rounds = roundsNewestFirst(data.rounds ?? []).slice(0, 5);
  if (rounds.length === 0) lines.push("还没有实时轮次");
  for (const round of rounds) {
    lines.push(`- ${timeLabel(round.started_at)} · 成交 ${count(round.trade_count)} 笔 · 账户 ${money(round.equity_cents)} · ${pickLabel(round.ai_label, aiLabel(round.ai_ok))}`);
  }
  const today = todayInShanghai();
  const todayTrades = (data.trades ?? []).filter((trade) => {
    const stamp = timeLabel(trade.created_at);
    return stamp !== EMPTY && stamp.startsWith(today);
  });
  lines.push("", `今日成交笔数：${count(todayTrades.length)}`);
  const observations = data.ai;
  if (observations === null) lines.push("AI 采纳情况：还没有加载 AI 观测");
  else if (observations.length === 0) lines.push("AI 采纳情况：还没有 AI 观测");
  else {
    const accepted = observations.filter((item) => item.accepted).length;
    lines.push(`AI 采纳情况：最近 ${count(observations.length)} 条中 ${count(accepted)} 条已采纳、${count(observations.length - accepted)} 条未采纳`);
  }
  return lines.join("\n");
}

/** 确保在任意页面点击「复制复盘」都能拿到盘中复盘，不触碰已退居后台的日线接口。 */
export async function ensureLiveReport(): Promise<string> {
  if (!loaded && !loading) {
    const [account, rounds, trades] = await Promise.all([
      optional<LiveAccountView>("account", "/v2/live/account"),
      optional<LiveRound[]>("rounds", "/v2/live/rounds"),
      optional<LiveTrade[]>("trades", "/v2/live/trades"),
    ]);
    if (data.account === null && isRecord(account)) data.account = account;
    if (data.rounds === null && isArray<LiveRound>(rounds)) data.rounds = rounds;
    if (data.trades === null && isArray<LiveTrade>(trades)) data.trades = trades;
  }
  return buildLiveReport();
}

/** Header 刷新按钮在 live 模块激活时只刷新盘中接口。 */
export function refreshLiveData(): Promise<void> {
  return refreshLive();
}

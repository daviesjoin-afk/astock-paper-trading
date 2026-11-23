import "./styles.css";
import { installRouter, NAV, parseHash, pushHash, replaceHash, type Route } from "./router";
import {
  ensureLiveReport,
  openLiveSymbolArchive,
  refreshLiveData as refreshLive,
  renderLive,
  setLiveRouteApplier,
} from "./live";

class ApiError extends Error {
  constructor(readonly status: number | null, readonly detail: string) {
    super(status === null ? "network" : `http_${status}`);
  }
}

const byId = <T extends HTMLElement>(id: string): T => {
  const node = document.getElementById(id);
  if (!node) throw new Error(`missing #${id}`);
  return node as T;
};
/** Find a node by its test id; the console now owns at most one copy of each. */
const byTestId = <T extends HTMLElement>(id: string): T => {
  const node = document.querySelector<T>(`[data-testid="${id}"]`);
  if (!node) throw new Error(`missing [data-testid="${id}"]`);
  return node;
};
const setText = (id: string, value: string): void => {
  byTestId(id).textContent = value;
};
/** Write to an element addressed by its `id` attribute rather than a test id. */
const setTextById = (id: string, value: string): void => {
  byId(id).textContent = value;
};
const show = (node: HTMLElement, visible: boolean): void => {
  node.hidden = !visible;
};
const announce = (message: string): void => {
  byId("live").textContent = message;
};

const money = (cents: number | null | undefined): string => {
  if (cents === null || cents === undefined) return "—";
  return "¥" + (cents / 100).toLocaleString("zh-CN", {
    minimumFractionDigits: 2,
    maximumFractionDigits: 2,
  });
};
const number = (value: number | null | undefined): string =>
  value === null || value === undefined ? "—" : value.toLocaleString("zh-CN");
const percent = (bps: number | null | undefined): string => {
  if (bps === null || bps === undefined) return "—";
  const value = bps / 100;
  return `${value > 0 ? "+" : ""}${value.toFixed(2)}%`;
};

const errorDetail = (error: unknown): string => {
  if (error instanceof ApiError) {
    if (error.status === null) return "无法连接本地服务";
    try {
      const body = JSON.parse(error.detail) as {
        error?: string | { code?: string; detail?: unknown };
      };
      if (typeof body.error === "string") return body.error;
      const code = body.error?.code;
      if (typeof code === "string") return code;
    } catch {
      // Preserve the raw status for unexpected responses.
    }
    return `HTTP ${error.status}`;
  }
  return error instanceof Error ? error.message : String(error);
};

async function request<T>(path: string, init?: RequestInit): Promise<T> {
  let response: Response;
  try {
    response = await fetch(path, {
      cache: "no-store",
      ...init,
      headers: { Accept: "application/json", ...(init?.headers ?? {}) },
    });
  } catch (error) {
    throw new ApiError(null, error instanceof Error ? error.message : String(error));
  }
  const text = await response.text();
  if (!response.ok) throw new ApiError(response.status, text.slice(0, 500));
  return text ? (JSON.parse(text) as T) : (undefined as T);
}

const api = <T>(path: string, init?: RequestInit): Promise<T> => request<T>(path, init);

const setBusy = (busy: boolean): void => {
  document.documentElement.dataset.busy = String(busy);
  byId("main").setAttribute("aria-busy", String(busy));
};

/** Text copied by the report-copy control: live views use the live ledger only. */
let reportText = "";

const theme = (): void => {
  const root = document.documentElement;
  const dark = root.dataset.theme === "dark";
  root.dataset.theme = dark ? "light" : "dark";
  const button = byTestId<HTMLButtonElement>("theme-toggle");
  button.textContent = dark ? "深色" : "浅色";
  button.setAttribute("aria-pressed", String(!dark));
};

// ---- navigation ------------------------------------------------------------

/**
 * Which views have been loaded at least once. A view that has never been shown
 * has no data to keep; one that has keeps its last render so flipping back is
 * instant and does not re-hit the API.
 */
const loadedViews = new Set<string>();
setLiveRouteApplier((route) => applyRoute(route));

/**
 * Views that must re-read on every visit because something outside the browser
 * can change them. Caching these showed "还没有影子候选" while the API was
 * already returning one.
 */
const ALWAYS_FRESH = new Set<string>(["research/candidates"]);

function renderModuleNav(activeModule: string): void {
  const host = byId("module-nav");
  host.replaceChildren();
  for (const module of NAV) {
    const button = document.createElement("button");
    button.type = "button";
    button.className = "module-tab" + (module.id === activeModule ? " active" : "");
    button.dataset.module = module.id;
    button.setAttribute("aria-current", module.id === activeModule ? "page" : "false");

    const label = document.createElement("b");
    label.textContent = module.label;
    const hint = document.createElement("small");
    hint.textContent = module.hint;
    button.append(label, hint);

    button.addEventListener("click", () => {
      const next = { module: module.id, view: module.views[0].id };
      pushHash(next);
      // pushState does not fire hashchange, so drive the render directly.
      applyRoute(next);
    });
    host.appendChild(button);
  }
}

function renderViewNav(route: Route): void {
  const nav = byId("view-nav");
  const module = NAV.find((entry) => entry.id === route.module);
  if (!module) {
    nav.hidden = true;
    return;
  }
  nav.hidden = false;
  nav.replaceChildren();
  for (const view of module.views) {
    const button = document.createElement("button");
    button.type = "button";
    button.className = "view-tab" + (view.id === route.view ? " active" : "");
    button.dataset.view = view.id;
    button.setAttribute("role", "tab");
    button.setAttribute("aria-selected", String(view.id === route.view));
    button.textContent = view.label;
    button.addEventListener("click", () => {
      const next = { module: route.module, view: view.id };
      pushHash(next);
      applyRoute(next);
    });
    nav.appendChild(button);
  }
}

function applyRoute(route: Route): void {
  replaceHash(route);
  renderModuleNav(route.module);
  renderViewNav(route);

  const target = `${route.module}/${route.view}`;
  for (const node of document.querySelectorAll<HTMLElement>(".view[data-view]")) {
    show(node, node.dataset.view === target);
  }
  // 盘中模块用同一个容器承载 8 个视图，仅在 live 激活时接管渲染与定时刷新。
  // renderLive() 在首次进入（!loaded）时自动调用 refreshLive()。
  if (route.module === "live") {
    show(byId("live-module"), true);
    renderLive(route);
    return;
  }
  show(byId("live-module"), false);
  renderLive(route); // 离开盘中模块时停止定时器并清空容器
  void loadView(target);
}

/** Load whatever a view needs, once per session unless explicitly refreshed. */
async function loadView(view: string): Promise<void> {
  // The symbol page is parameterised and intentionally never cached.
  // The research page reports candidate state, which `shadow_advance.sh` changes
  // outside this browser; a cached copy would claim "no candidates" long after
  // one appeared.
  if (!ALWAYS_FRESH.has(view) && loadedViews.has(view)) return;
  try {
    switch (view) {
      case "market/screener":
        await loadScreener();
        break;
      case "research/candidates":
        await loadCandidates();
        break;
      case "research/coverage":
        await loadResearchCoverage();
        break;
      case "research/evidence":
        await loadResearchEvidence();
        break;
      case "settings/ai":
      case "settings/runtime":
        await loadSettings();
        break;
      case "market/detail": {
        // Deliberately not cached: the symbol changes between visits, and a
        // cached "already loaded" would leave the previous chart on screen.
        await loadDetail(
          byId<HTMLInputElement>("detail-input").value.trim().toUpperCase(),
        );
        break;
      }
      default:
        break;
    }
    if (view === "market/detail") return; // never cached: symbol can change
    // Only mark as loaded once it actually succeeded. Marking on failure means
    // a view that raced the account load never retries, and it stays blank for
    // the rest of the session.
    loadedViews.add(view);
  } catch (error) {
    announce(`加载失败：${errorDetail(error)}`);
  }
}

// ---- research candidates ---------------------------------------------------

type CandidateRow = {
  candidate_id: string;
  algorithm_id: string;
  algorithm_version: string;
  state: string;
  created_day: string;
  updated_day: string;
  shadow_days: number;
  gate?: string;
  total_return_bps?: number;
  max_drawdown_bps?: number;
  win_rate_bps?: number;
  curve?: Array<{ day: string; equity_bps: number }>;
  history?: HistoryRow[];
};

type HistoryRow = {
  from: string;
  to: string;
  day: string;
  allowed: boolean;
  reasons: unknown;
  shortfalls: unknown;
};

/**
 * Why a candidate is not where its owner wants it.
 *
 * The gate's own shortfalls are shown verbatim rather than summarised: a
 * reader who sees "ir_bps: 1 of 300" can judge the gap, while "not eligible"
 * tells them nothing and invites a retry.
 */
function describeGate(history: HistoryRow[]): string {
  const last = history[history.length - 1];
  if (!last) return "还没有门禁记录";
  if (last.allowed) return `允许：${last.from} → ${last.to}`;
  const shortfalls = Array.isArray(last.shortfalls) ? last.shortfalls : [];
  if (shortfalls.length === 0) return `拒绝：${last.from} → ${last.to}`;
  return shortfalls
    .map((item) => {
      const row = item as { gate?: string; required?: number; actual?: number };
      return `${row.gate ?? "?"} ${row.actual ?? "?"}/${row.required ?? "?"}`;
    })
    .join("；");
}

async function loadCandidates(): Promise<void> {
  const table = byTestId<HTMLTableElement>("research-table");
  const empty = byTestId<HTMLElement>("research-empty");
  const body = byId<HTMLTableSectionElement>("research-rows");
  const rows = await api<CandidateRow[]>("/v2/research/candidates");
  const shadow = rows.filter((r) => r.state === "shadow").length;
  const promoted = rows.filter((r) => r.state === "promoted").length;
  const rejected = rows.filter((r) => r.state === "rejected").length;
  setTextById("research-count", `${rows.length} 个`);
  setTextById("research-stat-total", String(rows.length));
  setTextById("research-stat-shadow", String(shadow));
  setTextById("research-stat-promoted", String(promoted));
  setTextById("research-stat-rejected", String(rejected));

  body.replaceChildren();
  if (rows.length === 0) {
    show(table, false);
    show(empty, true);
    return;
  }
  show(empty, false);
  show(table, true);
  for (const row of rows) {
    const gate = row.gate
      ? row.gate
      : Array.isArray(row.history)
        ? describeGate(row.history)
        : "暂无门禁明细";
    const retText = row.total_return_bps !== undefined ? percent(row.total_return_bps) : "—";
    const ddText = row.max_drawdown_bps !== undefined ? percent(-Math.abs(row.max_drawdown_bps)) : "—";
    const stateLabel = row.state === "promoted" ? "已晋升" : row.state === "rejected" ? "已淘汰" : row.state === "shadow" ? "影子观察" : row.state;
    const tr = document.createElement("tr");
    const cells: Array<[string, string]> = [
      [row.candidate_id, ""],
      [`${row.algorithm_id}@${row.algorithm_version}`, ""],
      [stateLabel, ""],
      [String(row.shadow_days), ""],
      [retText, `num-col ${row.total_return_bps !== undefined ? (row.total_return_bps >= 0 ? "pos" : "neg") : ""}`],
      [ddText, "num-col"],
      [gate, "live-note"],
    ];
    for (const [text, cls] of cells) {
      const cell = document.createElement("td");
      cell.textContent = text;
      if (cls) cell.className = cls;
      tr.appendChild(cell);
    }
    body.appendChild(tr);
  }
  renderResearchCurves(rows);
  announce(`候选 ${rows.length} 个`);
}

function renderResearchCurves(rows: CandidateRow[]): void {
  const panel = byId("research-curve-panel");
  const host = byId("research-curve");
  const withCurve = rows.filter((r) => Array.isArray(r.curve) && r.curve.length >= 2);
  if (withCurve.length === 0) {
    host.replaceChildren();
    show(panel, false);
    return;
  }
  show(panel, true);
  const colors = ["var(--accent, #1f5bc9)", "var(--positive, #137a4a)", "var(--aggressive, #9a4c16)"];
  setTextById(
    "research-curve-legend",
    withCurve.map((r, i) => `${i + 1}. ${r.algorithm_id}`).join(" · "),
  );
  const allVals = withCurve.flatMap((r) => (r.curve ?? []).map((p) => p.equity_bps));
  const min = Math.min(...allVals);
  const max = Math.max(...allVals);
  const span = Math.max(1, max - min);
  const width = 720;
  const height = 210;
  const padX = 12;
  const padY = 14;
  const ns = "http://www.w3.org/2000/svg";
  const svg = document.createElementNS(ns, "svg");
  svg.setAttribute("class", "curve");
  svg.setAttribute("viewBox", `0 0 ${width} ${height}`);
  svg.setAttribute("preserveAspectRatio", "none");
  svg.setAttribute("role", "img");
  svg.setAttribute("aria-label", "候选策略净值对比曲线");

  for (const frac of [0.25, 0.5, 0.75]) {
    const gy = (padY + (height - padY * 2) * frac).toFixed(1);
    const guide = document.createElementNS(ns, "line");
    guide.setAttribute("class", "curve-baseline");
    guide.setAttribute("x1", String(padX));
    guide.setAttribute("y1", gy);
    guide.setAttribute("x2", String(width - padX));
    guide.setAttribute("y2", gy);
    svg.appendChild(guide);
  }
  withCurve.forEach((cand, cIdx) => {
    const pts = cand.curve ?? [];
    const d = pts
      .map((pt, i) => {
        const px = padX + ((width - padX * 2) * i) / Math.max(pts.length - 1, 1);
        const py = padY + (height - padY * 2) * (1 - (pt.equity_bps - min) / span);
        return `${i === 0 ? "M" : "L"}${px.toFixed(1)},${py.toFixed(1)}`;
      })
      .join(" ");
    const path = document.createElementNS(ns, "path");
    path.setAttribute("d", d);
    path.setAttribute("fill", "none");
    path.setAttribute("stroke", colors[cIdx % colors.length]);
    path.setAttribute("stroke-width", "2");
    path.setAttribute("stroke-linejoin", "round");
    svg.appendChild(path);
  });
  const scale = document.createElement("div");
  scale.className = "live-scale";
  const firstDay = withCurve[0].curve?.[0]?.day ?? "";
  const lastDay = withCurve[0].curve?.[withCurve[0].curve.length - 1]?.day ?? "";
  scale.innerHTML = `<span>${firstDay} · 最低 ${(min / 10_000).toFixed(3)}</span><span>最高 ${(max / 10_000).toFixed(3)} · ${lastDay}</span>`;
  host.replaceChildren(svg, scale);
}

// ---- settings (AI provider & long-running daemon) ---------------------------

type SettingsView = {
  ai_provider: string;
  ai_base_url: string;
  ai_model: string;
  has_api_key: boolean;
  ai_api_key_masked: string;
  ai_enabled: boolean;
  ai_depth: "deep" | "advisory";
  ai_timeout_secs: number;
  live_interval_secs: number;
  live_auto_evolve: boolean;
  live_daemon_enabled: boolean;
  live_universe_path: string;
  updated_at: string;
  runtime: {
    daemon_enabled: boolean;
    running: boolean;
    os: string;
    phase: string;
    last_heartbeat: string | null;
  };
};

function applySettingsView(s: SettingsView): void {
  byId<HTMLInputElement>("set-ai-base").value = s.ai_base_url || "";
  byId<HTMLInputElement>("set-ai-model").value = s.ai_model || "";
  const keyInput = byId<HTMLInputElement>("set-ai-key");
  keyInput.value = "";
  keyInput.placeholder = s.has_api_key
    ? `已保存 (${s.ai_api_key_masked})，留空则保持不变`
    : "sk-...";
  byId<HTMLInputElement>("set-ai-timeout").value = String(s.ai_timeout_secs || 25);
  byId<HTMLSelectElement>("set-ai-enabled").value = s.ai_enabled ? "true" : "false";
  byId<HTMLSelectElement>("set-ai-depth").value = s.ai_depth === "advisory" ? "advisory" : "deep";
  setTextById("settings-ai-updated", s.updated_at ? `上次保存 ${s.updated_at.slice(0, 19).replace("T", " ")}` : "默认配置");
  for (const btn of byId("settings-provider-presets").querySelectorAll("button")) {
    btn.classList.toggle("active", btn.dataset.provider === s.ai_provider);
  }

  byId<HTMLSelectElement>("set-rt-daemon").value = s.live_daemon_enabled ? "true" : "false";
  byId<HTMLInputElement>("set-rt-interval").value = String(s.live_interval_secs || 180);
  byId<HTMLSelectElement>("set-rt-evolve").value = s.live_auto_evolve ? "true" : "false";
  byId<HTMLInputElement>("set-rt-universe").value = s.live_universe_path || "";
  setTextById("settings-rt-os", s.runtime.os);
  setTextById("rt-phase", s.runtime.phase);
  setTextById("rt-heartbeat", s.runtime.last_heartbeat ? s.runtime.last_heartbeat.slice(0, 19).replace("T", " ") : "—");
  setTextById("rt-os", s.runtime.os);
  setTextById("rt-ai-ready", s.ai_enabled && s.has_api_key ? `已就绪 (${s.ai_model})` : s.ai_enabled ? "未填 API Key" : "已关闭");
}

async function loadSettings(): Promise<void> {
  const s = await api<SettingsView>("/v2/settings");
  applySettingsView(s);
}

// ---- research coverage & evidence ------------------------------------------

type CoverageInfo = {
  price_set_id: string | null;
  market_data_pack_id: string | null;
  coverage_start: string | null;
  coverage_end: string | null;
  days: number;
  symbols: number;
  last_settled_day: string | null;
};

type RegimeInfo = {
  day: string;
  regime: string;
  label: string;
  index_points: number;
  fast_average_cents: number;
  slow_average_cents: number;
  separation_bps: number;
  breadth_bps: number | null;
};

type ObservationInfo = {
  day: string;
  regime: string;
  regime_separation_bps: number;
  regime_breadth_bps: number | null;
  candidates: Array<{
    slot: string;
    symbol: string;
    score: number;
    reason?: string;
  }>;
};

function regimeLabelText(code: string): string {
  switch (code) {
    case "trending": return "趋势偏强";
    case "choppy": return "震荡分化";
    case "declining": return "偏弱下行";
    default: return code || "未知";
  }
}

async function loadResearchCoverage(): Promise<void> {
  const [daysRes, covRes, gapsRes] = await Promise.allSettled([
    api<{ days: string[]; count: number; latest_day: string | null; latest_symbol_count: number }>("/v2/market/days"),
    api<CoverageInfo>("/v2/prices/coverage"),
    api<{ day: string; count: number; bars: Bar[] }>("/v2/market/gaps"),
  ]);

  let barStart: string | null = null;
  let barLatest: string | null = null;
  if (daysRes.status === "fulfilled") {
    const d = daysRes.value;
    barStart = d.days.length > 0 ? d.days[0] : null;
    barLatest = d.latest_day;
    setTextById("coverage-latest", d.latest_day ? `最新日 ${d.latest_day}` : "暂无数据");
    setTextById("cov-bar-days", `${d.count} 天`);
    setTextById("cov-bar-symbols", `${number(d.latest_symbol_count)} 只`);
  } else {
    setTextById("coverage-latest", "读取失败");
  }

  const note = byId("coverage-note");
  if (covRes.status === "fulfilled") {
    const c = covRes.value;
    const range = c.coverage_start && c.coverage_end
      ? `${c.coverage_start} ~ ${c.coverage_end}（${c.days} 日 / ${c.symbols} 只）`
      : barStart && barLatest
        ? `${barStart} ~ ${barLatest}（全市场日线库）`
        : "未加载已审核价格集";
    setTextById("cov-set-range", range);
    setTextById("cov-settled-day", c.last_settled_day ?? barLatest ?? "—");
    if (c.price_set_id) {
      note.textContent = `当前价格集 ID：${c.price_set_id}${c.market_data_pack_id ? ` · 数据包：${c.market_data_pack_id}` : ""}`;
      show(note, true);
    } else {
      show(note, false);
    }
  }

  const body = byId<HTMLTableSectionElement>("gaps-body");
  const empty = byId("gaps-empty");
  body.replaceChildren();
  if (gapsRes.status === "fulfilled" && gapsRes.value.bars.length > 0) {
    setTextById("gaps-count", `${gapsRes.value.day} · ${gapsRes.value.count} 只`);
    show(empty, false);
    for (const bar of gapsRes.value.bars) {
      const tr = document.createElement("tr");
      const gapBps = bar.prev_close_cents > 0
        ? ((bar.close_cents - bar.prev_close_cents) * 10_000) / bar.prev_close_cents
        : 0;
      const cells: Array<[string, string]> = [
        [bar.symbol, ""],
        [bar.name || "—", ""],
        [money(bar.prev_close_cents), "num-col"],
        [money(bar.close_cents), "num-col"],
        [percent(gapBps), `num-col ${gapBps >= 0 ? "pos" : "neg"}`],
        [number(Math.round(bar.volume_shares / 100)), "num-col"],
      ];
      for (const [text, cls] of cells) {
        const td = document.createElement("td");
        td.textContent = text;
        if (cls) td.className = cls;
        tr.appendChild(td);
      }
      tr.tabIndex = 0;
      tr.dataset.symbol = bar.symbol;
      tr.addEventListener("click", () => openDetail(bar.symbol));
      tr.addEventListener("keydown", (event) => {
        if (event.key === "Enter") openDetail(bar.symbol);
      });
      body.appendChild(tr);
    }
  } else {
    setTextById("gaps-count", "0 只");
    show(empty, true);
  }
}

async function loadResearchEvidence(): Promise<void> {
  const [regimeRes, obsRes, evoRes, barsRes] = await Promise.allSettled([
    api<RegimeInfo>("/v2/market/regime"),
    api<ObservationInfo>("/v2/market/observation"),
    api<{ proposals?: Array<Record<string, unknown>> } | Array<Record<string, unknown>>>("/v2/live/evolution"),
    api<{ bars: Bar[] }>("/v2/market/bars"),
  ]);

  const barBySymbol = new Map<string, Bar>();
  if (barsRes.status === "fulfilled") {
    for (const bar of barsRes.value.bars) barBySymbol.set(bar.symbol, bar);
  }

  if (regimeRes.status === "fulfilled") {
    const r = regimeRes.value;
    setTextById("evidence-day", r.day);
    setTextById("ev-regime", r.label || regimeLabelText(r.regime));
    setTextById("ev-index", number(Math.round(r.index_points)));
    setTextById("ev-sep", percent(r.separation_bps));
    setTextById("ev-breadth", r.breadth_bps !== null ? `${(r.breadth_bps / 100).toFixed(1)}%` : "—");
  } else {
    setTextById("evidence-day", "暂无数据");
    for (const id of ["ev-regime", "ev-index", "ev-sep", "ev-breadth"]) setTextById(id, "—");
  }

  const candBody = byId<HTMLTableSectionElement>("evidence-candidates-body");
  const candEmpty = byId("evidence-empty");
  candBody.replaceChildren();
  if (obsRes.status === "fulfilled" && Array.isArray(obsRes.value.candidates) && obsRes.value.candidates.length > 0) {
    show(candEmpty, false);
    for (const item of obsRes.value.candidates) {
      const tr = document.createElement("tr");
      const slotLabel = item.slot === "steady" ? "稳健" : item.slot === "aggressive" ? "激进" : item.slot;
      const bar = barBySymbol.get(item.symbol);
      const symLabel = bar && bar.name ? `${bar.name} ${item.symbol}` : item.symbol;
      const priceText = bar
        ? `${money(bar.close_cents)} (${percent(barChangeBps(bar))})`
        : "—";
      const reasonRaw = item.reason || "—";
      const shortHist = reasonRaw.endsWith("_short_history");
      const baseReason = shortHist ? reasonRaw.replace(/_short_history$/, "") : reasonRaw;
      const reasonTable: Record<string, string> = {
        trend_and_low_volatility: "均线趋势向上 · 低波动确认",
        trend_flattening: "均线趋势走平",
        trend_broken: "均线趋势走弱",
        fresh_breakout: "突破 20 日高点 · 温和动量",
        momentum_stalling: "短线动量放缓",
        momentum_lost: "短线动量回落",
        no_edge: "区间特征中性（无显著超额边际）",
      };
      const reasonLabel = (reasonTable[baseReason] ?? baseReason) + (shortHist ? "（短周期样本）" : "");
      const cells: Array<[string, string]> = [
        [slotLabel, ""],
        [symLabel, ""],
        [String(item.score), `num-col ${item.score >= 0 ? "pos" : "neg"}`],
        [priceText, `num-col ${bar ? (barChangeBps(bar) >= 0 ? "pos" : "neg") : ""}`],
        [reasonLabel, "live-note"],
      ];
      for (const [text, cls] of cells) {
        const td = document.createElement("td");
        td.textContent = text;
        if (cls) td.className = cls;
        tr.appendChild(td);
      }
      tr.tabIndex = 0;
      tr.dataset.symbol = item.symbol;
      tr.addEventListener("click", () => openDetail(item.symbol));
      tr.addEventListener("keydown", (event) => {
        if (event.key === "Enter") openDetail(item.symbol);
      });
      candBody.appendChild(tr);
    }
  } else {
    show(candEmpty, true);
  }

  const evoBody = byId<HTMLTableSectionElement>("evidence-evo-body");
  const evoEmpty = byId("evidence-evo-empty");
  evoBody.replaceChildren();
  const proposals = evoRes.status === "fulfilled"
    ? Array.isArray(evoRes.value)
      ? evoRes.value
      : Array.isArray(evoRes.value.proposals)
        ? evoRes.value.proposals
        : []
    : [];
  setTextById("evidence-evo-count", `${proposals.length} 条`);
  if (proposals.length === 0) {
    show(evoEmpty, true);
  } else {
    show(evoEmpty, false);
    for (const p of proposals.slice(0, 20)) {
      const tr = document.createElement("tr");
      const day = String(p.day ?? p.created_at ?? "—");
      const accepted = p.accepted === true ? "已采纳" : "仅记录（未启用）";
      const prop = (p.proposal && typeof p.proposal === "object" ? p.proposal : null) as Record<string, unknown> | null;
      const paramText = prop
        ? `入场 ${Number(prop.entry_delta ?? 0) >= 0 ? "+" : ""}${prop.entry_delta ?? 0} · 权重 ${prop.weight_tilt_bps ?? 0} bps`
        : "无调整";
      const before = (p.before_metrics && typeof p.before_metrics === "object" ? p.before_metrics : null) as Record<string, unknown> | null;
      const metricsText = before
        ? `收益 ${percent(Number(before.profit_bps ?? 0))} / 回撤 ${percent(Number(before.max_drawdown_bps ?? 0))}`
        : "—";
      const note = String(p.reason ?? "—");
      for (const text of [day, accepted, paramText, metricsText, note]) {
        const td = document.createElement("td");
        td.textContent = text;
        tr.appendChild(td);
      }
      evoBody.appendChild(tr);
    }
  }
}

// ---- market screener -------------------------------------------------------

type Bar = {
  symbol: string;
  day: string;
  name: string;
  close_cents: number;
  prev_close_cents: number;
  volume_shares: number;
};

type ScreenerFilter = "all" | "up" | "down" | "strong" | "weak";
type ScreenerSort = "symbol" | "name" | "close" | "change" | "volume";

let screenerRows: Bar[] = [];
let screenerFilter: ScreenerFilter = "all";
let screenerSort: ScreenerSort = "change";
let screenerAsc = false;
let screenerPage = 0;
const SCREENER_PAGE_SIZE = 100;

/**
 * Request generation for the day slice of the screener.
 *
 * Switching the trading day twice quickly puts two requests in flight and the
 * browser makes no promise about which resolves first. Without a generation
 * the late answer of the *first* day overwrites the rows and the summary of
 * the day the user is actually looking at. Every read increments this token;
 * only the token that is still current may write back.
 */
let screenerGeneration = 0;

const barChangeBps = (bar: Bar): number =>
  bar.prev_close_cents > 0
    ? ((bar.close_cents - bar.prev_close_cents) * 10_000) / bar.prev_close_cents
    : 0;

async function loadScreener(): Promise<void> {
  const generation = ++screenerGeneration;
  const days = await api<{ days: string[]; latest_day: string | null }>("/v2/market/days");
  // The day list arrived after the user already picked a day (or another
  // refresh started): rebuilding the picker here would move the selection
  // back and re-render rows the user did not ask for.
  if (generation !== screenerGeneration) return;
  const select = byId<HTMLSelectElement>("screener-day");
  select.replaceChildren();
  for (const day of [...days.days].reverse()) {
    const option = document.createElement("option");
    option.value = day;
    option.textContent = day;
    select.appendChild(option);
  }
  if (!days.latest_day) {
    byId("screener-summary").replaceChildren();
    show(byId("screener-empty"), true);
    return;
  }
  select.value = days.latest_day;
  await fetchScreenerDay(days.latest_day, generation);
}

/**
 * Load one trading day into the screener.
 *
 * `generation` is the token captured by whoever started this read. A response
 * that is no longer the newest one writes nothing at all: not the rows, not
 * the summary tiles, not the count. That is what keeps a slow answer for
 * Monday from repainting Tuesday.
 */
async function fetchScreenerDay(day: string, generation: number): Promise<void> {
  const [barsPayload, regimeRes] = await Promise.all([
    api<{ bars: Bar[]; count: number }>(`/v2/market/bars?day=${encodeURIComponent(day)}`),
    api<RegimeInfo>(`/v2/market/regime?day=${encodeURIComponent(day)}`).catch(() => null),
  ]);
  if (generation !== screenerGeneration) return;
  screenerRows = barsPayload.bars.slice();
  screenerPage = 0;
  renderScreenerSummary(day, screenerRows, regimeRes);
  renderScreener();
}

/** The day picker starts a new generation, so any in-flight day is now stale. */
function selectScreenerDay(day: string): void {
  const generation = ++screenerGeneration;
  void fetchScreenerDay(day, generation).catch((error: unknown) => {
    if (generation !== screenerGeneration) return;
    announce(`加载失败：${errorDetail(error)}`);
  });
}

function renderScreenerSummary(day: string, bars: Bar[], regime: RegimeInfo | null): void {
  const host = byId("screener-summary");
  host.replaceChildren();
  if (bars.length === 0) return;
  let up = 0;
  let down = 0;
  let flat = 0;
  let strong = 0;
  for (const bar of bars) {
    const bps = barChangeBps(bar);
    if (bps > 0) up += 1;
    else if (bps < 0) down += 1;
    else flat += 1;
    if (bps >= 500) strong += 1;
  }
  const tiles: Array<[string, string, string?]> = [
    ["交易日", day],
    ["上涨 / 平 / 下跌", `${up} / ${flat} / ${down}`],
    ["强势 (≥5%)", `${strong} 只`, "pos"],
    ["市场环境", regime ? (regime.label || regimeLabelText(regime.regime)) : "—"],
    ["上涨宽度", regime && regime.breadth_bps !== null ? `${(regime.breadth_bps / 100).toFixed(1)}%` : "—"],
  ];
  for (const [label, value, cls] of tiles) {
    const div = document.createElement("div");
    div.className = "summary-tile";
    const span = document.createElement("span");
    span.textContent = label;
    const strongEl = document.createElement("strong");
    strongEl.textContent = value;
    if (cls) strongEl.className = cls;
    div.append(span, strongEl);
    host.appendChild(div);
  }
}

function updateScreenerSortHeaders(): void {
  document.querySelectorAll<HTMLButtonElement>(".sort-btn").forEach((btn) => {
    const key = btn.dataset.sort as ScreenerSort | undefined;
    const base = { symbol: "代码", name: "名称", close: "收盘", change: "涨跌", volume: "成交量" }[key ?? "change"] ?? "";
    const active = key === screenerSort;
    btn.classList.toggle("active", active);
    btn.textContent = active ? `${base} ${screenerAsc ? "↑" : "↓"}` : base;
  });
}

function renderScreener(): void {
  updateScreenerSortHeaders();
  const filter = byId<HTMLInputElement>("screener-filter").value.trim().toUpperCase();
  const body = byId<HTMLTableSectionElement>("screener-body");
  body.replaceChildren();
  let rows = screenerRows.filter((row) => {
    if (filter && !row.symbol.toUpperCase().includes(filter) && !row.name.includes(filter)) {
      return false;
    }
    const bps = barChangeBps(row);
    if (screenerFilter === "up") return bps > 0;
    if (screenerFilter === "down") return bps < 0;
    if (screenerFilter === "strong") return bps >= 500;
    if (screenerFilter === "weak") return bps <= -500;
    return true;
  });

  rows = rows.slice().sort((a, b) => {
    let cmp = 0;
    if (screenerSort === "symbol") cmp = a.symbol.localeCompare(b.symbol);
    else if (screenerSort === "name") cmp = a.name.localeCompare(b.name, "zh-CN");
    else if (screenerSort === "close") cmp = a.close_cents - b.close_cents;
    else if (screenerSort === "volume") cmp = a.volume_shares - b.volume_shares;
    else cmp = barChangeBps(a) - barChangeBps(b);
    return screenerAsc ? cmp : -cmp;
  });

  const pages = Math.max(1, Math.ceil(rows.length / SCREENER_PAGE_SIZE));
  if (screenerPage >= pages) screenerPage = pages - 1;
  const start = screenerPage * SCREENER_PAGE_SIZE;
  const shown = rows.slice(start, start + SCREENER_PAGE_SIZE);

  for (const row of shown) {
    const tr = document.createElement("tr");
    const change = barChangeBps(row);
    const cells: Array<[string, string]> = [
      [row.symbol, ""],
      [row.name, ""],
      [money(row.close_cents), "num-col"],
      [percent(change), "num-col " + (change >= 0 ? "pos" : "neg")],
      [number(Math.round(row.volume_shares / 100)), "num-col"],
    ];
    for (const [text, cls] of cells) {
      const td = document.createElement("td");
      td.textContent = text;
      if (cls) td.className = cls;
      tr.appendChild(td);
    }
    tr.tabIndex = 0;
    tr.dataset.symbol = row.symbol;
    tr.addEventListener("click", () => openDetail(row.symbol));
    tr.addEventListener("keydown", (event) => {
      if (event.key === "Enter") openDetail(row.symbol);
    });
    body.appendChild(tr);
  }

  const pager = byId("screener-pager");
  pager.replaceChildren();
  if (rows.length > SCREENER_PAGE_SIZE) {
    show(pager, true);
    const prev = document.createElement("button");
    prev.type = "button";
    prev.className = "live-page";
    prev.textContent = "上一页";
    prev.disabled = screenerPage === 0;
    prev.addEventListener("click", () => {
      screenerPage = Math.max(0, screenerPage - 1);
      renderScreener();
    });
    const info = document.createElement("span");
    info.className = "live-dim";
    info.textContent = `第 ${screenerPage + 1} / ${pages} 页（共 ${rows.length} 只）`;
    const next = document.createElement("button");
    next.type = "button";
    next.className = "live-page";
    next.textContent = "下一页";
    next.disabled = screenerPage >= pages - 1;
    next.addEventListener("click", () => {
      screenerPage = Math.min(pages - 1, screenerPage + 1);
      renderScreener();
    });
    pager.append(prev, info, next);
  } else {
    show(pager, false);
  }
  setTextById("screener-count", rows.length > 0 ? `${start + 1}–${start + shown.length} / ${rows.length} 只` : "0 只");
  show(byId("screener-empty"), rows.length === 0);
}

// ---- wiring ----------------------------------------------------------------

/** The report-copy control always copies the live intraday ledger and never
 *  hits the retired daily account endpoint. */
async function loadReport(): Promise<void> {
  reportText = await ensureLiveReport();
  announce("复盘已更新");
}

byTestId("refresh").addEventListener("click", () => {
  const route = parseHash(location.hash);
  if (route.module === "live") {
    void refreshLive(); // 盘中模块的刷新只调 live API，不触碰日线账户
    return;
  }
  loadedViews.clear();
  setBusy(true);
  void Promise.allSettled([
    loadView(`${route.module}/${route.view}`),
  ]).then(() => setBusy(false));
});
byId<HTMLSelectElement>("screener-day").addEventListener("change", (event) => {
  const target = event.currentTarget;
  if (target instanceof HTMLSelectElement && target.value) {
    selectScreenerDay(target.value);
  }
});
byId<HTMLInputElement>("screener-filter").addEventListener("input", () => {
  screenerPage = 0;
  renderScreener();
});
byId("screener-chips").addEventListener("click", (event) => {
  const target = event.target;
  if (!(target instanceof HTMLButtonElement)) return;
  const f = target.dataset.filter as ScreenerFilter | undefined;
  if (!f) return;
  screenerFilter = f;
  screenerPage = 0;
  for (const btn of byId("screener-chips").querySelectorAll("button")) {
    btn.classList.toggle("active", btn === target);
  }
  renderScreener();
});
document.querySelectorAll<HTMLButtonElement>(".sort-btn").forEach((btn) => {
  btn.addEventListener("click", () => {
    const key = btn.dataset.sort as ScreenerSort | undefined;
    if (!key) return;
    if (screenerSort === key) screenerAsc = !screenerAsc;
    else {
      screenerSort = key;
      screenerAsc = key === "symbol" || key === "name";
    }
    screenerPage = 0;
    renderScreener();
  });
});
const reportCopy = document.getElementById("report-copy");
if (reportCopy) {
  reportCopy.addEventListener("click", async () => {
    await loadReport();
    if (!reportText) {
      reportCopy.textContent = "暂无复盘";
      setTimeout(() => {
        reportCopy.textContent = "复制复盘";
      }, 1500);
      return;
    }
    try {
      await navigator.clipboard.writeText(reportText);
      reportCopy.textContent = "已复制";
      setTimeout(() => {
        reportCopy.textContent = "复制复盘";
      }, 1500);
    } catch {
      reportCopy.textContent = "复制失败";
      setTimeout(() => {
        reportCopy.textContent = "复制复盘";
      }, 1500);
    }
  });
}
byId("detail-load").addEventListener("click", () => {
  const symbol = byId<HTMLInputElement>("detail-input").value.trim().toUpperCase();
  void loadDetail(symbol);
});
byId<HTMLInputElement>("detail-input").addEventListener("keydown", (event) => {
  if (event.key === "Enter") {
    const symbol = byId<HTMLInputElement>("detail-input").value.trim().toUpperCase();
    void loadDetail(symbol);
  }
});
byId("detail-ranges").addEventListener("click", (event) => {
  const target = event.target;
  if (!(target instanceof HTMLButtonElement)) return;
  const range = target.dataset.range as DetailRange | undefined;
  if (!range) return;
  detailRange = range;
  for (const btn of byId("detail-ranges").querySelectorAll("button")) {
    btn.classList.toggle("active", btn === target);
  }
  renderDetailView();
});
byId("detail-to-live").addEventListener("click", () => {
  const symbol = detailSymbol;
  if (!symbol) return;
  const next: Route = { module: "live", view: "symbol" };
  pushHash(next);
  applyRoute(next);
  // Set the target state directly instead of pressing the live view's button
  // on a timer: `applyRoute` has just rendered that view, and a programmatic
  // click on a *disabled* button (the view is loading its own refresh) is
  // silently dropped, leaving the symbol page empty while the URL says
  // otherwise.
  openLiveSymbolArchive(symbol);
});
byId("research-run").addEventListener("click", async () => {
  const btn = byId<HTMLButtonElement>("research-run");
  btn.disabled = true;
  btn.textContent = "评估中…";
  try {
    await api("/v2/research/run", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: "{}",
    });
    await loadCandidates();
  } catch (error) {
    announce(`研究评估失败：${errorDetail(error)}`);
  } finally {
    btn.disabled = false;
    btn.textContent = "运行研究评估";
  }
});

let selectedProvider = "deepseek";
byId("settings-provider-presets").addEventListener("click", (event) => {
  const target = event.target;
  if (!(target instanceof HTMLButtonElement)) return;
  const provider = target.dataset.provider;
  if (!provider) return;
  selectedProvider = provider;
  for (const btn of byId("settings-provider-presets").querySelectorAll("button")) {
    btn.classList.toggle("active", btn === target);
  }
  if (target.dataset.url) byId<HTMLInputElement>("set-ai-base").value = target.dataset.url;
  if (target.dataset.model) byId<HTMLInputElement>("set-ai-model").value = target.dataset.model;
});

byId("settings-ai-save").addEventListener("click", async () => {
  const feedback = byId("settings-ai-feedback");
  feedback.textContent = "正在保存…";
  try {
    const updated = await api<SettingsView>("/v2/settings", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        ai_provider: selectedProvider,
        ai_base_url: byId<HTMLInputElement>("set-ai-base").value.trim(),
        ai_model: byId<HTMLInputElement>("set-ai-model").value.trim(),
        ai_api_key: byId<HTMLInputElement>("set-ai-key").value.trim() || undefined,
        ai_timeout_secs: Number(byId<HTMLInputElement>("set-ai-timeout").value || 25),
        ai_enabled: byId<HTMLSelectElement>("set-ai-enabled").value === "true",
        ai_depth: byId<HTMLSelectElement>("set-ai-depth").value,
      }),
    });
    applySettingsView(updated);
    feedback.textContent = "已保存 AI 配置";
  } catch (error) {
    feedback.textContent = `保存失败：${errorDetail(error)}`;
  }
});

byId("settings-ai-test").addEventListener("click", async () => {
  const feedback = byId("settings-ai-feedback");
  feedback.textContent = "正在测试大模型连通性…";
  try {
    const res = await api<{ ok: boolean; latency_ms?: number; model?: string; reply?: string; error?: string }>(
      "/v2/settings/test-ai",
      {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          ai_base_url: byId<HTMLInputElement>("set-ai-base").value.trim(),
          ai_model: byId<HTMLInputElement>("set-ai-model").value.trim(),
          ai_api_key: byId<HTMLInputElement>("set-ai-key").value.trim() || undefined,
          ai_timeout_secs: Number(byId<HTMLInputElement>("set-ai-timeout").value || 25),
        }),
      },
    );
    feedback.textContent = res.ok
      ? `连通成功 (${res.latency_ms ?? 0}ms · ${res.model ?? ""} · "${res.reply ?? "OK"}")`
      : `连通失败：${res.error ?? "未知错误"}`;
  } catch (error) {
    feedback.textContent = `测试失败：${errorDetail(error)}`;
  }
});

byId("settings-rt-save").addEventListener("click", async () => {
  const feedback = byId("settings-rt-feedback");
  feedback.textContent = "正在保存…";
  try {
    const updated = await api<SettingsView>("/v2/settings", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        live_daemon_enabled: byId<HTMLSelectElement>("set-rt-daemon").value === "true",
        live_interval_secs: Number(byId<HTMLInputElement>("set-rt-interval").value || 180),
        live_auto_evolve: byId<HTMLSelectElement>("set-rt-evolve").value === "true",
        live_universe_path: byId<HTMLInputElement>("set-rt-universe").value.trim(),
      }),
    });
    applySettingsView(updated);
    feedback.textContent = "已保存长期运行设置";
  } catch (error) {
    feedback.textContent = `保存失败：${errorDetail(error)}`;
  }
});

byTestId("theme-toggle").addEventListener("click", theme);

function openDetail(symbol: string): void {
  byId<HTMLInputElement>("detail-input").value = symbol;
  // Navigation in this app pushes the hash and then applies the route
  // explicitly: `history.pushState` does not fire `hashchange`, so relying on
  // the router callback would leave the old view rendered.
  loadedViews.delete("market/detail");
  const next: Route = { module: "market", view: "detail" };
  pushHash(next);
  applyRoute(next);
}

type DetailRange = "20" | "60" | "120" | "all";
let detailAllRows: Bar[] = [];
let detailSymbol = "";
let detailRange: DetailRange = "all";
/**
 * Request generation for the (parameterised, never cached) symbol page.
 *
 * Looking up two codes in a row leaves two reads in flight. Without a
 * generation the slower first answer either repaints the chart with the old
 * symbol or, worse, lands after the new title is already on screen and empties
 * the new symbol's panels. Only the newest read may touch the DOM.
 */
let detailGeneration = 0;

/** Which symbol the panels currently on screen were drawn for (null = none). */
let detailRenderedSymbol: string | null = null;

function clearDetailPanels(): void {
  byId("detail-chart").replaceChildren();
  byId("detail-volume").replaceChildren();
  byId("detail-gaps").replaceChildren();
  byId("detail-hover").textContent = "";
  show(byId("detail-volume"), false);
  show(byId("detail-gaps"), false);
  show(byId("detail-hover"), false);
  show(byId("detail-to-live"), false);
  show(byId("detail-history-panel"), false);
}

async function loadDetail(symbol: string): Promise<void> {
  const empty = byTestId("detail-empty");
  // Every call — including the "cleared input" branch — starts a new
  // generation, so an answer for the previous symbol can never be applied on
  // top of the state this call is about to establish.
  const generation = ++detailGeneration;
  if (!symbol) {
    detailAllRows = [];
    detailSymbol = "";
    detailRenderedSymbol = null;
    clearDetailPanels();
    setText("detail-symbol", "—");
    for (const id of ["detail-close", "detail-change", "detail-range", "detail-days"]) setText(id, "—");
    byTestId("detail-change").className = "";
    empty.textContent = "输入代码查看个股走势。";
    show(empty, true);
    return;
  }
  detailSymbol = symbol;
  // Switching symbols must not leave the *previous* chart standing under the
  // new title (the exact complaint this fixes), so the panels are cleared when
  // the rendered symbol differs. A re-read of the same symbol keeps its chart
  // until the new numbers arrive instead of flashing empty.
  if (detailRenderedSymbol !== symbol) {
    detailAllRows = [];
    clearDetailPanels();
    // Nothing is drawn any more, so the next read of the same symbol must
    // clear again rather than assume the (now empty) panels still match.
    detailRenderedSymbol = null;
    setText("detail-close", "—");
    setText("detail-change", "—");
    byTestId("detail-change").className = "";
    setText("detail-range", "—");
    setText("detail-days", "—");
    empty.textContent = `正在读取 ${symbol} 的行情…`;
    show(empty, true);
  }
  // The title changes with the request, never with the response: the reader
  // asked for this code. Everything else waits behind the generation guard.
  setText("detail-symbol", symbol);
  let rows: Bar[];
  try {
    const payload = await api<{ bars: Bar[] }>(
      `/v2/market/bars/${encodeURIComponent(symbol)}`,
    );
    if (generation !== detailGeneration) return;
    rows = payload.bars;
  } catch (error) {
    // A slower failure must not clobber the newer symbol the reader switched
    // to while this request was in flight.
    if (generation !== detailGeneration) return;
    // A 404 here is a normal answer ("that code is not ingested"), not a
    // service failure: say which code is missing rather than "加载失败".
    const missing = error instanceof ApiError && error.status === 404;
    detailAllRows = [];
    detailRenderedSymbol = symbol;
    clearDetailPanels();
    setText("detail-close", "—");
    setText("detail-change", "—");
    byTestId("detail-change").className = "";
    setText("detail-range", "—");
    setText("detail-days", "0");
    empty.textContent = missing ? `没有 ${symbol} 的入库行情。` : `加载失败：${errorDetail(error)}`;
    show(empty, true);
    return;
  }
  detailAllRows = rows;
  detailRenderedSymbol = symbol;
  show(empty, rows.length === 0);
  if (rows.length === 0) {
    clearDetailPanels();
    // Never leave the in-flight placeholder standing as the final answer.
    empty.textContent = `${symbol} 没有可绘制的日线。`;
    return;
  }
  empty.textContent = "输入代码查看个股走势。";
  show(byId("detail-to-live"), true);
  renderDetailView();
}

function renderDetailView(): void {
  const chart = byId("detail-chart");
  if (detailAllRows.length === 0) return;
  const count = detailRange === "all" ? detailAllRows.length : Number(detailRange);
  const rows = detailAllRows.slice(-count);

  const closes = rows.map((row) => row.close_cents);
  const last = closes[closes.length - 1];
  const first = closes[0];
  const high = Math.max(...closes);
  const low = Math.min(...closes);
  const changeBps = first > 0 ? ((last - first) * 10_000) / first : null;
  setText("detail-close", money(last));
  const changeEl = byTestId("detail-change");
  changeEl.textContent = changeBps !== null ? percent(changeBps) : "—";
  changeEl.className = changeBps === null || changeBps === 0 ? "" : changeBps > 0 ? "pos" : "neg";
  setText("detail-range", `${money(high)} / ${money(low)}`);
  setText("detail-days", String(rows.length));

  // Gaps are drawn as a break, never a solid bar: an ex-dividend or split
  // session is not a real move and must not look like one.
  const gaps = new Map<string, number>();
  for (let index = 1; index < rows.length; index += 1) {
    const prev = rows[index - 1].close_cents;
    const current = rows[index].close_cents;
    if (prev > 0) {
      const bps = ((current - prev) * 10_000) / prev;
      if (Math.abs(bps) > 1_500) gaps.set(rows[index].day, bps);
    }
  }
  const gapDays = new Set(gaps.keys());
  const scale = document.createElement("div");
  scale.className = "live-scale";
  const left = document.createElement("span");
  left.textContent = `${rows[0].day} · 最低 ${money(low)}`;
  const right = document.createElement("span");
  right.textContent = `最高 ${money(high)} · ${rows[rows.length - 1].day}`;
  scale.append(left, right);
  const svg = buildLine(closes, gapDays, rows.map((row) => row.day));
  chart.replaceChildren(svg, scale);

  // Hover readout bar
  const hover = byId("detail-hover");
  const showBar = (idx: number): void => {
    const bar = rows[idx];
    if (!bar) return;
    const prev = idx > 0 ? rows[idx - 1].close_cents : bar.prev_close_cents;
    const dayBps = prev > 0 ? ((bar.close_cents - prev) * 10_000) / prev : 0;
    hover.textContent = `${bar.day} · 收盘 ${money(bar.close_cents)} · 日涨跌 ${percent(dayBps)} · 成交 ${number(Math.round(bar.volume_shares / 100))} 手`;
  };
  showBar(rows.length - 1);
  show(hover, true);
  svg.addEventListener("mousemove", (event) => {
    const rect = svg.getBoundingClientRect();
    if (rect.width <= 0 || rows.length === 0) return;
    const ratio = Math.min(1, Math.max(0, (event.clientX - rect.left) / rect.width));
    const idx = Math.min(rows.length - 1, Math.round(ratio * (rows.length - 1)));
    showBar(idx);
  });

  // Volume sub-chart
  const volWrap = byId("detail-volume");
  volWrap.replaceChildren(buildVolumeBars(rows));
  show(volWrap, true);

  // Gap badges
  const gapsWrap = byId("detail-gaps");
  gapsWrap.replaceChildren();
  if (gaps.size > 0) {
    for (const [day, bps] of gaps.entries()) {
      const badge = document.createElement("span");
      badge.className = "gap-badge";
      badge.textContent = `缺口 ${day} (${percent(bps)})`;
      gapsWrap.appendChild(badge);
    }
    show(gapsWrap, true);
  } else {
    show(gapsWrap, false);
  }

  // Recent 20 daily bars table
  const histPanel = byId("detail-history-panel");
  const histBody = byId<HTMLTableSectionElement>("detail-history-body");
  histBody.replaceChildren();
  const recent = rows.slice(-20).reverse();
  setTextById("detail-history-count", `最近 ${recent.length} 日`);
  for (const bar of recent) {
    const tr = document.createElement("tr");
    const dayBps = bar.prev_close_cents > 0
      ? ((bar.close_cents - bar.prev_close_cents) * 10_000) / bar.prev_close_cents
      : 0;
    const cells: Array<[string, string]> = [
      [bar.day, ""],
      [money(bar.close_cents), "num-col"],
      [percent(dayBps), `num-col ${dayBps >= 0 ? "pos" : "neg"}`],
      [number(Math.round(bar.volume_shares / 100)), "num-col"],
      [gapDays.has(bar.day) ? "除权/拆股缺口" : "正常交易", ""],
    ];
    for (const [text, cls] of cells) {
      const td = document.createElement("td");
      td.textContent = text;
      if (cls) td.className = cls;
      tr.appendChild(td);
    }
    histBody.appendChild(tr);
  }
  show(histPanel, true);
}

function buildVolumeBars(rows: Bar[]): SVGSVGElement {
  const width = 720;
  const height = 64;
  const padX = 8;
  const maxVol = Math.max(1, ...rows.map((r) => r.volume_shares));
  const ns = "http://www.w3.org/2000/svg";
  const svg = document.createElementNS(ns, "svg");
  svg.setAttribute("viewBox", `0 0 ${width} ${height}`);
  svg.setAttribute("width", "100%");
  svg.setAttribute("height", String(height));
  svg.setAttribute("role", "img");
  svg.setAttribute("aria-label", "成交量柱状图");
  const slotW = (width - padX * 2) / Math.max(rows.length, 1);
  const barW = Math.max(1.5, Math.min(10, slotW * 0.72));
  rows.forEach((row, index) => {
    const h = Math.max(2, (row.volume_shares / maxVol) * (height - 8));
    const rect = document.createElementNS(ns, "rect");
    const up = row.prev_close_cents <= 0 || row.close_cents >= row.prev_close_cents;
    rect.setAttribute("x", (padX + index * slotW + (slotW - barW) / 2).toFixed(1));
    rect.setAttribute("y", (height - h).toFixed(1));
    rect.setAttribute("width", barW.toFixed(1));
    rect.setAttribute("height", h.toFixed(1));
    rect.setAttribute("rx", "1");
    rect.setAttribute("fill", up ? "var(--positive)" : "var(--negative)");
    rect.setAttribute("fill-opacity", "0.55");
    svg.appendChild(rect);
  });
  return svg;
}

function buildLine(values: number[], gaps: Set<string>, days: string[]): SVGSVGElement {
  const width = 720;
  const height = 200;
  const padX = 8;
  const padY = 12;
  const max = Math.max(...values);
  const min = Math.min(...values);
  const span = Math.max(max - min, 1);
  const x = (index: number): number =>
    padX + ((width - padX * 2) * index) / Math.max(values.length - 1, 1);
  const y = (value: number): number =>
    padY + (height - padY * 2) * (1 - (value - min) / span);

  const ns = "http://www.w3.org/2000/svg";
  const svg = document.createElementNS(ns, "svg");
  svg.setAttribute("viewBox", `0 0 ${width} ${height}`);
  svg.setAttribute("width", "100%");
  svg.setAttribute("height", String(height));
  svg.setAttribute("role", "img");
  svg.setAttribute("aria-label", "收盘价走势");

  const defs = document.createElementNS(ns, "defs");
  const grad = document.createElementNS(ns, "linearGradient");
  grad.setAttribute("id", "detail-fill");
  grad.setAttribute("x1", "0");
  grad.setAttribute("y1", "0");
  grad.setAttribute("x2", "0");
  grad.setAttribute("y2", "1");
  const stop1 = document.createElementNS(ns, "stop");
  stop1.setAttribute("offset", "0%");
  stop1.setAttribute("stop-color", "var(--accent, #1f5bc9)");
  stop1.setAttribute("stop-opacity", "0.18");
  const stop2 = document.createElementNS(ns, "stop");
  stop2.setAttribute("offset", "100%");
  stop2.setAttribute("stop-color", "var(--accent, #1f5bc9)");
  stop2.setAttribute("stop-opacity", "0.01");
  grad.append(stop1, stop2);
  defs.appendChild(grad);
  svg.appendChild(defs);

  for (const frac of [0.25, 0.5, 0.75]) {
    const gy = (padY + (height - padY * 2) * frac).toFixed(1);
    const guide = document.createElementNS(ns, "line");
    guide.setAttribute("class", "curve-baseline");
    guide.setAttribute("x1", String(padX));
    guide.setAttribute("y1", gy);
    guide.setAttribute("x2", String(width - padX));
    guide.setAttribute("y2", gy);
    svg.appendChild(guide);
  }

  let path = "";
  let segPoints: string[] = [];
  let pen = false;
  const bottomY = (height - padY).toFixed(1);
  const flushArea = (endX: string): void => {
    if (segPoints.length < 2) {
      segPoints = [];
      return;
    }
    const startX = segPoints[0].split(",")[0];
    const poly = document.createElementNS(ns, "polygon");
    poly.setAttribute("points", [...segPoints, `${endX},${bottomY}`, `${startX},${bottomY}`].join(" "));
    poly.setAttribute("fill", "url(#detail-fill)");
    svg.appendChild(poly);
    segPoints = [];
  };
  values.forEach((value, index) => {
    const px = x(index).toFixed(1);
    const py = y(value).toFixed(1);
    if (gaps.has(days[index])) {
      if (pen) flushArea(x(index - 1).toFixed(1));
      pen = false; // lift the pen across the gap
    }
    path += `${pen ? "L" : "M"}${px},${py} `;
    segPoints.push(`${px},${py}`);
    pen = true;
  });
  if (pen && values.length > 0) {
    flushArea(x(values.length - 1).toFixed(1));
  }
  const line = document.createElementNS(ns, "path");
  line.setAttribute("d", path.trim());
  line.setAttribute("fill", "none");
  line.setAttribute("stroke", "var(--accent, #1f5bc9)");
  line.setAttribute("stroke-width", "1.9");
  line.setAttribute("stroke-linejoin", "round");
  svg.appendChild(line);
  if (values.length > 0) {
    const lastIdx = values.length - 1;
    const halo = document.createElementNS(ns, "circle");
    halo.setAttribute("cx", x(lastIdx).toFixed(1));
    halo.setAttribute("cy", y(values[lastIdx]).toFixed(1));
    halo.setAttribute("r", "5.5");
    halo.setAttribute("fill", "var(--accent, #1f5bc9)");
    halo.setAttribute("fill-opacity", "0.18");
    const dot = document.createElementNS(ns, "circle");
    dot.setAttribute("cx", x(lastIdx).toFixed(1));
    dot.setAttribute("cy", y(values[lastIdx]).toFixed(1));
    dot.setAttribute("r", "3");
    dot.setAttribute("fill", "var(--accent, #1f5bc9)");
    svg.append(halo, dot);
  }

  for (const [index, day] of days.entries()) {
    if (!gaps.has(day)) continue;
    const mark = document.createElementNS(ns, "circle");
    mark.setAttribute("cx", x(index).toFixed(1));
    mark.setAttribute("cy", y(values[index]).toFixed(1));
    mark.setAttribute("r", "3");
    mark.setAttribute("fill", "var(--muted-foreground, #999)");
    const title = document.createElementNS(ns, "title");
    title.textContent = `${day} 缺口（除权/拆股），不连成实线`;
    mark.appendChild(title);
    svg.appendChild(mark);
  }
  return svg;
}

// Startup needs no daily account round-trip: live views fetch /v2/live/* on
// first render, while market and research load their own read models per route.
installRouter((route) => applyRoute(route));

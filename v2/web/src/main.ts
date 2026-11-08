import "./styles.css";
import {
  decisionTime, experimentStatus, inputName, integer, money, packName,
  policyName, reasonLabel, resultLabel, sideLabel, signalName, strategyName,
} from "./format";

// ---------------------------------------------------------------------------
// Wire types: exactly the JSON the kernel returns.
// ---------------------------------------------------------------------------
type ExperimentSummary = {
  cycle_id: string;
  status: string;
  strategy_version_id: string;
  research_input_id: string;
  market_data_pack_id: string;
  execution_policy_version: string;
  initial_cash_cents: number;
  runs_total: number;
};

type ExperimentCatalogue = {
  experiments: ExperimentSummary[];
  total?: number;
  skipped?: number;
};

type ExperimentDetail = {
  spec: {
    cycle_id: string;
    initial_cash_cents: number;
    market_data_pack_id: string;
    execution_policy_version: string;
    strategy: {
      strategy_version_id: string;
      research_input_id: string;
      research_input_digest: string;
      source_digest: string;
      parameters_digest: string;
    };
  };
  status: string;
};

type Position = { symbol: string; quantity: number; sellable_quantity: number | null };
type Portfolio = {
  cycle_id: string;
  initial_cash_cents: number;
  cash_cents: number;
  asof_day: string | null;
  positions: Position[];
  runs_total: number;
};

type Run = {
  id: number;
  request: { signal_id: string; decision_at: string; signal: { side: string } };
  outcome: { status: string; reason: string };
};

type ReplayReport = {
  cycle_id: string; ok: boolean; runs_verified: number; fills_verified: number; cash_cents: number;
};
type ReproductionReport = { cycle_id: string; ok: boolean; signals_verified: number };

/** An experiment plus the exact as-of scope of the facts on screen. */
type Scope = { cycle: string; asof: string | null };
type VerifyKind = "replay" | "reproduction";

// ---------------------------------------------------------------------------
// DOM helpers. External strings only ever go through textContent.
// ---------------------------------------------------------------------------
const byId = <T extends HTMLElement>(id: string): T => {
  const node = document.getElementById(id);
  if (!node) throw new Error(`missing #${id}`);
  return node as T;
};
const testId = <T extends HTMLElement>(id: string): T => {
  const node = document.querySelector<T>(`[data-testid="${id}"]`);
  if (!node) throw new Error(`missing [data-testid=${id}]`);
  return node;
};
const setText = (id: string, value: string): void => { testId(id).textContent = value; };
const setById = (id: string, value: string): void => { byId(id).textContent = value; };
const toggle = (node: HTMLElement, visible: boolean): void => { node.hidden = !visible; };

const live = byId<HTMLElement>("live");
const announce = (message: string): void => { live.textContent = message; };

// ---------------------------------------------------------------------------
// API
// ---------------------------------------------------------------------------
class ApiError extends Error {
  constructor(readonly status: number | null, readonly detail: string) {
    super(status === null ? "network" : `http_${status}`);
  }
}

async function api<T>(path: string, signal: AbortSignal): Promise<T> {
  let response: Response;
  try {
    response = await fetch(path, { signal, cache: "no-store", headers: { Accept: "application/json" } });
  } catch (error) {
    if (signal.aborted) throw error;
    throw new ApiError(null, error instanceof Error ? error.message : String(error));
  }
  if (!response.ok) {
    const body = await response.text().catch(() => "");
    throw new ApiError(response.status, body.slice(0, 400));
  }
  return (await response.json()) as T;
}

const errorDetail = (error: unknown): string => {
  if (error instanceof ApiError) {
    if (error.status === null) return "无法连接本地服务";
    let code = error.detail;
    try {
      const parsed = JSON.parse(error.detail) as { error?: string };
      if (typeof parsed.error === "string") {
        code = parsed.error
          .replace(/_/g, " ")
          .replace(/\b\w/g, (letter) => letter.toUpperCase());
      }
    } catch { /* keep raw body */ }
    return code;
  }
  return error instanceof Error ? error.message : String(error);
};

// ---------------------------------------------------------------------------
// Views: a two-level menu; one panel at a time.
// ---------------------------------------------------------------------------
const VIEWS = {
  overview: { panel: "panel-overview", label: "概览" },
  positions: { panel: "panel-positions", label: "持仓" },
  decisions: { panel: "panel-decisions", label: "决策记录" },
  replay: { panel: "panel-replay", label: "交易回放" },
  reproduction: { panel: "panel-reproduction", label: "研究重做" },
  comparison: { panel: "panel-comparison", label: "实验比较" },
  experiment: { panel: "panel-experiment", label: "实验信息" },
} as const;
type View = keyof typeof VIEWS;
const viewFromHash = (): View => {
  const candidate = location.hash.replace(/^#\/?/, "") as View;
  return candidate in VIEWS ? candidate : "overview";
};
let currentView: View = viewFromHash();

const navButtons = Array.from(document.querySelectorAll<HTMLButtonElement>("[data-view]"));

const showView = (view: View, options: { updateUrl?: boolean } = {}): void => {
  currentView = view;
  for (const [name, meta] of Object.entries(VIEWS)) {
    toggle(byId(meta.panel), name === view);
  }
  for (const button of navButtons) {
    const active = button.dataset.view === view;
    button.classList.toggle("is-active", active);
    if (active) button.setAttribute("aria-current", "page");
    else button.removeAttribute("aria-current");
  }
  if (view === "comparison" && experiments.length > 0) hydrateComparison();
  if (options.updateUrl !== false && location.hash !== `#/${view}`) {
    history.replaceState(null, "", `#/${view}`);
    // Switching panels from a scrolled page would otherwise land mid-table.
    if (window.scrollY > 0) {
      const reduce = window.matchMedia("(prefers-reduced-motion: reduce)").matches;
      byId("main").scrollIntoView({ behavior: reduce ? "auto" : "smooth", block: "start" });
    }
  }
  announce(`已切换到${VIEWS[view].label}`);
};

type PanelState = "service" | "empty" | "unknown" | "broken" | "workspace" | "loading" | null;
const NOTICE_IDS: Record<Exclude<PanelState, "workspace" | null>, string> = {
  service: "service-error",
  empty: "empty-experiments",
  unknown: "unknown-experiment",
  broken: "catalogue-broken",
  loading: "loading",
};

const showOnly = (panel: PanelState): void => {
  const active = document.activeElement as HTMLElement | null;
  for (const [state, id] of Object.entries(NOTICE_IDS)) {
    toggle(testId(id), panel === state);
  }
  toggle(byId("context-bar"), panel === "workspace");
  toggle(byId("workspace"), panel === "workspace");
  // The damaged-row warning belongs to a readable workspace, not to notices.
  const warningVisible = panel === "workspace" && listSkipped > 0;
  toggle(byId("catalogue-warning"), warningVisible);
  if (warningVisible) {
    setById("catalogue-warning",
      `有 ${listSkipped} 个实验记录数据损坏已跳过；其余 ${experiments.length} 个实验仍可正常读取。`);
  }
  // Nothing to navigate to while a notice owns the screen.
  const navEnabled = panel === "workspace";
  for (const button of navButtons) button.disabled = !navEnabled;
  if (active && active !== document.body && !isVisible(active)) {
    const region = panel === "workspace" ? byId("workspace")
      : panel ? testId(NOTICE_IDS[panel])
      : byId("main");
    const heading = region.querySelector<HTMLElement>("h1, h2");
    if (heading) { heading.tabIndex = -1; heading.focus(); } else { byId("main").focus(); }
  }
};

const isVisible = (node: HTMLElement | null): boolean =>
  !!node && (node.offsetWidth > 0 || node.offsetHeight > 0 || node.getClientRects().length > 0);

// ---------------------------------------------------------------------------
// State
// ---------------------------------------------------------------------------
let experiments: ExperimentSummary[] = [];
let listTotal = 0;
let listSkipped = 0;
let currentCycle = "";
let loadSequence = 0;
let asofSequence = 0;
let listController: AbortController | null = null;
let cycleController: AbortController | null = null;
// `asofDay` is what the user asks for; `loadedScope` is what the API actually
// returned and what the screen is currently allowed to show.
let asofDay: string | null = null;
let loadedScope: Scope | null = null;
let dataEpoch = 0;
const verifySeq: Record<VerifyKind, number> = { replay: 0, reproduction: 0 };
const verifiedEpoch: Record<VerifyKind, number> = { replay: -1, reproduction: -1 };
let lastPositions: Position[] = [];
let lastRuns: Run[] = [];
const FILTER_LABELS = { all: "全部", filled: "已成交", rejected: "已拒绝" } as const;
type RunFilter = keyof typeof FILTER_LABELS;
const urlResult = (): RunFilter | null => {
  const raw = new URLSearchParams(location.search).get("result");
  return raw === "filled" || raw === "rejected" || raw === "all" ? raw : null;
};
let runFilter: RunFilter = urlResult() ?? "all";
let rendered = false;

const select = testId<HTMLSelectElement>("experiment-select");
const hint = testId("cycle-hint");

const setBusy = (busy: boolean): void => {
  byId("workspace").setAttribute("aria-busy", String(busy));
  document.documentElement.dataset.busy = String(busy);
};

// ---------------------------------------------------------------------------
// URL
// ---------------------------------------------------------------------------
const urlCycle = (): string => new URLSearchParams(location.search).get("cycle_id")?.trim() ?? "";
const syncUrl = (cycle: string): void => {
  const url = new URL(location.href);
  if (cycle) url.searchParams.set("cycle_id", cycle);
  else url.searchParams.delete("cycle_id");
  if (runFilter === "all") url.searchParams.delete("result");
  else url.searchParams.set("result", runFilter);
  if (url.toString() !== location.href) history.replaceState(null, "", url);
};

// ---------------------------------------------------------------------------
// Rendering
// ---------------------------------------------------------------------------
const resetWorkspace = (): void => {
  for (const id of ["summary-status", "summary-strategy", "summary-input", "summary-pack", "summary-policy",
                    "kpi-initial-cash", "kpi-cash", "kpi-positions", "kpi-runs"]) {
    setText(id, "—");
  }
  setText("experiment-name", "—");
  setText("experiment-state", "—");
  for (const id of ["tech-strategy", "tech-input", "tech-pack", "tech-policy"]) setText(id, "—");
  setById("context-cycle", "—");
  setById("last-refresh", "—");
  const contextStatus = byId("context-status");
  contextStatus.textContent = "—";
  contextStatus.dataset.state = "idle";
  byId("positions-body").replaceChildren();
  byId("runs-body").replaceChildren();
  lastRuns = [];
  paintRuns();
  toggle(testId("positions-empty-state"), true);
  toggle(testId("runs-empty-state"), true);
  toggle(testId("positions-error"), false);
  toggle(testId("runs-error"), false);
  for (const kind of VERIFY_KINDS) invalidateVerification(kind);
  setVerify("replay", "idle", "未验证", "");
  setVerify("reproduction", "idle", "未验证", "");
  asofDay = null;
  loadedScope = null;
  (byId<HTMLInputElement>("asof-day")).value = "";
  toggle(testId("sellable-header"), false);
  setById("positions-count", "按标的汇总的剩余数量");
  setById("kpi-scope", "资产口径：—");
};

const describeCatalogue = (selected: string, options: { unlisted?: boolean } = {}): string => {
  const parts = [`共 ${listTotal} 个实验`];
  if (experiments.length < listTotal) parts.push(`末页可读取 ${experiments.length} 个（上限 ${CATALOGUE_LIMIT}）`);
  if (listSkipped > 0) parts.push(`${listSkipped} 个数据损坏已跳过`);
  if (options.unlisted) parts.push("当前实验不在目录中，已直接核对");
  if (!selected) parts.push("未指定时默认选择第一个");
  return parts.join(" · ");
};

const compareA = byId<HTMLSelectElement>("compare-a");
const compareB = byId<HTMLSelectElement>("compare-b");

const fillSelect = (target: HTMLSelectElement, selected: string): void => {
  target.replaceChildren();
  for (const experiment of experiments) {
    const option = document.createElement("option");
    option.value = experiment.cycle_id;
    option.textContent = experiment.cycle_id;
    target.appendChild(option);
  }
  if (selected && experiments.some((experiment) => experiment.cycle_id === selected)) {
    target.value = selected;
  }
};

const renderSelector = (): void => {
  select.replaceChildren();
  for (const experiment of experiments) {
    const option = document.createElement("option");
    option.value = experiment.cycle_id;
    option.textContent = experiment.cycle_id;
    select.appendChild(option);
  }
  select.disabled = experiments.length === 0;
  fillSelect(compareA, compareA.value || experiments[0]?.cycle_id || "");
  fillSelect(compareB, compareB.value || experiments[1]?.cycle_id || experiments[0]?.cycle_id || "");
};

const renderSummary = (detail: ExperimentDetail): void => {
  const { spec } = detail;
  setText("summary-status", experimentStatus(detail.status));
  setText("summary-strategy", strategyName(spec.strategy.strategy_version_id));
  setText("summary-input", inputName(spec.strategy.research_input_id));
  setText("summary-pack", packName(spec.market_data_pack_id));
  setText("summary-policy", policyName(spec.execution_policy_version));
  setText("experiment-name", spec.cycle_id);
  setText("experiment-state", experimentStatus(detail.status));
  // Context bar: the same experiment identity, visible from every panel.
  setById("context-cycle", spec.cycle_id);
  const contextStatus = byId("context-status");
  contextStatus.textContent = experimentStatus(detail.status);
  contextStatus.dataset.state = detail.status;
  // Raw identifiers exist only inside the collapsed technical panel.
  setText("tech-strategy", spec.strategy.strategy_version_id);
  setText("tech-input", spec.strategy.research_input_id);
  setText("tech-pack", spec.market_data_pack_id);
  setText("tech-policy", spec.execution_policy_version);
};

const scopeText = (scope: Scope | null): string =>
  !scope || scope.asof === null ? "当前持仓（未指定日终）" : `日终 ${scope.asof}（中国时间）`;

const sameScope = (left: Scope, right: Scope): boolean =>
  left.cycle === right.cycle && left.asof === right.asof;

class ScopeMismatchError extends Error {}

/**
 * Render facts only when the response proves it belongs to the requested
 * experiment and as-of day. A mismatch is an error, never a silent relabel.
 */
const renderPortfolio = (portfolio: Portfolio, expected: Scope): void => {
  const actual: Scope = { cycle: portfolio.cycle_id, asof: portfolio.asof_day ?? null };
  if (!sameScope(actual, expected)) {
    throw new ScopeMismatchError(
      `资产口径不一致：请求 ${expected.cycle} / ${scopeText(expected)}，返回 ${actual.cycle} / ${scopeText(actual)}`,
    );
  }
  loadedScope = actual;
  setText("kpi-initial-cash", money(portfolio.initial_cash_cents));
  setText("kpi-cash", money(portfolio.cash_cents));
  setText("kpi-positions", integer(portfolio.positions.length));
  setText("kpi-runs", integer(portfolio.runs_total));
  setById("kpi-scope", `资产口径：${scopeText(actual)}`);
  lastPositions = portfolio.positions;
  toggle(testId("positions-error"), false);
  renderPositions();
};

const renderPositions = (): void => {
  const body = byId("positions-body");
  body.replaceChildren();
  const asof = loadedScope?.asof ?? null;
  toggle(testId("sellable-header"), asof !== null);
  setById("positions-count", asof === null
    ? `共 ${lastPositions.length} 个标的 · 未指定日终口径`
    : `共 ${lastPositions.length} 个标的 · 日终口径 ${asof}`);
  if (asof !== null) {
    const note = document.createElement("tr");
    note.className = "asof-row";
    const cell = document.createElement("td");
    cell.colSpan = 3;
    cell.textContent = `日终口径 ${asof} · 可卖数量按该日计算`;
    note.appendChild(cell);
    body.appendChild(note);
  }
  for (const position of lastPositions) {
    const row = document.createElement("tr");
    const symbol = document.createElement("td");
    symbol.className = "symbol";
    symbol.textContent = position.symbol;
    const quantity = document.createElement("td");
    quantity.className = "num-col";
    quantity.textContent = integer(position.quantity);
    row.append(symbol, quantity);
    if (asof !== null) {
      const sellable = document.createElement("td");
      sellable.className = "num-col";
      sellable.textContent = position.sellable_quantity === null ? "—" : integer(position.sellable_quantity);
      row.appendChild(sellable);
    }
    body.appendChild(row);
  }
  toggle(testId("positions-empty-state"), lastPositions.length === 0);
};

const filterChips = Array.from(document.querySelectorAll<HTMLButtonElement>(".chip[data-filter]"));

/** Repaint the decision table for the active result filter. */
const paintRuns = (): void => {
  const body = byId("runs-body");
  body.replaceChildren();
  const rows = runFilter === "all"
    ? lastRuns
    : lastRuns.filter((run) => run.outcome.status === runFilter);
  for (const run of rows) {
    const row = document.createElement("tr");
    const time = document.createElement("td");
    const runId = document.createElement("span");
    runId.className = "run-id";
    runId.textContent = `#${run.id}`;
    runId.title = `记录 #${run.id}`;
    time.append(runId, document.createTextNode(decisionTime(run.request.decision_at)));
    const signal = document.createElement("td");
    signal.textContent = signalName(run.request.signal_id);
    signal.title = run.request.signal_id;
    // The pill is a span inside the cell: styling a <td> as a pill breaks
    // table layout and pushes the cell out of the row's baseline.
    const side = document.createElement("td");
    const sidePill = document.createElement("span");
    sidePill.className = run.request.signal.side === "buy" ? "side-buy" : "side-sell";
    sidePill.textContent = sideLabel(run.request.signal.side);
    side.appendChild(sidePill);
    const result = document.createElement("td");
    const resultPill = document.createElement("span");
    resultPill.className = run.outcome.status === "filled" ? "result-filled" : "result-rejected";
    resultPill.textContent = resultLabel(run.outcome.status);
    result.appendChild(resultPill);
    const reason = document.createElement("td");
    reason.className = "wrap";
    reason.textContent = reasonLabel(run.outcome.reason);
    reason.title = run.outcome.reason;
    row.append(time, signal, side, result, reason);
    body.appendChild(row);
  }
  toggle(testId("runs-empty-state"), lastRuns.length === 0);
  toggle(byId("runs-filtered-empty"), lastRuns.length > 0 && rows.length === 0);
  for (const chip of filterChips) {
    const value = (chip.dataset.filter ?? "all") as RunFilter;
    const count = value === "all"
      ? lastRuns.length
      : lastRuns.filter((run) => run.outcome.status === value).length;
    chip.setAttribute("aria-pressed", String(value === runFilter));
    chip.textContent = `${FILTER_LABELS[value]} ${count}`;
  }
  renderRecent();
};

/** The overview shows the three newest decisions; the panel owns the detail. */
const renderRecent = (): void => {
  const list = byId("recent-list");
  list.replaceChildren();
  const recent = lastRuns.slice(0, 3);
  for (const run of recent) {
    const item = document.createElement("li");
    const time = document.createElement("span");
    time.className = "recent-time";
    time.textContent = decisionTime(run.request.decision_at);
    const signal = document.createElement("span");
    signal.className = "recent-signal";
    signal.textContent = signalName(run.request.signal_id);
    const side = document.createElement("span");
    side.className = run.request.signal.side === "buy" ? "side-buy" : "side-sell";
    side.textContent = sideLabel(run.request.signal.side);
    const result = document.createElement("span");
    result.className = run.outcome.status === "filled" ? "result-filled" : "result-rejected";
    result.textContent = resultLabel(run.outcome.status);
    item.append(time, signal, side, result);
    list.appendChild(item);
  }
  toggle(byId("recent-empty"), recent.length === 0);
  const filled = lastRuns.filter((run) => run.outcome.status === "filled").length;
  setById("recent-note", lastRuns.length === 0
    ? "共 0 条决策"
    : `共 ${lastRuns.length} 条决策 · 已成交 ${filled} · 已拒绝 ${lastRuns.length - filled}`);
};

const renderRuns = (runs: Run[]): void => {
  lastRuns = runs;
  toggle(testId("runs-error"), false);
  paintRuns();
};

type VerifyState = "idle" | "running" | "ok" | "fail" | "stale";
const VERIFY_KINDS: VerifyKind[] = ["replay", "reproduction"];
const verifyState: Record<VerifyKind, VerifyState> = { replay: "idle", reproduction: "idle" };

const setVerify = (
  kind: VerifyKind,
  state: VerifyState,
  message: string,
  detail: string,
): void => {
  verifyState[kind] = state;
  const status = testId(`${kind}-status`);
  status.dataset.state = state;
  status.textContent = message;
  setText(`${kind}-detail`, detail);
  // The overview shows the same two facts at a glance.
  const mirror = document.getElementById(`overview-${kind}`);
  if (mirror) {
    mirror.dataset.state = state;
    mirror.textContent = message;
  }
};

const verifyButton = (kind: VerifyKind): HTMLButtonElement => testId(`verify-${kind}`);

/** A notice taking over the screen must not leave a check stuck on "running". */
const settleInterruptedVerifications = (): void => {
  for (const kind of VERIFY_KINDS) {
    if (verifyState[kind] !== "running") continue;
    invalidateVerification(kind);
    setVerify(kind, "idle", "未验证", "");
  }
};

/**
 * Cancel whatever check is in flight for this kind and hand the control back.
 * The sequence bump makes any late answer inert; the button/aria state is owned
 * here so a reload or experiment switch can never leave a control locked.
 */
const invalidateVerification = (kind: VerifyKind): void => {
  verifySeq[kind] += 1;
  verifiedEpoch[kind] = -1;
  const button = verifyButton(kind);
  button.disabled = false;
  button.removeAttribute("aria-busy");
};

// ---------------------------------------------------------------------------
// Loading
// ---------------------------------------------------------------------------
const showSectionError = (section: "positions" | "runs", message: string, retry: () => void): void => {
  const node = testId<HTMLElement>(`${section}-error`);
  const hadFocus = node.contains(document.activeElement);
  node.replaceChildren();
  const text = document.createElement("span");
  text.textContent = message;
  const button = document.createElement("button");
  button.type = "button";
  button.className = "btn btn-ghost";
  button.textContent = "重试";
  button.addEventListener("click", retry);
  node.append(text, button);
  toggle(node, true);
  if (hadFocus) button.focus();
};

/** One sentence describing why the catalogue could not be read — never duplicates
 *  the "本地服务不可用" wording that the notice already shows. */
const listFailureText = (error: unknown): string => {
  if (error instanceof ApiError && error.status === null) return "无法连接本地服务。请确认内核已启动。";
  return `无法连接本地服务：${errorDetail(error)}`;
};

const showListFailure = (message: string): void => {
  setText("service-detail", message);
  showOnly("service");
};

const portfolioQuery = (cycle: string, asof: string | null): string => {
  const base = `/v2/portfolio?cycle_id=${encodeURIComponent(cycle)}`;
  return asof === null ? base : `${base}&asof_day=${encodeURIComponent(asof)}`;
};

/** Report a portfolio failure without ever relabelling facts already on screen. */
const portfolioFailure = (error: unknown, keepContent: boolean, retry: () => void): void => {
  if (keepContent && loadedScope) {
    showSectionError("positions",
      `本次资产查询失败，仍显示${scopeText(loadedScope)}：${errorDetail(error)}`, retry);
    announce("资产查询失败，已保留上次成功的资产口径");
    return;
  }
  setText("kpi-cash", "读取失败");
  setText("kpi-positions", "读取失败");
  setText("kpi-runs", "读取失败");
  setById("kpi-scope", "资产口径：读取失败");
  showSectionError("positions", `资产读取失败：${errorDetail(error)}`, retry);
  announce("资产读取失败");
};

const loadCycle = async (cycle: string, options: { keepContent?: boolean } = {}): Promise<void> => {
  const sequence = ++loadSequence;
  cycleController?.abort();
  const controller = new AbortController();
  cycleController = controller;
  currentCycle = cycle;
  asofSequence++;
  // Snapshot the outgoing verification state, then stop in-flight checks so the
  // buttons are usable again even if a late answer never lands on this screen.
  const previousVerify: Record<VerifyKind, VerifyState> = { ...verifyState };
  for (const kind of VERIFY_KINDS) invalidateVerification(kind);
  if (!rendered) showOnly("loading");
  if (!options.keepContent) resetWorkspace();
  setBusy(true);
  if (options.keepContent) announce("正在刷新…");

  // One request per refresh: the applied as-of day is part of the request, so a
  // current-holdings response can never be shown under a historical label.
  const requestedScope: Scope = { cycle, asof: asofDay };
  const encoded = encodeURIComponent(cycle);
  const results = await Promise.allSettled([
    api<ExperimentDetail>(`/v2/experiments/${encoded}`, controller.signal),
    api<Portfolio>(portfolioQuery(cycle, requestedScope.asof), controller.signal),
    api<Run[]>(`/v2/simulations?cycle_id=${encoded}`, controller.signal),
  ]);
  if (sequence !== loadSequence) return;

  setBusy(false);
  const [detail, portfolio, runs] = results;

  if (detail.status === "rejected") {
    const error = detail.reason;
    if (error instanceof ApiError && error.status === 404) {
      byId("unknown-cycle").textContent = cycle;
      settleInterruptedVerifications();
      showOnly("unknown");
      announce("实验不存在");
      return;
    }
    settleInterruptedVerifications();
    showListFailure(`读取实验失败：${errorDetail(error)}`);
    return;
  }
  if (detail.value.spec.cycle_id !== cycle) {
    settleInterruptedVerifications();
    showListFailure(`读取实验失败：返回的实验与请求不一致（${detail.value.spec.cycle_id}）`);
    return;
  }

  rendered = true;
  showOnly("workspace");
  showView(currentView);
  renderSummary(detail.value);
  setById("last-refresh", new Date().toLocaleTimeString("zh-CN", { hour12: false }));
  const retry = () => void loadCycle(currentCycle, { keepContent: true });

  if (portfolio.status === "fulfilled") {
    try {
      renderPortfolio(portfolio.value, requestedScope);
    } catch (error) {
      portfolioFailure(error, options.keepContent === true, retry);
    }
  } else {
    portfolioFailure(portfolio.reason, options.keepContent === true, retry);
  }

  if (runs.status === "fulfilled") {
    renderRuns(runs.value);
  } else {
    showSectionError("runs", `决策读取失败：${errorDetail(runs.reason)}`, retry);
  }

  // A fresh snapshot invalidates earlier verification results.
  dataEpoch += 1;
  for (const kind of VERIFY_KINDS) {
    if (verifyState[kind] === "running") {
      // A check started while the reload was in flight can never describe this
      // snapshot; stop it and ask for a re-run.
      invalidateVerification(kind);
      setVerify(kind, "stale", "需要重新验证", "实验数据已重新载入，请重新运行校验");
    } else if (options.keepContent && previousVerify[kind] !== "idle") {
      setVerify(kind, "stale", "需要重新验证", "实验数据已重新载入，请重新运行校验");
    }
  }

  announce(`已载入实验 ${cycle}（${scopeText(portfolio.status === "fulfilled" ? loadedScope : null)}）`);
};

const CATALOGUE_LIMIT = 500;

/** Ask the single-experiment endpoint whether an unlisted cycle really exists. */
const probeExperiment = async (
  cycle: string,
  signal: AbortSignal,
): Promise<ExperimentDetail | ApiError> => {
  try {
    return await api<ExperimentDetail>(`/v2/experiments/${encodeURIComponent(cycle)}`, signal);
  } catch (error) {
    return error instanceof ApiError ? error : new ApiError(null, String(error));
  }
};

const showCatalogueBroken = (total: number, skipped: number): void => {
  setText("broken-detail",
    `目录登记 ${total} 个实验，可读取 0 个${skipped > 0 ? `（${skipped} 个记录数据损坏）` : ""}。` +
    "这不是“没有实验”，而是登记数据无法读取。");
  hint.textContent = `共 ${total} 个实验，均无法读取`;
  showOnly("broken");
  announce("实验目录数据异常");
};

const loadList = async (): Promise<void> => {
  showOnly("loading");
  listController?.abort();
  const controller = new AbortController();
  listController = controller;
  let list: ExperimentSummary[];
  try {
    const body = await api<ExperimentCatalogue>(`/v2/experiments?limit=${CATALOGUE_LIMIT}`, controller.signal);
    list = Array.isArray(body.experiments) ? body.experiments : [];
    listTotal = typeof body.total === "number" ? body.total : list.length;
    listSkipped = typeof body.skipped === "number" ? body.skipped : 0;
  } catch (error) {
    if (controller.signal.aborted) return;
    showListFailure(listFailureText(error));
    announce("服务不可用");
    return;
  }

  experiments = list;
  renderSelector();

  if (list.length === 0) {
    if (listTotal > 0 || listSkipped > 0) {
      showCatalogueBroken(listTotal, listSkipped);
      return;
    }
    showOnly("empty");
    hint.textContent = "未登记实验";
    announce("还没有实验");
    return;
  }

  const requested = urlCycle();
  let unlisted = false;
  if (requested && !list.some((item) => item.cycle_id === requested)) {
    const probe = await probeExperiment(requested, controller.signal);
    if (controller.signal.aborted) return;
    if (probe instanceof ApiError) {
      if (probe.status === 404) {
        byId("unknown-cycle").textContent = requested;
        const placeholder = document.createElement("option");
        placeholder.value = "";
        placeholder.textContent = "选择实验…";
        placeholder.disabled = true;
        placeholder.selected = true;
        select.prepend(placeholder);
        hint.textContent = `共 ${list.length} 个实验（目录最多显示 ${CATALOGUE_LIMIT} 条）；地址中的实验不存在`;
        showOnly("unknown");
        announce("实验不存在");
        return;
      }
      showListFailure(`读取实验失败：${errorDetail(probe)}`);
      announce("实验读取失败");
      return;
    }
    // It exists beyond the catalogue window: keep the identity visible.
    unlisted = true;
    const option = document.createElement("option");
    option.value = requested;
    option.textContent = `${requested}（目录未列出）`;
    select.prepend(option);
  }

  const cycle = requested || list[0].cycle_id;
  select.value = cycle;
  hint.textContent = describeCatalogue(requested, { unlisted });
  syncUrl(cycle);
  await loadCycle(cycle);
  if (currentView === "comparison") hydrateComparison();
};

// ---------------------------------------------------------------------------
// Actions
// ---------------------------------------------------------------------------
select.addEventListener("change", () => {
  const cycle = select.value;
  if (!cycle) return;
  syncUrl(cycle);
  hint.textContent = describeCatalogue(cycle);
  void loadCycle(cycle);
});

byId("refresh").addEventListener("click", () => {
  // The refresh keeps whatever as-of scope is applied and loads it exactly once.
  if (currentCycle) void loadCycle(currentCycle, { keepContent: true });
  else void loadList();
});
testId("retry").addEventListener("click", () => void loadList());
testId("catalogue-retry").addEventListener("click", () => void loadList());

for (const button of navButtons) {
  button.addEventListener("click", () => showView(button.dataset.view as View));
  const index = (Object.keys(VIEWS) as View[]).indexOf(button.dataset.view as View);
  if (index >= 0) button.setAttribute("aria-keyshortcuts", String(index + 1));
}
testId("refresh").setAttribute("aria-keyshortcuts", "r");
for (const link of Array.from(document.querySelectorAll<HTMLElement>("[data-goto]"))) {
  link.addEventListener("click", () => showView(link.dataset.goto as View));
}
window.addEventListener("hashchange", () => {
  const view = viewFromHash();
  if (view !== currentView) showView(view, { updateUrl: false });
});

for (const chip of filterChips) {
  chip.addEventListener("click", () => {
    const value = (chip.dataset.filter ?? "all") as RunFilter;
    if (value === runFilter) return;
    runFilter = value;
    syncUrl(currentCycle);
    paintRuns();
    const shown = value === "all"
      ? lastRuns.length
      : lastRuns.filter((run) => run.outcome.status === value).length;
    announce(`已筛选为${FILTER_LABELS[value]}，${shown} 条决策`);
  });
}

const runVerification = async (kind: VerifyKind): Promise<void> => {
  const cycle = currentCycle;
  if (!cycle) return;
  const button = testId<HTMLButtonElement>(`verify-${kind}`);
  if (button.disabled) return; // one in-flight check per kind
  const label = kind === "replay" ? "回放" : "研究重做";
  const seq = ++verifySeq[kind];
  const epoch = dataEpoch;
  verifyButton(kind).disabled = true;
  verifyButton(kind).setAttribute("aria-busy", "true");
  setVerify(kind, "running", "进行中…", "");
  announce(`${label}开始`);
  const encoded = encodeURIComponent(cycle);
  const current = (): boolean =>
    seq === verifySeq[kind] && epoch === dataEpoch && cycle === currentCycle;
  try {
    if (kind === "replay") {
      const report = await api<ReplayReport>(`/v2/replay?cycle_id=${encoded}`, new AbortController().signal);
      if (!current()) return;
      if (report.cycle_id !== cycle) {
        setVerify(kind, "fail", "失败：回放报告属于其它实验", "跨实验的校验结果不能作为本实验证据");
        announce("交易回放实验身份不一致");
        return;
      }
      if (report.ok !== true) {
        setVerify(kind, "fail", `未通过（${report.runs_verified} 次决策，${report.fills_verified} 次成交）`,
          "回放报告未声明通过，不能视为可复现");
        announce("交易回放未通过");
        return;
      }
      verifiedEpoch[kind] = epoch;
      setVerify(kind, "ok", `通过（${report.runs_verified} 次决策，${report.fills_verified} 次成交）`,
        `校验后现金 ${money(report.cash_cents)}`);
      announce("交易回放通过");
    } else {
      const report = await api<ReproductionReport>(
        `/v2/experiments/${encoded}/research-reproduction`, new AbortController().signal);
      if (!current()) return;
      if (report.cycle_id !== cycle) {
        setVerify(kind, "fail", "失败：重做报告属于其它实验", "跨实验的校验结果不能作为本实验证据");
        announce("研究重做实验身份不一致");
        return;
      }
      if (report.ok !== true) {
        setVerify(kind, "fail", `未通过（${report.signals_verified} 条研究信号）`,
          "重做报告未声明通过，研究信号不可复现");
        announce("研究重做未通过");
        return;
      }
      verifiedEpoch[kind] = epoch;
      setVerify(kind, "ok", `通过（${report.signals_verified} 条研究信号）`, "研究信号与固定输入一致");
      announce("研究重做通过");
    }
  } catch (error) {
    if (!current()) return;
    setVerify(kind, "fail", `失败：${errorDetail(error)}`, "该实验结果不能用于研究比较");
    announce(`${label}失败`);
  } finally {
    if (seq === verifySeq[kind] && cycle === currentCycle) {
      verifyButton(kind).disabled = false;
      verifyButton(kind).removeAttribute("aria-busy");
      if (verifyState[kind] === "running") {
        // The answer belonged to another data epoch and was dropped.
        setVerify(kind, "idle", "未验证", "");
      }
    }
  }
};
testId("verify-replay").addEventListener("click", () => void runVerification("replay"));
testId("verify-reproduction").addEventListener("click", () => void runVerification("reproduction"));

const loadPortfolioOnly = async (): Promise<void> => {
  const cycle = currentCycle;
  if (!cycle) return;
  const requested: Scope = { cycle, asof: asofDay };
  const sequence = ++asofSequence;
  try {
    const portfolio = await api<Portfolio>(portfolioQuery(cycle, requested.asof), new AbortController().signal);
    if (sequence !== asofSequence || cycle !== currentCycle) return;
    renderPortfolio(portfolio, requested);
    announce(requested.asof === null ? "已清除日终口径" : `已按 ${requested.asof} 日终口径读取`);
  } catch (error) {
    if (sequence !== asofSequence || cycle !== currentCycle) return;
    // The screen keeps the last successfully loaded scope; the failed request is
    // reported as a failed query, never as new facts for the requested date.
    showSectionError("positions",
      loadedScope
        ? `本次日终查询失败，仍显示${scopeText(loadedScope)}：${errorDetail(error)}`
        : `资产读取失败：${errorDetail(error)}`,
      () => void loadPortfolioOnly());
    announce("日终查询失败，已保留上次成功的资产口径");
  }
};

byId("asof-apply").addEventListener("click", () => {
  const input = byId<HTMLInputElement>("asof-day");
  const error = testId<HTMLElement>("asof-error");
  const value = input.value.trim();
  if (!value) {
    input.setAttribute("aria-invalid", "true");
    toggle(error, true);
    announce("请先选择日终日期再应用");
    input.focus();
    return;
  }
  input.removeAttribute("aria-invalid");
  toggle(error, false);
  asofDay = value;
  void loadPortfolioOnly();
});
byId("asof-clear").addEventListener("click", () => {
  byId<HTMLInputElement>("asof-day").value = "";
  byId<HTMLInputElement>("asof-day").removeAttribute("aria-invalid");
  toggle(testId("asof-error"), false);
  asofDay = null;
  void loadPortfolioOnly();
});

byId("controls").addEventListener("submit", (event) => event.preventDefault());

const copyLink = testId<HTMLButtonElement>("copy-link");
copyLink.addEventListener("click", async () => {
  const label = copyLink.querySelector<HTMLElement>(".copy-label");
  try {
    await navigator.clipboard.writeText(location.href);
    if (label) label.textContent = "已复制";
    announce("当前视图链接已复制");
    window.setTimeout(() => { if (label) label.textContent = "复制链接"; }, 2000);
  } catch {
    announce("复制失败，请手动复制地址栏链接");
  }
});

const copyTech = testId<HTMLButtonElement>("copy-tech");
copyTech.addEventListener("click", async () => {
  const fields: Array<[string, string]> = [
    ["实验", "experiment-name"],
    ["策略标识", "tech-strategy"],
    ["研究输入标识", "tech-input"],
    ["数据包标识", "tech-pack"],
    ["执行规则标识", "tech-policy"],
  ];
  const text = fields.map(([label, id]) => `${label}: ${byId(id).textContent ?? ""}`).join("\n");
  try {
    await navigator.clipboard.writeText(text);
    copyTech.textContent = "已复制";
    announce("技术细节已复制");
    window.setTimeout(() => { copyTech.textContent = "复制技术细节"; }, 2000);
  } catch {
    announce("复制失败，请展开技术细节手动选择");
  }
});

byId("copy-demo").addEventListener("click", async () => {
  const command = byId("demo-command").textContent ?? "";
  const button = testId<HTMLButtonElement>("copy-demo");
  try {
    await navigator.clipboard.writeText(command);
    button.textContent = "已复制";
    announce("命令已复制到剪贴板");
    window.setTimeout(() => { button.textContent = "复制命令"; }, 2000);
  } catch {
    announce("复制失败，请手动选择命令文本");
  }
});

// ---------------------------------------------------------------------------
// Theme
// ---------------------------------------------------------------------------
const themeButton = testId<HTMLButtonElement>("theme-toggle");
const themeLabel = themeButton.querySelector<HTMLElement>(".theme-label");
const applyTheme = (theme: "light" | "dark"): void => {
  document.documentElement.dataset.theme = theme;
  const dark = theme === "dark";
  themeButton.setAttribute("aria-pressed", String(dark));
  themeButton.setAttribute("aria-label", dark ? "切换浅色模式" : "切换深色模式");
  if (themeLabel) themeLabel.textContent = dark ? "浅色模式" : "深色模式";
};
// Explicit choice wins; otherwise the operating system decides.
themeButton.setAttribute("aria-keyshortcuts", "t");
testId("shortcuts-open").setAttribute("aria-keyshortcuts", "?");
select.setAttribute("aria-keyshortcuts", "/");
const storedTheme = localStorage.getItem("astock-v2-theme");
const systemQuery = window.matchMedia("(prefers-color-scheme: dark)");
applyTheme(storedTheme === null ? (systemQuery.matches ? "dark" : "light")
  : storedTheme === "dark" ? "dark" : "light");
systemQuery.addEventListener("change", (event) => {
  if (localStorage.getItem("astock-v2-theme") !== null) return;
  applyTheme(event.matches ? "dark" : "light");
});
themeButton.addEventListener("click", () => {
  const next = document.documentElement.dataset.theme === "dark" ? "light" : "dark";
  localStorage.setItem("astock-v2-theme", next);
  applyTheme(next);
  announce(next === "dark" ? "已切换到深色模式" : "已切换到浅色模式");
});

// ---------------------------------------------------------------------------
// Experiment comparison (ADR 004): read-only juxtaposition of stored facts.
// No valuation, no return, no ranking, no writes.
// ---------------------------------------------------------------------------
type CompareFill = {
  symbol: string; side: string; quantity: number; price_cents: number;
  fees_cents: number; trade_day: string;
};
type CompareDecision = {
  run_id: number; signal_id: string; side: string; status: string;
  reason: string; fill: CompareFill | null;
};
type CompareSlot = { decision_at: string; a: CompareDecision[]; b: CompareDecision[] };
type CompareCheck = { name: string; matched: boolean; a: string; b: string };
type CompareTotals = {
  decisions: number; filled: number; rejected: number; fees_cents: number;
  cash_cents: number; positions: Array<{ symbol: string; quantity: number; sellable_quantity: number | null }>;
};
type CompareSide = {
  identity: {
    cycle_id: string; status: string; strategy_version_id: string; research_input_id: string;
    market_data_pack_id: string; execution_policy_version: string; initial_cash_cents: number;
  };
  verification: {
    replay_ok: boolean; runs_verified: number; fills_verified: number;
    reproduction_ok: boolean; signals_verified: number; detail: string;
  };
  totals: CompareTotals;
};
type CompareReport = {
  version: string; ok: boolean; reasons: string[];
  window: { start_day: string; end_day: string };
  a: CompareSide; b: CompareSide;
  comparability: CompareCheck[];
  slots: CompareSlot[];
  unmatched_a: number; unmatched_b: number;
  valuation: { status: string; reason: string };
  digest: string;
};

const COMPARE_REASONS: Record<string, string> = {
  comparison_research_input_mismatch: "研究输入不一致",
  comparison_market_data_pack_mismatch: "市场数据包不一致",
  comparison_execution_policy_mismatch: "执行规则或费用模型不一致",
  comparison_initial_cash_mismatch: "初始资金不一致",
  comparison_window_not_covered: "比较窗口没有被两个实验完整覆盖",
  comparison_replay_failed: "交易回放未通过",
  comparison_research_reproduction_failed: "研究重做未通过",
};
const COMPARE_CHECK_LABELS: Record<string, string> = {
  research_input: "研究输入",
  market_data_pack: "市场数据包",
  execution_policy: "执行规则",
  initial_cash: "初始资金",
};

let compareSeq = 0;
let compareKey = "";
const compareForm = byId<HTMLFormElement>("compare-form");
const compareError = testId<HTMLElement>("compare-error");
const compareResult = byId("compare-result");
const compareButton = testId<HTMLButtonElement>("compare-run");

const compareScope = () => ({
  a: compareA.value,
  b: compareB.value,
  start: byId<HTMLInputElement>("compare-start").value,
  end: byId<HTMLInputElement>("compare-end").value,
});

const clearCompareError = (): void => {
  compareError.replaceChildren();
  toggle(compareError, false);
};
const showCompareError = (message: string): void => {
  compareError.textContent = message;
  toggle(compareError, true);
};

const setCompareState = (
  state: "idle" | "running" | "ok" | "fail",
  message: string,
  detail: string,
): void => {
  const status = testId<HTMLElement>("compare-status");
  status.dataset.state = state;
  status.textContent = message;
  setText("compare-detail", detail);
  toggle(compareResult, state !== "idle");
};

const syncCompareUrl = (scope: { a: string; b: string; start: string; end: string }): void => {
  const url = new URL(location.href);
  const values: Array<[string, string]> = [["a", scope.a], ["b", scope.b], ["from", scope.start], ["to", scope.end]];
  for (const [key, value] of values) {
    if (value) url.searchParams.set(key, value);
    else url.searchParams.delete(key);
  }
  if (url.toString() !== location.href) history.replaceState(null, "", url);
};

const decisionCell = (facts: CompareDecision[]): HTMLTableCellElement => {
  const cell = document.createElement("td");
  cell.className = "compare-cell";
  if (facts.length === 0) {
    const empty = document.createElement("span");
    empty.className = "muted";
    empty.textContent = "无决策（不补造）";
    cell.appendChild(empty);
    return cell;
  }
  for (const fact of facts) {
    const line = document.createElement("div");
    line.className = "compare-fact";
    const side = document.createElement("span");
    side.className = fact.side === "buy" ? "side-buy" : "side-sell";
    side.textContent = sideLabel(fact.side);
    const status = document.createElement("span");
    status.className = fact.status === "filled" ? "result-filled" : "result-rejected";
    status.textContent = resultLabel(fact.status);
    const detail = document.createElement("span");
    detail.className = "compare-fact-detail";
    detail.textContent = fact.fill
      ? `${fact.fill.symbol} ${integer(fact.fill.quantity)} 股 @ ${money(fact.fill.price_cents)} · 费用 ${money(fact.fill.fees_cents)}`
      : reasonLabel(fact.reason);
    detail.title = fact.status === "filled" ? fact.signal_id : fact.reason;
    line.append(side, status, detail);
    cell.appendChild(line);
  }
  return cell;
};

const row = (label: string, a: string, b: string): HTMLTableRowElement => {
  const tr = document.createElement("tr");
  const th = document.createElement("th");
  th.scope = "row";
  th.textContent = label;
  const ca = document.createElement("td");
  ca.textContent = a;
  const cb = document.createElement("td");
  cb.textContent = b;
  tr.append(th, ca, cb);
  return tr;
};

const renderComparison = (report: CompareReport): void => {
  setCompareState(
    report.ok ? "ok" : "fail",
    report.ok
      ? `可比（${report.slots.length} 个决策时点${report.unmatched_a + report.unmatched_b > 0 ? "，含单边决策" : ""}）`
      : "不可比",
    report.ok ? `摘要 ${report.digest.slice(0, 12)}…` : "两侧事实不会并列展示，避免被误读为可比结果",
  );

  const reasonsWrap = byId("compare-reasons-wrap");
  const reasonsList = byId("compare-reasons");
  reasonsList.replaceChildren();
  for (const reason of report.reasons) {
    const item = document.createElement("li");
    item.textContent = COMPARE_REASONS[reason] ?? reason;
    reasonsList.appendChild(item);
  }
  toggle(reasonsWrap, report.reasons.length > 0);

  const checks = byId("compare-checks");
  checks.replaceChildren();
  for (const check of report.comparability) {
    const tr = document.createElement("tr");
    const th = document.createElement("th");
    th.scope = "row";
    th.textContent = COMPARE_CHECK_LABELS[check.name] ?? check.name;
    const shorten = (value: string): string =>
      value.length > 16 ? `${value.slice(0, 12)}…` : value;
    const ca = document.createElement("td");
    ca.textContent = shorten(check.a);
    ca.title = check.a;
    const cb = document.createElement("td");
    cb.textContent = shorten(check.b);
    cb.title = check.b;
    const verdict = document.createElement("td");
    verdict.textContent = check.matched ? "一致" : "不一致";
    verdict.className = check.matched ? "result-filled" : "result-rejected";
    tr.append(th, ca, cb, verdict);
    checks.appendChild(tr);
  }

  const verification = byId("compare-verification");
  verification.replaceChildren();
  for (const [label, side] of [["A", report.a], ["B", report.b]] as const) {
    const tr = document.createElement("tr");
    const th = document.createElement("th");
    th.scope = "row";
    th.textContent = label;
    const replay = document.createElement("td");
    replay.textContent = side.verification.replay_ok
      ? `通过（${side.verification.runs_verified} 次决策，${side.verification.fills_verified} 次成交）`
      : "未通过";
    replay.className = side.verification.replay_ok ? "result-filled" : "result-rejected";
    const repro = document.createElement("td");
    repro.textContent = side.verification.reproduction_ok
      ? `通过（${side.verification.signals_verified} 条研究信号）`
      : "未通过";
    repro.className = side.verification.reproduction_ok ? "result-filled" : "result-rejected";
    tr.append(th, replay, repro);
    verification.appendChild(tr);
  }

  const totals = byId("compare-totals");
  totals.replaceChildren();
  toggle(byId("compare-totals-wrap"), report.ok);
  toggle(byId("compare-slots-wrap"), report.ok);
  if (report.ok) {
    const formatPositions = (side: CompareSide): string =>
      side.totals.positions.length === 0
        ? "无持仓"
        : side.totals.positions.map((position) => `${position.symbol} ${integer(position.quantity)}`).join("；");
    totals.append(
      row("决策条数", integer(report.a.totals.decisions), integer(report.b.totals.decisions)),
      row("已成交 / 已拒绝",
        `${integer(report.a.totals.filled)} / ${integer(report.a.totals.rejected)}`,
        `${integer(report.b.totals.filled)} / ${integer(report.b.totals.rejected)}`),
      row("费用合计（窗口内成交）", money(report.a.totals.fees_cents), money(report.b.totals.fees_cents)),
      row("现金（窗口末）", money(report.a.totals.cash_cents), money(report.b.totals.cash_cents)),
      row("持仓数量（窗口末）", formatPositions(report.a), formatPositions(report.b)),
    );

    const slots = byId("compare-slots");
    slots.replaceChildren();
    for (const slot of report.slots) {
      const tr = document.createElement("tr");
      const time = document.createElement("td");
      time.className = "compare-time";
      time.textContent = decisionTime(slot.decision_at);
      tr.append(time, decisionCell(slot.a), decisionCell(slot.b));
      slots.appendChild(tr);
    }
    setById("compare-unmatched",
      `单边决策：A ${report.unmatched_a} 条 · B ${report.unmatched_b} 条（逐条标注，不补造另一方交易）`);
  }

  const valuation = testId<HTMLElement>("compare-valuation");
  toggle(valuation, report.ok);
  valuation.textContent = report.valuation.status === "unavailable"
    ? "估值不可用：数据包未声明审核过的盯市价格集合，因此不计算收益、净值、回撤或策略排名。"
    : "估值可用：价格来源与版本见比较摘要。";

  setText("compare-version", report.version);
  setText("compare-window", `${report.window.start_day} ~ ${report.window.end_day}（中国时间，含首尾）`);
  setText("compare-digest", report.digest);
  const identityText = (side: CompareSide): string =>
    `${side.identity.cycle_id} · ${strategyName(side.identity.strategy_version_id)} · ` +
    `${packName(side.identity.market_data_pack_id)} · ${policyName(side.identity.execution_policy_version)}`;
  setText("compare-identity-a", identityText(report.a));
  setText("compare-identity-b", identityText(report.b));
};

const runComparison = async (): Promise<void> => {
  const scope = compareScope();
  clearCompareError();
  if (!scope.start || !scope.end) {
    showCompareError("请先选择比较窗口的开始日与结束日");
    announce("请先选择比较窗口");
    return;
  }
  if (scope.a === scope.b) {
    showCompareError("实验 A 与实验 B 不能是同一个实验");
    announce("同一个实验无法比较");
    return;
  }
  compareKey = `${scope.a}|${scope.b}|${scope.start}|${scope.end}`;
  const seq = ++compareSeq;
  compareButton.disabled = true;
  compareButton.setAttribute("aria-busy", "true");
  setCompareState("running", "正在比较…", "");
  announce("开始比较两个实验");
  const query = "/v2/comparisons" +
    `?cycle_a=${encodeURIComponent(scope.a)}` +
    `&cycle_b=${encodeURIComponent(scope.b)}` +
    `&start_day=${encodeURIComponent(scope.start)}` +
    `&end_day=${encodeURIComponent(scope.end)}`;
  try {
    const report = await api<CompareReport>(query, new AbortController().signal);
    if (seq !== compareSeq) return;
    syncCompareUrl(scope);
    renderComparison(report);
    announce(report.ok
      ? `比较完成，${report.slots.length} 个决策时点`
      : "两个实验不可比");
  } catch (error) {
    if (seq !== compareSeq) return;
    setCompareState("fail", `比较失败：${errorDetail(error)}`, "");
    announce("比较失败");
  } finally {
    if (seq === compareSeq) {
      compareButton.disabled = false;
      compareButton.removeAttribute("aria-busy");
    }
  }
};

compareForm.addEventListener("submit", (event) => {
  event.preventDefault();
  void runComparison();
});

/** Prefill and auto-run when the deep link carries a full comparison scope. */
const hydrateComparison = (): void => {
  const params = new URLSearchParams(location.search);
  const a = params.get("a");
  const b = params.get("b");
  const from = params.get("from");
  const to = params.get("to");
  if (!a || !b || !from || !to) return;
  if (!experiments.some((experiment) => experiment.cycle_id === a)) return;
  if (!experiments.some((experiment) => experiment.cycle_id === b)) return;
  if (`${a}|${b}|${from}|${to}` === compareKey) return;
  compareA.value = a;
  compareB.value = b;
  byId<HTMLInputElement>("compare-start").value = from;
  byId<HTMLInputElement>("compare-end").value = to;
  void runComparison();
};

// ---------------------------------------------------------------------------
// Keyboard shortcuts
// ---------------------------------------------------------------------------
const sheet = byId<HTMLDialogElement>("shortcuts");
const sheetClose = testId<HTMLButtonElement>("shortcuts-close");
const workspaceVisible = (): boolean => !byId("workspace").hidden;

const openSheet = (): void => {
  if (sheet.open) return;
  sheet.showModal();
  announce("已打开快捷键说明");
};
const closeSheet = (): void => {
  if (!sheet.open) return;
  sheet.close();
};
testId("shortcuts-open").addEventListener("click", openSheet);
sheetClose.addEventListener("click", closeSheet);
sheet.addEventListener("close", () => announce("已关闭快捷键说明"));

const VIEW_ORDER = Object.keys(VIEWS) as View[];
const isTypingTarget = (target: EventTarget | null): boolean => {
  const node = target as HTMLElement | null;
  if (!node || !node.tagName) return false;
  const tag = node.tagName;
  return tag === "INPUT" || tag === "SELECT" || tag === "TEXTAREA" || node.isContentEditable;
};

window.addEventListener("keydown", (event) => {
  if (event.defaultPrevented || event.metaKey || event.ctrlKey || event.altKey) return;
  if (isTypingTarget(event.target) || sheet.open) return;
  const key = event.key;

  if (key === "?") {
    event.preventDefault();
    openSheet();
    return;
  }
  if (key === "/") {
    event.preventDefault();
    select.focus();
    announce("已聚焦实验选择器");
    return;
  }
  if (!workspaceVisible()) return;
  if (key >= "1" && key <= "9") {
    const view = VIEW_ORDER[Number(key) - 1];
    if (view && view !== currentView) {
      event.preventDefault();
      showView(view);
    }
    return;
  }
  if (key.toLowerCase() === "r") {
    event.preventDefault();
    byId<HTMLButtonElement>("refresh").click();
    return;
  }
  if (key.toLowerCase() === "t") {
    event.preventDefault();
    themeButton.click();
  }
});

// ---------------------------------------------------------------------------
// Boot
// ---------------------------------------------------------------------------
showOnly(null);
showView(currentView, { updateUrl: false });
void loadList();

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
  experiment: { panel: "panel-experiment", label: "实验信息" },
  account: { panel: "panel-account", label: "模拟账户" },
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
  if (options.updateUrl !== false && location.hash !== `#/${view}`) {
    history.replaceState(null, "", `#/${view}`);
    // Switching panels from a scrolled page would otherwise land mid-table.
    if (window.scrollY > 0) {
      const reduce = window.matchMedia("(prefers-reduced-motion: reduce)").matches;
      byId("main").scrollIntoView({ behavior: reduce ? "auto" : "smooth", block: "start" });
    }
  }
  if (view === "account" && accounts.length === 0) void loadAccounts();
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

const renderSelector = (): void => {
  select.replaceChildren();
  for (const experiment of experiments) {
    const option = document.createElement("option");
    option.value = experiment.cycle_id;
    option.textContent = experiment.cycle_id;
    select.appendChild(option);
  }
  select.disabled = experiments.length === 0;
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
  await loadAccounts();
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
// Paper account console (ADR 005): read-only facts for one account.
// ---------------------------------------------------------------------------
type PaperAccountSummarySlot = { slot: string; strategy_version_id: string; weight_bps: number };
type PaperAccountSummary = {
  account_id: string; initial_cash_cents: number; reserve_bps: number;
  created_at: string; slots: PaperAccountSummarySlot[];
};
type PaperAccountSlotView = {
  slot: string; strategy_version_id: string; research_input_id: string;
  market_data_pack_id: string; execution_policy_version: string;
  weight_bps: number; budget_cents: number; cash_cents: number;
  positions: Array<{ symbol: string; quantity: number; sellable_quantity: number | null }>;
  orders_total: number;
};
type PaperRiskLimits = {
  max_symbol_weight_bps: number; max_order_notional_bps: number; min_slot_cash_bps: number;
};
type PaperAccountView = {
  account_id: string; initial_cash_cents: number; created_at: string;
  cash_cents: number; reserve_cents: number; slots: PaperAccountSlotView[];
  valuation: { status: string; reason: string };
  limits: PaperRiskLimits;
};
type PaperFill = {
  symbol: string; side: string; quantity: number; price_cents: number;
  fees_cents: number; trade_day: string;
};
type PaperOrderRecord = {
  id: number; account_id: string; slot: string; strategy_version_id: string;
  request: {
    schema_version: number; account_id: string; slot: string; signal_id: string;
    decision_at: string; signal: { symbol: string; side: string };
    quote_ref: { market_data_pack_id: string; source: string; quote_id: string };
  };
  outcome: { status: string; reason: string; fill: PaperFill | null; cash_after_cents: number };
  duplicate: boolean;
};

type PaperValuationReport = {
  account_id: string; as_of_day: string; status: string; reason: string;
  requirements: Array<{ slot: string; market_data_pack_id: string; symbols: string[] }>;
  missing: string[]; priced_symbols: number; digest: string;
  valuation?: {
    trust: string; price_set_id: string; market_value_cents: number;
    cash_cents: number; equity_cents: number; profit_cents: number | null;
  } | null;
};

type PaperAuditReport = {
  account_id: string; ok: boolean;
  checks: Array<{ name: string; ok: boolean; detail: string }>;
  digest: string;
};

type PaperSessionView = {
  session_id: string; account_id: string; status: string;
  clock_at: string; cursor: number; total: number; last_order_id: number | null;
};

type StrategyCatalogue = {
  strategies: Array<{ strategy_version_id: string; research_input_id: string; research_input_digest: string }>;
  market_data_pack_id: string;
  execution_policy_version: string;
  reserve_bps: number;
};
type PaperSlotRequest = {
  slot: string; strategy_version_id: string; market_data_pack_id: string;
  execution_policy_version: string; weight_bps: number;
};

let strategyCatalogue: StrategyCatalogue | null = null;
let accounts: PaperAccountSummary[] = [];
let lastAccountView: PaperAccountView | null = null;
let currentAccountId = "";
const accountSelect = byId<HTMLSelectElement>("account-select");
const accountError = testId<HTMLElement>("account-error");
const accountBody = byId("account-body");
const accountEmpty = testId<HTMLElement>("account-empty");
const createForm = byId<HTMLFormElement>("account-create");
const createToggle = testId<HTMLButtonElement>("account-create-toggle");
const sessionForm = byId<HTMLFormElement>("session-create");
const sessionToggle = testId<HTMLButtonElement>("session-create-toggle");
const sessionIdInput = byId<HTMLInputElement>("session-id");
const sessionSlot = byId<HTMLSelectElement>("session-slot");
const sessionCycle = byId<HTMLSelectElement>("session-cycle");
const sessionHint = testId<HTMLElement>("session-hint");
const sessionSubmit = testId<HTMLButtonElement>("session-submit");

const urlAccount = (): string =>
  new URLSearchParams(location.search).get("account")?.trim() ?? "";

const syncAccountUrl = (accountId: string): void => {
  const url = new URL(location.href);
  if (accountId) url.searchParams.set("account", accountId);
  else url.searchParams.delete("account");
  if (url.toString() !== location.href) history.replaceState(null, "", url);
};

const showAccountError = (message: string): void => {
  accountError.textContent = message;
  toggle(accountError, true);
};
const clearAccountError = (): void => {
  accountError.replaceChildren();
  toggle(accountError, false);
};

const accountSlotCard = (slot: PaperAccountSlotView, limits: PaperRiskLimits): HTMLElement => {
  const card = document.createElement("section");
  card.className = "account-slot";
  card.dataset.testid = `account-slot-${slot.slot}`;
  const head = document.createElement("header");
  head.className = "account-slot-head";
  const title = document.createElement("h3");
  title.textContent = `槽位 ${slot.slot} · ${strategyName(slot.strategy_version_id)}`;
  const weight = document.createElement("span");
  weight.className = "tag";
  weight.textContent = `${(slot.weight_bps / 100).toFixed(1)}%`;
  head.append(title, weight);

  const facts = document.createElement("dl");
  facts.className = "account-slot-facts";
  const rows: Array<[string, string]> = [
    ["预算", money(slot.budget_cents)],
    ["现金", money(slot.cash_cents)],
    ["订单", integer(slot.orders_total)],
  ];
  for (const [label, value] of rows) {
    const item = document.createElement("div");
    const dt = document.createElement("dt");
    dt.textContent = label;
    const dd = document.createElement("dd");
    dd.textContent = value;
    item.append(dt, dd);
    facts.appendChild(item);
  }

  const risk = document.createElement("p");
  risk.className = "account-slot-risk";
  risk.textContent =
    `风控：单笔 ≤ ${(limits.max_order_notional_bps / 100).toFixed(0)}% · ` +
    `单标的 ≤ ${(limits.max_symbol_weight_bps / 100).toFixed(0)}% · ` +
    `现金 ≥ ${(limits.min_slot_cash_bps / 100).toFixed(0)}%`;

  const positions = document.createElement("p");
  positions.className = "account-slot-positions";
  positions.textContent = slot.positions.length === 0
    ? "无持仓"
    : slot.positions.map((position) => `${position.symbol} ${integer(position.quantity)} 股`).join("；");

  card.append(head, facts, risk, positions);
  return card;
};

const renderAccount = (account: PaperAccountView, orders: PaperOrderRecord[]): void => {
  lastAccountView = account;
  toggle(accountBody, true);
  toggle(accountEmpty, false);
  setText("account-initial", money(account.initial_cash_cents));
  setText("account-reserve", money(account.reserve_cents));
  setText("account-cash", money(account.cash_cents));
  const valuation = testId<HTMLElement>("account-valuation");
  toggle(valuation, true);
  valuation.textContent = account.valuation.status === "unavailable"
    ? "估值不可用：没有审核过的市价集合，本页不计算市值、净值或收益。"
    : "估值可用：价格来源与版本见账户摘要。";

  const slots = byId("account-slots");
  slots.replaceChildren(...account.slots.map((slot) => accountSlotCard(slot, account.limits)));

  const body = byId("account-orders");
  body.replaceChildren();
  for (const order of orders) {
    const row = document.createElement("tr");
    const time = document.createElement("td");
    time.className = "compare-time";
    time.textContent = decisionTime(order.request.decision_at);
    const slot = document.createElement("td");
    slot.textContent = order.slot;
    const signal = document.createElement("td");
    signal.textContent = signalName(order.request.signal_id);
    signal.title = order.request.signal_id;
    const side = document.createElement("td");
    const sidePill = document.createElement("span");
    sidePill.className = order.request.signal.side === "buy" ? "side-buy" : "side-sell";
    sidePill.textContent = sideLabel(order.request.signal.side);
    side.appendChild(sidePill);
    const result = document.createElement("td");
    const resultPill = document.createElement("span");
    resultPill.className = order.outcome.status === "filled" ? "result-filled" : "result-rejected";
    resultPill.textContent = resultLabel(order.outcome.status);
    result.appendChild(resultPill);
    const detail = document.createElement("td");
    detail.className = "wrap";
    if (order.outcome.fill) {
      const fill = order.outcome.fill;
      detail.textContent =
        `${fill.symbol} ${integer(fill.quantity)} 股 @ ${money(fill.price_cents)} · 费用 ${money(fill.fees_cents)}`;
    } else {
      detail.textContent = reasonLabel(order.outcome.reason);
      detail.title = order.outcome.reason;
    }
    row.append(time, slot, signal, side, result, detail);
    body.appendChild(row);
  }
  toggle(testId("account-orders-empty"), orders.length === 0);
};

const postJson = async <T>(path: string, body: unknown): Promise<T> => {
  let response: Response;
  try {
    response = await fetch(path, {
      method: "POST",
      headers: { "Content-Type": "application/json", Accept: "application/json" },
      body: JSON.stringify(body),
    });
  } catch (error) {
    throw new ApiError(null, error instanceof Error ? error.message : String(error));
  }
  if (!response.ok) {
    const text = await response.text().catch(() => "");
    throw new ApiError(response.status, text.slice(0, 400));
  }
  return (await response.json()) as T;
};

const SESSION_STATUS: Record<string, string> = {
  created: "未开始", running: "运行中", paused: "已暂停",
  stopped: "已停止", completed: "已完成",
};

const sessionCard = (session: PaperSessionView): HTMLElement => {
  const card = document.createElement("article");
  card.className = "account-session";
  card.dataset.testid = `session-card-${session.session_id}`;

  const head = document.createElement("header");
  head.className = "account-session-head";
  const title = document.createElement("strong");
  title.textContent = session.session_id;
  const status = document.createElement("span");
  status.className = "pill";
  status.dataset.state = session.status === "running" ? "active" : session.status === "paused" ? "completed" : "idle";
  status.textContent = SESSION_STATUS[session.status] ?? session.status;
  head.append(title, status);

  const meta = document.createElement("p");
  meta.className = "account-session-meta";
  meta.textContent = `时钟 ${decisionTime(session.clock_at)} · 进度 ${session.cursor}/${session.total}`;

  const actions = document.createElement("div");
  actions.className = "account-session-actions";
  const add = (action: string, label: string, primary = false): void => {
    const button = document.createElement("button");
    button.type = "button";
    button.className = primary ? "btn btn-small" : "btn btn-quiet btn-small";
    button.dataset.action = action;
    button.textContent = label;
    button.addEventListener("click", () => {
      button.disabled = true;
      button.setAttribute("aria-busy", "true");
      void sessionAction(session.session_id, action);
    });
    actions.appendChild(button);
  };
  if (session.status === "created") add("start", "启动", true);
  if (session.status === "running") {
    add("pause", "暂停");
    add("run", "运行到结束", true);
  }
  if (session.status === "running") {
    const advanceWrap = document.createElement("div");
    advanceWrap.className = "session-advance";
    const date = document.createElement("input");
    date.type = "date";
    date.dataset.testid = "session-advance-date";
    date.setAttribute("aria-label", "推进到的交易日");
    const advanceButton = document.createElement("button");
    advanceButton.type = "button";
    advanceButton.className = "btn btn-quiet btn-small";
    advanceButton.dataset.testid = "session-advance";
    advanceButton.textContent = "推进到该日";
    advanceButton.addEventListener("click", () => {
      if (!date.value) {
        showAccountError("请先选择要推进到的交易日");
        return;
      }
      advanceButton.disabled = true;
      advanceButton.setAttribute("aria-busy", "true");
      void sessionAdvance(session.session_id, date.value);
    });
    advanceWrap.append(date, advanceButton);
    actions.appendChild(advanceWrap);
  }
  if (session.status === "paused") {
    add("resume", "继续", true);
    add("run", "运行到结束");
  }
  if (session.status === "running" || session.status === "paused") add("stop", "停止");

  card.append(head, meta, actions);
  return card;
};

const renderSessions = (sessions: PaperSessionView[]): void => {
  const container = byId("account-sessions");
  container.replaceChildren(...sessions.map(sessionCard));
  toggle(testId("account-sessions-empty"), sessions.length === 0);
};

const sessionAdvance = async (sessionId: string, day: string): Promise<void> => {
  clearAccountError();
  try {
    await postJson(`/v2/paper-sessions/${encodeURIComponent(sessionId)}/advance`, { day });
    announce(`会话 ${sessionId} 已推进到 ${day}`);
    await loadAccount(currentAccountId);
  } catch (error) {
    showAccountError(`推进会话失败：${errorDetail(error)}`);
    announce("推进会话失败");
  }
};

const sessionAction = async (sessionId: string, action: string): Promise<void> => {
  clearAccountError();
  try {
    await postJson(`/v2/paper-sessions/${encodeURIComponent(sessionId)}/${action}`, {});
    announce(`会话 ${sessionId} 已执行 ${action}`);
    await loadAccount(currentAccountId);
  } catch (error) {
    showAccountError(`会话操作失败：${errorDetail(error)}`);
    announce("会话操作失败");
  }
};

const chinaDayOf = (value: string): string => {
  const date = new Date(value);
  if (Number.isNaN(date.getTime())) return "";
  return new Intl.DateTimeFormat("en-CA", {
    timeZone: "Asia/Shanghai", year: "numeric", month: "2-digit", day: "2-digit",
  }).format(date);
};

const renderValuation = (report: PaperValuationReport | null, asOf: string): void => {
  const detail = testId<HTMLElement>("account-valuation-detail");
  const banner = testId<HTMLElement>("account-valuation");
  if (!report || !asOf) {
    toggle(detail, false);
    return;
  }
  toggle(detail, true);
  if (report.status === "valued" && report.valuation) {
    const valued = report.valuation;
    banner.textContent = valued.trust === "reviewed"
      ? "估值可用：使用审核价格集合。"
      : "估值可用：使用合成价格集合（不计算盈亏）。";
    const profit = valued.profit_cents;
    detail.textContent =
      `估值快照（截至 ${asOf}）：市值 ${money(valued.market_value_cents)} · ` +
      `权益 ${money(valued.equity_cents)} · 现金 ${money(valued.cash_cents)}` +
      (profit === null ? " · 不计算盈亏" : ` · 盈亏 ${money(profit)}`) +
      ` · 价格集合 ${valued.price_set_id}`;
    return;
  }
  banner.textContent = "估值不可用：没有审核过的市价集合，本页不计算市值、净值或收益。";
  if (report.status === "unavailable") {
    const needs = report.requirements
      .flatMap((requirement) => requirement.symbols.map((symbol) => `${symbol}（${packName(requirement.market_data_pack_id)}）`));
    detail.textContent = `估值快照（截至 ${asOf}）：不可用 · 缺少 ${report.missing.join("、")}` +
      (needs.length > 0 ? ` · 需要 ${needs.join("、")}` : " · 当前无持仓");
  } else {
    detail.textContent = `估值快照（截至 ${asOf}）：${report.status}`;
  }
};

const renderAudit = (report: PaperAuditReport | null): void => {
  const line = testId<HTMLElement>("account-audit");
  if (!report) {
    toggle(line, false);
    toggle(testId<HTMLDetailsElement>("account-audit-details"), false);
    return;
  }
  toggle(line, true);
  line.dataset.state = report.ok ? "ok" : "fail";
  const failed = report.checks.filter((check) => !check.ok).map((check) => check.name);
  line.textContent = report.ok
    ? `账本自检：通过（${report.checks.length} 项）· 摘要 ${report.digest.slice(0, 12)}…`
    : `账本自检：失败（${failed.join("、")}）· 摘要 ${report.digest.slice(0, 12)}…`;
  const details = testId<HTMLDetailsElement>("account-audit-details");
  const list = testId<HTMLUListElement>("account-audit-checks");
  toggle(details, true);
  list.replaceChildren(...report.checks.map((check) => {
    const item = document.createElement("li");
    item.dataset.state = check.ok ? "ok" : "fail";
    item.textContent = `${check.ok ? "✓" : "✗"} ${check.name}：${check.detail}`;
    return item;
  }));
};

const loadAccount = async (accountId: string): Promise<void> => {
  if (!accountId) return;
  clearAccountError();
  currentAccountId = accountId;
  const encoded = encodeURIComponent(accountId);
  try {
    const [account, orders, sessions] = await Promise.all([
      api<PaperAccountView>(`/v2/paper-accounts/${encoded}`, new AbortController().signal),
      api<PaperOrderRecord[]>(`/v2/paper-accounts/${encoded}/orders`, new AbortController().signal),
      api<PaperSessionView[]>(
        `/v2/paper-sessions?account_id=${encoded}`,
        new AbortController().signal,
      ),
    ]);
    if (account.account_id !== accountId) {
      showAccountError("返回的账户与请求不一致");
      return;
    }
    syncAccountUrl(accountId);
    const orderList = Array.isArray(orders) ? orders : [];
    renderAccount(account, orderList);
    renderSessions(Array.isArray(sessions) ? sessions : []);
    const asOf = orderList.length > 0
      ? chinaDayOf(orderList[0].request.decision_at)
      : chinaDayOf(account.created_at);
    if (asOf) {
      try {
        const valuation = await api<PaperValuationReport>(
          `/v2/paper-accounts/${encoded}/valuation?as_of_day=${encodeURIComponent(asOf)}`,
          new AbortController().signal,
        );
        if (currentAccountId === accountId) renderValuation(valuation, asOf);
      } catch {
        if (currentAccountId === accountId) renderValuation(null, asOf);
      }
    } else {
      renderValuation(null, "");
    }
    try {
      const audit = await api<PaperAuditReport>(
        `/v2/paper-accounts/${encoded}/audit`,
        new AbortController().signal,
      );
      if (currentAccountId === accountId) renderAudit(audit);
    } catch {
      if (currentAccountId === accountId) renderAudit(null);
    }
  } catch (error) {
    toggle(accountBody, false);
    showAccountError(`读取账户失败：${errorDetail(error)}`);
  }
};

const loadStrategies = async (): Promise<void> => {
  if (strategyCatalogue) return;
  try {
    const catalogue = await api<StrategyCatalogue>("/v2/strategies", new AbortController().signal);
    strategyCatalogue = catalogue;
    for (const id of ["create-a", "create-b"]) {
      const select = byId<HTMLSelectElement>(id);
      select.replaceChildren();
      for (const strategy of catalogue.strategies) {
        const option = document.createElement("option");
        option.value = strategy.strategy_version_id;
        option.textContent = strategyName(strategy.strategy_version_id);
        select.appendChild(option);
      }
    }
    const strategies = catalogue.strategies;
    if (strategies.length > 1) byId<HTMLSelectElement>("create-b").value = strategies[1].strategy_version_id;
  } catch (error) {
    showAccountError(`读取策略目录失败：${errorDetail(error)}`);
  }
};

const openCreateForm = async (): Promise<void> => {
  await loadStrategies();
  toggle(createForm, true);
  toggle(createToggle, false);
  byId<HTMLInputElement>("create-id").focus();
};

const closeCreateForm = (): void => {
  toggle(createForm, false);
  toggle(createToggle, true);
};

createToggle.addEventListener("click", () => void openCreateForm());
testId("create-cancel").addEventListener("click", closeCreateForm);

createForm.addEventListener("submit", (event) => {
  event.preventDefault();
  void (async () => {
    const accountId = byId<HTMLInputElement>("create-id").value.trim();
    const cashYuan = Number(byId<HTMLInputElement>("create-cash").value);
    const slotA = byId<HTMLSelectElement>("create-a").value;
    const slotB = byId<HTMLSelectElement>("create-b").value;
    clearAccountError();
    if (!accountId) {
      showAccountError("请填写账户标识");
      return;
    }
    if (!Number.isFinite(cashYuan) || cashYuan < 100) {
      showAccountError("初始资金至少 100 元");
      return;
    }
    if (slotA === slotB) {
      showAccountError("两个槽位不能使用同一策略");
      return;
    }
    if (!strategyCatalogue) {
      showAccountError("策略目录尚未载入");
      return;
    }
    const weight = 4_500;
    const slots: PaperSlotRequest[] = [
      {
        slot: "A", strategy_version_id: slotA,
        market_data_pack_id: strategyCatalogue.market_data_pack_id,
        execution_policy_version: strategyCatalogue.execution_policy_version,
        weight_bps: weight,
      },
      {
        slot: "B", strategy_version_id: slotB,
        market_data_pack_id: strategyCatalogue.market_data_pack_id,
        execution_policy_version: strategyCatalogue.execution_policy_version,
        weight_bps: weight,
      },
    ];
    const button = testId<HTMLButtonElement>("create-submit");
    button.disabled = true;
    try {
      await postJson<PaperAccountView>("/v2/paper-accounts", {
        schema_version: 1,
        account_id: accountId,
        initial_cash_cents: Math.round(cashYuan * 100),
        reserve_bps: strategyCatalogue.reserve_bps,
        created_at: new Date().toISOString(),
        slots,
        limits: {
          max_symbol_weight_bps: 2_500,
          max_order_notional_bps: 5_000,
          min_slot_cash_bps: 1_000,
        },
      });
      announce(`账户 ${accountId} 已创建`);
      closeCreateForm();
      byId<HTMLInputElement>("create-id").value = "";
      await loadAccounts();
      const created = accounts.find((account) => account.account_id === accountId);
      if (created) {
        accountSelect.value = created.account_id;
        await loadAccount(created.account_id);
      }
    } catch (error) {
      showAccountError(`创建账户失败：${errorDetail(error)}`);
      announce("创建账户失败");
    } finally {
      button.disabled = false;
    }
  })();
});

const slotStrategy = (slot: string): string =>
  lastAccountView?.slots.find((candidate) => candidate.slot === slot)?.strategy_version_id ?? "";

const refreshSessionOptions = (): void => {
  const strategy = slotStrategy(sessionSlot.value);
  const matching = experiments.filter((experiment) => experiment.strategy_version_id === strategy);
  sessionCycle.replaceChildren();
  for (const experiment of matching) {
    const option = document.createElement("option");
    option.value = experiment.cycle_id;
    option.textContent = experiment.cycle_id;
    sessionCycle.appendChild(option);
  }
  sessionCycle.disabled = matching.length === 0;
  sessionSubmit.disabled = matching.length === 0;
  toggle(sessionHint, matching.length === 0);
  if (matching.length === 0) {
    sessionHint.textContent = "当前槽位的策略没有可用实验；换一个槽位或先建立匹配的实验。";
  } else if (!sessionIdInput.value.trim()) {
    sessionIdInput.value = `sess-${matching[0].cycle_id}`;
  }
};

sessionToggle.addEventListener("click", () => {
  refreshSessionOptions();
  toggle(sessionForm, true);
  toggle(sessionToggle, false);
  sessionIdInput.focus();
});
testId("session-cancel").addEventListener("click", () => {
  toggle(sessionForm, false);
  toggle(sessionToggle, true);
});
sessionSlot.addEventListener("change", refreshSessionOptions);

sessionForm.addEventListener("submit", (event) => {
  event.preventDefault();
  void (async () => {
    const sessionId = sessionIdInput.value.trim();
    const cycleId = sessionCycle.value;
    clearAccountError();
    if (!sessionId) {
      showAccountError("请填写会话标识");
      return;
    }
    if (!cycleId) {
      showAccountError("没有可用的信号来源实验");
      return;
    }
    sessionSubmit.disabled = true;
    try {
      await postJson("/v2/paper-sessions/from-experiment", {
        session_id: sessionId,
        account_id: currentAccountId,
        cycle_id: cycleId,
        slot: sessionSlot.value,
      });
      announce(`会话 ${sessionId} 已创建`);
      toggle(sessionForm, false);
      toggle(sessionToggle, true);
      sessionIdInput.value = "";
      await loadAccount(currentAccountId);
    } catch (error) {
      showAccountError(`创建会话失败：${errorDetail(error)}`);
      announce("创建会话失败");
    } finally {
      sessionSubmit.disabled = false;
    }
  })();
});

const loadAccounts = async (): Promise<void> => {
  clearAccountError();
  try {
    const list = await api<PaperAccountSummary[]>("/v2/paper-accounts", new AbortController().signal);
    accounts = Array.isArray(list) ? list : [];
  } catch (error) {
    accounts = [];
    toggle(accountBody, false);
    showAccountError(`读取账户列表失败：${errorDetail(error)}`);
    return;
  }
  accountSelect.replaceChildren();
  for (const account of accounts) {
    const option = document.createElement("option");
    option.value = account.account_id;
    option.textContent = account.account_id;
    accountSelect.appendChild(option);
  }
  accountSelect.disabled = accounts.length === 0;
  if (accounts.length === 0) {
    toggle(accountBody, false);
    toggle(accountEmpty, true);
    return;
  }
  toggle(accountEmpty, false);
  const requested = urlAccount();
  const selected = accounts.some((account) => account.account_id === requested)
    ? requested
    : accounts[0].account_id;
  accountSelect.value = selected;
  await loadAccount(selected);
};

accountSelect.addEventListener("change", () => {
  const selected = accountSelect.value;
  if (selected) void loadAccount(selected);
});

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

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

type ReplayReport = { ok: boolean; runs_verified: number; fills_verified: number; cash_cents: number };
type ReproductionReport = { ok: boolean; signals_verified: number };

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
  announce(`已切换到${VIEWS[view].label}`);
};

const showOnly = (panel: "service" | "empty" | "unknown" | "workspace" | "loading" | null): void => {
  const active = document.activeElement as HTMLElement | null;
  toggle(testId("loading"), panel === "loading");
  toggle(testId("service-error"), panel === "service");
  toggle(testId("empty-experiments"), panel === "empty");
  toggle(testId("unknown-experiment"), panel === "unknown");
  toggle(byId("context-bar"), panel === "workspace");
  toggle(byId("workspace"), panel === "workspace");
  // Nothing to navigate to while a notice owns the screen.
  const navEnabled = panel === "workspace";
  for (const button of navButtons) button.disabled = !navEnabled;
  if (active && active !== document.body && !isVisible(active)) {
    const region = panel === "workspace" ? byId("workspace")
      : panel ? testId(`${panel === "service" ? "service-error" : panel === "empty" ? "empty-experiments" : panel === "unknown" ? "unknown-experiment" : "loading"}`)
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
let asofDay: string | null = null;
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
  setVerify("replay", "idle", "未验证", "");
  setVerify("reproduction", "idle", "未验证", "");
  asofDay = null;
  (byId<HTMLInputElement>("asof-day")).value = "";
  toggle(testId("sellable-header"), false);
  setById("positions-count", "按标的汇总的剩余数量");
};

const describeCatalogue = (selected: string): string => {
  const parts = [`共 ${listTotal} 个实验`];
  if (experiments.length < listTotal) parts.push(`显示前 ${experiments.length} 个`);
  if (listSkipped > 0) parts.push(`${listSkipped} 个数据损坏已跳过`);
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

const renderPortfolio = (portfolio: Portfolio): void => {
  setText("kpi-initial-cash", money(portfolio.initial_cash_cents));
  setText("kpi-cash", money(portfolio.cash_cents));
  setText("kpi-positions", integer(portfolio.positions.length));
  setText("kpi-runs", integer(portfolio.runs_total));
  lastPositions = portfolio.positions;
  renderPositions();
};

const renderPositions = (): void => {
  const body = byId("positions-body");
  body.replaceChildren();
  toggle(testId("sellable-header"), asofDay !== null);
  setById("positions-count", asofDay === null
    ? `共 ${lastPositions.length} 个标的 · 未指定日终口径`
    : `共 ${lastPositions.length} 个标的 · 日终口径 ${asofDay}`);
  if (asofDay !== null) {
    const note = document.createElement("tr");
    note.className = "asof-row";
    const cell = document.createElement("td");
    cell.colSpan = 3;
    cell.textContent = `日终口径 ${asofDay} · 可卖数量按该日计算`;
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
    if (asofDay !== null) {
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
    time.textContent = decisionTime(run.request.decision_at);
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
  paintRuns();
};

const setVerify = (
  kind: "replay" | "reproduction",
  state: "idle" | "running" | "ok" | "fail",
  message: string,
  detail: string,
): void => {
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

const loadCycle = async (cycle: string, options: { keepContent?: boolean } = {}): Promise<void> => {
  const sequence = ++loadSequence;
  cycleController?.abort();
  const controller = new AbortController();
  cycleController = controller;
  currentCycle = cycle;
  asofSequence++;
  if (!rendered) showOnly("loading");
  if (!options.keepContent) resetWorkspace();
  setBusy(true);
  if (options.keepContent) announce("正在刷新…");

  const encoded = encodeURIComponent(cycle);
  const results = await Promise.allSettled([
    api<ExperimentDetail>(`/v2/experiments/${encoded}`, controller.signal),
    api<Portfolio>(`/v2/portfolio?cycle_id=${encoded}`, controller.signal),
    api<Run[]>(`/v2/simulations?cycle_id=${encoded}`, controller.signal),
  ]);
  if (sequence !== loadSequence) return;

  setBusy(false);
  const [detail, portfolio, runs] = results;

  if (detail.status === "rejected") {
    const error = detail.reason;
    if (error instanceof ApiError && error.status === 404) {
      byId("unknown-cycle").textContent = cycle;
      showOnly("unknown");
      announce("实验不存在");
      return;
    }
    showListFailure(`读取实验失败：${errorDetail(error)}`);
    return;
  }

  rendered = true;
  showOnly("workspace");
  showView(currentView);
  renderSummary(detail.value);
  setById("last-refresh", new Date().toLocaleTimeString("zh-CN", { hour12: false }));

  if (portfolio.status === "fulfilled") {
    renderPortfolio(portfolio.value);
  } else {
    setText("kpi-cash", "读取失败");
    setText("kpi-positions", "读取失败");
    setText("kpi-runs", "读取失败");
    showSectionError("positions", `资产读取失败：${errorDetail(portfolio.reason)}`,
      () => void loadCycle(currentCycle, { keepContent: true }));
  }

  if (runs.status === "fulfilled") {
    renderRuns(runs.value);
  } else {
    showSectionError("runs", `决策读取失败：${errorDetail(runs.reason)}`,
      () => void loadCycle(currentCycle, { keepContent: true }));
  }

  announce(`已载入实验 ${cycle}`);
};

const loadList = async (): Promise<void> => {
  showOnly("loading");
  listController?.abort();
  const controller = new AbortController();
  listController = controller;
  let list: ExperimentSummary[];
  try {
    const body = await api<ExperimentCatalogue>("/v2/experiments", controller.signal);
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
    showOnly("empty");
    hint.textContent = "未登记实验";
    announce("还没有实验");
    return;
  }

  const requested = urlCycle();
  if (requested && !list.some((item) => item.cycle_id === requested)) {
    byId("unknown-cycle").textContent = requested;
    const placeholder = document.createElement("option");
    placeholder.value = "";
    placeholder.textContent = "选择实验…";
    placeholder.disabled = true;
    placeholder.selected = true;
    select.prepend(placeholder);
    hint.textContent = `共 ${list.length} 个实验；地址中的实验不存在`;
    showOnly("unknown");
    announce("实验不存在");
    return;
  }

  const cycle = requested || list[0].cycle_id;
  select.value = cycle;
  hint.textContent = describeCatalogue(requested);
  syncUrl(cycle);
  await loadCycle(cycle);
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
  if (currentCycle) {
    void loadCycle(currentCycle, { keepContent: true }).then(() => {
      if (asofDay !== null) void loadPortfolioOnly();
    });
  } else {
    void loadList();
  }
});
testId("retry").addEventListener("click", () => void loadList());

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

const runVerification = async (kind: "replay" | "reproduction"): Promise<void> => {
  const cycle = currentCycle;
  if (!cycle) return;
  const label = kind === "replay" ? "回放" : "研究重做";
  setVerify(kind, "running", "进行中…", "");
  announce(`${label}开始`);
  const encoded = encodeURIComponent(cycle);
  try {
    if (kind === "replay") {
      const report = await api<ReplayReport>(`/v2/replay?cycle_id=${encoded}`, new AbortController().signal);
      if (cycle !== currentCycle) return;
      setVerify(kind, "ok", `通过（${report.runs_verified} 次决策，${report.fills_verified} 次成交）`,
        `校验后现金 ${money(report.cash_cents)}`);
      announce("交易回放通过");
    } else {
      const report = await api<ReproductionReport>(
        `/v2/experiments/${encoded}/research-reproduction`, new AbortController().signal);
      if (cycle !== currentCycle) return;
      setVerify(kind, "ok", `通过（${report.signals_verified} 条研究信号）`, "研究信号与固定输入一致");
      announce("研究重做通过");
    }
  } catch (error) {
    if (cycle !== currentCycle) return;
    setVerify(kind, "fail", `失败：${errorDetail(error)}`, "该实验结果不能用于研究比较");
    announce(`${label}失败`);
  }
};
testId("verify-replay").addEventListener("click", () => void runVerification("replay"));
testId("verify-reproduction").addEventListener("click", () => void runVerification("reproduction"));

const loadPortfolioOnly = async (): Promise<void> => {
  const cycle = currentCycle;
  if (!cycle) return;
  const sequence = ++asofSequence;
  const query = `/v2/portfolio?cycle_id=${encodeURIComponent(cycle)}` +
    (asofDay === null ? "" : `&asof_day=${encodeURIComponent(asofDay)}`);
  try {
    const portfolio = await api<Portfolio>(query, new AbortController().signal);
    if (sequence !== asofSequence || cycle !== currentCycle) return;
    renderPortfolio(portfolio);
    announce(asofDay === null ? "已清除日终口径" : `已按 ${asofDay} 日终口径读取`);
  } catch (error) {
    if (sequence !== asofSequence) return;
    showSectionError("positions", `资产读取失败：${errorDetail(error)}`, () => void loadPortfolioOnly());
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
  if (key >= "1" && key <= "6") {
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

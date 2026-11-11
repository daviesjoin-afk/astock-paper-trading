import "./styles.css";
import { installRouter, NAV, parseHash, pushHash, replaceHash, type Route } from "./router";
import { moneyShort, renderCurve, renderDailyBars, type CurvePoint } from "./chart";

type StrategyId = "steady" | "aggressive";

type Trade = {
  id: number;
  slot: StrategyId;
  symbol: string;
  side: "buy" | "sell";
  quantity: number;
  price_cents: number;
  fees_cents: number;
  trade_day: string;
  reason: string;
};

type Rebalance = {
  id: number;
  old_steady_bps: number;
  old_aggressive_bps: number;
  new_steady_bps: number;
  new_aggressive_bps: number;
  moved_cents: number;
  reason: string;
  applied_day: string;
};

type SlotView = {
  slot: StrategyId;
  name: string;
  description: string;
  weight_bps: number;
  cash_cents: number;
  market_value_cents: number | null;
  equity_cents: number | null;
  profit_cents: number | null;
  return_bps: number | null;
  drawdown_bps: number | null;
};

type PositionView = {
  slot: StrategyId;
  symbol: string;
  quantity: number;
  sellable_quantity: number | null;
  average_cost_cents: number;
  close_cents: number | null;
  market_value_cents: number | null;
};

type AccountView = {
  account_id: string;
  initial_cash_cents: number;
  cash_cents: number;
  equity_cents: number | null;
  profit_cents: number | null;
  return_bps: number | null;
  as_of_day: string | null;
  created_at: string;
  slots: SlotView[];
  positions: PositionView[];
  trades: Trade[];
  rebalances: Rebalance[];
  price_set_id: string | null;
  price_set_coverage_end: string | null;
  last_settled_day: string | null;
  warnings: string[];
};

type RunResult = {
  from_day: string | null;
  through_day: string;
  processed_days: string[];
  trades: Trade[];
  rebalances: Rebalance[];
  account: AccountView;
};

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
/**
 * Find an element by test id, preferring one that is actually visible.
 *
 * The same test id can legitimately appear in more than one view -- the account
 * figures exist on both the overview and the account page. Rendering must
 * target the visible copy, or updates land on a hidden node and the page
 * silently keeps showing placeholders.
 */
const byTestId = <T extends HTMLElement>(id: string): T => {
  const matches = Array.from(document.querySelectorAll<T>(`[data-testid="${id}"]`));
  if (matches.length === 0) throw new Error(`missing [data-testid="${id}"]`);
  const visible = matches.find(
    (node) => node.offsetParent !== null || node.getClientRects().length > 0,
  );
  return visible ?? matches[0];
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
const signedMoney = (cents: number | null | undefined): string => {
  if (cents === null || cents === undefined) return "—";
  return (cents > 0 ? "+" : "") + money(cents);
};
const percent = (bps: number | null | undefined): string => {
  if (bps === null || bps === undefined) return "—";
  const value = bps / 100;
  return `${value > 0 ? "+" : ""}${value.toFixed(2)}%`;
};
const weight = (bps: number | null | undefined): string =>
  bps === null || bps === undefined ? "—" : `${(bps / 100).toFixed(1)}%`;
const strategyLabel = (slot: StrategyId): string => (slot === "steady" ? "稳健" : "激进");
const sideLabel = (side: string): string => {
  if (side === "buy") return "买入";
  if (side === "sell") return "卖出";
  return side;
};

const errorDetail = (error: unknown): string => {
  if (error instanceof ApiError) {
    if (error.status === null) return "无法连接本地服务";
    try {
      const body = JSON.parse(error.detail) as { error?: string };
      if (typeof body.error === "string") return body.error;
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
      ...init,
      cache: "no-store",
      headers: {
        Accept: "application/json",
        ...(init?.body ? { "Content-Type": "application/json" } : {}),
      },
    });
  } catch (error) {
    throw new ApiError(null, error instanceof Error ? error.message : String(error));
  }
  const text = await response.text();
  if (!response.ok) throw new ApiError(response.status, text.slice(0, 500));
  return text ? (JSON.parse(text) as T) : (undefined as T);
}

const api = <T>(path: string): Promise<T> => request<T>(path);
const post = <T>(path: string, body: unknown): Promise<T> =>
  request<T>(path, { method: "POST", body: JSON.stringify(body) });

const setBusy = (busy: boolean): void => {
  document.documentElement.dataset.busy = String(busy);
  byId("main").setAttribute("aria-busy", String(busy));
};

let current: AccountView | null = null;

const renderStrategy = (slot: SlotView): void => {
  const prefix = slot.slot;
  setText(`${prefix}-weight`, weight(slot.weight_bps));
  setText(`${prefix}-desc`, slot.description);
  setText(`${prefix}-equity`, money(slot.equity_cents));
  setText(`${prefix}-profit`, signedMoney(slot.profit_cents));
  setText(`${prefix}-return`, percent(slot.return_bps));
  setText(`${prefix}-drawdown`, percent(slot.drawdown_bps));
  setText(`${prefix}-cash`, money(slot.cash_cents));
  setText(
    `${prefix}-count`,
    current ? number(current.positions.filter((position) => position.slot === slot.slot).length) : "—",
  );
};

const renderPositions = (account: AccountView): void => {
  const body = byId("positions-body");
  body.replaceChildren();
  for (const position of account.positions) {
    const row = document.createElement("tr");
    // Unrealised P&L per position, against its own average cost. This is the
    // number that answers "is this position working", which the market value
    // alone does not.
    const pnl =
      position.close_cents !== null && position.average_cost_cents > 0
        ? (position.close_cents - position.average_cost_cents) * position.quantity
        : null;
    const pnlBps =
      position.close_cents !== null && position.average_cost_cents > 0
        ? ((position.close_cents - position.average_cost_cents) * 10_000) /
          position.average_cost_cents
        : null;

    const values: Array<[string, string]> = [
      [strategyLabel(position.slot), ""],
      [position.symbol, ""],
      [number(position.quantity), "num-col"],
      [number(position.sellable_quantity), "num-col"],
      [money(position.average_cost_cents), "num-col"],
      [money(position.close_cents), "num-col"],
      [money(position.market_value_cents), "num-col"],
      [
        pnl === null ? "—" : `${signedMoney(pnl)} (${percent(pnlBps)})`,
        "num-col " + (pnl !== null && pnl >= 0 ? "pos" : pnl !== null ? "neg" : ""),
      ],
    ];
    for (const [value, cls] of values) {
      const cell = document.createElement("td");
      if (cls) cell.className = cls;
      cell.textContent = value;
      row.appendChild(cell);
    }
    body.appendChild(row);
  }
  setText("positions-count", `${account.positions.length} 个持仓`);
  show(byTestId("positions-empty"), account.positions.length === 0);
};

const renderTrades = (account: AccountView): void => {
  const body = byId("trades-body");
  body.replaceChildren();
  for (const trade of account.trades.slice(0, 30)) {
    const row = document.createElement("tr");
    const values = [
      trade.trade_day,
      strategyLabel(trade.slot),
      trade.symbol,
      sideLabel(trade.side),
      number(trade.quantity),
      money(trade.price_cents),
    ];
    values.forEach((value, index) => {
      const cell = document.createElement("td");
      if (index >= 4) cell.className = "num-col";
      cell.textContent = value;
      row.appendChild(cell);
    });
    body.appendChild(row);
  }
  show(byTestId("trades-empty"), account.trades.length === 0);
};

const renderRebalances = (account: AccountView): void => {
  const list = byId("rebalances-body");
  list.replaceChildren();
  for (const item of account.rebalances) {
    const entry = document.createElement("li");
    const head = document.createElement("div");
    head.className = "timeline-head";
    const day = document.createElement("strong");
    day.textContent = item.applied_day;
    const moved = document.createElement("span");
    moved.textContent = money(item.moved_cents);
    head.append(day, moved);
    const detail = document.createElement("p");
    detail.textContent =
      `稳健 ${weight(item.old_steady_bps)} → ${weight(item.new_steady_bps)}；` +
      `激进 ${weight(item.old_aggressive_bps)} → ${weight(item.new_aggressive_bps)}`;
    const reason = document.createElement("p");
    reason.className = "muted";
    reason.textContent = item.reason;
    entry.append(head, detail, reason);
    list.appendChild(entry);
  }
  show(byTestId("rebalances-empty"), account.rebalances.length === 0);
};

const warningText: Record<string, string> = {
  no_reviewed_price_set: "没有可用的已审核价格集合，当前无法估值，也不会下单。",
  no_reviewed_close_for_valuation: "价格集合已加载，但还没有对应交易日的收盘价。",
};

const render = (account: AccountView): void => {
  current = account;
  show(byId("loading"), false);
  show(byTestId("create-panel"), false);
  show(byId("dashboard"), true);

  setText("principal-value", money(account.initial_cash_cents));
  setText("equity", money(account.equity_cents));
  setText("profit", signedMoney(account.profit_cents));
  setText("cash", money(account.cash_cents));
  setText("as-of", account.last_settled_day ?? "—");
  setText(
    "coverage",
    account.price_set_coverage_end && account.last_settled_day
      ? `行情覆盖至 ${account.price_set_coverage_end}；账户已结算至 ${account.last_settled_day}`
      : account.price_set_coverage_end
        ? `已加载价格集合至 ${account.price_set_coverage_end}，账户尚未开始`
      : "尚未加载价格集合",
  );
  setText("price-set", `价格集合：${account.price_set_id ?? "—"}`);

  for (const slot of account.slots) renderStrategy(slot);
  renderPositions(account);
  renderTrades(account);
  renderRebalances(account);

  const warning = byId("warning");
  const messages = account.warnings.map((code) => warningText[code] ?? code);
  warning.textContent = messages.join("；");
  show(warning, messages.length > 0);

  const run = byTestId<HTMLButtonElement>("run-submit");
  run.disabled = account.price_set_coverage_end === null;
  announce("账户与策略已更新");
};

const load = async (): Promise<void> => {
  setBusy(true);
  show(byId("service-error"), false);
  try {
    render(await api<AccountView>("/v2/account"));
  } catch (error) {
    if (error instanceof ApiError && error.status === 404) {
      show(byId("loading"), false);
      show(byId("dashboard"), false);
      show(byTestId("create-panel"), true);
      byTestId<HTMLInputElement>("principal").focus();
    } else {
      show(byId("loading"), false);
      show(byId("service-error"), true);
      setText("service-detail", errorDetail(error));
    }
  } finally {
    setBusy(false);
  }
};

const describeRun = (result: RunResult): string => {
  if (result.processed_days.length === 0) {
    return `已经是最新交易日 ${result.through_day}，本次没有重复成交。`;
  }
  const parts = [
    `补齐 ${result.processed_days.length} 个交易日（${result.processed_days[0]} 至 ${result.processed_days[result.processed_days.length - 1]}）`,
    `成交 ${result.trades.length} 笔`,
  ];
  if (result.rebalances.length > 0) {
    const last = result.rebalances[result.rebalances.length - 1];
    parts.push(
      `调仓 ${result.rebalances.length} 次，最近一次 ${money(last.moved_cents)}（稳健 ${weight(last.new_steady_bps)} / 激进 ${weight(last.new_aggressive_bps)}）`,
    );
  } else {
    parts.push("本次未触发调仓");
  }
  return parts.join(" · ");
};

const submitRun = async (): Promise<void> => {
  const button = byTestId<HTMLButtonElement>("run-submit");
  button.disabled = true;
  show(byTestId("run-result"), false);
  setBusy(true);
  try {
    const result = await post<RunResult>("/v2/account/run", {});
    render(result.account);
    const line = byTestId("run-result");
    line.textContent = describeRun(result);
    line.dataset.state = result.rebalances.length > 0 ? "applied" : "held";
    show(line, true);
    announce(describeRun(result));
  } catch (error) {
    const line = byTestId("run-result");
    line.textContent = `推进失败：${errorDetail(error)}`;
    line.dataset.state = "error";
    show(line, true);
    announce("推进失败");
  } finally {
    button.disabled = false;
    setBusy(false);
  }
};

const createAccount = async (event: SubmitEvent): Promise<void> => {
  event.preventDefault();
  const input = byTestId<HTMLInputElement>("principal");
  const error = byTestId("create-error");
  const yuan = Number(input.value);
  const cents = Math.round(yuan * 100);
  show(error, false);
  if (!Number.isFinite(yuan) || !Number.isInteger(cents) || cents < 10_000 || cents > 100_000_000_000) {
    error.textContent = "请输入 100 元到 10 亿元之间的本金（最多两位小数）。";
    show(error, true);
    return;
  }
  const button = byTestId<HTMLButtonElement>("create-submit");
  button.disabled = true;
  setBusy(true);
  try {
    render(await post<AccountView>("/v2/account", { initial_cash_cents: cents }));
    announce("账户已创建");
  } catch (failure) {
    error.textContent = `创建失败：${errorDetail(failure)}`;
    show(error, true);
  } finally {
    button.disabled = false;
    setBusy(false);
  }
};

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
      pushHash({ module: module.id, view: module.views[0].id });
      // pushState does not fire hashchange, so drive the render directly.
      applyRoute(parseHash(location.hash));
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
      pushHash({ module: route.module, view: view.id });
      applyRoute(parseHash(location.hash));
    });
    nav.appendChild(button);
  }
}

function applyRoute(route: Route): void {
  replaceHash(route);
  renderModuleNav(route.module);
  renderViewNav(route);

  const target = `${route.module}/${route.view}`;
  for (const node of document.querySelectorAll<HTMLElement>(".view")) {
    show(node, node.dataset.view === target);
  }

  // Every view except the first assumes an account exists. If one does not,
  // the create panel is the only sensible thing to show.
  if (target !== "overview/dashboard" && current === null) {
    show(byId("loading"), false);
    show(byTestId("create-panel"), true);
    return;
  }

  // Re-render for the view that just became visible. `render` writes into the
  // copy of each figure that is visible at the time it runs, and when the
  // account was loaded on a different view these nodes were hidden -- so the
  // figures have to be pushed again now that this view is on screen.
  if (current) render(current);

  void loadView(target);
}

/** Load whatever a view needs, once per session unless explicitly refreshed. */
async function loadView(view: string): Promise<void> {
  if (loadedViews.has(view)) return;
  try {
    switch (view) {
      case "trading/account":
        await loadCurve();
        break;
      case "market/screener":
        await loadScreener();
        break;
      case "market/data":
        await loadCoverage();
        break;
      default:
        // Views backed by the account payload are already rendered by `load`.
        break;
    }
    loadedViews.add(view);
  } catch (error) {
    announce(`加载失败：${errorDetail(error)}`);
  }
}

// ---- equity curve ----------------------------------------------------------

type CurvePointPayload = CurvePoint & {
  profit_cents: number;
  return_bps: number;
  steady_return_bps: number;
  aggressive_return_bps: number;
};

type CurvePayload = {
  account_id: string;
  initial_cash_cents: number;
  points: CurvePointPayload[];
  summary: {
    first_day: string | null;
    last_day: string | null;
    days: number;
    equity_cents: number;
    profit_cents: number;
    return_bps: number;
    max_drawdown_bps: number;
    peak_day: string;
    trough_day: string;
    steady_return_bps: number;
    aggressive_return_bps: number;
    steady_cents: number;
    aggressive_cents: number;
  } | null;
};

let curve: CurvePayload | null = null;
let curveSeries: "total" | "steady" | "aggressive" = "total";
let curveRange = "all";

async function loadCurve(): Promise<void> {
  curve = await api<CurvePayload>("/v2/account/equity-curve");
  drawCurve();
}

function drawCurve(): void {
  if (!curve) return;
  const summary = byId("curve-summary");
  const chart = byId("equity-chart");
  const bars = byId("daily-bars");

  if (!curve.summary || curve.points.length === 0) {
    summary.replaceChildren();
    chart.innerHTML = renderCurve({
      points: [],
      initialCents: curve.initial_cash_cents,
      series: curveSeries,
      width: 900,
      height: 260,
    });
    bars.replaceChildren();
    return;
  }

  const take = curveRange === "all" ? curve.points.length : Number(curveRange);
  const points = curve.points.slice(-take);
  const s = curve.summary;

  const stat = (label: string, value: string, tone: string, sub: string): string =>
    `<div class="curve-stat">
       <span class="curve-stat-label">${label}</span>
       <strong class="${tone}">${value}</strong>
       <span class="muted">${sub}</span>
     </div>`;

  summary.innerHTML = [
    stat(
      "累计收益",
      signedMoney(s.profit_cents),
      s.profit_cents >= 0 ? "pos" : "neg",
      percent(s.return_bps),
    ),
    stat(
      "最大回撤",
      percent(-s.max_drawdown_bps),
      "neg",
      `${s.peak_day} → ${s.trough_day}`,
    ),
    stat(
      "稳健",
      percent(s.steady_return_bps),
      s.steady_return_bps >= 0 ? "pos" : "neg",
      moneyShort(s.steady_cents),
    ),
    stat(
      "激进",
      percent(s.aggressive_return_bps),
      s.aggressive_return_bps >= 0 ? "pos" : "neg",
      moneyShort(s.aggressive_cents),
    ),
    stat("样本", String(points.length), "", `个交易日，${s.first_day} 起`),
  ].join("");

  chart.innerHTML = renderCurve({
    points,
    initialCents: curve.initial_cash_cents,
    series: curveSeries,
    width: 900,
    height: 260,
  });
  // The day-by-day strip only makes sense on the account curve; a single slot's
  // per-day change is already visible as the curve's slope.
  bars.innerHTML = curveSeries === "total" ? renderDailyBars(points, 900, 64) : "";
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

let screenerRows: Bar[] = [];

async function loadScreener(): Promise<void> {
  const days = await api<{ days: string[]; latest_day: string | null }>("/v2/market/days");
  const select = byId<HTMLSelectElement>("screener-day");
  select.replaceChildren();
  for (const day of [...days.days].reverse()) {
    const option = document.createElement("option");
    option.value = day;
    option.textContent = day;
    select.appendChild(option);
  }
  if (!days.latest_day) {
    show(byId("screener-empty"), true);
    return;
  }
  select.value = days.latest_day;
  await fetchScreenerDay(days.latest_day);
  select.addEventListener("change", () => void fetchScreenerDay(select.value));
  byId<HTMLInputElement>("screener-filter").addEventListener("input", renderScreener);
}

async function fetchScreenerDay(day: string): Promise<void> {
  const payload = await api<{ bars: Bar[]; count: number }>(`/v2/market/bars?day=${day}`);
  // Sort by absolute move so the interesting rows float to the top.
  const move = (bar: Bar): number =>
    bar.prev_close_cents > 0
      ? Math.abs(bar.close_cents - bar.prev_close_cents) / bar.prev_close_cents
      : 0;
  screenerRows = payload.bars.slice().sort((a, b) => move(b) - move(a));
  renderScreener();
}

function renderScreener(): void {
  const filter = byId<HTMLInputElement>("screener-filter").value.trim().toUpperCase();
  const body = byId("screener-body");
  body.replaceChildren();
  const rows = filter
    ? screenerRows.filter(
        (row) => row.symbol.toUpperCase().includes(filter) || row.name.includes(filter),
      )
    : screenerRows;
  // Cap the rendered rows: 5.5k table rows is a lot of DOM for a list nobody
  // scrolls to the bottom of.
  const shown = rows.slice(0, 300);
  for (const row of shown) {
    const tr = document.createElement("tr");
    const change =
      row.prev_close_cents > 0
        ? ((row.close_cents - row.prev_close_cents) * 10_000) / row.prev_close_cents
        : 0;
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
    body.appendChild(tr);
  }
  setTextById("screener-count", `${shown.length} / ${rows.length} 只`);
  show(byId("screener-empty"), rows.length === 0);
}

// ---- data coverage ---------------------------------------------------------

async function loadCoverage(): Promise<void> {
  const days = await api<{
    days: string[];
    count: number;
    latest_day: string | null;
    latest_symbol_count: number;
  }>("/v2/market/days");

  setTextById("cov-days", number(days.count));
  setTextById("cov-first", days.days[0] ?? "—");
  setTextById("cov-last", days.latest_day ?? "—");
  setTextById("cov-symbols", number(days.latest_symbol_count));

  if (!days.latest_day) {
    show(byId("gaps-empty"), true);
    return;
  }
  const gaps = await api<{ bars: Bar[]; count: number }>(
    `/v2/market/gaps?day=${days.latest_day}`,
  );
  const body = byId("gaps-body");
  body.replaceChildren();
  for (const row of gaps.bars.slice(0, 200)) {
    const tr = document.createElement("tr");
    const change =
      row.prev_close_cents > 0
        ? ((row.close_cents - row.prev_close_cents) * 10_000) / row.prev_close_cents
        : 0;
    const cells: Array<[string, string]> = [
      [row.symbol, ""],
      [row.name, ""],
      [money(row.prev_close_cents), "num-col"],
      [money(row.close_cents), "num-col"],
      [percent(change), "num-col " + (change >= 0 ? "pos" : "neg")],
    ];
    for (const [text, cls] of cells) {
      const td = document.createElement("td");
      td.textContent = text;
      if (cls) td.className = cls;
      tr.appendChild(td);
    }
    body.appendChild(tr);
  }
  setTextById("gaps-count", `${gaps.count} 只`);
  show(byId("gaps-empty"), gaps.count === 0);
}

// ---- wiring ----------------------------------------------------------------

async function runToDay(day: string): Promise<void> {
  const line = byTestId("run-result");
  try {
    const result = await post<RunResult>("/v2/account/run", { day });
    render(result.account);
    loadedViews.delete("trading/account");
    line.textContent = describeRun(result);
    line.dataset.state = result.rebalances.length > 0 ? "applied" : "held";
    show(line, true);
    announce(describeRun(result));
  } catch (error) {
    line.textContent = `推进失败：${errorDetail(error)}`;
    line.dataset.state = "error";
    show(line, true);
  }
}

byId("create-form").addEventListener("submit", (event) => void createAccount(event as SubmitEvent));
byTestId("run-submit").addEventListener("click", () => void submitRun());
byId("advance-day-submit").addEventListener("click", () => {
  const day = byId<HTMLInputElement>("advance-day").value;
  if (day) void runToDay(day);
});
byTestId("refresh").addEventListener("click", () => {
  loadedViews.clear();
  void load().then(() => applyRoute(parseHash(location.hash)));
});
byTestId("retry").addEventListener("click", () => void load());
byTestId("theme-toggle").addEventListener("click", theme);

for (const button of document.querySelectorAll<HTMLButtonElement>(".seg-btn")) {
  button.addEventListener("click", () => {
    curveSeries = (button.dataset.series as typeof curveSeries) ?? "total";
    for (const other of document.querySelectorAll(".seg-btn")) {
      other.classList.toggle("active", other === button);
    }
    drawCurve();
  });
}

for (const chip of document.querySelectorAll<HTMLButtonElement>(".chip[data-range]")) {
  chip.addEventListener("click", () => {
    curveRange = chip.dataset.range ?? "all";
    for (const other of document.querySelectorAll(".chip[data-range]")) {
      other.classList.toggle("active", other === chip);
    }
    drawCurve();
  });
}

// Load the account first, then start routing: `applyRoute` needs `current` to
// decide whether a view can render or the create panel should show instead.
void load().then(() => installRouter((route) => applyRoute(route)));


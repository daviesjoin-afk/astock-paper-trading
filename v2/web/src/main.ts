import "./styles.css";

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
const byTestId = <T extends HTMLElement>(id: string): T => {
  const node = document.querySelector<T>(`[data-testid="${id}"]`);
  if (!node) throw new Error(`missing [data-testid="${id}"]`);
  return node;
};
const setText = (id: string, value: string): void => {
  byTestId(id).textContent = value;
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
    const values = [
      strategyLabel(position.slot),
      position.symbol,
      number(position.quantity),
      number(position.sellable_quantity),
      money(position.average_cost_cents),
      money(position.close_cents),
      money(position.market_value_cents),
    ];
    values.forEach((value, index) => {
      const cell = document.createElement("td");
      if (index >= 2) cell.className = "num-col";
      cell.textContent = value;
      row.appendChild(cell);
    });
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

byId("create-form").addEventListener("submit", (event) => void createAccount(event as SubmitEvent));
byTestId("run-submit").addEventListener("click", () => void submitRun());
byTestId("refresh").addEventListener("click", () => void load());
byTestId("retry").addEventListener("click", () => void load());
byTestId("theme-toggle").addEventListener("click", theme);

void load();

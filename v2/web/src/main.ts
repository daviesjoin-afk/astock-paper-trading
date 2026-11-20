import "./styles.css";
import { installRouter, NAV, parseHash, pushHash, replaceHash, type Route } from "./router";
import { buildLiveReport, refreshLiveData as refreshLive, renderLive, setLiveRouteApplier } from "./live";

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

async function request<T>(path: string): Promise<T> {
  let response: Response;
  try {
    response = await fetch(path, {
      cache: "no-store",
      headers: { Accept: "application/json" },
    });
  } catch (error) {
    throw new ApiError(null, error instanceof Error ? error.message : String(error));
  }
  const text = await response.text();
  if (!response.ok) throw new ApiError(response.status, text.slice(0, 500));
  return text ? (JSON.parse(text) as T) : (undefined as T);
}

const api = <T>(path: string): Promise<T> => request<T>(path);

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

/**
 * Bumped on every candidate render. `describeGate` awaits one request per row,
 * so two renders can interleave: the second clears the table, the first then
 * appends its rows to the cleared body. Without this guard the table shows
 * every candidate twice.
 */
let candidateRender = 0;

async function loadCandidates(): Promise<void> {
  const table = byTestId<HTMLTableElement>("research-table");
  const empty = byTestId<HTMLElement>("research-empty");
  const body = byId<HTMLTableSectionElement>("research-rows");
  const generation = ++candidateRender;
  const rows = await api<CandidateRow[]>("/v2/research/candidates");
  if (generation !== candidateRender) return;
  body.replaceChildren();
  if (rows.length === 0) {
    show(table, false);
    show(empty, true);
    return;
  }
  show(empty, false);
  show(table, true);
  for (const row of rows) {
    let gate = "—";
    try {
      const history = await api<HistoryRow[]>(
        `/v2/research/candidates/${encodeURIComponent(row.candidate_id)}/history`,
      );
      gate = describeGate(history);
    } catch {
      gate = "历史读取失败";
    }
    if (generation !== candidateRender) return;
    const tr = document.createElement("tr");
    for (const text of [row.candidate_id, `${row.algorithm_id}@${row.algorithm_version}`,
                        row.state, String(row.shadow_days), gate]) {
      const cell = document.createElement("td");
      cell.textContent = text;
      tr.appendChild(cell);
    }
    body.appendChild(tr);
  }
  announce(`候选 ${rows.length} 个`);
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
  const body = byId<HTMLTableSectionElement>("screener-body");
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
    // The list is a way in, not the destination: a row opens its own page so
    // the K-line and gap markers are one click away.
    tr.tabIndex = 0;
    tr.dataset.symbol = row.symbol;
    tr.addEventListener("click", () => openDetail(row.symbol));
    tr.addEventListener("keydown", (event) => {
      if (event.key === "Enter") openDetail(row.symbol);
    });
    body.appendChild(tr);
  }
  setTextById("screener-count", `${shown.length} / ${rows.length} 只`);
  show(byId("screener-empty"), rows.length === 0);
}

// ---- wiring ----------------------------------------------------------------

/** The report-copy control never mixes the retired daily account with the
 *  intraday ledger; other modules still read their own daily report API. */
async function loadReport(): Promise<void> {
  if (parseHash(location.hash).module === "live") {
    reportText = buildLiveReport();
    announce("复盘已更新");
    return;
  }
  setBusy(true);
  try {
    const report = await api<{ text?: string }>("/v2/account/daily-report");
    reportText = report.text ?? "";
    announce("复盘已更新");
  } catch {
    reportText = "";
  } finally {
    setBusy(false);
  }
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
    loadReport(),
    loadView(`${route.module}/${route.view}`),
  ]).then(() => setBusy(false));
});
const reportCopy = document.getElementById("report-copy");
if (reportCopy) {
  reportCopy.addEventListener("click", async () => {
    if (!reportText) await loadReport();
    if (!reportText) return;
    try {
      await navigator.clipboard.writeText(reportText);
      reportCopy.textContent = "已复制";
      setTimeout(() => {
        reportCopy.textContent = "复制复盘";
      }, 1500);
    } catch {
      reportCopy.textContent = "复制失败";
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

async function loadDetail(symbol: string): Promise<void> {
  const chart = byId("detail-chart");
  const empty = byTestId("detail-empty");
  if (!symbol) {
    chart.replaceChildren();
    setText("detail-symbol", "—");
    for (const id of ["detail-close", "detail-change", "detail-range", "detail-days"]) setText(id, "—");
    show(empty, true);
    return;
  }
  setText("detail-symbol", symbol);
  let rows: Bar[];
  try {
    const payload = await api<{ bars: Bar[] }>(
      `/v2/market/bars/${encodeURIComponent(symbol)}`,
    );
    rows = payload.bars;
  } catch (error) {
    // A 404 here is a normal answer ("that code is not ingested"), not a
    // service failure: say which code is missing rather than "加载失败".
    const missing = error instanceof ApiError && error.status === 404;
    chart.replaceChildren();
    setText("detail-close", "—");
    setText("detail-change", "—");
    setText("detail-range", "—");
    setText("detail-days", "0");
    empty.textContent = missing ? `没有 ${symbol} 的入库行情。` : `加载失败：${errorDetail(error)}`;
    show(empty, true);
    return;
  }
  show(empty, rows.length === 0);
  if (rows.length === 0) return;
  empty.textContent = "输入代码查看个股走势。";

  const closes = rows.map((row) => row.close_cents);
  const last = closes[closes.length - 1];
  const first = closes[0];
  const high = Math.max(...closes);
  const low = Math.min(...closes);
  setText("detail-close", money(last));
  setText("detail-change", first > 0 ? percent(((last - first) * 10_000) / first) : "—");
  setText("detail-range", `${money(high)} / ${money(low)}`);
  setText("detail-days", String(rows.length));

  // Gaps are drawn as a break, never a solid bar: an ex-dividend or split
  // session is not a real move and must not look like one.
  const gaps = new Set<string>();
  for (let index = 1; index < rows.length; index += 1) {
    const prev = rows[index - 1].close_cents;
    const current = rows[index].close_cents;
    if (prev > 0 && Math.abs(current - prev) * 10_000 / prev > 1_500) {
      gaps.add(rows[index].day);
    }
  }
  chart.replaceChildren(buildLine(closes, gaps, rows.map((row) => row.day)));
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

  let path = "";
  let pen = false;
  values.forEach((value, index) => {
    if (gaps.has(days[index])) pen = false; // lift the pen across the gap
    path += `${pen ? "L" : "M"}${x(index).toFixed(1)},${y(value).toFixed(1)} `;
    pen = true;
  });
  const line = document.createElementNS(ns, "path");
  line.setAttribute("d", path.trim());
  line.setAttribute("fill", "none");
  line.setAttribute("stroke", "var(--chart-1, #4f8cff)");
  line.setAttribute("stroke-width", "1.6");
  line.setAttribute("stroke-linejoin", "round");
  svg.appendChild(line);

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

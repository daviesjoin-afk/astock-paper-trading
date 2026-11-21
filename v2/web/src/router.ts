/**
 * Hash router for the console.
 *
 * Hand-rolled rather than pulling in a router library: the whole surface is
 * ~20 views, and a dependency here would cost more than these 120 lines. What
 * matters is that every view has a shareable URL, back/forward work, and a
 * reload lands on the same place.
 *
 * URL shape: `#/<module>/<view>` -- e.g. `#/trading/positions`.
 * One level of nesting is enough; anything deeper is an in-page tab.
 */

export type Route = {
  /** Top-level module, e.g. `overview`, `trading`. */
  module: string;
  /** View inside the module. Defaults to the module's first view. */
  view: string;
};

/**
 * Every module and its views, in navigation order.
 *
 * The intraday module is first because it is the only account that actually
 * trades. The former daily modules (`overview`, `trading`) are deliberately
 * absent: their "开始自动运行" button created a *separate* daily ledger that no
 * scheduler ever advanced, so a running intraday account could sit beside an
 * empty daily one and look broken. The daily kernel still exists behind
 * `/v2/account*`; this console no longer offers a misleading entry point.
 */
export const NAV = [
  {
    id: "live",
    label: "盘中",
    hint: "实时总览 · 决策 · 成交 · 轮次",
    views: [
      { id: "dashboard", label: "实时总览" },
      { id: "signals", label: "决策信号" },
      { id: "trades", label: "实时成交" },
      { id: "rounds", label: "调度记录" },
      { id: "ai", label: "AI 观测" },
      { id: "evolution", label: "进化提案" },
      { id: "risk", label: "风控拦截" },
      { id: "symbol", label: "个股档案" },
    ],
  },
  {
    id: "market",
    label: "行情",
    hint: "全市场 · 个股 · 板块",
    views: [
      { id: "screener", label: "全市场" },
      { id: "detail", label: "个股详情" },
    ],
  },
  {
    id: "research",
    label: "研究",
    hint: "候选 · 数据底座 · 证据",
    views: [
      { id: "candidates", label: "候选评审" },
      { id: "coverage", label: "数据底座" },
      { id: "evidence", label: "策略证据" },
    ],
  },
  {
    id: "settings",
    label: "设置",
    hint: "AI 模型 · 长期运行",
    views: [
      { id: "ai", label: "AI 模型" },
      { id: "runtime", label: "长期运行" },
    ],
  },
] as const;

export type ModuleId = (typeof NAV)[number]["id"];

const DEFAULT_MODULE = NAV[0].id;

/** Parse `#/trading/positions` into a validated route, falling back safely. */
export function parseHash(hash: string): Route {
  const raw = hash.replace(/^#\/?/, "").trim();
  if (!raw) return { module: DEFAULT_MODULE, view: firstView(DEFAULT_MODULE) };

  const [modulePart, viewPart] = raw.split("/");
  const module = NAV.find((entry) => entry.id === modulePart);
  if (!module) return { module: DEFAULT_MODULE, view: firstView(DEFAULT_MODULE) };

  const view = module.views.find((entry) => entry.id === viewPart);
  return {
    module: module.id,
    view: view ? view.id : module.views[0].id,
  };
}

export function firstView(moduleId: string): string {
  const module = NAV.find((entry) => entry.id === moduleId);
  return module ? module.views[0].id : "dashboard";
}

export function toHash(route: Route): string {
  return `#/${route.module}/${route.view}`;
}

/** Replace the current URL without adding a history entry (for normalisation). */
export function replaceHash(route: Route): void {
  const next = toHash(route);
  if (location.hash !== next) {
    history.replaceState(null, "", next);
  }
}

/** Push a new URL so the back button returns to the previous view. */
export function pushHash(route: Route): void {
  const next = toHash(route);
  if (location.hash !== next) {
    history.pushState(null, "", next);
  }
}

/**
 * Call `onChange` whenever the route changes, including on first load.
 *
 * `popstate` covers the back/forward buttons; `hashchange` covers direct
 * edits. Both are wired because browsers are inconsistent about which fires
 * for a hash-only navigation.
 */
export function installRouter(onChange: (route: Route) => void): void {
  const handle = () => onChange(parseHash(location.hash));
  window.addEventListener("hashchange", handle);
  window.addEventListener("popstate", handle);
  handle();
}

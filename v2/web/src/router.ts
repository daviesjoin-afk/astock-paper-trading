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

/** Every module and its views, in navigation order. */
export const NAV = [
  {
    id: "overview",
    label: "总览",
    hint: "净值 · 今日盈亏 · 告警",
    views: [
      { id: "dashboard", label: "驾驶舱" },
      { id: "signals", label: "今日信号" },
    ],
  },
  {
    id: "market",
    label: "行情",
    hint: "全市场 · 个股 · 板块",
    views: [
      { id: "screener", label: "全市场" },
      { id: "detail", label: "个股详情" },
      { id: "data", label: "数据覆盖" },
    ],
  },
  {
    id: "trading",
    label: "交易",
    hint: "账户 · 持仓 · 成交",
    views: [
      { id: "account", label: "账户" },
      { id: "positions", label: "持仓" },
      { id: "trades", label: "委托与成交" },
      { id: "rebalances", label: "调仓记录" },
      { id: "advance", label: "推进与结算" },
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

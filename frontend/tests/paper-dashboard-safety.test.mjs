// 纸盘仪表盘的对抗性回归（R26）：
//   1. 后端/外部行情源/AI 的自由文本（股票名、行业、原因、周报、免责声明、
//      策略名/模型名、周期键、行业暴露键）进入 innerHTML 前必须转义。
//   2. 缺失对象（d/accounts/shared/cycle 或字段）不得抛异常，也不得把
//      null 直接印成 "null"。
import assert from "node:assert/strict";
import test from "node:test";

const paper = await import(new URL("../src/features/paper.js", import.meta.url).href);

const XSS = '<img src=x onerror=alert(1)>';

function makeEl(extra) {
  return Object.assign({
    innerHTML: "", textContent: "", value: "", disabled: false,
    className: "", style: {}, hidden: false,
    setAttribute() {}, querySelectorAll: () => [], querySelector: () => null,
    closest: () => null,
  }, extra || {});
}

function withDom(nodes, fn) {
  const previousDocument = globalThis.document;
  const previousWindow = globalThis.window;
  const previousRaf = globalThis.requestAnimationFrame;
  globalThis.document = {
    activeElement: null,
    getElementById: (id) => nodes[id] || null,
    querySelectorAll: () => [],
    createElement: () => makeEl(),
  };
  globalThis.window = nodes.__window || {};
  globalThis.requestAnimationFrame = (cb) => cb();
  try {
    return fn();
  } finally {
    if (previousDocument === undefined) delete globalThis.document; else globalThis.document = previousDocument;
    if (previousWindow === undefined) delete globalThis.window; else globalThis.window = previousWindow;
    if (previousRaf === undefined) delete globalThis.requestAnimationFrame; else globalThis.requestAnimationFrame = previousRaf;
  }
}

function dashboardNodes() {
  const nodes = {
    paperCapital: makeEl({ value: "300000" }),
    paperCapitalHint: makeEl(),
    paperStatus: makeEl(),
    paperOrderAccount: makeEl(),
    paperAccountStrip: makeEl(),
    paperPositionFilter: makeEl(),
    paperPositionStateFilter: makeEl(),
    paperOrderAccountFilter: makeEl(),
    paperOrderSideFilter: makeEl(),
    paperOrderStatusFilter: makeEl(),
    paperOrderDateFilter: makeEl(),
    paperTerminalBoard: makeEl(),
    paperActivityBoard: makeEl(),
    paperHistoryAccount: makeEl(),
    paperHistoryQuick: makeEl(),
    paperResult: makeEl(),
    paperCompareChart: makeEl(),
  };
  nodes.__window = { _paperWorkspace: "portfolio" };
  return nodes;
}

test("纸盘仪表盘把后端自由文本转义，缺字段不抛异常且不显示 null", () => {
  const nodes = dashboardNodes();
  withDom(nodes, () => {
    assert.doesNotThrow(() => paper.renderPaperDashboard({
      shared: { nav: null, initial_cash: null, position_count: null, today_pnl: null, today_pnl_status: XSS },
      cycle: { cycle_key: XSS },
      accounts: [{
        id: "a1", name: XSS, status: "running",
        entry_model_name: XSS, risk_profile_name: "<script>rp</script>",
        position_count: null, today_pnl_status: "<b>bad</b>",
      }],
      positions: [{
        account_id: "a1", name: XSS, code: "600000", industry: XSS,
        t1_reason: '"><script>t1</script>', t1_status: XSS, price_state: XSS,
        available_date: XSS, quote_at: XSS,
        qty: 100, available_qty: 100, locked_qty: 0, hold_days: 1,
        cost: 10, price: 11, market_value: 1100, account_weight_pct: 1,
        ret_pct: 1, unrealized_pnl: 100, today_pnl: null, today_return_pct: null,
        quality_review_phase: XSS, quality_action: XSS,
      }],
      orders: [{
        id: 1, account_id: "a1", account_name: XSS, name: XSS, code: "600000",
        side: "buy", status: "filled", qty: 100, created_at: "2026-09-01 10:00:00",
        reason: XSS, origin: "manual",
      }],
      fills: [{
        fill_date: XSS, account_id: "a1", account_name: XSS, side: "buy",
        code: "600000", qty: 100, price: 10, amount: 1000, fees: 1, assumption: XSS,
      }],
      reviews: [{ account_id: XSS, week_key: XSS, recommendation: XSS, report: XSS }],
      observations: [{ observed_at: XSS, account_id: "a1", code: XSS, price: 1, action: "observe", reason: XSS }],
      parameter_versions: [{ created_at: XSS, account_id: XSS, version: XSS, style: XSS, effective_date: XSS, reason: XSS }],
      archives: [{ created_at: XSS, cycle_key: XSS, reason: XSS }],
      industry_exposure: { [XSS]: 1000 },
      disclaimer: XSS,
    }, null));
  });
  const html = nodes.paperResult.innerHTML
    + nodes.paperTerminalBoard.innerHTML
    + nodes.paperAccountStrip.innerHTML;
  assert.equal(html.includes("<img"), false, `存在未转义的 <img>：${html.slice(0, 400)}`);
  assert.equal(html.includes("<script"), false, "存在未转义的 <script>");
  assert.equal(html.includes("&lt;img src=x onerror=alert(1)&gt;"), true, "自由文本未按实体输出");
  assert.equal(html.includes("undefined"), false, "缺失字段被渲染成 undefined");
  assert.equal(html.includes(">null<"), false, "缺失字段被渲染成 null");
});

test("仪表盘 payload 缺失时不抛异常", () => {
  const nodes = dashboardNodes();
  withDom(nodes, () => {
    assert.doesNotThrow(() => paper.renderPaperDashboard(undefined, null));
    assert.doesNotThrow(() => paper.renderPaperDashboard({}, null));
  });
});

test("正常数据渲染不受转义收口影响（股票名/金额/状态仍按原样展示）", () => {
  const nodes = dashboardNodes();
  withDom(nodes, () => {
    paper.renderPaperDashboard({
      shared: { nav: 100000, initial_cash: 90000, position_count: 1, position_limit: 10, today_pnl: 100, today_return_pct: 0.1 },
      cycle: { cycle_key: "cycle-2026-09", status: "running" },
      accounts: [{
        id: "a1", name: "短线日内做T", status: "running", position_count: 1,
        entry_model_name: "趋势", risk_profile_name: "稳健", today_pnl: 100, today_return_pct: 0.1,
      }],
      positions: [{
        account_id: "a1", name: "平安银行", code: "000001", industry: "银行",
        qty: 100, available_qty: 100, locked_qty: 0, hold_days: 2,
        cost: 10, price: 11, market_value: 1100, account_weight_pct: 1.1,
        ret_pct: 10, unrealized_pnl: 100, today_pnl: 10, today_return_pct: 1,
        quality_score: 80, quality_grade: "核心", quality_action: "hold",
      }],
      orders: [{ id: 7, account_id: "a1", account_name: "短线日内做T", name: "平安银行", code: "000001", side: "buy", status: "filled", qty: 100, created_at: "2026-09-01 10:00:00", origin: "manual" }],
      fills: [], reviews: [], observations: [], parameter_versions: [], archives: [],
      industry_exposure: { "银行": 1100 },
      disclaimer: "模拟盘，不构成投资建议",
    }, null);
  });
  const html = nodes.paperResult.innerHTML + nodes.paperTerminalBoard.innerHTML;
  for (const expected of ["平安银行", "000001", "￥1100.00", "银行", "核心", "模拟盘，不构成投资建议"]) {
    assert.ok(html.includes(expected), `正常文本丢失：${expected}`);
  }
  assert.equal(html.includes("&amp;"), false, "正常文本被过度转义");
});

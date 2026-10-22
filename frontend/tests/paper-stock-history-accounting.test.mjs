// 个股档案“今日盈亏”汇总口径回归：
//   旧实现把多账户 today_pnl 求和后，只拿 positions[0].today_return_pct 当整体
//   百分比；并且部分账户缺当日行情时，部分和仍被当作完整“今日盈亏”展示。
//   0 是真实数据（平盘），null/undefined 才是“没有数据”，两者不能混。
import assert from "node:assert/strict";
import test from "node:test";

const paper = await import(new URL("../src/features/paper.js", import.meta.url).href);

function makeEl(extra) {
  return Object.assign({ innerHTML: "", textContent: "", value: "", disabled: false }, extra || {});
}

async function runHistory(payload) {
  const previousDocument = globalThis.document;
  const previousFetch = globalThis.fetch;
  const target = makeEl({ querySelector: () => null });
  const nodes = {
    paperHistoryCode: makeEl({ value: "600000" }),
    paperHistoryAccount: makeEl({ value: "" }),
    paperHistoryResult: target,
    paperHistoryLoad: makeEl(),
  };
  globalThis.document = {
    getElementById: (id) => nodes[id] || null,
    querySelectorAll: () => [],
  };
  globalThis.fetch = async () => ({ ok: true, json: async () => payload });
  try {
    await paper.loadPaperStockHistory();
    return target.innerHTML;
  } finally {
    if (previousDocument === undefined) delete globalThis.document; else globalThis.document = previousDocument;
    if (previousFetch === undefined) delete globalThis.fetch; else globalThis.fetch = previousFetch;
  }
}

const base = { name: "测试股", code: "600000", quote_at: "2026-09-01 15:00:00", summary: {}, fills: [], orders: [] };

test("多账户今日盈亏只显示合计口径，不拿第一个账户的收益率冒充整体", async () => {
  const html = await runHistory(Object.assign({}, base, {
    positions: [
      { account_id: "a1", account_name: "策略A", today_pnl: 100, today_return_pct: 1, qty: 100, cost: 10, price: 11, market_value: 1100, ret_pct: 10, hold_days: 1 },
      { account_id: "a2", account_name: "策略B", today_pnl: 200, today_return_pct: 2, qty: 100, cost: 10, price: 12, market_value: 1200, ret_pct: 20, hold_days: 1 },
    ],
  }));
  assert.ok(html.includes("2 个账户合计"), "多账户合计未标明口径");
  assert.ok(html.includes("￥300.00"), "今日盈亏合计金额错误");
  assert.equal(html.includes("（+1.00%）"), false, "仍用 positions[0] 的收益率冒充整体");
});

test("部分账户缺当日行情时明确提示，不把部分和当完整总额", async () => {
  const html = await runHistory(Object.assign({}, base, {
    positions: [
      { account_id: "a1", account_name: "策略A", today_pnl: 100, today_return_pct: 1, qty: 100, cost: 10, price: 11, market_value: 1100, ret_pct: 10, hold_days: 1 },
      { account_id: "a2", account_name: "策略B", today_pnl: null, today_return_pct: null, qty: 100, cost: 10, price: 12, market_value: 1200, ret_pct: 20, hold_days: 1 },
    ],
  }));
  assert.ok(html.includes("部分账户缺当日行情"), "部分数据缺失未提示");
  assert.ok(html.includes("1 个账户合计"), "贡献账户数未显示");
});

test("0 是真实数据：单账户平盘显示 ￥0.00，不退化成“盘前未开盘”", async () => {
  const html = await runHistory(Object.assign({}, base, {
    positions: [{ account_id: "a1", account_name: "策略A", today_pnl: 0, today_return_pct: 0, qty: 100, cost: 10, price: 10, market_value: 1000, ret_pct: 0, hold_days: 1 }],
  }));
  assert.ok(html.includes("￥0.00"), "0 盈亏未按真实数据展示");
  assert.ok(html.includes("（0.00%）"), "0 收益率未展示");
  assert.equal(html.includes("盘前未开盘"), false, "真实 0 被当成未开盘");
});

test("全部账户缺当日行情时显示后端状态，不显示 ￥0.00", async () => {
  const html = await runHistory(Object.assign({}, base, {
    summary: { today_pnl_status: "盘前未开盘" },
    positions: [{ account_id: "a1", account_name: "策略A", today_pnl: null, today_return_pct: null, qty: 100, cost: 10, price: 10, market_value: 1000, ret_pct: 0, hold_days: 1 }],
  }));
  assert.ok(html.includes("盘前未开盘"), "缺失行情未回退到后端状态");
  assert.equal(html.includes("￥0.00"), false, "缺失行情被渲染成 0 盈亏");
});

test("个股档案中的账户名/股票名被转义", async () => {
  const xss = '<img src=x onerror=alert(1)>';
  const html = await runHistory(Object.assign({}, base, {
    name: xss,
    positions: [{ account_id: "a1", account_name: xss, today_pnl: 1, today_return_pct: 1, qty: 100, cost: 10, price: 11, market_value: 1100, ret_pct: 10, hold_days: 1 }],
  }));
  assert.equal(html.includes("<img"), false, "个股档案存在未转义标签");
  assert.equal(html.includes("&lt;img src=x onerror=alert(1)&gt;"), true, "自由文本未按实体输出");
});

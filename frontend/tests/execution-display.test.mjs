// R26：执行权威给出成交状态与事实；前端只映射标签、展示持久化字段。
import assert from "node:assert/strict";
import test from "node:test";
import { readFileSync } from "node:fs";
import { fileURLToPath } from "node:url";
import path from "node:path";

const here = path.dirname(fileURLToPath(import.meta.url));
const source = readFileSync(path.join(here, "..", "src", "features", "paper.js"), "utf8");
const paper = await import(new URL("../src/features/paper.js", import.meta.url).href);
const execution = await import(new URL("../src/features/execution.js", import.meta.url).href);

test("委托生命周期状态只展示后端状态", () => {
  const cases = [
    ["pending_execution", "待执行"],
    ["partially_filled", "部分成交"],
    ["risk_rejected", "风控拒绝"],
    ["filled", "已成交"],
    ["cancelled", "已撤销"],
    ["superseded", "已失效或回收"],
  ];
  for (const [status, label] of cases) {
    assert.equal(paper.paperOrderStatusView(status)[1], label);
  }
});

test("执行流水原样展示成交份额、阻断原因、时点和行情信任事实", () => {
  const html = paper.paperExecutionFactsHtml({
    qty: 1000,
    filled_qty: 300,
    remaining_qty: 700,
    execution_asof: "2026-09-09 10:15:00",
    execution_market_asof: "2026-09-09",
    execution_market_freshness: "fresh",
    execution_market_verification: "verified",
    execution_reasons: ["T1_NOT_SELLABLE", "INSUFFICIENT_LIQUIDITY"],
    pricing_basis: "verified_quote_plus_deterministic_slippage",
    fees: 12.34,
    slippage: 0.02,
  });
  for (const text of [
    "成交 300 / 1000 股 · 剩余 700 股",
    "执行时点 2026-09-09 10:15:00",
    "市场 双源核验通过 · 新鲜",
    "市场 as-of 2026-09-09",
    "T+1 锁定",
    "成交量不足，等待后续成交",
    "￥12.34",
    "0.0200 元/股",
    "可信行情加固定滑点",
  ]) assert.ok(html.includes(text), `缺少执行事实：${text}`);
});

test("逐笔成交行使用成交事件数量，不覆盖成委托累计数量", () => {
  const html = paper.paperExecutionFactsHtml({
    order_id: 42,
    fill_date: "2026-09-09",
    qty: 300,
    filled_qty: 1000,
    remaining_qty: 0,
  });
  assert.ok(html.includes("本次成交 300 股"));
  assert.equal(html.includes("成交 1000 /"), false);
});

test("前端不实现成交资格或成交金额规则", () => {
  // Reason token 的展示映射允许存在；只审查执行事实投影，避免把页面中
  // 无关模块的日期过滤或状态文案误判成执行规则。
  const projection = source.split("export function paperExecutionFactsHtml(")[1]
    .split("export function setPaperTerminalFilter(")[0];
  for (const forbidden of [
    "sellable_qty", "limit_up", "limit_down", "is_suspended",
    "session_phase", "qty % 100", "qty%100", "fee_rate", "commission_rate",
  ]) assert.equal(projection.includes(forbidden), false, `前端执行投影读取或计算了规则：${forbidden}`);
});

/* 人工核验是写操作，全部弹窗必须走 ui/dialog.js（应用内模态），且
   取消（approved=false）绝不能继续提交。这里用最小假 DOM 驱动真实
   confirmDialog/promptDialog 流程，断言“点取消 → 零请求”。 */
function installDialogDom() {
  const previousDocument = globalThis.document;
  const previousWindow = globalThis.window;
  function makeNode() {
    const node = {
      id: "", className: "", style: {}, dataset: {}, value: "", textContent: "",
      _html: "", children: [], _queries: new Map(),
      setAttribute() {}, removeAttribute() {}, addEventListener() {}, removeEventListener() {},
      appendChild(child) { node.children.push(child); return child; },
      insertBefore(child) { node.children.unshift(child); return child; },
      focus() {}, remove() {},
      get offsetParent() { return {}; },
      querySelector(selector) {
        if (!node._queries.has(selector)) node._queries.set(selector, makeNode());
        return node._queries.get(selector);
      },
      querySelectorAll() { return []; },
    };
    Object.defineProperty(node, "innerHTML", {
      get() { return node._html; },
      set(value) { node._html = String(value); },
    });
    return node;
  }
  const body = makeNode();
  globalThis.document = { getElementById: () => null, createElement: () => makeNode(), body, activeElement: null };
  globalThis.window = { setTimeout: () => 0, clearTimeout: () => {} };
  return {
    lastMask() {
      const mask = body.children[body.children.length - 1];
      if (!mask) throw new Error("没有打开的应用内模态");
      return mask;
    },
    click(selector) { this.lastMask().querySelector(selector).onclick(); },
    setValue(selector, value) { this.lastMask().querySelector(selector).value = value; },
    restore() { globalThis.document = previousDocument; globalThis.window = previousWindow; },
  };
}

function withFetch(calls) {
  const previousFetch = globalThis.fetch;
  globalThis.fetch = async (path, init) => {
    calls.push({ path: String(path), method: init && init.method });
    return { ok: true, status: 200, json: async () => ({ ok: true }) };
  };
  return () => { globalThis.fetch = previousFetch; };
}

const tick = () => new Promise((resolve) => setImmediate(resolve));

test("驳回原因弹窗取消后不得提交核验请求", async () => {
  const calls = [];
  const dom = installDialogDom();
  const restoreFetch = withFetch(calls);
  try {
    const pending = execution.verifyExecutionOrder(7, false);
    dom.click('[data-action="cancel"]');
    await pending;
  } finally {
    restoreFetch();
    dom.restore();
  }
  assert.equal(calls.length, 0);
});

test("驳回核验在应用内模态确认后提交一次，并带上原因", async () => {
  const calls = [];
  const dom = installDialogDom();
  const restoreFetch = withFetch(calls);
  try {
    const pending = execution.verifyExecutionOrder(7, false);
    dom.setValue('[data-role="input"]', "事件与持仓不符");
    dom.click('[data-action="approve"]');
    await tick();
    dom.click('[data-action="approve"]');
    await pending;
  } finally {
    restoreFetch();
    dom.restore();
  }
  assert.equal(calls.length, 1);
  assert.equal(calls[0].method, "POST");
  assert.match(calls[0].path, /order_id=7/);
  assert.match(calls[0].path, /approved=0/);
  assert.match(calls[0].path, /note=%E4%BA%8B%E4%BB%B6/);
});

test("放行核验只提交一次 order_id/approved 写请求", async () => {
  const calls = [];
  const dom = installDialogDom();
  const restoreFetch = withFetch(calls);
  try {
    const pending = execution.verifyExecutionOrder(7, true);
    dom.click('[data-action="approve"]');
    await pending;
  } finally {
    restoreFetch();
    dom.restore();
  }
  assert.equal(calls.length, 1);
  assert.equal(calls[0].method, "POST");
  assert.match(calls[0].path, /^\/api\/paper\/execution-dispatch\/verify\?/);
  assert.match(calls[0].path, /order_id=7/);
  assert.match(calls[0].path, /approved=1/);
});

test("重复点击核验只产生一次写请求", async () => {
  const calls = [];
  const dom = installDialogDom();
  const restoreFetch = withFetch(calls);
  try {
    const first = execution.verifyExecutionOrder(7, true);
    const second = execution.verifyExecutionOrder(7, true);
    dom.click('[data-action="approve"]');
    await first;
    await second;
  } finally {
    restoreFetch();
    dom.restore();
  }
  assert.equal(calls.length, 1);
});

test("执行核验不再调用浏览器原生 confirm/alert/prompt", () => {
  const executionSource = readFileSync(path.join(here, "..", "src", "features", "execution.js"), "utf8");
  assert.doesNotMatch(executionSource, /window\.(confirm|alert|prompt)|\balert\(/);
  assert.match(executionSource, /from "\.\.\/ui\/dialog\.js"/);
});

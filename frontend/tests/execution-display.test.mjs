// R26：执行权威给出成交状态与事实；前端只映射标签、展示持久化字段。
import assert from "node:assert/strict";
import test from "node:test";
import { readFileSync } from "node:fs";
import { fileURLToPath } from "node:url";
import path from "node:path";

const here = path.dirname(fileURLToPath(import.meta.url));
const source = readFileSync(path.join(here, "..", "src", "features", "paper.js"), "utf8");
const paper = await import(new URL("../src/features/paper.js", import.meta.url).href);

test("委托生命周期状态只展示后端状态", () => {
  const cases = [
    ["pending_execution", "待执行"],
    ["partially_filled", "部分成交"],
    ["risk_rejected", "风控拒绝"],
    ["filled", "已成交"],
    ["cancelled", "已撤销"],
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

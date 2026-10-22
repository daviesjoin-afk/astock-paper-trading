// 风控中心的展示一致性回归：
//   1. 缺失的百分比/计数不得渲染成 "-%"（有单位、没数值），必须显示占位符。
//   2. 风控审计翻页失败后，页码不能被错误提示吞掉；翻页按钮禁用态必须来自
//      当前实际展示的数据快照，而不是失败请求前的旧值。
import assert from "node:assert/strict";
import test from "node:test";
import { loadPaperAudit, renderPaperRisk } from "../src/features/risk.js";

function withMockDocument(nodes, fn) {
  const previous = globalThis.document;
  const previousFetch = globalThis.fetch;
  globalThis.document = { getElementById: (id) => nodes[id] || null };
  try {
    return fn();
  } finally {
    if (previous === undefined) delete globalThis.document; else globalThis.document = previous;
    if (previousFetch === undefined) delete globalThis.fetch; else globalThis.fetch = previousFetch;
  }
}

test("风控卡片缺失百分比显示占位符，不出现 -%", () => {
  const target = { innerHTML: "" };
  withMockDocument({ paperRiskResult: target }, () => {
    renderPaperRisk({
      overall: { level: "normal", trade_permission: "允许", summary: "正常", sell_monitoring: "常规" },
      dynamic_risk: { mode: "normal", label: "正常" },
      data_source_health: { healthy: true },
      accounts: [{ level: "normal", name: "策略A", metrics: {} }],
      market: {}, fund_flow: {}, sentiment: {}, crowding: {},
      position_queue: [], data_quality: [], disclaimer: "仅供参考",
    });
  });
  const html = target.innerHTML;
  assert.equal(html.includes("-%"), false, `缺失百分比被渲染成 -%：${html.slice(0, 300)}`);
  assert.equal(html.includes("—"), true, "缺失值未显示占位符 —");
});

test("审计翻页失败后保留页码，禁用态来自当前展示的数据快照", async () => {
  const nodes = {
    paperRiskAuditHost: { innerHTML: "" },
    paperRiskAuditStatus: { textContent: "" },
    paperRiskAuditPrev: { disabled: false },
    paperRiskAuditNext: { disabled: false },
  };
  const page = (label, next) => ({
    accounts: [], alerts: [{ id: 1, time: "2026-09-01 10:00:00", name: label, level: "watch", reason: "x" }],
    pagination: { total: 51, has_next: !!next, next_cursor: next || null },
  });
  const previousDocument = globalThis.document;
  const previousFetch = globalThis.fetch;
  globalThis.document = { getElementById: (id) => nodes[id] || null };
  try {
    globalThis.fetch = async () => ({ ok: true, json: async () => page("第一页标的", "50") });
    await loadPaperAudit("current");
    assert.doesNotMatch(nodes.paperRiskAuditHost.innerHTML, /id="paperRiskAuditNext"[^>]*disabled/, "首屏有下一页时按钮应可用");
    assert.match(nodes.paperRiskAuditHost.innerHTML, /第 1 页/);

    globalThis.fetch = async () => ({ ok: false, status: 500, json: async () => ({ detail: "audit boom" }) });
    await loadPaperAudit("next");
    assert.match(nodes.paperRiskAuditStatus.textContent, /audit boom/);
    assert.match(nodes.paperRiskAuditStatus.textContent, /仍显示第 1 页/, "失败后页码被吞掉");
    assert.match(nodes.paperRiskAuditHost.innerHTML, /第一页标的/, "失败后不应清空已展示的数据");
    assert.equal(nodes.paperRiskAuditPrev.disabled, true, "第 1 页时上一页应禁用");
    assert.equal(nodes.paperRiskAuditNext.disabled, false, "下一页禁用态应来自当前展示快照");
  } finally {
    if (previousDocument === undefined) delete globalThis.document; else globalThis.document = previousDocument;
    if (previousFetch === undefined) delete globalThis.fetch; else globalThis.fetch = previousFetch;
  }
});

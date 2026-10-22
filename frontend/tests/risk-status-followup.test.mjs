import assert from "node:assert/strict";
import test from "node:test";
import { zhRiskText } from "../src/core/format.js";
import { renderPaperAudit } from "../src/features/risk.js";

test("行情拒绝状态整体翻译，不把子串翻译成已过期", () => {
  assert.equal(zhRiskText("rejected_stale_quote"), "行情核验未通过");
  assert.equal(zhRiskText("unverified"), "未核验");
  assert.equal(zhRiskText("MARKET_STALE"), "成交行情已过期");
});

test("没有告警也显示最新扫描失败和完成时间", () => {
  const html = renderPaperAudit({ alerts: [], scan_status: {
    status: "failed", finished_at: "2026-09-03 14:24:10", error: "provider <failure>"
  }});
  assert.ok(html.includes("2026-09-03 14:24:10 · 失败"));
  assert.ok(html.includes("审计表只记录触发事件"));
  assert.ok(html.includes("provider &lt;failure&gt;"));
  assert.ok(renderPaperAudit(null).includes("暂无记录"));
});

test("主行情字段缺失不会显示成独立来源未返回", () => {
  const html = renderPaperAudit({ alerts: [{ quote_validation: "incomplete",
    action: "rejected_stale_quote", reason: "主行情价格或涨跌幅无效" }] });
  assert.ok(html.includes("主源缺失"));
  assert.ok(html.includes("行情核验未通过"));
  assert.ok(!html.includes("rejected_"));
});

test("资金证据不足的历史审计完整显示中文，保留暂缓状态和委托关联", () => {
  const reason = ["exact_cycle_asof_nav_unavailable", "exact_cycle_asof_pending_capacity_unavailable",
    "exact_headroom_unavailable", "exact_market_quote_evidence_unavailable",
    "exact_market_value_by_account_unavailable", "exact_market_valued_classification_unavailable",
    "exact_nav_unavailable", "exact_pending_by_account_unavailable", "exact_pending_total_unavailable",
    "exact_pending_unavailable", "exact_strategy_version_return_series_unavailable", "exact_used_unavailable"].join("；");
  const row = { reason, action: "portfolio_allocation_deferred", execution_mode: "active",
    rule_version: "paper-risk-v4", quote_validation: "cross_source_unavailable",
    linked_signal: { id: 42, label: "待执行委托", intended_date: "2026-10-22" } };
  const html = renderPaperAudit({ alerts: [row] });
  assert.doesNotMatch(html, /exact_|portfolio_allocation|paper-risk-v|_待处理|_不可用/);
  assert.match(html, /本周期当前净值无法核验/);
  assert.match(html, /资金分配暂缓，等待证据补齐/);
  assert.match(html, /独立行情源未返回/);
  assert.match(html, /待执行委托 #42（2026-10-22）/);
  assert.match(html, /风控规则第4版/);
  assert.equal(row.reason, reason, "仅翻译展示，不改写原始审计事实");
  assert.equal(zhRiskText("future_risk_reason <script>"), "其他风控信息（待核查） &lt;script&gt;");
});

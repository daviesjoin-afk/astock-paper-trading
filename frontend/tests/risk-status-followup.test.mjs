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

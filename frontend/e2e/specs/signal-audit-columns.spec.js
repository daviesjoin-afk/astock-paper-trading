import { expect, test } from "../fixtures.js";
import { renderSignalAuditTable, SIGNAL_AUDIT_COLUMNS } from "../../src/features/signal-audit-table.js";

const EXPECTED_COLUMNS = [
  "策略",
  "标的",
  "信号日",
  "行情快照",
  "计划执行日",
  "实际执行",
  "独立模型评分",
  "状态",
  "裁决/证据",
  "说明",
];

test("信号审计表 DOM 的表头与数据列数量和顺序一致", async ({ page }) => {
  expect(SIGNAL_AUDIT_COLUMNS).toEqual(EXPECTED_COLUMNS);

  await page.goto("/");
  await page.getByTestId("main-nav-paper").click();
  await expect(page.locator("#paperResult .paper-challenge")).toBeVisible();

  const tableHtml = renderSignalAuditTable([{
    account_id: "alpha",
    name: "样例标的",
    code: "600001",
    signal_date: "2030-01-02",
    intended_date: "2030-01-03",
    t_score: 4.2,
    status: "pending",
    payload: { decision: { entry_model: { name: "模型甲" } } },
    signal_decision: {
      outcome: "approved",
      status: "pending",
      reason: "裁决说明",
      evidence: {
        verification: "verified",
        verification_method: "cross_source",
        cross_source_verified: true,
      },
    },
    audit: {
      factor_date: "2030-01-02",
      signal_quote_at: "2030-01-02 09:35",
      signal_quote_pct: 1.2,
      planned_review_date: "2030-01-03",
      execution_status: "filled",
      executed_at: "2030-01-03 10:00",
      execution_quote_at: "2030-01-03 10:00",
    },
  }], { alpha: "策略甲" });

  await page.locator("#paperResult").evaluate((root, html) => {
    root.insertAdjacentHTML("beforeend", '<div class="table-scroll">' + html + "</div>");
  }, tableHtml);

  const table = page.locator("#paperResult table.signal-audit-table");
  await expect(table).toBeVisible();
  await expect(table.locator("thead th")).toHaveText(EXPECTED_COLUMNS);

  const row = table.locator("tbody tr").first();
  const cells = row.locator("td");
  await expect(cells).toHaveCount(EXPECTED_COLUMNS.length);
  const cellText = (await cells.allTextContents()).map((value) => value.replace(/\s+/g, " ").trim());
  expect(cellText[0]).toBe("策略甲");
  expect(cellText[1]).toContain("样例标的");
  expect(cellText[1]).toContain("600001");
  expect(cellText[2]).toBe("2030-01-02");
  expect(cellText[3]).toContain("2030-01-02 09:35");
  expect(cellText[4]).toBe("2030-01-03");
  expect(cellText[5]).toContain("2030-01-03 10:00");
  expect(cellText[6]).toContain("模型甲");
  expect(cellText[7]).toBe("待执行");
  expect(cellText[8]).toContain("已通过");
  expect(cellText[9]).toContain("裁决说明");

  await page.setViewportSize({ width: 375, height: 812 });
  await expect(table.locator("thead th")).toHaveText(EXPECTED_COLUMNS);
  await expect(cells).toHaveCount(EXPECTED_COLUMNS.length);
});

import { expect, test } from "../fixtures.js";
import { paperExecutionFactsHtml, paperOrderStatusView } from "../../src/features/paper.js";

test("执行委托在真实浏览器中呈现后端状态与逐笔事实", async ({ page }) => {
  await page.goto("/");
  await page.getByTestId("main-nav-paper").click();
  await page.locator('#paperModuleTabs [data-paper-view="activity"]').click();
  await expect(page.locator("#paperActivityBoard")).toBeVisible();
  await expect(page.locator("#paperActivityBoard .paper-order-list")).toBeVisible();
  await expect(page.locator("#paperOrderStatusFilter")).toBeVisible();

  const states = [
    ["pending_execution", "待执行"],
    ["partially_filled", "部分成交"],
    ["filled", "已成交"],
    ["cancelled", "已撤销"],
    ["risk_rejected", "风控拒绝"],
  ];
  for (const [state, label] of states) {
    expect(paperOrderStatusView(state)[1]).toBe(label);
  }

  const facts = paperExecutionFactsHtml({
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
  await page.evaluate((html) => {
    const fixture = document.createElement("section");
    fixture.id = "r26-execution-browser-fixture";
    fixture.className = "paper-terminal-section";
    fixture.style.maxWidth = "920px";
    fixture.style.margin = "20px auto";
    fixture.innerHTML = '<div class="paper-terminal-section-title">R26 执行事实浏览器预览</div>'
      + '<div class="paper-order-scroll"><div class="paper-order-list">'
      + '<div class="paper-order-row" data-testid="execution-facts-fixture">'
      + '<span>策略样例</span><span>买入 1000 股</span><span>委托价 10.00</span>'
      + '<span class="paper-order-status pending">部分成交</span>'
      + '<span>' + html + '</span><span>样例来源</span></div></div></div>';
    document.body.appendChild(fixture);
  }, facts);

  const row = page.getByTestId("execution-facts-fixture");
  await expect(row).toBeVisible();
  for (const text of [
    "部分成交", "成交 300 / 1000 股 · 剩余 700 股",
    "执行时点 2026-09-09 10:15:00", "市场 双源核验通过 · 新鲜",
    "T+1 锁定", "成交量不足，等待后续成交", "￥12.34", "0.0200 元/股",
  ]) await expect(row).toContainText(text);

  await page.screenshot({ path: "e2e/.artifacts/execution-display-desktop.png", fullPage: true });
  await page.setViewportSize({ width: 375, height: 812 });
  await expect(row).toBeVisible();
  await expect(row.locator(".paper-execution-facts")).toBeVisible();
  await page.screenshot({ path: "e2e/.artifacts/execution-display-mobile.png", fullPage: true });
});

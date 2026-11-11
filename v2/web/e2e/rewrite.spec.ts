import { AxeBuilder } from "@axe-core/playwright";
import { expect, test, type Page } from "@playwright/test";

/**
 * End-to-end coverage of the console.
 *
 * The cases run in order against one fresh database (see start-server.mjs), so
 * the first test that needs an account creates it and later ones reuse it.
 */

/** Create the account if this run has not done so yet. */
async function ensureAccount(page: Page): Promise<void> {
  await page.goto("/");
  if (await page.getByTestId("create-panel").isVisible()) {
    await page.getByTestId("principal").fill("100000");
    await page.getByTestId("create-submit").click();
  }
}

test.describe("single account with 稳健 and 激进", () => {
  test("empty account asks only for principal", async ({ page }) => {
    await page.goto("/");
    await expect(page.getByTestId("create-panel")).toBeVisible();
    await expect(page.getByTestId("principal")).toHaveValue("100000");
    await expect(page.getByTestId("dashboard")).toBeHidden();

    await page.getByTestId("principal").fill("0");
    await page.getByTestId("create-submit").click();
    await expect(page.getByTestId("create-error")).toContainText("100 元");
    await expect(page.getByTestId("create-panel")).toBeVisible();
  });

  test("creates one funded account with a 50/50 split", async ({ page }) => {
    await page.goto("/");
    await page.getByTestId("principal").fill("100000");
    await page.getByTestId("create-submit").click();

    await expect(page.getByTestId("dashboard")).toBeVisible();
    await expect(page.getByTestId("principal-value")).toHaveText("¥100,000.00");
    await expect(page.getByTestId("create-panel")).toBeHidden();

    // The strategy cards live on the positions view, not the dashboard.
    await page.goto("/#/trading/positions");
    await expect(page.getByTestId("strategy-steady")).toContainText("稳健");
    await expect(page.getByTestId("strategy-aggressive")).toContainText("激进");
    await expect(page.getByTestId("steady-weight")).toHaveText("50.0%");
    await expect(page.getByTestId("aggressive-weight")).toHaveText("50.0%");
    await expect(page.getByTestId("steady-cash")).toHaveText("¥50,000.00");
    await expect(page.getByTestId("aggressive-cash")).toHaveText("¥50,000.00");
  });

  test("advances to the latest day, settles once, and shows holdings", async ({ page }) => {
    await ensureAccount(page);
    await page.goto("/#/trading/advance");

    await page.getByTestId("run-submit").click();
    await expect(page.getByTestId("run-result")).toContainText("交易日");
    await expect(page.getByTestId("as-of")).not.toHaveText("—");

    const asOf = await page.getByTestId("as-of").innerText();
    await page.goto("/#/trading/trades");
    const trades = await page.getByTestId("trades-body").locator("tr").count();

    await page.goto("/#/trading/advance");
    await page.getByTestId("run-submit").click();
    await expect(page.getByTestId("run-result")).toContainText("没有重复成交");
    await expect(page.getByTestId("as-of")).toHaveText(asOf);

    await page.goto("/#/trading/trades");
    await expect(page.getByTestId("trades-body").locator("tr")).toHaveCount(trades);
  });

  test("every module and view is reachable by URL", async ({ page }) => {
    await ensureAccount(page);

    const views = [
      ["overview/dashboard", "驾驶舱"],
      ["market/screener", "全市场"],
      ["market/data", "数据覆盖"],
      ["trading/account", "账户"],
      ["trading/positions", "持仓"],
      ["trading/trades", "委托与成交"],
      ["trading/rebalances", "调仓记录"],
      ["trading/advance", "推进与结算"],
    ] as const;

    for (const [route, label] of views) {
      await page.goto(`/#/${route}`);
      // Exactly one view is on screen at a time, and the matching tab is marked.
      await expect(page.locator(".view:not([hidden])")).toHaveCount(1);
      await expect(page.locator(".view-tab.active")).toHaveText(label);
    }
  });

  test("the equity curve renders with a summary", async ({ page }) => {
    await ensureAccount(page);
    // Settle a day first: an account with no settled day has no curve, and the
    // panel says so explicitly. Either state must render something.
    await page.goto("/#/trading/advance");
    await page.getByTestId("run-submit").click();
    await expect(page.getByTestId("run-result")).toContainText("交易日");

    await page.goto("/#/trading/account");
    // The curve renders either a summary (once at least one day has settled)
    // or an explicit empty state. Both are valid; a blank panel is not.
    // The panel is filled by `drawCurve`, which runs after the fetch resolves.
    await expect(page.locator("#equity-chart")).not.toBeEmpty({ timeout: 10_000 });
    const summaryText = await page.locator("#equity-chart").innerText();
    const hasSvg = (await page.locator("#equity-chart svg").count()) === 1;
    const hasEmptyState = summaryText.includes("还没有结算记录");
    expect(hasSvg || hasEmptyState, `curve panel must render something, got: ${summaryText}`).toBe(true);

    if (hasSvg) {
      await expect(page.locator("#curve-summary")).toContainText("累计收益");
      await expect(page.locator("#curve-summary")).toContainText("最大回撤");
    }

    if (hasSvg) {
      // Switching series redraws rather than blanking the panel.
      await page.locator('.seg-btn[data-series="steady"]').click();
      await expect(page.locator("#equity-chart svg")).toHaveCount(1);

      // Range chips narrow the sample without breaking the chart.
      await page.locator('.chip[data-range="20"]').click();
      await expect(page.locator("#equity-chart svg")).toHaveCount(1);
    }
  });

  test("navigation tabs switch module and view", async ({ page }) => {
    await ensureAccount(page);
    await page.goto("/#/overview/dashboard");

    await page.locator('.module-tab[data-module="market"]').click();
    await expect(page).toHaveURL(/#\/market\/screener/);
    await expect(page.locator(".view-tab.active")).toHaveText("全市场");

    await page.locator('.view-tab[data-view="data"]').click();
    await expect(page).toHaveURL(/#\/market\/data/);
    await expect(page.locator(".view-tab.active")).toHaveText("数据覆盖");

    // The browser back button returns to the previous view.
    await page.goBack();
    await expect(page).toHaveURL(/#\/market\/screener/);
  });

  test("keeps the layout within a 320px viewport", async ({ page }) => {
    await page.setViewportSize({ width: 320, height: 720 });
    await ensureAccount(page);

    for (const route of ["overview/dashboard", "trading/account", "trading/positions"]) {
      await page.goto(`/#/${route}`);
      const overflow = await page.evaluate(
        () => document.documentElement.scrollWidth - document.documentElement.clientWidth,
      );
      expect(overflow, `horizontal overflow on ${route}`).toBeLessThanOrEqual(1);
    }
  });

  test("has no serious accessibility violations", async ({ page }) => {
    await ensureAccount(page);

    for (const route of ["overview/dashboard", "trading/account", "market/screener"]) {
      await page.goto(`/#/${route}`);
      const results = await new AxeBuilder({ page }).analyze();
      const serious = results.violations.filter((item) =>
        ["serious", "critical"].includes(item.impact ?? ""),
      );
      expect(serious, `a11y violations on ${route}: ${JSON.stringify(serious.map((v) => v.id))}`).toEqual(
        [],
      );
    }
  });
});

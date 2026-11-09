import { expect, test } from "@playwright/test";
import { CYCLES, ensureFixtures } from "./fixtures";

test.beforeEach(async ({ request }) => {
  await ensureFixtures(request);
});

test.describe("A/B comparison panel", () => {
  test("shows both experiments side by side without ranking", async ({ page }) => {
    await page.route(/\/v2\/portfolio\?cycle_id=/, (route) => {
      const cycle = new URL(route.request().url()).searchParams.get("cycle_id");
      const cash = cycle === CYCLES.repro ? 9_500_000 : 10_000_000;
      return route.fulfill({
        json: {
          cycle_id: cycle, cash_cents: cash, initial_cash_cents: 10_000_000,
          runs_total: cycle === CYCLES.repro ? 2 : 1,
          positions: [{ symbol: "600000.SH" }], asof_day: "2026-11-02",
        },
      });
    });
    await page.route(/\/v2\/replay\?cycle_id=/, (route) => {
      const cycle = new URL(route.request().url()).searchParams.get("cycle_id");
      return route.fulfill({ json: { ok: cycle === CYCLES.holdings } });
    });
    await page.goto(`/?cycle_id=${CYCLES.holdings}`);
    await page.getByTestId("nav-experiment").click();
    await page.getByTestId("compare-candidate").fill(CYCLES.repro);
    await page.getByTestId("compare-run").click();
    const row = page.getByTestId("compare-body").locator("tr", { hasText: "现金" });
    await expect(row).toContainText("¥100000.00", { timeout: 15_000 });
    await expect(row).toContainText("¥95000.00");
    await expect(page.getByTestId("compare-note")).toContainText("不展示净值、收益率或排名");
    await expect(page.getByTestId("compare-note")).toContainText("少");
    const replay = page.getByTestId("compare-body").locator("tr", { hasText: "回放校验" });
    await expect(replay).toContainText("未通过");
  });

  test("refuses to compare an experiment with itself", async ({ page }) => {
    await page.goto(`/?cycle_id=${CYCLES.holdings}`);
    await page.getByTestId("nav-experiment").click();
    await page.getByTestId("compare-candidate").fill(CYCLES.holdings);
    await page.getByTestId("compare-run").click();
    await expect(page.getByTestId("compare-note")).toHaveText("两个实验标识相同，无法对照");
    await expect(page.getByTestId("compare-table")).toBeHidden();
  });

  test("requires both experiment ids", async ({ page }) => {
    await page.goto(`/?cycle_id=${CYCLES.holdings}`);
    await page.getByTestId("nav-experiment").click();
    await page.getByTestId("compare-base").fill("");
    await page.getByTestId("compare-run").click();
    await expect(page.getByTestId("compare-note")).toHaveText("请填写两个实验标识");
    await expect(page.getByTestId("compare-table")).toBeHidden();
  });
});

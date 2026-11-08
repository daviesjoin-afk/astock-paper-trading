import { expect, test } from "@playwright/test";
import { CYCLES, ensureFixtures } from "./fixtures";

test.beforeEach(async ({ request }) => {
  await ensureFixtures(request);
});

test.describe("price set catalogue", () => {
  test("lists published sets and checks coverage", async ({ page }) => {
    await page.route(/\/v2\/price-sets$/, (route) => route.fulfill({
      json: [
        {
          price_set_id: "reviewed-prices-2026-11", trust: "reviewed",
          market_data_pack_id: "cn-offline-fixture-pack-1",
          coverage_start: "2026-10-01", coverage_end: "2026-11-30",
          entries: 42, digest: "a".repeat(64),
        },
        {
          price_set_id: "smoke-synthetic", trust: "synthetic",
          market_data_pack_id: "cn-offline-fixture-pack-1",
          coverage_start: "2026-10-30", coverage_end: "2026-11-02",
          entries: 2, digest: "b".repeat(64),
        },
      ],
    }));
    await page.route(/\/v2\/price-sets\/coverage\?/, (route) => route.fulfill({
      json: {
        pack_id: "cn-offline-fixture-pack-1",
        symbols: ["600000.SH"], days: ["2026-10-30", "2026-11-02"],
        covered_pairs: 1,
        missing: [{ symbol: "600000.SH", day: "2026-11-02" }],
        digest: "c".repeat(64),
      },
    }));
    await page.goto(`/?cycle_id=${CYCLES.holdings}`);
    await page.getByTestId("nav-experiment").click();
    await expect(page.getByTestId("price-set-list")).toContainText("reviewed-prices-2026-11");
    await expect(page.getByTestId("price-set-list")).toContainText("smoke-synthetic · synthetic");

    await page.getByTestId("coverage-pack").fill("cn-offline-fixture-pack-1");
    await page.getByTestId("coverage-symbols").fill("600000.SH");
    await page.getByTestId("coverage-days").fill("2026-10-30,2026-11-02");
    await page.getByTestId("coverage-run").click();
    await expect(page.getByTestId("coverage-result")).toContainText("覆盖 1/2");
    await expect(page.getByTestId("coverage-result")).toContainText("600000.SH@2026-11-02");
  });

  test("an empty catalogue says so and refuses incomplete checks", async ({ page }) => {
    await page.route(/\/v2\/price-sets$/, (route) => route.fulfill({ json: [] }));
    await page.goto(`/?cycle_id=${CYCLES.holdings}`);
    await page.getByTestId("nav-experiment").click();
    await expect(page.getByTestId("price-set-empty")).toBeVisible();
    await page.getByTestId("coverage-run").click();
    await expect(page.getByTestId("coverage-result")).toContainText("请填写数据包");
  });
});

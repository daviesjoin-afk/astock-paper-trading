import { expect, test } from "@playwright/test";
import { CYCLES, ensureFixtures } from "./fixtures";

test.beforeEach(async ({ request }) => {
  await ensureFixtures(request);
});

test.describe("overview run status", () => {
  test("summarises accounts, sessions, price sets and evolution", async ({ page }) => {
    await page.route(/\/v2\/health$/, (route) => route.fulfill({
      json: {
        status: "ok", system: "astock-v2", paper_only: true,
        schema_version: 15, writer: "store-single-writer",
        accounts: 2, sessions_active: 3,
        price_sets: 2, reviewed_price_sets: 1, synthetic_price_sets: 1,
        evolution_events: 2,
      },
    }));
    await page.route(/\/v2\/evolution\/events\/page\?limit=1/, (route) => route.fulfill({
      json: {
        events: [{
          event_id: "e1", candidate_id: "research-momentum-v1-ab12cd34",
          status: "promoted", decided_at: "2026-11-05T15:00:00+08:00",
          decision: { reasons: [], digest: "a".repeat(64) }, digest: "b".repeat(64),
        }],
        next_before: null, next_before_id: null, scanned: 1,
      },
    }));
    await page.goto(`/?cycle_id=${CYCLES.holdings}`);
    await expect(page.getByTestId("overview-accounts")).toHaveText("账户：2", { timeout: 15_000 });
    await expect(page.getByTestId("overview-sessions")).toHaveText("会话：活动 3");
    await expect(page.getByTestId("overview-price-sets")).toHaveText("价格集合：2（审核 1 / 合成 1）");
    await expect(page.getByTestId("overview-evolution")).toContainText("进化：2 条");
    await expect(page.getByTestId("overview-evolution")).toContainText("最近 已晋级");
  });

  test("failed probes show dashes instead of stale numbers", async ({ page }) => {
    await page.route(/\/v2\/health$/, (route) => route.abort());
    await page.route(/\/v2\/evolution\/events\/page/, (route) => route.abort());
    await page.goto(`/?cycle_id=${CYCLES.holdings}`);
    await expect(page.getByTestId("overview-accounts")).toHaveText("账户：—", { timeout: 15_000 });
    await expect(page.getByTestId("overview-price-sets")).toHaveText("价格集合：—");
    await expect(page.getByTestId("overview-evolution")).toHaveText("进化：—");
  });
});

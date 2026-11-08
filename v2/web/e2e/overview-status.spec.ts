import { expect, test } from "@playwright/test";
import { CYCLES, ensureFixtures } from "./fixtures";

test.beforeEach(async ({ request }) => {
  await ensureFixtures(request);
});

test.describe("overview run status", () => {
  test("summarises accounts, sessions, price sets and evolution", async ({ page }) => {
    await page.route(/\/v2\/paper-accounts\/overview$/, (route) => route.fulfill({
      json: [
        {
          account_id: "a1", initial_cash_cents: 10_000_000, cash_cents: 9_600_000,
          reserve_cents: 1_000_000, created_at: "2026-11-08T09:00:00+08:00",
          slots: [], audit_ok: true,
          sessions: { created: 1, running: 1, paused: 0, completed: 2, stopped: 0 },
        },
        {
          account_id: "a2", initial_cash_cents: 5_000_000, cash_cents: 5_000_000,
          reserve_cents: 500_000, created_at: "2026-11-08T09:00:00+08:00",
          slots: [], audit_ok: true,
          sessions: { created: 0, running: 0, paused: 1, completed: 0, stopped: 0 },
        },
      ],
    }));
    await page.route(/\/v2\/price-sets$/, (route) => route.fulfill({ json: [{ price_set_id: "x" }] }));
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
    await expect(page.getByTestId("overview-price-sets")).toHaveText("价格集合：1");
    await expect(page.getByTestId("overview-evolution")).toContainText("最近 已晋级");
  });

  test("failed probes show dashes instead of stale numbers", async ({ page }) => {
    await page.route(/\/v2\/paper-accounts\/overview$/, (route) => route.abort());
    await page.route(/\/v2\/price-sets$/, (route) => route.abort());
    await page.route(/\/v2\/evolution\/events\/page/, (route) => route.abort());
    await page.goto(`/?cycle_id=${CYCLES.holdings}`);
    await expect(page.getByTestId("overview-accounts")).toHaveText("账户：—", { timeout: 15_000 });
    await expect(page.getByTestId("overview-price-sets")).toHaveText("价格集合：—");
    await expect(page.getByTestId("overview-evolution")).toHaveText("进化：—");
  });
});

import { test, expect } from "@playwright/test";
import { CYCLES, ensureFixtures } from "./fixtures";

test.beforeEach(async ({ request }) => {
  await ensureFixtures(request);
});

const openComparison = async (page: import("@playwright/test").Page) => {
  await page.goto(`/?cycle_id=${CYCLES.repro}`);
  await page.getByTestId("summary-strategy").waitFor();
  await page.getByTestId("nav-comparison").click();
  await expect(page.getByTestId("panel-comparison")).toBeVisible();
};

const fill = async (
  page: import("@playwright/test").Page,
  a: string,
  b: string,
  start = "2026-10-30",
  end = "2026-11-02",
) => {
  await page.getByTestId("compare-a").selectOption(a);
  await page.getByTestId("compare-b").selectOption(b);
  await page.getByTestId("compare-start").fill(start);
  await page.getByTestId("compare-end").fill(end);
};

const report = (overrides: Record<string, unknown> = {}) => ({
  version: "comparison-v1",
  ok: true,
  reasons: [] as string[],
  window: { start_day: "2026-10-30", end_day: "2026-11-02" },
  a: {
    identity: {
      cycle_id: CYCLES.repro, status: "active", strategy_version_id: "research-momentum-v1",
      research_input_id: "offline-research-input-v1", market_data_pack_id: "cn-offline-fixture-pack-1",
      execution_policy_version: "v2-paper-policy-6", initial_cash_cents: 10_000_000,
    },
    verification: {
      replay_ok: true, runs_verified: 2, fills_verified: 2,
      reproduction_ok: true, signals_verified: 2, detail: "ok",
    },
    totals: {
      decisions: 2, filled: 2, rejected: 0, fees_cents: 1529,
      cash_cents: 9_789_300,
      positions: [{ symbol: "600000.SH", quantity: 700, sellable_quantity: 0 }],
    },
  },
  b: {
    identity: {
      cycle_id: CYCLES.reproB, status: "active", strategy_version_id: "research-momentum-v1",
      research_input_id: "offline-research-input-v1", market_data_pack_id: "cn-offline-fixture-pack-1",
      execution_policy_version: "v2-paper-policy-6", initial_cash_cents: 10_000_000,
    },
    verification: {
      replay_ok: true, runs_verified: 2, fills_verified: 2,
      reproduction_ok: true, signals_verified: 2, detail: "ok",
    },
    totals: {
      decisions: 2, filled: 1, rejected: 1, fees_cents: 800,
      cash_cents: 9_900_000,
      positions: [{ symbol: "600000.SH", quantity: 300, sellable_quantity: 0 }],
    },
  },
  comparability: [
    { name: "research_input", matched: true, a: "d1", b: "d1" },
    { name: "market_data_pack", matched: true, a: "pack", b: "pack" },
    { name: "execution_policy", matched: true, a: "policy", b: "policy" },
    { name: "initial_cash", matched: true, a: "10000000", b: "10000000" },
  ],
  slots: [
    {
      decision_at: "2026-10-30T14:00:00+08:00",
      a: [{
        run_id: 1, signal_id: "s-1", side: "buy", status: "filled", reason: "filled",
        fill: { symbol: "600000.SH", side: "buy", quantity: 700, price_cents: 2153, fees_cents: 1529, trade_day: "2026-10-30" },
      }],
      b: [{ run_id: 2, signal_id: "s-1", side: "buy", status: "rejected", reason: "stale_quote", fill: null }],
    },
    {
      decision_at: "2026-11-02T10:00:00+08:00",
      a: [{
        run_id: 3, signal_id: "s-2", side: "sell", status: "filled", reason: "filled",
        fill: { symbol: "600000.SH", side: "sell", quantity: 100, price_cents: 2100, fees_cents: 21, trade_day: "2026-11-02" },
      }],
      b: [],
    },
  ],
  unmatched_a: 1,
  unmatched_b: 0,
  valuation: { status: "unavailable", reason: "no_reviewed_price_set" },
  digest: "abcdef0123456789abcdef0123456789abcdef0123456789abcdef0123456789",
  ...overrides,
});

test.describe("experiment comparison", () => {
  test("compares two reproducible experiments and states the missing valuation", async ({ page }) => {
    await openComparison(page);
    await fill(page, CYCLES.repro, CYCLES.reproB);
    await page.getByTestId("compare-run").click();

    await expect(page.getByTestId("compare-status")).toContainText("可比", { timeout: 20_000 });
    await expect(page.getByTestId("compare-status")).toHaveAttribute("data-state", "ok");
    await expect(page.getByTestId("compare-checks").locator("tr")).toHaveCount(4);
    await expect(page.getByTestId("compare-slots").locator("tr")).toHaveCount(2);
    await expect(page.getByTestId("compare-totals")).toContainText("费用合计");
    await expect(page.getByTestId("compare-valuation")).toContainText("估值不可用");
    await expect(page.getByTestId("compare-valuation")).toContainText("不计算收益");
    await expect(page.getByTestId("compare-digest")).not.toHaveText("—");
    await expect(page).toHaveURL(new RegExp(`a=${CYCLES.repro}`));
  });

  test("a rejection shows why and never renders partial facts", async ({ page }) => {
    await page.route("**/v2/comparisons?*", (route) => route.fulfill({
      json: report({
        ok: false,
        reasons: ["comparison_initial_cash_mismatch", "comparison_window_not_covered"],
        slots: [],
        unmatched_a: 0,
        unmatched_b: 0,
      }),
    }));
    await openComparison(page);
    await fill(page, CYCLES.repro, CYCLES.reproB);
    await page.getByTestId("compare-run").click();

    await expect(page.getByTestId("compare-status")).toContainText("不可比");
    await expect(page.getByTestId("compare-status")).toHaveAttribute("data-state", "fail");
    await expect(page.getByTestId("compare-reasons")).toContainText("初始资金不一致");
    await expect(page.getByTestId("compare-reasons")).toContainText("比较窗口没有被两个实验完整覆盖");
    await expect(page.locator("#compare-totals-wrap")).toBeHidden();
    await expect(page.locator("#compare-slots-wrap")).toBeHidden();
  });

  test("an invalid window is reported from the server", async ({ page }) => {
    await page.route("**/v2/comparisons?*", (route) => route.fulfill({
      status: 422, json: { error: "comparison_window_invalid" },
    }));
    await openComparison(page);
    await fill(page, CYCLES.repro, CYCLES.reproB, "2026-11-02", "2026-10-30");
    await page.getByTestId("compare-run").click();
    await expect(page.getByTestId("compare-status")).toContainText("比较失败");
    await expect(page.getByTestId("compare-status")).toContainText("Comparison Window Invalid");
    await expect(page.getByTestId("compare-status")).toHaveAttribute("data-state", "fail");
  });

  test("the same experiment is refused before any request", async ({ page }) => {
    const calls: string[] = [];
    page.on("request", (request) => {
      if (request.url().includes("/v2/comparisons")) calls.push(request.url());
    });
    await openComparison(page);
    await fill(page, CYCLES.repro, CYCLES.repro);
    await page.getByTestId("compare-run").click();
    await expect(page.getByTestId("compare-error")).toBeVisible();
    await expect(page.getByTestId("compare-error")).toContainText("不能是同一个实验");
    expect(calls).toEqual([]);
  });

  test("a late answer cannot overwrite a newer comparison", async ({ page }) => {
    let round = 0;
    await page.route("**/v2/comparisons?*", async (route) => {
      round += 1;
      const current = round;
      if (current === 1) await new Promise((resolve) => setTimeout(resolve, 700));
      await route.fulfill({ json: report({ digest: current === 1 ? "1".repeat(64) : "2".repeat(64) }) });
    });
    await openComparison(page);
    await fill(page, CYCLES.repro, CYCLES.reproB);
    await page.getByTestId("compare-run").click();
    await page.getByTestId("compare-end").fill("2026-11-03");
    await page.getByTestId("compare-run").click();

    await expect(page.getByTestId("compare-digest")).toHaveText("2".repeat(64), { timeout: 10_000 });
    await page.waitForTimeout(1_000);
    await expect(page.getByTestId("compare-digest")).toHaveText("2".repeat(64));
  });

  test("a deep link prefills and runs the comparison", async ({ page }) => {
    await page.goto(
      `/?cycle_id=${CYCLES.repro}&a=${CYCLES.repro}&b=${CYCLES.reproB}&from=2026-10-30&to=2026-11-02#/comparison`,
    );
    await expect(page.getByTestId("panel-comparison")).toBeVisible();
    await expect(page.getByTestId("compare-status")).toContainText("可比", { timeout: 20_000 });
    await expect(page.getByTestId("compare-start")).toHaveValue("2026-10-30");
    await expect(page.getByTestId("compare-end")).toHaveValue("2026-11-02");
  });

  test("comparison stays read-only", async ({ page }) => {
    const writes: string[] = [];
    page.on("request", (request) => {
      if (["POST", "PUT", "PATCH", "DELETE"].includes(request.method()) && request.url().includes("/v2/")) {
        writes.push(`${request.method()} ${request.url()}`);
      }
    });
    await openComparison(page);
    await fill(page, CYCLES.repro, CYCLES.reproB);
    await page.getByTestId("compare-run").click();
    await expect(page.getByTestId("compare-status")).toContainText("可比", { timeout: 20_000 });
    expect(writes).toEqual([]);
  });

  test("390px keeps the comparison readable without page overflow", async ({ page }) => {
    await page.setViewportSize({ width: 390, height: 844 });
    await openComparison(page);
    await fill(page, CYCLES.repro, CYCLES.reproB);
    await page.getByTestId("compare-run").click();
    await expect(page.getByTestId("compare-status")).toContainText("可比", { timeout: 20_000 });
    const overflow = await page.evaluate(() =>
      document.documentElement.scrollWidth - document.documentElement.clientWidth);
    expect(overflow).toBeLessThanOrEqual(1);
  });
});

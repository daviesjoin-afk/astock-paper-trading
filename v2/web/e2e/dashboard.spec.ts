import { test, expect } from "@playwright/test";
import { CYCLES, ensureFixtures } from "./fixtures";

test.beforeEach(async ({ request }) => {
  await ensureFixtures(request);
});

test.describe("experiment discovery and identity", () => {
  test("lists every experiment and switches the whole page to the chosen one", async ({ page }) => {
    await page.goto("/");
    const select = page.getByTestId("experiment-select");
    await expect(select).toBeVisible();
    await expect(select.locator("option")).toHaveCount(5);

    await select.selectOption(CYCLES.holdings);
    await expect(page).toHaveURL(new RegExp(`cycle_id=${CYCLES.holdings}`));
    await expect(page.getByTestId("summary-strategy")).toContainText("research-momentum-v1");
    await expect(page.getByTestId("summary-status")).toContainText("进行中");
    await expect(page.getByTestId("summary-pack")).toContainText("cn-offline-fixture-pack-1");
    await expect(page.getByTestId("summary-policy")).toContainText("v2-paper-policy-6");
    await expect(page.getByTestId("summary-input")).toContainText("offline-research-input-v1");
    await expect(page.getByTestId("kpi-initial-cash")).toContainText("100,000.00");
    await expect(page.getByTestId("kpi-cash")).toContainText("90,930.00");
    await expect(page.getByTestId("kpi-positions")).toHaveText("1");
    await expect(page.getByTestId("kpi-runs")).toHaveText("1");
    await expect(page.getByTestId("positions-body")).toContainText("600000.SH");
    await expect(page.getByTestId("positions-body")).toContainText("700");

    await select.selectOption(CYCLES.flat);
    await expect(page.getByTestId("kpi-cash")).toContainText("99,635.64");
    await expect(page.getByTestId("kpi-positions")).toHaveText("0");
    await expect(page.getByTestId("kpi-runs")).toHaveText("2");
    await expect(page.getByTestId("positions-empty-state")).toContainText("没有持仓");
  });

  test("a single experiment is selected automatically without inventing one", async ({ page }) => {
    await page.route(/\/v2\/experiments(\?.*)?$/, async (route) => {
      if (route.request().method() !== "GET") return route.continue();
      await route.fulfill({
        json: {
          experiments: [{
            cycle_id: CYCLES.holdings,
            status: "active",
            strategy_version_id: "research-momentum-v1",
            research_input_id: "offline-research-input-v1",
            market_data_pack_id: "cn-offline-fixture-pack-1",
            execution_policy_version: "v2-paper-policy-6",
            initial_cash_cents: 10_000_000,
            runs_total: 1,
          }],
        },
      });
    });
    await page.goto("/");
    await expect(page).toHaveURL(new RegExp(`cycle_id=${CYCLES.holdings}`));
    await expect(page.getByTestId("summary-strategy")).toContainText("research-momentum-v1");
  });

  test("an unknown cycle_id shows recovery instead of silently using another experiment", async ({ page }) => {
    await page.goto("/?cycle_id=e2e-does-not-exist");
    await expect(page.getByTestId("unknown-experiment")).toBeVisible();
    await expect(page.getByTestId("unknown-experiment")).toContainText("e2e-does-not-exist");
    await expect(page.getByTestId("summary-strategy")).toBeHidden();
    await page.getByTestId("experiment-select").selectOption(CYCLES.holdings);
    await expect(page.getByTestId("summary-strategy")).toContainText("research-momentum-v1");
  });

  test("no experiment explains how to create one locally", async ({ page }) => {
    await page.route(/\/v2\/experiments(\?.*)?$/, async (route) => {
      if (route.request().method() !== "GET") return route.continue();
      await route.fulfill({ json: { experiments: [] } });
    });
    await page.goto("/");
    const empty = page.getByTestId("empty-experiments");
    await expect(empty).toBeVisible();
    await expect(empty).toContainText("还没有实验");
    await expect(empty).toContainText("experiment_demo.py");
    await expect(page.getByText("请在地址中指定实验")).toHaveCount(0);
  });
});

test.describe("fact presentation", () => {
  test("empty positions and empty runs are explained, not rendered as bare headers", async ({ page }) => {
    await page.goto(`/?cycle_id=${CYCLES.empty}`);
    await expect(page.getByTestId("positions-empty-state")).toContainText("没有持仓");
    await expect(page.getByTestId("runs-empty-state")).toContainText("还没有决策");
    await expect(page.getByTestId("kpi-runs")).toHaveText("0");
  });

  test("filled rows stay clean and rejected rows keep the raw code", async ({ page }) => {
    await page.goto(`/?cycle_id=${CYCLES.holdings}`);
    const filled = page.getByTestId("runs-body").locator("tr").first();
    await expect(filled).toContainText("research-momentum-v1-0003-buy");
    await expect(filled).toContainText("买入");
    await expect(filled).toContainText("已成交");
    await expect(filled).not.toContainText("filled");

    await page.getByTestId("experiment-select").selectOption(CYCLES.rejected);
    const rejected = page.getByTestId("runs-body").locator("tr").first();
    await expect(rejected).toContainText("卖出");
    await expect(rejected).toContainText("已拒绝");
    await expect(rejected).toContainText("没有 T+1 可卖持仓");
    await expect(rejected).toContainText("no_t1_sellable_lot");
  });

  test("the table states that it shows only the latest decisions", async ({ page }) => {
    await page.goto(`/?cycle_id=${CYCLES.holdings}`);
    await expect(page.getByTestId("runs-section")).toContainText("最近 100 条");
  });

  test("a sellable-quantity column appears only under an explicit as-of date", async ({ page }) => {
    await page.goto(`/?cycle_id=${CYCLES.holdings}`);
    await expect(page.getByTestId("sellable-header")).toBeHidden();
    await page.getByTestId("asof-day").fill("2026-10-30");
    await page.getByTestId("asof-apply").click();
    await expect(page.getByTestId("sellable-header")).toBeVisible();
    await expect(page.getByTestId("positions-body")).toContainText("2026-10-30");
    await page.getByTestId("asof-clear").click();
    await expect(page.getByTestId("sellable-header")).toBeHidden();
  });
});

test.describe("verification is two independent facts", () => {
  test("replay and research reproduction report separately", async ({ page }) => {
    await page.goto(`/?cycle_id=${CYCLES.holdings}`);
    await expect(page.getByTestId("replay-status")).toContainText("未验证");
    await expect(page.getByTestId("reproduction-status")).toContainText("未验证");

    await page.getByTestId("verify-replay").click();
    await expect(page.getByTestId("replay-status")).toContainText("通过", { timeout: 15_000 });
    await expect(page.getByTestId("replay-status")).toContainText("1 次决策");
    await expect(page.getByTestId("reproduction-status")).toContainText("未验证");

    // One frozen signal out of two: replay is fine, reproduction must fail.
    await page.getByTestId("verify-reproduction").click();
    await expect(page.getByTestId("reproduction-status")).toContainText("失败", { timeout: 15_000 });
    await expect(page.getByTestId("replay-status")).toContainText("通过");
  });

  test("a full reproduction experiment passes both checks", async ({ page }) => {
    await page.goto(`/?cycle_id=${CYCLES.repro}`);
    await page.getByTestId("verify-replay").click();
    await page.getByTestId("verify-reproduction").click();
    await expect(page.getByTestId("replay-status")).toContainText("通过", { timeout: 15_000 });
    await expect(page.getByTestId("reproduction-status")).toContainText("通过", { timeout: 15_000 });
    await expect(page.getByTestId("reproduction-status")).toContainText("2 条研究信号");
  });
});

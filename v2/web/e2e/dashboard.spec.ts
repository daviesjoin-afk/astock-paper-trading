import { test, expect } from "@playwright/test";
import { CYCLES, ensureFixtures } from "./fixtures";

test.beforeEach(async ({ request }) => {
  await ensureFixtures(request);
});

const view = async (page: import("@playwright/test").Page, name: string) => {
  await page.getByTestId(`nav-${name}`).click();
};

test.describe("navigation and discovery", () => {
  test("the menu switches panels instead of stacking everything", async ({ page }) => {
    await page.goto(`/?cycle_id=${CYCLES.holdings}`);
    await expect(page.getByTestId("panel-overview")).toBeVisible();
    await expect(page.getByTestId("panel-positions")).toBeHidden();
    await expect(page.getByTestId("panel-decisions")).toBeHidden();

    await view(page, "positions");
    await expect(page.getByTestId("panel-positions")).toBeVisible();
    await expect(page.getByTestId("panel-overview")).toBeHidden();
    await expect(page.getByTestId("nav-positions")).toHaveAttribute("aria-current", "page");

    await view(page, "decisions");
    await expect(page.getByTestId("panel-decisions")).toBeVisible();
    await expect(page.getByTestId("panel-positions")).toBeHidden();
    await expect(page).toHaveURL(/#\/decisions/);
  });

  test("the overview names the experiment in business terms and hides identifiers", async ({ page }) => {
    await page.goto(`/?cycle_id=${CYCLES.holdings}`);
    await expect(page.getByTestId("summary-strategy")).toHaveText("动量策略");
    await expect(page.getByTestId("summary-input")).toHaveText("固定离线研究输入");
    await expect(page.getByTestId("summary-pack")).toHaveText("离线样本包");
    await expect(page.getByTestId("summary-policy")).toHaveText("默认执行规则");
    await expect(page.getByTestId("summary-status")).toHaveText("进行中");

    const body = await page.locator("body").innerText();
    for (const raw of ["research-momentum-v1", "offline-research-input-v1",
                       "cn-offline-fixture-pack-1", "v2-paper-policy-6", "beca60bb"]) {
      expect(body, `raw identifier leaked: ${raw}`).not.toContain(raw);
    }
  });

  test("technical identifiers exist only behind the collapsed details", async ({ page }) => {
    await page.goto(`/?cycle_id=${CYCLES.holdings}`);
    await view(page, "experiment");
    const details = page.locator("details.tech");
    await expect(details).not.toHaveAttribute("open", "");
    await expect(page.getByTestId("tech-strategy")).toBeHidden();
    await details.locator("summary").click();
    await expect(page.getByTestId("tech-strategy")).toHaveText("research-momentum-v1");
  });

  test("lists every experiment and switches the whole page to the chosen one", async ({ page }) => {
    await page.goto("/");
    const select = page.getByTestId("experiment-select");
    await expect(select).toBeVisible();
    await expect(select.locator("option")).toHaveCount(5);

    await select.selectOption(CYCLES.holdings);
    await expect(page).toHaveURL(new RegExp(`cycle_id=${CYCLES.holdings}`));
    await expect(page.getByTestId("kpi-initial-cash")).toContainText("100,000.00");
    await expect(page.getByTestId("kpi-cash")).toContainText("90,930.00");
    await expect(page.getByTestId("kpi-positions")).toHaveText("1");
    await expect(page.getByTestId("kpi-runs")).toHaveText("1");

    await select.selectOption(CYCLES.flat);
    await expect(page.getByTestId("kpi-cash")).toContainText("99,635.64");
    await expect(page.getByTestId("kpi-positions")).toHaveText("0");
    await expect(page.getByTestId("kpi-runs")).toHaveText("2");
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
    await expect(page.getByTestId("summary-strategy")).toHaveText("动量策略");
  });

  test("an unknown cycle_id shows recovery instead of silently using another experiment", async ({ page }) => {
    await page.goto("/?cycle_id=e2e-does-not-exist");
    await expect(page.getByTestId("unknown-experiment")).toBeVisible();
    await expect(page.getByTestId("unknown-experiment")).toContainText("e2e-does-not-exist");
    await expect(page.getByTestId("summary-strategy")).toBeHidden();
    await page.getByTestId("experiment-select").selectOption(CYCLES.holdings);
    await expect(page.getByTestId("summary-strategy")).toHaveText("动量策略");
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
  });
});

test.describe("facts per view", () => {
  test("positions view explains empty holdings", async ({ page }) => {
    await page.goto(`/?cycle_id=${CYCLES.empty}`);
    await view(page, "positions");
    await expect(page.getByTestId("positions-empty-state")).toContainText("没有持仓");
  });

  test("decisions view shows business labels, not internal codes", async ({ page }) => {
    await page.goto(`/?cycle_id=${CYCLES.holdings}`);
    await view(page, "decisions");
    const row = page.getByTestId("runs-body").locator("tr").first();
    await expect(row).toContainText("第 3 条信号");
    await expect(row).toContainText("买入");
    await expect(row).toContainText("已成交");
    await expect(row).toContainText("已按显式执行价与容量成交");
    await expect(row).not.toContainText("filled");
    const body = await page.locator("body").innerText();
    expect(body).not.toContain("research-momentum-v1-0003-buy");
  });

  test("rejections keep their raw reason only as a tooltip", async ({ page }) => {
    await page.goto(`/?cycle_id=${CYCLES.rejected}`);
    await view(page, "decisions");
    const row = page.getByTestId("runs-body").locator("tr").first();
    await expect(row).toContainText("已拒绝");
    await expect(row).toContainText("没有 T+1 可卖持仓");
    await expect(row.locator("td").last()).toHaveAttribute("title", "no_t1_sellable_lot");
    const body = await page.locator("body").innerText();
    expect(body).not.toContain("no_t1_sellable_lot");
  });

  test("the decisions view states its window", async ({ page }) => {
    await page.goto(`/?cycle_id=${CYCLES.holdings}`);
    await view(page, "decisions");
    await expect(page.getByTestId("panel-decisions")).toContainText("最近 100 条");
  });

  test("a sellable-quantity column appears only under an explicit as-of date", async ({ page }) => {
    await page.goto(`/?cycle_id=${CYCLES.holdings}`);
    await view(page, "positions");
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
    await view(page, "replay");
    await expect(page.getByTestId("replay-status")).toContainText("未验证");
    await page.getByTestId("verify-replay").click();
    await expect(page.getByTestId("replay-status")).toContainText("通过", { timeout: 15_000 });
    await expect(page.getByTestId("replay-status")).toContainText("1 次决策");

    await view(page, "reproduction");
    await expect(page.getByTestId("reproduction-status")).toContainText("未验证");
    await page.getByTestId("verify-reproduction").click();
    await expect(page.getByTestId("reproduction-status")).toContainText("失败", { timeout: 15_000 });

    await view(page, "replay");
    await expect(page.getByTestId("replay-status")).toContainText("通过");
  });

  test("a full reproduction experiment passes both checks", async ({ page }) => {
    await page.goto(`/?cycle_id=${CYCLES.repro}`);
    await view(page, "replay");
    await page.getByTestId("verify-replay").click();
    await expect(page.getByTestId("replay-status")).toContainText("通过", { timeout: 15_000 });
    await view(page, "reproduction");
    await page.getByTestId("verify-reproduction").click();
    await expect(page.getByTestId("reproduction-status")).toContainText("通过", { timeout: 15_000 });
    await expect(page.getByTestId("reproduction-status")).toContainText("2 条研究信号");
  });
});

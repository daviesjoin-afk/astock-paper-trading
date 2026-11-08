import { test, expect } from "@playwright/test";
import { CYCLES, ensureFixtures } from "./fixtures";

test.beforeEach(async ({ request }) => {
  await ensureFixtures(request);
});

const portfolio = (cycle: string, asof: string | null, overrides: Record<string, unknown> = {}) => ({
  cycle_id: cycle,
  initial_cash_cents: 10_000_000,
  cash_cents: 9_093_000,
  asof_day: asof,
  positions: [{ symbol: "600000.SH", quantity: 700, sellable_quantity: asof === null ? null : 0 }],
  lots: [],
  runs_total: 1,
  ...overrides,
});

const detail = (cycle: string, pack = "cn-offline-fixture-pack-1") => ({
  spec: {
    cycle_id: cycle,
    initial_cash_cents: 10_000_000,
    market_data_pack_id: pack,
    execution_policy_version: "v2-paper-policy-6",
    strategy: {
      strategy_version_id: "research-momentum-v1",
      research_input_id: "offline-research-input-v1",
      research_input_digest: "0".repeat(64),
      source_digest: "1".repeat(64),
      parameters_digest: "2".repeat(64),
    },
  },
  status: "active",
});

test.describe("as-of scope is never mixed", () => {
  test("a refresh keeps the applied as-of day and never asks for the current book", async ({ page }) => {
    await page.goto(`/?cycle_id=${CYCLES.holdings}`);
    await page.getByTestId("nav-positions").click();
    await page.getByTestId("asof-day").fill("2026-10-30");
    await page.getByTestId("asof-apply").click();
    await expect(page.getByTestId("sellable-header")).toBeVisible();
    await expect(page.getByTestId("kpi-scope")).toContainText("2026-10-30");

    const urls: string[] = [];
    page.on("request", (request) => {
      if (request.url().includes("/v2/portfolio")) urls.push(request.url());
    });
    await page.route(/\/v2\/portfolio\?cycle_id=e2e-holdings&asof_day=2026-10-30/, (route) =>
      route.fulfill({ status: 500, json: { error: "storage_error" } }));

    await page.getByTestId("refresh").click();
    await expect(page.getByTestId("positions-error")).toContainText("仍显示日终 2026-10-30");
    await expect(page.getByTestId("sellable-header")).toBeVisible();
    await expect(page.getByTestId("kpi-scope")).toContainText("2026-10-30");
    await expect(page.getByTestId("asof-day")).toHaveValue("2026-10-30");
    expect(urls.length).toBeGreaterThan(0);
    for (const url of urls) expect(url).toContain("asof_day=2026-10-30");
  });

  test("a response for another scope is an error, not a relabel", async ({ page }) => {
    await page.goto(`/?cycle_id=${CYCLES.holdings}`);
    await page.getByTestId("nav-positions").click();
    await page.route(/\/v2\/portfolio\?cycle_id=e2e-holdings&asof_day=2026-10-30/, (route) =>
      route.fulfill({ json: portfolio(CYCLES.holdings, null) }));

    await page.getByTestId("asof-day").fill("2026-10-30");
    await page.getByTestId("asof-apply").click();
    await expect(page.getByTestId("positions-error")).toContainText("资产口径不一致");
    await expect(page.getByTestId("kpi-scope")).toContainText("当前持仓");
    await expect(page.getByTestId("sellable-header")).toBeHidden();
  });

  test("the overview and the positions view print the same scope", async ({ page }) => {
    await page.goto(`/?cycle_id=${CYCLES.holdings}`);
    await page.getByTestId("nav-positions").click();
    await page.getByTestId("asof-day").fill("2026-10-30");
    await page.getByTestId("asof-apply").click();
    await expect(page.getByTestId("sellable-header")).toBeVisible();
    await expect(page.getByTestId("kpi-scope")).toContainText("日终 2026-10-30");
    await expect(page.getByTestId("positions-count")).toContainText("日终口径 2026-10-30");
    await expect(page.getByTestId("positions-body")).toContainText("日终口径 2026-10-30");
  });
});

test.describe("decision time is China time", () => {
  test("UTC, +08:00 and -08:00 instants render identically in Asia/Shanghai", async ({ page }) => {
    const runs = [
      {
        id: 1,
        request: { signal_id: "s-0001-buy", decision_at: "2026-10-30T06:00:00Z", signal: { side: "buy" } },
        outcome: { status: "filled", reason: "filled" },
      },
      {
        id: 2,
        request: { signal_id: "s-0002-buy", decision_at: "2026-10-30T14:00:00+08:00", signal: { side: "buy" } },
        outcome: { status: "filled", reason: "filled" },
      },
      {
        id: 3,
        request: { signal_id: "s-0003-sell", decision_at: "2026-10-30T20:00:00-08:00", signal: { side: "sell" } },
        outcome: { status: "rejected", reason: "stale_quote" },
      },
    ];
    await page.route("**/v2/simulations?*", (route) => route.fulfill({ json: runs }));
    await page.goto(`/?cycle_id=${CYCLES.holdings}`);
    await page.getByTestId("nav-decisions").click();

    const panel = page.getByTestId("panel-decisions");
    await expect(panel).toContainText("中国时间");
    const rows = page.getByTestId("runs-body").locator("tr");
    await expect(rows).toHaveCount(3);
    await expect(rows.nth(0)).toContainText("10-30 14:00");
    await expect(rows.nth(1)).toContainText("10-30 14:00");
    // 20:00 at -08:00 is 12:00 on the next day in China.
    await expect(rows.nth(2)).toContainText("10-31 12:00");
  });
});

test.describe("verification belongs to the current snapshot", () => {
  test("ok:false is reported as a failure, never as a pass", async ({ page }) => {
    await page.route("**/v2/replay?*", (route) => route.fulfill({
      json: { cycle_id: CYCLES.holdings, ok: false, runs_verified: 1, fills_verified: 0, cash_cents: 9_093_000 },
    }));
    await page.goto(`/?cycle_id=${CYCLES.holdings}`);
    await page.getByTestId("nav-replay").click();
    await page.getByTestId("verify-replay").click();
    await expect(page.getByTestId("replay-status")).toContainText("未通过");
    await expect(page.getByTestId("replay-status")).toHaveAttribute("data-state", "fail");
  });

  test("a reload marks earlier passes as needing re-verification", async ({ page }) => {
    await page.goto(`/?cycle_id=${CYCLES.holdings}`);
    await page.getByTestId("nav-replay").click();
    await page.getByTestId("verify-replay").click();
    await expect(page.getByTestId("replay-status")).toContainText("通过", { timeout: 15_000 });

    await page.getByTestId("refresh").click();
    await expect(page.getByTestId("replay-status")).toContainText("需要重新验证");
    await expect(page.getByTestId("replay-status")).toHaveAttribute("data-state", "stale");
    await expect(page.locator("#overview-replay")).toHaveAttribute("data-state", "stale");
  });

  test("a late answer cannot repaint another experiment", async ({ page }) => {
    await page.route("**/v2/replay?*", async (route) => {
      await new Promise((resolve) => setTimeout(resolve, 900));
      try {
        await route.fulfill({
          json: { cycle_id: CYCLES.holdings, ok: true, runs_verified: 1, fills_verified: 1, cash_cents: 9_093_000 },
        });
      } catch {
        // The page may have moved on; that is the expected path here.
      }
    });
    await page.goto(`/?cycle_id=${CYCLES.holdings}`);
    await page.getByTestId("nav-replay").click();
    await page.getByTestId("verify-replay").click();
    await page.getByTestId("experiment-select").selectOption(CYCLES.flat);
    await expect(page.locator("#context-cycle")).toHaveText(CYCLES.flat);
    await page.waitForTimeout(1_300);
    await page.getByTestId("nav-replay").click();
    await expect(page.getByTestId("replay-status")).not.toContainText("通过");
  });

  test("one check runs at a time per kind", async ({ page }) => {
    let calls = 0;
    await page.route("**/v2/replay?*", async (route) => {
      calls += 1;
      await new Promise((resolve) => setTimeout(resolve, 400));
      try {
        await route.fulfill({
          json: { cycle_id: CYCLES.holdings, ok: true, runs_verified: 1, fills_verified: 1, cash_cents: 9_093_000 },
        });
      } catch {
        // Aborted by the page.
      }
    });
    await page.goto(`/?cycle_id=${CYCLES.holdings}`);
    await page.getByTestId("nav-replay").click();
    await page.getByTestId("verify-replay").click();
    await expect(page.getByTestId("verify-replay")).toBeDisabled();
    await page.getByTestId("verify-replay").evaluate((node) => (node as HTMLButtonElement).click());
    await expect(page.getByTestId("replay-status")).toContainText("通过", { timeout: 10_000 });
    expect(calls).toBe(1);
  });
});

test.describe("catalogue truthfulness", () => {
  const summary = (cycle: string) => ({
    cycle_id: cycle,
    status: "active",
    strategy_version_id: "research-momentum-v1",
    research_input_id: "offline-research-input-v1",
    market_data_pack_id: "cn-offline-fixture-pack-1",
    execution_policy_version: "v2-paper-policy-6",
    initial_cash_cents: 10_000_000,
    runs_total: 0,
  });

  test("an experiment beyond the catalogue window is verified directly", async ({ page }) => {
    const beyond = "e2e-beyond-window";
    await page.route(/\/v2\/experiments\?limit=500$/, (route) =>
      route.fulfill({ json: { experiments: [summary(CYCLES.holdings)], total: 501, skipped: 0 } }));
    await page.route(/\/v2\/experiments\/e2e-beyond-window$/, (route) =>
      route.fulfill({ json: detail(beyond) }));
    await page.route(/\/v2\/portfolio\?cycle_id=e2e-beyond-window$/, (route) =>
      route.fulfill({ json: portfolio(beyond, null) }));
    await page.route(/\/v2\/simulations\?cycle_id=e2e-beyond-window$/, (route) =>
      route.fulfill({ json: [] }));

    await page.goto(`/?cycle_id=${beyond}`);
    await expect(page.getByTestId("summary-strategy")).toBeVisible();
    await expect(page.locator("#context-cycle")).toHaveText(beyond);
    await expect(page.getByTestId("cycle-hint")).toContainText("不在目录中");
  });

  test("a catalogue with only damaged rows is an error, not an empty state", async ({ page }) => {
    await page.route(/\/v2\/experiments\?limit=500$/, (route) =>
      route.fulfill({ json: { experiments: [], total: 3, skipped: 3 } }));
    await page.goto("/");
    await expect(page.getByTestId("catalogue-broken")).toBeVisible();
    await expect(page.getByTestId("catalogue-broken")).toContainText("目录登记 3 个实验");
    await expect(page.getByTestId("empty-experiments")).toBeHidden();
    await expect(page.getByTestId("nav-overview")).toBeDisabled();
  });

  test("partially damaged catalogues keep the readable experiments and say so", async ({ page }) => {
    await page.route(/\/v2\/experiments\?limit=500$/, (route) =>
      route.fulfill({ json: { experiments: [summary(CYCLES.holdings)], total: 2, skipped: 1 } }));
    await page.goto(`/?cycle_id=${CYCLES.holdings}`);
    await expect(page.getByTestId("catalogue-warning")).toBeVisible();
    await expect(page.getByTestId("catalogue-warning")).toContainText("1 个实验记录数据损坏");
    await expect(page.getByTestId("summary-strategy")).toBeVisible();
  });
});

test.describe("auditability of visible facts", () => {
  test("two data packs never share a label", async ({ page }) => {
    await page.goto(`/?cycle_id=${CYCLES.holdings}`);
    await expect(page.getByTestId("summary-pack")).toHaveText("离线样本包 #1");
    await page.route(/\/v2\/experiments\/e2e-holdings$/, (route) =>
      route.fulfill({ json: detail(CYCLES.holdings, "cn-offline-fixture-pack-2") }));
    await page.getByTestId("refresh").click();
    await expect(page.getByTestId("summary-pack")).toHaveText("离线样本包 #2");
  });

  test("unknown signal ids stay distinguishable without leaking the raw id", async ({ page }) => {
    const runs = [
      {
        id: 7,
        request: { signal_id: "alpha-shaped-zz", decision_at: "2026-10-30T06:00:00Z", signal: { side: "buy" } },
        outcome: { status: "filled", reason: "filled" },
      },
      {
        id: 8,
        request: { signal_id: "beta-shaped-qq", decision_at: "2026-10-30T06:05:00Z", signal: { side: "sell" } },
        outcome: { status: "rejected", reason: "stale_quote" },
      },
    ];
    await page.route("**/v2/simulations?*", (route) => route.fulfill({ json: runs }));
    await page.goto(`/?cycle_id=${CYCLES.holdings}`);
    await page.getByTestId("nav-decisions").click();
    const rows = page.getByTestId("runs-body").locator("tr");
    const first = await rows.nth(0).locator("td").nth(1).innerText();
    const second = await rows.nth(1).locator("td").nth(1).innerText();
    expect(first).not.toBe(second);
    expect(first).not.toBe("模拟信号");
    await expect(rows.nth(0)).toContainText("#7");
    const body = await page.locator("body").innerText();
    expect(body).not.toContain("alpha-shaped-zz");
  });

  test("the technical identifiers can be copied with the keyboard", async ({ page, context }) => {
    await context.grantPermissions(["clipboard-read", "clipboard-write"]);
    await page.goto(`/?cycle_id=${CYCLES.holdings}`);
    await page.getByTestId("nav-experiment").click();
    await page.locator("details.tech summary").click();
    await page.getByTestId("copy-tech").focus();
    await page.keyboard.press("Enter");
    await expect(page.getByTestId("live-status")).toContainText("技术细节已复制");
    const copied = await page.evaluate(() => navigator.clipboard.readText());
    expect(copied).toContain("策略标识: research-momentum-v1");
    expect(copied).toContain("数据包标识: cn-offline-fixture-pack-1");
  });
});

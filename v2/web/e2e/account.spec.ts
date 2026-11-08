import { test, expect } from "@playwright/test";
import { CYCLES, PAPER_ACCOUNT, ensureFixtures } from "./fixtures";

test.beforeEach(async ({ request }) => {
  await ensureFixtures(request);
});

const openAccount = async (page: import("@playwright/test").Page) => {
  await page.goto(`/?cycle_id=${CYCLES.repro}`);
  await page.getByTestId("summary-strategy").waitFor();
  await page.getByTestId("nav-account").click();
  await expect(page.getByTestId("panel-account")).toBeVisible();
};

test.describe("paper account console", () => {
  test("shows the split, both slots and the stored order", async ({ page }) => {
    await openAccount(page);
    await expect(page.getByTestId("account-initial")).toHaveText("¥100,000.00");
    await expect(page.getByTestId("account-reserve")).toHaveText("¥10,000.00");
    // One filled buy has left the account, so cash is below the initial capital.
    const cash = await page.getByTestId("account-cash").innerText();
    expect(cash).not.toBe("¥100,000.00");
    expect(cash.startsWith("¥9")).toBe(true);

    const slotA = page.getByTestId("account-slot-A");
    const slotB = page.getByTestId("account-slot-B");
    await expect(slotA).toContainText("槽位 A · 动量策略");
    await expect(slotA).toContainText("45.0%");
    await expect(slotA).toContainText("预算");
    await expect(slotA).toContainText("¥45,000.00");
    await expect(slotA).toContainText("600000.SH");
    await expect(slotA).toContainText("风控：单笔 ≤ 50% · 单标的 ≤ 25% · 现金 ≥ 10%");
    await expect(slotB).toContainText("槽位 B · 均值回归策略");
    await expect(slotB).toContainText("无持仓");

    await expect(page.getByTestId("account-valuation")).toBeVisible();
    await expect(page.getByTestId("account-valuation")).toContainText("估值不可用");
    await expect(page.getByTestId("account-valuation")).toContainText("不计算市值");
    await expect(page.getByTestId("account-valuation-detail")).toBeVisible();
    await expect(page.getByTestId("account-valuation-detail")).toContainText("估值快照");
    await expect(page.getByTestId("account-valuation-detail")).toContainText("600000.SH");
    await expect(page.getByTestId("account-audit")).toBeVisible();
    await expect(page.getByTestId("account-audit")).toContainText("账本自检：通过");
    await expect(page.getByTestId("account-audit")).toHaveAttribute("data-state", "ok");
    await expect(page.getByTestId("account-rebalances")).toBeHidden();
    await expect(page.getByTestId("account-exposure")).toBeVisible();
    await expect(page.getByTestId("account-exposure-summary")).toContainText("成本占用");
    await expect(page.getByTestId("account-exposure-breaches")).toContainText("无集中度超限");
    await page.getByTestId("account-audit-details").locator("summary").click();
    const checks = page.getByTestId("account-audit-checks").locator("li");
    await expect(checks).toHaveCount(9);
    await expect(checks.first()).toContainText("budget_split");
    await expect(checks.last()).toContainText("switch_chain");

    const orders = page.getByTestId("account-orders").locator("tr");
    await expect(orders).toHaveCount(1);
    await expect(orders.first()).toContainText("A");
    await expect(orders.first()).toContainText("已成交");
    await expect(page).toHaveURL(new RegExp(`account=${PAPER_ACCOUNT}`));
  });

  test("a reviewed price set renders market value and profit", async ({ page }) => {
    await page.route(/\/v2\/paper-accounts\/.+\/valuation\?/, (route) => route.fulfill({
      json: {
        account_id: "e2e-account", as_of_day: "2026-11-02", status: "valued", reason: "",
        requirements: [], missing: [], priced_symbols: 1, digest: "d".repeat(64),
        valuation: {
          trust: "reviewed", price_set_id: "reviewed-prices-2026-11-02",
          market_value_cents: 900_000, cash_cents: 9_000_000, equity_cents: 9_900_000,
          profit_cents: -100_000,
        },
      },
    }));
    await openAccount(page);
    await expect(page.getByTestId("account-valuation")).toContainText("估值可用", { timeout: 15_000 });
    await expect(page.getByTestId("account-valuation-detail")).toContainText("市值");
    await expect(page.getByTestId("account-valuation-detail")).toContainText("权益");
    await expect(page.getByTestId("account-valuation-detail")).toContainText("盈亏");
    await expect(page.getByTestId("account-valuation-detail")).toContainText("reviewed-prices-2026-11-02");
  });

  test("applied rebalances are listed with their weight change", async ({ page }) => {
    await page.route(/\/v2\/paper-accounts\/.+\/rebalances$/, (route) => route.fulfill({
      json: [{
        account_id: "e2e-account", from_slot: "A", to_slot: "B", amount_cents: 1_000_000,
        weights_before: [4500, 4500], weights_after: [3500, 5500],
        applied_at: "2026-11-03T09:00:00+08:00", note: "rebalance",
        proposal_digest: "a".repeat(64), digest: "b".repeat(64),
      }],
    }));
    await openAccount(page);
    await expect(page.getByTestId("account-rebalances")).toBeVisible({ timeout: 15_000 });
    const items = page.getByTestId("account-rebalance-list").locator("li");
    await expect(items).toHaveCount(1);
    await expect(items.first()).toContainText("A→B");
    await expect(items.first()).toContainText("45.0%/45.0% → 35.0%/55.0%");
    await expect(items.first()).toContainText("2026-11-03");
  });

  test("a valued equity series renders points and drawdown", async ({ page }) => {
    await page.route(/\/v2\/paper-accounts\/.+\/equity-series\?/, (route) => route.fulfill({
      json: {
        status: "valued", trust: "reviewed", price_set_id: "reviewed-prices-2026-11",
        points: [
          { day: "2026-10-30", cash_cents: 9_600_000, market_value_cents: 390_000, equity_cents: 9_990_000 },
          { day: "2026-11-02", cash_cents: 9_600_000, market_value_cents: 360_000, equity_cents: 9_960_000 },
        ],
        max_drawdown_cents: 30_000, max_drawdown_day: "2026-11-02",
        profit_cents: -40_000, digest: "e".repeat(64),
      },
    }));
    await openAccount(page);
    await expect(page.getByTestId("account-equity")).toBeVisible({ timeout: 15_000 });
    await expect(page.getByTestId("equity-summary")).toContainText("覆盖 2 个交易日");
    await expect(page.getByTestId("equity-summary")).toContainText("最大回撤");
    await expect(page.getByTestId("equity-summary")).toContainText("盈亏");
    const points = page.getByTestId("equity-points").locator("li");
    await expect(points).toHaveCount(2);
    await expect(points.first()).toContainText("2026-10-30");
  });

  test("a covered series also shows return, drawdown and fees", async ({ page }) => {
    await page.route(/\/v2\/paper-accounts\/.+\/equity-series\?/, (route) => route.fulfill({
      json: {
        status: "valued", trust: "reviewed", price_set_id: "reviewed-prices-2026-11",
        points: [
          { day: "2026-10-30", cash_cents: 9_600_000, market_value_cents: 390_000, equity_cents: 9_990_000 },
          { day: "2026-11-02", cash_cents: 9_600_000, market_value_cents: 360_000, equity_cents: 9_960_000 },
        ],
        max_drawdown_cents: 30_000, max_drawdown_day: "2026-11-02",
        profit_cents: -40_000, digest: "e".repeat(64),
      },
    }));
    await page.route(/\/v2\/paper-accounts\/.+\/performance\?/, (route) => route.fulfill({
      json: {
        account_id: "e2e-account", days: 2, start_equity_cents: 9_990_000, end_equity_cents: 9_960_000,
        profit_cents: -40_000, return_bps: -40, max_drawdown_cents: 30_000,
        max_drawdown_day: "2026-11-02", drawdown_bps: 30, fees_cents: 500, trades: 1,
        price_set_id: "reviewed-prices-2026-11", digest: "f".repeat(64),
      },
    }));
    await openAccount(page);
    await expect(page.getByTestId("equity-performance")).toBeVisible({ timeout: 15_000 });
    await expect(page.getByTestId("equity-performance")).toContainText("收益 ¥-400.00");
    await expect(page.getByTestId("equity-performance")).toContainText("回撤 0.30%");
    await expect(page.getByTestId("equity-performance")).toContainText("费用 ¥5.00");
    await expect(page.getByTestId("equity-performance")).toContainText("1 笔成交");
  });

  test("shows an allocation proposal and applies it", async ({ page }) => {
    let posted: Record<string, unknown> | null = null;
    await page.route(/\/v2\/paper-accounts\/.+\/allocation-proposal\?/, (route) => route.fulfill({
      json: {
        proposal: {
          status: "proposed", reserve_bps: 1_000, digest: "a".repeat(64),
          rationale: [
            { slot: "A", current_bps: 4_500, target_bps: 3_500, score_bps: -50, note: "保留下限" },
            { slot: "B", current_bps: 4_500, target_bps: 5_500, score_bps: 120, note: "获得更多预算" },
          ],
        },
        scores: [
          { slot: "A", return_bps: -100, drawdown_bps: 50, score_bps: -150, days: 2 },
          { slot: "B", return_bps: 20, drawdown_bps: 0, score_bps: 20, days: 2 },
        ],
      },
    }));
    await page.route(/\/v2\/paper-accounts\/.+\/rebalances$/, (route) => {
      if (route.request().method() === "POST") {
        posted = route.request().postDataJSON();
        return route.fulfill({ json: { account_id: "e2e-account", amount_cents: 1_000_000 } });
      }
      return route.fulfill({ json: [] });
    });
    await openAccount(page);
    await expect(page.getByTestId("account-allocation")).toBeVisible({ timeout: 15_000 });
    await expect(page.getByTestId("allocation-summary")).toContainText("目标：A 35.0% · B 55.0%");
    await expect(page.getByTestId("allocation-rationale")).toContainText("获得更多预算");
    await page.getByTestId("allocation-apply").click();
    await expect.poll(() => posted).not.toBeNull();
    expect(posted?.["targets"]).toEqual([3_500, 5_500]);
    expect(posted?.["proposal_digest"]).toBe("a".repeat(64));
    expect(String(posted?.["applied_at"])).toContain("T");
    await expect(page.getByTestId("live-status")).toContainText("资金再平衡已执行");
  });

  test("summarises sessions, price sets and the latest evolution event", async ({ page }) => {
    await page.route(/\/v2\/price-sets$/, (route) => route.fulfill({
      json: [{ price_set_id: "reviewed-prices-2026-11", trust: "reviewed" }],
    }));
    await page.route(/\/v2\/evolution\/events\/page\?limit=1/, (route) => route.fulfill({
      json: {
        events: [{
          event_id: "evo-status", candidate_id: "research-momentum-v1-ab12cd34",
          status: "promoted", decided_at: "2026-11-05T15:00:00+08:00",
          decision: { reasons: ["通过"], digest: "a".repeat(64) }, digest: "b".repeat(64),
        }],
        next_before: null, next_before_id: null, scanned: 1,
      },
    }));
    await openAccount(page);
    await expect(page.getByTestId("status-sessions")).toContainText("运行 0");
    await expect(page.getByTestId("status-price-sets")).toHaveText("价格集合：1", { timeout: 15_000 });
    await expect(page.getByTestId("status-evolution")).toContainText("最近 已晋级");
    await expect(page.getByTestId("status-evolution")).toContainText("research-momentum-v1-ab12cd34");
  });

  test("lists every account and switches on click", async ({ page }) => {
    await page.route(/\/v2\/paper-accounts\/overview$/, (route) => route.fulfill({
      json: [
        {
          account_id: "e2e-account", initial_cash_cents: 10_000_000, cash_cents: 9_600_000,
          reserve_cents: 1_000_000, created_at: "2026-11-08T09:00:00+08:00",
          slots: [
            { slot: "A", weight_bps: 4500, budget_cents: 4_500_000, cash_cents: 4_100_000 },
            { slot: "B", weight_bps: 4500, budget_cents: 4_500_000, cash_cents: 4_500_000 },
          ],
          sessions: { created: 0, running: 0, paused: 0, completed: 0, stopped: 0 },
          audit_ok: true,
        },
        {
          account_id: "e2e-session-account", initial_cash_cents: 10_000_000, cash_cents: 10_000_000,
          reserve_cents: 1_000_000, created_at: "2026-11-08T09:00:00+08:00",
          slots: [
            { slot: "A", weight_bps: 4500, budget_cents: 4_500_000, cash_cents: 4_500_000 },
            { slot: "B", weight_bps: 4500, budget_cents: 4_500_000, cash_cents: 4_500_000 },
          ],
          sessions: { created: 1, running: 0, paused: 0, completed: 0, stopped: 0 },
          audit_ok: true,
        },
      ],
    }));
    await openAccount(page);
    await expect(page.getByTestId("account-overview")).toBeVisible({ timeout: 15_000 });
    await expect(page.getByTestId("overview-e2e-account")).toContainText("A 45.0% / B 45.0%");
    await page.getByTestId("overview-e2e-session-account").click();
    await expect(page.getByTestId("account-select")).toHaveValue("e2e-session-account");
    await expect(page.getByTestId("overview-e2e-session-account")).toHaveAttribute("data-state", "active");
  });

  test("states whether reviewed prices cover the held symbols", async ({ page }) => {
    let coverageUrl = "";
    await page.route(/\/v2\/price-sets\/coverage\?/, (route) => {
      coverageUrl = route.request().url();
      return route.fulfill({
        json: {
          pack_id: "cn-offline-fixture-pack-1",
          symbols: ["600000.SH"], days: ["2026-10-30", "2026-11-08"],
          covered_pairs: 0,
          missing: [
            { symbol: "600000.SH", day: "2026-10-30" },
            { symbol: "600000.SH", day: "2026-11-08" },
          ],
          digest: "a".repeat(64),
        },
      });
    });
    await openAccount(page);
    await expect(page.getByTestId("account-coverage")).toBeVisible({ timeout: 15_000 });
    await expect(page.getByTestId("account-coverage")).toContainText("价格覆盖：0/2");
    await expect(page.getByTestId("account-coverage")).toContainText("600000.SH@2026-10-30");
    await expect(page.getByTestId("account-coverage")).toHaveAttribute("data-state", "gap");
    expect(coverageUrl).toContain("symbols=600000.SH");
  });

  test("shows account performance when reviewed prices exist", async ({ page }) => {
    await page.route(/\/v2\/paper-accounts\/.+\/performance\?/, (route) => route.fulfill({
      json: {
        days: 2, trades: 1, fees_cents: 500,
        start_equity_cents: 10_000_000, end_equity_cents: 9_960_000,
        profit_cents: -40_000, return_bps: -40,
        max_drawdown_cents: 30_000, max_drawdown_day: "2026-11-02",
        drawdown_bps: 30, price_set_id: "reviewed-prices-2026-11", digest: "a".repeat(64),
      },
    }));
    await openAccount(page);
    await expect(page.getByTestId("account-performance")).toBeVisible({ timeout: 15_000 });
    await expect(page.getByTestId("account-performance")).toContainText("收益 ¥-400.00");
    await expect(page.getByTestId("account-performance")).toContainText("回撤 0.30%");
    await expect(page.getByTestId("account-performance")).toContainText("1 笔成交");
  });

  test("hides account performance when prices are missing", async ({ page }) => {
    await page.route(/\/v2\/paper-accounts\/.+\/performance\?/, (route) => route.fulfill({
      json: { status: "unavailable", reason: "no_reviewed_price_set" },
    }));
    await openAccount(page);
    await expect(page.getByTestId("account-performance")).toBeHidden();
  });

  test("an empty catalogue explains itself instead of rendering an empty account", async ({ page }) => {
    await page.route("**/v2/paper-accounts", async (route) => {
      if (route.request().method() !== "GET") return route.continue();
      await route.fulfill({ json: [] });
    });
    await openAccount(page);
    await expect(page.getByTestId("account-empty")).toBeVisible();
    await expect(page.getByTestId("account-select")).toBeDisabled();
    await expect(page.locator("#account-body")).toBeHidden();
  });

  test("a missing account is reported, not silently replaced", async ({ page }) => {
    await page.route("**/v2/paper-accounts/e2e-account", (route) =>
      route.fulfill({ status: 404, json: { error: "paper_account_not_found" } }));
    await openAccount(page);
    await expect(page.getByTestId("account-error")).toContainText("读取账户失败");
    await expect(page.locator("#account-body")).toBeHidden();
  });

  test("a deep link selects the account", async ({ page }) => {
    await page.goto(`/?cycle_id=${CYCLES.repro}&account=${PAPER_ACCOUNT}#/account`);
    await expect(page.getByTestId("panel-account")).toBeVisible();
    await expect(page.getByTestId("account-select")).toHaveValue(PAPER_ACCOUNT);
    await expect(page.getByTestId("account-slot-A")).toBeVisible();
  });

  test("the console only reads", async ({ page }) => {
    const writes: string[] = [];
    page.on("request", (request) => {
      if (["POST", "PUT", "PATCH", "DELETE"].includes(request.method()) && request.url().includes("/v2/")) {
        writes.push(`${request.method()} ${request.url()}`);
      }
    });
    await openAccount(page);
    await expect(page.getByTestId("account-slot-A")).toBeVisible();
    expect(writes).toEqual([]);
  });

  test("390px keeps the account console inside the viewport", async ({ page }) => {
    await page.setViewportSize({ width: 390, height: 844 });
    await openAccount(page);
    await expect(page.getByTestId("account-slot-A")).toBeVisible();
    const overflow = await page.evaluate(() =>
      document.documentElement.scrollWidth - document.documentElement.clientWidth);
    expect(overflow).toBeLessThanOrEqual(1);
  });
});

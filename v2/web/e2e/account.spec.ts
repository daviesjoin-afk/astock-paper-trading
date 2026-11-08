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

import { test, expect } from "@playwright/test";
import { CYCLES, ensureFixtures } from "./fixtures";

test.beforeEach(async ({ request }) => {
  await ensureFixtures(request);
});

const openAccount = async (page: import("@playwright/test").Page) => {
  await page.goto(`/?cycle_id=${CYCLES.repro}`);
  await page.getByTestId("summary-strategy").waitFor();
  await page.getByTestId("nav-account").click();
  await expect(page.getByTestId("panel-account")).toBeVisible();
};

const createAccount = async (
  page: import("@playwright/test").Page,
  id: string,
  cash = "100000",
) => {
  await page.getByTestId("account-create-toggle").click();
  await expect(page.getByTestId("account-create")).toBeVisible();
  await page.getByTestId("create-id").fill(id);
  await page.getByTestId("create-cash").fill(cash);
  await page.getByTestId("create-a").selectOption("research-momentum-v1");
  await page.getByTestId("create-b").selectOption("research-reversion-v1");
  await page.getByTestId("create-submit").click();
};

test.describe("account creation", () => {
  test("creates a funded account with two reviewed strategies", async ({ page }) => {
    await openAccount(page);
    await createAccount(page, "ui-created-1");

    await expect(page.getByTestId("account-select")).toHaveValue("ui-created-1", { timeout: 15_000 });
    await expect(page.getByTestId("account-initial")).toHaveText("¥100,000.00");
    await expect(page.getByTestId("account-reserve")).toHaveText("¥10,000.00");
    await expect(page.getByTestId("account-slot-A")).toContainText("动量策略");
    await expect(page.getByTestId("account-slot-A")).toContainText("45.0%");
    await expect(page.getByTestId("account-slot-B")).toContainText("均值回归策略");
    await expect(page.getByTestId("live-status")).toContainText("已创建");
    await expect(page.locator("#account-create")).toBeHidden();
  });

  test("a duplicate account id is refused with the server reason", async ({ page }) => {
    await openAccount(page);
    await createAccount(page, "ui-duplicate");
    await expect(page.getByTestId("account-select")).toHaveValue("ui-duplicate", { timeout: 15_000 });

    await page.getByTestId("account-create-toggle").click();
    await page.getByTestId("create-id").fill("ui-duplicate");
    await page.getByTestId("create-cash").fill("100000");
    await page.getByTestId("create-submit").click();
    await expect(page.getByTestId("account-error")).toContainText("创建账户失败");
    await expect(page.getByTestId("account-error")).toContainText("Paper Account Conflict");
  });

  test("the same strategy cannot fill both slots", async ({ page }) => {
    await openAccount(page);
    await page.getByTestId("account-create-toggle").click();
    await page.getByTestId("create-id").fill("ui-same-strategy");
    await page.getByTestId("create-a").selectOption("research-momentum-v1");
    await page.getByTestId("create-b").selectOption("research-momentum-v1");
    await page.getByTestId("create-submit").click();
    await expect(page.getByTestId("account-error")).toContainText("不能使用同一策略");
    // Nothing was created.
    await expect(page.getByTestId("account-select")).not.toHaveValue("ui-same-strategy");
  });

  test("an invalid capital is refused before any request", async ({ page }) => {
    let posts = 0;
    page.on("request", (request) => {
      if (request.method() === "POST" && request.url().includes("/v2/paper-accounts")) posts += 1;
    });
    await openAccount(page);
    await page.getByTestId("account-create-toggle").click();
    await page.getByTestId("create-id").fill("ui-bad-cash");
    await page.getByTestId("create-cash").fill("0");
    await page.getByTestId("create-submit").click();
    await expect(page.getByTestId("account-error")).toContainText("至少 100 元");
    expect(posts).toBe(0);
  });

  test("the creation form fits a 390px viewport", async ({ page }) => {
    await page.setViewportSize({ width: 390, height: 844 });
    await openAccount(page);
    await page.getByTestId("account-create-toggle").click();
    await expect(page.getByTestId("account-create")).toBeVisible();
    const overflow = await page.evaluate(() =>
      document.documentElement.scrollWidth - document.documentElement.clientWidth);
    expect(overflow).toBeLessThanOrEqual(1);
  });
});

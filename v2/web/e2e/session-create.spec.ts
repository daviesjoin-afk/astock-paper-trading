import { test, expect } from "@playwright/test";
import { CYCLES, SESSION_ACCOUNT, ensureFixtures } from "./fixtures";

test.beforeEach(async ({ request }) => {
  await ensureFixtures(request);
});

const openSessionAccount = async (page: import("@playwright/test").Page) => {
  await page.goto(`/?cycle_id=${CYCLES.repro}&account=${SESSION_ACCOUNT}#/account`);
  await expect(page.getByTestId("account-select")).toHaveValue(SESSION_ACCOUNT);
  await expect(page.getByTestId("account-sessions")).toBeVisible();
};

test.describe("session creation from an experiment", () => {
  test("imports a matching experiment and runs the frozen decisions", async ({ page }) => {
    await openSessionAccount(page);
    await page.getByTestId("session-create-toggle").click();
    await expect(page.getByTestId("session-create")).toBeVisible();

    // Slot A is the momentum slot, so the momentum experiments are offered.
    await page.getByTestId("session-slot").selectOption("A");
    await expect(page.getByTestId("session-cycle")).toContainText(CYCLES.repro);
    await page.getByTestId("session-id").fill("ui-import-1");
    await page.getByTestId("session-cycle").selectOption(CYCLES.repro);
    await page.getByTestId("session-submit").click();

    const card = page.getByTestId("session-card-ui-import-1");
    await expect(card).toBeVisible({ timeout: 15_000 });
    await expect(card).toContainText("未开始");
    await expect(card).toContainText("0/2");
    await expect(page.getByTestId("live-status")).toContainText("已创建");

    await card.getByRole("button", { name: "启动" }).click();
    await expect(card).toContainText("运行中");
    await card.getByRole("button", { name: "运行到结束" }).click();
    await expect(card).toContainText("已完成", { timeout: 15_000 });
    await expect(card).toContainText("2/2");

    // The imported decisions landed in the account's slot-A orders.
    const orders = page.getByTestId("account-orders").locator("tr");
    await expect(orders.first()).toContainText("600000.SH");
  });

  test("a slot whose strategy has no experiment explains why it cannot run", async ({ page }) => {
    await openSessionAccount(page);
    await page.getByTestId("session-create-toggle").click();
    await page.getByTestId("session-slot").selectOption("B");
    await expect(page.getByTestId("session-hint")).toBeVisible();
    await expect(page.getByTestId("session-hint")).toContainText("没有可用实验");
    await expect(page.getByTestId("session-submit")).toBeDisabled();
    await expect(page.getByTestId("session-cycle")).toBeDisabled();
  });

  test("a missing session id is refused before any request", async ({ page }) => {
    let posts = 0;
    page.on("request", (request) => {
      if (request.method() === "POST" && request.url().includes("/from-experiment")) posts += 1;
    });
    await openSessionAccount(page);
    await page.getByTestId("session-create-toggle").click();
    await page.getByTestId("session-id").fill("");
    await page.getByTestId("session-submit").click();
    await expect(page.getByTestId("account-error")).toContainText("请填写会话标识");
    expect(posts).toBe(0);
  });

  test("the form fits a 390px viewport", async ({ page }) => {
    await page.setViewportSize({ width: 390, height: 844 });
    await openSessionAccount(page);
    await page.getByTestId("session-create-toggle").click();
    await expect(page.getByTestId("session-create")).toBeVisible();
    const overflow = await page.evaluate(() =>
      document.documentElement.scrollWidth - document.documentElement.clientWidth);
    expect(overflow).toBeLessThanOrEqual(1);
  });
});

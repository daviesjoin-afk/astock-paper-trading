import { test, expect } from "@playwright/test";
import { CYCLES, ensureFixtures } from "./fixtures";

test.beforeEach(async ({ request }) => {
  await ensureFixtures(request);
});

test.describe("keyboard shortcuts", () => {
  test("digits switch views and the live region reports it", async ({ page }) => {
    await page.goto(`/?cycle_id=${CYCLES.holdings}`);
    await expect(page.getByTestId("panel-overview")).toBeVisible();

    await page.keyboard.press("2");
    await expect(page.getByTestId("panel-positions")).toBeVisible();
    await expect(page).toHaveURL(/#\/positions/);
    await expect(page.getByTestId("live-status")).toContainText("已切换到持仓");

    await page.keyboard.press("6");
    await expect(page.getByTestId("panel-experiment")).toBeVisible();
    await page.keyboard.press("7");
    await expect(page.getByTestId("panel-account")).toBeVisible();
    await page.keyboard.press("3");
    await expect(page.getByTestId("panel-decisions")).toBeVisible();
  });

  test("controls advertise their shortcut to assistive technology", async ({ page }) => {
    await page.goto(`/?cycle_id=${CYCLES.holdings}`);
    await expect(page.getByTestId("nav-overview")).toHaveAttribute("aria-keyshortcuts", "1");
    await expect(page.getByTestId("nav-positions")).toHaveAttribute("aria-keyshortcuts", "2");
    await expect(page.getByTestId("nav-decisions")).toHaveAttribute("aria-keyshortcuts", "3");
    await expect(page.getByTestId("nav-experiment")).toHaveAttribute("aria-keyshortcuts", "6");
    await expect(page.getByTestId("nav-account")).toHaveAttribute("aria-keyshortcuts", "7");
    await expect(page.getByTestId("refresh")).toHaveAttribute("aria-keyshortcuts", "r");
    await expect(page.getByTestId("theme-toggle")).toHaveAttribute("aria-keyshortcuts", "t");
    await expect(page.getByTestId("shortcuts-open")).toHaveAttribute("aria-keyshortcuts", "?");
    await expect(page.getByTestId("experiment-select")).toHaveAttribute("aria-keyshortcuts", "/");
  });

  test("typing in a field never triggers a shortcut", async ({ page }) => {
    await page.goto(`/?cycle_id=${CYCLES.holdings}`);
    await page.getByTestId("nav-positions").click();
    await page.getByTestId("asof-day").focus();
    await page.keyboard.press("3");
    await page.keyboard.press("t");
    await expect(page.getByTestId("panel-positions")).toBeVisible();
    await expect(page.locator("html")).toHaveAttribute("data-theme", "light");
  });

  test("r refreshes, t flips the theme and / focuses the selector", async ({ page }) => {
    const portfolios: string[] = [];
    page.on("request", (request) => {
      if (request.url().includes("/v2/portfolio")) portfolios.push(request.url());
    });
    await page.goto(`/?cycle_id=${CYCLES.holdings}`);
    await expect(page.getByTestId("summary-strategy")).toBeVisible();
    const before = portfolios.length;

    await page.keyboard.press("r");
    await expect.poll(() => portfolios.length).toBeGreaterThan(before);

    await page.keyboard.press("t");
    await expect(page.locator("html")).toHaveAttribute("data-theme", "dark");
    await page.keyboard.press("t");
    await expect(page.locator("html")).toHaveAttribute("data-theme", "light");

    await page.keyboard.press("/");
    await expect(page.getByTestId("experiment-select")).toBeFocused();
  });

  test("? opens the reference sheet and Escape closes it", async ({ page }) => {
    await page.goto(`/?cycle_id=${CYCLES.holdings}`);
    await page.getByTestId("shortcuts-open").focus();
    await page.keyboard.press("?");
    const sheet = page.getByTestId("shortcuts");
    await expect(sheet).toBeVisible();
    await expect(sheet).toContainText("概览 · 持仓 · 决策 · 回放 · 重做 · 实验 · 账户");

    await page.keyboard.press("Escape");
    await expect(sheet).toBeHidden();
    await expect(page.getByTestId("shortcuts-open")).toBeFocused();
  });

  test("navigation shortcuts stay inert while a notice owns the screen", async ({ page }) => {
    await page.route(/\/v2\/experiments(\?.*)?$/, async (route) => {
      if (route.request().method() !== "GET") return route.continue();
      await route.fulfill({ status: 500, json: { error: "storage_error" } });
    });
    await page.goto("/");
    await expect(page.getByTestId("service-error")).toBeVisible();
    await page.keyboard.press("3");
    await expect(page.getByTestId("panel-decisions")).toBeHidden();
    await expect(page.getByTestId("service-error")).toBeVisible();
  });
});

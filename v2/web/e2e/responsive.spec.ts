import { test, expect } from "@playwright/test";
import { CYCLES, ensureFixtures } from "./fixtures";

test.beforeEach(async ({ request }) => {
  await ensureFixtures(request);
});

test.describe("responsive and keyboard", () => {
  test("390px viewport has no page-level horizontal overflow", async ({ page }) => {
    await page.setViewportSize({ width: 390, height: 844 });
    await page.goto(`/?cycle_id=${CYCLES.holdings}`);
    await expect(page.getByTestId("summary-strategy")).toBeVisible();
    const overflow = await page.evaluate(() =>
      document.documentElement.scrollWidth - document.documentElement.clientWidth);
    expect(overflow).toBeLessThanOrEqual(1);
  });

  test("390px keeps tables scrollable inside their own container", async ({ page }) => {
    await page.setViewportSize({ width: 390, height: 844 });
    await page.goto(`/?cycle_id=${CYCLES.holdings}`);
    await page.getByTestId("nav-decisions").click();
    const scroller = page.getByTestId("panel-decisions").locator(".table-scroll");
    const box = await scroller.evaluate((node) => ({
      scrollWidth: node.scrollWidth,
      clientWidth: node.clientWidth,
      tabIndex: node.tabIndex,
    }));
    expect(box.tabIndex).toBe(0);
    expect(box.scrollWidth).toBeGreaterThan(box.clientWidth);
  });

  test("the experiment selector is reachable and operable by keyboard", async ({ page }) => {
    await page.goto("/");
    const select = page.getByTestId("experiment-select");
    await select.focus();
    await expect(select).toBeFocused();
    await page.keyboard.press("ArrowDown");
    await page.keyboard.press("Enter");
    await expect(page.getByTestId("summary-strategy")).toBeVisible();
    const outline = await select.evaluate((node) => getComputedStyle(node).outlineStyle);
    expect(outline).not.toBe("none");
  });

  test("light is the default theme and dark stays readable", async ({ page }) => {
    await page.emulateMedia({ colorScheme: "light" });
    await page.goto(`/?cycle_id=${CYCLES.holdings}`);
    await expect(page.locator("html")).toHaveAttribute("data-theme", "light");
    const light = await page.evaluate(() => getComputedStyle(document.body).backgroundColor);
    await page.getByTestId("theme-toggle").click();
    await expect(page.locator("html")).toHaveAttribute("data-theme", "dark");
    const dark = await page.evaluate(() => getComputedStyle(document.body).backgroundColor);
    expect(dark).not.toBe(light);
    await expect(page.getByTestId("kpi-cash")).toBeVisible();
    await page.reload();
    await expect(page.locator("html")).toHaveAttribute("data-theme", "dark");
  });

  test("without a stored choice the operating system theme is used", async ({ page }) => {
    await page.emulateMedia({ colorScheme: "dark" });
    await page.goto(`/?cycle_id=${CYCLES.holdings}`);
    await expect(page.locator("html")).toHaveAttribute("data-theme", "dark");
    await expect(page.getByTestId("theme-toggle")).toHaveAttribute("aria-pressed", "true");

    // An explicit choice then wins over the system setting.
    await page.getByTestId("theme-toggle").click();
    await expect(page.locator("html")).toHaveAttribute("data-theme", "light");
    await page.emulateMedia({ colorScheme: "dark" });
    await page.reload();
    await expect(page.locator("html")).toHaveAttribute("data-theme", "light");
  });
});

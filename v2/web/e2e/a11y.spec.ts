import { test, expect } from "@playwright/test";
import AxeBuilder from "@axe-core/playwright";
import { CYCLES, ensureFixtures } from "./fixtures";

test.beforeEach(async ({ request }) => {
  await ensureFixtures(request);
});

const scan = async (page: import("@playwright/test").Page, name: string) => {
  const results = await new AxeBuilder({ page })
    .withTags(["wcag2a", "wcag2aa", "wcag21a", "wcag21aa"])
    .analyze();
  const summary = results.violations.map((violation) =>
    `${violation.id} (${violation.impact}): ${violation.nodes.length} node(s) · ${violation.help}`);
  expect(summary, `axe violations on ${name}`).toEqual([]);
};

for (const viewport of [{ width: 1280, height: 800, name: "desktop" }, { width: 390, height: 844, name: "mobile" }]) {
  test(`no WCAG A/AA violations in light theme at ${viewport.name}`, async ({ page }) => {
    await page.setViewportSize({ width: viewport.width, height: viewport.height });
    await page.goto(`/?cycle_id=${CYCLES.holdings}`);
    await expect(page.getByTestId("summary-strategy")).toBeVisible();
    await scan(page, `light-overview-${viewport.name}`);
    await page.getByTestId("nav-decisions").click();
    await scan(page, `light-decisions-${viewport.name}`);
  });

  test(`no WCAG A/AA violations in dark theme at ${viewport.name}`, async ({ page }) => {
    await page.setViewportSize({ width: viewport.width, height: viewport.height });
    await page.goto(`/?cycle_id=${CYCLES.holdings}`);
    await page.getByTestId("theme-toggle").click();
    await expect(page.locator("html")).toHaveAttribute("data-theme", "dark");
    await scan(page, `dark-overview-${viewport.name}`);
  });
}

test("skip link moves focus into the main region", async ({ page }) => {
  await page.goto(`/?cycle_id=${CYCLES.holdings}`);
  await page.keyboard.press("Tab");
  const skip = page.locator(".skip-link");
  await expect(skip).toBeFocused();
  await page.keyboard.press("Enter");
  const focused = await page.evaluate(() => document.activeElement?.id ?? "");
  expect(focused).toBe("main");
});

test("refreshing announces itself and keeps the explicit as-of scope", async ({ page }) => {
  const portfolios: string[] = [];
  page.on("request", (request) => {
    if (request.url().includes("/v2/portfolio")) portfolios.push(request.url());
  });
  await page.goto(`/?cycle_id=${CYCLES.holdings}`);
  await page.getByTestId("nav-positions").click();
  await page.getByTestId("asof-day").fill("2026-10-30");
  await page.getByTestId("asof-apply").click();
  await expect(page.getByTestId("sellable-header")).toBeVisible();

  // Announcements are transient; record the sequence instead of sampling it.
  await page.evaluate(() => {
    const target = document.getElementById("live") as HTMLElement;
    const seen: string[] = [];
    (window as unknown as { __live: string[] }).__live = seen;
    new MutationObserver(() => seen.push(target.textContent ?? "")).observe(target, {
      childList: true, characterData: true, subtree: true,
    });
  });
  await page.getByTestId("refresh").click();
  await expect(page.getByTestId("sellable-header")).toBeVisible();
  await expect.poll(() => portfolios.filter((url) => url.includes("asof_day=2026-10-30")).length)
    .toBeGreaterThanOrEqual(2);
  const announcements = await page.evaluate(() => (window as unknown as { __live: string[] }).__live);
  expect(announcements.join("|")).toContain("正在刷新");
});

test("an empty as-of date is a visible error, not a silent no-op", async ({ page }) => {
  await page.goto(`/?cycle_id=${CYCLES.holdings}`);
  await page.getByTestId("nav-positions").click();
  await page.getByTestId("asof-apply").click();
  await expect(page.getByTestId("asof-error")).toBeVisible();
  await expect(page.getByTestId("asof-day")).toHaveAttribute("aria-invalid", "true");
  await expect(page.getByTestId("live-status")).toContainText("请先选择日终日期");
});

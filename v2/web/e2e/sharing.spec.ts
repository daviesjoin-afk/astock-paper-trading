import { test, expect } from "@playwright/test";
import { CYCLES, ensureFixtures } from "./fixtures";

test.beforeEach(async ({ request }) => {
  await ensureFixtures(request);
});

test.describe("shareable view state", () => {
  test("the result filter round-trips through the URL", async ({ page }) => {
    await page.goto(`/?cycle_id=${CYCLES.rejected}`);
    await page.getByTestId("nav-decisions").click();
    await page.getByRole("button", { name: /^已拒绝/ }).click();
    await expect(page).toHaveURL(/result=rejected/);
    await expect(page).toHaveURL(/#\/decisions/);

    await page.reload();
    await expect(page.locator(".chip[data-filter='rejected']")).toHaveAttribute("aria-pressed", "true");
    await expect(page.getByTestId("runs-body").locator("tr")).toHaveCount(1);

    await page.getByRole("button", { name: /^全部/ }).click();
    await expect(page).not.toHaveURL(/result=/);
    await expect(page.getByTestId("runs-body").locator("tr")).toHaveCount(1);
  });

  test("the filter survives switching experiments", async ({ page }) => {
    await page.goto(`/?cycle_id=${CYCLES.holdings}`);
    await page.getByTestId("nav-decisions").click();
    await page.getByRole("button", { name: /^已拒绝/ }).click();
    await page.getByTestId("experiment-select").selectOption(CYCLES.rejected);
    await expect(page).toHaveURL(new RegExp(`cycle_id=${CYCLES.rejected}`));
    await expect(page).toHaveURL(/result=rejected/);
    await expect(page.locator(".chip[data-filter='rejected']")).toHaveAttribute("aria-pressed", "true");
    await expect(page.getByTestId("runs-body").locator("tr")).toHaveCount(1);
  });

  test("copy link yields the exact deep link and reports back", async ({ page, context }) => {
    await context.grantPermissions(["clipboard-read", "clipboard-write"]);
    await page.goto(`/?cycle_id=${CYCLES.rejected}`);
    await page.getByTestId("nav-positions").click();
    await page.getByTestId("nav-decisions").click();
    await page.getByRole("button", { name: /^已拒绝/ }).click();

    await page.getByTestId("copy-link").click();
    await expect(page.getByTestId("live-status")).toContainText("当前视图链接已复制");
    await expect(page.getByTestId("copy-link")).toContainText("已复制");

    const copied = await page.evaluate(() => navigator.clipboard.readText());
    expect(copied).toContain(`cycle_id=${CYCLES.rejected}`);
    expect(copied).toContain("result=rejected");
    expect(copied).toContain("#/decisions");
  });

  test("refreshing spins the refresh control until the data settles", async ({ page }) => {
    await page.route("**/v2/portfolio?*", async (route) => {
      await new Promise((resolve) => setTimeout(resolve, 700));
      try {
        await route.continue();
      } catch {
        // The page may abort a stale request; that is expected here.
      }
    });
    await page.goto(`/?cycle_id=${CYCLES.holdings}`);
    await expect(page.getByTestId("summary-strategy")).toBeVisible();
    await page.getByTestId("refresh").click();
    await expect(page.locator("html")).toHaveAttribute("data-busy", "true");
    await expect(page.locator("html")).toHaveAttribute("data-busy", "false", { timeout: 10_000 });
    await expect(page.getByTestId("kpi-cash")).toContainText("90,930.00");
  });
});

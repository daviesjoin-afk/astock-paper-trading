import { AxeBuilder } from "@axe-core/playwright";
import { expect, test } from "@playwright/test";

const api = "/v2";

test.describe("single account with 稳健 and 激进", () => {
  test("empty account asks only for principal", async ({ page }) => {
    await page.goto("/");
    await expect(page.getByTestId("create-panel")).toBeVisible();
    await expect(page.getByTestId("principal")).toHaveValue("100000");
    await expect(page.getByTestId("dashboard")).toBeHidden();

    await page.getByTestId("principal").fill("0");
    await page.getByTestId("create-submit").click();
    await expect(page.getByTestId("create-error")).toContainText("100 元");
    await expect(page.getByTestId("create-panel")).toBeVisible();
  });

  test("creates one funded account with a 50/50 split", async ({ page }) => {
    await page.goto("/");
    await page.getByTestId("principal").fill("100000");
    await page.getByTestId("create-submit").click();

    await expect(page.getByTestId("dashboard")).toBeVisible();
    await expect(page.getByTestId("principal-value")).toHaveText("¥100,000.00");
    await expect(page.getByTestId("strategy-steady")).toContainText("稳健");
    await expect(page.getByTestId("strategy-aggressive")).toContainText("激进");
    await expect(page.getByTestId("steady-weight")).toHaveText("50.0%");
    await expect(page.getByTestId("aggressive-weight")).toHaveText("50.0%");
    await expect(page.getByTestId("steady-cash")).toHaveText("¥50,000.00");
    await expect(page.getByTestId("aggressive-cash")).toHaveText("¥50,000.00");
    await expect(page.getByTestId("create-panel")).toBeHidden();
  });

  test("advances to the latest day, settles once, and shows holdings", async ({ page }) => {
    await page.goto("/");
    // This file runs in order against one fresh database. Earlier cases may
    // already have created the account, which is a valid UI state.
    if (await page.getByTestId("create-panel").isVisible()) {
      await page.getByTestId("principal").fill("100000");
      await page.getByTestId("create-submit").click();
    }
    await page.getByTestId("run-submit").click();
    await expect(page.getByTestId("run-result")).toContainText("交易日");
    await expect(page.getByTestId("as-of")).not.toHaveText("—");

    const asOf = await page.getByTestId("as-of").innerText();
    const trades = await page.getByTestId("trades-body").locator("tr").count();
    await page.getByTestId("run-submit").click();
    await expect(page.getByTestId("run-result")).toContainText("没有重复成交");
    await expect(page.getByTestId("as-of")).toHaveText(asOf);
    await expect(page.getByTestId("trades-body").locator("tr")).toHaveCount(trades);
  });

  test("keeps both cards within a 320px viewport", async ({ page }) => {
    await page.setViewportSize({ width: 320, height: 720 });
    await page.goto("/");
    if (await page.getByTestId("create-panel").isVisible()) {
      await page.getByTestId("principal").fill("100000");
      await page.getByTestId("create-submit").click();
    }
    await expect(page.getByTestId("strategy-steady")).toBeVisible();
    await expect(page.getByTestId("strategy-aggressive")).toBeVisible();
    const offenders = await page.evaluate(() =>
      [...document.querySelectorAll("body *")]
        .map((node) => {
          const element = node as HTMLElement;
          const box = element.getBoundingClientRect();
          return {
            tag: element.tagName.toLowerCase(),
            id: element.id,
            testid: element.dataset.testid ?? "",
            className: typeof element.className === "string" ? element.className : "",
            left: Math.round(box.left),
            right: Math.round(box.right),
            width: Math.round(box.width),
          };
        })
        .filter((item) => item.right > document.documentElement.clientWidth + 1)
        .slice(0, 12),
    );
    test.info().annotations.push({ type: "overflow", description: JSON.stringify(offenders) });
    const overflow = await page.evaluate(
      () => document.documentElement.scrollWidth - document.documentElement.clientWidth,
    );
    expect(overflow, JSON.stringify(offenders)).toBeLessThanOrEqual(1);
  });

  test("has no serious accessibility violations", async ({ page }) => {
    await page.goto("/");
    if (await page.getByTestId("create-panel").isVisible()) {
      await page.getByTestId("principal").fill("100000");
      await page.getByTestId("create-submit").click();
    }
    await expect(page.getByTestId("strategy-steady")).toBeVisible();
    const results = await new AxeBuilder({ page }).analyze();
    expect(
      results.violations.filter((item) => ["serious", "critical"].includes(item.impact ?? "")),
    ).toEqual([]);
  });
});

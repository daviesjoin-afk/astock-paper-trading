import { test } from "@playwright/test";
import { mkdirSync } from "node:fs";
import { fileURLToPath } from "node:url";
import { CYCLES, ensureFixtures } from "./fixtures";

// Screenshots are evidence, not a build artifact: they only run on demand
// (CAPTURE_UI=1) so CI never rewrites repository files.
const enabled = process.env.CAPTURE_UI === "1";
const prefix = process.env.CAPTURE_PREFIX || "after";
const dir = fileURLToPath(new URL("../../docs/assets/ui/", import.meta.url));

const capture = async (page: import("@playwright/test").Page, name: string) => {
  mkdirSync(dir, { recursive: true });
  await page.screenshot({ path: `${dir}/${prefix}-${name}.png`, fullPage: true });
};

test.describe("screenshots", () => {
  test.beforeEach(async ({ request }) => {
    test.skip(!enabled, "set CAPTURE_UI=1 to capture evidence screenshots");
    await ensureFixtures(request);
  });

  test("empty state", async ({ page }) => {
    await page.route(/\/v2\/experiments(\?.*)?$/, async (route) => {
      if (route.request().method() !== "GET") return route.continue();
      await route.fulfill({ json: { experiments: [] } });
    });
    await page.goto("/");
    await page.waitForTimeout(400);
    await capture(page, "empty");
  });

  test("normal experiment", async ({ page }) => {
    await page.goto(`/?cycle_id=${CYCLES.holdings}`);
    await page.waitForLoadState("networkidle").catch(() => undefined);
    await page.waitForTimeout(600);
    await capture(page, "normal");
  });

  test("error state", async ({ page }) => {
    await page.route("**/v2/**", (route) => route.abort("connectionrefused"));
    await page.goto("/");
    await page.waitForTimeout(400);
    await capture(page, "error");
  });

  test("mobile narrow", async ({ page }) => {
    await page.setViewportSize({ width: 390, height: 844 });
    await page.goto(`/?cycle_id=${CYCLES.holdings}`);
    await page.waitForLoadState("networkidle").catch(() => undefined);
    await page.waitForTimeout(600);
    await capture(page, "mobile");
  });
});

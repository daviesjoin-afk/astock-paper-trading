import { test, expect } from "@playwright/test";
import { CYCLES, ensureFixtures } from "./fixtures";

test.beforeEach(async ({ request }) => {
  await ensureFixtures(request);
});

test.describe("failure isolation", () => {
  test("an unavailable service is named, keeps a retry, and recovers", async ({ page }) => {
    let offline = true;
    await page.route("**/v2/**", async (route) => {
      if (offline) return route.abort("connectionrefused");
      return route.continue();
    });
    await page.goto("/");
    await expect(page.getByTestId("service-error")).toBeVisible();
    await expect(page.getByTestId("service-error")).toContainText("服务不可用");
    offline = false;
    await page.getByTestId("retry").click();
    await expect(page.getByTestId("experiment-select")).toBeVisible();
    await expect(page.getByTestId("service-error")).toBeHidden();
  });

  test("a failing decisions request does not hide portfolio facts", async ({ page }) => {
    await page.route("**/v2/simulations?*", async (route) => {
      await route.fulfill({ status: 500, json: { error: "storage_error" } });
    });
    await page.goto(`/?cycle_id=${CYCLES.holdings}`);
    await expect(page.getByTestId("kpi-cash")).toContainText("90,930.00");
    await expect(page.getByTestId("positions-body")).toContainText("600000.SH");
    await expect(page.getByTestId("runs-error")).toBeVisible();
    await expect(page.getByTestId("runs-error")).toContainText("决策读取失败");
  });

  test("a slow response for the previous experiment cannot overwrite the current one", async ({ page }) => {
    await page.route(/\/v2\/portfolio\?cycle_id=e2e-holdings/, async (route) => {
      await new Promise((resolve) => setTimeout(resolve, 1500));
      try {
        await route.continue();
      } catch {
        // The page aborted the stale request; that is the expected path.
      }
    });
    await page.goto(`/?cycle_id=${CYCLES.holdings}`);
    await page.getByTestId("experiment-select").selectOption(CYCLES.flat);
    await expect(page.getByTestId("kpi-cash")).toContainText("99,635.64");
    await page.waitForTimeout(2200);
    await expect(page.getByTestId("kpi-cash")).toContainText("99,635.64");
    await expect(page).toHaveURL(new RegExp(`cycle_id=${CYCLES.flat}`));
  });

  test("a malformed experiment list is reported, not rendered as empty", async ({ page }) => {
    await page.route(/\/v2\/experiments(\?.*)?$/, async (route) => {
      if (route.request().method() !== "GET") return route.continue();
      await route.fulfill({ status: 500, json: { error: "storage_error" } });
    });
    await page.goto("/");
    await expect(page.getByTestId("service-error")).toBeVisible();
  });
});

test.describe("error semantics", () => {
  test("a stored experiment that fails validation is an error, not a missing one", async ({ page }) => {
    await page.route("**/v2/experiments/e2e-holdings", async (route) => {
      if (route.request().method() !== "GET") return route.continue();
      await route.fulfill({ status: 422, json: { error: "invalid_experiment_spec" } });
    });
    await page.goto(`/?cycle_id=${CYCLES.holdings}`);
    await expect(page.getByTestId("unknown-experiment")).toBeHidden();
    await expect(page.getByTestId("service-error")).toBeVisible();
    await expect(page.getByTestId("service-detail")).toContainText("invalid_experiment_spec");
  });
});

test.describe("safety", () => {
  test("malicious text from the API is rendered as text, never as markup", async ({ page }) => {
    const payload = '<img src=x onerror="window.__xss=1"><script>window.__xss=1</script>';
    await page.route("**/v2/simulations?*", async (route) => {
      await route.fulfill({
        json: [{
          id: 1,
          request: { signal_id: payload, signal: { side: "buy" } },
          outcome: { status: "rejected", reason: payload },
        }],
      });
    });
    await page.route("**/v2/portfolio?*", async (route) => {
      await route.fulfill({
        json: {
          cycle_id: CYCLES.holdings,
          initial_cash_cents: 10_000_000,
          cash_cents: 9_093_000,
          asof_day: null,
          positions: [{ symbol: payload, quantity: 700, sellable_quantity: null }],
          lots: [],
          runs_total: 1,
        },
      });
    });
    await page.goto(`/?cycle_id=${CYCLES.holdings}`);
    await expect(page.getByTestId("runs-body")).toContainText("<img src=x");
    await expect(page.getByTestId("positions-body")).toContainText("<img src=x");
    await expect(page.locator("main img")).toHaveCount(0);
    expect(await page.evaluate(() => (window as unknown as { __xss?: number }).__xss)).toBeUndefined();
  });

  test("the page never creates experiments or calls a write endpoint on load", async ({ page }) => {
    const writes: string[] = [];
    page.on("request", (request) => {
      if (["POST", "PUT", "PATCH", "DELETE"].includes(request.method()) &&
          request.url().includes("/v2/")) {
        writes.push(`${request.method()} ${request.url()}`);
      }
    });
    await page.goto("/");
    await page.getByTestId("experiment-select").selectOption(CYCLES.flat);
    await page.getByTestId("verify-replay").click();
    await expect(page.getByTestId("replay-status")).toContainText("通过", { timeout: 15_000 });
    expect(writes).toEqual([]);
  });
});

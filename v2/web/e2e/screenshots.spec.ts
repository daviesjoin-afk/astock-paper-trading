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

const settle = async (page: import("@playwright/test").Page) => {
  await page.waitForLoadState("networkidle").catch(() => undefined);
  await page.waitForTimeout(400);
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
    await settle(page);
    await capture(page, "empty");
  });

  test("normal experiment", async ({ page }) => {
    await page.goto(`/?cycle_id=${CYCLES.holdings}`);
    await settle(page);
    await capture(page, "normal");
  });

  test("every panel", async ({ page }) => {
    await page.goto(`/?cycle_id=${CYCLES.holdings}`);
    await page.getByTestId("summary-strategy").waitFor();
    await settle(page);
    for (const view of ["positions", "decisions", "replay", "reproduction", "experiment"]) {
      await page.getByTestId(`nav-${view}`).click();
      await page.waitForTimeout(150);
      await capture(page, `view-${view}`);
    }
  });

  test("positions under an explicit as-of date", async ({ page }) => {
    await page.goto(`/?cycle_id=${CYCLES.holdings}`);
    await page.getByTestId("summary-strategy").waitFor();
    await page.getByTestId("nav-positions").click();
    await page.getByTestId("asof-day").fill("2026-10-30");
    await page.getByTestId("asof-apply").click();
    await page.getByTestId("sellable-header").waitFor();
    await settle(page);
    await capture(page, "positions-asof");
  });

  test("verification results", async ({ page }) => {
    await page.goto(`/?cycle_id=${CYCLES.repro}`);
    await page.getByTestId("summary-strategy").waitFor();
    await page.getByTestId("nav-replay").click();
    await page.getByTestId("verify-replay").click();
    await page.getByTestId("replay-status").filter({ hasText: "通过" }).waitFor();
    await capture(page, "replay-ok");
    await page.getByTestId("nav-reproduction").click();
    await page.getByTestId("verify-reproduction").click();
    await page.getByTestId("reproduction-status").filter({ hasText: "通过" }).waitFor();
    await capture(page, "reproduction-ok");
  });

  test("dark theme", async ({ page }) => {
    await page.goto(`/?cycle_id=${CYCLES.holdings}`);
    await page.getByTestId("summary-strategy").waitFor();
    await page.getByTestId("theme-toggle").click();
    await settle(page);
    await capture(page, "dark-overview");
    await page.getByTestId("nav-decisions").click();
    await settle(page);
    await capture(page, "dark-decisions");
  });

  test("empty tables", async ({ page }) => {
    await page.goto(`/?cycle_id=${CYCLES.empty}`);
    await page.getByTestId("summary-strategy").waitFor();
    await page.getByTestId("nav-positions").click();
    await settle(page);
    await capture(page, "positions-empty");
    await page.getByTestId("nav-decisions").click();
    await page.getByRole("button", { name: /^已拒绝/ }).click();
    await settle(page);
    await capture(page, "decisions-filter-empty");
  });

  test("filtered decisions with a shared link", async ({ page }) => {
    await page.goto(`/?cycle_id=${CYCLES.holdings}`);
    await page.getByTestId("summary-strategy").waitFor();
    await page.getByTestId("nav-decisions").click();
    await page.getByRole("button", { name: /^已成交/ }).click();
    await settle(page);
    await capture(page, "decisions-filtered");
  });

  test("paper account console", async ({ page }) => {
    await page.goto(`/?cycle_id=${CYCLES.repro}`);
    await page.getByTestId("summary-strategy").waitFor();
    await page.getByTestId("nav-account").click();
    await page.getByTestId("account-slot-A").waitFor();
    await settle(page);
    await capture(page, "account");
  });

  test("shortcut reference", async ({ page }) => {
    await page.goto(`/?cycle_id=${CYCLES.holdings}`);
    await page.getByTestId("summary-strategy").waitFor();
    await page.getByTestId("shortcuts-open").click();
    await page.waitForTimeout(200);
    await page.screenshot({ path: `${dir}/${prefix}-shortcuts.png` });
  });

  test("print layout", async ({ page }) => {
    await page.goto(`/?cycle_id=${CYCLES.holdings}`);
    await page.getByTestId("summary-strategy").waitFor();
    await page.emulateMedia({ media: "print" });
    await page.waitForTimeout(200);
    await page.screenshot({ path: `${dir}/${prefix}-print.png`, fullPage: true });
  });

  test("stale verification after a reload", async ({ page }) => {
    await page.goto(`/?cycle_id=${CYCLES.holdings}`);
    await page.getByTestId("summary-strategy").waitFor();
    await page.getByTestId("nav-replay").click();
    await page.getByTestId("verify-replay").click();
    await page.getByTestId("replay-status").filter({ hasText: "通过" }).waitFor();
    await page.getByTestId("refresh").click();
    await page.getByTestId("replay-status").filter({ hasText: "需要重新验证" }).waitFor();
    await capture(page, "verify-stale");
  });

  test("damaged catalogue", async ({ page }) => {
    await page.route(/\/v2\/experiments\?limit=500$/, (route) =>
      route.fulfill({ json: { experiments: [], total: 3, skipped: 3 } }));
    await page.goto("/");
    await page.getByTestId("catalogue-broken").waitFor();
    await capture(page, "catalogue-broken");
  });

  test("as-of scope label on the overview", async ({ page }) => {
    await page.goto(`/?cycle_id=${CYCLES.holdings}`);
    await page.getByTestId("summary-strategy").waitFor();
    await page.getByTestId("nav-positions").click();
    await page.getByTestId("asof-day").fill("2026-10-30");
    await page.getByTestId("asof-apply").click();
    await page.getByTestId("sellable-header").waitFor();
    await settle(page);
    await capture(page, "positions-asof-scope");
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
    await settle(page);
    await capture(page, "mobile");
  });

  test("a long decision list", async ({ page }) => {
    const runs = Array.from({ length: 60 }, (_, index) => ({
      id: index + 1,
      request: {
        signal_id: `signal-${String(2000 + index)}-buy`,
        decision_at: `2026-10-30T14:${String(59 - (index % 60)).padStart(2, "0")}:00Z`,
        signal: { side: index % 3 === 0 ? "sell" : "buy" },
      },
      outcome: index % 4 === 0
        ? { status: "rejected", reason: "stale_quote" }
        : { status: "filled", reason: "filled" },
    }));
    await page.route("**/v2/simulations?*", async (route) => {
      await route.fulfill({ json: runs });
    });
    await page.goto(`/?cycle_id=${CYCLES.holdings}`);
    await page.getByTestId("summary-strategy").waitFor();
    await page.getByTestId("nav-decisions").click();
    await settle(page);
    await capture(page, "decisions-long");
  });

  test("mobile decisions keep the horizontal scroller", async ({ page }) => {
    await page.setViewportSize({ width: 390, height: 844 });
    await page.goto(`/?cycle_id=${CYCLES.holdings}`);
    await page.getByTestId("summary-strategy").waitFor();
    await page.getByTestId("nav-decisions").click();
    await settle(page);
    await capture(page, "mobile-decisions");
  });

  test("small phone 320px", async ({ page }) => {
    await page.setViewportSize({ width: 320, height: 720 });
    await page.goto(`/?cycle_id=${CYCLES.holdings}`);
    await page.getByTestId("summary-strategy").waitFor();
    await settle(page);
    await capture(page, "mobile-320");
  });

  test("tablet width", async ({ page }) => {
    await page.setViewportSize({ width: 834, height: 1000 });
    await page.goto(`/?cycle_id=${CYCLES.holdings}`);
    await settle(page);
    await capture(page, "tablet");
  });
});

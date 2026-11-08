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

const VIEWS = ["overview", "positions", "decisions", "replay", "reproduction", "experiment", "account"] as const;

for (const viewport of [{ width: 1280, height: 800, name: "desktop" }, { width: 390, height: 844, name: "mobile" }]) {
  test(`no WCAG A/AA violations in light theme at ${viewport.name}`, async ({ page }) => {
    await page.setViewportSize({ width: viewport.width, height: viewport.height });
    await page.goto(`/?cycle_id=${CYCLES.holdings}`);
    await expect(page.getByTestId("summary-strategy")).toBeVisible();
    for (const view of VIEWS) {
      await page.getByTestId(`nav-${view}`).click();
      await expect(page.getByTestId(`panel-${view}`)).toBeVisible();
      await scan(page, `light-${view}-${viewport.name}`);
    }
  });

  test(`no WCAG A/AA violations in dark theme at ${viewport.name}`, async ({ page }) => {
    await page.setViewportSize({ width: viewport.width, height: viewport.height });
    await page.goto(`/?cycle_id=${CYCLES.holdings}`);
    await page.getByTestId("theme-toggle").click();
    await expect(page.locator("html")).toHaveAttribute("data-theme", "dark");
    for (const view of VIEWS) {
      await page.getByTestId(`nav-${view}`).click();
      await expect(page.getByTestId(`panel-${view}`)).toBeVisible();
      await scan(page, `dark-${view}-${viewport.name}`);
    }
  });
}

test("the account creation form is accessible", async ({ page }) => {
  await page.goto(`/?cycle_id=${CYCLES.holdings}`);
  await page.getByTestId("summary-strategy").waitFor();
  await page.getByTestId("nav-account").click();
  await page.getByTestId("account-create-toggle").click();
  await expect(page.getByTestId("account-create")).toBeVisible();
  await scan(page, "account-create");
});

test("the session creation form is accessible", async ({ page }) => {
  await page.goto(`/?cycle_id=${CYCLES.repro}&account=e2e-session-account#/account`);
  await page.getByTestId("session-card-e2e-session").waitFor();
  await page.getByTestId("session-create-toggle").click();
  await expect(page.getByTestId("session-create")).toBeVisible();
  await scan(page, "session-create");
});

test("the evolution history is accessible", async ({ page }) => {
  await page.route(/\/v2\/evolution\/events/, (route) => route.fulfill({
    json: { events: [{
      event_id: "evo-a11y", candidate_id: "research-momentum-v1-ab12cd34",
      status: "promoted", decided_at: "2026-11-05T15:00:00+08:00",
      decision: { reasons: ["通过：收益与回撤均在阈值内"], digest: "a".repeat(64) },
      digest: "b".repeat(64),
    }], next_before: null, next_before_id: null, scanned: 1 },
  }));
  await page.goto(`/?cycle_id=${CYCLES.repro}#/evolution`);
  await expect(page.getByTestId("evolution-event-evo-a11y")).toBeVisible();
  await scan(page, "evolution");
});

test("the shortcut sheet is an accessible modal", async ({ page }) => {
  await page.goto(`/?cycle_id=${CYCLES.holdings}`);
  await page.getByTestId("shortcuts-open").click();
  const sheet = page.getByTestId("shortcuts");
  await expect(sheet).toBeVisible();
  await scan(page, "shortcut-sheet");
  await page.getByTestId("shortcuts-close").click();
  await expect(sheet).toBeHidden();
});

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

test.describe("notice and interaction states", () => {
  test("the empty catalogue and its copy action are accessible", async ({ page }) => {
    await page.route(/\/v2\/experiments(\?.*)?$/, async (route) => {
      if (route.request().method() !== "GET") return route.continue();
      await route.fulfill({ json: { experiments: [] } });
    });
    await page.goto("/");
    await expect(page.getByTestId("empty-experiments")).toBeVisible();
    await expect(page.getByTestId("nav-overview")).toBeDisabled();
    await scan(page, "empty-catalogue");
  });

  test("a damaged catalogue notice is accessible", async ({ page }) => {
    await page.route(/\/v2\/experiments\?limit=500$/, (route) =>
      route.fulfill({ json: { experiments: [], total: 2, skipped: 2 } }));
    await page.goto("/");
    await expect(page.getByTestId("catalogue-broken")).toBeVisible();
    await scan(page, "catalogue-broken");
  });

  test("a partially damaged catalogue keeps its warning readable", async ({ page, request }) => {
    await page.route(/\/v2\/experiments\?limit=500$/, async (route) => {
      const response = await route.fetch();
      const body = await response.json();
      await route.fulfill({ json: { ...body, skipped: 1 } });
    });
    await page.goto(`/?cycle_id=${CYCLES.holdings}`);
    await expect(page.getByTestId("catalogue-warning")).toBeVisible();
    await scan(page, "catalogue-warning");
  });

  test("the service error state is accessible", async ({ page }) => {
    await page.route("**/v2/**", (route) => route.abort("connectionrefused"));
    await page.goto("/");
    await expect(page.getByTestId("service-error")).toBeVisible();
    await scan(page, "service-error");
  });

  test("the unknown experiment state is accessible", async ({ page }) => {
    await page.goto("/?cycle_id=e2e-does-not-exist");
    await expect(page.getByTestId("unknown-experiment")).toBeVisible();
    await scan(page, "unknown-experiment");
  });

  test("an applied as-of scope and a narrowed decision list are accessible", async ({ page }) => {
    await page.goto(`/?cycle_id=${CYCLES.holdings}`);
    await page.getByTestId("summary-strategy").waitFor();
    await page.getByTestId("nav-positions").click();
    await page.getByTestId("asof-day").fill("2026-10-30");
    await page.getByTestId("asof-apply").click();
    await expect(page.getByTestId("sellable-header")).toBeVisible();
    await scan(page, "positions-asof");

    await page.getByTestId("nav-decisions").click();
    await page.getByRole("button", { name: /^已拒绝/ }).click();
    await expect(page.locator("#runs-filtered-empty")).toBeVisible();
    await scan(page, "decisions-filter-empty");
  });

  test("the validation error inside a notification is accessible in dark mode", async ({ page }) => {
    await page.goto(`/?cycle_id=${CYCLES.holdings}`);
    await page.getByTestId("theme-toggle").click();
    await page.getByTestId("nav-positions").click();
    await page.getByTestId("asof-apply").click();
    await expect(page.getByTestId("asof-error")).toBeVisible();
    await scan(page, "dark-asof-error");
  });
});

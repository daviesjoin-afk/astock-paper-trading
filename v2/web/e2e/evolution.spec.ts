import { test, expect } from "@playwright/test";
import { CYCLES, ensureFixtures } from "./fixtures";

const events = [
  {
    event_id: "evo-1",
    candidate_id: "research-momentum-v1-ab12cd34",
    status: "promoted",
    decided_at: "2026-11-05T15:00:00+08:00",
    decision: { reasons: ["通过：收益 700 bps 超过基准 500 + 50，回撤 900 bps 在 1500 以内"], digest: "a".repeat(64) },
    digest: "b".repeat(64),
  },
  {
    event_id: "evo-2",
    candidate_id: "research-reversion-v1-99887766",
    status: "rejected",
    decided_at: "2026-11-04T15:00:00+08:00",
    decision: { reasons: ["优势不足：收益 510 bps 未超过基准 500 + 50"], digest: "c".repeat(64) },
    digest: "d".repeat(64),
  },
];

test.beforeEach(async ({ request }) => {
  await ensureFixtures(request);
});

const openEvolution = async (page: import("@playwright/test").Page) => {
  await page.goto(`/?cycle_id=${CYCLES.repro}#/evolution`);
  await expect(page.getByTestId("panel-evolution")).toBeVisible();
};

test.describe("evolution history", () => {
  test("lists promotion and rejection events with reasons", async ({ page }) => {
    await page.route(/\/v2\/evolution\/events/, (route) => route.fulfill({ json: events }));
    await openEvolution(page);
    const cards = page.getByTestId("evolution-events").locator("article");
    await expect(cards).toHaveCount(2);
    await expect(page.getByTestId("evolution-event-evo-1")).toContainText("research-momentum-v1-ab12cd34");
    await expect(page.getByTestId("evolution-event-evo-1").getByTestId("evolution-status")).toHaveText("已晋级");
    await expect(page.getByTestId("evolution-event-evo-1")).toContainText("通过：收益 700 bps");
    await expect(page.getByTestId("evolution-event-evo-2").getByTestId("evolution-status")).toHaveText("已淘汰");
    await expect(page.getByTestId("evolution-event-evo-2")).toContainText("优势不足");
  });

  test("an empty log explains itself", async ({ page }) => {
    await page.route(/\/v2\/evolution\/events/, (route) => route.fulfill({ json: [] }));
    await openEvolution(page);
    await expect(page.getByTestId("evolution-empty")).toBeVisible();
    await expect(page.getByTestId("evolution-events").locator("article")).toHaveCount(0);
  });

  test("the shortcut opens the evolution view", async ({ page }) => {
    await page.route(/\/v2\/evolution\/events/, (route) => route.fulfill({ json: [] }));
    await page.goto(`/?cycle_id=${CYCLES.repro}`);
    await page.getByTestId("summary-strategy").waitFor();
    await page.keyboard.press("8");
    await expect(page.getByTestId("panel-evolution")).toBeVisible();
    await expect(page).toHaveURL(/#\/evolution$/);
  });

  test("fits a 390px viewport", async ({ page }) => {
    await page.setViewportSize({ width: 390, height: 844 });
    await page.route(/\/v2\/evolution\/events/, (route) => route.fulfill({ json: events }));
    await openEvolution(page);
    await expect(page.getByTestId("evolution-events").locator("article")).toHaveCount(2);
    const overflow = await page.evaluate(() =>
      document.documentElement.scrollWidth - document.documentElement.clientWidth);
    expect(overflow).toBeLessThanOrEqual(1);
  });
});

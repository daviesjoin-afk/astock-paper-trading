import { test, expect } from "@playwright/test";
import { CYCLES, ensureFixtures } from "./fixtures";

const events = [
  {
    event_id: "evo-1",
    candidate_id: "research-momentum-v1-ab12cd34",
    status: "promoted",
    decided_at: "2026-11-05T15:00:00+08:00",
    base_cycle: "shadow-base-1",
    candidate_cycle: "shadow-candidate-1",
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


const pageOf = (list: unknown[], next?: { before: string; id: string }) => ({
  events: list,
  next_before: next?.before ?? null,
  next_before_id: next?.id ?? null,
  scanned: list.length,
});

const openEvolution = async (page: import("@playwright/test").Page) => {
  await page.goto(`/?cycle_id=${CYCLES.repro}#/evolution`);
  await expect(page.getByTestId("panel-evolution")).toBeVisible();
};

test.describe("evolution history", () => {
  test("lists promotion and rejection events with reasons", async ({ page }) => {
    await page.route(/\/v2\/evolution\/events/, (route) => route.fulfill({ json: pageOf(events) }));
    await openEvolution(page);
    const cards = page.getByTestId("evolution-events").locator("article");
    await expect(cards).toHaveCount(2);
    await expect(page.getByTestId("evolution-event-evo-1")).toContainText("research-momentum-v1-ab12cd34");
    await expect(page.getByTestId("evolution-event-evo-1").getByTestId("evolution-status")).toHaveText("已晋级");
    await expect(page.getByTestId("evolution-event-evo-1")).toContainText("通过：收益 700 bps");
    await expect(page.getByTestId("evolution-event-evo-1").getByTestId("evolution-links"))
      .toContainText("基准 shadow-base-1 · 候选 shadow-candidate-1");
    await expect(page.getByTestId("evolution-event-evo-2").getByTestId("evolution-status")).toHaveText("已淘汰");
    await expect(page.getByTestId("evolution-event-evo-2")).toContainText("优势不足");
  });

  test("filters narrow the log by candidate and status", async ({ page }) => {
    await page.route(/\/v2\/evolution\/events/, (route) => route.fulfill({ json: pageOf(events) }));
    await openEvolution(page);
    await page.getByTestId("evolution-filter-status").selectOption("rejected");
    await expect(page.getByTestId("evolution-events").locator("article")).toHaveCount(1);
    await expect(page.getByTestId("evolution-event-evo-2")).toBeVisible();

    await page.getByTestId("evolution-filter-status").selectOption("");
    await page.getByTestId("evolution-filter-candidate").fill("momentum");
    await expect(page.getByTestId("evolution-events").locator("article")).toHaveCount(1);
    await expect(page.getByTestId("evolution-event-evo-1")).toBeVisible();

    await page.getByTestId("evolution-filter-candidate").fill("no-such-candidate");
    await expect(page.getByTestId("evolution-events").locator("article")).toHaveCount(0);
    await expect(page.getByTestId("evolution-empty")).toContainText("没有匹配的事件");
  });

  test("compares two experiments at the signal level", async ({ page }) => {
    await page.route(/\/v2\/signal-diff/, (route) => route.fulfill({
      json: {
        base_cycle: "shadow-base-1", candidate_cycle: "shadow-candidate-1",
        base_runs: 2, candidate_runs: 1, matched: 1,
        base_only: [{
          decision_at: "2026-10-30T14:00:00+08:00",
          quote_ref: { market_data_pack_id: "p", source: "offline-fixture", quote_id: "q-1359" },
          symbol: "600000.SH", side: "buy",
        }],
        candidate_only: [],
        digest: "a".repeat(64),
      },
    }));
    await page.route(/\/v2\/evolution\/events/, (route) => route.fulfill({ json: pageOf([]) }));
    await openEvolution(page);
    await page.getByTestId("diff-base").fill("shadow-base-1");
    await page.getByTestId("diff-candidate").fill("shadow-candidate-1");
    await page.getByTestId("diff-run").click();
    await expect(page.getByTestId("diff-summary")).toContainText("命中 1");
    await expect(page.getByTestId("diff-summary")).toContainText("基准独有 1");
    await expect(page.getByTestId("diff-list")).toContainText("基准独有");
    await expect(page.getByTestId("diff-list")).toContainText("600000.SH 买入");
  });

  test("signal comparison needs both experiment ids", async ({ page }) => {
    await page.route(/\/v2\/evolution\/events/, (route) => route.fulfill({ json: pageOf([]) }));
    await openEvolution(page);
    await page.getByTestId("diff-run").click();
    await expect(page.getByTestId("diff-error")).toContainText("请填写基准与候选实验 id");
    await expect(page.getByTestId("diff-result")).toBeHidden();
  });


  test("loads older events through the cursor", async ({ page }) => {
    await page.route(/\/v2\/evolution\/events/, (route) => {
      const url = route.request().url();
      if (url.includes("before=")) {
        return route.fulfill({
          json: pageOf([{
            event_id: "evo-old",
            candidate_id: "research-momentum-v1-older",
            status: "kept",
            decided_at: "2026-11-01T15:00:00+08:00",
            decision: { reasons: ["保持：风险与优势均在阈值内"], digest: "e".repeat(64) },
            digest: "f".repeat(64),
          }]),
        });
      }
      return route.fulfill({
        json: pageOf(events, { before: "2026-11-04T15:00:00+08:00", id: "evo-2" }),
      });
    });
    await openEvolution(page);
    await expect(page.getByTestId("evolution-more")).toBeVisible();
    await page.getByTestId("evolution-more").click();
    await expect(page.getByTestId("evolution-event-evo-old")).toBeVisible();
    await expect(page.getByTestId("evolution-events").locator("article")).toHaveCount(3);
    await expect(page.getByTestId("evolution-more")).toBeHidden();
  });

  test("shows experiment performance when the series is reviewed", async ({ page }) => {
    await page.route(/\/v2\/experiments\/.+\/performance\?/, (route) => route.fulfill({
      json: {
        cycle_id: "shadow-base-1", days: 2, start_equity_cents: 9_990_000,
        end_equity_cents: 9_960_000, profit_cents: -40_000, return_bps: -40,
        max_drawdown_cents: 30_000, max_drawdown_day: "2026-11-02",
        drawdown_bps: 30, fees_cents: 500, trades: 1,
        price_set_id: "reviewed-prices-2026-11", digest: "a".repeat(64),
      },
    }));
    await page.route(/\/v2\/evolution\/events/, (route) => route.fulfill({ json: pageOf([]) }));
    await openEvolution(page);
    await page.getByTestId("perf-cycle").fill("shadow-base-1");
    await page.getByTestId("perf-days").fill("2026-10-30,2026-11-02");
    await page.getByTestId("perf-run").click();
    await expect(page.getByTestId("perf-result")).toContainText("收益 ¥-400.00");
    await expect(page.getByTestId("perf-result")).toContainText("回撤 0.30%");
    await expect(page.getByTestId("perf-result")).toContainText("1 笔成交");
  });

  test("experiment performance explains missing coverage", async ({ page }) => {
    await page.route(/\/v2\/experiments\/.+\/performance\?/, (route) => route.fulfill({
      json: { status: "unavailable", reason: "no_reviewed_price_set", days: 2 },
    }));
    await page.route(/\/v2\/evolution\/events/, (route) => route.fulfill({ json: pageOf([]) }));
    await openEvolution(page);
    await page.getByTestId("perf-cycle").fill("shadow-base-1");
    await page.getByTestId("perf-days").fill("2026-10-30,2026-11-02");
    await page.getByTestId("perf-run").click();
    await expect(page.getByTestId("perf-result")).toContainText("缺少审核价格集合");
  });

  test("experiment performance needs an id and days", async ({ page }) => {
    await page.route(/\/v2\/evolution\/events/, (route) => route.fulfill({ json: pageOf([]) }));
    await openEvolution(page);
    await page.getByTestId("perf-run").click();
    await expect(page.getByTestId("perf-error")).toContainText("请填写实验 id");
    await expect(page.getByTestId("perf-result")).toBeHidden();
  });

  test("filters by the linked experiments", async ({ page }) => {
    await page.route(/\/v2\/evolution\/events/, (route) => route.fulfill({ json: pageOf(events) }));
    await openEvolution(page);
    await page.getByTestId("evolution-filter-experiment").fill("shadow-base-1");
    await expect(page.getByTestId("evolution-events").locator("article")).toHaveCount(1);
    await expect(page.getByTestId("evolution-event-evo-1")).toBeVisible();

    await page.getByTestId("evolution-filter-experiment").fill("shadow-candidate-1");
    await expect(page.getByTestId("evolution-events").locator("article")).toHaveCount(1);

    await page.getByTestId("evolution-filter-experiment").fill("no-such-cycle");
    await expect(page.getByTestId("evolution-events").locator("article")).toHaveCount(0);
    await expect(page.getByTestId("evolution-empty")).toContainText("没有匹配的事件");
  });

  test("an empty log explains itself", async ({ page }) => {
    await page.route(/\/v2\/evolution\/events/, (route) => route.fulfill({ json: pageOf([]) }));
    await openEvolution(page);
    await expect(page.getByTestId("evolution-empty")).toBeVisible();
    await expect(page.getByTestId("evolution-events").locator("article")).toHaveCount(0);
  });

  test("the shortcut opens the evolution view", async ({ page }) => {
    await page.route(/\/v2\/evolution\/events/, (route) => route.fulfill({ json: pageOf([]) }));
    await page.goto(`/?cycle_id=${CYCLES.repro}`);
    await page.getByTestId("summary-strategy").waitFor();
    await page.keyboard.press("8");
    await expect(page.getByTestId("panel-evolution")).toBeVisible();
    await expect(page).toHaveURL(/#\/evolution$/);
  });

  test("fits a 390px viewport", async ({ page }) => {
    await page.setViewportSize({ width: 390, height: 844 });
    await page.route(/\/v2\/evolution\/events/, (route) => route.fulfill({ json: pageOf(events) }));
    await openEvolution(page);
    await expect(page.getByTestId("evolution-events").locator("article")).toHaveCount(2);
    const overflow = await page.evaluate(() =>
      document.documentElement.scrollWidth - document.documentElement.clientWidth);
    expect(overflow).toBeLessThanOrEqual(1);
  });
});

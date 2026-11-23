import { expect, test, type Page, type Route as PlaywrightRoute } from "@playwright/test";

/**
 * Race and hand-off coverage for the console.
 *
 * The browser makes no promise about the order two in-flight requests resolve
 * in, so these tests *force* the slow answer to arrive last and then assert
 * what a reader actually sees. Asserting that a field merely exists would pass
 * on the buggy build; every assertion below is about which symbol/day/round
 * ends up on screen.
 *
 * The kernel boots isolated and empty (see start-server.mjs), so each test
 * mocks only the endpoints it needs to create the race.
 */

async function openRoute(page: Page, route: string): Promise<void> {
  await page.goto(`/#/${route}`);
  await expect(page).toHaveURL(new RegExp(`#/${route.replace("/", "\\/")}$`));
  await expect(page.locator(".view:not([hidden])")).toHaveCount(1);
}

async function waitForLive(page: Page): Promise<void> {
  await expect(page.locator("#live-module .live-status")).toHaveAttribute(
    "data-state",
    "ready",
    { timeout: 20_000 },
  );
}

const sleep = (ms: number): Promise<void> => new Promise((resolve) => setTimeout(resolve, ms));

// ---- market: switching the trading day -------------------------------------

test("a slower older screener day never overwrites the day the reader picked", async ({ page }) => {
  await openRoute(page, "market/screener");
  await expect(page.locator("#screener-body tr")).not.toHaveCount(0, { timeout: 20_000 });

  const days = await page
    .locator("#screener-day option")
    .evaluateAll((nodes) => nodes.map((node) => (node as HTMLOptionElement).value));
  expect(days.length).toBeGreaterThan(1);
  const [fastDay, slowDay] = days;

  const bars = (symbol: string, name: string, day: string) => ({
    count: 1,
    bars: [
      {
        symbol,
        day,
        name,
        close_cents: 2_000,
        prev_close_cents: 1_900,
        volume_shares: 12_345,
      },
    ],
  });
  // The deliberately slow answer belongs to the day the reader asked for
  // *first*; the fast one is the day they switched to.
  await page.route(/\/v2\/market\/bars\?day=/, async (route: PlaywrightRoute) => {
    const day = new URL(route.request().url()).searchParams.get("day");
    if (day === slowDay) {
      await sleep(700);
      await route.fulfill({ json: bars("900001.SH", "慢响应", slowDay) });
      return;
    }
    await route.fulfill({ json: bars("900002.SH", "快响应", fastDay) });
  });
  await page.route(/\/v2\/market\/regime\?day=/, async (route: PlaywrightRoute) => {
    const day = new URL(route.request().url()).searchParams.get("day");
    if (day === slowDay) await sleep(700);
    await route.fulfill({
      json: {
        day,
        regime: "choppy",
        label: `环境-${day}`,
        index_points: 3_000,
        fast_average_cents: 100,
        slow_average_cents: 99,
        separation_bps: 10,
        breadth_bps: 5_000,
      },
    });
  });

  await page.locator("#screener-day").selectOption(slowDay);
  await page.locator("#screener-day").selectOption(fastDay);
  // Long enough for the stale (slow) answer to land: if it were allowed to
  // write back, it would repaint the summary and the rows for `slowDay`.
  await sleep(1_200);

  const activeDay = page.locator("#screener-summary .summary-tile").first().locator("strong");
  await expect(activeDay).toHaveText(fastDay);
  await expect(page.locator("#screener-summary")).toContainText("环境-" + fastDay);
  await expect(page.locator("#screener-summary")).not.toContainText("环境-" + slowDay);
  await expect(page.locator("#screener-body tr").first().locator("td").first()).toHaveText(
    "900002.SH",
  );
  await expect(page.locator("#screener-body")).not.toContainText("900001.SH");
});

// ---- market: switching the symbol ------------------------------------------

function detailBars(symbol: string, name: string, closes: number[]) {
  return {
    bars: closes.map((close, index) => ({
      symbol,
      day: `2026-07-0${index + 1}`,
      name,
      close_cents: close,
      prev_close_cents: index === 0 ? close : closes[index - 1],
      volume_shares: 1_000 * (index + 1),
    })),
  };
}

test("a slower older detail lookup never repaints the chart of the newer symbol", async ({ page }) => {
  await page.route("**/v2/market/bars/600000.SH", async (route) => {
    await sleep(700);
    await route.fulfill({ json: detailBars("600000.SH", "浦发银行", [1_111, 1_234]) });
  });
  await page.route("**/v2/market/bars/000001.SZ", (route) =>
    route.fulfill({ json: detailBars("000001.SZ", "平安银行", [2_222, 2_345]) }),
  );

  await openRoute(page, "market/detail");
  await page.locator("#detail-input").fill("600000.SH");
  await page.locator("#detail-load").click();
  // Switch before the first answer arrives: the second symbol is the one the
  // reader is looking at.
  await page.locator("#detail-input").fill("000001.SZ");
  await page.locator("#detail-load").click();

  await expect(page.getByTestId("detail-symbol")).toHaveText("000001.SZ");
  await expect(page.getByTestId("detail-close")).toHaveText("¥23.45");
  await expect(page.getByTestId("detail-days")).toHaveText("2");
  await expect(page.getByTestId("detail-chart").locator("svg")).toHaveCount(1);

  // Give the stale response every chance to land and clobber the panels.
  await sleep(1_000);
  await expect(page.getByTestId("detail-symbol")).toHaveText("000001.SZ");
  await expect(page.getByTestId("detail-close")).toHaveText("¥23.45");
  await expect(page.getByTestId("detail-days")).toHaveText("2");
  await expect(page.getByTestId("detail-chart").locator("svg")).toHaveCount(1);
  await expect(page.getByTestId("detail-empty")).toBeHidden();
});

// ---- live: switching the round while sentiment is in flight ----------------

function liveRound(roundId: string, startedAt: string, tradeCount: number) {
  return {
    round_id: roundId,
    started_at: startedAt,
    finished_at: startedAt,
    quote_count: 2,
    trade_count: tradeCount,
    ai_ok: true,
    ai_error: null,
    equity_cents: 10_000_000,
    regime: "neutral",
    note: "",
    regime_label: "中性",
    ai_label: "正常",
    note_label: null,
  };
}

function liveDecision(roundId: string, symbol: string, name: string) {
  return {
    round_id: roundId,
    slot: "steady",
    symbol,
    name,
    score: 50,
    threshold: 40,
    reached_threshold: true,
    side: "buy",
    blocked_reason: null,
    ai_bias: 0.2,
    ai_confidence: 70,
    ai_applied: true,
    factors_json: {},
    note: "模拟信号",
    created_at: "2026-11-16T14:00:00+08:00",
    side_label: "买入",
    blocked_label: null,
    slot_label: "稳健",
  };
}

test("the sentiment response cannot restore a round the reader already left", async ({ page }) => {
  const newer = "2026-11-16-140000";
  const older = "2026-11-16-130000";
  let sentimentCalls = 0;
  let releaseSentiment: (() => void) | null = null;

  await page.route("**/v2/live/account", (route) =>
    route.fulfill({
      json: {
        id: "paper",
        initial_cash_cents: 10_000_000,
        cash_cents: 10_000_000,
        created_at: "2026-11-16T09:00:00+08:00",
        as_of_round_id: newer,
        equity_cents: 10_000_000,
        profit_cents: 0,
        return_bps: 0,
        slots: [],
        positions: [],
      },
    }),
  );
  await page.route("**/v2/live/rounds", (route) =>
    route.fulfill({
      json: [
        liveRound(newer, "2026-11-16T14:00:00+08:00", 2),
        liveRound(older, "2026-11-16T13:00:00+08:00", 1),
      ],
    }),
  );
  await page.route("**/v2/live/trades", (route) => route.fulfill({ json: [] }));
  await page.route("**/v2/live/equity-curve", (route) => route.fulfill({ json: [] }));
  await page.route(/\/v2\/live\/decisions\?/, (route) => {
    const roundId = new URL(route.request().url()).searchParams.get("round_id");
    const decisions =
      roundId === older
        ? [liveDecision(older, "000001.SZ", "平安银行")]
        : [liveDecision(newer, "600000.SH", "浦发银行"), liveDecision(newer, "600036.SH", "招商银行")];
    return route.fulfill({
      json: {
        round_id: roundId,
        signals_only: true,
        total: decisions.length,
        returned: decisions.length,
        decisions,
      },
    });
  });
  // First sentiment read answers at once (initial page load). Every later one
  // is held until the test releases it, which opens the exact window the bug
  // lived in: the reader changes round while that request is outstanding.
  await page.route("**/v2/live/sentiment", async (route) => {
    sentimentCalls += 1;
    if (sentimentCalls > 1) {
      await new Promise<void>((resolve) => {
        releaseSentiment = resolve;
      });
    }
    await route.fulfill({
      json: {
        round_id: newer,
        quote_time: "2026-11-16T14:00:00+08:00",
        total_quotes: 2,
        up_count: 2,
        down_count: 0,
        flat_count: 0,
        up_ratio_bps: 10_000,
        strong_count: 0,
        weak_count: 0,
        limit_up_count: 0,
        limit_down_count: 0,
        bull_trap_count: 0,
        bull_trap_rate_bps: 0,
        score: 70,
        label: "偏热",
        posture: "情绪偏热",
        recommended_max_exposure_bps: 6_000,
      },
    });
  });

  await openRoute(page, "live/signals");
  await waitForLive(page);
  await expect(page.locator("#live-round")).toHaveValue(newer);
  await expect(page.locator("#live-module table tbody")).toContainText("600000.SH");

  await page.locator("#live-refresh").click();
  await expect.poll(() => sentimentCalls, { timeout: 15_000 }).toBe(2);
  await page.locator("#live-round").selectOption(older);
  await expect(page.locator("#live-module table tbody")).toContainText("000001.SZ");

  releaseSentiment?.();
  // The stale batch finishes last; it must not put round `newer` (or its two
  // signals) back on screen.
  await sleep(800);
  await expect(page.locator("#live-round")).toHaveValue(older);
  await expect(page.locator("#live-module table tbody")).toContainText("000001.SZ");
  await expect(page.locator("#live-module table tbody")).not.toContainText("600000.SH");
  await expect(page.getByTestId("live-decisions-summary")).toContainText("本轮共 1 条决策");
});

// ---- live: cross-page hand-off and a loading marker that must clear -------

function symbolArchive(symbol: string, name: string) {
  return {
    symbol,
    name,
    trade_count: 1,
    buy_quantity: 100,
    sell_quantity: 0,
    realized_fee_cents: 500,
    decision_count: 1,
    events: [
      {
        round_id: "2026-11-16-140000",
        started_at: "2026-11-16T14:00:00+08:00",
        kind: "trade",
        slot: "steady",
        slot_label: "稳健",
        side: "buy",
        side_label: "买入",
        quantity: 100,
        price_cents: 1_234,
        score: 50,
        blocked_label: "",
        note: "",
      },
    ],
  };
}

async function mockSymbolArchive(
  page: Page,
  delays: Record<string, number>,
): Promise<{ symbols: string[] }> {
  const seen = { symbols: [] as string[] };
  await page.route(/\/v2\/live\/symbol\?/, async (route) => {
    const symbol = new URL(route.request().url()).searchParams.get("symbol") ?? "";
    seen.symbols.push(symbol);
    const delay = delays[symbol] ?? 0;
    if (delay > 0) await sleep(delay);
    await route.fulfill({
      json: symbolArchive(symbol, symbol === "600000.SH" ? "浦发银行" : "平安银行"),
    });
  });
  return seen;
}

test("查看盘中档案 loads the requested symbol even while that view is still busy", async ({ page }) => {
  // The first hand-off keeps the live view's "正在读取…" state alive long
  // enough that its load button is disabled when the second hand-off happens.
  await mockSymbolArchive(page, { "600000.SH": 1_500, "000001.SZ": 50 });
  await page.route("**/v2/market/bars/600000.SH", (route) =>
    route.fulfill({ json: detailBars("600000.SH", "浦发银行", [1_111, 1_234]) }),
  );
  await page.route("**/v2/market/bars/000001.SZ", (route) =>
    route.fulfill({ json: detailBars("000001.SZ", "平安银行", [2_222, 2_345]) }),
  );

  await openRoute(page, "market/detail");
  await page.locator("#detail-input").fill("600000.SH");
  await page.locator("#detail-load").click();
  await expect(page.getByTestId("detail-close")).toHaveText("¥12.34");
  await page.locator("#detail-to-live").click();
  await expect(page).toHaveURL(/#\/live\/symbol$/);
  await expect(page.locator("#live-symbol-input")).toHaveValue("600000.SH");

  // Back to the detail page while the archive above is still loading, ask for a
  // different stock, then hand off again: the second symbol is what must win.
  await page.goBack();
  await expect(page).toHaveURL(/#\/market\/detail$/);
  await page.locator("#detail-input").fill("000001.SZ");
  await page.locator("#detail-load").click();
  await expect(page.getByTestId("detail-close")).toHaveText("¥23.45");
  await page.locator("#detail-to-live").click();

  await expect(page).toHaveURL(/#\/live\/symbol$/);
  await expect(page.locator("#live-symbol-input")).toHaveValue("000001.SZ");
  await expect(page.locator("#live-module")).toContainText("平安银行 000001.SZ");
  // Let the abandoned 600000.SH read land: it must not replace this archive.
  await sleep(1_800);
  await expect(page.locator("#live-module")).toContainText("平安银行 000001.SZ");
  await expect(page.locator("#live-module")).not.toContainText("浦发银行 600000.SH");
});

test("leaving the archive view mid-request does not strand the loading state", async ({ page }) => {
  await mockSymbolArchive(page, { "600000.SH": 800 });
  await openRoute(page, "live/symbol");
  await waitForLive(page);
  await page.locator("#live-symbol-input").fill("600000.SH");
  await page.locator("#live-symbol-load").click();
  await expect(page.locator("#live-symbol-load")).toBeDisabled();
  await expect(page.locator("#live-module .empty")).toContainText("正在读取");

  // Leave the live module entirely — this is what makes the bug reachable:
  // the module deactivates, and an in-flight archive read used to keep its
  // loading marker set forever because it returned before clearing it.
  await page.locator('.module-tab[data-module="market"]').click();
  await expect(page).toHaveURL(/#\/market\/screener$/);
  await sleep(1_200);
  await page.locator('.module-tab[data-module="live"]').click();
  await page.locator('.view-tab[data-view="symbol"]').click();
  await expect(page).toHaveURL(/#\/live\/symbol$/);

  // The marker was released, so the view is usable again: no permanent
  // "正在读取…" placeholder, a button the reader can press, and the result the
  // abandoned request already fetched is on screen instead of being thrown away.
  await expect(page.locator("#live-symbol-load")).toBeEnabled();
  await expect(page.locator("#live-module")).toContainText("浦发银行 600000.SH");
  await expect(page.locator("#live-module")).not.toContainText("正在读取");
});

import { AxeBuilder } from "@axe-core/playwright";
import { expect, test, type Page } from "@playwright/test";

/**
 * E2E coverage for the current hash router: live / market / research.
 *
 * start-server.mjs boots an isolated, empty kernel. The live account API
 * therefore returns 404 by design; tests assert explicit empty states, never
 * account numbers. Market bars are seeded by the test fixture so the screener
 * row -> detail navigation exercises real data.
 */

const ROUTES = {
  "live/dashboard": "实时总览",
  "live/signals": "决策信号",
  "live/trades": "实时成交",
  "live/rounds": "调度记录",
  "live/ai": "AI 观测",
  "live/evolution": "进化提案",
  "live/risk": "风控拦截",
  "live/symbol": "个股档案",
  "market/screener": "全市场",
  "market/detail": "个股详情",
  "research/candidates": "候选评审",
  "research/coverage": "数据底座",
  "research/evidence": "策略证据",
  "settings/ai": "AI 模型",
  "settings/runtime": "长期运行",
} as const;

type RoutePath = keyof typeof ROUTES;

async function openRoute(page: Page, route: RoutePath): Promise<void> {
  await page.goto(`/#/${route}`);
  await expect(page).toHaveURL(new RegExp(`#/${route.replace("/", "\\/")}$`));
  await expect(page.locator(".view-tab.active")).toHaveText(ROUTES[route]);
  await expect(page.locator(".view:not([hidden])")).toHaveCount(1);
}

async function waitForLive(page: Page): Promise<void> {
  await expect(page.locator("#live-module .live-status")).toHaveAttribute(
    "data-state",
    "ready",
    { timeout: 20_000 },
  );
}

async function waitForResearch(page: Page): Promise<void> {
  const table = page.getByTestId("research-table");
  const empty = page.getByTestId("research-empty");
  await expect.poll(async () => {
    const tableVisible = await table.isVisible();
    const emptyVisible = await empty.isVisible();
    return tableVisible !== emptyVisible; // exactly one state, never a blank panel
  }, { timeout: 20_000 }).toBe(true);
}

test.describe("current console routes", () => {
  test("default route is live/dashboard and shows the empty live account", async ({ page }) => {
    const apiPaths: string[] = [];
    page.on("request", (request) => {
      const path = new URL(request.url()).pathname;
      if (path.startsWith("/v2/")) apiPaths.push(path);
    });

    await page.goto("/");
    await expect(page).toHaveURL(/#\/live\/dashboard$/);
    await expect(page.locator(".view-tab.active")).toHaveText("实时总览");
    await expect(page.locator("#live-module")).toBeVisible();
    await waitForLive(page);
    await expect(page.getByText("现在没有持股，全部是现金。")).toBeVisible();
    await expect(page.getByText("还没有开始")).toBeVisible();
    // Startup must not pre-fetch the retired daily account endpoint.
    expect(apiPaths).not.toContain("/v2/account");
    expect(apiPaths.some((path) => path.startsWith("/v2/live/"))).toBe(true);
  });

  test("all four live views are reachable by URL with the correct active tab", async ({ page }) => {
    const emptyStates: Record<string, RegExp> = {
      "live/dashboard": /现在没有持股|还没有开始/,
      "live/signals": /还没有实时轮次/,
      "live/trades": /还没有实时成交/,
      "live/rounds": /还没有实时轮次/,
    };
    for (const route of ["live/dashboard", "live/signals", "live/trades", "live/rounds"] as const) {
      await openRoute(page, route);
      await waitForLive(page);
      await expect(page.locator(".module-tab.active")).toHaveAttribute("data-module", "live");
      await expect(page.locator("#live-module")).toContainText(emptyStates[route]);
    }
  });

  test("all eight live views are reachable by URL with the correct active tab", async ({ page }) => {
    const routes: RoutePath[] = [
      "live/dashboard", "live/signals", "live/trades", "live/rounds",
      "live/ai", "live/evolution", "live/risk", "live/symbol",
    ];
    for (const route of routes) {
      await openRoute(page, route);
      await waitForLive(page);
      await expect(page.locator(".module-tab.active")).toHaveAttribute("data-module", "live");
      await expect(page.locator("#live-module")).toBeVisible();
    }
  });

  test("ai, evolution and risk show explicit empty states on an empty kernel", async ({ page }) => {
    for (const [route, message] of [
      ["live/ai", "还没有 AI 观测。等第一轮跑完，这里就会出现模型的原始建议。"],
      ["live/evolution", "还没有进化提案。收盘后才会生成供人工评审的提案。"],
      ["live/risk", "最近 40 轮还没有风控拦截。"],
    ] as const) {
      await openRoute(page, route);
      await waitForLive(page);
      await expect(page.locator("#live-module .empty")).toContainText(message, { timeout: 20_000 });
    }
  });

  test("the symbol archive has a search form and an explicit empty state", async ({ page }) => {
    await openRoute(page, "live/symbol");
    await waitForLive(page);
    const input = page.locator("#live-symbol-input");
    const load = page.locator("#live-symbol-load");
    await expect(input).toBeVisible();
    await expect(load).toBeVisible();
    await expect(input).toHaveAttribute("placeholder", "600000.SH");
    await expect(page.locator("#live-module .empty")).toContainText(
      /输入股票代码后点「加载」|还没有盘中档案/,
      { timeout: 20_000 },
    );
    await input.fill("600000.SH");
    await load.click();
    await expect(page.locator("#live-module .empty")).toContainText("600000.SH", { timeout: 20_000 });
  });

  test("decision signals show an explicit empty state and a working show-all switch", async ({ page }) => {
    const decisionUrls: string[] = [];
    page.on("request", (request) => {
      const url = new URL(request.url());
      if (url.pathname === "/v2/live/decisions") decisionUrls.push(url.search);
    });
    await openRoute(page, "live/signals");
    await waitForLive(page);
    await expect(page.locator("#live-module .live-status")).not.toHaveAttribute("data-state", "error");
    await expect(page.locator("#live-module .empty")).toContainText(
      /还没有实时轮次|本轮没有决策信号|本轮决策信号读取失败/,
      { timeout: 20_000 },
    );
    const showAll = page.getByTestId("live-show-all");
    await expect(showAll).toBeVisible();
    await expect(showAll).not.toBeChecked();
    await expect(page.getByTestId("live-decisions-mode-label")).toContainText("只看有信号的");
    await showAll.check();
    await expect(showAll).toBeChecked();
    await expect(page.getByTestId("live-decisions-mode-label")).toContainText("显示全部", { timeout: 20_000 });
    await expect(page.locator("#live-module .empty")).toContainText(
      /还没有实时轮次|本轮还没有决策记录|本轮决策信号读取失败/,
      { timeout: 20_000 },
    );
    // 空库没有轮次：desired 为 null，现状既不会拉 decisions，也不该因 404 报错。
    expect(decisionUrls).toEqual([]);
  });

  for (const mode of ["object", "legacy array"] as const) test(`decision signals show rows from ${mode}`, async ({ page }) => {
    const roundId = "2026-11-16-065813";
    const signal: Record<string, unknown> = {
      round_id: roundId, slot: "steady", symbol: "600000.SH", score: 50,
      threshold: 40, reached_threshold: true, side: "buy", blocked_reason: null,
      ai_bias: 0.2, ai_confidence: 70, ai_applied: true, factors_json: {},
      note: "模拟信号", created_at: "2026-11-16T14:00:00+08:00", name: "浦发银行",
      side_label: "买入", blocked_label: null, slot_label: "稳健",
    };
    const hold = {
      ...signal, slot: "aggressive", symbol: "000001.SZ", side: "hold",
      score: 0, reached_threshold: false, side_label: "观望", name: "平安银行",
    };
    const requests: URL[] = [];
    await page.route("**/v2/live/*", async (route) => {
      const url = new URL(route.request().url());
      requests.push(url);
      const path = url.pathname;
      let body: unknown;
      if (path === "/v2/live/account") {
        await route.fulfill({ status: 404, contentType: "application/json", body: '{"error":"no live account"}' });
        return;
      } else if (path === "/v2/live/rounds") {
        body = [{ round_id: roundId, started_at: "2026-11-16T14:00:00+08:00", finished_at: "2026-11-16T14:00:01+08:00", quote_count: 2, trade_count: 1, ai_ok: true, ai_error: null, equity_cents: 10000000, regime: "neutral", note: "", regime_label: "中性", ai_label: "正常", note_label: null }];
      } else if (path === "/v2/live/decisions") {
        const all = url.searchParams.get("signals_only") !== "false";
        body = mode === "legacy array" ? [signal, hold] : {
          round_id: roundId,
          signals_only: all,
          total: 2,
          returned: all ? 1 : 2,
          decisions: all ? [signal] : [signal, hold],
        };
      } else if (path === "/v2/live/trades" || path === "/v2/live/equity-curve") {
        body = [];
      } else {
        await route.continue();
        return;
      }
      await route.fulfill({ status: 200, contentType: "application/json", body: JSON.stringify(body) });
    });

    await openRoute(page, "live/signals");
    await expect(page.getByTestId("live-decisions-mode-label")).toContainText("只看有信号的（1 / 2）", { timeout: 20_000 });
    await expect(page.getByTestId("live-decisions-summary")).toContainText("本轮共 2 条决策，其中 1 条有信号");
    await expect(page.locator("#live-module table tbody tr")).toHaveCount(1);
    await expect(page.locator("#live-module table tbody tr").first()).toContainText("600000.SH");

    await page.getByTestId("live-show-all").check();
    await expect(page.getByTestId("live-decisions-mode-label")).toContainText("显示全部（2 / 2）", { timeout: 20_000 });
    await expect(page.getByTestId("live-decisions-summary")).toContainText("本轮共 2 条决策（显示全部）");
    await expect(page.locator("#live-module table tbody tr")).toHaveCount(2);
    const modes = requests
      .filter((url) => url.pathname === "/v2/live/decisions")
      .map((url) => url.searchParams.get("signals_only"));
    expect(modes).toContain("true");
    expect(modes).toContain("false");
  });

  test("AI observations explain signal changes and a real fill's comparison", async ({ page }) => {
    const observation = {
      round_id: "2026-11-20-013609", started_at: "2026-11-20T09:36:09+08:00",
      model: "test-model", confidence: 80, accepted: true, clamped: false,
      reason: "测试记录", raw_advice: "{}", clamped_fields: [],
    };
    await page.route("**/v2/live/ai", (route) => route.fulfill({ json: [
      { ...observation, signal_baseline: { compared: 10, side_changed: 3,
        fills: [{ slot: "steady", symbol: "003007.SZ", side: "buy", without_ai: "hold" }] } },
      { ...observation, round_id: "2026-11-19-013609", model: "historical" },
    ] }));
    await openRoute(page, "live/ai");
    const comparison = page.getByText("方向变化 3 / 10", { exact: true });
    await expect(comparison).toBeVisible();
    await comparison.click();
    await expect(page.getByText(/003007.SZ.*无 AI 信号 观望.*实际成交 买入/)).toBeVisible();
    await expect(page.getByRole("row", { name: /historical/ }).getByText("—", { exact: true })).toBeVisible();
  });

  test("the live report copies the intraday ledger, not the retired daily ledger", async ({ page }) => {
    const apiPaths: string[] = [];
    page.on("request", (request) => {
      apiPaths.push(new URL(request.url()).pathname);
    });
    await openRoute(page, "live/dashboard");
    await waitForLive(page);
    let copied = "";
    await page.evaluate(() => {
      Object.defineProperty(navigator, "clipboard", {
        configurable: true,
        value: {
          writeText: (value: string) => {
            (window as typeof window & { __copiedReport?: string }).__copiedReport = value;
            return Promise.resolve();
          },
        },
      });
    });
    await page.getByTestId("report-copy").click();
    await expect(page.getByTestId("report-copy")).toHaveText("已复制");
    copied = await page.evaluate(
      () => (window as typeof window & { __copiedReport?: string }).__copiedReport ?? "",
    );
    expect(copied).toContain("盘中模拟盘复盘");
    expect(copied).toContain("今日成交笔数");
    expect(apiPaths).not.toContain("/v2/account/daily-report");
  });

  test("copying the report from market views still uses the intraday ledger without 404", async ({ page }) => {
    const apiPaths: string[] = [];
    const failedUrls: string[] = [];
    page.on("request", (request) => {
      apiPaths.push(new URL(request.url()).pathname);
    });
    page.on("response", (response) => {
      if (response.status() >= 400 && new URL(response.url()).pathname === "/v2/account/daily-report") {
        failedUrls.push(`${response.status()} ${response.url()}`);
      }
    });
    await openRoute(page, "market/screener");
    await expect(page.locator("#screener-body tr")).not.toHaveCount(0, { timeout: 20_000 });
    await page.evaluate(() => {
      Object.defineProperty(navigator, "clipboard", {
        configurable: true,
        value: {
          writeText: (value: string) => {
            (window as typeof window & { __copiedMarketReport?: string }).__copiedMarketReport = value;
            return Promise.resolve();
          },
        },
      });
    });
    await page.getByTestId("report-copy").click();
    await expect(page.getByTestId("report-copy")).toHaveText("已复制");
    const copied = await page.evaluate(
      () => (window as typeof window & { __copiedMarketReport?: string }).__copiedMarketReport ?? "",
    );
    expect(copied).toContain("盘中模拟盘复盘");
    expect(apiPaths).not.toContain("/v2/account/daily-report");
    expect(failedUrls).toEqual([]);
  });

  test("a screener row opens the symbol detail page with a drawn series", async ({ page }) => {
    await openRoute(page, "market/screener");
    // The fixture feeds two symbols; a row must be clickable, not decorative.
    // The list is populated after its fetch resolves, so wait for the rows.
    const rows = page.locator("#screener-body tr");
    await expect(rows).not.toHaveCount(0, { timeout: 20_000 });
    const firstRow = rows.first();
    await expect(firstRow).toBeVisible();
    const symbol = (await firstRow.getAttribute("data-symbol")) ?? "";
    expect(symbol).not.toBe("");
    await firstRow.click();

    await expect(page).toHaveURL(/#\/market\/detail$/);
    await expect(page.locator(".view-tab.active")).toHaveText("个股详情");
    await expect(page.getByTestId("detail-symbol")).toHaveText(symbol);
    await expect(page.getByTestId("detail-close")).not.toHaveText("—");
    await expect(page.getByTestId("detail-days")).not.toHaveText("0");
    await expect(page.getByTestId("detail-chart").locator("svg")).toHaveCount(1);
    const path = await page.getByTestId("detail-chart").locator("path").getAttribute("d");
    expect((path ?? "").length).toBeGreaterThan(10);
  });

  test("an unknown symbol says so instead of rendering an empty chart", async ({ page }) => {
    await openRoute(page, "market/detail");
    await page.locator("#detail-input").fill("999999.SH");
    await page.locator("#detail-load").click();
    await expect(page.getByTestId("detail-empty")).toContainText("999999.SH", { timeout: 20_000 });
    await expect(page.getByTestId("detail-empty")).toContainText("没有");
    await expect(page.getByTestId("detail-chart").locator("svg")).toHaveCount(0);
  });

  test("research candidates shows a table or an explicit empty state", async ({ page }) => {
    await openRoute(page, "research/candidates");
    await waitForResearch(page);
    // The nav entry must exist, otherwise the view is unreachable by a user.
    await expect(page.locator('[data-module="research"]')).toBeVisible();
    await expect(page.locator('.module-tab.active[data-module="research"]')).toBeVisible();
  });

  test("navigation tabs switch module/view and browser back returns", async ({ page }) => {
    await openRoute(page, "live/dashboard");
    await waitForLive(page);

    await page.locator('.module-tab[data-module="market"]').click();
    await expect(page).toHaveURL(/#\/market\/screener$/);
    await expect(page.locator(".view-tab.active")).toHaveText("全市场");
    await expect(page.locator("#view-market-screener")).toBeVisible();

    await page.locator('.view-tab[data-view="detail"]').click();
    await expect(page).toHaveURL(/#\/market\/detail$/);
    await expect(page.locator(".view-tab.active")).toHaveText("个股详情");
    await expect(page.locator("#view-market-detail")).toBeVisible();

    await page.goBack();
    await expect(page).toHaveURL(/#\/market\/screener$/);
    await expect(page.locator(".view-tab.active")).toHaveText("全市场");
    await expect(page.locator("#view-market-screener")).toBeVisible();
  });

  test("keeps the layout within a 320px viewport", async ({ page }) => {
    await page.setViewportSize({ width: 320, height: 720 });
    const routes: RoutePath[] = [
      "live/dashboard",
      "market/screener",
      "market/detail",
      "research/candidates",
    ];
    for (const route of routes) {
      await openRoute(page, route);
      if (route.startsWith("live/")) await waitForLive(page);
      if (route === "market/screener") {
        await expect(page.locator("#screener-body tr")).not.toHaveCount(0, { timeout: 20_000 });
      }
      if (route === "research/candidates") await waitForResearch(page);
      const overflow = await page.evaluate(
        () => document.documentElement.scrollWidth - document.documentElement.clientWidth,
      );
      expect(overflow, `horizontal overflow on ${route}`).toBeLessThanOrEqual(1);
    }
  });

  test("new live views stay within a 320px viewport and have no serious a11y violations", async ({ page }) => {
    await page.setViewportSize({ width: 320, height: 720 });
    for (const route of ["live/ai", "live/evolution", "live/risk", "live/symbol"] as const) {
      await openRoute(page, route);
      await waitForLive(page);
      await expect(page.locator("#live-module .empty")).not.toHaveCount(0, { timeout: 20_000 });
      const overflow = await page.evaluate(
        () => document.documentElement.scrollWidth - document.documentElement.clientWidth,
      );
      expect(overflow, `horizontal overflow on ${route}`).toBeLessThanOrEqual(1);
      const results = await new AxeBuilder({ page }).analyze();
      const serious = results.violations.filter((item) =>
        ["serious", "critical"].includes(item.impact ?? ""),
      );
      expect(serious, `a11y violations on ${route}: ${JSON.stringify(serious.map((v) => v.id))}`).toEqual(
        [],
      );
    }
  });

  test("opens without a login gate and sends no credentials", async ({ page }) => {
    await page.addInitScript(() => {
      sessionStorage.setItem("astock-v2-token", "ignored-legacy-value");
    });
    const headers: Record<string, string>[] = [];
    page.on("request", (request) => {
      if (new URL(request.url()).pathname.startsWith("/v2/")) headers.push(request.headers());
    });

    await page.goto("/");
    await expect(page).toHaveURL(/#\/live\/dashboard$/);
    await expect(page.locator("#live-module")).toBeVisible();
    await waitForLive(page);
    await expect(page.locator('input[type="password"]')).toHaveCount(0);
    await expect(page.getByTestId("create-panel")).toHaveCount(0);
    const bodyText = await page.locator("body").innerText();
    expect(bodyText).not.toContain("登录");
    expect(bodyText).not.toContain("令牌");
    expect(headers.length).toBeGreaterThan(0);
    for (const value of headers) expect(value).not.toHaveProperty("authorization");
  });

  test("has no serious accessibility violations", async ({ page }) => {
    const routes: RoutePath[] = [
      "live/dashboard",
      "market/screener",
      "market/detail",
      "research/candidates",
    ];
    for (const route of routes) {
      await openRoute(page, route);
      if (route.startsWith("live/")) await waitForLive(page);
      if (route === "market/screener") {
        await expect(page.locator("#screener-body tr")).not.toHaveCount(0, { timeout: 20_000 });
      }
      if (route === "research/candidates") await waitForResearch(page);
      const results = await new AxeBuilder({ page }).analyze();
      const serious = results.violations.filter((item) =>
        ["serious", "critical"].includes(item.impact ?? ""),
      );
      expect(serious, `a11y violations on ${route}: ${JSON.stringify(serious.map((v) => v.id))}`).toEqual(
        [],
      );
    }
  });

  test("upgraded market and research views work end-to-end without overflow or a11y issues", async ({ page }) => {
    await openRoute(page, "market/screener");
    await expect(page.locator("#screener-body tr")).not.toHaveCount(0, { timeout: 20_000 });
    await expect(page.locator("#screener-summary .summary-tile")).toHaveCount(5);
    await page.locator("#screener-chips button[data-filter='up']").click();
    await page.locator("#screener-chips button[data-filter='all']").click();
    await page.locator(".sort-btn[data-sort='symbol']").click();

    await page.locator("#screener-body tr").first().click();
    await expect(page).toHaveURL(/#\/market\/detail$/);
    await expect(page.locator("#detail-volume svg")).toHaveCount(1);
    await expect(page.locator("#detail-history-body tr")).not.toHaveCount(0);
    await page.locator("#detail-ranges button[data-range='20']").click();
    await expect(page.getByTestId("detail-days")).toHaveText("20");
    await page.locator("#detail-to-live").click();
    await expect(page).toHaveURL(/#\/live\/symbol$/);

    for (const sub of ["research/coverage", "research/evidence", "settings/ai", "settings/runtime"] as const) {
      await openRoute(page, sub as RoutePath);
      const results = await new AxeBuilder({ page }).analyze();
      const serious = results.violations.filter((item) =>
        ["serious", "critical"].includes(item.impact ?? ""),
      );
      expect(serious, `a11y violations on ${sub}`).toEqual([]);
      await page.setViewportSize({ width: 320, height: 720 });
      const overflow = await page.evaluate(
        () => document.documentElement.scrollWidth - document.documentElement.clientWidth,
      );
      expect(overflow, `${sub} overflow on 320px`).toBeLessThanOrEqual(1);
      await page.setViewportSize({ width: 1280, height: 800 });
    }

    // Verify research evaluation runner generates real candidates & curve comparison
    await openRoute(page, "research/candidates");
    await page.locator("#research-run").click();
    await expect(page.getByTestId("research-rows").locator("tr")).toHaveCount(3, { timeout: 15_000 });
    await expect(page.locator("#research-curve svg path")).toHaveCount(3);

    // Verify settings save persists AI depth & runtime config
    await openRoute(page, "settings/ai");
    await page.locator("#settings-ai-save").click();
    await expect(page.locator("#settings-ai-feedback")).toContainText("已保存");
  });
});

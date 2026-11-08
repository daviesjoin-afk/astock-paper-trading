import { test, expect } from "@playwright/test";
import { CYCLES, SESSION_ACCOUNT, PAPER_SESSION, ensureFixtures } from "./fixtures";

test.beforeEach(async ({ request }) => {
  await ensureFixtures(request);
});

/** Create a fresh session per test so shared fixtures stay deterministic. */
const createSession = async (
  request: import("@playwright/test").APIRequestContext,
  sessionId: string,
  signalCount = 2,
) => {
  const pack = "cn-offline-fixture-pack-1";
  const signals = [
    {
      slot: "A", signal_id: "smoke-buy", decision_at: "2026-10-30T14:00:00+08:00",
      signal: { symbol: "600000.SH", side: "buy" },
      quote_ref: { market_data_pack_id: pack, source: "offline-fixture", quote_id: "offline-quote-20261030-1359" },
    },
    {
      slot: "A", signal_id: "smoke-sell", decision_at: "2026-11-02T10:00:00+08:00",
      signal: { symbol: "600000.SH", side: "sell" },
      quote_ref: { market_data_pack_id: pack, source: "offline-fixture", quote_id: "offline-quote-20261102-0959" },
    },
  ].slice(0, signalCount);
  const response = await request.post("/v2/paper-sessions", {
    data: {
      schema_version: 1,
      session_id: sessionId,
      account_id: SESSION_ACCOUNT,
      start_at: "2026-10-30T00:00:00+08:00",
      signals,
    },
  });
  expect(response.ok(), `${response.status()} ${await response.text()}`).toBeTruthy();
};

const openSessionAccount = async (page: import("@playwright/test").Page) => {
  await page.goto(`/?cycle_id=${CYCLES.repro}&account=${SESSION_ACCOUNT}#/account`);
  await expect(page.getByTestId("account-select")).toHaveValue(SESSION_ACCOUNT);
  await expect(page.getByTestId("account-sessions")).toBeVisible();
};

test.describe("paper session control", () => {
  test("start, pause, resume and run the clock to completion", async ({ page, request }) => {
    const id = "ui-session-flow";
    await createSession(request, id);
    await openSessionAccount(page);
    const card = page.getByTestId(`session-card-${id}`);
    await expect(card).toContainText("未开始");
    await expect(card).toContainText("0/2");

    await card.getByRole("button", { name: "启动" }).click();
    await expect(card).toContainText("运行中");
    await expect(page.getByTestId("live-status")).toContainText("已执行 start");

    await card.getByRole("button", { name: "暂停" }).click();
    await expect(card).toContainText("已暂停");
    await expect(card).toContainText("0/2");

    await card.getByRole("button", { name: "继续" }).click();
    await expect(card).toContainText("运行中");

    await card.getByRole("button", { name: "运行到结束" }).click();
    await expect(card).toContainText("已完成", { timeout: 15_000 });
    await expect(card).toContainText("2/2");
    await expect(card).toContainText("11-02 10:00");
  });

  test("a paused session does not trade when run is pressed", async ({ page, request }) => {
    const id = "ui-session-paused";
    await createSession(request, id);
    await openSessionAccount(page);
    const card = page.getByTestId(`session-card-${id}`);

    await card.getByRole("button", { name: "启动" }).click();
    await expect(card).toContainText("运行中");
    await card.getByRole("button", { name: "暂停" }).click();
    await expect(card).toContainText("已暂停");

    await card.getByRole("button", { name: "运行到结束" }).click();
    await expect(card).toContainText("已暂停");
    await expect(card).toContainText("0/2");
  });

  test("session controls only post to session endpoints", async ({ page, request }) => {
    const id = "ui-session-readonly";
    await createSession(request, id);
    const writes: string[] = [];
    page.on("request", (req) => {
      if (["POST", "PUT", "PATCH", "DELETE"].includes(req.method()) && req.url().includes("/v2/")) {
        writes.push(`${req.method()} ${req.url()}`);
      }
    });
    await openSessionAccount(page);
    const card = page.getByTestId(`session-card-${id}`);
    await card.getByRole("button", { name: "启动" }).click();
    await expect(card).toContainText("运行中");
    expect(writes.length).toBeGreaterThan(0);
    for (const write of writes) expect(write).toContain("/v2/paper-sessions/");
  });

  test("390px keeps session cards inside the viewport", async ({ page, request }) => {
    const id = "ui-session-mobile";
    await createSession(request, id);
    await page.setViewportSize({ width: 390, height: 844 });
    await openSessionAccount(page);
    await expect(page.getByTestId(`session-card-${id}`)).toBeVisible();
    const overflow = await page.evaluate(() =>
      document.documentElement.scrollWidth - document.documentElement.clientWidth);
    expect(overflow).toBeLessThanOrEqual(1);
  });

  test("the fixture session exists for the session account", async ({ page }) => {
    await openSessionAccount(page);
    await expect(page.getByTestId(`session-card-${PAPER_SESSION}`)).toBeVisible();
  });
});

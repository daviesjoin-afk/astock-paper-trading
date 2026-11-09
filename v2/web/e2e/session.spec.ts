import { test, expect } from "@playwright/test";
import { CYCLES, createAccount, ensureFixtures } from "./fixtures";

test.beforeEach(async ({ request }) => {
  await ensureFixtures(request);
});

/** Create a fresh session per test so shared fixtures stay deterministic. */
const createSession = async (
  request: import("@playwright/test").APIRequestContext,
  accountId: string,
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
      account_id: accountId,
      start_at: "2026-10-30T00:00:00+08:00",
      signals,
    },
  });
  expect(response.ok(), `${response.status()} ${await response.text()}`).toBeTruthy();
};

const openAccount = async (page: import("@playwright/test").Page, accountId: string) => {
  await page.goto(`/?cycle_id=${CYCLES.repro}&account=${accountId}#/account`);
  await expect(page.getByTestId("account-select")).toHaveValue(accountId);
  await expect(page.getByTestId("account-sessions")).toBeVisible();
};

test.describe("paper session control", () => {
  test("start, pause, resume and run the clock to completion", async ({ page, request }) => {
    await createAccount(request, "ui-acc-flow");
    await createSession(request, "ui-acc-flow", "ui-session-flow");
    await openAccount(page, "ui-acc-flow");
    const card = page.getByTestId("session-card-ui-session-flow");
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
    await createAccount(request, "ui-acc-paused");
    await createSession(request, "ui-acc-paused", "ui-session-paused");
    await openAccount(page, "ui-acc-paused");
    const card = page.getByTestId("session-card-ui-session-paused");

    await card.getByRole("button", { name: "启动" }).click();
    await expect(card).toContainText("运行中");
    await card.getByRole("button", { name: "暂停" }).click();
    await expect(card).toContainText("已暂停");

    await card.getByRole("button", { name: "运行到结束" }).click();
    await expect(card).toContainText("已暂停");
    await expect(card).toContainText("0/2");
  });

  test("session controls only post to session endpoints", async ({ page, request }) => {
    await createAccount(request, "ui-acc-readonly");
    await createSession(request, "ui-acc-readonly", "ui-session-readonly");
    const writes: string[] = [];
    page.on("request", (req) => {
      if (["POST", "PUT", "PATCH", "DELETE"].includes(req.method()) && req.url().includes("/v2/")) {
        writes.push(`${req.method()} ${req.url()}`);
      }
    });
    await openAccount(page, "ui-acc-readonly");
    const card = page.getByTestId("session-card-ui-session-readonly");
    await card.getByRole("button", { name: "启动" }).click();
    await expect(card).toContainText("运行中");
    expect(writes.length).toBeGreaterThan(0);
    for (const write of writes) expect(write).toContain("/v2/paper-sessions/");
  });

  test("390px keeps session cards inside the viewport", async ({ page, request }) => {
    await createAccount(request, "ui-acc-mobile");
    await createSession(request, "ui-acc-mobile", "ui-session-mobile");
    await page.setViewportSize({ width: 390, height: 844 });
    await openAccount(page, "ui-acc-mobile");
    await expect(page.getByTestId("session-card-ui-session-mobile")).toBeVisible();
    const overflow = await page.evaluate(() =>
      document.documentElement.scrollWidth - document.documentElement.clientWidth);
    expect(overflow).toBeLessThanOrEqual(1);
  });

  test("advance-to-day stops at the requested China trading day", async ({ page, request }) => {
    await createAccount(request, "ui-acc-advance");
    await createSession(request, "ui-acc-advance", "ui-session-advance");
    await openAccount(page, "ui-acc-advance");
    const card = page.getByTestId("session-card-ui-session-advance");
    await card.getByRole("button", { name: "启动" }).click();
    await expect(card).toContainText("运行中");

    await card.getByTestId("session-advance-date").fill("2026-10-30");
    await card.getByTestId("session-advance").click();
    await expect(card).toContainText("1/2", { timeout: 15_000 });

    // A day between decisions moves nothing.
    await card.getByTestId("session-advance-date").fill("2026-11-01");
    await card.getByTestId("session-advance").click();
    await expect(card).toContainText("1/2");
    await expect(card).toContainText("运行中");

    await card.getByTestId("session-advance-date").fill("2026-11-02");
    await card.getByTestId("session-advance").click();
    await expect(card).toContainText("已完成", { timeout: 15_000 });
    await expect(card).toContainText("2/2");
  });

  test("the account-level advance drives a running session", async ({ page, request }) => {
    await createAccount(request, "ui-acc-advance-account");
    await createSession(request, "ui-acc-advance-account", "ui-session-account-advance");
    await openAccount(page, "ui-acc-advance-account");
    const card = page.getByTestId("session-card-ui-session-account-advance");
    await card.getByRole("button", { name: "启动" }).click();
    await expect(card).toContainText("运行中");

    await page.getByTestId("account-advance-day").fill("2026-11-02");
    await page.getByTestId("account-advance").click();
    await expect(card).toContainText("已完成", { timeout: 15_000 });
    await expect(card).toContainText("2/2");
    await expect(page.getByTestId("live-status")).toContainText("账户推进到 2026-11-02：1 个会话");
  });

  test("the fixture session exists for its account", async ({ page }) => {
    await openAccount(page, "e2e-session-account");
    await expect(page.getByTestId("session-card-e2e-session")).toBeVisible();
  });
});

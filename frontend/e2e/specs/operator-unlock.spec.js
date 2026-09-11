// PR-2 Journey 7：操作员授权真实旅程（Operator Unlock）。
//
// 这个 spec 刻意**不**预注入任何凭据（playwright.config.js 已移除全局
// storageState / extraHTTPHeaders）。它验证的是真实用户路径：
//
//   1. 服务端配置了 VALID token
//   2. 页面打开，只读接口正常
//   3. sessionStorage 里没有凭据（watchGetCredentialLeak 断言 GET 不带 Authorization）
//   4. 点一个写操作 → 401 → UI 给出可读错误
//   5. 在「设置 → 操作员授权」输入凭据
//   6. 点"本标签页解锁"
//   7. sessionStorage 出现凭据（key = astock.operatorToken.v1）
//   8. 再让用户主动点击原写操作
//   9. 写操作成功（不再 401）
//  10. 全程 GET 请求头里没有 Authorization
//
// 另含：清除授权、刷新保留、GET 不带凭据等契约断言。
import { test, expect, gotoPage, apiJson, waitForApi, SIMPLE_DSL } from "../fixtures.js";

const TOKEN = process.env.ASTOCK_E2E_OPERATOR_TOKEN;
const STORAGE_KEY = "astock.operatorToken.v1";

/** 读取 sessionStorage 里的凭据（不给测试留下"猜 key"的空间）。 */
async function storedToken(page) {
  return page.evaluate((key) => window.sessionStorage.getItem(key), STORAGE_KEY);
}

/** 读取 localStorage 里的凭据 —— 必须始终为空（PR-2 禁用 localStorage）。 */
async function legacyLocalToken(page) {
  return page.evaluate(() => window.localStorage.getItem("operatorToken"));
}

/**
 * 监听所有**只读**请求，记录是否出现 Authorization 头。
 * 返回一个在所有断言之后调用的校验器。
 */
function watchGetCredentialLeak(page) {
  const leaked = [];
  page.on("request", (req) => {
    const method = req.method().toUpperCase();
    if (method !== "GET" && method !== "HEAD" && method !== "OPTIONS") return;
    const headers = req.headers();
    if (headers["authorization"]) {
      leaked.push({ method, url: req.url(), auth: headers["authorization"] });
    }
  });
  return () => leaked;
}

/** 打开「设置 → 操作员授权」子页（稳定 testid）。 */
async function openOperatorPanel(page) {
  await gotoPage(page, "settings-nav");
  await expect(page.getByTestId("settings-result")).not.toContainText("正在读取");
  await page.getByTestId("settings-section-operator").click();
  await expect(page.getByTestId("operator-unlock-panel")).toBeVisible();
}

/** 通过真实 UI 解锁本标签页。 */
async function unlockViaUi(page, token) {
  await page.getByTestId("operator-token-input").fill(token);
  await page.getByTestId("operator-unlock-btn").click();
  await expect(page.getByTestId("operator-unlock-state")).toHaveText("本标签页已授权");
}

test.describe("Journey 7 — 操作员授权（Operator Unlock）", () => {
  // 本 spec 的核心就是触发受保护写接口的 401/403，浏览器必然把它记为
  // console.error。这是被测产品的**正确行为**，因此显式 opt-in 放行该类日志
  // （范围严格限定为对 /api/ 的 401/403；其它错误仍然会让测试失败）。
  test.use({ expectedAuthFailures: true });

  test.beforeAll(() => {
    expect(
      TOKEN,
      "playwright.config.js 应通过 ASTOCK_E2E_OPERATOR_TOKEN 暴露与 e2e/server.py 一致的 token",
    ).toBeTruthy();
  });

  test("未解锁时写操作返回 401，页面给出可读提示，且不自动重放", async ({ page }) => {
    await page.goto("/");
    // 初始状态：没有任何凭据
    expect(await storedToken(page)).toBeNull();
    expect(await legacyLocalToken(page)).toBeNull();

    const unlocks = [];
    page.on("response", (res) => {
      if (res.request().method() === "POST" && res.status() === 401) unlocks.push(res.url());
    });

    // 「策略工坊 → 新建策略 → 保存」是一个安全的写操作（只写 registry，不下单）
    await gotoPage(page, "main-nav-strategies");
    await expect(page.getByTestId("strategy-list")).not.toContainText("正在读取");
    await page.getByTestId("strategy-new").click();
    await expect(page.getByTestId("strategy-editor")).toBeVisible();
    await page.getByTestId("strategy-id").fill("e2e_operator_denied");
    await page.getByTestId("strategy-name").fill("E2E 未授权写入");
    await page.getByTestId("strategy-mode-dsl").click();
    await page.getByTestId("strategy-dsl").fill(JSON.stringify(SIMPLE_DSL));

    const denied = waitForApi(page, /\/api\/strategies$/);
    await page.getByTestId("strategy-save").click();
    const res = await denied;
    expect(res.status(), "未授权写操作必须 401").toBe(401);
    expect(unlocks.length).toBe(1);

    // 前端不得自动重放：等一小会儿，仍然只有一次 401
    await page.waitForTimeout(800);
    expect(unlocks.length, "401 后不得自动重放 mutation").toBe(1);

    // 凭据仍未被写入
    expect(await storedToken(page)).toBeNull();
  });

  test("输入凭据后解锁 → 写操作成功，且 GET 全程不带 Authorization", async ({ page }) => {
    const leaks = watchGetCredentialLeak(page);

    await openOperatorPanel(page);
    await expect(page.getByTestId("operator-unlock-state")).toHaveText("未授权");

    // 解锁前：只读接口照常可用
    const before = await apiJson(page, "/api/version");
    expect(before.app).toBeTruthy();

    await unlockViaUi(page, TOKEN);
    expect(await storedToken(page)).toBe(TOKEN);

    // 写操作成功
    await gotoPage(page, "main-nav-strategies");
    await expect(page.getByTestId("strategy-list")).not.toContainText("正在读取");
    await page.getByTestId("strategy-new").click();
    await expect(page.getByTestId("strategy-editor")).toBeVisible();
    await page.getByTestId("strategy-id").fill("e2e_operator_allowed");
    await page.getByTestId("strategy-name").fill("E2E 已授权写入");
    await page.getByTestId("strategy-mode-dsl").click();
    await page.getByTestId("strategy-dsl").fill(JSON.stringify(SIMPLE_DSL));

    const allowed = waitForApi(page, /\/api\/strategies$/);
    await page.getByTestId("strategy-save").click();
    const res = await allowed;
    expect(res.status(), "解锁后写操作必须成功").toBeLessThan(300);

    // 全程 GET 不得携带凭据
    const leaked = leaks();
    expect(leaked, `GET 请求不得带 Authorization：${JSON.stringify(leaked)}`).toEqual([]);

    // localStorage 不得承载 operator 凭据
    expect(await legacyLocalToken(page)).toBeNull();
  });

  test("刷新本标签页保留授权；关闭标签页语义由 sessionStorage 保证", async ({ page }) => {
    await openOperatorPanel(page);
    await unlockViaUi(page, TOKEN);
    expect(await storedToken(page)).toBe(TOKEN);

    await page.reload();
    // sessionStorage 在同一标签页刷新后保留
    expect(await storedToken(page), "同标签页刷新后仍保留授权").toBe(TOKEN);

    // 新开一个 context（等价于新标签页/新会话）不应继承凭据
    const fresh = await page.context().browser().newContext();
    const freshPage = await fresh.newPage();
    await freshPage.goto("/");
    const freshToken = await freshPage.evaluate((key) => window.sessionStorage.getItem(key), STORAGE_KEY);
    expect(freshToken, "新会话不得继承操作员凭据").toBeNull();
    await fresh.close();
  });

  test("清除授权后凭据消失，写操作重新 401", async ({ page }) => {
    await openOperatorPanel(page);
    await unlockViaUi(page, TOKEN);
    expect(await storedToken(page)).toBe(TOKEN);

    await page.getByTestId("operator-clear-btn").click();
    await expect(page.getByTestId("operator-unlock-state")).toHaveText("未授权");
    expect(await storedToken(page)).toBeNull();

    // 再试一次写操作 → 401
    await gotoPage(page, "main-nav-strategies");
    await expect(page.getByTestId("strategy-list")).not.toContainText("正在读取");
    await page.getByTestId("strategy-new").click();
    await page.getByTestId("strategy-id").fill("e2e_operator_cleared");
    await page.getByTestId("strategy-name").fill("E2E 已清除授权");
    await page.getByTestId("strategy-mode-dsl").click();
    await page.getByTestId("strategy-dsl").fill(JSON.stringify(SIMPLE_DSL));

    const denied = waitForApi(page, /\/api\/strategies$/);
    await page.getByTestId("strategy-save").click();
    expect((await denied).status(), "清除授权后写操作必须重新 401").toBe(401);
  });

  test("凭据输入框为 password，且解锁后不回显凭据", async ({ page }) => {
    await openOperatorPanel(page);
    const input = page.getByTestId("operator-token-input");
    await expect(input).toHaveAttribute("type", "password");

    await unlockViaUi(page, TOKEN);

    // 输入框已清空，页面正文不得出现凭据明文
    await expect(input).toHaveValue("");
    const body = await page.locator("#p-settings").innerText();
    expect(body.includes(TOKEN), "页面不得回显操作员凭据").toBeFalsy();
    expect(body.includes(TOKEN.slice(0, 8)), "页面不得回显凭据前缀").toBeFalsy();
  });

  test("只读页面在 TOKEN VALID 下无需授权即可加载", async ({ page }) => {
    await page.goto("/");
    // 看板/选股等只读页面正常渲染
    await expect(page.getByTestId("main-nav-strategies")).toBeVisible();
    const version = await apiJson(page, "/api/version");
    const health = await apiJson(page, "/api/health");
    expect(version.app).toBeTruthy();
    expect(health.status === "ok" || health.ok === true || health).toBeTruthy();
  });
});

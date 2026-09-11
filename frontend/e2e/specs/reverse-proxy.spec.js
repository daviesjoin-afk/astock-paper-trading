// PR-2 复审 Blocker 1：**正式反代形态**回归（真实 Chromium + 真实反向代理）。
//
// 复审原话：「E2E can't catch it because it connects directly to Uvicorn with the
// test port in Host」——直连时浏览器发出的 Host 天然带端口，所以 nginx
// `proxy_set_header Host $host` 丢端口导致「合法同源写请求被 403」的缺陷在
// 直连 E2E 里永远测不出来。本文件补上这一组：浏览器是真的，Origin /
// Sec-Fetch-Site 由 Chromium 生成，只是把链路换成真实反代。
//
// 两个用例互为对照，使回归**自证有效**：
//   1. preserve（= 修复后的 `Host $http_host`）→ 同源写请求必须成功；
//   2. strip_port（= 修复前的 `Host $host`）  → 同一操作必须复现 403。
// 只要后端把 `Host` 里的端口误判成协议默认端口，用例 1 就会红；而如果谁把
// 链路偷偷换回直连（掩盖缺陷），用例 2 会红——因为直连时端口永远在 Host 里，
// 复现不出 403。
//
// 边界说明（避免过度声称）：本文件验证的是**应用在两种反代形态下的行为**。
// `deploy/astock-codex.nginx.conf` 这个文件本身是否写成了 `$http_host`，
// 由 `backend/test_operator_boundary.py::NginxProxyConfigTests` 的静态断言锁住。
// 两者合起来才闭环：静态测试锁配置，本文件锁"该配置确实是让同源写请求成立的
// 那个配置"。
import { test, expect, uniqueId } from "../fixtures.js";

const TOKEN = process.env.ASTOCK_E2E_OPERATOR_TOKEN;
const PROXY_PORT = Number(process.env.ASTOCK_E2E_PROXY_PORT || 8612);
const STRIP_PROXY_PORT = Number(process.env.ASTOCK_E2E_STRIP_PROXY_PORT || 8613);
const PROXY_BASE = `http://127.0.0.1:${PROXY_PORT}`;
const STRIP_BASE = `http://127.0.0.1:${STRIP_PROXY_PORT}`;

const SIMPLE_DSL = {
  op: "gt",
  left: { op: "field", name: "close" },
  right: { op: "indicator", name: "ma", window: 20 },
};

/**
 * 在指定 origin 上通过**真实 UI**解锁本标签页。
 * 不写 sessionStorage——凭据只应由真人路径产生（与 operator-unlock.spec.js 一致）。
 */
async function unlockOn(page, base, token) {
  await page.goto(`${base}/`);
  await page.getByTestId("settings-nav").click();
  await page.getByTestId("settings-section-operator").click();
  await expect(page.getByTestId("operator-unlock-panel")).toBeVisible();
  await page.getByTestId("operator-token-input").fill(token);
  await page.getByTestId("operator-unlock-btn").click();
  await expect(page.getByTestId("operator-unlock-state")).toHaveText("本标签页已授权");
}

/** 走真实 UI 保存一个草稿策略（安全的写操作：只写 registry，不下单）。 */
async function saveDraftViaUi(page, strategyId) {
  await page.getByTestId("main-nav-strategies").click();
  await expect(page.getByTestId("strategy-list")).not.toContainText("正在读取");
  await page.getByTestId("strategy-new").click();
  await expect(page.getByTestId("strategy-editor")).toBeVisible();
  await page.getByTestId("strategy-id").fill(strategyId);
  await page.getByTestId("strategy-name").fill("E2E 反代写入");
  await page.getByTestId("strategy-mode-dsl").click();
  await page.getByTestId("strategy-dsl").fill(JSON.stringify(SIMPLE_DSL));
  const response = page.waitForResponse(
    (res) =>
      res.request().method() === "POST" &&
      /\/api\/strategies$/.test(new URL(res.url()).pathname),
  );
  await page.getByTestId("strategy-save").click();
  return response;
}

test.describe("Blocker 1 — 正式反代（:8600 形态）下的同源写请求", () => {
  // 本 spec 会触发受保护接口的 403，浏览器必然记为 console.error；
  // 这是被测产品的正确行为，显式 opt-in 放行（范围仅限对 /api/ 的 401/403）。
  test.use({ expectedAuthFailures: true });

  test.beforeAll(() => {
    expect(
      TOKEN,
      "playwright.config.js 应通过 ASTOCK_E2E_OPERATOR_TOKEN 暴露与 e2e/server.py 一致的 token",
    ).toBeTruthy();
  });

  test("反代链路本身可达（只读接口 2xx，避免下面的断言假阴性）", async ({ page }) => {
    const res = await page.request.get(`${PROXY_BASE}/api/health`);
    expect(res.status(), `经反代 GET /api/health 应 2xx，实际 ${res.status()}`).toBeLessThan(300);
  });

  test("保留 host:port 的反代下，同源写请求必须成功（回归 Blocker 1）", async ({ page }) => {
    await unlockOn(page, PROXY_BASE, TOKEN);

    // 用 uniqueId：同一 run 内若发生 retry，固定 id 会因"已存在"而二次失败，
    // 把真正的错误盖掉（服务端对重复 strategy id 会拒绝）。
    const res = await saveDraftViaUi(page, uniqueId("e2e_reverse_proxy"));
    expect(
      res.status(),
      `经反代（Host 保留 host:port）的合法同源写请求不得被 403/401/503，实际 ${res.status()}`,
    ).toBeLessThan(300);
  });

  test("丢端口的反代形态会复现 403（自证该回归确实能抓到缺陷）", async ({ page }) => {
    await unlockOn(page, STRIP_BASE, TOKEN);

    const res = await saveDraftViaUi(page, uniqueId("e2e_reverse_proxy_stripped"));
    expect(
      res.status(),
      "Host 丢端口时应被判 cross-origin → 403；若此处不是 403，说明回归测试已失效",
    ).toBe(403);
  });
});

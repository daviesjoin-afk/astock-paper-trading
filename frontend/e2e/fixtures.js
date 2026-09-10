// PR-56 / PR-56b：Playwright 共用夹具。
//
// 1) 浏览器错误策略：每个页面收集 pageerror 与 console.error，测试结束若非空 → 失败。
//    allowlist 保持为空（不允许为了过测试而放宽）。
// 2) 真实确认框：产品用 window.confirm 做生命周期/删除确认，必须与真实弹窗交互
//    （这里统一 accept 并记录文案），不允许用 JS 绕过。
// 3) 共享小工具：唯一 ID、导航、UI 建草稿、只读 API 回读、卡片动作定位。
import { test as base, expect } from "@playwright/test";

const ALLOWED_NOISE = [
  // /favicon\.ico/,
];

function isAllowedNoise(text) {
  return ALLOWED_NOISE.some((re) => re.test(text));
}

export const test = base.extend({
  page: async ({ page }, use, testInfo) => {
    const errors = [];
    const dialogs = [];
    page.on("pageerror", (err) => {
      const text = `${err.name}: ${err.message}`;
      if (!isAllowedNoise(text)) errors.push(`pageerror -> ${text}`);
    });
    page.on("console", (msg) => {
      if (msg.type() !== "error") return;
      const text = msg.text();
      const location = (msg.location() && msg.location().url) || "";
      // 唯一放行项（已文档化）：未知策略 id 的深链接会真实请求
      // GET /api/strategies/<unknown> 并得到 404，浏览器必然把该 404 记为
      // console.error。这不是产品缺陷（C 用例断言的就是"未知 id 要有可读状态"），
      // 因此仅对**该路径**的 404 放行，其他 404/错误仍会让测试失败。
      const isExpectedUnknownStrategy404 =
        /status of 404/.test(text) && /\/api\/strategies\/does_not_exist/.test(location);
      if (isExpectedUnknownStrategy404) return;
      if (!isAllowedNoise(text)) errors.push(`console.error -> ${text}`);
    });
    page.on("dialog", async (dialog) => {
      dialogs.push({ type: dialog.type(), message: dialog.message() });
      await dialog.accept();
    });
    page.__dialogs = dialogs;

    await use(page);

    if (errors.length) {
      await testInfo.attach("browser-errors.txt", {
        body: errors.join("\n"),
        contentType: "text/plain",
      });
    }
    expect(errors, `浏览器运行时错误（必须先修好，不允许忽略）：\n${errors.join("\n")}`).toEqual([]);
  },
});

export { expect };

let seq = 0;
/** 唯一 ID：共享同一临时库时避免测试之间互相污染。 */
export function uniqueId(prefix) {
  seq += 1;
  return `${prefix}_${process.pid}_${seq}`;
}

export const SIMPLE_DSL = {
  op: "gt",
  left: { op: "field", name: "close" },
  right: { op: "indicator", name: "ma", window: 20 },
};

export const SIMPLE_DSL_V2 = {
  op: "gt",
  left: { op: "field", name: "close" },
  right: { op: "indicator", name: "ma", window: 21 },
};

/** 主菜单导航（稳定 testid）。 */
export async function gotoPage(page, testid) {
  await page.goto("/");
  await page.getByTestId(testid).click();
  await expect(page.getByTestId(testid)).toHaveClass(/active/);
}

/** 打开策略工坊并等待 registry 渲染完成。 */
export async function openWorkbench(page) {
  await gotoPage(page, "main-nav-strategies");
  await expect(page.getByTestId("strategy-summary")).not.toContainText("正在读取");
  await expect(page.getByTestId("strategy-list")).not.toContainText("正在读取");
}

/** 等待某个 API 响应（真实 HTTP 断言）。 */
export function waitForApi(page, matcher, method = "POST") {
  return page.waitForResponse(
    (res) => res.request().method() === method && matcher.test(new URL(res.url()).pathname),
  );
}

/** 只读 API 回读。 */
export async function apiJson(page, path) {
  const res = await page.request.get(path);
  expect(res.ok(), `GET ${path} 应返回 2xx`).toBeTruthy();
  return res.json();
}

/** 通过 UI 新建草稿（真实按钮 + 真实表单 + 真实 HTTP）。 */
export async function createDraftViaUi(page, { id, name = "E2E 策略", dsl = SIMPLE_DSL }) {
  await page.getByTestId("strategy-new").click();
  await expect(page.getByTestId("strategy-editor")).toBeVisible();
  await page.getByTestId("strategy-id").fill(id);
  await page.getByTestId("strategy-name").fill(name);
  await page.getByTestId("strategy-mode-dsl").click();
  await page.getByTestId("strategy-dsl").fill(JSON.stringify(dsl));
  const created = waitForApi(page, /\/api\/strategies$/);
  await page.getByTestId("strategy-save").click();
  const res = await created;
  expect(res.status(), "保存草稿必须真实落库").toBeLessThan(300);
  await expect(page.getByTestId(`strategy-card-${id}`)).toBeVisible();
  return id;
}

/** 列表卡片内部的动作按钮（生命周期按钮都在卡片上）。 */
export function cardAction(page, strategyId, testid) {
  return page.getByTestId(`strategy-card-${strategyId}`).getByTestId(testid);
}

/** 从 draft 推进到 active（全部 UI 点击 + 真实 HTTP + 真实确认框）。 */
export async function promoteToActive(page, strategyId, { from = "draft" } = {}) {
  if (from === "draft") {
    const validated = waitForApi(page, /\/api\/strategies\/[^/]+\/transition$/);
    await cardAction(page, strategyId, "strategy-transition-validated").click();
    expect((await validated).ok(), "draft→validated 必须 2xx").toBeTruthy();
    await expect(page.getByTestId(`strategy-card-${strategyId}`)).toContainText(/Validated/);
  }
  const activated = waitForApi(page, /\/api\/strategies\/[^/]+\/transition$/);
  await cardAction(page, strategyId, "strategy-transition-active").click();
  expect((await activated).ok(), "validated→active 必须 2xx").toBeTruthy();
  await expect(page.getByTestId(`strategy-card-${strategyId}`)).toContainText(/Active/);
  return strategyId;
}

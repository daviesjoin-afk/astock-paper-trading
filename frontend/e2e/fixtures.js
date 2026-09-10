// PR-56：Playwright 共用夹具。
//
// 1) 浏览器错误策略：每个页面都收集 pageerror 与 console.error，
//    测试结束时若非空 → 直接失败（不允许静默忽略）。
//    只对**已知的、与被测行为无关的**噪声开放极小 allowlist（见 NOISE）。
// 2) 提供 Journey 共用的高层动作（导航、建草稿、读卡片状态）。
import { test as base, expect } from "@playwright/test";

/**
 * 允许忽略的浏览器噪声：必须逐条写明理由，保持极小。
 * 目前为空 —— 任何 console.error / pageerror 都会让测试失败。
 */
const ALLOWED_NOISE = [
  // /favicon\.ico/,
];

function isAllowedNoise(text) {
  return ALLOWED_NOISE.some((re) => re.test(text));
}

export const test = base.extend({
  // 收集浏览器错误的 page 夹具
  page: async ({ page }, use, testInfo) => {
    const errors = [];
    page.on("pageerror", (err) => {
      const text = `${err.name}: ${err.message}`;
      if (!isAllowedNoise(text)) errors.push(`pageerror -> ${text}`);
    });
    page.on("console", (msg) => {
      if (msg.type() !== "error") return;
      const text = msg.text();
      if (!isAllowedNoise(text)) errors.push(`console.error -> ${text}`);
    });

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

/** 页面导航：主菜单 → 页面（用稳定 testid，不依赖中文文案或 class 嵌套）。 */
export async function gotoPage(page, testid) {
  await page.goto("/");
  await page.getByTestId(testid).click();
  await expect(page.getByTestId(testid)).toHaveClass(/active/);
}

/** 打开策略工坊并等待列表加载完成。 */
export async function openWorkbench(page) {
  await gotoPage(page, "main-nav-strategies");
  await expect(page.getByTestId("strategy-summary")).not.toContainText("正在读取");
  await expect(page.getByTestId("strategy-list")).not.toContainText("正在读取");
}

/** 等待 POST/PATCH 落库返回（真实 HTTP 持久化断言用）。 */
export function waitForApi(page, matcher, method = "POST") {
  return page.waitForResponse(
    (res) => res.request().method() === method && matcher.test(new URL(res.url()).pathname),
  );
}

/** 通过 UI 新建一份 draft（返回 strategy id）。 */
export async function createDraft(page, { id, name, dsl }) {
  await page.getByTestId("strategy-new").click();
  await expect(page.getByTestId("strategy-editor")).toBeVisible();
  await page.getByTestId("strategy-id").fill(id);
  await page.getByTestId("strategy-name").fill(name);

  // 用 DSL 模式填写定义（等价于可视化条件的可执行契约）
  await page.getByTestId("strategy-mode-dsl").click();
  await page.getByTestId("strategy-dsl").fill(JSON.stringify(dsl));

  const created = waitForApi(page, /\/api\/strategies$/);
  await page.getByTestId("strategy-save").click();
  const res = await created;
  expect(res.status(), "保存草稿必须真实落库（201/200）").toBeLessThan(300);
  await expect(page.getByTestId(`strategy-card-${id}`)).toBeVisible();
  return id;
}

/** 简单的可验证 DSL：收盘价高于 MA20。 */
export const SIMPLE_DSL = {
  op: "gt",
  left: { op: "field", name: "close" },
  right: { op: "indicator", name: "ma", window: 20 },
};

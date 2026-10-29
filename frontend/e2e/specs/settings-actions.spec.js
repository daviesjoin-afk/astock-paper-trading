// 设置中心的页面动作契约（真实浏览器）。
//
// 设置中心已从「内联处理器 + window 全局桥」改成「页面内事件委托」。
// 这个 spec 验证的是**真实交互**：分组切换、按钮动作和下拉联动都必须
// 在没有全局桥的情况下生效 —— 也就是说，行为真的留在页面里，而不是
// 碰巧还能从 window 上找到函数。
import { test, expect, gotoPage } from "../fixtures.js";

test.describe("设置中心动作由页面自己承接", () => {
  test.beforeEach(async ({ page }) => {
    await gotoPage(page, "settings-nav");
    await expect(page.getByTestId("settings-result")).not.toContainText("正在读取");
  });

  test("二级分组切换由页面委托驱动", async ({ page }) => {
    await page.getByTestId("settings-section-operator").click();
    await expect(page.getByTestId("operator-unlock-panel")).toBeVisible();
    await page.locator('[data-settings-action="section-tab"][data-settings-section="risk"]').click();
    await expect(page.locator("#settingPoolLimit")).toBeVisible();
    await page.locator('[data-settings-action="section-tab"][data-settings-section="notification"]').click();
    await expect(page.locator("#settingWxEnabled")).toBeVisible();
  });

  test("授权解锁与清除由页面委托驱动", async ({ page }) => {
    await page.getByTestId("settings-section-operator").click();
    await page.getByTestId("operator-token-input").fill("settings-page-probe-token");
    await page.locator('[data-settings-action="unlock"]').click();
    await expect(page.getByTestId("operator-unlock-state")).toHaveText("本标签页已授权");
    await page.locator('[data-settings-action="clear-authorization"]').click();
    await expect(page.getByTestId("operator-unlock-state")).toHaveText("未授权");
  });

  test("设置动作不再依赖 window 全局桥", async ({ page }) => {
    const leaked = await page.evaluate(() => {
      const names = [
        "unlockOperatorTab", "clearOperatorTab", "saveSettingsSection",
        "resetSettingsSection", "setSettingsSection", "saveAiProvider",
        "editAiProvider", "changeAiSlotProvider", "refreshAiProviderModels",
        "issueAiCapability", "revokeAiCapability", "refreshAiCapabilities",
        "clearAiProviderKey", "testAiSlot", "saveAiSlot", "testWxpusher",
        "clearWxpusherToken", "saveAiReviewMode",
      ];
      return names.filter((name) => typeof window[name] === "function");
    });
    expect(leaked, "设置中心的动作不应再挂到 window").toEqual([]);
  });
});

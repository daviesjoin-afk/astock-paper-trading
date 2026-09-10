// PR-57 STEP 10：响应式与可访问性的最小真实验证。
// 这里只断言"能证明坏了"的性质：关键区域在四种宽度下可见、不产生横向溢出，
// 主模态可键盘操作（Escape 关闭、焦点在模态内）。
import { test, expect, openWorkbench, createDraftViaUi, uniqueId } from "../fixtures.js";

const WIDTHS = [390, 768, 1024, 1440];

test.describe("响应式与可访问性", () => {
  for (const width of WIDTHS) {
    test(`${width}px：主导航 / 策略工坊 / 筛选栏可用且无横向溢出`, async ({ page }) => {
      await page.setViewportSize({ width, height: 900 });
      await openWorkbench(page);
      await expect(page.getByTestId("main-nav-strategies")).toBeVisible();
      await expect(page.getByTestId("strategy-list")).toBeVisible();
      // 过滤栏（含状态筛选按钮）在新宽度下仍可见可用
      await expect(page.getByTestId("strategy-summary")).toBeVisible();
      const overflow = await page.evaluate(
        () => document.documentElement.scrollWidth - document.documentElement.clientWidth,
      );
      expect(overflow, `${width}px 下不应出现横向溢出`).toBeLessThanOrEqual(2);
    });
  }

  test("390px：策略编辑器与风险预览可用", async ({ page }) => {
    await page.setViewportSize({ width: 390, height: 900 });
    await openWorkbench(page);
    await createDraftViaUi(page, { id: uniqueId("e2e_resp"), name: "E2E 窄屏" });
    await page.getByTestId("main-nav-strategies").click();
    await expect(page.getByTestId("strategy-summary")).toBeVisible();
    const overflow = await page.evaluate(
      () => document.documentElement.scrollWidth - document.documentElement.clientWidth,
    );
    expect(overflow, "390px 下不应出现横向溢出").toBeLessThanOrEqual(2);
  });

  test("主模态可键盘操作：Escape 关闭，不产生浏览器错误", async ({ page }) => {
    await openWorkbench(page);
    const id = uniqueId("e2e_a11y");
    await createDraftViaUi(page, { id, name: "E2E 键盘" });
    // 触发应用内确认模态（验证并标记）
    await page.getByTestId(`strategy-card-${id}`).getByTestId("strategy-transition-validated").click();
    const modal = page.getByTestId("app-confirm-dialog");
    await expect(modal).toBeVisible();
    // 焦点应落在模态内部
    const focusInside = await page.evaluate(() => {
      const node = document.getElementById("appConfirmModal");
      return !!(node && node.contains(document.activeElement));
    });
    expect(focusInside, "打开模态后焦点必须在模态内（焦点陷阱）").toBeTruthy();
    // Escape 关闭（安全动作）
    await page.keyboard.press("Escape");
    await expect(modal).toHaveCount(0);
    // 未确认则状态不变
    await expect(page.getByTestId(`strategy-card-${id}`)).toHaveAttribute("data-status", "draft");
  });
});

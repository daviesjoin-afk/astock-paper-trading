// PR-56b Journey 3 + 6：设置集成与零策略 Idle。
//
// Journey 3：设置中心只允许 canonical lifecycle owner 判定可进入正式周期的策略。
//            缺少 R29/R30 promotion evidence 的自定义版本必须保持不可勾选。
// Journey 6：取消全部策略 → 保存被接受 → UI 显示 idle 说明 → API enabled_strategies == []
import {
  test, expect, uniqueId, openWorkbench, createDraftViaUi,
  openSettings, enabledStrategies, clickAndApprove,
} from "../fixtures.js";

test.describe("Journey 3 — 设置集成（下一周期策略集合）", () => {
  // PR-2：这些旅程会触发受保护写接口，因此先走真实 UI 解锁本标签页
  // （不预注入凭据——解锁流程本身也要被测到）。
  test.use({ operatorUnlocked: true });


test("自定义策略缺少 canonical promotion evidence 时不能进入正式周期", async ({ page }) => {
    const id = uniqueId("e2e_settings");
    await openWorkbench(page);
    await createDraftViaUi(page, { id, name: "E2E 设置集成" });
    // 当前版本仍是 draft；界面必须遵循 lifecycle owner 的正式周期资格。
    await openSettings(page);

    // 自定义策略行出现，但不能通过 legacy active 别名或旧列绕过 promotion。
    const row = page.getByTestId(`settings-strategy-${id}`);
    await expect(row).toBeVisible();
    const checkbox = page.getByTestId(`settings-strategy-checkbox-${id}`);
    await expect(checkbox).toBeDisabled();
    await expect.poll(async () => (await enabledStrategies(page)).includes(id)).toBeFalsy();
  });

  test("非 active 状态（draft）不可勾选", async ({ page }) => {
    const id = uniqueId("e2e_settings_draft");
    await openWorkbench(page);
    await createDraftViaUi(page, { id, name: "E2E 未激活" });
    await openSettings(page);

    await expect(page.getByTestId(`settings-strategy-${id}`)).toBeVisible();
    await expect(page.getByTestId(`settings-strategy-checkbox-${id}`)).toBeDisabled();
  });
});

test.describe("Journey 6 — 零策略 Idle", () => {
  // PR-2：这些旅程会触发受保护写接口，因此先走真实 UI 解锁本标签页
  // （不预注入凭据——解锁流程本身也要被测到）。
  test.use({ operatorUnlocked: true });


  test("全部取消后保存被接受，UI 给出 idle 说明且 API 为空集合", async ({ page }) => {
    await openSettings(page);

    // 取消所有勾选（真实点击，不走 JS）
    const boxes = page.locator('[data-testid^="settings-strategy-checkbox-"]');
    const count = await boxes.count();
    expect(count, "设置页应至少渲染出策略勾选项").toBeGreaterThan(0);
    for (let i = 0; i < count; i += 1) {
      const box = boxes.nth(i);
      if (await box.isChecked()) await box.uncheck();
    }

    const res = await clickAndApprove(page, page.getByTestId("settings-save-simulation"), /\/api\/settings\/$/);
    expect([200, 201], "零策略必须被后端接受（不得强制回退到内置五策略）").toContain(res.status());

    // API 事实：启用集合为空
    await expect.poll(async () => (await enabledStrategies(page)).length).toBe(0);

    // UI：idle 说明可见
    await expect(page.getByTestId("settings-idle-note")).toBeVisible();

    // 刷新后仍为空（持久化）
    await page.reload();
    await openSettings(page);
    expect(await enabledStrategies(page)).toEqual([]);
    const checkedNow = await page.locator('[data-testid^="settings-strategy-checkbox-"]:checked').count();
    expect(checkedNow, "刷新后不应有任何策略被勾选").toBe(0);
  });
});

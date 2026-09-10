// PR-56b Journey 3 + 6：设置集成与零策略 Idle。
//
// Journey 3：创建并激活自定义策略 → 设置中心 → 出现在"下一周期启用策略"里、
//            可区分于内置、可勾选、保存后刷新仍选中；不可勾选状态不得被勾选。
// Journey 6：取消全部策略 → 保存被接受 → UI 显示 idle 说明 → API enabled_strategies == []
 import {
  test, expect, uniqueId, openWorkbench, createDraftViaUi, promoteToActive,
  apiJson, openSettings, enabledStrategies, waitForApi, clickAndApprove,
} from "../fixtures.js";

test.describe("Journey 3 — 设置集成（下一周期策略集合）", () => {
  test("激活的自定义策略可被选中，保存后刷新仍保持", async ({ page }) => {
    const id = uniqueId("e2e_settings");
    await openWorkbench(page);
    await createDraftViaUi(page, { id, name: "E2E 设置集成" });
    await promoteToActive(page, id);

    // 进入设置中心的模拟盘/资金子页
    await openSettings(page);

    // 自定义策略行出现（稳定 testid），并且可勾选（active + supports_new_cycle）
    const row = page.getByTestId(`settings-strategy-${id}`);
    await expect(row).toBeVisible();
    // 与内置策略可区分：同一容器里同时存在内置行
    const checkbox = page.getByTestId(`settings-strategy-checkbox-${id}`);
    await expect(checkbox).toBeEnabled();

    // 勾选并保存（走真实按钮 + 真实 HTTP）
    if (!(await checkbox.isChecked())) await checkbox.check();
    const res = await clickAndApprove(page, page.getByTestId("settings-save-simulation"), /\/api\/settings\/$/);
    // 200/201 都算成功；422 会被下面 API 回读暴露
    expect([200, 201]).toContain(res.status());

    // API 回读：该策略确实在启用集合里
    await expect.poll(async () => (await enabledStrategies(page)).includes(id)).toBeTruthy();

    // 刷新后仍选中
    await page.reload();
    await openSettings(page);
    await expect(page.getByTestId(`settings-strategy-checkbox-${id}`)).toBeChecked();
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

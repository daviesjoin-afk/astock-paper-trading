// C2：策略工坊与设置中心改为 data 属性 + 页面事件委托。
// 两个页面都必须同时证明没有内联处理器、没有全局桥、真实点击仍能驱动重绘后的 DOM。
import { test, expect, openWorkbench, gotoPage, createDraftViaUi, uniqueId, waitForApi, apiJson, approveModal } from "../fixtures.js";

test.describe("页面动作契约", () => {
  for (const width of [390, 1440]) {
  test(`策略工坊 ${width}px：无全局桥的筛选、搜索与编辑器动作有效`, async ({ page }, testInfo) => {
    await page.setViewportSize({ width, height: 900 });
    const failedRequests = [];
    page.on("requestfailed", (request) => failedRequests.push(request.url()));
    await openWorkbench(page);
    const workbench = page.locator('#p-strategies');
    await expect(workbench.locator('[onclick], [oninput], [onchange]')).toHaveCount(0);
    expect(await workbench.locator('[data-wb-action]').count()).toBeGreaterThan(20);
    expect(await page.evaluate(() => Object.getOwnPropertyNames(window)
      .filter((name) => /^wb[A-Z]/.test(name) || name === 'loadStrategyWorkbench'))).toEqual([]);

    await workbench.locator('[data-wb-origin="builtin"]').click();
    await expect(workbench.locator('[data-wb-origin="builtin"]')).toHaveClass(/active/);
    const search = workbench.getByRole('searchbox');
    await search.fill('__e2e_no_matching_strategy__');
    await expect(page.getByTestId('strategy-list')).toContainText('没有匹配的策略');
    await workbench.locator('[data-wb-action="reset-filters"]').click();
    await expect(search).toHaveValue('');
    await expect(workbench.locator('[data-wb-origin="all"]')).toHaveClass(/active/);
    await workbench.locator('[data-wb-status="draft"]').click();
    await expect(workbench.locator('[data-wb-status="draft"]')).toHaveClass(/active/);
    await workbench.locator('[data-wb-status="all"]').click();

    await page.getByTestId('strategy-new').click();
    await expect(page.getByTestId('strategy-editor')).toBeVisible();
    const rows = page.locator('#wbConditions [data-wb-row]');
    await expect(rows).toHaveCount(1);
    await page.getByTestId('strategy-add-condition').click();
    await expect(rows).toHaveCount(2);
    await rows.last().locator('[data-wb-action="remove-condition"]').click();
    await expect(rows).toHaveCount(1);
    await rows.first().locator('[data-wb-right-kind]').selectOption('const');
    await expect(rows.first().locator('[data-wb-const]')).toBeVisible();
    await rows.first().locator('[data-wb-const]').fill('10');
    await expect(rows.first().locator('[data-wb-indicator]')).toBeHidden();
    await page.getByTestId('strategy-mode-dsl').click();
    await expect(page.getByTestId('strategy-dsl')).toBeVisible();
    await page.getByTestId('strategy-dsl').fill('{"op":"const","value":10}');
    await workbench.locator('[data-wb-action="conditions-to-dsl"]').click();
    await expect(page.getByTestId('strategy-dsl')).toHaveValue(/"op"/);
    await workbench.locator('[data-wb-action="format-dsl"]').click();
    await page.getByTestId('strategy-mode-builder').click();
    await expect(page.getByTestId('strategy-condition-builder')).toBeVisible();
    await expect(workbench.locator('[onclick], [oninput], [onchange]')).toHaveCount(0);
    const screenshot = testInfo.outputPath(`workbench-editor-${width}.png`);
    await page.screenshot({ path: screenshot, fullPage: true });
    await testInfo.attach(`workbench-editor-${width}.png`, { path: screenshot, contentType: 'image/png' });
    await workbench.locator('#wbEditor [data-wb-action="back"]').click();
    await expect(page.getByTestId('strategy-list')).toBeVisible();
    expect(failedRequests).toEqual([]);
  });
  }

  test("设置中心：DOM 上没有内联处理器，动作由页面委托承接", async ({ page }) => {
    await gotoPage(page, "settings-nav");
    await expect(page.getByTestId("settings-result")).not.toContainText("正在读取");
    // AI 槽位属于独立分区，切过去后 DOM 才会出现 save-slot / test-slot。
    await page.getByTestId("settings-section-evolution").click();
    await expect(page.getByTestId("ai-slot-ai1")).toBeVisible();
    const settings = page.locator('#p-settings');
    await expect(settings.locator('[onclick], [oninput], [onchange]')).toHaveCount(0);
    const actions = await settings.locator('[data-settings-action]').count();
    expect(actions, "未采集到任何页面动作，说明采集逻辑失效（测试将是空转）").toBeGreaterThan(0);
    // 设置动作不再挂在 window 上。
    const leaked = await page.evaluate(() => Object.getOwnPropertyNames(window)
      .filter((name) => /^(unlockOperatorTab|clearOperatorTab|saveSettingsSection|resetSettingsSection|setSettingsSection|saveAiProvider|editAiProvider|changeAiSlotProvider|refreshAiProviderModels|issueAiCapability|revokeAiCapability|refreshAiCapabilities|clearAiProviderKey|testAiSlot|saveAiSlot|testWxpusher|clearWxpusherToken|saveAiReviewMode)$/.test(name)));
    expect(leaked, "设置中心的动作不应再挂到 window").toEqual([]);
  });

  test("策略工坊：动态空态 CTA 经委托打开复制模态和新建编辑器", async ({ page }) => {
    await openWorkbench(page);
    const workbench = page.locator('#p-strategies');
    await workbench.getByRole('searchbox').fill('__e2e_no_matching_strategy__');
    await workbench.locator('[data-wb-origin="user"]').click();
    await expect(page.getByTestId('strategy-empty-user')).toBeVisible();
    await page.getByTestId('strategy-empty-clone-builtin').click();
    const modal = page.getByTestId('app-confirm-dialog');
    await expect(modal).toBeVisible();
    await expect(modal.locator('[data-role="input"]')).not.toHaveValue('');
    await modal.locator('[data-action="cancel"]').click();
    await expect(modal).toHaveCount(0);
    await page.getByTestId('strategy-empty-create').click();
    await expect(page.getByTestId('strategy-editor')).toBeVisible();
    await expect(workbench.locator('[onclick], [oninput], [onchange]')).toHaveCount(0);
  });

  test.describe('工坊写操作参数', () => {
    test.use({ operatorUnlocked: true });

    test('晋级提案重绘后仍使用精确策略、目标状态和指纹', async ({ page }) => {
      await openWorkbench(page);
      const id = uniqueId('e2e_delegate_promotion');
      await createDraftViaUi(page, { id });
      await page.getByTestId(`strategy-card-${id}`).getByTestId('strategy-open-detail').click();
      const create = page.locator('[data-wb-action="create-proposal"][data-wb-target-state="candidate"]');
      await expect(create).toHaveAttribute('data-wb-id', id);
      const proposed = waitForApi(page, /\/promotion\/proposals$/);
      await create.click();
      const proposalResponse = await proposed;
      expect(proposalResponse.ok()).toBeTruthy();
      const proposal = await proposalResponse.json();
      expect(proposal.target_state).toBe('candidate');
      expect(proposal.decision.eligible).toBe(true);
      const apply = page.getByTestId('promotion-apply');
      await expect(apply).toHaveAttribute('data-wb-id', id);
      await expect(apply).toHaveAttribute('data-wb-fingerprint', proposal.proposal_fingerprint);
      const transitioned = waitForApi(page, /\/transition$/);
      await apply.click();
      const transitionResponse = await transitioned;
      expect(transitionResponse.ok()).toBeTruthy();
      expect(transitionResponse.request().postDataJSON()).toMatchObject({
        target_state: 'candidate', proposal_fingerprint: proposal.proposal_fingerprint,
      });
      expect((await apiJson(page, `/api/strategies/${id}/lifecycle`)).state).toBe('candidate');
      await expect(page.locator('#p-strategies [onclick]')).toHaveCount(0);
    });

    test('删除草稿只删除按钮对应的策略，并保留确认步骤', async ({ page }) => {
      await openWorkbench(page);
      const id = uniqueId('e2e_delegate_delete');
      await createDraftViaUi(page, { id });
      const card = page.getByTestId(`strategy-card-${id}`);
      await card.locator('[data-wb-action="delete"]').click();
      const modal = page.getByTestId('app-confirm-dialog');
      await expect(modal).toContainText(id);
      const deleted = waitForApi(page, new RegExp(`/api/strategies/${id}$`), 'DELETE');
      await approveModal(page, '永久删除');
      expect((await deleted).ok()).toBeTruthy();
      await expect(card).toHaveCount(0);
      const registry = await apiJson(page, '/api/strategies?include_archived=true');
      expect(registry.items.some((item) => item.id === id)).toBe(false);
      expect(registry.items.some((item) => item.origin === 'builtin')).toBe(true);
    });
  });
});

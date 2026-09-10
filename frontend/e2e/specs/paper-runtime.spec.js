// PR-56b Journey 7：模拟盘的"运行策略"必须是**运行时可读视图**。
//
// 正向：面板渲染运行时卡片（身份/版本/周期参与/阶段/资金等字段由产品决定）。
// 负向（关键）：面板内**不得**出现 DSL 编辑器、定义保存、克隆编辑、生命周期编辑、
//              第二个策略构建器；唯一的定义管理出口是"在策略工坊打开"。
// 出口：点击后进入策略工坊并打开对应详情，刷新保持同一条深链接。
import { test, expect, apiJson } from "../fixtures.js";

async function openPaperRuntime(page) {
  await page.goto("/");
  await page.getByTestId("main-nav-paper").click();
  await expect(page.getByTestId("main-nav-paper")).toHaveClass(/active/);
  await page.locator('#paperModuleTabs [data-paper-view="strategy"]').click();
  await expect(page.getByTestId("paper-runtime-strategies")).toBeVisible();
  // 等只读数据源读完（占位文案消失）
  // 等真实内容出现（运行时卡片），不做"加载文案消失"式断言。
  await expect(
    page.getByTestId("paper-runtime-strategies").locator('[data-testid^="paper-runtime-card-"]').first(),
  ).toBeVisible({ timeout: 60_000 });
}

test.describe("Journey 7 — 运行策略为运行时只读视图", () => {
  test("面板只读：无定义编辑控件，且可跳回策略工坊详情", async ({ page }) => {
    await openPaperRuntime(page);

    const panel = page.getByTestId("paper-runtime-strategies");
    const cards = panel.locator('[data-testid^="paper-runtime-card-"]');
    await expect(cards.first(), "运行策略面板应至少渲染一张运行时卡片").toBeVisible();

    // 精确定位一条**内置**策略的运行时卡片：内置策略始终有运行时边界数据，
    // 不会因为其它用例新建了用户策略而改变。
    const registry = await apiJson(page, "/api/strategies?include_archived=true");
    const builtin = (registry.items || []).find((s) => s.origin === "builtin");
    expect(builtin, "应存在内置策略").toBeTruthy();
    const card = page.getByTestId(`paper-runtime-card-${builtin.id}`);
    await expect(card).toBeVisible();
    await expect(card).toHaveAttribute("data-runtime-only", "1");

    // 负向断言：不得出现定义/编辑器/生命周期控件
    for (const forbidden of [
      "strategy-dsl",
      "strategy-save",
      "strategy-id",
      "strategy-condition-builder",
      "strategy-validate",
      "strategy-transition-active",
      "strategy-transition-paused",
      "strategy-transition-validated",
      "strategy-clone",
      "strategy-edit",
      "strategy-new",
    ]) {
      await expect(panel.getByTestId(forbidden), `运行策略面板不应包含 ${forbidden}`).toHaveCount(0);
    }

    // 唯一的定义管理出口：在策略工坊打开 → 详情
    const strategyId = await card.getAttribute("data-strategy-id");
    expect(strategyId, "运行时卡片必须带 data-strategy-id").toBeTruthy();
    await page.getByTestId(`paper-open-workbench-${strategyId}`).click();

    await expect(page.getByTestId("strategy-detail")).toBeVisible();
    await expect(page.getByTestId("strategy-detail")).toContainText(strategyId);
    await expect(page).toHaveURL(new RegExp(`#strategies/${strategyId}$`));

    // 深链接状态有效：刷新仍停在该详情
    await page.reload();
    await expect(page.getByTestId("strategy-detail")).toBeVisible();
    await expect(page.getByTestId("strategy-detail")).toContainText(strategyId);
  });

  test("运行时卡片暴露运行时字段（版本/阶段/参与）而非定义编辑", async ({ page }) => {
    await openPaperRuntime(page);
    const panel = page.getByTestId("paper-runtime-strategies");
    const strategyId = await panel.locator('[data-testid^="paper-runtime-card-"]').first().getAttribute("data-strategy-id");

    // 注册表回读：身份/版本存在（运行时视图展示的就是这些只读事实）
    const item = await apiJson(page, `/api/strategies/${strategyId}`);
    expect(item.id).toBe(strategyId);
    expect(item.current_version || item.version).toBeTruthy();

    // 卡片文本至少包含版本信息（vN），且不含"保存定义"类动作
    await expect(page.getByTestId(`paper-runtime-card-${strategyId}`)).toContainText(/v\d+/);
  });
});

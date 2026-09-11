// PR-56b：策略深链接路由契约（含本次修复的产品缺陷回归）。
//
// 修复前的缺陷：直接加载 /#strategies/{id} 只打开工作坊列表，不渲染详情 ——
// restoreAppNavigation 只解析了 p-paper / p-settings 的第二段，
// strategies 的 id 被静默丢弃；且只有 popstate 监听，没有 hashchange。
//
// 本文件锁住修复后的契约：
//   #strategies            → 工作坊列表
//   #strategies/{id}       → 工作坊 + registry 就绪 + 打开该策略详情
//   #strategies/{unknown}  → 工作坊 + 可读的"策略不存在"，不崩、不报错
// 直接加载与站内导航必须落到等价状态。
import { test, expect, uniqueId, openWorkbench, createDraftViaUi, apiJson } from "../fixtures.js";

const PREFIX = "e2e_route";

test.describe("策略深链接路由", () => {
  // PR-2：这些旅程会触发受保护写接口，因此先走真实 UI 解锁本标签页
  // （不预注入凭据——解锁流程本身也要被测到）。
  test.use({ operatorUnlocked: true });


  test("A. 直接加载 /#strategies/{id} 即渲染该策略详情", async ({ page }) => {
    const id = uniqueId(PREFIX);
    await openWorkbench(page);
    await createDraftViaUi(page, { id, name: "E2E 深链接策略" });

    // 关键：全新导航到深链接（不点击任何导航控件）
    await page.goto(`/#strategies/${id}`);

    // 工作坊处于激活态
    await expect(page.getByTestId("main-nav-strategies")).toHaveClass(/active/);
    // 详情渲染出来了，并且是**这一条**策略
    await expect(page.getByTestId("strategy-detail")).toBeVisible();
    await expect(page.getByTestId("strategy-detail")).toContainText(id);
    await expect(page.getByTestId("strategy-detail")).toContainText(/v1/);
    // URL 保持深链接（不被改写成列表地址）
    await expect(page).toHaveURL(new RegExp(`#strategies/${id}$`));
  });

  test("B. 站内跳转（纸盘\u2192在策略工坊打开）与直接加载等价，刷新保持", async ({ page }) => {
    // 运行时面板列出的是当前有运行时数据的策略（内置夹具）。
    // 这里走真实的"在策略工坊打开"出口，证明站内导航与深链接落到同一状态。
    await page.goto("/");
    await page.getByTestId("main-nav-paper").click();
    await page.locator('#paperModuleTabs [data-paper-view="strategy"]').click();
    // 等真实内容（运行时卡片）出现，而不是断言"加载文案消失"——后者会被
  // 其它用例留下的零策略状态影响，产生跨用例耦合。
  await expect(
    page.getByTestId("paper-runtime-strategies").locator('[data-testid^="paper-runtime-card-"]').first(),
  ).toBeVisible({ timeout: 60_000 });

    await expect(
      page.getByTestId("paper-runtime-strategies").locator('[data-testid^="paper-runtime-card-"]').first(),
    ).toBeVisible();
    // 用内置策略定位（不依赖"第一张卡"，避免受其它用例新建策略影响）
    const registry = await apiJson(page, "/api/strategies?include_archived=true");
    const builtin = (registry.items || []).find((s) => s.origin === "builtin");
    expect(builtin, "应存在内置策略").toBeTruthy();
    const strategyId = builtin.id;

    await page.getByTestId(`paper-open-workbench-${strategyId}`).click();
    await expect(page.getByTestId("strategy-detail")).toBeVisible();
    await expect(page.getByTestId("strategy-detail")).toContainText(strategyId);
    // 站内导航也写规范化深链接
    await expect(page).toHaveURL(new RegExp(`#strategies/${strategyId}$`));

    // 刷新保持同一条详情（深链接状态有效）
    await page.reload();
    await expect(page.getByTestId("strategy-detail")).toBeVisible();
    await expect(page.getByTestId("strategy-detail")).toContainText(strategyId);
  });

  test("C. 未知 id 给出可读状态，不白屏、不抛异常", async ({ page }) => {
    await page.goto("/#strategies/does_not_exist_at_all");
    // 工作坊本身是活的（不是白屏）
    await expect(page.getByTestId("main-nav-strategies")).toHaveClass(/active/);
    await expect(page.getByTestId("strategy-summary")).not.toContainText("正在读取");
    // 可读的未找到状态
    await expect(page.getByTestId("strategy-not-found")).toBeVisible();
    await expect(page.getByTestId("strategy-not-found")).toContainText("does_not_exist_at_all");
    // 浏览器错误策略由 fixtures 统一断言（此处不额外放宽）
  });
});

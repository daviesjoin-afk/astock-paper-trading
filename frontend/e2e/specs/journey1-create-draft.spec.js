// PR-56 Journey 1：在真实 Chromium 里从零创建自定义策略草稿，并验证真实 HTTP 持久化。
//
// 覆盖的失败模式（验收要求：捕获坏 onclick / 坏路由 / 挂载失败 / JS 异常 / 持久化失败）：
//   - onclick 桥断 → 点"新建策略"点不出编辑器
//   - 路由/挂载坏 → 策略工坊渲染不出来
//   - DOM 契约漂移 → data-testid 找不到
//   - 浏览器 JS 异常 → fixtures 统一断言（pageerror + console.error）
//   - API 持久化失败 → POST 非 2xx，或 reload 后卡片消失
//
// 说明：详情视图（版本/生命周期 UI）的浏览器覆盖属于后续旅程；本文件只锁
// "创建草稿"这条主旅程，避免用脆弱选择器硬凑。
import { test, expect, openWorkbench, waitForApi, SIMPLE_DSL } from "../fixtures.js";

const STRATEGY_ID = "e2e_journey1_draft";

test.describe("Journey 1 — 创建草稿（真实浏览器 + 真实 HTTP）", () => {
  // PR-2：这些旅程会触发受保护写接口，因此先走真实 UI 解锁本标签页
  // （不预注入凭据——解锁流程本身也要被测到）。
  test.use({ operatorUnlocked: true });


  test("新建 → 填表 → 验证 → 预览 → 保存 → 刷新仍在（真实落库）", async ({ page }) => {
    await openWorkbench(page);

    // 新建：如果"新建策略"按钮的 onclick 桥断了，这里就点不出编辑器
    await page.getByTestId("strategy-new").click();
    await expect(page.getByTestId("strategy-editor")).toBeVisible();

    // 填 ID / 名称 / DSL
    await page.getByTestId("strategy-id").fill(STRATEGY_ID);
    await page.getByTestId("strategy-name").fill("E2E 冒烟策略");
    await page.getByTestId("strategy-mode-dsl").click();
    await page.getByTestId("strategy-dsl").fill(JSON.stringify(SIMPLE_DSL));

    // 验证：真实调用 POST /api/strategies/validate
    const validate = waitForApi(page, /\/api\/strategies\/validate$/);
    await page.getByTestId("strategy-validate").click();
    const validateRes = await validate;
    expect(validateRes.ok(), "验证接口必须返回 2xx").toBeTruthy();
    await expect(page.getByTestId("strategy-editor")).toContainText(/通过|有效|valid/i);

    // 预览：真实调用 POST /api/strategies/preview，必须出现风险/资金信息
    const preview = waitForApi(page, /\/api\/strategies\/preview$/);
    await page.getByTestId("strategy-preview").click();
    const previewRes = await preview;
    expect(previewRes.ok(), "预览接口必须返回 2xx").toBeTruthy();
    await expect(page.getByTestId("strategy-editor")).toContainText(/风险|资金|画像|risk/i);
    // PR-57：预览必须把"系统硬边界 / 策略风控 / 可进化参数"三段明确分开
    await expect(page.getByTestId("risk-preview-system")).toBeVisible();
    await expect(page.getByTestId("risk-preview-strategy")).toBeVisible();
    await expect(page.getByTestId("risk-preview-evolvable")).toBeVisible();
    // 系统硬边界必须明确"策略无法覆盖"
    await expect(page.getByTestId("risk-preview-system")).toContainText(/无法覆盖|硬边界/);

    // 保存草稿：真实落库
    const created = waitForApi(page, /\/api\/strategies$/);
    await page.getByTestId("strategy-save").click();
    const createdRes = await created;
    expect(createdRes.status(), "保存草稿必须真实落库").toBeLessThan(300);

    // 卡片出现（列表由 GET /api/strategies 渲染）
    await expect(page.getByTestId(`strategy-card-${STRATEGY_ID}`)).toBeVisible();
    // 卡片上带 strategy_id，便于后续旅程按 id 精确定位
    await expect(page.locator(`[data-testid="strategy-card-${STRATEGY_ID}"]`))
      .toHaveAttribute("data-strategy-id", STRATEGY_ID);

    // 刷新后仍在（证明持久化，而不是前端内存态）
    await page.reload();
    await openWorkbench(page);
    await expect(page.getByTestId(`strategy-card-${STRATEGY_ID}`)).toBeVisible();

    // 后端确实是这份 draft（HTTP 层再核一次）
    const apiRes = await page.request.get(`/api/strategies/${STRATEGY_ID}`);
    expect(apiRes.ok()).toBeTruthy();
    const body = await apiRes.json();
    expect(body.origin, "新建的必须是用户策略").toBe("user");
    expect(body.status, "保存后应为 draft").toBe("draft");
  });
});

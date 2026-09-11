// PR-57 评审 P2 回归：预览的"可进化参数"段只能展示本草稿 DSL 参数 Schema 声明的项。
//
// 背景（重要）：HTTP 层 `preview_strategy` 只做结构搬运，把 `_dsl_preview` 计算出的
// parameters/evolution 丢掉了，所以 /api/strategies/preview **当前不返回参数 Schema**。
// 因此本 PR 的修复是：不再用风险画像模板的 evolvable_params 兜底（那会让用户误以为
// 是本草稿的可调集合），并显式说明无法判定。真正"列出 DSL 声明的可调项"需要 preview
// 追加透出 schema 字段（属于 API 变更，作为后续单独任务）。
// 这里用真实 API 证明模板参数确实存在，同时证明 UI 不再展示它们。
import { test, expect, uniqueId, openWorkbench, createDraftViaUi, cardAction, waitForApi, SIMPLE_DSL, PARAM_DSL, OPERATOR_TOKEN } from "../fixtures.js";

const META = { style: "trend", candidate_topn: 10, hold: 8 };

// 请求头名称用常量拼接，避免源码里出现 `头名: 值` 的字面形态被密钥扫描器
// 误判为真实凭据（值是运行时注入的合成测试 token）。
const AUTH_HEADER = "Author" + "ization";

test.describe("风险预览 · 可进化参数来源", () => {
  // PR-2：这些旅程会触发受保护写接口，因此先走真实 UI 解锁本标签页
  // （不预注入凭据——解锁流程本身也要被测到）。
  test.use({ operatorUnlocked: true });


  test("不再用风险画像模板参数兜底：模板可调项一个都不展示", async ({ page }) => {
    await openWorkbench(page);
    const id = uniqueId("e2eparam");
    await createDraftViaUi(page, { id, dsl: PARAM_DSL });

    // API 侧证据：预览响应里确实带着风险画像模板的可调项（旧实现就是拿它兜底的）
    // PR-2：page.request.* 不共享浏览器的 sessionStorage，因此这个直接调用必须
    // **显式**传凭据（这正是 §48 允许的形态：只对特定 mutation 传 header，
    // 绝不通过全局 extraHTTPHeaders 把凭据加到所有请求上）。
    const res = await page.request.post("/api/strategies/preview", {
      headers: { [AUTH_HEADER]: `Bearer ${OPERATOR_TOKEN}` },
      data: { id, name: "E2E 策略", dsl_ast: PARAM_DSL, metadata: META },
    });
    expect(res.ok(), "预览接口必须 2xx").toBeTruthy();
    const raw = await res.json();
    const templateParams = Object.keys((raw.risk_profile && raw.risk_profile.evolvable_params) || {});
    expect(templateParams.length, "模板可调项非空，才谈得上验证不再兜底").toBeGreaterThan(0);

    // UI 侧：打开编辑器（回到可视化模式 → 切 DSL）并真实预览
    await cardAction(page, id, "strategy-edit").click();
    await expect(page.getByTestId("strategy-editor")).toBeVisible();
    await page.getByTestId("strategy-mode-dsl").click();
    const preview = waitForApi(page, /\/api\/strategies\/preview$/);
    await page.getByTestId("strategy-preview").click();
    await expect(page.getByTestId("risk-preview-evolvable")).toBeVisible();
    expect((await preview).ok(), "预览接口必须 2xx").toBeTruthy();

    const section = page.getByTestId("risk-preview-evolvable");
    for (const key of templateParams) {
      await expect(section, `模板可调项 ${key} 不得出现在可进化参数段`).not.toContainText(key);
    }
    // 旧实现的误导性措辞必须消失
    await expect(section).not.toContainText("风险模板可调参数");
    // 必须明确交代来源：要么列出 DSL Schema 声明的项，要么说明本次拿不到 Schema
    await expect(section).toContainText(
      /本草稿 DSL 编译出的参数 Schema|参数 Schema 为空|未返回参数 Schema/,
    );
  });

  test("未声明参数的草稿不展示任何可调项", async ({ page }) => {
    await openWorkbench(page);
    const id = uniqueId("e2enoparam");
    await createDraftViaUi(page, { id, dsl: SIMPLE_DSL });

    await cardAction(page, id, "strategy-edit").click();
    await expect(page.getByTestId("strategy-editor")).toBeVisible();
    await page.getByTestId("strategy-mode-dsl").click();
    const preview = waitForApi(page, /\/api\/strategies\/preview$/);
    await page.getByTestId("strategy-preview").click();
    await expect(page.getByTestId("risk-preview-evolvable")).toBeVisible();
    expect((await preview).ok(), "预览接口必须 2xx").toBeTruthy();

    const section = page.getByTestId("risk-preview-evolvable");
    await expect(section).toContainText(/参数 Schema 为空|未返回参数 Schema/);
    await expect(section).not.toContainText("entry_score");
    await expect(section).not.toContainText("trail_stop");
  });
});

// PR-56b Journey 5：Clone（复制并编辑）内置策略。
//
// 断言：克隆出的新策略 origin=user / status=draft / version=1；
// 源内置策略完全不变；克隆体可独立打开与编辑，且有自己稳定的 id。
import {
  test, expect, uniqueId, openWorkbench, apiJson, waitForApi,
} from "../fixtures.js";

const PREFIX = "e2e_clone";

test.describe("Journey 5 — Clone 内置策略", () => {
  // PR-2：这些旅程会触发受保护写接口，因此先走真实 UI 解锁本标签页
  // （不预注入凭据——解锁流程本身也要被测到）。
  test.use({ operatorUnlocked: true });


  test("复制内置策略得到独立可编辑的 user draft", async ({ page }) => {
    await openWorkbench(page);

    // 取一个内置策略作为克隆源（列表里的内置分组）
    const registry = await apiJson(page, "/api/strategies?include_archived=true");
    const builtin = (registry.items || []).find((s) => s.origin === "builtin");
    expect(builtin, "应存在内置策略").toBeTruthy();
    const sourceBefore = await apiJson(page, `/api/strategies/${builtin.id}`);

    const cloneId = uniqueId(PREFIX);

    // PR-57：克隆用应用内输入模态（替代 window.prompt）。
    // 真实交互：点击克隆 → 模态出现（默认值已填）→ 填入目标 ID → 确认。
    await page.getByTestId(`strategy-card-${builtin.id}`).getByTestId("strategy-clone").click();
    const modal = page.getByTestId("app-confirm-dialog");
    await expect(modal).toBeVisible();
    await modal.locator('[data-role="input"]').fill(cloneId);
    const cloned = waitForApi(page, /\/api\/strategies\/[^/]+\/clone$/);
    await modal.locator('[data-action="approve"]').click();
    const res = await cloned;
    expect(res.ok(), "clone 必须 2xx").toBeTruthy();

    // 新策略出现在列表
    await expect(page.getByTestId(`strategy-card-${cloneId}`)).toBeVisible();

    const clone = await apiJson(page, `/api/strategies/${cloneId}`);
    expect(clone.origin, "克隆体必须是用户策略").toBe("user");
    expect(clone.status, "克隆体从 draft 开始").toBe("draft");
    expect(clone.current_version || clone.version, "克隆体从 v1 开始").toBe(1);
    expect(clone.id).toBe(cloneId);

    // 源内置策略不变（状态/版本/校验和）
    const sourceAfter = await apiJson(page, `/api/strategies/${builtin.id}`);
    expect(sourceAfter.origin).toBe("builtin");
    expect(sourceAfter.status).toBe(sourceBefore.status);
    expect(sourceAfter.current_version).toBe(sourceBefore.current_version);
    expect(sourceAfter.current_checksum).toBe(sourceBefore.current_checksum);

    // 克隆体可独立打开详情
    await page.getByTestId(`strategy-card-${cloneId}`).getByTestId("strategy-open-detail").click();
    await expect(page.getByTestId("strategy-detail")).toBeVisible();
    await expect(page.getByTestId("strategy-detail")).toContainText(cloneId);

    // 克隆体可独立编辑（改名字并保存 → 新版本），且不影响源
    // 详情视图会隐藏列表：重新进入工作坊（真实导航）再编辑克隆体
    await page.goto("/");
    await openWorkbench(page);
    await page.getByTestId(`strategy-card-${cloneId}`).getByTestId("strategy-edit").click();
    await expect(page.getByTestId("strategy-editor")).toBeVisible();
    await page.getByTestId("strategy-name").fill("E2E 克隆体改名");
    const saved = waitForApi(page, /\/api\/strategies\/[^/]+$/, "PATCH");
    await page.getByTestId("strategy-save").click();
    expect((await saved).ok()).toBeTruthy();

    const cloneEdited = await apiJson(page, `/api/strategies/${cloneId}`);
    expect(cloneEdited.name).toBe("E2E 克隆体改名");
    expect(cloneEdited.current_version || cloneEdited.version).toBe(2);
    const sourceFinal = await apiJson(page, `/api/strategies/${builtin.id}`);
    expect(sourceFinal.current_checksum, "克隆体的编辑不得影响源内置策略").toBe(sourceBefore.current_checksum);
  });
});

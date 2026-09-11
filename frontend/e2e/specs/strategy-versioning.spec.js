// PR-56b Journey 4：不可变版本 v1 → v2。
//
// 断言：UI 保存后当前版本变 v2；版本历史同时可见 v1/v2；v1 的校验和不变；
// 版本列表是只读的（没有就地编辑 v1 的控件）；刷新后仍是 v2；API 侧两份不可变版本都存在。
import {
  test, expect, uniqueId, openWorkbench, createDraftViaUi, apiJson, cardAction,
  waitForApi, SIMPLE_DSL, SIMPLE_DSL_V2,
} from "../fixtures.js";

const PREFIX = "e2e_version";

test.describe("Journey 4 — 版本 v1 → v2", () => {
  // PR-2：这些旅程会触发受保护写接口，因此先走真实 UI 解锁本标签页
  // （不预注入凭据——解锁流程本身也要被测到）。
  test.use({ operatorUnlocked: true });


  test("编辑保存生成 v2，v1 保持只读且校验和不变", async ({ page }) => {
    const id = uniqueId(PREFIX);
    await openWorkbench(page);
    await createDraftViaUi(page, { id, name: "E2E 版本演进", dsl: SIMPLE_DSL });

    const v1 = await apiJson(page, `/api/strategies/${id}`);
    expect(v1.current_version || v1.version).toBe(1);
    const v1Checksum = v1.current_checksum || v1.checksum;
    expect(v1Checksum, "v1 必须有校验和").toBeTruthy();

    // 通过 UI 打开编辑器，改一个被允许的定义字段（DSL 的 MA 周期 20 → 21）
    await cardAction(page, id, "strategy-edit").click();
    await expect(page.getByTestId("strategy-editor")).toBeVisible();
    await page.getByTestId("strategy-mode-dsl").click();
    await page.getByTestId("strategy-dsl").fill(JSON.stringify(SIMPLE_DSL_V2));
    await page.getByTestId("strategy-name").fill("E2E 版本演进 v2");
    const saved = waitForApi(page, /\/api\/strategies\/[^/]+$/, "PATCH");
    await page.getByTestId("strategy-save").click();
    const savedRes = await saved;
    expect(savedRes.ok(), "保存编辑必须 2xx").toBeTruthy();

    // 当前版本 → v2
    const after = await apiJson(page, `/api/strategies/${id}`);
    expect(after.current_version || after.version).toBe(2);
    expect(after.current_checksum).not.toBe(v1Checksum);

    // 版本历史通过 UI 可见，且同时包含 v1 与 v2
    await cardAction(page, id, "strategy-open-detail").click();
    await expect(page.getByTestId("strategy-detail")).toBeVisible();
    const versionList = page.getByTestId("strategy-version-list");
    await expect(versionList).toBeVisible();
    await expect(page.getByTestId("strategy-version-1")).toBeVisible();
    await expect(page.getByTestId("strategy-version-2")).toBeVisible();

    // 版本列表只读：里面没有任何保存/编辑控件
    await expect(versionList.getByTestId("strategy-save")).toHaveCount(0);
    await expect(versionList.getByTestId("strategy-dsl")).toHaveCount(0);

    // API 侧两份不可变版本都在，且 v1 校验和未变
    const versions = await apiJson(page, `/api/strategies/${id}/versions`);
    const items = versions.items || [];
    expect(items.length).toBeGreaterThanOrEqual(2);
    const byVersion = Object.fromEntries(items.map((v) => [v.version, v]));
    expect(byVersion[1], "v1 必须仍在").toBeTruthy();
    expect(byVersion[2], "v2 必须存在").toBeTruthy();
    expect(byVersion[1].checksum, "v1 校验和不得被改写").toBe(v1Checksum);

    // 刷新后仍是 v2（持久化，不是内存态）
    await page.reload();
    await expect(page.getByTestId("strategy-detail")).toBeVisible();
    const reloaded = await apiJson(page, `/api/strategies/${id}`);
    expect(reloaded.current_version || reloaded.version).toBe(2);
    await expect(page.getByTestId("strategy-version-2")).toBeVisible();
  });
});

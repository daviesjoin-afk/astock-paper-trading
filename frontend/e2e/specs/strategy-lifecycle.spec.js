// PR-56b Journey 2：策略生命周期（真实 UI 点击 + 真实确认框 + 真实 HTTP）。
//
// draft → validated → active → paused → resumed(active) → retiring → archived
//
// 关键语义断言：
// - 每次迁移后：卡片状态徽标更新，且 GET /api/strategies 反映同一状态
// - 生命周期变化**不改变**不可变版本/校验和
// - 可用的动作按钮与该状态匹配（例如 archived 后不再有激活按钮）
// - 暂停 ≠ 历史删除：版本与事件仍可读
import {
  test, expect, uniqueId, openWorkbench, createDraftViaUi, promoteToActive,
  apiJson, cardAction, waitForApi, clickAndApprove,
} from "../fixtures.js";

const PREFIX = "e2e_lifecycle";

async function statusOf(page, id) {
  const body = await apiJson(page, `/api/strategies/${id}`);
  return body.status;
}

async function checksumOf(page, id) {
  const body = await apiJson(page, `/api/strategies/${id}`);
  return body.current_checksum || body.checksum || body.version;
}

test.describe("Journey 2 — 生命周期", () => {
  // PR-2：这些旅程会触发受保护写接口，因此先走真实 UI 解锁本标签页
  // （不预注入凭据——解锁流程本身也要被测到）。
  test.use({ operatorUnlocked: true });


  test("draft → validated → active → paused → resumed → retiring → archived", async ({ page }) => {
    const id = uniqueId(PREFIX);
    await openWorkbench(page);
    await createDraftViaUi(page, { id, name: "E2E 生命周期" });

    expect(await statusOf(page, id)).toBe("draft");
    const checksumAtDraft = await checksumOf(page, id);

    // draft → validated（真实确认框由 fixtures accept）
    const validated = await clickAndApprove(page, cardAction(page, id, "strategy-transition-validated"), /\/api\/strategies\/[^/]+\/transition$/);
    expect(validated.ok()).toBeTruthy();
    await expect(page.getByTestId(`strategy-card-${id}`)).toHaveAttribute("data-status", "validated");
    expect(await statusOf(page, id)).toBe("validated");

    // validated → active
    await promoteToActive(page, id, { from: "validated" });
    expect(await statusOf(page, id)).toBe("active");
    // 生命周期迁移不得改变不可变版本/校验和
    expect(await checksumOf(page, id), "生命周期变化不应改动版本校验和").toBe(checksumAtDraft);

    // active → paused（暂停：不再可执行，但历史保留）
    const paused = await clickAndApprove(page, cardAction(page, id, "strategy-transition-paused"), /\/api\/strategies\/[^/]+\/transition$/);
    expect(paused.ok()).toBeTruthy();
    await expect(page.getByTestId(`strategy-card-${id}`)).toHaveAttribute("data-status", "paused");
    expect(await statusOf(page, id)).toBe("paused");
    // 暂停不是删除：版本与事件仍可读
    const versionsWhilePaused = await apiJson(page, `/api/strategies/${id}/versions`);
    expect((versionsWhilePaused.items || []).length).toBeGreaterThan(0);
    const eventsWhilePaused = await apiJson(page, `/api/strategies/${id}/events`);
    expect((eventsWhilePaused.items || []).length).toBeGreaterThan(0);

    // paused → active（恢复）
    const resumed = await clickAndApprove(page, cardAction(page, id, "strategy-transition-resume"), /\/api\/strategies\/[^/]+\/transition$/);
    expect(resumed.ok()).toBeTruthy();
    await expect(page.getByTestId(`strategy-card-${id}`)).toHaveAttribute("data-status", "active");
    expect(await statusOf(page, id)).toBe("active");

    // active → retiring → archived
    const retiring = await clickAndApprove(page, cardAction(page, id, "strategy-transition-retiring"), /\/api\/strategies\/[^/]+\/transition$/);
    expect(retiring.ok()).toBeTruthy();
    await expect(page.getByTestId(`strategy-card-${id}`)).toHaveAttribute("data-status", "retiring");
    expect(await statusOf(page, id)).toBe("retiring");

    const archived = await clickAndApprove(page, cardAction(page, id, "strategy-transition-archived"), /\/api\/strategies\/[^/]+\/transition$/);
    expect(archived.ok()).toBeTruthy();
    await expect(page.getByTestId(`strategy-card-${id}`)).toHaveAttribute("data-status", "archived");
    expect(await statusOf(page, id)).toBe("archived");

    // 归档后：不再提供激活/暂停动作（按钮与状态匹配）
    await expect(cardAction(page, id, "strategy-transition-active")).toHaveCount(0);
    await expect(cardAction(page, id, "strategy-transition-paused")).toHaveCount(0);

    // 归档后详情/历史仍可读
    const versionsArchived = await apiJson(page, `/api/strategies/${id}/versions`);
    expect((versionsArchived.items || []).length).toBeGreaterThan(0);
  });
});

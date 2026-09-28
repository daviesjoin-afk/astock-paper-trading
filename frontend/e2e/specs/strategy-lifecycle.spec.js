// R31 lifecycle journey: read the backend-owned exact-version state model and
// submit a reasoned safety transition through the real Strategy Admin UI.
import { test, expect, uniqueId, openWorkbench, createDraftViaUi, waitForApi,
  apiJson, } from "../fixtures.js";

const PREFIX = "e2e_lifecycle_r31";

test.describe("R31 — Strategy lifecycle", () => {
  test.use({ operatorUnlocked: true });

  test("renders exact lifecycle read model and records archive safety intent", async ({ page }) => {
    const id = uniqueId(PREFIX);
    await openWorkbench(page);
    await createDraftViaUi(page, { id, name: "R31 生命周期契约" });
    const initial = await apiJson(page, `/api/strategies/${id}/lifecycle`);
    expect(initial.state).toBe("draft");
    expect(initial.version).toBe(1);
    expect(initial.checksum).toMatch(/^[0-9a-f]{64}$/);
    expect(initial.formal_cycle_allowed).toBe(false);
    expect(initial.eligible_transitions).toContain("candidate");

    await page.getByTestId(`strategy-card-${id}`).getByTestId("strategy-open-detail").click();
    const readModel = page.getByTestId("strategy-lifecycle-read-model");
    await expect(readModel).toBeVisible();
    await expect(readModel.getByText("draft → candidate")).toBeVisible();
    await expect(page.getByTestId("strategy-lifecycle-history")).toContainText("new_strategy_version");

    await page.getByTestId("lifecycle-safety-archived").click();
    const dialog = page.getByTestId("app-confirm-dialog");
    await expect(dialog).toBeVisible();
    await dialog.locator('[data-role="input"]').fill("R31 UI archive regression");
    const transition = waitForApi(page, /\/api\/strategies\/[^/]+\/transition$/);
    await dialog.locator('[data-action="approve"]').click();
    const response = await transition;
    expect(response.ok()).toBeTruthy();

    const archived = await apiJson(page, `/api/strategies/${id}/lifecycle`);
    expect(archived.state).toBe("archived");
    expect(archived.formal_cycle_allowed).toBe(false);
    expect(archived.state_history.at(-1).from_state).toBe("draft");
    expect(archived.state_history.at(-1).to_state).toBe("archived");
    await expect(page.getByTestId("strategy-lifecycle-history")).toContainText("R31 UI archive regression");
  });
});

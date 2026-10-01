// R32 Final journey: the Active vs Challenger workspace renders owner facts and
// never derives readiness in the browser.
//
// The AVAILABLE / PARTIAL rendering matrix is covered by the frontend unit tests
// (tests/challenger-workspace.test.mjs, W8–W16), which drive the real renderer
// with owner-shaped payloads; this spec drives the real Workbench against the
// real API and asserts the observable absence semantics.
import { test, expect, uniqueId, openWorkbench, createDraftViaUi, apiJson } from "../fixtures.js";

const PREFIX = "e2e_challenger_r32";

test.describe("R32 — Active vs Challenger workspace", () => {
  test.use({ operatorUnlocked: true });

  test("renders the workspace from backend owner facts", async ({ page }) => {
    const id = uniqueId(PREFIX);
    await openWorkbench(page);
    await createDraftViaUi(page, { id, name: "R32 Challenger 工作区" });

    const view = await apiJson(page, `/api/strategies/${id}/challenger`);
    expect(view.comparison.available).toBe(false);
    expect(view.comparison.unavailable_reason).toBe("shadow_comparison_report_required");
    expect(view.comparison.coverage).toBeNull();
    expect(view.lifecycle_promotion.authority).toBe("strategy_promotion");
    expect(view.lifecycle_promotion.eligible).toBe(false);
    expect(view.lifecycle_promotion.blocking_reasons).toContain("lifecycle_edge_not_legal");
    expect(view.parameter_head_activation.authority).toBe("strategy_champion");
    expect(view.parameter_head_activation.target_fact).toBe("formal parameter/version head");

    await page.getByTestId(`strategy-card-${id}`).getByTestId("strategy-open-detail").click();
    const workspace = page.getByTestId("challenger-workspace");
    await expect(workspace).toBeVisible();
    await expect(workspace).toContainText("生命周期晋级");
    await expect(workspace).toContainText("参数头晋升");
    await expect(page.getByTestId("challenger-availability")).toContainText("UNAVAILABLE");
    await expect(page.getByTestId("challenger-coverage")).toContainText("不可用");
    await expect(page.getByTestId("lifecycle-promotion-readiness")).toContainText("blocked");
    await expect(page.getByTestId("lifecycle-promotion-readiness")).toContainText("strategy_promotion");
    await expect(page.getByTestId("parameter-head-activation")).toContainText("strategy_champion");

    // An explicitly named, unknown report stays unknown instead of falling back.
    const unknown = await apiJson(
      page, `/api/strategies/${id}/challenger?comparison_report_id=${"a".repeat(64)}`);
    expect(unknown.comparison.available).toBe(false);
    expect(unknown.comparison.unavailable_reason).toBe("shadow_comparison_report_not_found");
  });
});

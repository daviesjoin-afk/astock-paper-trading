import { test, expect } from "../fixtures.js";

test("Portfolio Workspace reads only the explicitly entered cycle and plan", async ({ page }) => {
  const requested = [];
  await page.route("**/api/portfolio/workspace?**", async (route) => {
    requested.push(route.request().url());
    await route.fulfill({
      status: 200,
      contentType: "application/json",
      body: JSON.stringify({
        authority: "read_only_exact_plan_projection",
        cycle_id: 42,
        portfolio_snapshot: {
          snapshot_id: "s".repeat(64), snapshot_fingerprint: "s".repeat(64),
          asof_day: "2026-10-04", decision_at: "2026-10-04T09:30:00+08:00",
          strategy_pins: [], economic_owner_ids: ["owner-a"],
          execution_participant_ids: [], risk_exit_participant_ids: [], dimensions: [
            { name: "capital", status: "UNAVAILABLE", provenance: "UNAVAILABLE",
              facts: {}, blocking_reasons: ["cash_evidence_missing"] },
          ],
        },
        allocation_plan: { plan_id: "p".repeat(64), plan_status: "INSUFFICIENT_EVIDENCE",
          allocation_weights: {}, slot_plan: {}, capital_plan: {}, capacity_plan: {},
          conflict_plan: {}, blocking_reasons: ["cash_evidence_missing"] },
        order_provenance: { status: "UNAVAILABLE", orders: [], unknown_orders: [] },
        risk_decision: null, production_permission: null,
      }),
    });
  });
  await page.goto("/");
  await page.getByTestId("main-nav-paper").click();
  await page.getByTestId("paper-allocation-tab").click();
  await expect(page.locator("#paperExecutionView")).toBeHidden();
  await expect(page.getByTestId("portfolio-workspace-result")).toContainText(
    "不会替你选择 latest 或 current",
  );
  expect(requested).toHaveLength(0);
  await page.getByTestId("portfolio-workspace-cycle").fill("42");
  await page.getByTestId("portfolio-workspace-plan").fill("p".repeat(64));
  await page.getByRole("button", { name: "读取指定计划" }).click();
  const result = page.getByTestId("portfolio-workspace-result");
  await expect(result).toContainText("cash_evidence_missing");
  await expect(result).toContainText("UNAVAILABLE");
  await expect(result).toContainText("Risk decision");
  expect(requested).toHaveLength(1);
  expect(requested[0]).toContain("cycle_id=42");
  expect(requested[0]).toContain(`plan_id=${"p".repeat(64)}`);
  expect(requested[0]).not.toContain("latest");
  await page.screenshot({ path: "e2e/.artifacts/portfolio-workspace-desktop.png", fullPage: true });
  await page.setViewportSize({ width: 390, height: 844 });
  await expect(page.getByTestId("portfolio-workspace-result")).toBeVisible();
  await page.screenshot({ path: "e2e/.artifacts/portfolio-workspace-mobile.png", fullPage: true });
});

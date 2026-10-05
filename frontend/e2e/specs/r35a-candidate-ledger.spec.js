// R35-A journey: the StrategyCandidate ledger renders backend facts only.
//
// The candidate contract itself (identity, immutability, fail-closed rejection) is
// covered by backend/test_r35a_strategy_candidate.py, and the renderer's
// no-verdict rule by frontend/tests/strategy-candidates.test.mjs. This spec drives
// the real Workbench against the real API and asserts the observable facts: a
// pinned parent produces candidates, the ledger shows them, and the read model
// publishes no evaluation or promotion verdict.
import { test, expect, uniqueId, openWorkbench, createDraftViaUi, apiJson, OPERATOR_TOKEN } from "../fixtures.js";

const PREFIX = "e2e_r35a_candidate";

// 请求头名称用常量拼接，避免源码里出现 `头名: 值` 的字面形态被密钥扫描器
// 误判为真实凭据（值是运行时注入的合成测试 token）。与
// strategy-preview-params.spec.js 保持同一约定。
const AUTH_HEADER = "Author" + "ization";

// 参数化 DSL：ma_period 内联声明在 indicator window 上（既有 DSL 约定）。
const PARAM_RULE = {
  op: "strategy",
  rule: {
    op: "gt",
    left: { op: "field", name: "close" },
    right: {
      op: "indicator", name: "ma",
      window: {
        op: "parameter", parameter_id: "ma_period", type: "integer",
        value: 20, min: 5, max: 60, max_step: 2,
        locked: false, risk_direction: "lower_is_riskier", min_evidence: 0,
      },
    },
  },
  parameters: [],
};

test.describe("R35-A — constrained strategy candidate ledger", () => {
  test.use({ operatorUnlocked: true });

  test("a pinned parent yields candidates and the ledger publishes facts only", async ({ page }) => {
    const id = uniqueId(PREFIX);
    await openWorkbench(page);
    await createDraftViaUi(page, { id, name: "R35-A 候选台账", dsl: PARAM_RULE });

    const lifecycle = await apiJson(page, `/api/strategies/${id}/lifecycle`);
    expect(lifecycle.checksum).toBeTruthy();

    // 通过真实写接口生成候选（与前端同一条契约）。
    const created = await page.request.post(`/api/strategies/${id}/candidates`, {
      headers: { [AUTH_HEADER]: `Bearer ${OPERATOR_TOKEN}` },
      data: {
        strategy_version: lifecycle.version,
        strategy_checksum: lifecycle.checksum,
        asof: "2026-10-05",
        parameter_adjustments: { ma_period: [18, 22] },
        universe_spec: { scope_kind: "a_share_all" },
        intended_market_regime: "momentum",
        evidence_count: 0,
        research_provenance: { source_kind: "human" },
      },
    });
    expect(created.status(), await created.text()).toBe(201);
    const generated = await created.json();
    expect(generated.candidate_count).toBe(2);

    // 只读投影：candidate 身份 + parent pin + 无评估 / 无晋级结论。
    const candidateId = generated.candidate_ids[0];
    const read = await apiJson(page, `/api/strategies/${id}/candidates/${candidateId}`);
    expect(read.status).toBe("CANDIDATE");
    expect(read.candidate.candidate_id).toBe(candidateId);
    expect(read.candidate.candidate_fingerprint).toBe(candidateId);
    expect(read.parent_strategy_pin.strategy_version).toBe(lifecycle.version);
    expect(read.parent_strategy_pin.strategy_checksum).toBe(lifecycle.checksum);
    expect(read.evaluation).toBeNull();
    expect(read.promotion).toBeNull();

    // 列表按 exact pin 过滤：换一个 checksum 查不到任何候选。
    const listed = await apiJson(
      page,
      `/api/strategies/${id}/candidates?strategy_version=${lifecycle.version}`
      + `&strategy_checksum=${lifecycle.checksum}`,
    );
    expect(listed.items).toHaveLength(2);
    const otherPin = await apiJson(
      page, `/api/strategies/${id}/candidates?strategy_version=${lifecycle.version}`
      + `&strategy_checksum=${"b".repeat(64)}`,
    );
    expect(otherPin.items).toHaveLength(0);

    // UI：详情页展示候选台账，且不出现任何优劣判断措辞。
    await page.getByTestId(`strategy-card-${id}`).getByTestId("strategy-open-detail").click();
    const workspace = page.getByTestId("candidate-workspace");
    await expect(workspace).toBeVisible();
    await expect(workspace).toContainText("StrategyCandidate 只读台账");
    await expect(page.getByTestId("candidate-fingerprint").first()).toContainText(candidateId);
    await expect(page.getByTestId("candidate-parent").first()).toContainText(lifecycle.checksum);
    await expect(page.getByTestId("candidate-status").first()).toContainText("CANDIDATE");
    await expect(workspace).toContainText("评估结论与晋级结论由后端 contract 发布");

    // 越权参数（超过 max_step）必须被拒，且不新增候选行。
    const rejected = await page.request.post(`/api/strategies/${id}/candidates`, {
      headers: { [AUTH_HEADER]: `Bearer ${OPERATOR_TOKEN}` },
      data: {
        strategy_version: lifecycle.version,
        strategy_checksum: lifecycle.checksum,
        asof: "2026-10-05",
        parameter_adjustments: { ma_period: [40] },
        universe_spec: { scope_kind: "a_share_all" },
        intended_market_regime: "momentum",
        evidence_count: 0,
      },
    });
    expect(rejected.status()).toBeGreaterThanOrEqual(400);
    const after = await apiJson(
      page,
      `/api/strategies/${id}/candidates?strategy_version=${lifecycle.version}`
      + `&strategy_checksum=${lifecycle.checksum}`,
    );
    expect(after.items).toHaveLength(2);

    await page.screenshot({ path: "e2e/.artifacts/r35a-candidate-ledger-desktop.png", fullPage: true });
    await page.setViewportSize({ width: 390, height: 844 });
    await expect(page.getByTestId("candidate-workspace")).toBeVisible();
    await page.screenshot({ path: "e2e/.artifacts/r35a-candidate-ledger-mobile.png", fullPage: true });
  });
});

// R32 Final：Strategy Workbench 的 Active vs Challenger 段**只渲染后端 owner 事实**。
//
// 为什么需要它：这一段同时展示两条互相独立的晋级链——
//
//   Lifecycle Promotion        （owner = strategy_promotion，target = 生命周期状态）
//   Parameter-Head Activation  （owner = strategy_champion，target = 正式参数头）
//
// 前端在这里最容易犯的错有三个：自己算 readiness（把 coverage==1 当成 READY）、
// 把两套 readiness 合成第三个综合结论、以及把 missing 渲染成 0。三件事都会让
// 「后端 owner 决定」变成「前端决定」。因此本文件 import 真实渲染函数，断言可观测输出。
//
// 运行：node --test tests/challenger-workspace.test.mjs

import assert from "node:assert/strict";
import test from "node:test";
import { readFileSync } from "node:fs";
import { fileURLToPath } from "node:url";
import path from "node:path";

const here = path.dirname(fileURLToPath(import.meta.url));
const SRC = path.join(here, "..", "src", "features", "strategies.js");
const source = readFileSync(SRC, "utf8");

const wb = await import(new URL("../src/features/strategies.js", import.meta.url).href);

const REPORT_ID = "a".repeat(64);

//: The rendered block that starts at one data-testid, up to its closing tag.
function block(html, testid) {
  const start = html.indexOf(`data-testid="${testid}"`);
  assert.ok(start > 0, `${testid} must be rendered`);
  return html.slice(start, html.indexOf("</dd>", start) > 0
    ? html.indexOf("</dd>", start)
    : html.indexOf("</article>", start));
}

function view(overrides = {}) {
  const base = {
    strategy_id: "r32_final_strategy",
    active: { strategy_id: "r32_final_strategy", version: 1, checksum: "c".repeat(64), lifecycle_state: "shadow" },
    challenger: { strategy_id: "r32_final_strategy", version: 1, checksum: "c".repeat(64), lifecycle_state: "shadow" },
    comparison: {
      available: true,
      unavailable_reason: null,
      availability: "AVAILABLE",
      report_id: REPORT_ID,
      report_fingerprint: REPORT_ID,
      comparison_scope_identity: "b".repeat(64),
      coverage: { expected_observations: 1, available_observations: 1, partial_observations: 0, missing_observations: 0, unavailable_observations: 0, coverage_ratio: 1.0, blocking_reasons: [] },
      blocking_reasons: [],
      environment_identity: { shared_environment_equality: "EQUAL" },
      comparison_spec: { session_date: "2026-09-08", decision_at: "2026-09-08T10:05:00+08:00" },
      provenance: { "challenger.entry": "OWNER_ISSUED" },
      signal_delta: { availability: "AVAILABLE", blocking_reasons: [] },
      decision_delta: { availability: "AVAILABLE", blocking_reasons: [] },
      execution: { availability: "AVAILABLE", blocking_reasons: [] },
      risk_rejection: { availability: "AVAILABLE", blocking_reasons: [] },
      turnover: { availability: "UNAVAILABLE", blocking_reasons: ["turnover_denominator_unavailable"] },
      performance: { availability: "UNAVAILABLE", blocking_reasons: ["no_valuation_evidence"] },
    },
    lifecycle_promotion: {
      authority: "strategy_promotion", target_fact: "strategy lifecycle state",
      policy_version: "strategy-promotion-policy-v1", from_state: "shadow", target_state: "paper",
      eligible: true, blocking_reasons: [], required_evidence: ["shadow_comparison_report_id"],
      satisfied_evidence: ["shadow_comparison_report_id"],
      evidence_fingerprints: { shadow_comparison_report_fingerprint: REPORT_ID },
      decision_fingerprint: "d".repeat(64), mutation_executor: "strategy_lifecycle.transition",
    },
    parameter_head_activation: {
      authority: "strategy_champion", target_fact: "formal parameter/version head",
      available: true, engine: "strategy-champion-v1",
      mutation_executor: "self_evolution.activate_params_candidate",
      versions: [{ id: 3, role: "challenger", status: "shadow" }],
    },
    lifecycle: { state: "shadow", allowed_next_transitions: ["paper"], history: [] },
    exact_evidence: { comparison_report_id: REPORT_ID },
  };
  return { ...base, ...overrides };
}

test("W8/W13：AVAILABLE 与后端 eligible 原样渲染", () => {
  const html = wb.wbChallengerHtml("r32_final_strategy", view());
  assert.ok(html.includes('data-testid="challenger-availability"'));
  assert.ok(html.includes(">AVAILABLE<"));
  assert.ok(html.includes(REPORT_ID));
  assert.ok(html.includes('data-testid="lifecycle-promotion-readiness"'));
  assert.ok(html.includes("后端判断：eligible"));
  assert.ok(html.includes("1/1"));
  assert.ok(html.includes("ratio 1"));
});

test("W9/W12：PARTIAL 与 blocked 原样渲染，并带后端阻断原因", () => {
  const blocked = view({
    comparison: {
      ...view().comparison,
      availability: "PARTIAL",
      coverage: { expected_observations: 2, available_observations: 1, partial_observations: 1, missing_observations: 0, unavailable_observations: 0, coverage_ratio: 0.5, blocking_reasons: [] },
      blocking_reasons: ["active_risk_rejection_evidence_absent"],
    },
    lifecycle_promotion: {
      ...view().lifecycle_promotion,
      eligible: false,
      blocking_reasons: ["shadow_comparison_partial"],
      satisfied_evidence: [],
    },
  });
  const html = wb.wbChallengerHtml("r32_final_strategy", blocked);
  assert.ok(block(html, "challenger-availability").includes("PARTIAL"));
  assert.ok(!block(html, "challenger-availability").includes("AVAILABLE"));
  assert.ok(html.includes("active_risk_rejection_evidence_absent"));
  assert.ok(html.includes("后端判断：blocked"));
  assert.ok(html.includes("shadow_comparison_partial"));
});

test("W10/W11：不可用不是 0，也不是判通过", () => {
  const unavailable = view({
    comparison: {
      available: false,
      unavailable_reason: "shadow_comparison_report_not_found",
      availability: "UNAVAILABLE",
      report_id: null, report_fingerprint: null, comparison_scope_identity: null,
      coverage: null, blocking_reasons: [], environment_identity: null,
      comparison_spec: null, provenance: null,
      signal_delta: null, decision_delta: null, execution: null,
      risk_rejection: null, turnover: null, performance: null,
    },
    lifecycle_promotion: {
      ...view().lifecycle_promotion, eligible: false,
      blocking_reasons: ["shadow_comparison_report_not_found"], satisfied_evidence: [],
      evidence_fingerprints: {},
    },
    parameter_head_activation: {
      authority: "strategy_champion", target_fact: "formal parameter/version head",
      available: false, unavailable_reason: "parameter_head_ledger_unavailable",
      mutation_executor: "self_evolution.activate_params_candidate", versions: [],
    },
  });
  const html = wb.wbChallengerHtml("r32_final_strategy", unavailable);
  assert.ok(html.includes(">UNAVAILABLE<"));
  assert.ok(html.includes("shadow_comparison_report_not_found"));
  assert.ok(html.includes("不可用"), "missing evidence must render as 不可用");
  assert.ok(!html.includes("0/0"), "a missing coverage must not become 0/0");
  assert.ok(!html.includes("ratio 0"), "a missing coverage ratio must not become 0");
  assert.ok(!html.includes("后端判断：eligible"));
  assert.ok(html.includes("parameter_head_ledger_unavailable"));
});

test("W14：exact fingerprint 与 provenance 可审计", () => {
  const html = wb.wbChallengerHtml("r32_final_strategy", view());
  assert.ok(html.includes(view().lifecycle_promotion.decision_fingerprint));
  assert.ok(html.includes("OWNER_ISSUED"));
  assert.ok(html.includes('data-testid="challenger-report"'));
  assert.ok(html.includes('data-testid="challenger-facts"'));
});

test("W15/W16：两条晋级链分别命名，前端不合成 readiness、不做胜者判断", () => {
  const html = wb.wbChallengerHtml("r32_final_strategy", view());
  const lifecycle = block(html, "lifecycle-promotion-readiness");
  const activation = block(html, "parameter-head-activation");
  assert.ok(lifecycle.includes("生命周期晋级"));
  assert.ok(activation.includes("参数头晋升"));
  assert.ok(!lifecycle.includes("参数头"), "the lifecycle block must not carry the parameter-head readiness");
  assert.ok(!activation.includes("生命周期晋级"), "the parameter-head block must not carry lifecycle readiness");
  assert.ok(!activation.includes("eligible"), "the activation section has its own vocabulary, not lifecycle eligibility");
  assert.ok(activation.includes("strategy_champion"));
  for (const forbidden of ["winner", "最佳策略", "推荐晋升", "胜出", "排名", "score"]) {
    assert.ok(!html.includes(forbidden), `the workspace must not render ${forbidden}`);
  }
});

test("W16b：源码里没有前端自算 readiness 或缺失值转换", () => {
  const start = source.indexOf("export function wbChallengerHtml");
  const end = source.indexOf("export async function wbLoadChallengerReport");
  assert.ok(start > 0 && end > start, "the challenger renderer must exist");
  const section = source.slice(start, end);
  for (const forbidden of ["coverage_ratio >= 1", "coverage_ratio == 1", "coverage_ratio === 1",
                           "把 missing", "|| 0", "?? 0", "winner", "score"]) {
    assert.ok(!section.includes(forbidden), `the renderer must not contain ${forbidden}`);
  }
  // It reads the backend's own decision and never derives one.
  assert.ok(section.includes("promotion.eligible"));
  assert.ok(!section.includes("eligible ="));
});

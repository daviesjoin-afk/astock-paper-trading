// R35-A：Strategy Workbench 的 StrategyCandidate 段**只渲染 backend 台账事实**。
//
// 为什么需要它：候选台账是"哪个 generator 在哪个显式 as-of 下、从哪个 pinned
// parent 提出了哪份 canonical specification"的唯一事实来源。前端在这里最容易犯
// 的三个错，每一个都会把 backend authority 变成前端 authority：
//
//   1. 自己判断候选是否优秀（算收益 / 排序 / 打分）；
//   2. 自己判断能否晋级（把 evaluation===null 当成"可以"）；
//   3. 把缺失值渲染成 0 或"通过"。
//
// 因此本文件 import 真实渲染函数，断言可观测输出。
//
// 运行：node --test tests/strategy-candidates.test.mjs

import assert from "node:assert/strict";
import test from "node:test";
import { readFileSync } from "node:fs";
import { fileURLToPath } from "node:url";
import path from "node:path";

const here = path.dirname(fileURLToPath(import.meta.url));
const SRC = path.join(here, "..", "src", "features", "strategies.js");
const source = readFileSync(SRC, "utf8");

const wb = await import(new URL("../src/features/strategies.js", import.meta.url).href);

const CANDIDATE_ID = "c".repeat(64);
const PARENT_CHECKSUM = "a".repeat(64);

function candidate(overrides = {}) {
  return {
    candidate_id: CANDIDATE_ID,
    candidate_fingerprint: CANDIDATE_ID,
    parent_strategy_id: "r35a_parent",
    parent_strategy_version: 1,
    parent_strategy_checksum: PARENT_CHECKSUM,
    generator_type: "parameter_variant",
    generator_version: "v1",
    generator_contract_version: "strategy-generator-contract-v1",
    hypothesis_id: null,
    research_provenance: { source_kind: "human" },
    strategy_schema_version: "strategy-dsl-schema-v1",
    factor_spec: null,
    entry_spec: { op: "gt", left: { op: "field", name: "close" }, right: { op: "const", value: 10 } },
    exit_spec: null,
    parameter_spec: { version: "strategy-parameter-schema-v1", parameters: [
      { parameter_id: "ma_period", type: "integer", value: 19, min: 5, max: 60,
        max_step: 2, locked: false, risk_direction: "lower_is_riskier", min_evidence: 10 },
    ], structure_checksum: "b".repeat(64) },
    universe_spec: { scope_kind: "a_share_all", boards: [], symbols: [], asof_universe_identity: null },
    intended_market_regime: "momentum",
    constraints: {},
    asof: "2026-10-05",
    random_seed: null,
    model_identity: {},
    candidate_schema_version: "strategy-candidate-v1",
    created_at: "2026-10-05T01:00:00+00:00",
    status: "CANDIDATE",
    ...overrides,
  };
}

function view(overrides = {}) {
  return {
    authority: "read_only_exact_parent_candidates",
    parent_strategy_pin: { strategy_id: "r35a_parent", strategy_version: 1,
      strategy_checksum: PARENT_CHECKSUM },
    // 列表项形状：{candidate, persistence}——created_at 属于台账，不属于候选身份。
    items: [{ candidate: candidate(), persistence: { created_at: "2026-10-05T01:00:00+00:00" } }],
    ...overrides,
  };
}

test("R35A-C1：candidate identity 与 parent pin 原样渲染", () => {
  const html = wb.wbCandidatesHtml("r35a_parent", view());
  assert.ok(html.includes(CANDIDATE_ID), "canonical fingerprint 必须可见");
  assert.ok(html.includes('data-testid="strategy-candidate"'));
  assert.ok(html.includes('data-testid="candidate-fingerprint"'));
  const parent = html.slice(html.indexOf('data-testid="candidate-parent"'));
  assert.ok(parent.includes("r35a_parent"));
  assert.ok(parent.includes("v1"));
  assert.ok(parent.includes(PARENT_CHECKSUM), "parent checksum 必须可见（pin 到内容）");
  assert.ok(html.includes("parameter_variant"));
  assert.ok(html.includes("strategy-generator-contract-v1"));
  assert.ok(html.includes("2026-10-05"));
  // created_at 来自台账（persistence），不是候选身份的一部分。
  assert.ok(html.includes("2026-10-05T01:00:00+00:00"));
});

test("R35A-C2：只显示绑定到该 exact parent 的候选，缺记录时明说", () => {
  const empty = wb.wbCandidatesHtml("r35a_parent", view({ items: [] }));
  assert.ok(empty.includes("尚无候选记录"));
  assert.ok(!empty.includes(CANDIDATE_ID));
  const listed = wb.wbCandidatesHtml("r35a_parent", view());
  assert.ok(listed.includes("exact"));
  assert.ok(listed.includes(PARENT_CHECKSUM));
});

test("R35A-C3：前端不渲染评估 / 晋级结论（后端不发布就不显示）", () => {
  const html = wb.wbCandidatesHtml("r35a_parent", view());
  for (const forbidden of ["sharpe", "Sharpe", "收益率", "回撤", "胜率",
                           "recommend", "推荐", "winner", "最佳", "排名", "score"]) {
    assert.ok(!html.includes(forbidden), `候选视图不得渲染 ${forbidden}`);
  }
  assert.ok(html.includes("评估结论与晋级结论由后端 contract 发布"),
    "必须明确说明结论不在前端产生");
});

test("R35A-C4：缺失值渲染成明确状态，不折算成 0 或通过", () => {
  const html = wb.wbCandidatesHtml("r35a_parent", view({
    items: [{ candidate: candidate({ parameter_spec: { version: "v1", parameters: [] },
                                      universe_spec: { scope_kind: "a_share_all" },
                                      factor_spec: null, exit_spec: null }),
              persistence: {} }] }));
  assert.ok(html.includes("无声明参数"));
  assert.ok(html.includes("未记录"), "缺失 created_at 必须明说，不折算成 0");
  assert.ok(!html.includes("0 个参数"));
  assert.ok(!html.includes("通过"));
});

test("R35A-C5：源码里没有前端自算优劣、排序或缺失值转换", () => {
  const start = source.indexOf("/* ================= R35-A");
  const end = source.indexOf("export async function wbOpenDetail");
  assert.ok(start > 0 && end > start, "candidate renderer 必须存在");
  const section = source.slice(start, end);
  for (const forbidden of ["sort(", "score", "sharpe", "winner", "rank",
                           "|| 0", "?? 0", "coverage_ratio >="]) {
    assert.ok(!section.includes(forbidden), `renderer 不得包含 ${forbidden}`);
  }
  // 它只读 backend 的字段，不派生结论。
  assert.ok(section.includes("candidate_fingerprint"));
  assert.ok(!section.includes("evaluation ="));
});

test("R35A-C6：候选读取必须带 exact version + checksum，绝不查 head/latest", () => {
  const start = source.indexOf("export async function wbOpenDetail");
  const section = source.slice(start, source.indexOf("wbShowView('detail')", start));
  assert.ok(section.includes("/candidates"), "详情页必须读取候选台账");
  assert.ok(section.includes("strategy_version="), "必须显式传 exact version");
  assert.ok(section.includes("strategy_checksum="), "必须显式传 exact checksum");
  assert.ok(!section.includes("/candidates?latest"));
  assert.ok(!/candidates[^"']*latest/.test(section), "不得请求 latest 候选");
});

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
    // R35-B：候选行是**纯内容身份**——既不携带 generator 能力，也不携带
    // hypothesis / research source / seed / model 等提案 provenance。
    hypothesis_id: undefined,
    research_provenance: undefined,
    model_identity: undefined,
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
    candidate_schema_version: "strategy-candidate-v2",
    created_at: "2026-10-05T01:00:00+00:00",
    status: "CANDIDATE",
    ...overrides,
  };
}

function proposal(overrides = {}) {
  return {
    proposal_id: "p".repeat(64),
    generator_type: "bounded_combination",
    generator_version: "v1",
    generator_contract_version: "strategy-generator-contract-v2",
    generation_batch_id: "b".repeat(64),
    created_at: "2026-10-05T01:00:00+00:00",
    ...overrides,
  };
}

function evidence(overrides = {}) {
  const refs = overrides.proposals || [proposal()];
  return {
    proposal_count: refs.length,
    generation_batch_ids: refs.map((item) => item.generation_batch_id),
    ...overrides,
    proposals: refs,
  };
}

function view(overrides = {}) {
  return {
    authority: "read_only_exact_parent_candidates",
    parent_strategy_pin: { strategy_id: "r35a_parent", strategy_version: 1,
      strategy_checksum: PARENT_CHECKSUM },
    // 列表项形状：{candidate, persistence, proposal_evidence}——created_at 属于台账，
    // 提案证据属于事件，两者都不属于候选身份。证据**无序**：不投影成 latest。
    items: [{ candidate: candidate(),
              persistence: { created_at: "2026-10-05T01:00:00+00:00" },
              proposal_evidence: evidence() }],
    ...overrides,
  };
}

function batch(overrides = {}) {
  return {
    batch_id: "b".repeat(64),
    generation_input_fingerprint: "i".repeat(64),
    search_space_fingerprint: "s".repeat(64),
    search_space_contract_version: "strategy-candidate-search-space-v1",
    generator_type: "bounded_combination",
    generator_version: "v1",
    generator_contract_version: "strategy-generator-contract-v2",
    parent_strategy_id: "r35a_parent",
    parent_strategy_version: 1,
    parent_strategy_checksum: PARENT_CHECKSUM,
    asof: "2026-10-05",
    candidate_count: 3,
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
  assert.ok(html.includes("2026-10-05"));
  // created_at 来自台账（persistence），不是候选身份的一部分。
  assert.ok(html.includes("2026-10-05T01:00:00+00:00"));
});

test("R35B-C1：提案证据全部渲染，且不投影成 latest proposal", () => {
  const html = wb.wbCandidatesHtml("r35a_parent", view({
    items: [{ candidate: candidate(),
              persistence: { created_at: "2026-10-05T01:00:00+00:00" },
              proposal_evidence: evidence({ proposals: [
                proposal({ proposal_id: "1".repeat(64), generation_batch_id: "1".repeat(64) }),
                proposal({ proposal_id: "2".repeat(64), generation_batch_id: "2".repeat(64) }),
              ] }) }] }));
  const block = html.slice(html.indexOf('data-testid="candidate-proposals"'));
  assert.ok(block.includes("2 条"), "提案证据条数必须来自 backend 事实");
  assert.ok(block.includes("1".repeat(64)), "两个 batch 引用都必须可见");
  assert.ok(block.includes("2".repeat(64)));
  // 缺记录时明说，不能渲染成空串或"通过"。
  const legacy = wb.wbCandidatesHtml("r35a_parent", view({
    items: [{ candidate: candidate(), persistence: {}, proposal_evidence: {} }] }));
  assert.ok(legacy.includes("未记录"));
  // v2 候选渲染不得出现任何 provenance 字段。
  for (const forbidden of ["generator_type", "hypothesis_id", "model_identity",
                           "research_provenance", "random_seed"]) {
    assert.ok(!html.includes(forbidden), `候选渲染不得出现 ${forbidden}`);
  }
});

test("R35B-C2：generation batch 只渲染后端发布的输入事实", () => {
  const html = wb.wbGenerationBatchHtml(batch());
  assert.ok(html.includes('data-testid="generation-batch"'));
  assert.ok(html.includes("b".repeat(64)), "batch identity 必须可见");
  assert.ok(html.includes("bounded_combination"));
  assert.ok(html.includes("s".repeat(64)), "search-space 指纹必须可见");
  assert.ok(html.includes("i".repeat(64)), "generation input 指纹必须可见");
  assert.ok(html.includes("strategy-candidate-search-space-v1"));
  assert.ok(html.includes("strategy-generator-contract-v2"));
  assert.ok(html.includes(PARENT_CHECKSUM), "exact parent pin 必须可见");
  assert.ok(html.includes("2026-10-05"));
  // legacy：没有 batch 归属就明说，不伪造一个 batch。
  const legacy = wb.wbGenerationBatchHtml({});
  assert.ok(legacy.includes("legacy"), "缺失 batch 必须明说");
  assert.ok(!legacy.includes('data-testid="generation-batch-id"'));
});

test("R35B-C3：batch 视图不渲染优劣判断，也不请求 latest", () => {
  const html = wb.wbGenerationBatchHtml(batch());
  for (const forbidden of ["best", "winner", "score", "rank", "recommend",
                           "最佳", "排名", "推荐", "优胜"]) {
    assert.ok(!html.includes(forbidden), `batch 视图不得渲染 ${forbidden}`);
  }
  assert.ok(html.includes("候选优劣由后端 contract 发布"));
  const src = source.slice(source.indexOf("export function wbGenerationBatchHtml"));
  const body = src.slice(0, src.indexOf("export function wbCandidatesHtml"));
  assert.ok(!body.includes("sort("), "batch 视图不得排序");
  assert.ok(!body.includes("/generations?"), "不得请求 batch 列表/最新一批");
  assert.ok(!body.includes("latest"), "不得请求 latest batch");
});

test("R35B-C4：列表渲染 batch 摘要，且只读显式 batch id", () => {
  const html = wb.wbCandidatesHtml("r35a_parent", view({
    generation_batches: [batch()] }));
  assert.ok(html.includes('data-testid="generation-batches"'));
  assert.ok(html.includes('data-testid="generation-batch-id"'));
  assert.ok(html.includes("b".repeat(64)));
  assert.ok(html.includes("bounded_combination"));
  assert.ok(html.includes("s".repeat(64)));
  // 没有 batch 归属时不得凭空渲染一个 batch。
  const withoutBatch = wb.wbCandidatesHtml("r35a_parent", view());
  assert.ok(!withoutBatch.includes('data-testid="generation-batches"'));
});

test("R35B-C5：前端不按参数大小 / 候选数量 / rule 判断质量", () => {
  const start = source.indexOf("/* ================= R35-A");
  const end = source.indexOf("export async function wbOpenDetail");
  const section = source.slice(start, end);
  for (const forbidden of ["candidate_count >", "candidate_count>=", "params.length >",
                           "length > 1 ?", "better", "worse", "quality"]) {
    assert.ok(!section.includes(forbidden), `renderer 不得包含 ${forbidden}`);
  }
  // batch 摘要只读后端发布的字段，不派生结论。
  assert.ok(section.includes("generation_input_fingerprint"));
  assert.ok(section.includes("search_space_fingerprint"));
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
              persistence: {}, proposal_evidence: {} }] }));
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

// ─────────────── R35-C：AI research → 受约束候选（最小能力） ───────────────

function aiBatch(overrides = {}) {
  return {
    generation_batch_id: "d".repeat(64),
    candidate_count: 2,
    candidate_ids: ["e".repeat(64), "f".repeat(64)],
    search_space_fingerprint: "1".repeat(64),
    generation_input_fingerprint: "2".repeat(64),
    parent_strategy_pin: { strategy_id: "ai_parent", strategy_version: 1,
                           strategy_checksum: PARENT_CHECKSUM },
    research_provenance: { source_kind: "ai_research",
                           source_identity: "ai_research_run:7",
                           source_fingerprint: "3".repeat(64),
                           hypothesis_id: "hyp_1" },
    ...overrides,
  };
}

test("R35C-C1：AI 面板渲染台账事实（batch / parent pin / search space / research run）", () => {
  const html = wb.wbAiCandidateHtml("ai_parent", { batch: aiBatch() });
  assert.ok(html.includes("d".repeat(64)), "必须显示 generation batch id");
  assert.ok(html.includes(PARENT_CHECKSUM), "必须显示 exact parent pin");
  assert.ok(html.includes("1".repeat(64)), "必须显示 search-space fingerprint");
  assert.ok(html.includes("2".repeat(64)), "必须显示 generation input fingerprint");
  assert.ok(html.includes("ai_research_run:7"), "必须显示 exact research run");
  assert.ok(html.includes("hyp_1"), "必须显示 hypothesis id");
  assert.ok(html.includes("3".repeat(64)), "必须显示 exact research record hash");
  assert.ok(html.includes("e".repeat(64)) && html.includes("f".repeat(64)),
    "必须显示全部 candidate id");
});

test("R35C-C2：AI 面板不得出现推荐 / 评分 / 上线语言", () => {
  const html = wb.wbAiCandidateHtml("ai_parent", { batch: aiBatch() });
  for (const forbidden of ["推荐策略", "最佳策略", "最优参数", "预计收益", "AI Score",
                           "score", "Sharpe", "sharpe", "一键上线", "Promote",
                           "promote", "Deploy", "deploy", "winner", "排名"]) {
    assert.ok(!html.includes(forbidden), `AI 面板不得渲染 ${forbidden}`);
  }
  assert.ok(html.includes("本页不评估候选、不排序、不推荐"),
    "必须明确说明结论不在前端产生");
});

test("R35C-C3：未发起生成时面板明说，不编造结果", () => {
  const html = wb.wbAiCandidateHtml("ai_parent", null);
  assert.ok(html.includes("尚未发起生成"));
  assert.ok(!html.includes("candidate_count"));
});

test("R35C-C4：AI 生成请求只带 exact pin 与 run，不替后端制造 asof/universe/regime", () => {
  const start = source.indexOf("export async function wbAiGenerateCandidates");
  const end = source.indexOf("export async function wbOpenDetail", start);
  assert.ok(start > 0 && end > start, "AI 生成入口必须存在");
  const section = source.slice(start, end);
  // 必须显式传 exact version + checksum + exact run id。
  assert.ok(section.includes("strategy_version:pinned.strategy_version"));
  assert.ok(section.includes("strategy_checksum:pinned.strategy_checksum"));
  assert.ok(section.includes("research_run_id:researchRunId"));
  // 不得由前端填 as-of / universe / regime：那些来自 research run 与 exact parent。
  assert.ok(!section.includes("asof:"), "前端不得制造业务日事实");
  assert.ok(!section.includes("universe_spec:"), "universe 不归前端");
  assert.ok(!section.includes("intended_market_regime:"), "regime 不归前端");
  // business 逻辑不得出现在前端：AI cap 由后端裁决，前端只提交。
  assert.ok(!/max_candidates\s*>\s*32/.test(section),
    "上限判定归后端，前端不得复制业务规则");
});

test("R35C-C6：AI 批次必须绑定到它自己的策略，绝不跨策略显示", () => {
  // A 生成的 batch 不得在 B 的详情页出现：那是把 A 的台账事实冒充成 B 的。
  const start = source.indexOf("export async function wbOpenDetail");
  const section = source.slice(start, source.indexOf("wbShowView('detail')", start));
  assert.ok(section.includes("aiView.strategy_id!==strategyId"),
    "打开别的策略时必须丢弃不属于它的 AI 批次");
  // 生成时必须记录归属策略。
  const genStart = source.indexOf("export async function wbAiGenerateCandidates");
  const gen = source.slice(genStart, source.indexOf("export async function wbOpenDetail", genStart));
  assert.ok(gen.includes("strategy_id:strategyId"), "必须记录该批次属于哪条策略");
  // 渲染调用必须用过滤后的 view，而不是全局状态。
  assert.ok(section.includes("wbAiCandidateHtml(strategyId,aiView)"),
    "必须渲染过滤后的 view");
  assert.ok(!section.includes("wbAiCandidateHtml(strategyId,WB_STATE.aiCandidateView)"),
    "不得直接渲染全局 AI 状态");
});
test("R35C-C5：AI 面板不自动触发（无 scheduler / cron / 自动批量）", () => {
  const start = source.indexOf("/* R35-C：AI candidate generation 面板");
  const end = source.indexOf("export function wbCandidatesHtml", start);
  const section = source.slice(start, end);
  // 去掉开头那段块注释后只在**代码**里找自动触发机制；注释里说明"没有 cron"是允许的。
  const close = section.indexOf("*/");
  const code = close >= 0 ? section.slice(close + 2) : section;
  for (const forbidden of ["setInterval", "setTimeout", "cron", "scheduler",
                           "autoGenerate"]) {
    assert.ok(!code.includes(forbidden), `AI 面板不得自动触发：${forbidden}`);
  }
  assert.ok(section.includes("onclick"), "必须由人显式点击触发");
});

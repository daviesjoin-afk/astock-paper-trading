// 第二轮：inline handler 参数注入面 + renderAdaptive 系统性转义。
// 断言的是"生成的 HTML/属性本身安全"，不是"调用方记得先转义"。
import test from 'node:test';
import assert from 'node:assert/strict';
import { readFile } from 'node:fs/promises';
import {
  adaptiveCssToken,
  adaptiveTimelineHtml,
  adaptiveValidationRunsHtml,
  renderAdaptive,
} from '../src/features/adaptive.js';
import { allocationActionPanel } from '../src/features/execution.js';
import { adaptiveJsArg } from '../src/core/format.js';

const HOSTILE = '"><img src=x onerror=alert(1)>\'';
const HOSTILE_ID = "x');alert(1)//";

function attr(html, name) {
  const match = html.match(new RegExp('onclick="' + name + '\\(([^"]*)\\)"'));
  assert.ok(match, `没有找到 ${name} 的 onclick 属性`);
  return match[1];
}

test('adaptiveCssToken 只放行 [A-Za-z0-9_-]，其余退化为空/回退值', () => {
  assert.equal(adaptiveCssToken('tq_breakout'), 'tq_breakout');
  assert.equal(adaptiveCssToken('risk-card-2'), 'risk-card-2');
  assert.equal(adaptiveCssToken('bad id" onmouseover="alert(1)'), '');
  assert.equal(adaptiveCssToken('"><script>'), '');
  assert.equal(adaptiveCssToken(null), '');
  assert.equal(adaptiveCssToken(undefined), '');
  assert.equal(adaptiveCssToken('"><script>', 'partial'), 'partial');
});

test('R29 run_key 进入 onclick 时是 JS 字面量而不是 HTML 实体', () => {
  const html = adaptiveValidationRunsHtml({
    status: 'ok',
    runs: [{
      run_key: HOSTILE_ID, strategy_id: '<img>', strategy_version: 1,
      experiment_fingerprint: '<svg>', validation_status: '<b>', created_at: '<i>',
      result: { status: '<script>' },
    }],
  });
  assert.equal(attr(html, 'openAdaptiveValidationRun'), adaptiveJsArg(HOSTILE_ID));
  assert.equal(attr(html, 'compareAdaptiveValidationRun'), adaptiveJsArg(HOSTILE_ID));
  assert.equal(eval(attr(html, 'openAdaptiveValidationRun')), HOSTILE_ID); // eslint-disable-line no-eval
  // 属性值里只有两个字面量定界单引号；`'` 本身已被转成 \u0027，无法闭合字符串。
  assert.equal((attr(html, 'openAdaptiveValidationRun').match(/'/g) || []).length, 2);
  assert.doesNotMatch(html, /&#39;/);
  assert.doesNotMatch(html, /<img|<svg|<script|<i>/);
});

test('时间线重试按钮的窗口名不会被引号逃逸', () => {
  const html = adaptiveTimelineHtml({
    status: 'ok',
    windows: [{ window: HOSTILE_ID, status: 'failed', started_at: '2026-01-01T00:00:00' }],
  });
  assert.equal(attr(html, 'retryAdaptiveAiWindow'), adaptiveJsArg(HOSTILE_ID));
  assert.equal(eval(attr(html, 'retryAdaptiveAiWindow')), HOSTILE_ID); // eslint-disable-line no-eval
});

test('资金分摊回滚按钮的账户 id 不会被引号逃逸', () => {
  const html = allocationActionPanel({
    allocation: {
      active: { [HOSTILE_ID]: { weight_pct: 50, decision_id: '<img>', effective_date: '<b>' } },
      latest_decision: { can_apply: false },
    },
  });
  assert.equal(attr(html, 'rollbackAdaptiveAllocation'), adaptiveJsArg(HOSTILE_ID));
  assert.equal(eval(attr(html, 'rollbackAdaptiveAllocation')), HOSTILE_ID); // eslint-disable-line no-eval
  assert.doesNotMatch(html, /<img|<script/);
});

function installRenderDom(target) {
  const previousDocument = globalThis.document;
  const previousWindow = globalThis.window;
  const previousFetch = globalThis.fetch;
  const nodes = new Map();
  const node = (id) => {
    if (id === 'adaptiveResult') return target;
    if (!nodes.has(id)) nodes.set(id, { innerHTML: '', textContent: '', className: '', value: '' });
    return nodes.get(id);
  };
  globalThis.document = {
    getElementById: node,
    querySelector: () => null,
    querySelectorAll: () => [],
    body: { appendChild() {}, insertBefore() {} },
  };
  globalThis.window = { _adaptiveOverviewPayload: null };
  globalThis.fetch = async (path) => {
    const url = String(path);
    if (url.includes('/research/runs')) return { ok: true, status: 200, json: async () => ({ status: 'ok', runs: [] }) };
    if (url.includes('/experiments/runs')) return { ok: true, status: 200, json: async () => ({ status: 'ok', runs: [] }) };
    return { ok: true, status: 200, json: async () => ({ status: 'unavailable' }) };
  };
  return () => {
    globalThis.document = previousDocument;
    globalThis.window = previousWindow;
    globalThis.fetch = previousFetch;
  };
}

const tick = () => new Promise((resolve) => setImmediate(resolve));

function hostileOverview() {
  return {
    engine: { mode_label: '影子学习', stage: 'shadow', principle: '<img>', stage_label: '<b>', mature_reward_count: 3 },
    market_profile: {
      profile_date: '<img>', quality: '<svg>', regime: 'balanced',
      features: {
        top_sectors: [{ name: '<img>', sample_count: '<b>', median_pct: 1, main_net_yi: 2 }],
        weak_sectors: [],
      },
      drivers: [{ name: '<img>', detail: '<svg>', score: 1 }],
    },
    decision: {
      weights: { [HOSTILE_ID]: 100 },
      evidence: { strategies: { [HOSTILE_ID]: { name: '<img>' } } },
    },
    guardrails: [{ name: '<img>', detail: '<svg>', status: '"><script>' }],
    runs: [{ status: 'completed', trigger: '<img>', finished_at: '2026-01-01T00:00:00', new_rewards: 1,
      detail: { regime: '<svg>', alpha_lab_status: '<b>' } }],
    horizon_summary: [{ account_id: 'a', name: '<img>', horizon: 1, samples: 3, mean_excess_pct: 1 }],
    alpha_lab: { architecture: '<img>', candidates: [{ genome: { '<img>': 1 }, status: '<b>' }],
      profile_days: 1, mature_rows: 1, required_profile_days: 1, required_mature_rows: 1 },
    risk_optimizer: {
      candidates: [{ id: 1, status: 'human_review_required', account_name: '<img>', reason: '<svg>',
        evidence: { evolution_tier: '<b>', gates: {} }, baseline_params: {}, candidate_params: {} }],
      active_versions: [{ account_name: '<img>', account_id: HOSTILE_ID, version: 'risk-evo-20260101-1',
        created_at: '<svg>', meta: { approved_by: '<img>', candidate_id: '<img>' } }],
    },
    selection_optimizer: {
      candidates: [{ id: 2, status: 'human_review_required', account_name: '<img>', model_id: '<b>',
        reason: '<svg>', tier: '<i>', evidence: { gates: {} }, baseline_params: {},
        candidate_params: { weights: { '<img>': 0.2 } } }],
      active_versions: [{ account_name: '<img>', account_id: HOSTILE_ID, version: 'sel-evo-20260101-2',
        created_at: '<img>', meta: { tier: '<b>' } }],
    },
    deepseek_advisor: { enabled: false, tasks: [{ purpose: HOSTILE_ID, label: '<img>', short: '<svg>' }] },
    neural_control: {},
    portfolio_shadow: {},
    data_inputs: { categories: [{ id: '<img>', name: '<img>', status: '"><script>', detail: '<b>', sources: ['<img>'] }] },
    news_learning: {
      events: [{ evidence_grade: '"><script>', title: '<img>', code: '<b>', event_type: '<svg>',
        source_url: 'javascript:alert(1)' }],
      sources: [], totals: {}, collection_policy: { times: ['<img>'] }, pool: { counts: {} }, major_radar: {},
    },
    trade_attribution: {},
    closed_loop: {},
    dynamic_risk: {},
    risk_closure: { deployments: [{ status: '"><script>', account_name: '<img>', risk_version: '<b>', reason: '<svg>' }] },
    allocation: { active: { [HOSTILE_ID]: { weight_pct: 50, decision_id: '<img>', effective_date: '<b>' } },
      latest_decision: { can_apply: false } },
    validation: {},
    data_note: '<img>',
  };
}

test('renderAdaptive 全量转义：恶意后端字段不产生裸标签/属性', async () => {
  const target = { innerHTML: '' };
  const restore = installRenderDom(target);
  try {
    renderAdaptive(hostileOverview());
    await tick();
  } finally {
    restore();
  }
  const html = target.innerHTML;
  assert.ok(html.length > 1000, '应该渲染出完整页面');
  // 只允许模板自身的标签：任何后端字符串都不得变成新的标签/属性。
  for (const pattern of [/<img/i, /<svg/i, /<script/i, /<iframe/i, /javascript:/i]) {
    assert.doesNotMatch(html, pattern);
  }
  assert.doesNotMatch(html, /NaN/);
  // 进 class 的标识符被白名单化：坏 id 不追加到 class 里，也不另开属性。
  assert.match(html, /class="adaptive-strategy-card"/);
  assert.match(html, /class="adaptive-guard-state "/);
  assert.match(html, /class="adaptive-risk-card [A-Za-z0-9_-]+"/);
  assert.match(html, /class="adaptive-selection-card [A-Za-z0-9_-]+"/);
  assert.match(html, /class="data-input-card partial"/);
  assert.match(html, /grade-d/);
  assert.doesNotMatch(html, /onmouseover=/);
});

test('renderAdaptive 的 inline handler 参数使用 adaptiveJsArg', async () => {
  const target = { innerHTML: '' };
  const restore = installRenderDom(target);
  try {
    renderAdaptive(hostileOverview());
    await tick();
  } finally {
    restore();
  }
  const html = target.innerHTML;
  assert.equal(attr(html, 'rollbackAdaptiveRisk'), adaptiveJsArg(HOSTILE_ID));
  assert.equal(attr(html, 'rollbackAdaptiveSelection'), adaptiveJsArg(HOSTILE_ID));
  assert.ok(html.includes('recordAdaptiveFeedback(' + adaptiveJsArg(HOSTILE_ID) + ",'approve')"));
  assert.ok(html.includes('runAdaptiveResearchTask(' + adaptiveJsArg(HOSTILE_ID) + ',this)'));
});

test('源码守卫：inline handler 参数不再用 adaptiveEsc 拼 JS 字符串', async () => {
  const adaptive = await readFile(new URL('../src/features/adaptive.js', import.meta.url), 'utf8');
  const execution = await readFile(new URL('../src/features/execution.js', import.meta.url), 'utf8');
  for (const source of [adaptive, execution]) {
    assert.doesNotMatch(source, /\\''\+adaptiveEsc\(/);
    assert.doesNotMatch(source, /rollbackAdaptive\w*\(\\''/);
  }
  assert.match(adaptive, /accountArg=adaptiveJsArg\(x\.account_id\)/);
  assert.match(adaptive, /runArg=adaptiveJsArg\(run\.run_key\)/);
  assert.match(adaptive, /decision_id='\+encodeURIComponent\(/);
});

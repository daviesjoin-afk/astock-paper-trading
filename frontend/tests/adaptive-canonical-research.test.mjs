import test from 'node:test';
import assert from 'node:assert/strict';
import { readFile } from 'node:fs/promises';
import {
  adaptiveResearchConfidencePercent,
  adaptiveResearchDetailHtml,
  adaptiveResearchEvidenceHtml,
  adaptiveResearchHistoryHtml,
  adaptiveResearchStatusLabel,
  adaptiveValidationDetailHtml,
  adaptiveValidationRunsHtml,
  openAdaptiveResearchRun,
  refreshAdaptiveResearchHistory,
} from '../src/features/adaptive.js';

const run = {
  id: 12,
  purpose: 'candidate_challenge',
  trigger: 'manual-ui',
  hypothesis_id: 'H-12',
  as_of: '2026-08-27',
  subject: '600000',
  status: 'supported',
  reason: null,
  confidence: 0.8,
  authority: 'research',
  is_authoritative: false,
  provider_slot: 'ai2',
  provider_model: 'test-model',
  hypothesis: {
    thesis: '研究假设文本',
    evidence: [{
      relation: 'supports',
      source_type: 'market_data',
      source_id: 'snapshot:600000',
      as_of: '2026-08-27T10:00:00+08:00',
      verification: 'verified',
      verification_method: 'coverage_integrity',
      cross_source_verified: false,
      is_verified: true,
      verification_attributes: { source_count: 2 },
    }],
  },
  narrative: '历史研究叙述',
  counter_arguments: ['历史反方论据'],
  created_at: '2026-08-27T11:00:00+08:00',
};

test('research status is labeled as a hypothesis, never as approval', () => {
  assert.equal(adaptiveResearchStatusLabel('supported'), '支持（研究假设）');
  assert.equal(adaptiveResearchStatusLabel('unsupported'), '不支持（研究假设）');
  assert.equal(adaptiveResearchStatusLabel('insufficient_evidence'), '证据不足（研究假设）');
});

test('R29 validation view renders periods, windows, per-owner coverage and warnings without zero-filling', () => {
  const html = adaptiveValidationDetailHtml({
    run_key: 'a'.repeat(64), experiment_fingerprint: 'b'.repeat(64),
    strategy_id: 'trend', strategy_version: 3, strategy_checksum: 'c'.repeat(64),
    runner_code_revision: 'a'.repeat(40),
    dataset_fingerprint: 'd'.repeat(64), market_archive_fingerprint: 'e'.repeat(64),
    financial_archive_fingerprint: '2'.repeat(64),
    universe_archive_fingerprint: 'f'.repeat(64), calendar_fingerprint: '1'.repeat(64),
    validation_evidence: { status: 'blocked', reason_codes: ['historical_market_data_unavailable'],
      pit_warnings: ['market gap'], periods: {train:{start:'2026-01-01',end:'2026-01-05'},
        validation:{start:'2026-01-06',end:'2026-01-07'},oos:{start:'2026-01-08',end:'2026-01-09'}},
      data_coverage: {session_calendar:{ratio:1,requested:3}, universe:{ratio:1,requested:3},
        tradability:{ratio:null,requested:3,unknown:3}, market_data:{ratio:null},
        fundamental:{ratio:null}, labels:{ratio:0.5,requested:4}} },
    result: {status:'unavailable', failure_reason:'historical_market_data_unavailable', metrics:{}},
    folds: [{fold_id:1,status:'ready',reason:'ok',train_period:{start:'2026-01-01',end:'2026-01-05'},
      validation_period:{start:'2026-01-06',end:'2026-01-07'},oos_period:{start:'2026-01-08',end:'2026-01-09'}}],
  });
  for (const expected of ['2026-01-01', '2026-01-07', '2026-01-09', 'historical_market_data_unavailable',
    'Walk-forward windows', '交易日历', '可交易性', 'Code revision', 'a'.repeat(40), 'Financial archive', '2'.repeat(64), 'PIT warnings', '不可用']) assert.match(html, new RegExp(expected));
  assert.doesNotMatch(html, /收益<\/dt><dd>0(?:\.0+)?<\/dd>/);
  assert.doesNotMatch(html, /winner|champion|promot|可上线/i);
});

test('R29 run list offers detail and neutral comparison without ranking language', () => {
  const html = adaptiveValidationRunsHtml({status:'ok',runs:[{
    run_key:'a'.repeat(64), strategy_id:'trend', strategy_version:2,
    experiment_fingerprint:'b'.repeat(64), validation_status:'blocked', created_at:'2026-09-28',
    result:{status:'unavailable'},
  }]});
  assert.match(html, /查看 Experiment \/ Evidence \/ Result/);
  assert.match(html, /加入比较/);
  assert.doesNotMatch(html, /winner|best strategy|冠军|晋级/);
});

test('confidence conversion happens only in presentation and preserves the 0..1 contract', () => {
  assert.equal(adaptiveResearchConfidencePercent(0.8), '80%');
  assert.equal(adaptiveResearchConfidencePercent(0), '0%');
  assert.equal(adaptiveResearchConfidencePercent(1), '100%');
  assert.equal(adaptiveResearchConfidencePercent(80), '—');
  assert.equal(adaptiveResearchConfidencePercent(null), '—');
});

test('canonical history and detail render saved fields without legacy advisor shape', () => {
  const html = adaptiveResearchHistoryHtml({ status: 'ok', runs: [run] });
  assert.match(html, /candidate_challenge/);
  assert.match(html, /支持（研究假设）/);
  assert.match(html, /run #12/);
  assert.match(html, /80%/);
  assert.doesNotMatch(html, /latest_by_purpose|advisorLatest|legacy_adaptive_advisor_runs/);

  const detail = adaptiveResearchDetailHtml({ status: 'ok', run });
  assert.match(detail, /这是历史研究产物/);
  assert.match(detail, /研究假设文本/);
  assert.match(detail, /历史研究叙述/);
  assert.match(detail, /历史反方论据/);
  assert.match(detail, /is_authoritative=false/);
  assert.match(detail, /verified/);
  assert.match(detail, /coverage_integrity/);
  assert.match(detail, /双源核验/);
  assert.match(detail, /未确认/);
  assert.doesNotMatch(detail, /AI 已验证|交易已确认|可以执行|真实性通过/);
});

test('flat canonical market evidence renders source and confirmed cross-source state', () => {
  const trueHtml = adaptiveResearchEvidenceHtml([{
    relation: 'supports', source_type: 'market_data', source_id: 'snapshot:600000',
    as_of: '2026-08-27T10:00:00+08:00', verification: 'verified',
    verification_method: 'coverage_integrity', cross_source_verified: true,
    is_verified: true, verification_attributes: { source_count: 2 },
  }]);
  assert.match(trueHtml, /market_data/);
  assert.match(trueHtml, /snapshot:600000/);
  assert.match(trueHtml, /双源核验<\/dt><dd>已确认/);
});

test('verified market evidence with cross_source_verified false remains unconfirmed', () => {
  const falseHtml = adaptiveResearchEvidenceHtml([{
    relation: 'supports', source_type: 'market_data', source_id: 'snapshot:000001',
    as_of: '2026-08-27T10:00:00+08:00', verification: 'verified',
    verification_method: 'coverage_integrity', cross_source_verified: false,
    is_verified: true, verification_attributes: { source_count: 1 },
  }]);
  assert.match(falseHtml, /来源核验状态<\/dt><dd>verified/);
  assert.match(falseHtml, /双源核验<\/dt><dd>未确认/);
});

test('non-market evidence labels the market-only cross-source dimension not applicable', () => {
  for (const sourceType of [
    'execution', 'portfolio_research', 'news', 'strategy_research', 'runtime_incident',
  ]) {
    const html = adaptiveResearchEvidenceHtml([{
      relation: 'supports', source_type: sourceType, source_id: `${sourceType}:1`,
      as_of: '2026-08-27T10:00:00+08:00', verification: 'verified',
      verification_method: null, cross_source_verified: false,
      is_verified: true, verification_attributes: {},
    }]);
    assert.match(html, new RegExp(sourceType));
    assert.match(html, /双源核验<\/dt><dd>不适用/);
    assert.doesNotMatch(html, /双源核验<\/dt><dd>未确认/);
  }
});

test('history and detail refresh use only canonical read endpoints', async () => {
  const nodes = new Map();
  const history = { innerHTML: '' };
  const detail = { innerHTML: '' };
  nodes.set('adaptiveCanonicalResearchHistory', history);
  nodes.set('adaptiveCanonicalResearchDetail', detail);
  const oldDocument = globalThis.document;
  const oldFetch = globalThis.fetch;
  const calls = [];
  globalThis.document = { getElementById: (id) => nodes.get(id) || null };
  globalThis.fetch = async (path) => {
    calls.push(String(path));
    const payload = String(path).includes('/runs/12')
      ? { status: 'ok', run }
      : { status: 'ok', runs: [run] };
    return { ok: true, status: 200, json: async () => payload };
  };
  try {
    await refreshAdaptiveResearchHistory();
    await openAdaptiveResearchRun(12);
  } finally {
    globalThis.document = oldDocument;
    globalThis.fetch = oldFetch;
  }
  assert.ok(calls.some((path) => path.startsWith('/api/adaptive/research/runs?limit=50')));
  assert.ok(calls.some((path) => path.startsWith('/api/adaptive/research/runs/12')));
  assert.match(history.innerHTML, /candidate_challenge/);
  assert.match(detail.innerHTML, /历史研究叙述/);
});

test('every adaptive rerender refreshes canonical history', async () => {
  const source = await readFile(new URL('../src/features/adaptive.js', import.meta.url), 'utf8');
  const render = source.split('export function renderAdaptive(')[1]
    .split('\nexport async function loadAdaptive(')[0];
  assert.match(render, /refreshAdaptiveResearchHistory\(\)/);
});

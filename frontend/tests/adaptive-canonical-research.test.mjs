import test from 'node:test';
import assert from 'node:assert/strict';
import { readFile } from 'node:fs/promises';
import {
  adaptiveResearchConfidencePercent,
  adaptiveResearchDetailHtml,
  adaptiveResearchEvidenceHtml,
  adaptiveResearchHistoryHtml,
  adaptiveResearchStatusLabel,
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
      evidence: {
        source_type: 'market_data',
        source_id: 'snapshot:600000',
        as_of: '2026-08-27T10:00:00+08:00',
        verification: 'verified',
        verification_method: 'coverage_integrity',
        cross_source_verified: false,
      },
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

test('cross-source label only says confirmed for the canonical true flag', () => {
  const falseHtml = adaptiveResearchEvidenceHtml([{
    evidence: { verification: 'verified', cross_source_verified: false },
  }]);
  const trueHtml = adaptiveResearchEvidenceHtml([{
    evidence: { verification: 'verified', cross_source_verified: true },
  }]);
  assert.match(falseHtml, /双源核验<\/dt><dd>未确认/);
  assert.match(trueHtml, /双源核验<\/dt><dd>已确认/);
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

test('task and suite actions refresh canonical history after execution', async () => {
  const source = await readFile(new URL('../src/features/adaptive.js', import.meta.url), 'utf8');
  const task = source.split('export async function runAdaptiveResearchTask(')[1]
    .split('export async function runAdaptiveResearchSuite(')[0];
  const suite = source.split('export async function runAdaptiveResearchSuite(')[1]
    .split('export async function recordAdaptiveFeedback(')[0];
  assert.match(task, /await refreshAdaptiveResearchHistory\(\)/);
  assert.match(suite, /await refreshAdaptiveResearchHistory\(\)/);
});

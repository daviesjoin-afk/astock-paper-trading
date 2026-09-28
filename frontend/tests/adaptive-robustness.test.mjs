import test from 'node:test';
import assert from 'node:assert/strict';
import { readFile } from 'node:fs/promises';
import { adaptiveRobustnessReportHtml } from '../src/features/adaptive.js';

const report = {
  report_fingerprint: 'report-fingerprint',
  plan_fingerprint: 'plan-fingerprint',
  baseline_run_key: 'canonical-run-key',
  baseline_experiment_fingerprint: 'experiment-fingerprint',
  baseline_identity: { market_archive_fingerprint: 'market-owner' },
  sensitivity_analysis: {
    cost: [{ parameters: { commission_multiplier: 2 }, metrics: { return: -0.1, cost: 12 } }],
    slippage: [{ parameters: { multiplier: 1.5 }, metrics: { return: -0.2, cost: 15 } }],
    liquidity: [{ parameters: { liquidity_multiplier: 0.5 }, metrics: { capacity_proxy: 0.25 } }],
  },
  cases: [
    { scenario: { category: 'cost', parameters: { commission_multiplier: 2 } },
      evidence: { status: 'available', regime_breakdown: { trend: { bull: { return: 0.1 } } } },
      result: { status: 'completed', metrics: { return: -0.1, trade_count: 3 }, baseline_delta: { return: -0.2 } } },
    { scenario: { category: 'data_missingness', parameters: { missing_fraction: 0.1 } },
      evidence: { status: 'unavailable', reason_code: 'pit_bar_missing', regime_breakdown: {} },
      result: { status: 'unavailable', reason_code: 'pit_bar_missing', metrics: null, baseline_delta: null } },
  ],
};

test('R30-75 renders exact baseline identity', () => {
  const html = adaptiveRobustnessReportHtml(report);
  assert.match(html, /canonical-run-key/);
  assert.match(html, /experiment-fingerprint/);
  assert.match(html, /market-owner/);
});

test('R30-76 renders scenario categories and parameters', () => {
  const html = adaptiveRobustnessReportHtml(report);
  assert.match(html, /data_missingness/);
  assert.match(html, /commission_multiplier/);
});

test('R30-77 renders regime breakdown labels', () => {
  assert.match(adaptiveRobustnessReportHtml(report), /bull/);
});

test('R30-78 renders cost and slippage sensitivity supplied by the report', () => {
  const html = adaptiveRobustnessReportHtml(report);
  assert.match(html, /commission_multiplier/);
  assert.match(html, /slippage/);
});

test('R30-79 renders liquidity sensitivity', () => {
  assert.match(adaptiveRobustnessReportHtml(report), /liquidity_multiplier/);
});

test('R30-80 renders data missingness cases', () => {
  assert.match(adaptiveRobustnessReportHtml(report), /missing_fraction/);
});

test('R30-81 renders PIT unavailable reason', () => {
  const html = adaptiveRobustnessReportHtml(report);
  assert.match(html, /unavailable/);
  assert.match(html, /pit_bar_missing/);
});

test('R30-82 null metrics stay unavailable instead of becoming zero', () => {
  const html = adaptiveRobustnessReportHtml(report);
  assert.match(html, /不可用/);
  assert.doesNotMatch(html, /trade_count<\/dt><dd>0<\/dd>/);
});

test('R30-83 report view does not render score or grade', () => {
  const html = adaptiveRobustnessReportHtml(report);
  assert.doesNotMatch(html, /单一评分|grade|score/i);
});

test('R30-84 report view does not render promotion actions', () => {
  assert.doesNotMatch(adaptiveRobustnessReportHtml(report), /promot|晋级|shadow/i);
});

test('R30-85 frontend renders backend metrics without performance arithmetic', async () => {
  const source = await readFile(new URL('../src/features/adaptive.js', import.meta.url), 'utf8');
  const start = source.indexOf('export function adaptiveRobustnessReportHtml');
  const end = source.indexOf('export function adaptiveRobustnessReportsHtml', start);
  const renderer = source.slice(start, end);
  assert.doesNotMatch(renderer, /Math\.|Number\(|parseFloat\(|\.reduce\(|baseline_delta\s*\[/);
  assert.match(renderer, /result\.baseline_delta/);
});

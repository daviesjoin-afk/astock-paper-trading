import test from 'node:test';
import assert from 'node:assert/strict';
import { renderDataValidity } from '../src/features/selection.js';

function elements() {
  return Object.fromEntries(['dataValidityStatus', 'dataValidityCards', 'dataValidityDetails', 'dataValidityCancel'].map(id => [id, {
    innerHTML: '', textContent: '', className: '', hidden: false, style: {},
  }]));
}

test('快照覆盖只渲染 authority 的 fresh_ratio，不按股票池总数重算', () => {
  const nodes = elements();
  globalThis.document = { getElementById: id => nodes[id] };
  try {
    renderDataValidity({
      coverage: { universe: 5557 },
      live_snapshot: {
        rows: 5300, valid_today_rows: 5000, fresh_ratio: 0.94,
        min_fresh_ratio: 0.9, saved_at: '2026-11-03T06:00:00+00:00',
      },
      market_data: { data_fact: { status: 'degraded', observed_at: '2026-11-03T14:00:00+08:00' } },
      factor_cache: {}, source_health: {}, auction_tracking: {},
    });
    assert.match(nodes.dataValidityCards.innerHTML, /94\.0%/);
    assert.match(nodes.dataValidityCards.innerHTML, /5000 \/ 5300 行/);
    assert.match(nodes.dataValidityCards.innerHTML, /观测时点 2026-11-03T14:00:00\+08:00/);
    assert.match(nodes.dataValidityCards.innerHTML, /覆盖门槛 90%/);
    assert.doesNotMatch(nodes.dataValidityCards.innerHTML, /90\.0%.*有效覆盖/);
    assert.equal(nodes.dataValidityDetails.hidden, false);
  } finally { delete globalThis.document; }
});

test('缺失覆盖率不是 0%，接口事实也不由前端补造', () => {
  const nodes = elements();
  globalThis.document = { getElementById: id => nodes[id] };
  try {
    renderDataValidity({ live_snapshot: { valid_today_rows: null }, coverage: {}, factor_cache: {} });
    assert.match(nodes.dataValidityCards.innerHTML, /无盘中快照/);
    assert.match(nodes.dataValidityCards.innerHTML, /行情事实：未知/);
    assert.doesNotMatch(nodes.dataValidityCards.innerHTML, /行情时效覆盖<\/small><b>0\.0%/);
  } finally { delete globalThis.document; }
});

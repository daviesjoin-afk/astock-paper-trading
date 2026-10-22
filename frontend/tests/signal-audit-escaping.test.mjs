import assert from 'node:assert/strict';
import test from 'node:test';

import { SIGNAL_AUDIT_COLUMNS, renderSignalAuditTable } from '../src/features/signal-audit-table.js';

/* 信号审计表（纸盘 → 活动）此前把外部行情源与后端投影里的自由文本裸拼进
   innerHTML：股票名、代码、账户名、模型名、时间戳、状态。这与 selection.js
   被修掉的是同一类注入面，只是这张表不在那一轮的写范围里。 */

function hostileSignal() {
  return {
    account_id: 'acc1',
    name: '<script>alert(1)</script>',
    code: '"><b>x</b>',
    status: 'unknown<script>',
    signal_date: '<date>',
    t_score: 1.5,
    payload: { decision: { entry_model: { name: '<model>' } } },
    audit: {
      factor_date: '<factor>',
      signal_quote_at: '<quote>',
      signal_quote_pct: 1.23,
      planned_review_date: '<planned>',
      execution_status: 'filled',
      executed_at: '<executed>',
      execution_quote_at: '<execquote>',
    },
    signal_decision: { outcome: 'blocked', reason: '<reason>', evidence: {} },
  };
}

test('信号审计表转义外部自由文本，不产生可执行标记', () => {
  const html = renderSignalAuditTable([hostileSignal()], { acc1: '<img src=x onerror=alert(1)>' });
  assert.doesNotMatch(html, /<script>/);
  assert.doesNotMatch(html, /<img/);
  assert.doesNotMatch(html, /<b>x<\/b>/);
  assert.doesNotMatch(html, /<model>/);
  assert.doesNotMatch(html, /<quote>/);
  assert.doesNotMatch(html, /<planned>/);
  assert.doesNotMatch(html, /<executed>/);
  assert.doesNotMatch(html, /<factor>/);
  assert.match(html, /&lt;script&gt;alert\(1\)&lt;\/script&gt;/);
  assert.match(html, /&lt;img src=x onerror=alert\(1\)&gt;/);
});

test('行情快照列用间隔点而不是历史编码损坏的 "?" 分隔符', () => {
  const html = renderSignalAuditTable([hostileSignal()], {});
  assert.match(html, /· \+1\.23%/);
  assert.doesNotMatch(html, / \? \+1\.23%/);
});

test('表头列数与常量一致，缺字段不产生 undefined', () => {
  const html = renderSignalAuditTable([{ status: 'blocked' }], {});
  assert.equal((html.match(/<th>/g) || []).length, SIGNAL_AUDIT_COLUMNS.length);
  assert.doesNotMatch(html, /undefined/);
  assert.doesNotThrow(() => renderSignalAuditTable(null, {}));
  assert.doesNotThrow(() => renderSignalAuditTable([], {}));
});

test('自由文本只转义一次（不出现 &amp;amp; 双重转义）', () => {
  const signal = hostileSignal();
  signal.signal_decision = { outcome: 'blocked', reason: 'A&B <未成交>', evidence: {} };
  const html = renderSignalAuditTable([signal], {});
  assert.match(html, /A&amp;B/);
  assert.doesNotMatch(html, /&amp;amp;/);
  assert.doesNotMatch(html, /&lt;未成交&gt;.*&lt;未成交&gt;/);
});

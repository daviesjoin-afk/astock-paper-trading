// 自动采用授权面板：默认关闭、动态值必须转义、写操作必须先确认。
import test from 'node:test';
import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';

import { renderAiCapabilitySection } from '../src/features/ai-capability.js';

const source = readFileSync(new URL('../src/features/ai-capability.js', import.meta.url), 'utf8');

test('没有凭据时显示 ADVISORY 且明说不会自动采用', () => {
  const html = renderAiCapabilitySection({ current: { mode: 'ADVISORY' }, capabilities: [] });
  assert.match(html, /ADVISORY/);
  assert.match(html, /不会自动采用/);
  assert.match(html, /ai-capability-empty/);
});

test('签发凭据不是一键开关：要填全部字段并二次确认', () => {
  const html = renderAiCapabilitySection({ current: { mode: 'ADVISORY' }, capabilities: [] });
  for (const id of ['ai-capability-id', 'ai-capability-strategies', 'ai-capability-fields',
    'ai-capability-per-apply', 'ai-capability-per-day', 'ai-capability-cost',
    'ai-capability-expires', 'ai-capability-operator']) {
    assert.ok(html.includes('data-testid="' + id + '"'), 'missing field ' + id);
  }
  assert.match(source, /settingsConfirm/);
});

test('已签发的凭据逐条列出并带撤销入口', () => {
  const html = renderAiCapabilitySection({
    current: { mode: 'ADVISORY' },
    capabilities: [{
      capability_id: 'cap-1', mode: 'BOUNDED_AUTO', expires_at: '2026-11-26T02:00:00+00:00',
      revocation_epoch: 3, scope: { strategy_ids: ['tq_breakout'], fields: ['first_board_bonus'] },
      limits: { max_change_per_day: 0.1 },
    }],
  });
  assert.match(html, /cap-1/);
  assert.match(html, /ai-capability-revoke-cap-1/);
  assert.match(html, /\b3\b/);
});

test('凭据里的动态值会被转义，不会变成可执行标记', () => {
  const html = renderAiCapabilitySection({
    current: { mode: 'ADVISORY' },
    capabilities: [{
      capability_id: '<img src=x onerror=alert(1)>', mode: 'BOUNDED_AUTO',
      expires_at: 'x', revocation_epoch: 1,
      scope: { strategy_ids: ['<script>bad()</script>'], fields: [] },
      limits: {},
    }],
  });
  assert.ok(!html.includes('<img src=x'));
  assert.ok(!html.includes('<script>bad()'));
  assert.match(html, /&lt;img/);
});

test('写接口都走 confirmed=true，并且都需要操作人', () => {
  const calls = source.match(/await api\(/g) || [];
  assert.ok(calls.length >= 3, 'there should be list + issue + revoke calls');
  assert.match(source, /capabilities\?confirmed=true/);
  assert.match(source, /revoke\?confirmed=true/);
  assert.match(source, /撤销需要填写操作人/);
});

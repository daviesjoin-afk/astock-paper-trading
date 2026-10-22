import assert from 'node:assert/strict';
import test from 'node:test';

import { renderSettings } from '../src/features/settings.js';

/* 设置中心的 AI 槽位卡片把后端 `review.slot_order` 的槽位 id 拼进
   data-testid / id / for / option value 等属性。槽位 id 名义上是 ai1/ai2，
   但实际由后端下发；属性值里的引号可以另开属性（与 inline handler 的 JS
   参数注入是不同上下文，adaptiveJsArg 不适用）。 */

function renderWithSlots(slotOrder) {
  const target = { innerHTML: '' };
  const other = () => ({ innerHTML: '', style: {}, checked: false, value: '' });
  const previousDocument = globalThis.document;
  const previousWindow = globalThis.window;
  globalThis.document = {
    getElementById: (id) => (id === 'settingsResult' ? target : other()),
    querySelector: () => other(),
    querySelectorAll: () => [],
  };
  globalThis.window = { _settingsSection: 'evolution' };
  try {
    renderSettings({
      settings: { simulation: {}, risk: {}, strategy: {}, evolution: {}, execution: {} },
      defaults: {},
      effective: { current_cycle: {} },
      ai: { settings: {}, review: { slot_order: slotOrder, slots: {} } },
    });
  } finally {
    globalThis.document = previousDocument;
    globalThis.window = previousWindow;
  }
  return target.innerHTML;
}

test('AI 槽位 id 进属性前被转义，不能另开属性或闭合标签', () => {
  const html = renderWithSlots(['ai1', 'bad"><img src=x onerror=alert(1)>']);
  assert.doesNotMatch(html, /<img/);
  assert.match(html, /ai-slot-bad&quot;&gt;&lt;img/);
  // 正常槽位仍然生成 e2e 依赖的 testid
  assert.match(html, /data-testid="ai-slot-ai1"/);
  assert.match(html, /data-testid="ai-slot-ai1-save"/);
});

test('单 AI 槽位下拉框的 option value 同样被转义', () => {
  const html = renderWithSlots(['ai1', 'x"><script>alert(1)</script>']);
  assert.doesNotMatch(html, /<script>/);
  assert.match(html, /<option value="x&quot;&gt;&lt;script&gt;/);
});

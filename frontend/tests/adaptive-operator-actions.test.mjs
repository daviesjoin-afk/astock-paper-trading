import test from 'node:test';
import assert from 'node:assert/strict';
import { ADAPTIVE_OPERATOR_ACTIONS, installAdaptiveOperatorActions } from '../src/features/adaptive.js';

/* 资金分摊/调参的四个写操作由自适应页自己持有（容器事件委托），不再经 window
   全局 + 启动文件承接。这里锁住三件事：动作闭集、确认文案在请求之前、URL 归属。 */

const EXPECTED = {
  'apply-allocation': '/api/adaptive/allocation/apply',
  'rollback-allocation': '/api/adaptive/allocation/rollback',
  'apply-tuner': '/api/adaptive/tuner/apply',
  'rollback-tuner': '/api/adaptive/tuner/rollback',
};

test('四个动作的 URL 归属与人工确认标记保持原样', () => {
  assert.deepEqual(Object.keys(ADAPTIVE_OPERATOR_ACTIONS).sort(), Object.keys(EXPECTED).sort());
  for (const [action, prefix] of Object.entries(EXPECTED)) {
    const url = ADAPTIVE_OPERATOR_ACTIONS[action].url('7');
    assert.ok(url.startsWith(prefix), `${action}: ${url}`);
    assert.match(url, /confirmed=true/);
  }
  assert.match(ADAPTIVE_OPERATOR_ACTIONS['apply-allocation'].url('7'), /approved_by=human-ui/);
  assert.match(ADAPTIVE_OPERATOR_ACTIONS['apply-tuner'].url('7'), /approved_by=human-ui/);
});

test('每个动作都先要求人工确认，参数进确认文案', () => {
  for (const action of Object.keys(EXPECTED)) {
    const prompt = ADAPTIVE_OPERATOR_ACTIONS[action].confirm('tq_breakout');
    assert.match(prompt, /^确认回滚|^确认把/, action);
    assert.match(prompt, /tq_breakout/, action);
  }
});

test('参数的编码不走字符串拼接漏洞', () => {
  const url = ADAPTIVE_OPERATOR_ACTIONS['rollback-tuner'].url('a b&confirmed=false');
  assert.ok(url.includes(encodeURIComponent('a b&confirmed=false')));
  assert.equal(url.match(/confirmed=/g).length, 1);
});

test('没有可监听容器时明确返回 false，不静默成功', () => {
  assert.equal(installAdaptiveOperatorActions(undefined), false);
  assert.equal(installAdaptiveOperatorActions({}), false);
});

test('委托点击：先人工确认，确认后才发写请求；取消则什么也不发', async () => {
  const listeners = {};
  const container = {addEventListener: (type, handler) => { listeners[type] = handler; }};
  assert.equal(installAdaptiveOperatorActions(container), true);

  const previousWindow = globalThis.window;
  const previousFetch = globalThis.fetch;
  const previousDocument = globalThis.document;
  const requested = [];
  const button = {dataset: {adaptiveAction: 'apply-tuner', adaptiveArg: '9'}};
  const event = {
    target: {closest: selector => (selector === '[data-adaptive-action]' ? button : null)},
    preventDefault() {},
  };
  try {
    globalThis.document = {getElementById: () => null};
    globalThis.fetch = async url => {
      requested.push(String(url));
      return {ok: true, status: 200, json: async () => ({})};
    };

    globalThis.window = {confirm: () => false};
    await listeners.click(event);
    assert.deepEqual(requested, [], '取消后不得发出写请求');

    let confirmed = '';
    globalThis.window = {confirm: message => { confirmed = message; return true; }};
    await listeners.click(event);
    assert.match(confirmed, /双AI共识运行 #9/);
    // 第一个请求必须是那次写操作；其后的读刷新（loadEvolutionStatus）可以再有。
    assert.match(requested[0], /\/api\/adaptive\/tuner\/apply\?run_id=9/);
    assert.equal(requested.filter(url => url.includes('/api/adaptive/tuner/apply')).length, 1);
    // after() 的读刷新是异步的：等它落定再恢复全局桩，否则会在测试结束后才去读 document。
    await new Promise(resolve => setTimeout(resolve, 0));
  } finally {
    globalThis.window = previousWindow;
    globalThis.fetch = previousFetch;
    globalThis.document = previousDocument;
  }
});

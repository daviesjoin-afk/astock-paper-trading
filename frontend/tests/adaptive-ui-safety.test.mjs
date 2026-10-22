// 进化中心 UI 安全 / 数据诚实性回归：转义、切页竞态、缺失字段不得折算成假值。
import test from 'node:test';
import assert from 'node:assert/strict';
import { readFile } from 'node:fs/promises';
import {
  adaptiveRatioPercent,
  adaptiveTimelineHtml,
  loadAdaptive,
  renderAdaptive,
  loadEvolutionStatus,
  testModlensRead,
} from '../src/features/adaptive.js';

function installDocument(getById) {
  const previous = globalThis.document;
  globalThis.document = { getElementById: getById };
  return () => { globalThis.document = previous; };
}

function installWindow(stub) {
  const previous = globalThis.window;
  globalThis.window = stub;
  return () => { globalThis.window = previous; };
}

function nodeMap() {
  const nodes = new Map();
  const get = (id) => {
    if (!nodes.has(id)) nodes.set(id, { innerHTML: '', textContent: '', value: '' });
    return nodes.get(id);
  };
  get.nodes = nodes;
  return get;
}

test('时间线空态消息按文本转义，后端返回的 HTML 不会进入 DOM', () => {
  const html = adaptiveTimelineHtml({ status: 'error', message: '<img src=x onerror="alert(1)">' });
  assert.doesNotMatch(html, /<img/i);
  assert.match(html, /&lt;img/);
});

test('页面未装载时 renderAdaptive 是安全的空操作', () => {
  const restoreDocument = installDocument(() => null);
  const restoreWindow = installWindow({});
  try {
    assert.doesNotThrow(() => renderAdaptive({ engine: { mode_label: '影子学习' } }));
  } finally {
    restoreWindow();
    restoreDocument();
  }
});

test('切页后异步概览不再写已卸载的 DOM，也不排空转重试', async () => {
  const scheduled = [];
  const cleared = [];
  const restoreWindow = installWindow({
    _adaptiveRetryTimer: 99,
    setTimeout: (fn) => { scheduled.push(fn); return 100; },
    clearTimeout: (id) => { cleared.push(id); },
  });
  const restoreDocument = installDocument(() => null);
  const previousFetch = globalThis.fetch;
  let fetches = 0;
  globalThis.fetch = async () => {
    fetches += 1;
    return { ok: true, status: 200, json: async () => ({}) };
  };
  try {
    await loadAdaptive();
  } finally {
    globalThis.fetch = previousFetch;
    restoreDocument();
    restoreWindow();
  }
  assert.equal(fetches, 1);
  assert.deepEqual(cleared, [99]);
  assert.equal(scheduled.length, 0);
});

test('快照未生成时的重试有上限，到上限明说而不是无限自旋', async () => {
  const scheduled = [];
  const target = { innerHTML: '' };
  const restoreWindow = installWindow({
    _adaptiveRetryTimer: null,
    setTimeout: (fn) => { scheduled.push(fn); return scheduled.length; },
    clearTimeout: () => {},
  });
  const restoreDocument = installDocument((id) => (id === 'adaptiveResult' ? target : null));
  const previousFetch = globalThis.fetch;
  globalThis.fetch = async () => ({ ok: true, status: 200, json: async () => ({}) });
  try {
    for (let i = 0; i < 21; i += 1) await loadAdaptive();
  } finally {
    globalThis.fetch = previousFetch;
    restoreDocument();
    restoreWindow();
  }
  assert.equal(scheduled.length, 20);
  assert.match(target.innerHTML, /仍未生成/);
});

test('缺失比率字段显示“—”，不出现“-%”或 NaN', async () => {
  assert.equal(adaptiveRatioPercent(undefined), '—');
  assert.equal(adaptiveRatioPercent(null), '—');
  assert.equal(adaptiveRatioPercent('n/a'), '—');
  assert.equal(adaptiveRatioPercent(0.6), '60.0%');
  assert.equal(adaptiveRatioPercent(0), '0.0%');

  const get = nodeMap();
  const restoreDocument = installDocument(get);
  const previousFetch = globalThis.fetch;
  globalThis.fetch = async (path) => {
    const url = String(path);
    if (url.startsWith('/api/adaptive/dual-ai/status')) {
      return { ok: true, status: 200, json: async () => ({ slots: {}, consensus_rules: {} }) };
    }
    if (url.startsWith('/api/adaptive/modlens/status')) {
      return { ok: true, status: 200, json: async () => ({ modlens_available: true }) };
    }
    if (url.startsWith('/api/adaptive/evolution/status')) {
      return { ok: true, status: 200, json: async () => ({ should_evolve: false, current_params: {}, pending_candidates: {}, performance_metrics: { has_data: true } }) };
    }
    if (url.startsWith('/api/adaptive/dual-ai/runs')) {
      return { ok: true, status: 200, json: async () => ({ runs: [] }) };
    }
    if (url.startsWith('/api/adaptive/evolution/log')) {
      return { ok: true, status: 200, json: async () => ({ log: [] }) };
    }
    return { ok: true, status: 200, json: async () => ({}) };
  };
  try {
    await loadEvolutionStatus();
  } finally {
    globalThis.fetch = previousFetch;
    restoreDocument();
  }
  assert.equal(get('aiConsensusRate').innerHTML, '—');
  const metrics = get('aiMetricsGrid').innerHTML;
  assert.doesNotMatch(metrics, /NaN|-%/);
  assert.match(metrics, /样本数<\/small><b>—<\/b>/);
});

test('modlens 结果缺失耗时显示“—”，不伪造 0ms；缺失成功标记不写成失败', async () => {  const get = nodeMap();
  get('aiImageUrl').value = 'https://example.com/a.png';
  const restoreDocument = installDocument(get);
  const previousFetch = globalThis.fetch;
  let payload = { success: true };
  globalThis.fetch = async () => ({ ok: true, status: 200, json: async () => payload });
  try {
    await testModlensRead();
    let html = get('aiModlensResult').innerHTML;
    assert.match(html, /耗时：<\/b>—/);
    assert.doesNotMatch(html, /0ms/);

    payload = { latency_ms: 12 };
    await testModlensRead();
    html = get('aiModlensResult').innerHTML;
    assert.match(html, /结果未标记/);
    assert.doesNotMatch(html, /状态：<\/b>失败/);
  } finally {
    globalThis.fetch = previousFetch;
    restoreDocument();
  }
});

test('adaptive.js 不再使用浏览器原生弹窗，统一走 ui/dialog.js', async () => {
  const source = await readFile(new URL('../src/features/adaptive.js', import.meta.url), 'utf8');
  assert.doesNotMatch(source, /window\.(confirm|alert|prompt)|\balert\(/);
  assert.match(source, /from "\.\.\/ui\/dialog\.js"/);
});


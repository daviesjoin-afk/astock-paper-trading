// 自进化时间线：字段契约、条数上限、位置与状态翻译。
import test from 'node:test';
import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';

import { shortStamp } from '../src/core/format.js';

const source = (relative) => readFileSync(new URL(relative, import.meta.url), 'utf8');
const adaptive = () => source('../src/features/adaptive.js');

test('shortStamp 去掉 ISO 时间戳里的 T', () => {
  assert.equal(shortStamp('2026-10-21T09:30:00+08:00'), '10-21 09:30');
  assert.equal(shortStamp('2026-10-21T09:30'), '10-21 09:30');
  assert.equal(shortStamp(''), '—');
  assert.equal(shortStamp(null), '—');
});

test('时间线读后端的 analysis_window 而不是不存在的 window 字段', () => {
  const text = adaptive();
  assert.ok(text.includes('row.analysis_window'), '未读取后端实际字段 analysis_window');
});

test('时间线状态覆盖后端会返回的枚举', () => {
  const text = adaptive();
  assert.ok(text.includes("killed:'已中断'"), 'killed 未翻译');
  assert.ok(text.includes("intraday_observation:'午间观测'"), 'intraday_observation 未翻译');
  assert.ok(text.includes("advisor_skipped:'AI 审阅跳过'"), 'advisor_skipped 未翻译');
});

test('已知英文失败原因有中文解释', () => {
  const text = adaptive();
  assert.ok(text.includes('进程未完成即被中断'), '缺少 killed 错误的中文解释');
  assert.ok(text.includes('错误码：'), '未知英文枚举缺少兜底前缀');
});

test('时间线只显示最近 10 条并提供滚动容器', () => {
  const text = adaptive();
  assert.ok(text.includes('items.slice(0,10)'), '未限制条数');
  assert.ok(text.includes('adaptive-ai-timeline-scroll'), '缺少滚动容器');
  assert.ok(text.includes('overflow-y:auto'), '缺少纵向滚动');
  assert.ok(text.includes('仅显示最近'), '未说明只显示部分条目');
});

test('时间线不再拿 learning runs 充当数据源', () => {
  const text = adaptive();
  assert.ok(!text.includes('(d.runs||[]).length?{runs:d.runs'),
    '仍在用自进化运行记录兜底，会让今日时间线显示历史条目');
});

test('时间线面板位于总览末尾而不是开头', () => {
  const text = adaptive();
  const panel = text.indexOf('+adaptiveTimelinePanel');
  const dataInput = text.indexOf('+dataInputPanel');
  const closedLoop = text.indexOf('+closedLoopPanel');
  assert.ok(panel > 0, '未找到时间线面板拼接');
  assert.ok(panel > dataInput && panel > closedLoop, '时间线仍在页面靠前位置');
});

test('设置中心状态不再直接印英文枚举', () => {
  const text = source('../src/features/settings.js');
  assert.ok(text.includes('CYCLE_STATUS_LABELS'), '缺少周期状态映射');
  assert.ok(!text.includes("adaptiveEsc(current.status||'—')"), '当前周期状态仍直出');
  assert.ok(!text.includes("adaptiveEsc(runtime.lifecycle_stage||'—')"), '生命周期阶段仍直出');
});

test('Base URL 的 placeholder 不再像一个真实地址', () => {
  const text = source('../src/features/settings.js');
  assert.ok(!text.includes('placeholder="https://example.com/v1"'), '示例地址仍会被误读为已配置');
  assert.ok(text.includes('留空表示未配置'), 'placeholder 未说明留空语义');
});

test('选股页人工更新状态已翻译', () => {
  const text = source('../src/features/selection.js');
  assert.ok(!text.includes("adaptiveEsc(u.status||'idle')"), '人工更新状态仍直出英文');
  assert.ok(text.includes("running:'运行中'"), '缺少运行状态映射');
});

// 前端格式化与转义契约：缺值不得被渲染成真实读数，动态字段必须转义。
import test from 'node:test';
import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';

import {
  adaptiveEsc, adaptiveValue, dataValidityTone, fmt, fmtPct, pctTxt, sellTag, sigTag,
} from '../src/core/format.js';
import { adaptiveValue as legacyAdaptiveValue } from '../src/features/adaptive.js';
import { allocationActionPanel } from '../src/features/execution.js';
import { paperRunningStrategyCard } from '../src/features/paper.js';

const source = (relative) => readFileSync(new URL(relative, import.meta.url), 'utf8');

test('策略运行卡片区分未知与已知的单日亏损', () => {
  const render = value => paperRunningStrategyCard('tq_breakout', {}, {}, {
    daily_loss_pct: value, drawdown_pct: 2,
  });
  assert.match(render(null), /单日亏损 \/ 回撤<b>— \/ 2\.0%<\/b>/);
  assert.match(render(1.25), /单日亏损 \/ 回撤<b>1\.3% \/ 2\.0%<\/b>/);
  assert.match(render(0), /单日亏损 \/ 回撤<b>0\.0% \/ 2\.0%<\/b>/);
});

test('提供商设置模块不再反向导入设置页面', () => {
  assert.doesNotMatch(source('../src/features/ai-provider-settings.js'), /from\s+["']\.\/settings\.js["']/);
});

test('adaptiveValue 保持缺值、精度、后缀和旧导出兼容', () => {
  assert.equal(legacyAdaptiveValue, adaptiveValue);
  assert.equal(adaptiveValue(null, '%'), '—');
  assert.equal(adaptiveValue(undefined, '%'), '—');
  assert.equal(adaptiveValue(0, '%'), '0.0%');
  assert.equal(adaptiveValue(1.2345, '', 3), '1.234');
  assert.equal(adaptiveValue('2.5', 'ms', 0), '3ms');
  assert.equal(adaptiveValue('', '%'), '-%');
  assert.equal(adaptiveValue(NaN, '%'), '-%');
});

test('分摊展示从公共格式模块取值，不再依赖研究页面', () => {
  const execution = source('../src/features/execution.js');
  assert.doesNotMatch(execution, /from\s+["']\.\/adaptive\.js["']/);
  const html = allocationActionPanel({allocation:{active:{alpha:{weight_pct:12.34}, beta:{weight_pct:null}}}});
  assert.match(html, /当前分摊 12\.3%/);
  assert.match(html, /当前分摊 —/);
});

test('fmtPct 把缺值渲染为占位符，而不是拼成 "-%"', () => {
  assert.equal(fmtPct(null), '—');
  assert.equal(fmtPct(undefined), '—');
  assert.equal(fmtPct(''), '—');
  assert.equal(fmtPct(NaN), '—');
  assert.equal(fmtPct(1.5), '1.50%');
  assert.equal(fmtPct(0), '0.00%');
});

test('fmt 不把空字符串当成 0', () => {
  assert.equal(fmt(''), '-');
  assert.equal(fmt(null), '-');
  assert.equal(fmt(0), '0.00');
});

test('pctTxt 缺值返回占位符而不是 0.00%', () => {
  assert.equal(pctTxt(''), '-');
  assert.equal(pctTxt(null), '-');
  assert.equal(pctTxt(2), '+2.00%');
});

test('dataValidityTone 缺值保持中性，不渲染成坏数据', () => {
  assert.equal(dataValidityTone(null, 98, 90), '');
  assert.equal(dataValidityTone('', 98, 90), '');
  assert.equal(dataValidityTone(99, 98, 90), 'good');
  assert.equal(dataValidityTone(95, 98, 90), 'warn');
  assert.equal(dataValidityTone(50, 98, 90), 'bad');
});

test('sigTag 与 sellTag 转义动态字段', () => {
  const tag = sigTag({ level: 'info', type: '<b>x</b>', msg: '" onmouseover="alert(1)' });
  assert.ok(!tag.includes('<b>x</b>'), '类型未转义');
  assert.ok(!tag.includes('" onmouseover="'), 'title 属性可被引号逃逸');
  assert.ok(tag.includes('&lt;b&gt;'));
  assert.ok(tag.includes('&quot;'));

  const sell = sellTag({ action: '<i>卖出</i>', auction_matrix: { level: 'sell', tier: 'A"' } });
  assert.ok(!sell.includes('<i>卖出</i>'));
  assert.ok(sell.includes('&lt;i&gt;'));
});

test('adaptiveEsc 覆盖五个 HTML 特殊字符', () => {
  assert.equal(adaptiveEsc(`&<>"'`), '&amp;&lt;&gt;&quot;&#39;');
});

test('选股与持仓表格不再直接拼接未转义的名称字段', () => {
  const selection = source('../src/features/selection.js');
  assert.ok(!selection.includes("'<b>'+p.name+'</b>'"), '选股表 p.name 未转义');
  assert.ok(!selection.includes("'<b>'+x.name+'</b>'"), '跟踪表 x.name 未转义');

  const paper = source('../src/features/paper.js');
  assert.ok(!paper.includes("'<b>'+p.name+'</b>'"), '持仓表 p.name 未转义');
  assert.ok(!paper.includes("'+r.report+'</pre>'"), '复盘报告未转义');
  assert.ok(!paper.includes("'+d.disclaimer+'</div>'"), '免责声明未转义');
});

test('设置中心包含推送分组入口', () => {
  const html = source('../index.html');
  assert.ok(html.includes('data-settings-section="notification"'), '缺少推送设置分组');
  assert.ok(html.includes('日报与周报推送'), '缺少推送分组文案');
});

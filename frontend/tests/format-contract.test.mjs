// 前端格式化与转义契约：缺值不得被渲染成真实读数，动态字段必须转义。
import test from 'node:test';
import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';

import {
  adaptiveEsc, dataValidityTone, fmt, fmtPct, pctTxt, sellTag, sigTag,
} from '../src/core/format.js';

const source = (relative) => readFileSync(new URL(relative, import.meta.url), 'utf8');

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

test('批量加入跟踪池的代码正则可以匹配六位数字', () => {
  // 曾经写成 /(\\d{6})/：那是字面反斜杠加 d，永远匹配不到任何代码。
  const text = source('../src/features/selection.js');
  assert.ok(!text.includes('match(/(\\\\d{6})/)'), '正则里仍存在字面双反斜杠');
  assert.ok(text.includes('match(/(\\d{6})/)'), '缺少正确的六位数字正则');
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

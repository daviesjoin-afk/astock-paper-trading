import assert from 'node:assert/strict';
import test from 'node:test';

import {
  adaptiveEsc,
  adaptiveJsArg,
  paperStatusTag,
  sellTag,
  sigTag,
} from '../src/core/format.js';

/* core/format.js 里这几个函数把后端字段拼进 innerHTML，所以它们自己就是
   转义边界。此文件锁两件事：
   1. 任何进入 innerHTML 的字符串都被转义（不是"调用方应该先转义"）；
   2. 可选投影缺失时函数本身不抛异常 —— 一行坏数据不能让整张表渲染失败。 */

test('sigTag 转义 title 与文本，缺字段不抛异常', () => {
  const html = sigTag({ level: 'sell', msg: '"><img src=x onerror=alert(1)>', type: '<b>止损</b>' });
  assert.doesNotMatch(html, /<img/);
  assert.doesNotMatch(html, /<b>/);
  assert.match(html, /&quot;&gt;&lt;img/);
  assert.match(html, /&lt;b&gt;止损&lt;\/b&gt;/);
  assert.match(html, /tag-warn/);

  // 历史/裁剪载荷可能只有部分字段。
  assert.doesNotThrow(() => sigTag({}));
  assert.doesNotThrow(() => sigTag(null));
  assert.match(sigTag({}), /title=""/);
});

test('sellTag 在 auction_matrix 缺失时不抛异常（旧实现整行渲染崩溃）', () => {
  assert.doesNotThrow(() => sellTag({ action: '卖出' }));
  assert.match(sellTag({ action: '卖出' }), /tag-warn/);
  assert.doesNotThrow(() => sellTag(null));
  assert.doesNotThrow(() => sellTag({ action: '止盈减仓', auction_matrix: {} }));
});

test('sellTag 转义 action 与 tier，并保留卖出/止盈的语义颜色', () => {
  const html = sellTag({
    action: '卖出<script>alert(1)</script>',
    auction_matrix: { level: 'sell', tier: 'Q1"><svg onload=alert(1)>' },
  });
  assert.doesNotMatch(html, /<script>/);
  assert.doesNotMatch(html, /<svg/);
  assert.match(html, /tag-warn/);

  assert.match(sellTag({ action: '卖出', auction_matrix: { level: 'buy', tier: 'Q2' } }), /tag-warn/);
  assert.match(sellTag({ action: '止盈减仓', auction_matrix: { level: 'buy', tier: 'Q2' } }), /tag-ok/);
  assert.match(sellTag({ action: '关注', auction_matrix: { level: 'buy', tier: 'Q2' } }), /tag-info/);
});

test('paperStatusTag 对已知状态输出稳定，对未知状态仍转义', () => {
  assert.equal(paperStatusTag('filled'), '<span class="tag tag-ok">已成交</span>');
  assert.equal(paperStatusTag('blocked'), '<span class="tag tag-warn">已拦截</span>');

  const html = paperStatusTag('custom"><script>alert(1)</script>');
  assert.doesNotMatch(html, /<script>/);
  assert.match(html, /&quot;&gt;&lt;script&gt;/);
  assert.match(paperStatusTag(null), /未知/);
});

test('adaptiveEsc 覆盖五个 HTML 元字符', () => {
  assert.equal(adaptiveEsc('&<>"\''), '&amp;&lt;&gt;&quot;&#39;');
  assert.equal(adaptiveEsc(null), '');
  assert.equal(adaptiveEsc(undefined), '');
});

test('adaptiveJsArg 产出双引号属性内不可逃逸的 JS 字符串字面量', () => {
  const hostile = [
    "a'); alert(1);//",
    'x"y',
    'back\\slash',
    'line\nbreak',
    '<img src=x onerror=alert(1)>',
    '&amp;',
    '\u2028\u2029',
    '正常 run_key',
  ];
  for (const value of hostile) {
    const literal = adaptiveJsArg(value);
    // 输出只允许：单引号定界、\uXXXX、以及少量反斜杠转义 —— 不允许裸引号/尖括号/&。
    assert.match(literal, /^'(?:[^'\\]|\\u[0-9a-fA-F]{4}|\\.)*'$/);
    assert.doesNotMatch(literal, /["<>&]/);
    // 作为 JS 求值必须还原成原字符串（不是 HTML 实体、不是 URI 编码）。
    assert.equal(eval(literal), value); // eslint-disable-line no-eval
  }
  assert.equal(eval(adaptiveJsArg(null)), ''); // eslint-disable-line no-eval
  assert.equal(eval(adaptiveJsArg(undefined)), ''); // eslint-disable-line no-eval
});

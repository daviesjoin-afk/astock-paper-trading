/* inline handler 的 JS 字符串参数注入守卫。
 *
 * 错误写法：``onclick="fn(\''+adaptiveEsc(x)+'\')"``。
 * 浏览器会**先把 HTML 属性值解码、再当 JS 解析**，adaptiveEsc 产出的 `&#39;`
 * 解码回 `'` 后直接闭合字符串 → 注入。正确写法是 core/format.js 的
 * adaptiveJsArg(value) 在模板**外**预计算出 ``xxxArg``，handler 文本里只留
 * 预计算变量（``onclick="fn('+xArg+')"``）。
 *
 * 本文件锁两件事：
 *   1. 源码守卫：frontend/src 下任何 inline handler 文本里都不允许出现
 *      ``\''+<任意表达式>`` 这种"把值拼进 JS 字符串字面量"的裸拼，也不允许
 *      在 handler 文本里直接调用 adaptiveEsc/paperText/paperNum/adaptiveJsArg
 *      （后者会被契约测试当成需要挂 window 的全局函数）。
 *   2. 行为断言：拿含 ``');alert(1)//`` 的 id/code 真跑渲染函数，断言生成的
 *      onclick 属性里除字符串定界符外没有裸单引号，且转义结果是 `\u0027`。
 */
import assert from 'node:assert/strict';
import test from 'node:test';
import { readdirSync, readFileSync, statSync } from 'node:fs';
import { fileURLToPath } from 'node:url';
import path from 'node:path';

const here = path.dirname(fileURLToPath(import.meta.url));
const srcDir = path.join(here, '..', 'src');

const paper = await import(new URL('../src/features/paper.js', import.meta.url).href);
const selection = await import(new URL('../src/features/selection.js', import.meta.url).href);
const strategies = await import(new URL('../src/features/strategies.js', import.meta.url).href);

const HOSTILE = "x');alert(1)//";

/* 注释里出现的 onclick 示例不是代码，不参与守卫；把注释内容换成空格以保留行号。 */
function stripComments(text) {
  const blank = (block) => block.replace(/[^\n]/g, ' ');
  return text
    .replace(/\/\*[\s\S]*?\*\//g, blank)
    .replace(/(^|\s)\/\/[^\n]*/g, (line, head) => head + blank(line.slice(head.length)));
}

/* 与 backend/test_frontend_module_contract.py 相同的 handler 文本提取：
   从 `on<event>="` 开始，按括号深度找到真正的收尾引号，跳过 `\'` 这类源码转义。
   这里只认真正的事件属性名（`data-runtime-only="1"` 这种以 on 开头的自定义属性不算）。 */
const EVENT_ATTR = /\bon(?:click|change|input|submit|reset|keydown|keyup|keypress|blur|focus|focusin|focusout|mouseover|mouseout|mouseenter|mouseleave|mousedown|mouseup|mousemove|dblclick|contextmenu|touchstart|touchend|touchmove|pointerdown|pointerup|dragstart|dragover|dragleave|drop|scroll|wheel|load|error|animationend|transitionend)\s*=\s*\\?["']/gi;

/* 错误模式：`onclick="fn(\''+adaptiveEsc(x)+'\')"` —— 源码里先闭合外层 JS 字符串、
   转义一个引号、再把值拼回字符串字面量。正确写法 `onclick="fn('+xArg+')"` 里
   没有 `\'`，参数是模板外预计算的变量。 */
const NAKED_ARG = /\\''\s*\+/;

function handlerTexts(blob) {
  const found = [];
  const re = new RegExp(EVENT_ATTR.source, 'gi');
  let match;
  while ((match = re.exec(blob))) {
    const quote = blob[match.index + match[0].length - 1];
    let i = match.index + match[0].length;
    let depth = 0;
    let text = '';
    while (i < blob.length) {
      const ch = blob[i];
      if (ch === '\\') { i += 2; continue; }
      if (ch === quote && depth === 0) break;
      if (ch === '(') depth += 1;
      else if (ch === ')') depth -= 1;
      text += ch;
      i += 1;
    }
    found.push({ text, index: match.index });
  }
  return found;
}

function jsFiles(dir) {
  const files = [];
  for (const name of readdirSync(dir)) {
    const full = path.join(dir, name);
    if (statSync(full).isDirectory()) files.push(...jsFiles(full));
    else if (name.endsWith('.js')) files.push(full);
  }
  return files;
}

/* 逐行扫描源码（排除注释行）：同一行里既出现 inline 事件属性、又出现 `\''+`
   裸拼，就是必须修的注入面。返回 `行号: 源码片段`。 */
function nakedHandlerArgs(source) {
  const codeLines = stripComments(source).split('\n');
  const rawLines = source.split('\n');
  const eventLine = new RegExp(EVENT_ATTR.source, 'i');
  const violations = [];
  for (let i = 0; i < codeLines.length; i += 1) {
    if (!eventLine.test(codeLines[i])) continue;
    if (!NAKED_ARG.test(codeLines[i])) continue;
    violations.push(`${i + 1}: ${(rawLines[i] || '').trim().slice(0, 160)}`);
  }
  return violations;
}

test('源码守卫：inline handler 不再把值裸拼进 JS 字符串字面量', () => {
  // 守卫先自证能抓到经典坏写法，再要求全仓库干净 —— 否则全绿可能只是规则失效。
  const canonicalBad = "x+='<button onclick=\"wbOpenDetail(\\''+adaptiveEsc(item.id)+'\\')\">x</button>';";
  assert.equal(nakedHandlerArgs(canonicalBad).length, 1, '守卫抓不到经典坏写法，规则已失效');
  assert.equal(nakedHandlerArgs("x+='<button onclick=\"wbOpenDetail('+itemArg+')\">x</button>';").length, 0);

  const violations = [];
  let handlers = 0;
  for (const file of jsFiles(srcDir)) {
    const raw = readFileSync(file, 'utf8');
    const rel = path.relative(srcDir, file);
    for (const violation of nakedHandlerArgs(raw)) violations.push(`${rel}:${violation}`);
    for (const { text } of handlerTexts(stripComments(raw))) {
      handlers += 1;
      // handler 文本里不允许调用转义/格式化函数：参数必须在模板外预计算。
      if (/\b(?:adaptiveEsc|paperText|paperNum|adaptiveJsArg)\s*\(/.test(text)) {
        violations.push(`${rel} handler 文本内调用了转义函数：${text.slice(0, 120)}`);
      }
    }
  }
  // 守卫不能因为提取失败而静默通过。
  assert.ok(handlers > 100, `只提取到 ${handlers} 个 inline handler，提取规则可能失效`);
  assert.deepEqual(violations, []);
});

function onclickValues(html) {
  const values = [];
  const re = new RegExp(EVENT_ATTR.source, 'gi');
  let match;
  while ((match = re.exec(html))) {
    const quote = html[match.index + match[0].length - 1];
    const start = match.index + match[0].length;
    const end = html.indexOf(quote, start);
    if (end > start) values.push(html.slice(start, end));
  }
  return values;
}

/* 断言一个 handler 属性值：单引号只能出现在字符串定界符位置，
   定界符内部不含裸单引号（否则就能提前闭合字符串），且恶意引号已转成 \u0027。 */
function assertSafeHandler(value) {
  assert.match(value, /^[A-Za-z_$][\w$]*\('[^']*'(?:,'[^']*')*\)$/, `handler 形状不安全：${value}`);
  assert.equal((value.match(/'/g) || []).length, 2 * (value.split(',').length), `引号数量异常：${value}`);
  assert.match(value, /\\u0027/, `恶意单引号没有转义成 \\u0027：${value}`);
  assert.doesNotMatch(value, /\\u0027'\)/, '转义序列后紧跟定界符，说明拼接错位');
}

test('paper 策略卡：恶意 strategy id 逃不出 inline handler 的字符串参数', () => {
  const html = paper.paperRunningStrategyCard(HOSTILE, { name: '演示', status: 'validated', origin: 'builtin' }, {}, null);
  const values = onclickValues(html);
  assert.equal(values.length, 1, `策略卡应只有一个 inline handler：${html.slice(0, 300)}`);
  assert.equal(values[0], "openInStrategyWorkbench('x\\u0027);alert(1)//')");
  assertSafeHandler(values[0]);
});

test('策略工坊卡片：恶意 strategy id 在四个 handler 中都被转义', () => {
  const html = strategies.wbCard({ id: HOSTILE, name: '演示', origin: 'user', status: 'draft', metadata: {} });
  const values = onclickValues(html);
  assert.equal(values.length, 4, `卡片应有 4 个 inline handler：${html.slice(0, 400)}`);
  for (const value of values) {
    assert.match(value, /^(wbOpenEditor|wbCloneStrategy|wbDeleteDraft|wbOpenDetail)\(/);
    assertSafeHandler(value);
  }
});

test('跟踪池：恶意股票 code 在 trackRemove handler 中被转义', () => {
  const nodes = { trackSummary: { innerHTML: '' }, trackTable: { innerHTML: '' }, trackFilterCount: { textContent: '' } };
  const previous = globalThis.document;
  globalThis.document = { getElementById: (id) => nodes[id] || null, querySelectorAll: () => [] };
  try {
    selection.renderTrack({
      positions: [{
        code: HOSTILE, name: '演示', industry: '测试', cost: 10, added_at: '2026-01-01',
        price: 10, pct_today: 0, ret_pct: null, drawdown_from_peak: null,
        strategy: 'builtin', hold_days: 1, action: '观察', signals: [], sell_decision: {},
      }],
      disclaimer: '',
      checked_at: '2026-01-01',
    });
  } finally {
    if (previous === undefined) delete globalThis.document;
    else globalThis.document = previous;
  }
  const values = onclickValues(nodes.trackTable.innerHTML);
  assert.equal(values.length, 1, `跟踪表应只有一个 inline handler：${nodes.trackTable.innerHTML.slice(0, 400)}`);
  assert.match(values[0], /^trackRemove\('/);
  assertSafeHandler(values[0]);
});

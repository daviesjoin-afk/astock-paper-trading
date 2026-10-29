import assert from 'node:assert/strict';
import fs from 'node:fs';
import test from 'node:test';

const read = (name) => fs.readFileSync(new URL(name, import.meta.url), 'utf8');
const feature = read('../src/features/strategies.js');
const index = read('../index.html');
const page = index.slice(index.indexOf('<div id="p-strategies"'), index.indexOf('<div id="p-settings"'));

test('工坊静态和动态动作都由本页承接，禁止内联处理器和全局桥回流', () => {
  assert.doesNotMatch(feature + page, /\bon(?:click|input|change)\s*=["']/);
  assert.doesNotMatch(read('../src/bridge.js'), /window\.(?:wb[A-Z]\w*|loadStrategyWorkbench)\s*=/);
  assert.match(read('../src/boot.js'), /installStrategyWorkbenchActions\(document\)/);
  assert.match(page, /data-wb-input="search"/);
});

test('所有已渲染的工坊动作都有明确分发，检查不能为空', () => {
  const rendered = new Set(Array.from((feature + page).matchAll(/data-wb-action="([\w-]+)"/g), (m) => m[1]));
  const dispatch = feature.slice(feature.indexOf('var actions={'), feature.indexOf('if(!Object.prototype.hasOwnProperty'));
  const handled = new Set(Array.from(dispatch.matchAll(/'([\w-]+)':/g), (m) => m[1]));
  assert.ok(rendered.size >= 20, '没有采集到完整工坊动作，守卫空转');
  assert.deepEqual([...rendered].sort(), [...handled].sort());
});

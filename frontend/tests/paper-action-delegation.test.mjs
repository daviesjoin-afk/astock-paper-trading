import assert from 'node:assert/strict';
import fs from 'node:fs';
import path from 'node:path';
import test from 'node:test';
import { fileURLToPath } from 'node:url';

const here = path.dirname(fileURLToPath(import.meta.url));
const src = (name) => fs.readFileSync(path.join(here, '..', 'src', name), 'utf8');

test('模拟交易页与选股页不再使用内联 onclick（C2 逐页撤全局桥）', () => {
  const paper = src('features/paper.js');
  const selection = src('features/selection.js');
  for (const [name, text] of [['paper.js', paper], ['selection.js', selection]]) {
    assert.equal(
      /onclick="/.test(text), false,
      `${name} 仍有内联 onclick`,
    );
  }
  assert.match(paper, /data-paper-action="sell"/);
  assert.match(paper, /data-paper-action="cancel-order"/);
  assert.match(paper, /data-paper-action="stock-history"/);
  assert.match(paper, /data-paper-action="open-workbench"/);
  assert.match(selection, /data-selection-action="init"/);
  assert.match(paper, /installPaperActions/);
  assert.match(selection, /installSelectionActions/);
});

test('bridge 不再暴露已迁移的全局函数', () => {
  const bridge = src('bridge.js');
  for (const name of ['startInit', 'preparePaperSell', 'cancelPaperOrder',
                     'openInStrategyWorkbench', 'showPaperStockHistory']) {
    assert.equal(
      bridge.includes(`window.${name}`), false,
      `bridge 仍暴露 window.${name}`,
    );
  }
});

test('boot 装好三个页面的委托监听', () => {
  const boot = src('boot.js');
  assert.match(boot, /installPaperRiskActions\(document\)/);
  assert.match(boot, /installPaperActions\(document\)/);
  assert.match(boot, /installSelectionActions\(document\)/);
});

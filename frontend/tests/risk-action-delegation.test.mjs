import assert from 'node:assert/strict';
import fs from 'node:fs';
import path from 'node:path';
import test from 'node:test';
import { fileURLToPath } from 'node:url';

const here = path.dirname(fileURLToPath(import.meta.url));
const src = (name) => fs.readFileSync(path.join(here, '..', 'src', name), 'utf8');

test('风控页的动作按钮不再使用内联 onclick（C2 逐页撤全局桥）', () => {
  const risk = src('features/risk.js');
  assert.equal(risk.includes('onclick='), false, 'risk.js 仍有内联 onclick');
  assert.match(risk, /data-risk-action="audit-prev"/);
  assert.match(risk, /data-risk-action="audit-next"/);
  assert.match(risk, /installPaperRiskActions/);
});

test('bridge 不再暴露风控页的动作函数', () => {
  const bridge = src('bridge.js');
  assert.equal(bridge.includes('window.loadPaperAudit'), false);
  assert.equal(bridge.includes('window.loadPaperRisk'), false);
});

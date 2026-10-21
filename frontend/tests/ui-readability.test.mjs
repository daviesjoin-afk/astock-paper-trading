// 可读性回归：设置页不被慢接口阻塞、枚举有中文、缺失值不算成 0、表格可滚动。
import test from 'node:test';
import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';

const source = (relative) => readFileSync(new URL(relative, import.meta.url), 'utf8');

test('设置页不再被策略注册表阻塞', () => {
  const text = source('../src/features/settings.js');
  assert.ok(!text.includes('var results=await Promise.all(['),
    '仍在用 Promise.all 等待注册表，冷启动时整页会卡住');
  assert.ok(text.includes('timeout:8000'), '注册表请求缺少独立超时');
  assert.ok(text.includes('await api(\'/api/settings/?_='), '设置请求不是主路径');
});

test('执行画像的族、紧急度与状态已翻译', () => {
  const text = source('../src/features/execution.js');
  for (const name of ['EXEC_FAMILY_LABELS', 'EXEC_URGENCY_LABELS', 'EXEC_STATUS_LABELS']) {
    assert.ok(text.includes(name), `缺少 ${name}`);
  }
  assert.ok(!text.includes('execText(p.family)'), '画像族仍直出英文');
  assert.ok(!text.includes('execText(p.urgency)'), '紧急度仍直出英文');
  assert.ok(!text.includes('execText(row.status)'), '委托状态仍直出英文');
});

test('策略工坊筛选按钮使用中文', () => {
  const html = source('../index.html');
  assert.ok(!html.includes('>Production Sim</button>'), '筛选按钮仍是英文');
  assert.ok(html.includes('>生产模拟</button>'), '缺少中文筛选按钮');
  assert.ok(html.includes('>安全隔离</button>'), '缺少中文筛选按钮');
});

test('无盘中快照时不把覆盖率算成 0', () => {
  const text = source('../src/features/selection.js');
  assert.ok(text.includes('liveCoverage===null'), '缺少未知覆盖率的处理');
  assert.ok(text.includes('无盘中快照'), '缺少无快照文案');
});

test('行情校验词表只在 format.js 维护一份', () => {
  const risk = source('../src/features/risk.js');
  assert.ok(risk.includes('zhRiskText(validation)'), '风控页未复用统一词表');
  assert.ok(!risk.includes("cross_source_checked:'通过'"), '风控页仍在维护自己的词表');
  const format = source('../src/core/format.js');
  assert.ok(format.includes("'incomplete':'主源缺失'"), '统一词表缺少主源缺失');
});

test('表格容器提供横向滚动而不是裁切', () => {
  const layout = source('../styles/layout.css');
  assert.ok(!layout.includes('.paper-challenge-table { overflow:hidden'),
    '挑战表格仍禁止滚动，右侧列看不到');
  const adaptive = source('../styles/adaptive.css');
  assert.ok(adaptive.includes('.adaptive-table'), '缺少 adaptive-table 样式定义');
});

test('时间线卡片字段网格允许长文本换行', () => {
  const text = source('../src/features/adaptive.js');
  assert.ok(text.includes('word-break:break-word'), '字段网格缺少换行保护');
  assert.ok(text.includes('minmax(170px'), '字段网格列宽仍过窄');
});

test('AI 槽位表单用两列结构，标签不再挤在输入行里', () => {
  const text = source('../src/features/ai-provider-settings.js');
  assert.ok(text.includes('ai-slot-form'), '缺少槽位表单类');
  assert.ok(!text.includes('<div class="setting-control"><label for="aiSlot_'),
    '标签仍与输入框挤在同一 flex 行，宽度会随标签长短参差');
  assert.ok(text.includes('setting-label"><label for="aiSlot_'), '标签未独占左列');
});

test('槽位表单的输入框统一放宽', () => {
  const css = source('../styles/tokens.css');
  assert.ok(css.includes('.ai-slot-form .setting-control input'), '缺少槽位输入框样式');
  assert.ok(css.includes('max-width:420px'), '输入框未放宽到统一宽度');
});

test('模型名称支持从上游列表选择', () => {
  const text = source('../src/features/ai-provider-settings.js');
  assert.ok(text.includes('<select id="aiSlot_'), '槽位未使用下拉框');
  assert.ok(!text.includes('datalist'), '槽位仍使用输入框补全而不是下拉选择');
  assert.ok(text.includes('refreshAiProviderModels'), '缺少获取模型的处理函数');
  assert.ok(text.includes('/models'), '未调用上游模型列表接口');
  const bridge = source('../src/bridge.js');
  assert.ok(bridge.includes('window.refreshAiProviderModels'), '获取模型函数未挂到全局');
});

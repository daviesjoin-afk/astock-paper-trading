import test from 'node:test';
import assert from 'node:assert/strict';
import { adaptiveNeuralPanelHtml } from '../src/features/adaptive.js';

test('missing research status does not claim readiness, approval or training', () => {
  const html = adaptiveNeuralPanelHtml({});
  assert.match(html, /研究条件<\/small><b>未取得结果/);
  assert.match(html, /人工确认<\/small><b>未取得状态/);
  assert.match(html, /模型训练<\/small><b>尚不能确认/);
  assert.match(html, /预测证据<\/small><b>样本外有效性尚未证明/);
  assert.doesNotMatch(html, /onclick="approveAdaptiveNeural/);
});

test('blocked evidence is escaped and cannot offer approval', () => {
  const html = adaptiveNeuralPanelHtml({approved: false, readiness: {
    admitted: false, blockers: ['<缺少证据>'], evaluation_admitted: false,
  }});
  assert.match(html, /研究条件<\/small><b>尚未满足/);
  assert.match(html, /人工确认<\/small><b>未确认/);
  assert.match(html, /&lt;缺少证据&gt;/);
  assert.doesNotMatch(html, /onclick="approveAdaptiveNeural/);
});

test('approval and passed research evidence remain distinct from trading use', () => {
  const readiness = {
    admitted: true, evaluation_admitted: true,
    evaluation_model_artifact_fingerprint: 'artifact', evaluation_trained_through: '2026-10-20',
  };
  const awaiting = adaptiveNeuralPanelHtml({approved: false, readiness});
  assert.match(awaiting, /研究条件<\/small><b>已满足/);
  assert.match(awaiting, /模型训练<\/small><b>已记录训练溯源/);
  assert.match(awaiting, /预测证据<\/small><b>样本外评估已通过/);
  assert.match(awaiting, /onclick="approveAdaptiveNeural\(\)"/);
  const approved = adaptiveNeuralPanelHtml({approved: true, readiness, max_rank_adjustment: 0.05});
  assert.match(approved, /人工确认<\/small><b>已确认/);
  assert.match(approved, /交易排序<\/small><b>未参与/);
  assert.match(approved, /不参与交易候选排序/);
  assert.doesNotMatch(approved, /onclick="approveAdaptiveNeural|影响排序分|排序副分|启用有界影子评分/);
});

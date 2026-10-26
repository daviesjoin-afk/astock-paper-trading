// 自动采用授权（方案 §5）：签发一份有界凭据，或撤销它。
//
// 这一区默认是"没有凭据"的状态，也就是 ADVISORY：只出建议、不自动采用。
// 界面刻意不做"一键开启"——签发要填全字段并二次确认，撤销会立刻作废旧凭据。
import { apiJson as api } from "../core/api.js";
import { $ } from "../core/dom.js";
import { adaptiveEsc as esc } from "../core/format.js";
import { settingsConfirm, toast } from "../ui/dialog.js";

const MODES = [
  ['BOUNDED_AUTO', 'BOUNDED_AUTO · 授权范围内自动采用'],
  ['ADVISORY', 'ADVISORY · 只出建议（安全默认）'],
  ['REQUIRE_AI', 'REQUIRE_AI · 无有效意见则冻结新增风险'],
  ['OFF', 'OFF · 完全关闭 AI 意见消费'],
];

function numberField(id, label, value, step, testid) {
  return '<div class="setting-row"><div class="setting-label"><b>' + esc(label) + '</b></div>'
    + '<div class="setting-control"><input id="' + id + '" data-testid="' + (testid || id)
    + '" type="number" min="0.0001" step="' + step + '" value="' + esc(String(value)) + '"></div></div>';
}

function textField(id, label, value, testid) {
  return '<div class="setting-row"><div class="setting-label"><b>' + esc(label) + '</b></div>'
    + '<div class="setting-control"><input id="' + id + '" data-testid="' + (testid || id)
    + '" type="text" maxlength="120" value="' + esc(String(value || '')) + '"></div></div>';
}

export function renderAiCapabilitySection(payload) {
  const current = (payload && payload.current) || {};
  const rows = (payload && payload.capabilities) || [];
  const mode = current.mode || 'ADVISORY';
  const badges = '<p class="setting-help">当前生效模式：<b data-testid="ai-capability-mode">'
    + esc(mode) + '</b>'
    + (current.auto_applies ? '（会在授权范围内自动采用）'
      : '（不会自动采用任何变更：只出建议）')
    + '</p>'
    + '<p class="setting-help">签发凭据是<strong>运维行为</strong>：字段必须填全，'
    + '且平台不会因为"训练好了"或"模型建议"自动签发。没有凭据时系统保持 ADVISORY。</p>';

  const list = rows.length
    ? '<table class="settings-table" data-testid="ai-capability-table"><thead><tr>'
      + '<th>凭据</th><th>模式</th><th>策略</th><th>字段</th><th>日变更上限</th>'
      + '<th>到期</th><th>撤销代号</th><th></th></tr></thead><tbody>'
      + rows.map(function (row) {
        const scope = row.scope || {};
        return '<tr data-testid="ai-capability-row"><td>' + esc(row.capability_id) + '</td>'
          + '<td>' + esc(row.mode) + '</td>'
          + '<td>' + esc((scope.strategy_ids || []).join('、')) + '</td>'
          + '<td>' + esc((scope.fields || []).join('、')) + '</td>'
          + '<td>' + esc(String((row.limits || {}).max_change_per_day)) + '</td>'
          + '<td>' + esc(row.expires_at) + '</td>'
          + '<td>' + esc(String(row.revocation_epoch)) + '</td>'
          + '<td><button type="button" class="ghost" data-testid="ai-capability-revoke-'
          + esc(row.capability_id) + '" onclick="revokeAiCapability(\''
          + esc(row.capability_id) + '\')">撤销</button></td></tr>';
      }).join('') + '</tbody></table>'
    : '<p class="setting-help" data-testid="ai-capability-empty">还没有签发任何凭据。</p>';

  const form = '<div class="ai-slot-form" data-testid="ai-capability-form">'
    + textField('aiCapId', '凭据 ID', '', 'ai-capability-id')
    + '<div class="setting-row"><div class="setting-label"><b>模式</b></div>'
    + '<div class="setting-control"><select id="aiCapMode" data-testid="ai-capability-form-mode">'
    + MODES.map(function (item) {
      return '<option value="' + esc(item[0]) + '">' + esc(item[1]) + '</option>';
    }).join('') + '</select></div></div>'
    + textField('aiCapStrategies', '授权策略（逗号分隔）', 'tq_breakout', 'ai-capability-strategies')
    + textField('aiCapTargets', '目标类型（逗号分隔）', 'entry_policy', 'ai-capability-targets')
    + textField('aiCapFields', '允许字段（逗号分隔）', 'first_board_bonus', 'ai-capability-fields')
    + textField('aiCapEvidence', '证据政策 ID', 'evidence-v1', 'ai-capability-evidence')
    + textField('aiCapBounds', '参数边界版本', 'bounds-v1', 'ai-capability-bounds')
    + textField('aiCapLoss', '损失停止政策 ID', 'loss-v1', 'ai-capability-loss')
    + numberField('aiCapPerApply', '单次变更上限', 0.05, '0.01', 'ai-capability-per-apply')
    + numberField('aiCapPerDay', '每日变更上限', 0.1, '0.01', 'ai-capability-per-day')
    + numberField('aiCapTrials', '每日试次上限', 3, '1', 'ai-capability-trials')
    + numberField('aiCapCost', '每日成本上限', 5, '0.5', 'ai-capability-cost')
    + numberField('aiCapSteps', '单任务工具步数上限', 12, '1', 'ai-capability-steps')
    + textField('aiCapExpires', '到期时间（ISO，带时区）', '', 'ai-capability-expires')
    + textField('aiCapOperator', '操作人', '', 'ai-capability-operator')
    + '<div class="settings-actions"><button type="button" data-testid="ai-capability-submit"'
    + ' onclick="issueAiCapability()">签发凭据</button>'
    + '<button type="button" class="ghost" onclick="refreshAiCapabilities()">刷新</button></div>'
    + '<div id="aiCapabilityFeedback" class="setting-help" role="status" aria-live="polite"></div>'
    + '</div>';

  return '<section class="ai-capability" data-testid="ai-capability-section">'
    + '<h4>3. 自动采用授权（默认关闭）</h4>' + badges + list
    + '<h4 class="ai-review-heading">签发新凭据</h4>' + form + '</section>';
}

function feedback(message) {
  const node = $('aiCapabilityFeedback');
  if (node) node.textContent = message;
}

function splitList(value) {
  return String(value || '').split(/[,，]/).map(function (item) {
    return item.trim();
  }).filter(Boolean);
}

function num(id) {
  const node = $(id);
  return node ? Number(node.value) : NaN;
}

function text(id) {
  const node = $(id);
  return node ? String(node.value || '').trim() : '';
}

export async function refreshAiCapabilities() {
  try {
    const payload = await api('/api/settings/ai-review/capabilities', 'GET');
    const host = document.querySelector('[data-testid="ai-capability-section"]');
    if (host) host.outerHTML = renderAiCapabilitySection(payload);
    const mode = document.querySelector('[data-testid="ai-capability-mode"]');
    if (mode) mode.textContent = (payload.current || {}).mode || 'ADVISORY';
    return payload;
  } catch (err) {
    feedback('读取授权凭据失败：' + (err && err.message ? err.message : err));
    return null;
  }
}

export async function issueAiCapability() {
  const body = {
    capability_id: text('aiCapId'),
    mode: text('aiCapMode') || 'ADVISORY',
    strategy_ids: splitList(text('aiCapStrategies')),
    allowed_target_kinds: splitList(text('aiCapTargets')),
    allowed_fields: splitList(text('aiCapFields')),
    bounds_version: text('aiCapBounds'),
    evidence_policy_id: text('aiCapEvidence'),
    loss_stop_policy_id: text('aiCapLoss'),
    max_change_per_apply: num('aiCapPerApply'),
    max_change_per_day: num('aiCapPerDay'),
    max_trials_per_day: num('aiCapTrials'),
    daily_cost_cap: num('aiCapCost'),
    max_tool_steps_per_task: num('aiCapSteps'),
    expires_at: text('aiCapExpires'),
    revocation_epoch: 1,
    issued_by: text('aiCapOperator'),
  };
  if (!body.capability_id) { feedback('请填写凭据 ID'); return null; }
  if (!body.issued_by) { feedback('请填写操作人'); return null; }
  if (!(body.max_change_per_day > 0) || !(body.max_change_per_apply > 0)) {
    feedback('变更上限必须是正数'); return null;
  }
  const ok = await settingsConfirm(
    '签发后，' + body.mode + ' 模式会在这些策略/字段范围内自动采用通过校验的变更。'
    + '凭据 ID：' + body.capability_id + '。确认签发？');
  if (!ok) return null;
  try {
    const result = await api('/api/settings/ai-review/capabilities?confirmed=true', 'POST', body,
      {});
    toast('凭据已签发：' + (result.capability_id || body.capability_id));
    await refreshAiCapabilities();
    return result;
  } catch (err) {
    feedback('签发失败：' + (err && err.message ? err.message : err));
    return null;
  }
}

export async function revokeAiCapability(capabilityId) {
  const operator = text('aiCapOperator');
  if (!operator) {
    feedback('撤销需要填写操作人');
    return null;
  }
  const ok = await settingsConfirm('撤销后，所有持旧撤销代号的请求立即失效。确认撤销 '
    + capabilityId + '？');
  if (!ok) return null;
  try {
    const result = await api(
      '/api/settings/ai-review/capabilities/' + encodeURIComponent(capabilityId) + '/revoke?confirmed=true',
      'POST', { operator: operator, reason: 'revoked from settings page' },
      {});
    toast('已撤销：' + capabilityId + '（撤销代号 ' + result.revocation_epoch + '）');
    await refreshAiCapabilities();
    return result;
  } catch (err) {
    feedback('撤销失败：' + (err && err.message ? err.message : err));
    return null;
  }
}

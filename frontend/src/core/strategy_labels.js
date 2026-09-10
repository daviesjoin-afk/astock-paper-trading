/* PR-57：策略生命周期标签的**唯一**规范来源。
 *
 * 内部枚举（后端契约，不变）：draft / validated / active / paused / retiring / archived
 * 用户可见主标签：草稿 / 已验证 / 运行中 / 已暂停 / 退役中 / 已归档
 * 技术枚举只允许出现在"技术详情 / 审计"这类次级视图里。
 *
 * 任何 feature（Strategy Workbench、Settings、Paper）都必须从这里取标签，
 * 不允许各自再维护一份 map。
 */

export var STRATEGY_STATUS_ENUM = ['draft', 'validated', 'active', 'paused', 'retiring', 'archived'];

/** 用户可见主标签（中文，界面主文案）。 */
export var STRATEGY_STATUS_LABELS = {
  draft: '草稿',
  validated: '已验证',
  active: '运行中',
  paused: '已暂停',
  retiring: '退役中',
  archived: '已归档',
};

/** 技术枚举（仅供技术详情 / 审计视图显示，不作用户主标签）。 */
export var STRATEGY_STATUS_LABELS_TECH = {
  draft: 'draft',
  validated: 'validated',
  active: 'active',
  paused: 'paused',
  retiring: 'retiring',
  archived: 'archived',
};

/** 主标签；status 未知时回退为原值，绝不显示 undefined。 */
export function strategyStatusLabel(status) {
  if (!status) return '—';
  return STRATEGY_STATUS_LABELS[status] || String(status);
}

/** 技术标签；用于"技术详情"披露区。 */
export function strategyStatusLabelTech(status) {
  if (!status) return '—';
  return STRATEGY_STATUS_LABELS_TECH[status] || String(status);
}

/** 是否处于"不参与未来周期"的终态。 */
export function strategyStatusIsTerminal(status) {
  return status === 'archived';
}

/* 徽标配色类名：与颜色无关的语义类，保留原命名以便既有 CSS/测试继续工作。 */
export var STRATEGY_STATUS_BADGE_CLASS = {
  draft: 'strategy-status-draft',
  validated: 'strategy-status-validated',
  active: 'strategy-status-active',
  paused: 'strategy-status-paused',
  retiring: 'strategy-status-retiring',
  archived: 'strategy-status-archived',
};

/**
 * 状态徽标：主文案是中文标签 + 非颜色信号（符号），避免"只靠颜色"表达状态。
 * 技术枚举通过 title 属性暴露，键盘/读屏也能拿到。
 */
export function strategyStatusBadge(status) {
  var tone = STRATEGY_STATUS_BADGE_CLASS[status] || 'strategy-status-draft';
  var label = strategyStatusLabel(status);
  var tech = strategyStatusLabelTech(status);
  var glyph = status === 'active' ? '●'
    : status === 'paused' ? '❙❙'
    : status === 'validated' ? '✓'
    : status === 'retiring' ? '⤳'
    : status === 'archived' ? '⧈'
    : '✎';
  return '<span class="strategy-status-badge ' + tone + '" data-status="' + tech + '" title="' + tech + '">'
    + '<span class="strategy-status-glyph" aria-hidden="true">' + glyph + '</span>'
    + '<span class="strategy-status-text">' + label + '</span></span>';
}

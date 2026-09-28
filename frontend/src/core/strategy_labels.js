/* PR-57：策略生命周期标签的**唯一**规范来源。
 *
 * 内部枚举与展示词表跟随 Strategy Lifecycle owner。
 * 技术枚举只允许出现在"技术详情 / 审计"这类次级视图里。
 *
 * 任何 feature（Strategy Workbench、Settings、Paper）都必须从这里取标签，
 * 不允许各自再维护一份 map。
 */

export var STRATEGY_STATUS_ENUM = ['draft','candidate','research','validated','shadow','paper','production_sim','degraded','paused','retiring','archived','rejected','validation_failed','quarantined'];

/** 用户可见主标签（中文，界面主文案）。 */
export var STRATEGY_STATUS_LABELS = {
  draft: '草稿',
  candidate: '候选',
  research: '研究中',
  validated: '已验证',
  shadow: '影子阶段',
  paper: '模拟运行中',
  production_sim: '生产模拟',
  degraded: '已降级',
  paused: '已暂停',
  retiring: '退役中',
  archived: '已归档',
  rejected: '已拒绝',
  validation_failed: '验证失败',
  quarantined: '安全隔离',
};

/** 技术枚举（仅供技术详情 / 审计视图显示，不作用户主标签）。 */
export var STRATEGY_STATUS_LABELS_TECH = {
  draft: 'draft',
  candidate: 'candidate',
  research: 'research',
  validated: 'validated',
  shadow: 'shadow',
  paper: 'paper',
  production_sim: 'production_sim',
  degraded: 'degraded',
  paused: 'paused',
  retiring: 'retiring',
  archived: 'archived',
  rejected: 'rejected',
  validation_failed: 'validation_failed',
  quarantined: 'quarantined',
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
  return status === 'archived' || status === 'rejected' || status === 'validation_failed';
}

/* 徽标配色类名：与颜色无关的语义类，保留原命名以便既有 CSS/测试继续工作。 */
export var STRATEGY_STATUS_BADGE_CLASS = {
  draft: 'strategy-status-draft',
  validated: 'strategy-status-validated',
  candidate: 'strategy-status-validated',
  research: 'strategy-status-validated',
  shadow: 'strategy-status-draft',
  paper: 'strategy-status-active',
  production_sim: 'strategy-status-active',
  degraded: 'strategy-status-paused',
  paused: 'strategy-status-paused',
  retiring: 'strategy-status-retiring',
  archived: 'strategy-status-archived',
  rejected: 'strategy-status-archived',
  validation_failed: 'strategy-status-archived',
  quarantined: 'strategy-status-paused',
};

/**
 * 状态徽标：主文案是中文标签 + 非颜色信号（符号），避免"只靠颜色"表达状态。
 * 技术枚举通过 title 属性暴露，键盘/读屏也能拿到。
 */
export function strategyStatusBadge(status) {
  var tone = STRATEGY_STATUS_BADGE_CLASS[status] || 'strategy-status-draft';
  var label = strategyStatusLabel(status);
  var tech = strategyStatusLabelTech(status);
  var glyph = status === 'paper' || status === 'production_sim' ? '●'
    : status === 'paused' ? '❙❙'
    : status === 'validated' ? '✓'
    : status === 'retiring' ? '⤳'
    : status === 'archived' ? '⧈'
    : '✎';
  return '<span class="strategy-status-badge ' + tone + '" data-status="' + tech + '" title="' + tech + '">'
    + '<span class="strategy-status-glyph" aria-hidden="true">' + glyph + '</span>'
    + '<span class="strategy-status-text">' + label + '</span></span>';
}

/* PR-55：由 frontend/app.js 拆分（纯搬运，逻辑/文案未改） */
export function fmt(v, d){ if(v===null||v===undefined||v===''||isNaN(v)) return '-'; return Number(v).toFixed(d===undefined?2:d); }

/* 百分比专用：缺值渲染成「—」。直接写 fmt(v)+'%' 会把占位符拼成 '-%'，
   让「没有数据」看起来像一个真实的百分比读数。 */
export function fmtPct(v, d){ if(v===null||v===undefined||v===''||isNaN(v)) return '—'; return Number(v).toFixed(d===undefined?2:d)+'%'; }

export function pctCls(v){ return v>0?'up':(v<0?'down':''); }

export function pctTxt(v){ if(v===null||v===undefined||v===''||isNaN(v)) return '-'; return (v>0?'+':'')+fmt(v)+'%'; }

/* 后端统一返回带 T 的 ISO 时间戳。直接 slice 会把 T 一起显示成
   "01-02T09:30"，所以时间展示统一走这里。 */
export function shortStamp(v){
  var text=String(v||'').replace('T',' ');
  if(!text) return '—';
  return text.length>16?text.slice(5,16):text.slice(5);
}

export function yi(v){ if(v===null||v===undefined) return '-'; return fmt(v/1e8,1)+'亿'; }

export function adaptiveEsc(value){
  return String(value===null||value===undefined?'':value).replace(/[&<>"']/g,function(ch){return {'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[ch];});
}

export function adaptiveJsArg(value){
  return encodeURIComponent(String(value===null||value===undefined?'':value));
}

/* Optional snapshot fields can be absent while a backend is warming up.
   Never render transport placeholders as a state visible to an operator. */
export function adaptiveText(value,fallback){
  var text=String(value===null||value===undefined?'':value).trim();
  if(!text||text==='undefined'||text==='null'||text==='None') return fallback||'';
  return text;
}

export function adaptiveSafeUrl(value){
  var url=String(value||''); return /^https:\/\/[a-z0-9.-]+\//i.test(url)?adaptiveEsc(url):'#';
}

export function dataValidityTone(value, good, warn){
  // 缺值不是「坏数据」：返回空串让样式保持中性，避免把未知渲染成红色告警。
  if(value===null||value===undefined||value===''||isNaN(Number(value))) return '';
  var n=Number(value); return n>=good?'good':(n>=warn?'warn':'bad');
}

export function sigTag(s){
  var cls = s.level==='sell' ? 'tag-warn' : 'tag-info';
  return '<span class="tag '+cls+'" title="'+adaptiveEsc(s.msg)+'">'+adaptiveEsc(s.type)+'</span>';
}

export function sellTag(sd){
  var cls = (sd.action==='卖出'||sd.auction_matrix.level==='sell') ? 'tag-warn' : (sd.action==='止盈减仓' ? 'tag-ok' : 'tag-info');
  return '<span class="tag '+cls+'">'+adaptiveEsc(sd.action)+'｜'+adaptiveEsc(sd.auction_matrix.tier)+'</span>';
}

// ---------- 策略模拟：独立账本，不与自选跟踪混用 ----------
export function cny(v, signed){
  if(v===null||v===undefined||isNaN(v)) return '-';
  return ((signed&&v>0)?'+':'')+'￥'+Number(v).toFixed(2);
}

export function paperStatusTag(status){
  var map={
    running:['tag-ok','运行中'],paused:['tag-info','已暂停'],
    pending:['tag-info','待执行'],filled:['tag-ok','已成交'],
    blocked:['tag-warn','已拦截'],rejected:['tag-warn','已拒绝'],
    superseded:['tag-info','已失效'],cancelled:['tag-info','已撤销'],
    // R25：signal 生命周期状态此前落到 raw token（前端直接打印英文枚举）。
    // 这些是**已落库的裁决结果**，只做展示映射，不在这里推断任何业务规则。
    deferred_capacity:['tag-info','容量等待'],
    entry_frozen_waitlist:['tag-info','冻结待买'],
    recheck_capacity:['tag-info','容量复核'],
    recovery_watch:['tag-info','恢复观察'],
    shadow_q3:['tag-info','影子Q3'],
    expired:['tag-info','已过期'],
    risk_rejected:['tag-warn','风控拒绝']
  };
  var view=map[status]||['tag-info',status||'未知'];
  var cls=view[0], text=view[1];
  return '<span class="tag '+cls+'">'+text+'</span>';
}

// R25：signal decision 的展示投影。后端下发 outcome / reason / evidence 状态，
// 前端只渲染 —— **不得**在这里比较 verification === 'verified' 就显示"双源验证
// 通过"（那是重算后端 policy）。双源结论只认后端算好的 cross_source_verified。
export function signalDecisionView(decision){
  var d=decision||{}, ev=d.evidence||{};
  var labels={approved:'已通过',blocked:'未通过'};
  var outcome=d.outcome||'';
  var evidenceText=ev.cross_source_verified?'证据：双源可信'
    :(ev.verification_method?('证据：'+riskText(zhRiskText(ev.verification||'未核验')))
      :'证据：未知');
  return {
    outcome: outcome,
    outcomeText: labels[outcome]||'待复核',
    reason: d.reason||'',
    evidenceText: evidenceText,
    crossSourceVerified: ev.cross_source_verified===true
  };
}

export function riskText(value){
  return String(value===null||value===undefined||value===''?'-':value)
    .replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;').replace(/"/g,'&quot;');
}

export function zhRiskText(value){
  var text=riskText(value);
  var replacements={
    'exact_cycle_asof_nav_unavailable':'本周期当前净值无法核验',
    'exact_cycle_asof_pending_capacity_unavailable':'当前待执行委托的资金占用无法核验',
    'exact_headroom_unavailable':'剩余可分配资金无法核验',
    'exact_market_quote_evidence_unavailable':'缺少通过核验的市场行情',
    'exact_market_quote_identity_unavailable':'行情来源凭据无法核验',
    'exact_market_value_by_account_unavailable':'各策略持仓市值无法核验',
    'exact_market_valued_classification_unavailable':'持仓分类及集中度无法核验',
    'exact_nav_unavailable':'账户净值无法核验',
    'exact_pending_by_account_unavailable':'各策略待执行委托占用资金无法核验',
    'exact_pending_total_unavailable':'待执行委托占用资金总额无法核验',
    'exact_pending_unavailable':'待执行委托占用资金无法核验',
    'exact_strategy_version_return_series_unavailable':'对应策略版本的历史收益数据无法核验',
    'exact_used_unavailable':'已使用资金无法核验',
    'exact_position_quote_missing_or_invalid':'持仓行情缺失或无效',
    'exact_market_valuations_not_supplied':'未提供通过核验的持仓估值',
    'exact_asof_risk_consumption_owner_unavailable':'当前风险额度占用无法核验',
    'exact_strategy_lifecycle_state_unavailable':'策略运行状态无法核验',
    'exact_cycle_strategy_pin_unavailable':'本周期策略版本无法核验',
    'exact_cycle_strategy_pin_mismatch':'策略版本与本周期记录不一致',
    'bounded_accounting_facts_incomplete':'账务核验资料不完整',
    'bounded_lot_cost_evidence_incomplete':'持仓批次成本资料不完整',
    'bounded_position_quantity_evidence_incomplete':'持仓数量资料不完整',
    'asof_risk_exit_lot_evidence_unavailable':'当前退出持仓批次无法核验',
    'explicit_verified_turnover_window_unavailable':'缺少已核验的成交额统计区间',
    'pending_order_intent_evidence_incomplete':'待执行委托资料不完整',
    'pending_order_intent_evidence_unavailable':'待执行委托无法核验',
    'pending_reservation_evidence_unavailable':'预留资金无法核验',
    'reservation_order_identity_mismatch':'预留资金与委托记录不一致',
    'market_evidence_identity_mismatch':'行情核验凭据不一致',
    'explicit_shared_pool_exposure_cap_unavailable':'未提供资金池仓位上限',
    'explicit_strategy_pool_floor_ratio_unavailable':'未提供策略保底资金比例',
    'concentration_not_available':'集中度资料不可用',
    'no_owner_issued_concentration_adjustment_rule':'尚无可用的集中度调整规则',
    'no_execution_participant_has_new_resource_eligibility':'暂无策略符合新增资金分配条件',
    'unscoped_unknown_pending_order':'存在归属不明的待执行委托',
    'portfolio_allocation_deferred':'资金分配暂缓，等待证据补齐',
    'portfolio_allocation_unavailable':'资金分配核验未完成，暂缓新增委托',
    'entry_frozen_waitlist':'新增买入已冻结，保留候选等待重试',
    'rejected_bootstrap':'候选入场核验未通过',
    'deferred_capacity':'等待可用资金核验',
    'shared_risk_state_blocked':'共享风控状态未通过，暂停新增委托',
    'order_intent_rejected':'委托核验未通过',
    'execution_event_error':'委托执行异常，等待核查',
    'execution_retry':'等待重试执行',
    'signal_expired':'候选信号已过期',
    'paper-risk-v4':'风控规则第4版',
    '精确 allocation plan':'精确资金分配方案',
    'real-time quote lacks passing independent cross-source check':'实时行情未通过独立交叉核验',
    'real-time quote failed price/range validation':'实时行情未通过价格或范围核验',
    '未获得通过的独立行情源校验结果':'未获得通过的独立行情源校验结果',
    '主行情价格或涨跌幅无效':'主行情价格或涨跌幅无效',
    '独立行情源未返回有效价格或时间':'独立行情源未返回有效价格或时间',
    '独立行情源返回结果与主行情不一致':'独立行情源返回结果与主行情不一致',
    '历史记录未保存备用源细节；已进入独立行情校验门但未通过':'历史记录未保存备用源细节；已进入独立行情校验门但未通过',
    'cross_source_checked':'双源核验通过',
    'cross_source_failed':'双源核验未通过', 'cross_source_unavailable':'独立行情源未返回',
    'range_timestamp_checked':'单源行情有效',
    'degraded_cross_source':'降级核验退出',
    'not_independently_verified':'未进行独立交叉核验',
    'reference_only':'仅供参考',
    'fresh':'新鲜', 'stale':'已过期', 'unknown':'未知', 'missing':'缺失',
    'incomplete':'主源缺失',
    'source_error':'主源异常',
    'not_applicable':'不适用', 'failed':'获取失败',
    'active':'运行中', 'paper-risk-v4-shadow':'风控模型 V4（审计模式）',
    'exit_pending_data':'等待有效行情后退出', 'rejected_stale_quote':'行情核验未通过',
    'held_t1':'T+1 锁定', 'unfilled_limit_down':'跌停未成交',
    'MARKET_UNAVAILABLE':'缺少可用于成交的行情', 'MARKET_STALE':'成交行情已过期',
    'MARKET_UNVERIFIED':'成交行情未通过双源核验', 'MARKET_DISAGREEMENT':'双源行情不一致',
    'OUT_OF_SESSION':'当前不在连续交易时段', 'TRADABILITY_UNKNOWN':'缺少可交易性证据',
    'SUSPENDED':'停牌，不可成交', 'PRICE_LIMIT_LOCKED':'涨跌停封板，不可成交',
    'INVALID_QUANTITY':'委托数量不符合整手规则', 'T1_NOT_SELLABLE':'T+1 锁定，暂无可卖份额',
    'INSUFFICIENT_LIQUIDITY':'成交量不足，等待后续成交', 'INSUFFICIENT_CASH':'可用资金不足',
    'LIMIT_PRICE_NOT_REACHED':'限价未触发', 'CANCELLED':'委托已撤销',
    'ORDER_NOT_WORKING':'委托已不在可执行状态',
    'verified_quote_plus_deterministic_slippage':'可信行情加固定滑点',
    'last_verified_quote':'最后可信行情',
    'execution_quote':'成交行情', 'verified':'双源核验通过',
    'single_source':'单源核验', 'disagreement':'双源不一致', 'unavailable':'不可用',
    'not_attempted':'未核验',
    'filled':'已成交', 'pending':'待处理', 'local_cache':'本地缓存', 'unverified':'未核验', 'invalid':'无效'
  };
  // 按完整代码翻译，避免把 pending/unavailable 等片段嵌入中文。
  // 转义用函数而不是 `$&` 模板，也不在正则里写行尾锚点：这个文件里的 `$`
  // 会被跨模块引用扫描误判成 dom.js 的全局名。
  Object.keys(replacements).sort(function(a,b){return b.length-a.length;}).forEach(function(key){
    var escaped=key.replace(/[^A-Za-z0-9_]/g,function(ch){return '\\'+ch;});
    text=text.replace(new RegExp('(^|[^A-Za-z0-9_])'+escaped+'(?![A-Za-z0-9_])','g'),function(_,prefix){return prefix+replacements[key];});
  });
  // 新增但尚未登记的内部代码不直接暴露到页面，原值仍保留在审计接口和日志。
  return text.replace(/\b[A-Za-z][A-Za-z0-9]*_[A-Za-z0-9_]+\b/g,'其他风控信息（待核查）');
}

export function riskLevelView(level){
  return ({normal:['✓','正常'],watch:['!','关注'],tightened:['↓','收紧'],blocked:['×','禁止开仓']}[level]||['?','未知']);
}

export function riskMetric(label,current,limit){
  var c=current===null||current===undefined?'—':fmt(current,2)+'%';
  var l=limit===null||limit===undefined?'—':fmt(limit,2)+'%';
  return '<div class="paper-risk-metric">'+label+'<b>'+c+' / '+l+'</b></div>';
}

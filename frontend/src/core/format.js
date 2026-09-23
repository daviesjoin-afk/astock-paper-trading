/* PR-55：由 frontend/app.js 拆分（纯搬运，逻辑/文案未改） */
export function fmt(v, d){ if(v===null||v===undefined||isNaN(v)) return '-'; return Number(v).toFixed(d===undefined?2:d); }

export function pctCls(v){ return v>0?'up':(v<0?'down':''); }

export function pctTxt(v){ if(v===null||v===undefined) return '-'; return (v>0?'+':'')+fmt(v)+'%'; }

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
  var n=Number(value); return n>=good?'good':(n>=warn?'warn':'bad');
}

export function sigTag(s){
  var cls = s.level==='sell' ? 'tag-warn' : 'tag-info';
  return '<span class="tag '+cls+'" title="'+s.msg+'">'+s.type+'</span>';
}

export function sellTag(sd){
  var cls = (sd.action==='卖出'||sd.auction_matrix.level==='sell') ? 'tag-warn' : (sd.action==='止盈减仓' ? 'tag-ok' : 'tag-info');
  return '<span class="tag '+cls+'">'+sd.action+'｜'+sd.auction_matrix.tier+'</span>';
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
    'real-time quote lacks passing independent cross-source check':'实时行情未通过独立交叉核验',
    'real-time quote failed price/range validation':'实时行情未通过价格或范围核验',
    '未获得通过的独立行情源校验结果':'未获得通过的独立行情源校验结果',
    '主行情价格或涨跌幅无效':'主行情价格或涨跌幅无效',
    '独立行情源未返回有效价格或时间':'独立行情源未返回有效价格或时间',
    '独立行情源返回结果与主行情不一致':'独立行情源返回结果与主行情不一致',
    '历史记录未保存备用源细节；已进入独立行情校验门但未通过':'历史记录未保存备用源细节；已进入独立行情校验门但未通过',
    'cross_source_checked':'双源核验通过',
    'cross_source_failed':'双源核验未通过', 'cross_source_unavailable':'独立行情源未返回',
    'range_timestamp_checked':'主行情有效，待交叉核验',
    'degraded_cross_source':'降级核验退出',
    'not_independently_verified':'未进行独立交叉核验',
    'reference_only':'仅供参考',
    'fresh':'新鲜', 'stale':'已过期', 'unknown':'未知', 'missing':'缺失', 'failed':'获取失败',
    'active':'运行中', 'paper-risk-v4-shadow':'风控模型 V4（审计模式）',
    'exit_pending_data':'等待有效行情后退出', 'rejected_stale_quote':'行情核验未通过',
    'held_t1':'T+1 锁定', 'unfilled_limit_down':'跌停未成交',
    'filled':'已成交', 'pending':'待处理', 'local_cache':'本地缓存', 'unverified':'未核验', 'invalid':'无效', 'cross_source_failed':'双源核验未通过', 'cross_source_unavailable':'独立行情源未返回'
  };
  Object.keys(replacements).forEach(function(key){ text=text.split(key).join(replacements[key]); });
  return text;
}

export function riskLevelView(level){
  return ({normal:['✓','正常'],watch:['!','关注'],tightened:['↓','收紧'],blocked:['×','禁止开仓']}[level]||['?','未知']);
}

export function riskMetric(label,current,limit){
  var c=current===null||current===undefined?'—':fmt(current,2)+'%';
  var l=limit===null||limit===undefined?'—':fmt(limit,2)+'%';
  return '<div class="paper-risk-metric">'+label+'<b>'+c+' / '+l+'</b></div>';
}

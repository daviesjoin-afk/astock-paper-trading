/* PR-55：由 frontend/app.js 拆分（纯搬运，逻辑/文案未改） */
// 跨模块依赖（由原单文件作用域推导）
import { api, apiPost } from "../core/api.js";
import { $, setEl } from "../core/dom.js";
import { adaptiveEsc, adaptiveSafeUrl, adaptiveText, fmt, pctCls, pctTxt } from "../core/format.js";
import { allocationActionPanel } from "./execution.js";
import { adaptiveActionNotice, adaptiveConfirm } from "../ui/dialog.js";

export function adaptiveStageClass(stage){
  if(stage==='eligible_for_review'||stage==='advisory') return 'ready';
  if(stage==='shadow'||stage==='regime_validation') return 'learning';
  return 'collecting';
}

export function adaptiveBar(value, tone){
  var width=Math.max(0,Math.min(100,Number(value)||0));
  return '<div class="adaptive-meter '+(tone||'')+'"><span style="width:'+width+'%"></span></div>';
}

export function adaptiveValue(value,suffix,digits){
  return value===null||value===undefined?'—':fmt(value,digits===undefined?1:digits)+(suffix||'');
}

export function adaptiveJsonArray(value){
  if(Array.isArray(value)) return value;
  try{var parsed=JSON.parse(value||'[]');return Array.isArray(parsed)?parsed:[];}catch(e){return [];}
}

export function adaptiveJsonObject(value){
  if(value&&typeof value==='object'&&!Array.isArray(value)) return value;
  try{var parsed=JSON.parse(value||'{}');return parsed&&typeof parsed==='object'&&!Array.isArray(parsed)?parsed:{};}catch(e){return {};}
}

export function adaptiveLocalDate(){
  var now=new Date();
  return now.getFullYear()+'-'+String(now.getMonth()+1).padStart(2,'0')+'-'+String(now.getDate()).padStart(2,'0');
}

export function adaptiveTimelineRows(payload){
  var root=payload||{}, rows=root.windows||root.rows||root.runs||root.timeline||[];
  if(!Array.isArray(rows)&&rows&&typeof rows==='object') rows=rows.windows||rows.rows||rows.events||[];
  if(!Array.isArray(rows)) rows=[];
  return rows.map(function(row){
    row=row&&typeof row==='object'?row:{};
    var detail=adaptiveJsonObject(row.detail);
    var nested=adaptiveJsonObject(row.result);
    return {row:row,detail:detail,nested:nested};
  }).sort(function(a,b){
    var left=String(a.row.started_at||a.row.created_at||a.row.at||a.row.scheduled_at||'');
    var right=String(b.row.started_at||b.row.created_at||b.row.at||b.row.scheduled_at||'');
    return left<right?-1:(left>right?1:0);
  });
}

export function adaptiveTimelineStatus(value){
  var key=String(value||'unknown').toLowerCase();
  return ({completed:'已完成',success:'已完成',ok:'已完成',running:'运行中',in_progress:'运行中',processing:'处理中',queued:'排队中',pending:'待运行',retrying:'重试中',failed:'失败',error:'失败',blocked:'已阻断',skipped:'已跳过',unknown:'待记录'})[key]||adaptiveText(value,'待记录');
}

export function adaptiveTimelineQuality(value){
  var q=typeof value==='string'?{status:value}:adaptiveJsonObject(value), key=String(q.status||q.state||q.quality||'unknown').toLowerCase();
  var label=({valid:'通过',verified:'已核验',valid_close:'收盘有效',fresh:'新鲜',partial:'部分可用',degraded:'降级',failed:'失败',blocked:'阻断',unknown:'待检查'})[key]||adaptiveText(q.status||q.quality,'待检查');
  var extra=[];
  if(q.coverage_pct!==undefined) extra.push('覆盖 '+adaptiveValue(q.coverage_pct,'%',1));
  if(q.agreement_pct!==undefined) extra.push('一致 '+adaptiveValue(q.agreement_pct,'%',1));
  if(q.reason) extra.push(adaptiveText(q.reason,''));
  return label+(extra.length?' · '+extra.join(' · '):'');
}

export function adaptiveTimelineTune(value){
  var tuning=typeof value==='string'?{status:value}:adaptiveJsonObject(value), key=String(tuning.status||tuning.state||'not_run').toLowerCase();
  var active=['applied','effective','active'].indexOf(key)>=0;
  var inFlight=['running','in_progress','processing','queued','pending','retrying'].indexOf(key)>=0;
  var label=(tuning.in_progress||tuning.running)?'调参中 · 尚未生效':(active?'已生效':(inFlight?'调参中 · 尚未生效':({shadow_proposal:'影子建议',proposal_only:'候选待审',eligible_auto_adjust:'候选待审',hold:'维持当前',no_change:'无变更',blocked:'已阻断',blocked_quality:'质量阻断',blocked_cross_source:'跨源阻断',cooldown:'冷却中',disabled:'未启用',failed:'调参失败',not_run:'未运行'})[key]||adaptiveText(tuning.status||tuning.state,'待运行')));
  if(active&&tuning.human_confirmed===false) label='候选待人工确认';
  return {label:label,raw:key,proposal:adaptiveText(tuning.reason||tuning.summary,'')};
}

export function adaptiveTimelineHuman(value){
  var human=typeof value==='string'?{status:value}:adaptiveJsonObject(value), key=String(human.status||human.state||'not_required').toLowerCase();
  return ({approved:'已确认',confirmed:'已确认',accepted:'已确认',required:'需要确认',pending:'待人工确认',waiting:'待人工确认',rejected:'已拒绝',not_required:'无需确认'})[key]||adaptiveText(human.status||human.state,'待记录');
}

export function adaptiveTimelineHtml(payload){
  var root=payload||{}, items=adaptiveTimelineRows(root), status=adaptiveTimelineStatus(root.status||'ok');
  if(!items.length) return '<div class="adaptive-ai-timeline-empty" style="padding:14px;border:1px dashed var(--border);border-radius:10px;color:var(--text-muted)">'+(root.message||'当前交易日尚无分时段 AI 分析记录；完成时段任务后会在这里显示。')+'</div>';
  var windowNames={premarket:'盘前',auction:'集合竞价',open:'开盘确认','open-confirm':'开盘确认',morning:'上午',noon:'午间',afternoon:'午后','risk-review':'风险复核','close-risk':'收盘风控',close:'收盘',adversarial:'对抗复核',manual:'手动运行'};
  var cards=items.slice(0,40).map(function(item){
    var row=item.row,detail=item.detail,nested=item.nested;
    var rawStatus=row.status||detail.status||nested.status||'unknown', label=windowNames[row.window||row.slot||row.phase]||adaptiveText(row.label||row.window||row.slot,'分时段分析');
    var quality=row.data_quality||row.quality||detail.data_quality||detail.quality||nested.data_quality||{};
    var route=row.model_route||row.modelRoute||row.route||detail.model_route||detail.model_route_info||nested.model_route||{};
    var routeText=typeof route==='string'?route:([route.provider||route.vendor,route.model||route.model_id,route.route||route.reason].filter(Boolean).join(' · ')||'规则快照 / AI路由未记录');
    var evidence=row.evidence_hash||row.evidenceHash||detail.evidence_hash||nested.evidence_hash||adaptiveJsonObject(row.evidence).evidence_hash||adaptiveJsonObject(detail.evidence).evidence_hash||'未记录';
    var shadow=row.shadow_recommendation||row.shadow||detail.shadow_recommendation||detail.shadow||nested.shadow_recommendation||{};
    var tune=adaptiveTimelineTune(row.auto_tuning||row.ai_tuning||row.tuning||detail.auto_tuning||detail.ai_tuning||nested.auto_tuning||{});
    var human=adaptiveTimelineHuman(row.human_confirmation||row.humanConfirmation||detail.human_confirmation||nested.human_confirmation||{});
    var retries=Number(row.retry_count==null?(row.retries==null?(detail.retry_count==null?0:detail.retry_count):row.retries):row.retry_count)||0;
    var attempts=Number(row.attempts==null?(detail.attempts==null?0:detail.attempts):row.attempts)||0;
    var time=String(row.finished_at||row.updated_at||row.started_at||row.created_at||'').replace('T',' ').replace(/[+]\d\d:\d\d$/,'').slice(0,19)||'时间未记录';
    var retryable=['failed','error','retrying'].indexOf(String(rawStatus).toLowerCase())>=0||row.retryable===true;
    var retry=retryable?'<button class="ghost" style="padding:5px 9px;font-size:11px" onclick="retryAdaptiveAiWindow(\''+encodeURIComponent(String(row.window||row.slot||'manual'))+'\')">重试</button>':'';
    return '<article class="adaptive-ai-timeline-card" style="padding:13px;border:1px solid var(--border);border-radius:11px;background:var(--surface);display:grid;gap:8px">'
      +'<header style="display:flex;justify-content:space-between;gap:10px;align-items:center"><div><b>'+adaptiveEsc(label)+'</b><small style="display:block;color:var(--text-muted)">'+adaptiveEsc(time)+'</small></div><span class="tag '+(String(rawStatus).toLowerCase()==='completed'?'tag-ok':(retryable?'tag-warn':'tag-info'))+'">'+adaptiveEsc(adaptiveTimelineStatus(rawStatus))+'</span></header>'
      +'<div style="display:grid;grid-template-columns:repeat(auto-fit,minmax(150px,1fr));gap:7px;font-size:12px"><span><small>重试</small><b>'+retries+' 次'+(attempts?' · '+attempts+' 次尝试':'')+'</b></span><span><small>数据质量</small><b>'+adaptiveEsc(adaptiveTimelineQuality(quality))+'</b></span><span><small>模型路由</small><b>'+adaptiveEsc(routeText)+'</b></span><span><small>证据哈希</small><b title="'+adaptiveEsc(String(evidence))+'">'+adaptiveEsc(String(evidence).length>20?String(evidence).slice(0,10)+'…'+String(evidence).slice(-8):String(evidence))+'</b></span><span><small>影子建议</small><b>'+adaptiveEsc(typeof shadow==='string'?shadow:(shadow.status||shadow.summary||shadow.recommendation||'未生成'))+'</b></span><span><small>自动微调</small><b>'+adaptiveEsc(tune.label)+'</b>'+(tune.proposal?'<small style="display:block;color:var(--text-muted)">'+adaptiveEsc(tune.proposal)+'</small>':'')+'</span><span><small>人工确认</small><b>'+adaptiveEsc(human)+'</b></span></div>'
      +(row.error||detail.error?'<p style="margin:0;color:#a33;font-size:12px">'+adaptiveEsc(row.error||detail.error)+'</p>':'')
      +(retry?'<footer style="display:flex;justify-content:flex-end">'+retry+'</footer>':'')
      +'</article>';
  }).join('');
  return '<div style="display:grid;gap:10px"><div style="font-size:12px;color:var(--text-muted)">'+adaptiveEsc(root.trade_date||root.date||adaptiveLocalDate())+' · '+adaptiveEsc(status)+' · '+items.length+' 个时段</div>'+cards+'</div>';
}

export function renderAdaptiveTimeline(payload){
  var target=$('adaptiveAiTimelineContent'); if(target) target.innerHTML=adaptiveTimelineHtml(payload||{});
  var badge=$('adaptiveAiTimelineStatus'); if(badge) badge.textContent=(payload&&payload.status==='unavailable')?'接口未部署':((payload&&payload.status)||'已读取');
}

export async function refreshAdaptiveTimeline(base){
  var fallback=(base&& (base.ai_analysis_timeline||base.ai_timeline||base.timeline))||((base&&base.runs)?{runs:base.runs,trade_date:base.trade_date||base.date}:{});
  if(fallback&&typeof fallback==='object') renderAdaptiveTimeline(fallback);
  try{
    var day=adaptiveLocalDate(),timeline=await api('/api/adaptive/ai/timeline?trade_date='+encodeURIComponent(day)+'&limit=40&_='+Date.now());
    if(timeline&&timeline.status!=='unavailable') renderAdaptiveTimeline(timeline);
  }catch(e){
    if(!fallback||!adaptiveTimelineRows(fallback).length) renderAdaptiveTimeline({status:'error',message:'时间线暂不可用，可点击刷新或稍后重试。'});
  }
}

export async function retryAdaptiveAiWindow(encodedWindow){
  var windowName='manual'; try{windowName=decodeURIComponent(encodedWindow||'manual');}catch(ignore){}
  var confirmation=await adaptiveConfirm({title:'重试分时段 AI 分析',detail:'将重新生成该时段的确定性快照与影子建议。',boundary:'不会下单、不会直接应用 AI 调参；结果仍需人工确认。'}); if(!confirmation.approved) return;
  try{await apiPost('/api/adaptive/ai/analyze?trigger=manual-retry&window='+encodeURIComponent(windowName)+'&scope=all&confirmed=true');await refreshAdaptiveTimeline(window._adaptiveOverviewPayload||{});}
  catch(e){adaptiveActionNotice('分时段 AI 分析重试失败',e.message);}
}

export function refreshAdaptiveTimelineNow(){ refreshAdaptiveTimeline(window._adaptiveOverviewPayload||{}); }

export function setAdaptiveSection(section,button){
  var shell=document.querySelector('#adaptiveResult .adaptive-shell');
  var result=document.querySelector('#adaptiveResult');
  document.querySelectorAll('#p-adaptive .adaptive-section-tab').forEach(function(item){
    var active=item.dataset.section===section;
    item.classList.toggle('active',active);
    item.setAttribute('aria-selected',active?'true':'false');
  });
  sessionStorage.setItem('astock.adaptiveSection',section);
  if(!shell) return;
  /* Fallback: 如果 AI section 不存在，动态创建 */
  if(section==='ai' && !shell.querySelector('[data-adaptive-section="ai"]')){
    var aiHtml='<section class="adaptive-panel adaptive-ai-section" data-adaptive-section="ai">'
      +'<header><div><span>DUAL AI CONSENSUS</span><h3>AI审阅与调参</h3></div>'
      +'<div class="adaptive-advisor-actions">'
      +'<button class="ghost" onclick="loadEvolutionStatus()">刷新状态</button>'
      +'<button class="ghost" onclick="triggerEvolution()">手动触发进化</button>'
      +'<button class="ghost" onclick="runDualAiTuning()">运行双AI调参</button>'
      +'</div></header>'
      +'<div class="ai-status-cards" id="aiStatusCards">'
      +'<div class="ai-card"><small>MiMo</small><b id="aiMiMoStatus">检测中…</b></div>'
      +'<div class="ai-card"><small>DeepSeek</small><b id="aiDeepSeekStatus">检测中…</b></div>'
      +'<div class="ai-card"><small>modlens</small><b id="aiModlensStatus">检测中…</b></div>'
      +'<div class="ai-card"><small>共识率</small><b id="aiConsensusRate">—</b></div>'
      +'<div class="ai-card"><small>进化状态</small><b id="aiEvolutionState">检测中…</b></div>'
      +'<div class="ai-card"><small>参数版本</small><b id="aiParamsVersion">—</b></div>'
      +'</div>'
      +'<div class="ai-params-panel"><h4>当前进化参数</h4><div id="aiParamsTable">加载中…</div></div>'
      +'<div class="ai-runs-panel"><h4>最近双AI调参记录</h4><div id="aiRunsTable">加载中…</div></div>'
      +'<div class="ai-metrics-panel"><h4>调参性能指标</h4><div id="aiMetricsGrid">加载中…</div></div>'
      +'<div class="ai-log-panel"><h4>进化事件日志</h4><div id="aiLogTable">加载中…</div></div>'
      +'<div class="ai-modlens-panel"><h4>modlens 视觉测试</h4>'
      +'<div class="controls"><input id="aiImageUrl" type="text" placeholder="输入图片URL" style="width:400px">'
      +'<button class="ghost" onclick="testModlensRead()">读取</button></div>'
      +'<div id="aiModlensResult"></div></div>'
      +'<div class="adaptive-notice">双AI共识：MiMo + DeepSeek 独立分析，方向一致且幅度接近才执行。自进化系统自动优化调参策略。</div>'
      +'</section>';
    shell.insertAdjacentHTML('beforeend',aiHtml);
  }
  shell.querySelectorAll('[data-adaptive-section]').forEach(function(node){
    node.hidden=node.dataset.adaptiveSection!==section;
    if(section==='ai' && node.dataset.adaptiveSection==='research') node.hidden=false;
  });
  /* AI section 独立显示时，隐藏所有非 ai 的 section */
  if(section==='ai'){
    shell.querySelectorAll('[data-adaptive-section]').forEach(function(node){
      if(node.dataset.adaptiveSection!=='ai') node.hidden=true;
    });
    var aiNode=shell.querySelector('[data-adaptive-section="ai"]');
    if(aiNode) aiNode.hidden=false;
  }
  var first=shell.querySelector('[data-adaptive-section="'+section+'"]');
  if(first && result && button && button.dataset.section===section){
    result.dataset.activeSection=section;
  }
  /* 自动加载 AI section 数据 */
  if(section==='ai' && typeof loadEvolutionStatus==='function'){
    loadEvolutionStatus();
  }
}

export function renderAdaptive(d){
  window._adaptiveOverviewPayload=d||{};
  var engine=d.engine||{},profile=d.market_profile||{},features=profile.features||{},drivers=profile.drivers||[];
  var decision=d.decision||{},weights=decision.weights||{},evidence=((decision.evidence||{}).strategies)||{};
  var alpha=d.alpha_lab||{},alphaRun=alpha.latest_run||{},alphaDetail=alphaRun.detail||{};
  var neural=d.neural_control||alpha.neural_control||{},neuralReady=!!((neural.readiness||{}).admitted),neuralApproved=!!neural.approved;
  var riskOpt=d.risk_optimizer||{},riskCandidates=riskOpt.candidates||[],riskClosure=riskOpt.closure||{},deepseek=d.deepseek_advisor||riskOpt.deepseek_advisor||{};
  var selectionOpt=d.selection_optimizer||{},selectionCandidates=selectionOpt.candidates||[];
  var newsLearning=d.news_learning||{},newsTotals=newsLearning.totals||{},newsFactor=newsLearning.factor||{},newsGates=newsFactor.gates||{};
  var tradeAttribution=d.trade_attribution||{},tradeAttrAccounts=tradeAttribution.by_account||{};
  var closedLoop=d.closed_loop||{},chain=closedLoop.evidence_chain||{},admissionWindow=closedLoop.admission_window||{},legacyDebt=closedLoop.legacy_debt||{},canaryLimits=closedLoop.limits||{};
  var portfolioShadow=d.portfolio_shadow||{},portfolioSelected=portfolioShadow.selected||[];
  var dataInputs=d.data_inputs||{},dataCategories=dataInputs.categories||[];
  var dynamicRisk=d.dynamic_risk||{},dynamicNews=dynamicRisk.news||{},dynamicMode=dynamicRisk.mode||'normal';
  var dynamicCodes=Object.keys(dynamicNews.codes||{}).slice(0,8).map(function(code){var item=dynamicNews.codes[code]||{};return code+' '+(item.verified_negative?'公告否决':'舆情收紧');}).join('、')||'暂无受影响个股';
  var dynamicRiskNotice='<div class="adaptive-dynamic-risk '+adaptiveEsc(dynamicMode)+'"><b>统一动态风控 · '+adaptiveEsc(dynamicRisk.label||'正常')+'</b><span>'+adaptiveEsc(dynamicRisk.reason||'风控中心尚未生成动态状态')+'</span><small>新增风险额度 '+adaptiveValue(dynamicRisk.risk_scale_pct,'%',1)+' · 负面事件 '+Number(dynamicNews.negative_count||0)+' 条 · 核验负面 '+Number(dynamicNews.verified_negative_count||0)+' 条 · 影响标的 '+adaptiveEsc(dynamicCodes)+'</small></div>';
  var adaptiveTimelineFallback=d.ai_analysis_timeline||d.ai_timeline||((d.runs||[]).length?{runs:d.runs,trade_date:d.trade_date||d.date}:{});
  var adaptiveTimelinePanel='<section class="adaptive-panel adaptive-ai-timeline-panel"><header><div><span>TODAY · AI ANALYSIS TIMELINE</span><h3>今日分时段 AI 分析时间线</h3></div><div class="adaptive-advisor-actions"><em id="adaptiveAiTimelineStatus">读取中</em><button class="ghost" onclick="refreshAdaptiveTimelineNow()">刷新时间线</button></div></header><p class="adaptive-copy">每个时段先固化确定性快照，再记录模型路由、数据质量、证据哈希与影子建议。AI 调参在运行中或待人工确认时不会显示为已生效。</p><div id="adaptiveAiTimelineContent"><div class="loading">正在读取今日 AI 分析记录…</div></div></section>';
  window._adaptiveDecisionId=decision.id||null;
  $('adaptiveModeBadge').textContent=engine.mode_label||'影子学习';
  $('adaptiveModeBadge').className='adaptive-lock-badge '+adaptiveStageClass(engine.stage);
  var architecture=[
    {code:'01',name:'模拟盘账本',copy:'净值、成交、回撤与1/3/5日兑现组成唯一调参证据。',state:Number(engine.mature_reward_count||0)+' 个成熟奖励'},
    {code:'02',name:'盘面画像',copy:'把资金流、价格动量、波动、情绪与拥挤度转成上下文。',state:profile.quality==='valid_close'?'已更新':'数据降级'},
    {code:'03',name:'模拟盘选股进化',copy:'小步调整三账户内部因子权重与入场阈值，不影响公共选股。',state:adaptiveText(selectionOpt.mode,'等待样本')},
    {code:'04',name:'模拟盘风控进化',copy:'分级调整仓位与损失预算；自动调整受限，版本可回滚。',state:adaptiveText(riskOpt.mode,'受约束')}
  ].map(function(x){return '<article class="adaptive-flow-step"><span>'+x.code+'</span><div><b>'+x.name+'</b><p>'+x.copy+'</p></div><em>'+x.state+'</em></article>';}).join('');
  var driverRows=drivers.map(function(x,index){var tone=index===0?'capital':(index===1?'momentum':(index===4?'risk':''));return '<div class="adaptive-driver"><div><b>'+x.name+'</b><strong>'+adaptiveValue(x.score,'',1)+'</strong></div>'+adaptiveBar(x.score,tone)+'<p>'+x.detail+'</p></div>';}).join('')||'<div class="paper-empty">尚无有效盘面画像。</div>';
  var sectorGroup=function(rows,kind){return (rows||[]).map(function(x){return '<li><div><b>'+x.name+'</b><small>'+x.sample_count+' 只 · 中位 '+pctTxt(x.median_pct)+'</small></div><strong class="'+(x.main_net_yi>=0?'up':'down')+'">'+(x.main_net_yi>=0?'+':'')+fmt(x.main_net_yi,1)+' 亿</strong></li>';}).join('')||'<li class="adaptive-empty-row">暂无可用板块样本</li>';};
  var strategyCards=Object.keys(weights).map(function(id){var item=evidence[id]||{},weight=weights[id]||0,latest=(item.horizons||[]).filter(function(x){return x.samples>0;})[0]||{};return '<article class="adaptive-strategy-card '+id+'"><header><div><span>Bandit arm</span><h3>'+adaptiveEsc((item.name)||id)+'</h3></div><strong>'+fmt(weight,1)+'%</strong></header>'+adaptiveBar(weight,'allocation')+'<div class="adaptive-strategy-stats"><div><small>成熟样本</small><b>'+Number(item.samples||0)+'</b></div><div><small>当前盘面样本</small><b>'+Number(item.regime_samples||0)+'</b></div><div><small>最近可用超额</small><b class="'+pctCls(latest.mean_excess_pct)+'">'+adaptiveValue(latest.mean_excess_pct,'%',2)+'</b></div></div><p>后验 '+adaptiveValue(item.posterior_mean,'',3)+' · 探索奖励 '+adaptiveValue(item.exploration_bonus,'',3)+' · 风险扣分 '+adaptiveValue(item.downside_penalty,'',3)+'</p><div class="adaptive-human-actions"><button class="ghost" onclick="recordAdaptiveFeedback(\''+id+'\',\'approve\')">人工认可</button><button class="ghost" onclick="recordAdaptiveFeedback(\''+id+'\',\'watch\')">继续观察</button></div></article>';}).join('')||'<div class="paper-empty">尚未生成 Bandit 影子权重。</div>';
  var horizons={};(d.horizon_summary||[]).forEach(function(x){(horizons[x.account_id]||(horizons[x.account_id]=[])).push(x);});
  var horizonRows=Object.keys(horizons).map(function(id){var rows=horizons[id];return '<tr><td><b>'+adaptiveEsc(rows[0].name)+'</b></td>'+[1,3,5].map(function(h){var x=rows.filter(function(r){return r.horizon===h;})[0]||{};return '<td><span class="adaptive-sample-chip">'+Number(x.samples||0)+' 样本</span><br><b class="'+pctCls(x.mean_excess_pct)+'">'+adaptiveValue(x.mean_excess_pct,'%',2)+'</b></td>';}).join('')+'</tr>';}).join('');
  var guardrails=(d.guardrails||[]).map(function(x){return '<li><span class="adaptive-guard-state '+x.status+'"></span><div><b>'+x.name+'</b><p>'+x.detail+'</p></div><em>'+x.status+'</em></li>';}).join('');
  var runRows=(d.runs||[]).slice(0,8).map(function(x){var detail=x.detail||{};var isObservation=x.status==='intraday_observation';var isAdvisor=x.status==='advisor_batch'||x.status==='advisor_skipped';var isSuccess=x.status==='completed'||isObservation||isAdvisor;var title=isObservation?'午间观测已保存':(isAdvisor?(x.status==='advisor_batch'?'AI审阅已完成':'AI审阅已跳过'):(x.status==='completed'?'学习账本已更新':'任务失败'));var extra=isObservation?(' · 已保存样本 '+Number(detail.sample_rows||0)+' 行 · 仅观测未调参'):(isAdvisor?(detail.reason==='completed'?' · 已完成候选挑战':''):(detail.alpha_lab_status?' · GA '+detail.alpha_lab_status:''));return '<li><time>'+String(x.finished_at||'').replace('T',' ').slice(5,16)+'</time><div><b>'+title+'</b><p>'+((detail.regime?'盘面 '+detail.regime+' · ':'')+'新增成熟奖励 '+Number(x.new_rewards||0)+extra)+'</p></div><span class="tag '+(isSuccess?'tag-ok':'tag-warn')+'">'+x.trigger+'</span></li>';}).join('')||'<li class="adaptive-empty-row">尚无学习运行记录</li>';
  var alphaProgress=Math.min(100,100*Math.min((alpha.profile_days||0)/Math.max(alpha.required_profile_days||1,1),(alpha.mature_rows||0)/Math.max(alpha.required_mature_rows||1,1)));
  var candidateRows=(alpha.candidates||[]).map(function(x){var genome=x.genome||{},top=Object.keys(genome).sort(function(a,b){return Math.abs(genome[b])-Math.abs(genome[a]);}).slice(0,3).map(function(k){return k+' '+(genome[k]>=0?'+':'')+fmt(genome[k],2);}).join(' · ');return '<li><div><b>'+top+'</b><p>验证适应度 '+fmt(x.validation_fitness,3)+' · 多空差 '+fmt(x.validation_spread_pct,3)+'%</p></div><span class="tag '+(x.status==='shadow_candidate'?'tag-ok':'tag-warn')+'">'+x.status+'</span></li>';}).join('')||'<li class="adaptive-empty-row">达到 '+Number(alpha.required_profile_days||10)+' 个画像日后才启动遗传迭代，不生成伪 Alpha。</li>';
  var riskStatus={waiting_data:'等待数据',shadow_candidate:'影子验证',deployment_observing:'上一版本观察中',eligible_auto_tighten:'可保守晋级',human_review_required:'等待人工批准',applied:'已生效',rolled_back:'已回滚',no_change:'维持当前'};
  // 内部版本号如 risk-evo-20260811-49 不直接作为唯一说明：页面始终同时展示
  // 第几版、账本实际写入时间、实际开始生效日与应用来源，避免把两种日期混为一谈。
  var adaptiveVersionInfo=function(item){
    var meta=item.meta||{},raw=String(item.version||''),match=raw.match(/-(\d{8})-(\d+)$/),generated='记录缺失',revision='—';
    if(match){revision='第 '+Number(match[2])+' 版';}
    var created=String(item.created_at||meta.applied_at||'').replace('T',' ').replace(/\+\d\d:\d\d$/,'');
    if(created){generated=created.replace(/^(\d{4})-(\d{2})-(\d{2})/, '$1年$2月$3日').slice(0,16);}
    else if(match){generated=match[1].slice(0,4)+'年'+match[1].slice(4,6)+'月'+match[1].slice(6,8)+'日（编号日期）';}
    var effective=String(meta.effective_date||item.effective_date||'').slice(0,10);
    if(/^\d{4}-\d{2}-\d{2}$/.test(effective)){effective=effective.slice(0,4)+'年'+effective.slice(5,7)+'月'+effective.slice(8,10)+'日';}
    else {effective='尚未记录';}
    var source={human:'人工确认',manual:'人工确认','bounded-auto':'系统自动（受限）','conservative-auto':'系统自动（保守）',auto:'系统自动'}[String(meta.approved_by||item.approved_by||'').toLowerCase()]||String(meta.approved_by||item.approved_by||'系统');
    return {raw:raw||'基准版本',revision:revision,generated:generated,effective:effective,source:source};
  };
  var riskParamLabel={max_exposure:'总仓上限',max_weight:'单股上限',max_industry:'行业上限',single_risk:'单笔风险',daily_loss:'单日熔断',drawdown:'回撤熔断',cooldown_days:'冷静期',min_cost_edge:'成本边际'};
  var downsidePolicy=riskOpt.downside_policy||{};
  var downsideAccountNames={tq_breakout:'短线日内做T',trend_pullback:'趋势波段优选',sector_rotation:'板块轮动先锋',reported_profit_breakout:'三日策略',main_force_top10:'超强主力股'};
  var downsideDefaults=downsidePolicy.defaults||{};
  var downsidePolicyRows=Object.keys(downsideDefaults).filter(function(id){return downsideAccountNames[id];}).map(function(id){var p=downsideDefaults[id]||{},pick=function(short,long){return Number(p[short]==null?p[long]:p[short])||0;};return '<div><b>'+adaptiveEsc(downsideAccountNames[id]||id)+'</b><span>预警 '+fmt(pick('downside_warning_pct','warning_pct'),1)+'% · 部分 '+fmt(pick('downside_partial_pct','partial_pct'),1)+'% · 强制 '+fmt(pick('downside_full_pct','full_pct'),1)+'%</span><small>相对大盘 '+fmt(pick('downside_relative_pct','relative_pct'),1)+'% · 峰值回撤 '+fmt(pick('downside_peak_retrace_pct','peak_retrace_pct'),1)+'% · 部分比例 '+fmt(pick('downside_partial_ratio','partial_ratio')*100,0)+'%</small></div>';}).join('')||'<div><b>等待风控策略基准</b><span>下一次风险评估后生成三段式阈值</span></div>';
  var downsideNotice='<div class="adaptive-notice adaptive-downside-notice"><b>下跌防线 · '+adaptiveEsc(downsidePolicy.engine||'主力意图 + 三段式下跌防线')+'</b><span>连续 '+Number(downsidePolicy.confirmation_scans||2)+' 次确认后才执行部分/强制减仓；自进化只在事件≥3、确认出货占比≥60%、奖励窗口为负时保守收紧，放宽必须人工批准。</span></div>';
  var riskCandidateRows=riskCandidates.slice(0,3).map(function(x){var ev=x.evidence||{},gates=ev.gates||{},dg=ev.downside_guard||{},base=x.baseline_params||{},next=x.candidate_params||{};var gateRows=['nav_days','trade_events','reward_samples','regime_count'].map(function(k){var gate=gates[k]||{};return '<span class="'+(gate.passed?'pass':'wait')+'">'+({nav_days:'净值日',trade_events:'交易事件',reward_samples:'奖励',regime_count:'盘面'}[k])+' '+Number(gate.current||0)+'/'+Number(gate.required||0)+'</span>';}).join('');var params=['max_exposure','max_weight','daily_loss','drawdown'].map(function(k){return '<div><small>'+riskParamLabel[k]+'</small><b>'+fmt((base[k]||0)*100,1)+'% → '+fmt((next[k]||0)*100,1)+'%</b></div>';}).join('');var guardSummary='<div class="adaptive-downside-evidence"><div><small>下跌防线样本</small><b>'+Number(dg.events||0)+' 事件 · '+Number(dg.confirmed_events||0)+' 已确认</b></div><span>预警 '+Number(dg.warning_events||0)+' · 部分 '+Number(dg.partial_events||0)+' · 强制 '+Number(dg.full_events||0)+'</span><span>疑似出货 '+Number(dg.distribution_events||0)+' · 洗盘 '+Number(dg.washout_events||0)+' · 成交退出 '+Number(dg.filled_exits||0)+'</span></div>';var action=x.status==='human_review_required'?'<button class="ghost" onclick="applyAdaptiveRiskCandidate('+x.id+')">人工批准</button>':'';return '<article class="adaptive-risk-card '+x.status+'"><header><div><span>'+x.account_name+'</span><h4>'+riskStatus[x.status]+'</h4></div><strong>'+(({waiting:'待1日影子',fast_shadow:'1日快速影子',micro:'3日小步调整',standard:'5日标准验证',mature:'10日成熟确认'})[ev.evolution_tier]||ev.evolution_tier||'等待')+'</strong></header><div class="adaptive-risk-params">'+params+'</div><div class="adaptive-risk-gates">'+gateRows+'</div>'+guardSummary+'<p>'+x.reason+'</p>'+action+'</article>';}).join('')||'<div class="paper-empty">运行一次模拟盘学习后生成各策略的风控候选。</div>';
  var activeRiskRows=(riskOpt.active_versions||[]).map(function(x){var version=adaptiveVersionInfo(x);return '<li><div><b>'+x.account_name+' · '+version.revision+'</b><p>版本生成：'+version.generated+' · 开始生效：'+version.effective+' · '+version.source+'</p><small>内部编号：'+adaptiveEsc(version.raw)+' · 候选 '+String((x.meta||{}).candidate_id||'—')+'</small></div><button class="ghost" onclick="rollbackAdaptiveRisk(\''+x.account_id+'\')">回滚</button></li>';}).join('')||'<li class="adaptive-empty-row">当前没有自进化风控覆盖，继续使用策略基准风控。</li>';
  var closureFlow=(riskClosure.flow||[]).map(function(x,i){return '<span><b>'+String(i+1).padStart(2,'0')+'</b>'+adaptiveEsc(x)+'</span>'+(i<(riskClosure.flow||[]).length-1?'<i></i>':'');}).join('');
  var deploymentNames={observing:'观察中',validated:'验证通过',review_required:'暂停复核',rollback_required:'需要回滚',rolled_back:'已回滚',superseded:'已迭代'};
  var deploymentRows=(riskClosure.deployments||[]).map(function(x){var post=x.post_metrics||{};return '<li class="'+adaptiveEsc(x.status)+'"><div><span>'+adaptiveEsc(x.account_name||x.account_id)+'</span><b>'+adaptiveEsc(deploymentNames[x.status]||x.status)+'</b><p>'+adaptiveEsc(x.risk_version)+' · '+Number(x.observation_days||0)+'/5 净值日 · 归因订单 '+Number(post.attributed_orders||0)+'</p><small>'+adaptiveEsc(x.reason||'等待结果回写')+'</small></div><em>'+adaptiveValue(post.version_wiring_pct,'%',1)+'</em></li>';}).join('')||'<li class="adaptive-empty-row">尚无已生效的风控进化版本；订单与结果账本已经开始积累。</li>';
  var selectionStatus={waiting_data:'等待数据',shadow_candidate:'影子候选',eligible_auto_adjust:'可自动微调',human_review_required:'等待人工确认',applied:'已生效',rolled_back:'已回滚',no_change:'维持当前'};
  var factorNames={mom_short:'短周期动量',mom:'中期动量',flow:'资金流',volsurge:'量能',sentiment:'情绪/板块',value:'估值',quality:'质量',rsi:'超跌修复'};
  var selectionCandidateRows=selectionCandidates.slice(0,3).map(function(x){var ev=x.evidence||{},gates=ev.gates||{},base=x.baseline_params||{},next=x.candidate_params||{},oldW=base.weights||{},newW=next.weights||{};var changes=Object.keys(newW).sort(function(a,b){return Math.abs((newW[b]||0)-(oldW[b]||0))-Math.abs((newW[a]||0)-(oldW[a]||0));}).slice(0,3).map(function(k){return '<div><small>'+factorNames[k]+'</small><b>'+fmt((oldW[k]||0)*100,1)+'% → '+fmt((newW[k]||0)*100,1)+'%</b></div>';}).join('');var gateRows=['nav_days','trade_events','reward_samples','regime_count'].map(function(k){var gate=gates[k]||{};return '<span class="'+(gate.passed?'pass':'wait')+'">'+({nav_days:'净值日',trade_events:'交易事件',reward_samples:'奖励',regime_count:'盘面'}[k])+' '+Number(gate.current||0)+'/'+Number(gate.required||0)+'</span>';}).join('');var action=x.status==='human_review_required'?'<button class="ghost" onclick="applyAdaptiveSelectionCandidate('+Number(x.id||0)+')">人工确认并应用</button>':'';return '<article class="adaptive-selection-card '+x.status+'"><header><div><span>'+x.account_name+' · '+x.model_id+'</span><h4>'+selectionStatus[x.status]+'</h4></div><strong>'+(({waiting:'待1日影子',fast_shadow:'1日快速影子',micro:'3日小步调整',standard:'5日标准验证',mature:'10日成熟确认'})[x.tier]||x.tier)+'</strong></header><div class="adaptive-selection-weights">'+changes+'</div><div class="adaptive-selection-delta"><span>入场评分偏移</span><b>'+fmt((base.entry_score_delta||0),3)+' → '+fmt((next.entry_score_delta||0),3)+'</b></div><div class="adaptive-risk-gates">'+gateRows+'</div><p>'+x.reason+'</p>'+action+'</article>';}).join('')||'<div class="paper-empty">运行一次模拟盘学习后生成内部选股候选。</div>';
  var activeSelectionRows=(selectionOpt.active_versions||[]).map(function(x){var version=adaptiveVersionInfo(x);return '<li><div><b>'+x.account_name+' · '+version.revision+'</b><p>版本生成：'+version.generated+' · 开始生效：'+version.effective+' · '+version.source+'</p><small>内部编号：'+adaptiveEsc(version.raw)+' · '+String((x.meta||{}).tier||'')+'</small></div><button class="ghost" onclick="rollbackAdaptiveSelection(\''+x.account_id+'\')">回滚</button></li>';}).join('')||'<li class="adaptive-empty-row">当前没有选股进化覆盖，继续使用模拟盘基准因子权重。</li>';
  var newsGateNames={mature_5d_events:'5日成熟事件',event_dates:'独立事件日',source_grades:'来源等级'};
  var newsGateRows=Object.keys(newsGates).map(function(k){var x=newsGates[k]||{};return '<span class="'+(x.passed?'pass':'wait')+'"><b>'+adaptiveEsc(newsGateNames[k]||k)+'</b>'+Number(x.current||0)+' / '+Number(x.required||0)+'</span>';}).join('')||'<span class="wait"><b>学习门禁</b>等待首次运行</span>';
  var newsEvents=(newsLearning.events||[]).slice(0,8).map(function(x){var url=adaptiveSafeUrl(x.source_url);return '<li><span class="news-grade grade-'+adaptiveEsc(String(x.evidence_grade||'D').toLowerCase())+'">'+adaptiveEsc(x.evidence_grade||'D')+'</span><div><b>'+adaptiveEsc(x.title||'未命名事件')+'</b><p>'+adaptiveEsc(x.code)+' · '+adaptiveEsc(x.event_type)+' · 首次看到 '+adaptiveEsc(String(x.first_seen_at||'').replace('T',' ').slice(5,16))+'</p></div>'+(url==='#'?'<em>'+Number(x.outcome_count||0)+'/3</em>':'<a href="'+url+'" target="_blank" rel="noopener noreferrer">证据 ↗</a>')+'</li>';}).join('')||'<li class="adaptive-empty-row">尚未捕获与模拟盘相关的公告或快讯。</li>';
  var newsSources=(newsLearning.sources||[]).map(function(x){return '<div><span>'+adaptiveEsc(x.evidence_grade)+'级</span><b>'+adaptiveEsc(x.source_name)+'</b><strong>'+fmt(x.credibility_score,1)+'</strong><small>链接 '+fmt(x.linked_pct,0)+'% · 去重 '+fmt(x.unique_pct,0)+'%</small></div>';}).join('')||'<div class="adaptive-empty-row">来源信誉将在首次采集后生成。</div>';
  var newsPolicy=newsLearning.collection_policy||{},newsPool=newsLearning.pool||{},newsPoolCounts=newsPool.counts||{},majorRadar=newsLearning.major_radar||{};
  var poolTierNames={holding:'持仓',pending_signal:'待执行',active_candidate:'候选前15',near_candidate:'观察16–30'};
  var poolTierCards=['holding','pending_signal','active_candidate','near_candidate'].map(function(k){return '<div><small>'+poolTierNames[k]+'</small><b>'+Number(newsPoolCounts[k]||0)+'</b></div>';}).join('');
  var newsSchedule=(newsPolicy.times||['08:15','12:15','18:45']).map(function(t,i){return '<span><b>'+adaptiveEsc(t)+'</b>'+(['盘前','午间','盘后'][i]||'增量')+'</span>';}).join('');
  var majorEvents=(majorRadar.events||[]).slice(0,8).map(function(x){var url=adaptiveSafeUrl(x.source_url),themes=(x.themes||[]).map(function(t){return '<span>'+adaptiveEsc(t.label||t.id)+'</span>';}).join('');return '<li><div class="major-event-score"><b>'+fmt((x.significance_score||0)*100,0)+'</b><small>重要度</small></div><div><b>'+adaptiveEsc(x.title||'重大市场事件')+'</b><p>'+adaptiveEsc(x.event_type)+' · 首次看到 '+adaptiveEsc(String(x.first_seen_at||'').replace('T',' ').slice(5,16))+' · 关联候选 '+Number(x.candidate_links||0)+'</p><div class="major-event-themes">'+themes+'</div></div>'+(url==='#'?'<em>'+adaptiveEsc(x.verification_status||'待核验')+'</em>':'<a href="'+url+'" target="_blank" rel="noopener noreferrer">来源 ↗</a>')+'</li>';}).join('')||'<li class="adaptive-empty-row">尚未捕获达到重大事件阈值的市场新闻。</li>';
  var rebalanceRows=(selectionOpt.active_versions||[]).map(function(x){var version=adaptiveVersionInfo(x);return '<li><div><b>'+adaptiveEsc(x.account_name||x.account_id)+' · '+adaptiveEsc(version.revision)+'</b><p>版本生成：'+adaptiveEsc(version.generated)+' · 开始生效：'+adaptiveEsc(version.effective)+' · '+adaptiveEsc(version.source)+'</p><small>内部编号：'+adaptiveEsc(version.raw)+'。回滚将恢复该策略上一版参数。</small></div><button class="ghost" onclick="rollbackAdaptiveRebalance(\''+adaptiveEsc(x.account_id)+'\')">回滚到上一版</button></li>';}).join('')||'<li class="adaptive-empty-row">当前没有已生效的调仓版本。</li>';
  var advisorRuns=deepseek.latest_by_purpose||{},advisorLatest=advisorRuns.data_quality||deepseek.latest||{},advisorReport=advisorLatest.report||{},advisorEvidence=advisorLatest.evidence||{},advisorMarket=advisorEvidence.market_snapshot||{},advisorLedger=advisorEvidence.paper_ledger||{},crossSource=advisorMarket.cross_source||{};
  var advisorReady=deepseek.enabled&&deepseek.configured;
  var advisorState=advisorLatest.status==='completed'?'审阅完成':(advisorLatest.status==='failed'?'最近调用失败':(advisorReady?'已接入 · 等待首次审阅':'未启用'));
  var aiTuning=deepseek.realtime_tuning||{},aiLatest=aiTuning.latest||{};
  var aiTuningState=aiLatest.status==='applied'?'已同日应用':(aiLatest.status==='hold'?'AI建议维持':(aiLatest.status==='cooldown'?'冷却中':(aiLatest.status||'等待运行')));
  var severityNames={critical:'严重',high:'高',medium:'中',low:'低',info:'正常'};
  var advisorFindings=(advisorReport.findings||[]).map(function(x){return '<li class="'+adaptiveEsc(x.severity||'info')+'"><div><span>'+adaptiveEsc(severityNames[x.severity]||x.severity||'提示')+'</span><b>'+adaptiveEsc(x.title||'待复核项')+'</b></div><p>'+adaptiveEsc(x.evidence||'')+'</p><small>'+adaptiveEsc(x.recommended_action||'')+'</small></li>';}).join('')||'<li class="adaptive-empty-row">'+(advisorLatest.status==='failed'?'调用未成功：'+adaptiveEsc(advisorLatest.error_code||'provider_error'):(advisorLatest.status==='completed'?'本次跨源与账本审阅未发现需要升级的问题。':'尚无 DeepSeek 审阅结论；确定性检查仍独立运行。'))+'</li>';
  var deterministicCount=(advisorEvidence.deterministic_findings||[]).length;
  var crossSourceLabel=advisorReport.cross_source_status==='verified'?'已验证':(advisorReport.cross_source_status==='partial'?'部分通过':'未通过');
  var researchCards=(deepseek.tasks||[]).map(function(task){
    var latest=advisorRuns[task.purpose]||{},report=latest.report||{},taskEvidence=latest.evidence||{};
    var topFinding=(report.findings||[])[0]||{};
    var eventLinks=task.purpose==='event_evidence'?(taskEvidence.events||[]).slice(0,3).map(function(event){return '<a href="'+adaptiveSafeUrl(event.source_url)+'" target="_blank" rel="noopener noreferrer"><span>'+adaptiveEsc(event.evidence_grade||'C')+'</span><b>'+adaptiveEsc(event.summary||event.title||'来源事件')+'</b></a>';}).join(''):'';
    var state=latest.status==='completed'?'已完成':(latest.status==='failed'?'调用失败':'等待首次运行');
    return '<article class="adaptive-research-card '+adaptiveEsc(task.purpose)+'"><header><div><span>'+adaptiveEsc(task.purpose.replace(/_/g,' '))+'</span><h4>'+adaptiveEsc(task.label)+'</h4></div><em>'+state+'</em></header><p>'+adaptiveEsc(report.summary||task.short)+'</p><div class="adaptive-research-meta"><span>置信度 <b>'+adaptiveValue(report.confidence,'%',0)+'</b></span><span>发现 <b>'+Number((report.findings||[]).length)+'</b></span><span>耗时 <b>'+(latest.latency_ms==null?'—':Number(latest.latency_ms)+'ms')+'</b></span></div>'+(topFinding.title?'<div class="adaptive-research-finding"><span>'+adaptiveEsc(topFinding.severity||'info')+'</span><b>'+adaptiveEsc(topFinding.title)+'</b><p>'+adaptiveEsc(topFinding.evidence||'')+'</p></div>':'')+(eventLinks?'<div class="adaptive-event-links">'+eventLinks+'</div>':'')+'<button class="ghost" onclick="runAdaptiveResearchTask(\''+adaptiveEsc(task.purpose)+'\',this)">单独运行</button></article>';
  }).join('')||'<div class="paper-empty">研究任务尚未加载。</div>';
  var tradeAttrAccountCards=Object.keys(tradeAttrAccounts).filter(function(id){return downsideAccountNames[id];}).map(function(id){var x=tradeAttrAccounts[id]||{};return '<div><small>'+adaptiveEsc(downsideAccountNames[id]||id)+'</small><b>'+Number(x.filled||0)+' 笔</b><span>个股 '+adaptiveValue(x.mean_stock_move_pct,'%',2)+' · 超额 '+adaptiveValue(x.mean_alpha_pct,'%',2)+'</span><em>公告偏空 '+Number(x.negative_news_records||0)+' 笔 · AI '+Number(x.ai_completed||0)+' 笔</em></div>';}).join('')||'<div class="adaptive-empty-row">盘后收盘后生成逐笔归因。</div>';
  var tradeAttrRows=(tradeAttribution.recent||[]).filter(function(x){return x.order_status==='filled';}).slice(0,8).map(function(x){var reason=adaptiveJsonArray(x.reason_codes).join('、');return '<li><time>'+adaptiveEsc(String(x.fill_date||'').slice(5,10))+'</time><div><b>'+adaptiveEsc(x.name||x.code)+' '+adaptiveEsc(x.code)+'</b><p>'+adaptiveEsc(({tq_breakout:'短线日内做T',trend_pullback:'趋势波段优选',sector_rotation:'板块轮动先锋',reported_profit_breakout:'三日策略'})[x.account_id]||x.account_id)+' · '+adaptiveEsc(x.side==='buy'?'买入':'卖出')+' '+Number(x.qty||0)+'股 · 成交 '+fmt(x.fill_price,2)+' · 收盘 '+fmt(x.close_price,2)+'</p><small>'+adaptiveEsc(reason||'暂无足够证据')+' · 大盘 '+adaptiveValue(x.benchmark_move_pct,'%',2)+' · 个股超额 '+adaptiveValue(x.stock_alpha_pct,'%',2)+'</small></div><em class="tag '+(x.ai_status==='completed'?'tag-ok':'tag-warn')+'">'+adaptiveEsc(x.ai_status==='completed'?'AI已归因':'规则归因')+'</em></li>';}).join('')||'<li class="adaptive-empty-row">当天没有已成交操作。</li>';
  var tradeAttrPanel='<section class="adaptive-panel trade-attribution-panel"><header><div><span>TRADE → REASON → LEARNING</span><h3>盘后逐笔涨跌归因</h3></div><em>'+Number(tradeAttribution.records||0)+' 条记录</em></header><p class="adaptive-copy">每个收盘任务先计算个股涨跌、大盘拖累、板块贡献、公告/舆情和行情质量，再批量调用 AI 做可审计解释；AI 结论只进入自进化证据，不直接下单。</p><div class="adaptive-grid trade-attribution-summary">'+tradeAttrAccountCards+'</div><ul class="adaptive-run-log trade-attribution-list">'+tradeAttrRows+'</ul></section>';
  var closedLoopStages=(closedLoop.timeline||[]).map(function(x){var active=String(x.stage)===String(closedLoop.stage);return '<div class="closed-loop-stage '+(active?'active':'')+'"><b>'+adaptiveEsc(x.stage)+'</b><span>'+adaptiveEsc(x.mode)+'</span><em>'+Number(x.nav_pct||0)+'% 资金</em></div>';}).join('');
  var closedLoopBlockers=(closedLoop.blockers||[]).map(function(x){return '<li>'+adaptiveEsc(x)+'</li>';}).join('')||'<li class="pass">当前阶段的确定性门禁已通过</li>';
  var closedLoopPanel='<section class="adaptive-panel closed-loop-panel"><header><div><span>DATA → ALPHA → PORTFOLIO → RISK → EXECUTION → FEEDBACK</span><h3>量化闭环准入台</h3></div><em>'+adaptiveEsc(closedLoop.stage||'D1-D3')+' · '+adaptiveEsc(closedLoop.mode==='shadow'?'影子运行':closedLoop.mode||'影子运行')+'</em></header><p class="adaptive-copy">先把信号、风控、委托、成交和结果串成同一证据链，再让自适应模块按 0% → 5% → 10% 的模拟资金逐级接管。日期到了但证据不达标不会强行放权。</p><div class="closed-loop-stages">'+closedLoopStages+'</div><div class="closed-loop-kpis"><div><small>历史证据链委托</small><b>'+Number(chain.orders||0)+'</b></div><div><small>新窗口关联率</small><b>'+adaptiveValue(admissionWindow.link_pct,'%',1)+'</b></div><div><small>新窗口完整率</small><b>'+adaptiveValue(admissionWindow.valid_pct,'%',1)+'</b></div><div><small>实际 / 反事实</small><b>'+Number(chain.actual||0)+' / '+Number(chain.counterfactual||0)+'</b></div><div><small>策略成交覆盖</small><b>'+Number(closedLoop.strategy_coverage||0)+' / 5 当前账户</b></div><div><small>灰度硬上限</small><b>'+Number(canaryLimits.max_nav_pct||10)+'% · '+Number(canaryLimits.max_new_slots||2)+'槽</b></div></div><div class="closed-loop-gates"><h4>当前阻断项</h4><ul>'+closedLoopBlockers+'</ul><small>历史债务：未完整关联 '+Number(legacyDebt.unlinked_orders||0)+' 条；只保留审计，不计入新闭环准入。</small></div><div class="adaptive-notice">未成交、风控拒绝、容量延期只进入反事实账本，不再混入真实 Bandit 收益。GA、神经网络和 DeepSeek 在本阶段只能生成影子研究证据，不能直接控制订单。</div></section>';
  var portfolioRows=portfolioSelected.map(function(x){return '<article><div><span>'+adaptiveEsc(x.account_name)+'</span><b>'+adaptiveEsc(x.name||x.code)+' '+adaptiveEsc(x.code)+'</b><small>'+adaptiveEsc(x.industry||'未知行业')+' · '+adaptiveEsc((x.reasons||[]).join('；'))+'</small></div><strong>'+fmt(x.utility,1)+'</strong></article>';}).join('')||'<div class="paper-empty">当前没有可进入组合比较的新增候选。</div>';
  var portfolioPanel='<section class="adaptive-panel portfolio-shadow-panel"><header><div><span>PORTFOLIO ARBITER · SHADOW</span><h3>跨策略组合裁决</h3></div><em>影子运行 · 不改变订单</em></header><p class="adaptive-copy">已启用策略继续独立选股；组合层只在共享资金池里比较边际效用，并对同股重复、行业集中和容量延期扣分。当前最多展示 '+Number(portfolioShadow.max_canary_slots||2)+' 个灰度候选。</p><div class="portfolio-shadow-kpis"><span>候选 <b>'+Number(portfolioShadow.candidate_count||0)+'</b></span><span>当前持仓槽 <b>'+Number(portfolioShadow.held_slots||0)+'</b></span><span>重复代码 <b>'+Number(portfolioShadow.duplicate_code_count||0)+'</b></span></div><div class="portfolio-shadow-list">'+portfolioRows+'</div><div class="adaptive-notice">该裁决器不会因为 Bandit 权重变化强制卖出现有持仓；T+1、双源行情、82%总暴露和原策略风控仍拥有最终否决权。</div></section>';
  var dataCards=dataCategories.map(function(x){var coverage=x.coverage_pct==null?'分项统计':fmt(x.coverage_pct,1)+'%';var freshness=x.freshness_minutes==null?'':(' · 延迟 '+fmt(x.freshness_minutes,1)+'分钟');return '<article class="data-input-card '+adaptiveEsc(x.status||'partial')+'"><header><div><span>'+adaptiveEsc(x.id||'data')+'</span><h4>'+adaptiveEsc(x.name)+'</h4></div><em>'+adaptiveEsc(({usable:'可用于交易',shadow:'仅影子',partial:'部分可用',blocked:'禁止新增'})[x.status]||x.status)+'</em></header><div class="data-input-metric"><b>'+coverage+'</b><small>'+Number(x.records||0)+' 条/行'+freshness+'</small></div><p>'+adaptiveEsc(x.detail||'')+'</p><div class="data-input-sources">'+(x.sources||[]).map(function(s){return '<span>'+adaptiveEsc(s)+'</span>';}).join('')+'</div><small>'+adaptiveEsc(x.authority||'')+'</small></article>';}).join('');
  var dataBlockers=(dataInputs.blockers||[]).map(function(x){return '<li>'+adaptiveEsc(x)+'</li>';}).join('')||'<li class="pass">五类输入均达到当前使用门槛</li>';
  var dataInputPanel='<section class="adaptive-panel data-input-panel"><header><div><span>DATA INPUT BUS · QUALITY GATES</span><h3>五类数据输入总线</h3></div><em>'+adaptiveEsc(dataInputs.version||'data-input-bus-v1')+'</em></header><p class="adaptive-copy">全面不等于把所有字段都接进来，而是每类数据都要有来源、覆盖率、源时间、降级状态和明确使用权限。缺失数据不会静默用代理值冒充。</p><div class="data-input-grid">'+dataCards+'</div><div class="data-input-bottom"><div><h4>当前数据缺口</h4><ul>'+dataBlockers+'</ul></div><div><h4>输入纪律</h4><ul>'+(dataInputs.rules||[]).map(function(x){return '<li>'+adaptiveEsc(x)+'</li>';}).join('')+'</ul></div></div></section>';
  var neuralBlockers=(neural.readiness&&neural.readiness.blockers||[]).map(function(x){return '<li>'+adaptiveEsc(x)+'</li>';}).join('')||'<li class="pass">样本门槛已满足，可申请人工确认</li>';
  var neuralPanel='<section class="adaptive-panel neural-control-panel"><header><div><span>NEURAL SHADOW · HUMAN GATE</span><h3>神经网络候选评分</h3></div><em class="'+(neuralApproved?'tag-ok':'tag-warn')+'">'+adaptiveEsc(({shadow_only:'影子运行',approval_waiting_data:'已申请 · 等待数据',approved_bounded_shadow:'人工确认 · 有界影子',disabled:'已停用'})[neural.status]||'影子运行')+'</em></header><p class="adaptive-copy">当前对已启用策略集合做候选排序对照，最多影响排序分 '+fmt(neural.max_rank_adjustment||0,3)+'；不直接下单、不绕过行情双源、板块权限、仓位、T+1或风控卖出。</p><div class="neural-gate-kpis"><div><small>特征样本</small><b>'+Number((neural.readiness||{}).feature_rows||0)+'</b></div><div><small>标签样本</small><b>'+Number((neural.readiness||{}).label_rows||0)+'</b></div><div><small>独立盘面日</small><b>'+Number((neural.readiness||{}).profile_days||0)+' / '+Number((neural.readiness||{}).requirements&&neural.readiness.requirements.min_profile_days||60)+'</b></div><div><small>可用周期</small><b>'+adaptiveEsc(((neural.readiness||{}).available_horizons||[]).join('/')||'—')+'</b></div></div><ul class="adaptive-guardrails neural-blockers">'+neuralBlockers+'</ul>'+(neuralReady&&!neuralApproved?'<button class="ghost" onclick="approveAdaptiveNeural()">人工确认，启用有界影子评分</button>':'')+(neuralApproved?'<div class="adaptive-notice">已确认：仅作为排序副分，硬门禁仍由原策略和风控最终决定。</div>':'<div class="adaptive-notice">未满足样本外门槛前，按钮不会放权；当前结果只记录在自进化证据中。</div>')+'</section>';
  $('adaptiveResult').innerHTML='<div class="adaptive-shell">'
    +closedLoopPanel
    +adaptiveTimelinePanel
    +dataInputPanel
    +neuralPanel
    +portfolioPanel
    +'<section class="adaptive-hero"><div><span class="adaptive-eyebrow">'+(profile.profile_date||'等待首个画像')+' · '+(profile.quality||'not ready')+'</span><h3>'+((profile.regime&&({momentum:'资金共振 · 动量扩张',rotation:'板块轮动 · 结构分化',risk_off:'风险收缩 · 资金退潮',high_volatility:'高波动 · 拥挤博弈',balanced:'均衡震荡 · 等待确认'}[profile.regime]))||'尚未识别盘面')+'</h3><p>'+engine.principle+'</p></div><div class="adaptive-hero-metrics"><div><small>成熟奖励</small><b>'+Number(engine.mature_reward_count||0)+'</b></div><div><small>学习置信度</small><b>'+adaptiveValue(((decision.evidence||{}).summary||{}).confidence_pct,'%',1)+'</b></div><div><small>自进化阶段</small><b>'+engine.stage_label+'</b></div></div></section>'
    +'<section class="adaptive-flow" aria-label="自进化流程">'+architecture+'</section>'
    +'<div class="adaptive-grid"><section class="adaptive-panel adaptive-profile-panel"><header><div><span>MARKET TRANSFORMER</span><h3>盘面画像</h3></div><em>'+Number(profile.valid_rows||0)+' 个有效样本</em></header><div class="adaptive-drivers">'+driverRows+'</div></section><section class="adaptive-panel adaptive-sector-panel"><header><div><span>CAPITAL FLOW</span><h3>资金流方向</h3></div><em>只作代理证据</em></header><div class="adaptive-sector-columns"><div><h4>资金共振方向</h4><ul>'+sectorGroup(features.top_sectors,'up')+'</ul></div><div><h4>资金减弱方向</h4><ul>'+sectorGroup(features.weak_sectors,'down')+'</ul></div></div></section></div>'
    +'<section class="adaptive-panel adaptive-selection-evolution"><header><div><span>PAPER SELECTION EVOLUTION</span><h3>模拟盘选股进化</h3></div><em>'+adaptiveEsc(adaptiveText(selectionOpt.mode,'等待样本'))+'</em></header><p class="adaptive-copy">'+adaptiveEsc(adaptiveText(selectionOpt.policy,'等待选股进化证据汇总。'))+'</p><div class="adaptive-tier-track"><span><b>3日</b>快速影子</span><span><b>5日</b>明显微调</span><span><b>10日</b>标准进化</span><span><b>20日</b>成熟进化</span></div><div class="adaptive-selection-layout"><div class="adaptive-selection-candidates">'+selectionCandidateRows+'</div><aside class="adaptive-active-risk"><h4>已生效选股版本</h4><ul>'+activeSelectionRows+'</ul></aside></div></section>'
    +'<section class="adaptive-panel adaptive-risk-evolution"><header><div><span>PAPER RISK EVOLUTION</span><h3>模拟盘风控进化</h3></div><em>'+adaptiveEsc(adaptiveText(riskOpt.mode,'等待样本'))+'</em></header><p class="adaptive-copy">'+adaptiveEsc(adaptiveText(riskOpt.policy,'等待风控进化证据汇总。'))+'</p>'+downsideNotice+'<div class="adaptive-downside-policy"><header><b>当前已启用策略防线基准</b><span>只读展示；参数变更仍受版本、影子观察和人工放权约束</span></header>'+downsidePolicyRows+'</div><div class="adaptive-tier-track"><span><b>3日</b>快速影子</span><span><b>5日</b>明显微调</span><span><b>10日</b>标准进化</span><span><b>20日</b>完整受限区间</span></div><div class="adaptive-risk-layout"><div class="adaptive-risk-candidates">'+riskCandidateRows+'</div><aside class="adaptive-risk-side"><div class="adaptive-advisor-card"><span>AI EVIDENCE REVIEWER</span><h4>DeepSeek 数据审阅</h4><b class="'+(advisorReady?'on':'off')+'">'+adaptiveEsc(advisorState)+'</b><p>'+adaptiveEsc(adaptiveText(deepseek.truth_boundary,'证据解释器，不是真实性证明。'))+'</p></div><div class="adaptive-active-risk"><h4>已生效风控版本</h4><ul>'+activeRiskRows+'</ul></div></aside></div></section>'
    +'<section class="adaptive-panel news-learning-panel"><header><div><span>EVENT → OUTCOME → CALIBRATION</span><h3>统一情报与事件学习</h3></div><div class="adaptive-advisor-actions"><em>'+(newsLearning.mode==='paper_micro_eligible'?'有界微调资格':'影子学习')+'</em><button id="newsLearningRunButton" class="ghost" onclick="runNewsLearning()">运行新闻学习</button></div></header><p class="adaptive-copy">风控中心与自进化共用同一份新闻/公告事件账本；风控负责实时门禁，自进化负责1/3/5日兑现校准。</p>'+dynamicRiskNotice+'<div class="news-learning-flow"><span><b>01</b>采集去重</span><i></i><span><b>02</b>事件分型</span><i></i><span><b>03</b>1/3/5日兑现</span><i></i><span><b>04</b>来源校准</span><i></i><span><b>05</b>模拟盘微调</span></div><div class="news-kpis"><div><small>事件账本</small><b>'+Number(newsTotals.events||0)+'</b></div><div><small>可追溯链接</small><b>'+adaptiveValue(newsTotals.linked_pct,'%',1)+'</b></div><div><small>成熟结果</small><b>'+Number(newsTotals.mature_outcomes||0)+'</b></div><div><small>5日成熟事件</small><b>'+Number(newsTotals.mature_5d_events||0)+'</b></div></div><div class="news-learning-layout"><div><h4>最近进入账本</h4><ul class="news-event-list">'+newsEvents+'</ul></div><aside><h4>来源信誉（不使用涨跌评分）</h4><div class="news-source-list">'+newsSources+'</div><h4>微调门禁</h4><div class="news-gates">'+newsGateRows+'</div></aside></div><div class="adaptive-notice">'+adaptiveEsc(newsLearning.authority||'当前仅影子记录。')+'</div></section>'
    +'<section class="adaptive-panel adaptive-advisor-evidence"><header><div><span>DEEPSEEK · DATA QUALITY + TUNING</span><h3>模拟盘数据校验与有界调参</h3></div><div class="adaptive-advisor-actions"><em>'+adaptiveEsc(deepseek.model||'deepseek-v4-flash')+'</em><button id="advisorRunButton" class="ghost" onclick="runAdaptiveAdvisor()" '+(advisorReady?'':'disabled')+'>运行数据质量审阅</button><button id="adaptiveAiTuneInlineButton" class="ghost" onclick="runAdaptiveAiTuning()" '+(advisorReady&&aiTuning.enabled?'':'disabled')+'>运行AI有界调参</button></div></header><div class="adaptive-advisor-summary"><div><small>数据审阅</small><b>'+adaptiveEsc(advisorState)+'</b></div><div><small>AI调参状态</small><b>'+adaptiveEsc(aiTuningState)+'</b></div><div><small>确定性异常</small><b>'+Number(deterministicCount)+'</b></div><div><small>审阅置信度</small><b>'+adaptiveValue(advisorReport.confidence,'%',0)+'</b></div><div><small>跨源真实性</small><b class="'+(advisorReport.cross_source_status==='verified'?'up':'down')+'">'+crossSourceLabel+'</b></div><div><small>双源覆盖 / 一致</small><b>'+adaptiveValue(crossSource.coverage_pct,'%',1)+' / '+adaptiveValue(crossSource.agreement_pct,'%',1)+'</b></div></div><div class="adaptive-advisor-report"><div><h4>审阅摘要</h4><p>'+adaptiveEsc(advisorReport.summary||deepseek.truth_boundary||'DeepSeek只复核确定性证据；行情真实性仍需独立数据源交叉验证。')+'</p><small>市场状态：'+(advisorMarket.session_status==='closed'?'已收盘':'交易中')+' · 收盘口径 '+adaptiveEsc(String(advisorMarket.close_cutoff_at||'—').replace('T',' '))+' · 源行情最后到达 '+adaptiveEsc(String(advisorMarket.latest_source_at||'—').replace('T',' '))+' · 最近审阅 '+adaptiveEsc(String(advisorLatest.finished_at||'—').replace('T',' '))+'</small></div><ul>'+advisorFindings+'</ul></div><div class="adaptive-notice">AI只可在已启用的模拟策略内提出白名单权重、入场阈值和选股条件的小步补丁；系统先做行情质量、跨源、幅度、冷却和回滚校验，再允许盘中同日生效。AI不能下单、修改公共选股或放宽风控；超出边界的建议只留在影子候选中。</div></section>'
    +'<section class="adaptive-panel adaptive-research-suite"><header><div><span>DEEPSEEK · PAPER RESEARCH SUITE</span><h3>模拟盘智能研究任务</h3></div><button id="advisorSuiteButton" class="ghost" onclick="runAdaptiveResearchSuite()" '+(advisorReady?'':'disabled')+'>运行全部研究任务</button></header><p class="adaptive-copy">收盘后自动运行；每项独立留痕。结论只能进入研究和人工复核，不能直接改选股、风控或订单。</p><div class="adaptive-research-grid">'+researchCards+'</div></section>'
    +'<section class="adaptive-panel"><header><div><span>CONTEXTUAL BANDIT</span><h3>策略集合影子分配</h3></div><em>总和 100% · 不改变账户资金</em></header><div class="adaptive-strategy-grid">'+strategyCards+'</div>'+allocationActionPanel(d)+'<div class="adaptive-notice">'+adaptiveEsc(d.data_note||'')+'</div></section>'
    +'<section class="adaptive-panel"><header><div><span>EVOLUTION A/B · VERSION ATTRIBUTION</span><h3>进化版本对照归因</h3></div><em>部署后 5 净值日 vs 部署前等长基线</em></header>'+abValidationPanel(d)+'</section>'
    +'<div class="adaptive-grid"><section class="adaptive-panel"><header><div><span>GENETIC ALGORITHM</span><h3>GA Alpha 实验室</h3></div><em>非神经网络</em></header><p class="adaptive-copy">'+alpha.architecture+'</p><div class="adaptive-progress-copy"><span>画像日 '+Number(alpha.profile_days||0)+' / '+Number(alpha.required_profile_days||10)+'</span><span>成熟标签 '+Number(alpha.mature_rows||0)+' / '+Number(alpha.required_mature_rows||5000)+'</span></div>'+adaptiveBar(alphaProgress,'ga')+'<ul class="adaptive-alpha-list">'+candidateRows+'</ul></section><section class="adaptive-panel"><header><div><span>MULTI-HORIZON REWARD</span><h3>策略周期兑现</h3></div><em>1日 20% · 3日 35% · 5日 45%</em></header><div class="table-scroll"><table class="adaptive-horizon-table"><thead><tr><th>策略</th><th>1日超额</th><th>3日超额</th><th>5日超额</th></tr></thead><tbody>'+horizonRows+'</tbody></table></div></section></div>'
    +'<div class="adaptive-grid"><section class="adaptive-panel"><header><div><span>RISK GATE</span><h3>放权门槛</h3></div><em>默认全部锁定</em></header><ul class="adaptive-guardrails">'+guardrails+'</ul></section><section class="adaptive-panel"><header><div><span>AUDIT LOG</span><h3>真实学习日志</h3></div><em>不展示虚构迭代数</em></header><ul class="adaptive-run-log">'+runRows+'</ul></section></div>'
    +'</div>';
  var newsPanel=document.querySelector('#adaptiveResult .news-learning-panel');
  if(newsPanel){newsPanel.insertAdjacentHTML('beforebegin',tradeAttrPanel);}
  if(newsPanel){newsPanel.insertAdjacentHTML('beforebegin','<section class="adaptive-panel risk-closure-panel"><header><div><span>ORDER · RISK · OUTCOME · EVOLUTION</span><h3>模拟盘下单风控闭环</h3></div><em>'+adaptiveEsc(riskClosure.stage==='observing'?'部署观察中':'证据账本运行中')+'</em></header><div class="risk-closure-flow">'+closureFlow+'</div><div class="risk-closure-kpis"><div><small>已归因委托</small><b>'+Number(riskClosure.orders_attributed||0)+'</b></div><div><small>决策关联率</small><b>'+adaptiveValue(riskClosure.decision_link_pct,'%',1)+'</b></div><div><small>风险载荷完整率</small><b>'+adaptiveValue(riskClosure.payload_complete_pct,'%',1)+'</b></div><div><small>执行完整率</small><b>'+adaptiveValue(riskClosure.execution_integrity_pct,'%',1)+'</b></div></div><div class="risk-closure-layout"><div><h4>风控版本部署观察</h4><ul class="risk-deployment-list">'+deploymentRows+'</ul></div><aside><h4>闭环判断边界</h4><p>'+adaptiveEsc(riskClosure.rollback_policy||'技术接线故障自动回滚；效果变化需要人工复核。')+'</p><div class="adaptive-notice">每笔委托绑定当时生效的风控版本；成交、拒绝、容量延期、退出和已实现盈亏按日回写。满5个净值日后才能验证版本，不使用未来数据。</div></aside></div></section>');}
  var rebalancePanel=document.querySelector('#adaptiveResult .adaptive-rebalance-panel');
  if(!rebalancePanel){
    var shellForRebalance=document.querySelector('#adaptiveResult .adaptive-shell');
    if(shellForRebalance){shellForRebalance.insertAdjacentHTML('afterbegin','<section class="adaptive-panel adaptive-rebalance-panel"><header><div><span>PAPER REBALANCE · ROLLBACK</span><h3>模拟盘调仓管理</h3></div><div class="adaptive-advisor-actions"><em>'+adaptiveEsc(aiTuningState)+'</em><button class="ghost" onclick="runAdaptiveAiTuning()" '+(advisorReady&&aiTuning.enabled?'':'disabled')+'>进入AI调仓</button></div></header><p class="adaptive-copy">调仓只作用于模拟盘策略参数；每个策略独立记录版本，发生异常可单独回滚，不影响其他账户、公共选股和历史成交。</p><ul class="adaptive-run-log">'+rebalanceRows+'</ul></section>');}
  }
  var advisorPanel=document.querySelector('#adaptiveResult .adaptive-advisor-evidence');
  if(advisorPanel){advisorPanel.insertAdjacentHTML('beforebegin','<section class="adaptive-panel intelligence-scope-panel"><header><div><span>CANDIDATE SCOPE · MARKET RADAR</span><h3>候选池情报与重大事件雷达</h3></div><em>当前 '+Number(newsPool.size||0)+' 只标的</em></header><div class="news-scope-top"><div><h4>模拟盘候选范围</h4><div class="news-pool-kpis">'+poolTierCards+'</div><p>'+adaptiveEsc(newsPolicy.candidate_scope||'持仓、待执行信号与策略候选分级采集')+'；快照有效期 '+adaptiveEsc(newsPolicy.snapshot_ttl||'两个交易日')+'。</p></div><div><h4>每日三次增量采集</h4><div class="news-schedule">'+newsSchedule+'</div><p>普通新闻不做全市场逐股抓取；重大政策、产业、流动性和系统性风险独立扫描。</p></div></div><div class="major-radar-layout"><div><h4>最近重大事件</h4><ul class="major-event-list">'+majorEvents+'</ul></div><aside><h4>使用边界</h4><p>'+adaptiveEsc(majorRadar.authority||'单一来源重大事件只作上下文，不直接影响模拟盘。')+'</p><div class="adaptive-notice">主题映射只说明“可能相关”，不等于因果确认。必须通过第二行情源、后续价格兑现和样本门禁，才可能进入有界参数评估。</div></aside></div></section>');}
  var shell=document.querySelector('#adaptiveResult .adaptive-shell');
  if(shell){
    var mark=function(selector,section){shell.querySelectorAll(selector).forEach(function(node){node.dataset.adaptiveSection=section;});};
    mark('.adaptive-hero,.adaptive-flow,.adaptive-profile-panel,.adaptive-sector-panel','overview');
    mark('.adaptive-ai-timeline-panel','overview');
    mark('.adaptive-selection-evolution,.adaptive-rebalance-panel','selection');
    mark('.adaptive-risk-evolution,.risk-closure-panel','risk');
    mark('.intelligence-scope-panel,.news-learning-panel','news');
    mark('.adaptive-advisor-evidence,.adaptive-research-suite','research');
    /* ─── AI审阅与调参 section ─── */
    var aiSectionHtml='<section class="adaptive-panel adaptive-ai-section" data-adaptive-section="ai">'
      +'<header><div><span>DUAL AI CONSENSUS · SELF-EVOLUTION</span><h3>AI审阅与调参</h3></div>'
      +'<div class="adaptive-advisor-actions">'
      +'<button class="ghost" onclick="loadEvolutionStatus()">刷新状态</button>'
      +'<button class="ghost" onclick="triggerEvolution()">手动触发进化</button>'
      +'<button class="ghost" onclick="runDualAiTuning()">运行双AI调参</button>'
      +'</div></header>'
      /* 双AI状态卡片 */
      +'<div class="ai-status-cards" id="aiStatusCards">'
      +'<div class="ai-card"><small>MiMo v2.5 Pro</small><b id="aiMiMoStatus">检测中…</b></div>'
      +'<div class="ai-card"><small>DeepSeek</small><b id="aiDeepSeekStatus">检测中…</b></div>'
      +'<div class="ai-card"><small>modlens 视觉</small><b id="aiModlensStatus">检测中…</b></div>'
      +'<div class="ai-card"><small>共识率</small><b id="aiConsensusRate">—</b></div>'
      +'<div class="ai-card"><small>进化状态</small><b id="aiEvolutionState">检测中…</b></div>'
      +'<div class="ai-card"><small>参数版本</small><b id="aiParamsVersion">—</b></div>'
      +'</div>'
      /* 进化参数面板 */
      +'<div class="ai-params-panel">'
      +'<h4>当前进化参数</h4>'
      +'<div id="aiParamsTable" class="ai-params-grid">加载中…</div>'
      +'</div>'
      /* 最近调参记录 */
      +'<div class="ai-runs-panel">'
      +'<h4>最近双AI调参记录</h4>'
      +'<div id="aiRunsTable"><div class="loading">加载中…</div></div>'
      +'</div>'
      /* 性能指标 */
      +'<div class="ai-metrics-panel">'
      +'<h4>调参性能指标</h4>'
      +'<div class="ai-metrics-grid" id="aiMetricsGrid">加载中…</div>'
      +'</div>'
      /* 进化日志 */
      +'<div class="ai-log-panel">'
      +'<h4>进化事件日志</h4>'
      +'<div id="aiLogTable"><div class="loading">加载中…</div></div>'
      +'</div>'
      /* modlens 图片测试 */
      +'<div class="ai-modlens-panel">'
      +'<h4>modlens 视觉测试</h4>'
      +'<div class="controls"><input id="aiImageUrl" type="text" placeholder="输入图片URL或本地路径" style="width:400px">'
      +'<button class="ghost" onclick="testModlensRead()">读取图片</button></div>'
      +'<div id="aiModlensResult" style="margin-top:10px"></div>'
      +'</div>'
      +'<div class="adaptive-notice">双AI共识调参：MiMo + DeepSeek 独立分析同一份市场证据，双方提案方向一致且幅度接近时才合并执行。自进化系统根据历史表现自动调整调参策略参数。modlens 为纯文本模型提供视觉能力。</div>'
      +'</section>';
    shell.insertAdjacentHTML('beforeend', aiSectionHtml);

    var grids=shell.querySelectorAll(':scope > .adaptive-grid');
    if(grids[0]) grids[0].dataset.adaptiveSection='overview';
    /* AI section: 复用 research 的内容，但添加 AI 双共识面板 */
    if(grids[1]) grids[1].dataset.adaptiveSection='model';
    if(grids[2]) grids[2].dataset.adaptiveSection='model';
    /* Every direct content block belongs to exactly one tab.  This prevents
       trailing evidence panels from leaking into another subsection. */

    /* AI section 标记 */
    mark('.adaptive-ai-section','ai');
  shell.querySelectorAll(':scope > *').forEach(function(node){
      if(!node.dataset.adaptiveSection) node.dataset.adaptiveSection='model';
    });
    setAdaptiveSection(sessionStorage.getItem('astock.adaptiveSection')||'overview');
  }
  refreshAdaptiveTimeline(adaptiveTimelineFallback);
}

export async function loadAdaptive(){
  if(window._adaptiveLoading) return;
  window._adaptiveLoading=true;
  var snapshotKey='astock.adaptiveOverview.v3';
  var cached=null;
  try{cached=JSON.parse(sessionStorage.getItem(snapshotKey)||'null');}catch(ignore){}
  if(adaptiveOverviewComplete(cached)) renderAdaptive(cached);
  else if($('adaptiveResult')) $('adaptiveResult').innerHTML='<div class="loading">正在读取模拟盘选股、风控与学习账本…</div>';
  try{
    // The evolution view is a live operational surface.  A browser-cached
    // bootstrap response can omit the newly completed advisor review and make
    // a configured service look "未启用".  Always bypass the HTTP cache here;
    // the API itself retains a short coherent read cache and never blocks the UI.
    var adaptiveData=await api('/api/adaptive/overview?_='+Date.now());
    // A stale payload from a just-restarted backend can still contain an old
    // engine shell but not the advisor block.  Do not render it as “未启用”.
    if(!adaptiveOverviewComplete(adaptiveData)){
      $('adaptiveResult').innerHTML='<div class="loading">'+(adaptiveData.message||'正在后台生成自进化快照…')+'</div>';
      window.setTimeout(loadAdaptive,800);
      return;
    }
    renderAdaptive(adaptiveData);
    try{sessionStorage.setItem(snapshotKey,JSON.stringify(adaptiveData));}catch(ignore){}
  }
  catch(e){$('adaptiveResult').innerHTML='<div class="banner">自进化中心加载失败：'+e.message+'</div>';}
  finally{window._adaptiveLoading=false;}
}

export function adaptiveOverviewComplete(data){
  return !!(data&&data.engine&&data.risk_optimizer&&data.selection_optimizer&&data.deepseek_advisor&&data.neural_control);
}

export async function runAdaptive(){
  var confirmation=await adaptiveConfirm({title:'运行一次模拟盘学习',detail:'将更新各模拟策略的研究、逐笔归因和候选证据。',boundary:'不会新增模拟买卖，不会放宽任何风控。'}); if(!confirmation.approved) return;
  var button=$('adaptiveRunButton'); if(button) button.disabled=true;
  try{
    var request=await apiPost('/api/adaptive/run?trigger=manual-ui&confirmed=true');
    if(!request||['accepted','busy'].indexOf(request.status)<0) throw new Error((request&&request.message)||'学习任务未启动');
    if(button) button.textContent=request.status==='accepted'?'学习运行中…':'已有学习运行中…';
    for(var i=0;i<120;i++){
      await new Promise(function(resolve){window.setTimeout(resolve,5000);});
      var state=await api('/api/adaptive/run/status?_='+Date.now());
        var stillRunning=['queued','claimed','running'].indexOf(state.status)>=0||state.running===true;
        if(!stillRunning){
          if(state.status==='failed'||state.error) throw new Error(state.error||'后台学习失败');
          await loadAdaptive();
          break;
      }
    }
  }
  catch(e){adaptiveActionNotice('模拟盘学习失败',e.message);}
  finally{if(button){button.disabled=false;button.textContent='运行模拟盘学习';}}
}

export async function approveAdaptiveNeural(){
  var confirmation=await adaptiveConfirm({title:'批准神经网络影子评分',detail:'神经网络只作为三个短线策略的候选排序参考。',boundary:'不会绕过行情双源、仓位、T+1 或风控门禁；不会直接下单。'}); if(!confirmation.approved) return;
  try{renderAdaptive(await apiPost('/api/adaptive/neural/approve?confirmed=true'));}
  catch(e){adaptiveActionNotice('神经网络仍未达到放权门槛',e.message);}
}

/* ─── AI审阅与调参 JavaScript 函数 ─── */
export async function loadEvolutionStatus(){
  try{
    /* 加载双AI状态 */
    var dualAi=await api('/api/adaptive/dual-ai/status');
    var keys=dualAi.providers||{};
    var mimo=keys.mimo||{};
    var ds=keys.deepseek||{};
    setEl('aiMiMoStatus', mimo.configured?'已配置 '+adaptiveEsc(mimo.model||''):'未配置');
    setEl('aiDeepSeekStatus', ds.configured?'已配置 '+adaptiveEsc(ds.model||''):'未配置');
    setEl('aiConsensusRate', dualAi.consensus_rules?adaptiveValue(dualAi.consensus_rules.weight_magnitude_ratio*100,'%'): '—');
    /* 加载modlens状态 */
    try{
      var modlens=await api('/api/adaptive/modlens/status');
      setEl('aiModlensStatus', modlens.modlens_available?'可用':'不可用');
    }catch(e){setEl('aiModlensStatus','检测失败');}
    /* 加载进化状态 */
    try{
      var evo=await api('/api/adaptive/evolution/status');
      setEl('aiEvolutionState', evo.should_evolve?'待进化':'稳定');
      var params=evo.current_params||{};
      /* 待激活候选：候选 != 生效，必须显式激活才改变 runtime */
      var pendingCount=0;
      var counts=evo.pending_candidates||{};
      for(var _pk in counts){ if(Object.prototype.hasOwnProperty.call(counts,_pk)) pendingCount+=Number(counts[_pk]||0); }
      setEl('aiParamsVersion', (params.id?'#'+params.id+' ('+adaptiveEsc(params.source||'')+')':'默认')
        +(pendingCount?(' · 待激活 '+pendingCount):''));
      renderEvolutionParams(params.params||{});
      renderEvolutionMetrics(evo.performance_metrics||{});
    }catch(e){
      setEl('aiEvolutionState','未初始化');
      setEl('aiParamsVersion','默认');
    }
  }catch(e){
    setEl('aiMiMoStatus','加载失败');
    setEl('aiDeepSeekStatus','加载失败');
  }
  /* 加载调参记录 */
  try{
    var runs=await api('/api/adaptive/dual-ai/runs?limit=10');
    renderDualAiRuns(runs.runs||[]);
  }catch(e){setEl('aiRunsTable','<div class="banner">加载调参记录失败：'+adaptiveEsc(e.message)+'</div>');}
  /* 加载进化日志 */
  try{
    var log=await api('/api/adaptive/evolution/log?limit=20');
    renderEvolutionLog(log.log||[]);
  }catch(e){setEl('aiLogTable','<div class="banner">加载进化日志失败：'+adaptiveEsc(e.message)+'</div>');}
}

export function abValidationPanel(d){
  var v=(d&&d.validation)||{},rows=v.rows||[];
  var label={risk:'风控版本',selection:'选股进化',allocation:'资金分摊',tuner:'AI调参'};
  var verdictCls={pass:'up',fail:'down',observing:''};
  var body=rows.map(function(r){
    return '<tr><td>'+adaptiveEsc(r.account_id||'')+'</td><td>'+adaptiveEsc(label[r.kind]||r.kind)+'</td>'
      +'<td>'+adaptiveEsc(String(r.version||'—'))+'</td>'
      +'<td>'+Number(r.observation_days||0)+'/'+Number(v.min_observation_days||5)+' 净值日</td>'
      +'<td class="'+(verdictCls[r.verdict]||'')+'">'+(r.excess_pct==null?'—':adaptiveValue(r.excess_pct,'%',2))+'</td>'
      +'<td><b>'+adaptiveEsc(r.verdict==='pass'?'有效':(r.verdict==='fail'?'弱于基线':'观察中'))+'</b></td>'
      +'<td>'+adaptiveEsc(String(r.deployed_at||'—')).replace('T',' ').substring(0,10)+'</td></tr>';
  }).join('');
  if(!body){return '<div class="adaptive-notice">尚无生效的进化部署；应用任一版本后，收盘学习会自动记录部署前后 A/B 对照。</div>';}
  return '<div class="table-scroll"><table class="adaptive-table"><thead><tr><th>账户</th><th>通道</th><th>版本</th><th>观察期</th><th>超额(vs部署前基线)</th><th>结论</th><th>部署日</th></tr></thead><tbody>'+body+'</tbody></table></div>';
}

export function renderEvolutionParams(params){
  var keys=['max_weight_delta','max_delta_threshold','confidence_threshold',
    'consensus_weight_ratio','consensus_direction_threshold','hold_bias',
    'max_proposals_per_run','require_dual_confidence','min_dual_confidence'];
  var labels={
    max_weight_delta:'最大权重步长',max_delta_threshold:'最大入场阈值调整',
    confidence_threshold:'置信度阈值',consensus_weight_ratio:'共识权重幅度比',
    consensus_direction_threshold:'共识方向阈值',hold_bias:'hold倾向',
    max_proposals_per_run:'单次最大提案数',require_dual_confidence:'要求双AI置信度',
    min_dual_confidence:'双AI最低置信度'
  };
  var h='<table class="adaptive-table"><thead><tr><th>参数</th><th>当前值</th></tr></thead><tbody>';
  keys.forEach(function(k){
    var v=params[k];
    if(v==null)return;
    var display=typeof v==='boolean'?(v?'是':'否'):Number(v).toFixed(4);
    h+='<tr><td>'+adaptiveEsc(labels[k]||k)+'</td><td>'+display+'</td></tr>';
  });
  h+='</tbody></table>';
  setEl('aiParamsTable',h);
}

export function renderEvolutionMetrics(m){
  if(!m.has_data){setEl('aiMetricsGrid','<p>暂无足够数据</p>');return;}
  var h='<div class="ai-metrics-kpis">'
    +'<div><small>样本数</small><b>'+Number(m.sample_count)+'</b></div>'
    +'<div><small>共识率</small><b>'+adaptiveValue(m.consensus_rate*100,'%')+'</b></div>'
    +'<div><small>propose率</small><b>'+adaptiveValue(m.propose_rate*100,'%')+'</b></div>'
    +'<div><small>失败率</small><b>'+adaptiveValue(m.failure_rate*100,'%')+'</b></div>'
    +'<div><small>平均延迟</small><b>'+adaptiveValue(m.avg_latency_ms,'ms',0)+'</b></div>'
    +'<div><small>评估分数</small><b>'+adaptiveValue(m.avg_eval_score,'',3)+'</b></div>'
    +'</div>';
  setEl('aiMetricsGrid',h);
}

export function renderDualAiRuns(runs){
  if(!runs.length){setEl('aiRunsTable','<p>暂无调参记录</p>');return;}
  var h='<table class="adaptive-table"><thead><tr><th>ID</th><th>触发</th><th>模式</th><th>状态</th><th>MiMo</th><th>DeepSeek</th><th>共识</th><th>时间</th><th>落地</th></tr></thead><tbody>';
  runs.forEach(function(r){
    var statusClass=r.status==='consensus'?'up':(r.status==='failed'?'down':'');
    var mimo=r.mimo||{};
    var ds=r.deepseek||{};
    var applied=Array.isArray(r.applied_ids)?r.applied_ids:(r.applied_ids?String(r.applied_ids):null);
    var proposalCount=Array.isArray(r.merged_proposals)?r.merged_proposals.length:0;
    var action='';
    if(r.status==='consensus'&&proposalCount>0&&!applied){
      var fresh=(Date.now()-new Date(String(r.created_at||'').replace(' ','T')).getTime())<30*60*1000;
      action=fresh?'<button class="ghost" onclick="applyAdaptiveTunerProposal('+Number(r.id)+')">应用共识</button>':'<span style="color:#9aa5b1;font-size:11px">已过期</span>';
    }else if(applied&&applied.length){
      action=applied.map(function(a){return '<button class="ghost" onclick="rollbackAdaptiveTunerOverlay(\''+adaptiveEsc(a)+'\')">回滚'+adaptiveEsc(a)+'</button>';}).join(' ');
    }
    h+='<tr>'
      +'<td>#'+r.id+'</td>'
      +'<td>'+adaptiveEsc(r.trigger)+'</td>'
      +'<td>'+adaptiveEsc(r.mode)+'</td>'
      +'<td class="'+statusClass+'">'+adaptiveEsc(r.status)+'</td>'
      +'<td>'+adaptiveEsc(mimo.status)+(mimo.latency_ms?' ('+mimo.latency_ms+'ms)':'')+'</td>'
      +'<td>'+adaptiveEsc(ds.status)+(ds.latency_ms?' ('+ds.latency_ms+'ms)':'')+'</td>'
      +'<td>'+adaptiveEsc(r.consensus_reason||'').substring(0,40)+'</td>'
      +'<td>'+adaptiveEsc(r.created_at||'').replace('T',' ').substring(0,19)+'</td>'
      +'<td>'+action+'</td>'
      +'</tr>';
  });
  h+='</tbody></table>';
  setEl('aiRunsTable',h);
}

export function renderEvolutionLog(log){
  if(!log.length){setEl('aiLogTable','<p>暂无进化日志</p>');return;}
  var h='<table class="adaptive-table"><thead><tr><th>时间</th><th>事件</th><th>详情</th></tr></thead><tbody>';
  log.forEach(function(e){
    var detail=e.detail||{};
    var desc='';
    if(detail.adjustments) desc=detail.adjustments.join('; ');
    else if(detail.reason) desc=detail.reason;
    else desc=JSON.stringify(detail).substring(0,80);
    h+='<tr>'
      +'<td>'+adaptiveEsc(e.created_at||'').replace('T',' ').substring(0,19)+'</td>'
      +'<td>'+adaptiveEsc(e.event_type)+'</td>'
      +'<td>'+adaptiveEsc(desc)+'</td>'
      +'</tr>';
  });
  h+='</tbody></table>';
  setEl('aiLogTable',h);
}

export async function triggerEvolution(){
  if(!confirm('确认手动触发一次自进化？\n\n注意：进化只生成候选参数，不会立即生效；需要显式激活后才会改变调参边界。')) return;
  try{
    var r=await apiPost('/api/adaptive/evolution/evolve?confirmed=true');
    if(r.evolved){
      alert('已生成进化候选 #'+(r.new_params_id||'-')+'（调整 '+((r.changed_keys||[]).length)+' 个参数）。\n'
        +'状态：'+(r.validation_state||'candidate')+'\n\n'
        +'该候选尚未生效，需显式激活后才会改变调参边界。');
    }else{
      alert('无需进化：'+(r.reason||r.metrics?'当前状态稳定':'无数据'));
    }
    loadEvolutionStatus();
  }catch(e){alert('进化失败：'+e.message);}
}

export async function runDualAiTuning(){
  if(!confirm('确认运行一次双AI共识调参？')) return;
  try{
    var r=await apiPost('/api/adaptive/dual-ai/tune?trigger=manual&mode=intraday&confirmed=true');
    var msg='调参完成\n状态：'+r.status+'\n共识：'+(r.consensus?'是':'否')+'\n原因：'+(r.consensus_reason||'');
    if(r.evolution_triggered) msg+='\n\n⚡ 自动进化已触发';
    alert(msg);
    loadEvolutionStatus();
  }catch(e){alert('双AI调参失败：'+e.message);}
}

export async function testModlensRead(){
  var url=($('aiImageUrl')||{}).value||'';
  if(!url){alert('请输入图片URL或路径');return;}
  setEl('aiModlensResult','<div class="loading">正在读取图片…</div>');
  try{
    var r=await apiPost('/api/adaptive/modlens/read-image?path='+encodeURIComponent(url));
    var h='<div class="ai-modlens-output">'
      +'<p><b>状态：</b>'+(r.success?'成功':'失败')+'</p>'
      +'<p><b>耗时：</b>'+(r.latency_ms||0)+'ms</p>';
    if(r.success){
      h+='<p><b>OCR文字：</b></p><pre style="max-height:200px;overflow:auto;background:rgba(0,0,0,.2);padding:8px;border-radius:6px;font-size:12px">'
        +adaptiveEsc(r.formatted_text||r.ocr_text||'（无文字）')+'</pre>';
    }else{
      h+='<p style="color:var(--state-error)">'+adaptiveEsc(r.error)+'</p>';
    }
    h+='</div>';
    setEl('aiModlensResult',h);
  }catch(e){setEl('aiModlensResult','<div class="banner">请求失败：'+adaptiveEsc(e.message)+'</div>');}
}

export function switchToAdaptiveAI(){
  /* 切换到自进化页面并打开AI tab */
  var adaptiveTab=document.querySelector('[data-page="p-adaptive"]');
  if(adaptiveTab && !adaptiveTab.classList.contains('active')) adaptiveTab.click();
  setTimeout(function(){
    var aiTab=document.querySelector('[data-section="ai"]');
    if(aiTab) setAdaptiveSection('ai',aiTab);
  },200);
}

export async function runAdaptiveAiTuning(){
  var confirmation=await adaptiveConfirm({title:'运行 AI 有界调参',detail:'AI 会复核行情、账本和策略证据，并产出受限调参候选。',boundary:'结果先进入候选与审计，不会直接改变交易规则或下单。'}); if(!confirmation.approved) return;
  var button=$('adaptiveAiTuneButton')||$('adaptiveAiTuneInlineButton');
  if(button){button.disabled=true;button.textContent='AI调参校验中…';}
  try{renderAdaptive(await apiPost('/api/adaptive/ai/tune?trigger=manual-ui&mode=intraday&confirmed=true'));}
  catch(e){adaptiveActionNotice('AI 有界调参失败',e.message);}
  finally{if(button){button.disabled=false;button.textContent='运行AI有界调参';}}
}

export async function runNewsLearning(){
  var confirmation=await adaptiveConfirm({title:'运行情报与事件学习',detail:'将采集并校准公告、新闻和事件证据。',boundary:'仅更新研究与影子证据，不会直接触发买卖。'}); if(!confirmation.approved) return;
  var button=$('newsLearningRunButton'); if(button){button.disabled=true;button.textContent='采集与校准中…';}
  try{renderAdaptive(await apiPost('/api/adaptive/news/run?trigger=manual-ui&confirmed=true'));}
  catch(e){adaptiveActionNotice('新闻学习失败',e.message);}
  finally{if(button){button.disabled=false;button.textContent='运行新闻学习';}}
}

export async function runAdaptiveAdvisor(){
  var confirmation=await adaptiveConfirm({title:'运行数据质量审阅',detail:'将校验全市场行情、双源一致性与模拟盘账本。',boundary:'审阅只输出证据和异常，不会下单或修改风控。'}); if(!confirmation.approved) return;
  var button=$('advisorRunButton'); if(button){button.disabled=true;button.textContent='审阅中…';}
  try{renderAdaptive(await apiPost('/api/adaptive/advisor/run?trigger=manual-ui&confirmed=true'));}
  catch(e){adaptiveActionNotice('数据质量审阅失败',e.message);}
  finally{if(button){button.disabled=false;button.textContent='运行数据质量审阅';}}
}

export async function runAdaptiveResearchTask(purpose,button){
  var confirmation=await adaptiveConfirm({title:'运行研究任务',detail:'将运行该项 AI 研究并写入可追溯的影子证据。',boundary:'不会直接改变策略参数或交易。'}); if(!confirmation.approved) return;
  if(button){button.disabled=true;button.textContent='运行中…';}
  try{renderAdaptive(await apiPost('/api/adaptive/advisor/run?trigger=manual-ui&purpose='+encodeURIComponent(purpose)+'&confirmed=true'));}
  catch(e){adaptiveActionNotice('研究任务失败',e.message);}
  finally{if(button){button.disabled=false;button.textContent='单独运行';}}
}

export async function runAdaptiveResearchSuite(){
  var confirmation=await adaptiveConfirm({title:'运行全部研究任务',detail:'将依次运行已启用的 AI 研究任务。',boundary:'只生成研究证据，不会直接交易或放宽风控。'}); if(!confirmation.approved) return;
  var button=$('advisorSuiteButton'); if(button){button.disabled=true;button.textContent='研究套件运行中…';}
  try{renderAdaptive(await apiPost('/api/adaptive/advisor/suite?trigger=manual-suite&confirmed=true'));}
  catch(e){adaptiveActionNotice('研究套件失败',e.message);}
  finally{if(button){button.disabled=false;button.textContent='运行全部研究任务';}}
}

export async function recordAdaptiveFeedback(accountId,verdict){
  if(!window._adaptiveDecisionId){adaptiveActionNotice('暂无可审阅决策','当前没有可提交人工反馈的 Bandit 决策。');return;}
  var confirmation=await adaptiveConfirm({title:verdict==='approve'?'记录人工认可':'记录继续观察',detail:'该反馈会写入策略学习审计。',boundary:'仅影响后续研究证据，不会直接放权或下单。',reason:true,placeholder:'请填写判断依据'}); if(!confirmation.approved) return;
  var note=confirmation.reason;
  try{renderAdaptive(await apiPost('/api/adaptive/feedback?decision_id='+window._adaptiveDecisionId+'&account_id='+encodeURIComponent(accountId)+'&verdict='+encodeURIComponent(verdict)+'&note='+encodeURIComponent(note)+'&confirmed=true'));}
  catch(e){adaptiveActionNotice('记录人工反馈失败',e.message);}
}

export async function applyAdaptiveSelectionCandidate(candidateId){
  if(!candidateId) return;
  var confirmation=await adaptiveConfirm({title:'批准选股进化版本',detail:'批准后会写入对应模拟策略的内部选股参数。',boundary:'仅作用于模拟盘，可在版本管理中回滚；不会改公共选股。'}); if(!confirmation.approved) return;
  try{renderAdaptive(await apiPost('/api/adaptive/selection/apply?candidate_id='+encodeURIComponent(candidateId)+'&approved_by=human-ui&confirmed=true'));}
  catch(e){adaptiveActionNotice('选股版本未能应用',e.message);}
}

export async function applyAdaptiveRiskCandidate(candidateId){
  var confirmation=await adaptiveConfirm({title:'批准风控进化版本',detail:'批准后会更新对应模拟策略的受限风控参数。',boundary:'仅作用于模拟盘并保留回滚；不会放宽硬风控或连接实盘。'}); if(!confirmation.approved) return;
  try{renderAdaptive(await apiPost('/api/adaptive/risk/apply?candidate_id='+candidateId+'&approved_by=human-ui&confirmed=true'));}
  catch(e){adaptiveActionNotice('风控版本未能晋级',e.message);}
}

export async function rollbackAdaptiveRisk(accountId){
  var confirmation=await adaptiveConfirm({title:'回滚风控版本',detail:'将恢复该策略上一版模拟盘风控参数。',boundary:'只影响该模拟策略，可再次审阅后重新批准。',reason:true,defaultReason:'人工复核后回滚'}); if(!confirmation.approved) return;
  var reason=confirmation.reason;
  try{renderAdaptive(await apiPost('/api/adaptive/risk/rollback?account_id='+encodeURIComponent(accountId)+'&reason='+encodeURIComponent(reason)+'&confirmed=true'));}
  catch(e){adaptiveActionNotice('风控版本回滚失败',e.message);}
}

export async function rollbackAdaptiveSelection(accountId){
  var confirmation=await adaptiveConfirm({title:'回滚选股版本',detail:'将恢复该策略上一版模拟盘内部选股权重。',boundary:'只影响该模拟策略，不影响公共选股。',reason:true,defaultReason:'人工复核后回滚'}); if(!confirmation.approved) return;
  var reason=confirmation.reason;
  try{renderAdaptive(await apiPost('/api/adaptive/selection/rollback?account_id='+encodeURIComponent(accountId)+'&reason='+encodeURIComponent(reason)+'&confirmed=true'));}
  catch(e){adaptiveActionNotice('选股版本回滚失败',e.message);}
}

export async function rollbackAdaptiveRebalance(accountId){
  var confirmation=await adaptiveConfirm({title:'回滚调仓版本',detail:'将恢复该策略上一版模拟盘调仓参数。',boundary:'只影响该模拟策略，不会改动历史成交。',reason:true,defaultReason:'人工复核后回滚调仓'}); if(!confirmation.approved) return;
  var reason=confirmation.reason;
  try{renderAdaptive(await apiPost('/api/adaptive/rebalance/rollback?account_id='+encodeURIComponent(accountId)+'&reason='+encodeURIComponent(reason)+'&confirmed=true'));}
  catch(e){adaptiveActionNotice('调仓版本回滚失败',e.message);}
}

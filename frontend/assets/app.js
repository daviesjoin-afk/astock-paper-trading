var charts = {};
window.__ASTOCK_ADAPTIVE_UI_BUILD__='20260813-confirm-v4';
function $(id){ return document.getElementById(id); }
function fmt(v, d){ if(v===null||v===undefined||isNaN(v)) return '-'; return Number(v).toFixed(d===undefined?2:d); }
function pctCls(v){ return v>0?'up':(v<0?'down':''); }
function pctTxt(v){ if(v===null||v===undefined) return '-'; return (v>0?'+':'')+fmt(v)+'%'; }
function yi(v){ if(v===null||v===undefined) return '-'; return fmt(v/1e8,1)+'亿'; }
async function api(path, options){
  options=options||{};
  var r;
  // A container restart can briefly close the upstream connection and make
  // nginx return 502/503. GETs are safe to retry; keep POST semantics intact.
  // 2026-09-03 P3：旧退避仅 250/500ms，短于一次容器重启的拒连窗口（实测
  // 约 2~3 秒），所以重新部署时前端仍会抛出硬错误“请求失败 HTTP 502”。
  // 放宽到 4 次 / 累计约 4.8 秒，让部署重启对只读请求透明；非 5xx 首次
  // 即跳出，正常请求不受影响。POST 保持不重试，避免重复下单类副作用。
  var MAX_GET_ATTEMPTS = 4;
  for(var attempt=0; attempt<MAX_GET_ATTEMPTS; attempt++){
    var controller=typeof AbortController==='undefined'?null:new AbortController();
    var timeout=controller?setTimeout(function(){controller.abort();},Number(options.timeout)||25000):null;
    try { r = await fetch(path,{signal:controller&&controller.signal}); }
    catch(err){ if(attempt===MAX_GET_ATTEMPTS-1) throw err; await new Promise(function(resolve){setTimeout(resolve,800*(attempt+1));}); continue; }
    finally { if(timeout) clearTimeout(timeout); }
    if(r.status!==502 && r.status!==503 && r.status!==504) break;
    if(attempt<MAX_GET_ATTEMPTS-1) await new Promise(function(resolve){setTimeout(resolve,800*(attempt+1));});
  }
  var d = await r.json().catch(function(){ return {}; });
  if(!r.ok) throw new Error(d.detail || d.error ||
    ('请求失败 HTTP '+r.status+(r.status>=500?'（后端可能正在重启或过载，请稍后重试）':'')));
  return d;
}
async function apiPost(path){
  var r = await fetch(path,{method:'POST'});
  var d = await r.json().catch(function(){ return {}; });
  if(!r.ok) throw new Error(d.detail || d.error || ('请求失败 HTTP '+r.status));
  return d;
}
function chart(id){
  var target=$(id), existing=charts[id];
  if(!target) return null;
  // Charts are optional: the local vendor bundle may be unavailable during a
  // partial static deploy.  Return a no-op adapter so data tables and risk
  // controls remain usable instead of throwing from every chart caller.
  if(typeof echarts==='undefined') return {
    getDom:function(){return target;}, dispose:function(){}, resize:function(){},
    setOption:function(){}, dispatchAction:function(){}, clear:function(){}
  };
  // Dynamic views replace their HTML on refresh. An ECharts instance keeps a
  // reference to its original DOM node, so reusing it would draw off-screen.
  if(existing && existing.getDom()!==target){
    existing.dispose();
    delete charts[id];
  }
  if(!charts[id]) charts[id] = echarts.init(target,null,{renderer:'canvas'});
  return charts[id];
}
function tableScroll(html, minWidth){
  return '<div class="table-scroll"'+(minWidth?' style="--table-min:'+minWidth+'px"':'')+'>'+html+'</div>';
}
function renderPaperCompareChart(curve){
  var target=$('paperCompareChart');
  if(!target||!curve||!(curve.dates||[]).length) return;
  if(typeof echarts==='undefined'){
    target.innerHTML='<div class="paper-empty">图表组件暂不可用；下方对比表仍保留完整数据。</div>';
    return;
  }
  var dates=curve.dates||[];
  if(dates.length<2){
    target.innerHTML='<div class="paper-empty">净值点不足两个，下一次有效收盘或日内快照后将绘制曲线。</div>';
    return;
  }
  var palette={tq_breakout:'#27775a',trend_pullback:'#5576bd',sector_rotation:'#b28131',reported_profit_breakout:'#8a5bb8'};
  // The shared pool is an accounting aggregate, not a fourth strategy.  It
  // used to dominate the y-axis and made the three strategy curves hard to
  // compare, so keep it in the API/account cards but exclude it from this
  // strategy comparison chart.
  var series=(curve.series||[]).filter(function(item){
    return item.id!=='shared_pool' && item.name!=='总资金池';
  }).map(function(item){return {name:item.name,type:'line',smooth:false,connectNulls:false,symbol:'circle',symbolSize:7,lineStyle:{width:2.5,color:palette[item.id]||'#26765a'},itemStyle:{color:palette[item.id]||'#26765a'},data:(item.values||[]).map(function(v){return v.return_pct;})};});
  series.push({name:(curve.benchmark||{}).name||'沪深300',type:'line',smooth:false,connectNulls:false,symbol:'diamond',symbolSize:6,lineStyle:{width:2,type:'dashed',color:'#65758b'},itemStyle:{color:'#65758b'},data:((curve.benchmark||{}).values||[]).map(function(v){return v.return_pct;})});
  requestAnimationFrame(function(){
    var instance=chart('paperCompareChart');
    if(!instance) return;
    instance.setOption({animation:false,grid:{left:56,right:22,top:54,bottom:42},tooltip:{trigger:'axis',valueFormatter:function(v){return v==null?'—':Number(v).toFixed(2)+'%';}},legend:{top:14},xAxis:{type:'category',data:dates.map(function(x){return x.slice(5);}),axisTick:{alignWithLabel:true}},yAxis:{type:'value',axisLabel:{formatter:'{value}%'},splitLine:{lineStyle:{color:'#edf1ef'}}},series:series},{notMerge:true});
    instance.resize();
  });
}

var APP_PAGE_KEY='astock.activePage', PAPER_VIEW_KEY='astock.paperView';
window._paperWorkspace=sessionStorage.getItem(PAPER_VIEW_KEY)||'portfolio';
function activatePage(page, options){
  options=options||{};
  var tab=document.querySelector('.tab[data-page="'+page+'"]');
  if(!tab){page='p-select';tab=document.querySelector('.tab[data-page="p-select"]');}
  document.querySelectorAll('.tab').forEach(function(x){x.classList.remove('active');x.setAttribute('aria-current','false');});
  document.querySelectorAll('.page').forEach(function(x){x.classList.remove('active');});
  tab.classList.add('active');tab.setAttribute('aria-current','page');$(page).classList.add('active');
  sessionStorage.setItem(APP_PAGE_KEY,page);
  if(options.writeHash!==false){
    history.replaceState(null,'','#'+page.replace(/^p-/,'')+(page==='p-paper'?'/'+(window._paperWorkspace||'portfolio'):''));
  }
  if(page==='p-sector'&&!window._sectorLoaded){loadSectors();loadLinkage();window._sectorLoaded=true;}
  if(page==='p-adaptive') loadAdaptive();
  if(page==='p-paper'){
    var paperTab=document.querySelector('#p-paper [data-paper-view="'+(window._paperWorkspace||'portfolio')+'"]');
    showPaperWorkspace(window._paperWorkspace||'portfolio',paperTab,{restore:true});
  }
  if(page==='p-stock'&&!window._stockLoaded){window._stockCode=window._stockCode||'002241';$('stockCode').value=window._stockCode;analyzeStock();}
  Object.keys(charts).forEach(function(k){charts[k].resize();});
}
document.querySelectorAll('.tab').forEach(function(t){
  t.setAttribute('aria-current',t.classList.contains('active')?'page':'false');
  t.onclick=function(){activatePage(t.dataset.page);};
});
function restoreAppNavigation(){
  var parts=String(location.hash||'').replace(/^#/,'').split('/');
  var page=parts[0]?'p-'+parts[0]:(sessionStorage.getItem(APP_PAGE_KEY)||'p-select');
  if(page==='p-paper'&&(parts[1]==='adaptive'||sessionStorage.getItem(PAPER_VIEW_KEY)==='adaptive')){
    page='p-adaptive';
    sessionStorage.removeItem(PAPER_VIEW_KEY);
  }
  if(page==='p-paper'&&parts[1]) window._paperWorkspace=parts[1];
  activatePage(page,{writeHash:false});
}
window.addEventListener('popstate',restoreAppNavigation);

/* The two deep workspaces have more tabs than a narrow browser can show.
   Native scrollbars are often configured as overlay-only on Windows, so keep a
   visible, keyboard-accessible rail in addition to the browser scrollbar. */
function workspaceTabStrip(id){ return $(id); }
function updateWorkspaceTabRail(id){
  var strip=workspaceTabStrip(id), rail=document.querySelector('[data-tab-rail="'+id+'"]');
  if(!strip||!rail) return;
  var track=rail.querySelector('.workspace-tab-track'), thumb=rail.querySelector('.workspace-tab-thumb');
  var max=Math.max(0,strip.scrollWidth-strip.clientWidth), needed=max>2;
  rail.hidden=!needed;
  if(!needed||!track||!thumb) return;
  var ratio=Math.max(.12,Math.min(1,strip.clientWidth/strip.scrollWidth));
  var usable=Math.max(0,track.clientWidth-(track.clientWidth*ratio));
  var progress=max?strip.scrollLeft/max:0;
  thumb.style.width=(ratio*100)+'%';
  thumb.style.transform='translateX('+(usable*progress)+'px)';
  track.setAttribute('aria-valuemin','0');
  track.setAttribute('aria-valuemax',String(Math.round(max)));
  track.setAttribute('aria-valuenow',String(Math.round(strip.scrollLeft)));
  track.setAttribute('aria-valuetext','当前在三级菜单的 '+Math.round(progress*100)+'%');
}
function syncWorkspaceTabRails(){
  document.querySelectorAll('[data-tab-scroll]').forEach(function(strip){ updateWorkspaceTabRail(strip.id); });
}
function scrollWorkspaceTabs(id,direction){
  var strip=workspaceTabStrip(id); if(!strip) return;
  var amount=Math.max(220,Math.round(strip.clientWidth*.72))*Number(direction||1);
  var reduce=window.matchMedia&&window.matchMedia('(prefers-reduced-motion: reduce)').matches;
  strip.scrollBy({left:amount,behavior:reduce?'auto':'smooth'});
}
function beginWorkspaceTabRail(event,id){
  var strip=workspaceTabStrip(id), track=event.currentTarget;
  if(!strip||!track) return;
  event.preventDefault();
  var move=function(moveEvent){
    var rect=track.getBoundingClientRect();
    var ratio=Math.max(0,Math.min(1,(moveEvent.clientX-rect.left)/Math.max(1,rect.width)));
    strip.scrollLeft=ratio*Math.max(0,strip.scrollWidth-strip.clientWidth);
  };
  var finish=function(){ window.removeEventListener('pointermove',move); window.removeEventListener('pointerup',finish); };
  move(event);
  window.addEventListener('pointermove',move);
  window.addEventListener('pointerup',finish,{once:true});
}
function handleWorkspaceTabRailKey(event,id){
  if(['ArrowLeft','ArrowRight','Home','End'].indexOf(event.key)<0) return;
  event.preventDefault();
  var strip=workspaceTabStrip(id); if(!strip) return;
  var reduce=window.matchMedia&&window.matchMedia('(prefers-reduced-motion: reduce)').matches;
  if(event.key==='Home') strip.scrollTo({left:0,behavior:reduce?'auto':'smooth'});
  else if(event.key==='End') strip.scrollTo({left:strip.scrollWidth,behavior:reduce?'auto':'smooth'});
  else scrollWorkspaceTabs(id,event.key==='ArrowRight'?1:-1);
}
function installWorkspaceTabRails(){
  document.querySelectorAll('[data-tab-scroll]').forEach(function(strip){
    if(strip.dataset.tabRailInstalled==='1') return;
    strip.dataset.tabRailInstalled='1';
    var queued=false;
    strip.addEventListener('scroll',function(){
      if(queued) return; queued=true;
      requestAnimationFrame(function(){ queued=false; updateWorkspaceTabRail(strip.id); });
    },{passive:true});
  });
  syncWorkspaceTabRails();
}
window.addEventListener('resize',syncWorkspaceTabRails,{passive:true});

function adaptiveStageClass(stage){
  if(stage==='eligible_for_review'||stage==='advisory') return 'ready';
  if(stage==='shadow'||stage==='regime_validation') return 'learning';
  return 'collecting';
}
function adaptiveBar(value, tone){
  var width=Math.max(0,Math.min(100,Number(value)||0));
  return '<div class="adaptive-meter '+(tone||'')+'"><span style="width:'+width+'%"></span></div>';
}
function adaptiveValue(value,suffix,digits){
  return value===null||value===undefined?'—':fmt(value,digits===undefined?1:digits)+(suffix||'');
}
function adaptiveEsc(value){
  return String(value===null||value===undefined?'':value).replace(/[&<>"']/g,function(ch){return {'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[ch];});
}
function adaptiveJsArg(value){
  return encodeURIComponent(String(value===null||value===undefined?'':value));
}
/* Optional snapshot fields can be absent while a backend is warming up.
   Never render transport placeholders as a state visible to an operator. */
function adaptiveText(value,fallback){
  var text=String(value===null||value===undefined?'':value).trim();
  if(!text||text==='undefined'||text==='null'||text==='None') return fallback||'';
  return text;
}
function adaptiveSafeUrl(value){
  var url=String(value||''); return /^https:\/\/[a-z0-9.-]+\//i.test(url)?adaptiveEsc(url):'#';
}
function adaptiveJsonArray(value){
  if(Array.isArray(value)) return value;
  try{var parsed=JSON.parse(value||'[]');return Array.isArray(parsed)?parsed:[];}catch(e){return [];}
}
function adaptiveJsonObject(value){
  if(value&&typeof value==='object'&&!Array.isArray(value)) return value;
  try{var parsed=JSON.parse(value||'{}');return parsed&&typeof parsed==='object'&&!Array.isArray(parsed)?parsed:{};}catch(e){return {};}
}
function adaptiveLocalDate(){
  var now=new Date();
  return now.getFullYear()+'-'+String(now.getMonth()+1).padStart(2,'0')+'-'+String(now.getDate()).padStart(2,'0');
}
function adaptiveTimelineRows(payload){
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
function adaptiveTimelineStatus(value){
  var key=String(value||'unknown').toLowerCase();
  return ({completed:'已完成',success:'已完成',ok:'已完成',running:'运行中',in_progress:'运行中',processing:'处理中',queued:'排队中',pending:'待运行',retrying:'重试中',failed:'失败',error:'失败',blocked:'已阻断',skipped:'已跳过',unknown:'待记录'})[key]||adaptiveText(value,'待记录');
}
function adaptiveTimelineQuality(value){
  var q=typeof value==='string'?{status:value}:adaptiveJsonObject(value), key=String(q.status||q.state||q.quality||'unknown').toLowerCase();
  var label=({valid:'通过',verified:'已核验',valid_close:'收盘有效',fresh:'新鲜',partial:'部分可用',degraded:'降级',failed:'失败',blocked:'阻断',unknown:'待检查'})[key]||adaptiveText(q.status||q.quality,'待检查');
  var extra=[];
  if(q.coverage_pct!==undefined) extra.push('覆盖 '+adaptiveValue(q.coverage_pct,'%',1));
  if(q.agreement_pct!==undefined) extra.push('一致 '+adaptiveValue(q.agreement_pct,'%',1));
  if(q.reason) extra.push(adaptiveText(q.reason,''));
  return label+(extra.length?' · '+extra.join(' · '):'');
}
function adaptiveTimelineTune(value){
  var tuning=typeof value==='string'?{status:value}:adaptiveJsonObject(value), key=String(tuning.status||tuning.state||'not_run').toLowerCase();
  var active=['applied','effective','active'].indexOf(key)>=0;
  var inFlight=['running','in_progress','processing','queued','pending','retrying'].indexOf(key)>=0;
  var label=(tuning.in_progress||tuning.running)?'调参中 · 尚未生效':(active?'已生效':(inFlight?'调参中 · 尚未生效':({shadow_proposal:'影子建议',proposal_only:'候选待审',eligible_auto_adjust:'候选待审',hold:'维持当前',no_change:'无变更',blocked:'已阻断',blocked_quality:'质量阻断',blocked_cross_source:'跨源阻断',cooldown:'冷却中',disabled:'未启用',failed:'调参失败',not_run:'未运行'})[key]||adaptiveText(tuning.status||tuning.state,'待运行')));
  if(active&&tuning.human_confirmed===false) label='候选待人工确认';
  return {label:label,raw:key,proposal:adaptiveText(tuning.reason||tuning.summary,'')};
}
function adaptiveTimelineHuman(value){
  var human=typeof value==='string'?{status:value}:adaptiveJsonObject(value), key=String(human.status||human.state||'not_required').toLowerCase();
  return ({approved:'已确认',confirmed:'已确认',accepted:'已确认',required:'需要确认',pending:'待人工确认',waiting:'待人工确认',rejected:'已拒绝',not_required:'无需确认'})[key]||adaptiveText(human.status||human.state,'待记录');
}
function adaptiveTimelineHtml(payload){
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
function renderAdaptiveTimeline(payload){
  var target=$('adaptiveAiTimelineContent'); if(target) target.innerHTML=adaptiveTimelineHtml(payload||{});
  var badge=$('adaptiveAiTimelineStatus'); if(badge) badge.textContent=(payload&&payload.status==='unavailable')?'接口未部署':((payload&&payload.status)||'已读取');
}
async function refreshAdaptiveTimeline(base){
  var fallback=(base&& (base.ai_analysis_timeline||base.ai_timeline||base.timeline))||((base&&base.runs)?{runs:base.runs,trade_date:base.trade_date||base.date}:{});
  if(fallback&&typeof fallback==='object') renderAdaptiveTimeline(fallback);
  try{
    var day=adaptiveLocalDate(),timeline=await api('/api/adaptive/ai/timeline?trade_date='+encodeURIComponent(day)+'&limit=40&_='+Date.now());
    if(timeline&&timeline.status!=='unavailable') renderAdaptiveTimeline(timeline);
  }catch(e){
    if(!fallback||!adaptiveTimelineRows(fallback).length) renderAdaptiveTimeline({status:'error',message:'时间线暂不可用，可点击刷新或稍后重试。'});
  }
}
async function retryAdaptiveAiWindow(encodedWindow){
  var windowName='manual'; try{windowName=decodeURIComponent(encodedWindow||'manual');}catch(ignore){}
  var confirmation=await adaptiveConfirm({title:'重试分时段 AI 分析',detail:'将重新生成该时段的确定性快照与影子建议。',boundary:'不会下单、不会直接应用 AI 调参；结果仍需人工确认。'}); if(!confirmation.approved) return;
  try{await apiPost('/api/adaptive/ai/analyze?trigger=manual-retry&window='+encodeURIComponent(windowName)+'&scope=all&confirmed=true');await refreshAdaptiveTimeline(window._adaptiveOverviewPayload||{});}
  catch(e){adaptiveActionNotice('分时段 AI 分析重试失败',e.message);}
}
function refreshAdaptiveTimelineNow(){ refreshAdaptiveTimeline(window._adaptiveOverviewPayload||{}); }
function setAdaptiveSection(section,button){
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
function renderAdaptive(d){
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
  var downsideAccountNames={tq_breakout:'短线日内做T',main_force_top10:'超强主力股'};
  var downsideDefaults=downsidePolicy.defaults||{};
  var downsidePolicyRows=Object.keys(downsideDefaults).filter(function(id){return id==='tq_breakout'||id==='main_force_top10';}).map(function(id){var p=downsideDefaults[id]||{},pick=function(short,long){return Number(p[short]==null?p[long]:p[short])||0;};return '<div><b>'+adaptiveEsc(downsideAccountNames[id]||id)+'</b><span>预警 '+fmt(pick('downside_warning_pct','warning_pct'),1)+'% · 部分 '+fmt(pick('downside_partial_pct','partial_pct'),1)+'% · 强制 '+fmt(pick('downside_full_pct','full_pct'),1)+'%</span><small>相对大盘 '+fmt(pick('downside_relative_pct','relative_pct'),1)+'% · 峰值回撤 '+fmt(pick('downside_peak_retrace_pct','peak_retrace_pct'),1)+'% · 部分比例 '+fmt(pick('downside_partial_ratio','partial_ratio')*100,0)+'%</small></div>';}).join('')||'<div><b>等待风控策略基准</b><span>下一次风险评估后生成三段式阈值</span></div>';
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
  var tradeAttrAccountCards=Object.keys(tradeAttrAccounts).filter(function(id){return id==='tq_breakout'||id==='main_force_top10';}).map(function(id){var x=tradeAttrAccounts[id]||{};return '<div><small>'+adaptiveEsc(({tq_breakout:'短线日内做T',main_force_top10:'超强主力股'})[id]||id)+'</small><b>'+Number(x.filled||0)+' 笔</b><span>个股 '+adaptiveValue(x.mean_stock_move_pct,'%',2)+' · 超额 '+adaptiveValue(x.mean_alpha_pct,'%',2)+'</span><em>公告偏空 '+Number(x.negative_news_records||0)+' 笔 · AI '+Number(x.ai_completed||0)+' 笔</em></div>';}).join('')||'<div class="adaptive-empty-row">盘后收盘后生成逐笔归因。</div>';
  var tradeAttrRows=(tradeAttribution.recent||[]).filter(function(x){return x.order_status==='filled';}).slice(0,8).map(function(x){var reason=adaptiveJsonArray(x.reason_codes).join('、');return '<li><time>'+adaptiveEsc(String(x.fill_date||'').slice(5,10))+'</time><div><b>'+adaptiveEsc(x.name||x.code)+' '+adaptiveEsc(x.code)+'</b><p>'+adaptiveEsc(({tq_breakout:'短线日内做T',trend_pullback:'趋势波段优选',sector_rotation:'板块轮动先锋',reported_profit_breakout:'三日策略'})[x.account_id]||x.account_id)+' · '+adaptiveEsc(x.side==='buy'?'买入':'卖出')+' '+Number(x.qty||0)+'股 · 成交 '+fmt(x.fill_price,2)+' · 收盘 '+fmt(x.close_price,2)+'</p><small>'+adaptiveEsc(reason||'暂无足够证据')+' · 大盘 '+adaptiveValue(x.benchmark_move_pct,'%',2)+' · 个股超额 '+adaptiveValue(x.stock_alpha_pct,'%',2)+'</small></div><em class="tag '+(x.ai_status==='completed'?'tag-ok':'tag-warn')+'">'+adaptiveEsc(x.ai_status==='completed'?'AI已归因':'规则归因')+'</em></li>';}).join('')||'<li class="adaptive-empty-row">当天没有已成交操作。</li>';
  var tradeAttrPanel='<section class="adaptive-panel trade-attribution-panel"><header><div><span>TRADE → REASON → LEARNING</span><h3>盘后逐笔涨跌归因</h3></div><em>'+Number(tradeAttribution.records||0)+' 条记录</em></header><p class="adaptive-copy">每个收盘任务先计算个股涨跌、大盘拖累、板块贡献、公告/舆情和行情质量，再批量调用 AI 做可审计解释；AI 结论只进入自进化证据，不直接下单。</p><div class="adaptive-grid trade-attribution-summary">'+tradeAttrAccountCards+'</div><ul class="adaptive-run-log trade-attribution-list">'+tradeAttrRows+'</ul></section>';
  var closedLoopStages=(closedLoop.timeline||[]).map(function(x){var active=String(x.stage)===String(closedLoop.stage);return '<div class="closed-loop-stage '+(active?'active':'')+'"><b>'+adaptiveEsc(x.stage)+'</b><span>'+adaptiveEsc(x.mode)+'</span><em>'+Number(x.nav_pct||0)+'% 资金</em></div>';}).join('');
  var closedLoopBlockers=(closedLoop.blockers||[]).map(function(x){return '<li>'+adaptiveEsc(x)+'</li>';}).join('')||'<li class="pass">当前阶段的确定性门禁已通过</li>';
  var closedLoopPanel='<section class="adaptive-panel closed-loop-panel"><header><div><span>DATA → ALPHA → PORTFOLIO → RISK → EXECUTION → FEEDBACK</span><h3>量化闭环准入台</h3></div><em>'+adaptiveEsc(closedLoop.stage||'D1-D3')+' · '+adaptiveEsc(closedLoop.mode==='shadow'?'影子运行':closedLoop.mode||'影子运行')+'</em></header><p class="adaptive-copy">先把信号、风控、委托、成交和结果串成同一证据链，再让自适应模块按 0% → 5% → 10% 的模拟资金逐级接管。日期到了但证据不达标不会强行放权。</p><div class="closed-loop-stages">'+closedLoopStages+'</div><div class="closed-loop-kpis"><div><small>历史证据链委托</small><b>'+Number(chain.orders||0)+'</b></div><div><small>新窗口关联率</small><b>'+adaptiveValue(admissionWindow.link_pct,'%',1)+'</b></div><div><small>新窗口完整率</small><b>'+adaptiveValue(admissionWindow.valid_pct,'%',1)+'</b></div><div><small>实际 / 反事实</small><b>'+Number(chain.actual||0)+' / '+Number(chain.counterfactual||0)+'</b></div><div><small>策略成交覆盖</small><b>'+Number(closedLoop.strategy_coverage||0)+' / 4</b></div><div><small>灰度硬上限</small><b>'+Number(canaryLimits.max_nav_pct||10)+'% · '+Number(canaryLimits.max_new_slots||2)+'槽</b></div></div><div class="closed-loop-gates"><h4>当前阻断项</h4><ul>'+closedLoopBlockers+'</ul><small>历史债务：未完整关联 '+Number(legacyDebt.unlinked_orders||0)+' 条；只保留审计，不计入新闭环准入。</small></div><div class="adaptive-notice">未成交、风控拒绝、容量延期只进入反事实账本，不再混入真实 Bandit 收益。GA、神经网络和 DeepSeek 在本阶段只能生成影子研究证据，不能直接控制订单。</div></section>';
  var portfolioRows=portfolioSelected.map(function(x){return '<article><div><span>'+adaptiveEsc(x.account_name)+'</span><b>'+adaptiveEsc(x.name||x.code)+' '+adaptiveEsc(x.code)+'</b><small>'+adaptiveEsc(x.industry||'未知行业')+' · '+adaptiveEsc((x.reasons||[]).join('；'))+'</small></div><strong>'+fmt(x.utility,1)+'</strong></article>';}).join('')||'<div class="paper-empty">当前没有可进入组合比较的新增候选。</div>';
  var portfolioPanel='<section class="adaptive-panel portfolio-shadow-panel"><header><div><span>PORTFOLIO ARBITER · SHADOW</span><h3>跨策略组合裁决</h3></div><em>影子运行 · 不改变订单</em></header><p class="adaptive-copy">两套策略继续独立选股；组合层只在共享资金池里比较边际效用，并对同股重复、行业集中和容量延期扣分。当前最多展示 '+Number(portfolioShadow.max_canary_slots||2)+' 个灰度候选。</p><div class="portfolio-shadow-kpis"><span>候选 <b>'+Number(portfolioShadow.candidate_count||0)+'</b></span><span>当前持仓槽 <b>'+Number(portfolioShadow.held_slots||0)+'</b></span><span>重复代码 <b>'+Number(portfolioShadow.duplicate_code_count||0)+'</b></span></div><div class="portfolio-shadow-list">'+portfolioRows+'</div><div class="adaptive-notice">该裁决器不会因为 Bandit 权重变化强制卖出现有持仓；T+1、双源行情、82%总暴露和原策略风控仍拥有最终否决权。</div></section>';
  var dataCards=dataCategories.map(function(x){var coverage=x.coverage_pct==null?'分项统计':fmt(x.coverage_pct,1)+'%';var freshness=x.freshness_minutes==null?'':(' · 延迟 '+fmt(x.freshness_minutes,1)+'分钟');return '<article class="data-input-card '+adaptiveEsc(x.status||'partial')+'"><header><div><span>'+adaptiveEsc(x.id||'data')+'</span><h4>'+adaptiveEsc(x.name)+'</h4></div><em>'+adaptiveEsc(({usable:'可用于交易',shadow:'仅影子',partial:'部分可用',blocked:'禁止新增'})[x.status]||x.status)+'</em></header><div class="data-input-metric"><b>'+coverage+'</b><small>'+Number(x.records||0)+' 条/行'+freshness+'</small></div><p>'+adaptiveEsc(x.detail||'')+'</p><div class="data-input-sources">'+(x.sources||[]).map(function(s){return '<span>'+adaptiveEsc(s)+'</span>';}).join('')+'</div><small>'+adaptiveEsc(x.authority||'')+'</small></article>';}).join('');
  var dataBlockers=(dataInputs.blockers||[]).map(function(x){return '<li>'+adaptiveEsc(x)+'</li>';}).join('')||'<li class="pass">五类输入均达到当前使用门槛</li>';
  var dataInputPanel='<section class="adaptive-panel data-input-panel"><header><div><span>DATA INPUT BUS · QUALITY GATES</span><h3>五类数据输入总线</h3></div><em>'+adaptiveEsc(dataInputs.version||'data-input-bus-v1')+'</em></header><p class="adaptive-copy">全面不等于把所有字段都接进来，而是每类数据都要有来源、覆盖率、源时间、降级状态和明确使用权限。缺失数据不会静默用代理值冒充。</p><div class="data-input-grid">'+dataCards+'</div><div class="data-input-bottom"><div><h4>当前数据缺口</h4><ul>'+dataBlockers+'</ul></div><div><h4>输入纪律</h4><ul>'+(dataInputs.rules||[]).map(function(x){return '<li>'+adaptiveEsc(x)+'</li>';}).join('')+'</ul></div></div></section>';
  var neuralBlockers=(neural.readiness&&neural.readiness.blockers||[]).map(function(x){return '<li>'+adaptiveEsc(x)+'</li>';}).join('')||'<li class="pass">样本门槛已满足，可申请人工确认</li>';
  var neuralPanel='<section class="adaptive-panel neural-control-panel"><header><div><span>NEURAL SHADOW · HUMAN GATE</span><h3>神经网络候选评分</h3></div><em class="'+(neuralApproved?'tag-ok':'tag-warn')+'">'+adaptiveEsc(({shadow_only:'影子运行',approval_waiting_data:'已申请 · 等待数据',approved_bounded_shadow:'人工确认 · 有界影子',disabled:'已停用'})[neural.status]||'影子运行')+'</em></header><p class="adaptive-copy">当前仅对三套短线日内策略做候选排序对照，最多影响排序分 '+fmt(neural.max_rank_adjustment||0,3)+'；不直接下单、不绕过行情双源、板块权限、仓位、T+1或风控卖出。</p><div class="neural-gate-kpis"><div><small>特征样本</small><b>'+Number((neural.readiness||{}).feature_rows||0)+'</b></div><div><small>标签样本</small><b>'+Number((neural.readiness||{}).label_rows||0)+'</b></div><div><small>独立盘面日</small><b>'+Number((neural.readiness||{}).profile_days||0)+' / '+Number((neural.readiness||{}).requirements&&neural.readiness.requirements.min_profile_days||60)+'</b></div><div><small>可用周期</small><b>'+adaptiveEsc(((neural.readiness||{}).available_horizons||[]).join('/')||'—')+'</b></div></div><ul class="adaptive-guardrails neural-blockers">'+neuralBlockers+'</ul>'+(neuralReady&&!neuralApproved?'<button class="ghost" onclick="approveAdaptiveNeural()">人工确认，启用有界影子评分</button>':'')+(neuralApproved?'<div class="adaptive-notice">已确认：仅作为排序副分，硬门禁仍由原策略和风控最终决定。</div>':'<div class="adaptive-notice">未满足样本外门槛前，按钮不会放权；当前结果只记录在自进化证据中。</div>')+'</section>';
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
    +'<section class="adaptive-panel adaptive-risk-evolution"><header><div><span>PAPER RISK EVOLUTION</span><h3>模拟盘风控进化</h3></div><em>'+adaptiveEsc(adaptiveText(riskOpt.mode,'等待样本'))+'</em></header><p class="adaptive-copy">'+adaptiveEsc(adaptiveText(riskOpt.policy,'等待风控进化证据汇总。'))+'</p>'+downsideNotice+'<div class="adaptive-downside-policy"><header><b>当前四套策略防线基准</b><span>只读展示；参数变更仍受版本、影子观察和人工放权约束</span></header>'+downsidePolicyRows+'</div><div class="adaptive-tier-track"><span><b>3日</b>快速影子</span><span><b>5日</b>明显微调</span><span><b>10日</b>标准进化</span><span><b>20日</b>完整受限区间</span></div><div class="adaptive-risk-layout"><div class="adaptive-risk-candidates">'+riskCandidateRows+'</div><aside class="adaptive-risk-side"><div class="adaptive-advisor-card"><span>AI EVIDENCE REVIEWER</span><h4>DeepSeek 数据审阅</h4><b class="'+(advisorReady?'on':'off')+'">'+adaptiveEsc(advisorState)+'</b><p>'+adaptiveEsc(adaptiveText(deepseek.truth_boundary,'证据解释器，不是真实性证明。'))+'</p></div><div class="adaptive-active-risk"><h4>已生效风控版本</h4><ul>'+activeRiskRows+'</ul></div></aside></div></section>'
    +'<section class="adaptive-panel news-learning-panel"><header><div><span>EVENT → OUTCOME → CALIBRATION</span><h3>统一情报与事件学习</h3></div><div class="adaptive-advisor-actions"><em>'+(newsLearning.mode==='paper_micro_eligible'?'有界微调资格':'影子学习')+'</em><button id="newsLearningRunButton" class="ghost" onclick="runNewsLearning()">运行新闻学习</button></div></header><p class="adaptive-copy">风控中心与自进化共用同一份新闻/公告事件账本；风控负责实时门禁，自进化负责1/3/5日兑现校准。</p>'+dynamicRiskNotice+'<div class="news-learning-flow"><span><b>01</b>采集去重</span><i></i><span><b>02</b>事件分型</span><i></i><span><b>03</b>1/3/5日兑现</span><i></i><span><b>04</b>来源校准</span><i></i><span><b>05</b>模拟盘微调</span></div><div class="news-kpis"><div><small>事件账本</small><b>'+Number(newsTotals.events||0)+'</b></div><div><small>可追溯链接</small><b>'+adaptiveValue(newsTotals.linked_pct,'%',1)+'</b></div><div><small>成熟结果</small><b>'+Number(newsTotals.mature_outcomes||0)+'</b></div><div><small>5日成熟事件</small><b>'+Number(newsTotals.mature_5d_events||0)+'</b></div></div><div class="news-learning-layout"><div><h4>最近进入账本</h4><ul class="news-event-list">'+newsEvents+'</ul></div><aside><h4>来源信誉（不使用涨跌评分）</h4><div class="news-source-list">'+newsSources+'</div><h4>微调门禁</h4><div class="news-gates">'+newsGateRows+'</div></aside></div><div class="adaptive-notice">'+adaptiveEsc(newsLearning.authority||'当前仅影子记录。')+'</div></section>'
    +'<section class="adaptive-panel adaptive-advisor-evidence"><header><div><span>DEEPSEEK · DATA QUALITY + TUNING</span><h3>模拟盘数据校验与有界调参</h3></div><div class="adaptive-advisor-actions"><em>'+adaptiveEsc(deepseek.model||'deepseek-v4-flash')+'</em><button id="advisorRunButton" class="ghost" onclick="runAdaptiveAdvisor()" '+(advisorReady?'':'disabled')+'>运行数据质量审阅</button><button id="adaptiveAiTuneInlineButton" class="ghost" onclick="runAdaptiveAiTuning()" '+(advisorReady&&aiTuning.enabled?'':'disabled')+'>运行AI有界调参</button></div></header><div class="adaptive-advisor-summary"><div><small>数据审阅</small><b>'+adaptiveEsc(advisorState)+'</b></div><div><small>AI调参状态</small><b>'+adaptiveEsc(aiTuningState)+'</b></div><div><small>确定性异常</small><b>'+Number(deterministicCount)+'</b></div><div><small>审阅置信度</small><b>'+adaptiveValue(advisorReport.confidence,'%',0)+'</b></div><div><small>跨源真实性</small><b class="'+(advisorReport.cross_source_status==='verified'?'up':'down')+'">'+crossSourceLabel+'</b></div><div><small>双源覆盖 / 一致</small><b>'+adaptiveValue(crossSource.coverage_pct,'%',1)+' / '+adaptiveValue(crossSource.agreement_pct,'%',1)+'</b></div></div><div class="adaptive-advisor-report"><div><h4>审阅摘要</h4><p>'+adaptiveEsc(advisorReport.summary||deepseek.truth_boundary||'DeepSeek只复核确定性证据；行情真实性仍需独立数据源交叉验证。')+'</p><small>市场状态：'+(advisorMarket.session_status==='closed'?'已收盘':'交易中')+' · 收盘口径 '+adaptiveEsc(String(advisorMarket.close_cutoff_at||'—').replace('T',' '))+' · 源行情最后到达 '+adaptiveEsc(String(advisorMarket.latest_source_at||'—').replace('T',' '))+' · 最近审阅 '+adaptiveEsc(String(advisorLatest.finished_at||'—').replace('T',' '))+'</small></div><ul>'+advisorFindings+'</ul></div><div class="adaptive-notice">AI只可在三套模拟账户内提出白名单权重、入场阈值和选股条件的小步补丁；系统先做行情质量、跨源、幅度、冷却和回滚校验，再允许盘中同日生效。AI不能下单、修改公共选股或放宽风控；超出边界的建议只留在影子候选中。</div></section>'
    +'<section class="adaptive-panel adaptive-research-suite"><header><div><span>DEEPSEEK · PAPER RESEARCH SUITE</span><h3>模拟盘智能研究任务</h3></div><button id="advisorSuiteButton" class="ghost" onclick="runAdaptiveResearchSuite()" '+(advisorReady?'':'disabled')+'>运行全部研究任务</button></header><p class="adaptive-copy">收盘后自动运行；每项独立留痕。结论只能进入研究和人工复核，不能直接改选股、风控或订单。</p><div class="adaptive-research-grid">'+researchCards+'</div></section>'
     +'<section class="adaptive-panel"><header><div><span>CONTEXTUAL BANDIT</span><h3>四策略影子分配</h3></div><em>总和 100% · 不改变账户资金</em></header><div class="adaptive-strategy-grid">'+strategyCards+'</div><div class="adaptive-notice">'+adaptiveEsc(d.data_note||'')+'</div></section>'
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
async function loadAdaptive(){
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
function adaptiveOverviewComplete(data){
  return !!(data&&data.engine&&data.risk_optimizer&&data.selection_optimizer&&data.deepseek_advisor&&data.neural_control);
}
function adaptiveConfirm(options){
  options=options||{};
  return new Promise(function(resolve){
    var prior=document.getElementById('adaptiveConfirmModal'); if(prior) prior.remove();
    var mask=document.createElement('div');
    mask.id='adaptiveConfirmModal'; mask.className='adaptive-confirm-mask';
    var needsReason=!!options.reason;
    mask.innerHTML='<section class="adaptive-confirm-dialog" role="dialog" aria-modal="true" aria-labelledby="adaptiveConfirmTitle">'
      +'<span class="adaptive-confirm-kicker">人工确认 · 模拟盘</span><h3 id="adaptiveConfirmTitle">'+adaptiveEsc(options.title||'确认操作')+'</h3>'
      +'<p>'+adaptiveEsc(options.detail||'此操作仅作用于模拟盘。')+'</p>'
      +'<div class="adaptive-confirm-boundary"><b>边界</b><span>'+adaptiveEsc(options.boundary||'不会连接券商、不会发送真实订单。')+'</span></div>'
      +(needsReason?'<label>确认说明 <textarea id="adaptiveConfirmReason" maxlength="300" placeholder="'+adaptiveEsc(options.placeholder||'请填写原因')+'">'+adaptiveEsc(options.defaultReason||'')+'</textarea></label>':'')
      +'<footer><button type="button" class="ghost" data-action="cancel">取消</button><button type="button" class="primary" data-action="approve">确认执行</button></footer></section>';
    function close(result){mask.remove(); resolve(result);}
    mask.addEventListener('click',function(event){if(event.target===mask) close({approved:false});});
    mask.querySelector('[data-action="cancel"]').onclick=function(){close({approved:false});};
    mask.querySelector('[data-action="approve"]').onclick=function(){
      var reason=needsReason?(mask.querySelector('#adaptiveConfirmReason').value||'').trim():'';
      if(needsReason&&!reason){mask.querySelector('#adaptiveConfirmReason').focus();return;}
      close({approved:true,reason:reason});
    };
    document.body.appendChild(mask);
    window.setTimeout(function(){var target=needsReason?mask.querySelector('#adaptiveConfirmReason'):mask.querySelector('[data-action="approve"]'); if(target) target.focus();},0);
  });
}
function adaptiveActionNotice(title,detail){
  var prior=document.getElementById('adaptiveActionNotice'); if(prior) prior.remove();
  var mask=document.createElement('div');
  mask.id='adaptiveActionNotice'; mask.className='adaptive-confirm-mask';
  mask.innerHTML='<section class="adaptive-confirm-dialog adaptive-confirm-error" role="alertdialog" aria-modal="true" aria-labelledby="adaptiveNoticeTitle">'
    +'<span class="adaptive-confirm-kicker">操作未执行</span><h3 id="adaptiveNoticeTitle">'+adaptiveEsc(title||'自进化操作失败')+'</h3>'
    +'<p>'+adaptiveEsc(detail||'本次操作没有写入任何调参或交易数据。')+'</p>'
    +'<div class="adaptive-confirm-boundary"><b>处理建议</b><span>请刷新证据后重试；若问题持续，请保留当前提示供审计排查。</span></div>'
    +'<footer><button type="button" class="primary" data-action="close">知道了</button></footer></section>';
  function close(){mask.remove();}
  mask.addEventListener('click',function(event){if(event.target===mask) close();});
  mask.querySelector('[data-action="close"]').onclick=close;
  document.body.appendChild(mask);
  window.setTimeout(function(){var target=mask.querySelector('[data-action="close"]'); if(target) target.focus();},0);
}
async function runAdaptive(){
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
async function approveAdaptiveNeural(){
  var confirmation=await adaptiveConfirm({title:'批准神经网络影子评分',detail:'神经网络只作为三个短线策略的候选排序参考。',boundary:'不会绕过行情双源、仓位、T+1 或风控门禁；不会直接下单。'}); if(!confirmation.approved) return;
  try{renderAdaptive(await apiPost('/api/adaptive/neural/approve?confirmed=true'));}
  catch(e){adaptiveActionNotice('神经网络仍未达到放权门槛',e.message);}
}

/* ─── AI审阅与调参 JavaScript 函数 ─── */

async function loadEvolutionStatus(){
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
      setEl('aiParamsVersion', params.id?'#'+params.id+' ('+adaptiveEsc(params.source||'')+')':'默认');
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

function renderEvolutionParams(params){
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

function renderEvolutionMetrics(m){
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

function renderDualAiRuns(runs){
  if(!runs.length){setEl('aiRunsTable','<p>暂无调参记录</p>');return;}
  var h='<table class="adaptive-table"><thead><tr><th>ID</th><th>触发</th><th>模式</th><th>状态</th><th>MiMo</th><th>DeepSeek</th><th>共识</th><th>时间</th></tr></thead><tbody>';
  runs.forEach(function(r){
    var statusClass=r.status==='consensus'?'up':(r.status==='failed'?'down':'');
    var mimo=r.mimo||{};
    var ds=r.deepseek||{};
    h+='<tr>'
      +'<td>#'+r.id+'</td>'
      +'<td>'+adaptiveEsc(r.trigger)+'</td>'
      +'<td>'+adaptiveEsc(r.mode)+'</td>'
      +'<td class="'+statusClass+'">'+adaptiveEsc(r.status)+'</td>'
      +'<td>'+adaptiveEsc(mimo.status)+(mimo.latency_ms?' ('+mimo.latency_ms+'ms)':'')+'</td>'
      +'<td>'+adaptiveEsc(ds.status)+(ds.latency_ms?' ('+ds.latency_ms+'ms)':'')+'</td>'
      +'<td>'+adaptiveEsc(r.consensus_reason||'').substring(0,40)+'</td>'
      +'<td>'+adaptiveEsc(r.created_at||'').replace('T',' ').substring(0,19)+'</td>'
      +'</tr>';
  });
  h+='</tbody></table>';
  setEl('aiRunsTable',h);
}

function renderEvolutionLog(log){
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

async function triggerEvolution(){
  if(!confirm('确认手动触发一次自进化？')) return;
  try{
    var r=await apiPost('/api/adaptive/evolution/evolve?confirmed=true');
    if(r.evolved){
      alert('进化完成！调整了 '+((r.changed_keys||[]).length)+' 个参数');
    }else{
      alert('无需进化：'+(r.reason||r.metrics?'当前状态稳定':'无数据'));
    }
    loadEvolutionStatus();
  }catch(e){alert('进化失败：'+e.message);}
}

async function runDualAiTuning(){
  if(!confirm('确认运行一次双AI共识调参？')) return;
  try{
    var r=await apiPost('/api/adaptive/dual-ai/tune?trigger=manual&mode=intraday&confirmed=true');
    var msg='调参完成\n状态：'+r.status+'\n共识：'+(r.consensus?'是':'否')+'\n原因：'+(r.consensus_reason||'');
    if(r.evolution_triggered) msg+='\n\n⚡ 自动进化已触发';
    alert(msg);
    loadEvolutionStatus();
  }catch(e){alert('双AI调参失败：'+e.message);}
}

async function testModlensRead(){
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

function setEl(id,html){var el=document.getElementById(id);if(el)el.innerHTML=html;}

/* 页面加载时自动执行 */
setTimeout(function(){if(document.getElementById('aiStatusCards')) loadEvolutionStatus();},1000);

function switchToAdaptiveAI(){
  /* 切换到自进化页面并打开AI tab */
  var adaptiveTab=document.querySelector('[data-page="p-adaptive"]');
  if(adaptiveTab && !adaptiveTab.classList.contains('active')) adaptiveTab.click();
  setTimeout(function(){
    var aiTab=document.querySelector('[data-section="ai"]');
    if(aiTab) setAdaptiveSection('ai',aiTab);
  },200);
}
async function runAdaptiveAiTuning(){
  var confirmation=await adaptiveConfirm({title:'运行 AI 有界调参',detail:'AI 会复核行情、账本和策略证据，并产出受限调参候选。',boundary:'结果先进入候选与审计，不会直接改变交易规则或下单。'}); if(!confirmation.approved) return;
  var button=$('adaptiveAiTuneButton')||$('adaptiveAiTuneInlineButton');
  if(button){button.disabled=true;button.textContent='AI调参校验中…';}
  try{renderAdaptive(await apiPost('/api/adaptive/ai/tune?trigger=manual-ui&mode=intraday&confirmed=true'));}
  catch(e){adaptiveActionNotice('AI 有界调参失败',e.message);}
  finally{if(button){button.disabled=false;button.textContent='运行AI有界调参';}}
}
async function runNewsLearning(){
  var confirmation=await adaptiveConfirm({title:'运行情报与事件学习',detail:'将采集并校准公告、新闻和事件证据。',boundary:'仅更新研究与影子证据，不会直接触发买卖。'}); if(!confirmation.approved) return;
  var button=$('newsLearningRunButton'); if(button){button.disabled=true;button.textContent='采集与校准中…';}
  try{renderAdaptive(await apiPost('/api/adaptive/news/run?trigger=manual-ui&confirmed=true'));}
  catch(e){adaptiveActionNotice('新闻学习失败',e.message);}
  finally{if(button){button.disabled=false;button.textContent='运行新闻学习';}}
}
async function runAdaptiveAdvisor(){
  var confirmation=await adaptiveConfirm({title:'运行数据质量审阅',detail:'将校验全市场行情、双源一致性与模拟盘账本。',boundary:'审阅只输出证据和异常，不会下单或修改风控。'}); if(!confirmation.approved) return;
  var button=$('advisorRunButton'); if(button){button.disabled=true;button.textContent='审阅中…';}
  try{renderAdaptive(await apiPost('/api/adaptive/advisor/run?trigger=manual-ui&confirmed=true'));}
  catch(e){adaptiveActionNotice('数据质量审阅失败',e.message);}
  finally{if(button){button.disabled=false;button.textContent='运行数据质量审阅';}}
}
async function runAdaptiveResearchTask(purpose,button){
  var confirmation=await adaptiveConfirm({title:'运行研究任务',detail:'将运行该项 AI 研究并写入可追溯的影子证据。',boundary:'不会直接改变策略参数或交易。'}); if(!confirmation.approved) return;
  if(button){button.disabled=true;button.textContent='运行中…';}
  try{renderAdaptive(await apiPost('/api/adaptive/advisor/run?trigger=manual-ui&purpose='+encodeURIComponent(purpose)+'&confirmed=true'));}
  catch(e){adaptiveActionNotice('研究任务失败',e.message);}
  finally{if(button){button.disabled=false;button.textContent='单独运行';}}
}
async function runAdaptiveResearchSuite(){
  var confirmation=await adaptiveConfirm({title:'运行全部研究任务',detail:'将依次运行已启用的 AI 研究任务。',boundary:'只生成研究证据，不会直接交易或放宽风控。'}); if(!confirmation.approved) return;
  var button=$('advisorSuiteButton'); if(button){button.disabled=true;button.textContent='研究套件运行中…';}
  try{renderAdaptive(await apiPost('/api/adaptive/advisor/suite?trigger=manual-suite&confirmed=true'));}
  catch(e){adaptiveActionNotice('研究套件失败',e.message);}
  finally{if(button){button.disabled=false;button.textContent='运行全部研究任务';}}
}
async function recordAdaptiveFeedback(accountId,verdict){
  if(!window._adaptiveDecisionId){adaptiveActionNotice('暂无可审阅决策','当前没有可提交人工反馈的 Bandit 决策。');return;}
  var confirmation=await adaptiveConfirm({title:verdict==='approve'?'记录人工认可':'记录继续观察',detail:'该反馈会写入策略学习审计。',boundary:'仅影响后续研究证据，不会直接放权或下单。',reason:true,placeholder:'请填写判断依据'}); if(!confirmation.approved) return;
  var note=confirmation.reason;
  try{renderAdaptive(await apiPost('/api/adaptive/feedback?decision_id='+window._adaptiveDecisionId+'&account_id='+encodeURIComponent(accountId)+'&verdict='+encodeURIComponent(verdict)+'&note='+encodeURIComponent(note)+'&confirmed=true'));}
  catch(e){adaptiveActionNotice('记录人工反馈失败',e.message);}
}

async function applyAdaptiveSelectionCandidate(candidateId){
  if(!candidateId) return;
  var confirmation=await adaptiveConfirm({title:'批准选股进化版本',detail:'批准后会写入对应模拟策略的内部选股参数。',boundary:'仅作用于模拟盘，可在版本管理中回滚；不会改公共选股。'}); if(!confirmation.approved) return;
  try{renderAdaptive(await apiPost('/api/adaptive/selection/apply?candidate_id='+encodeURIComponent(candidateId)+'&approved_by=human-ui&confirmed=true'));}
  catch(e){adaptiveActionNotice('选股版本未能应用',e.message);}
}

async function applyAdaptiveRiskCandidate(candidateId){
  var confirmation=await adaptiveConfirm({title:'批准风控进化版本',detail:'批准后会更新对应模拟策略的受限风控参数。',boundary:'仅作用于模拟盘并保留回滚；不会放宽硬风控或连接实盘。'}); if(!confirmation.approved) return;
  try{renderAdaptive(await apiPost('/api/adaptive/risk/apply?candidate_id='+candidateId+'&approved_by=human-ui&confirmed=true'));}
  catch(e){adaptiveActionNotice('风控版本未能晋级',e.message);}
}
async function rollbackAdaptiveRisk(accountId){
  var confirmation=await adaptiveConfirm({title:'回滚风控版本',detail:'将恢复该策略上一版模拟盘风控参数。',boundary:'只影响该模拟策略，可再次审阅后重新批准。',reason:true,defaultReason:'人工复核后回滚'}); if(!confirmation.approved) return;
  var reason=confirmation.reason;
  try{renderAdaptive(await apiPost('/api/adaptive/risk/rollback?account_id='+encodeURIComponent(accountId)+'&reason='+encodeURIComponent(reason)+'&confirmed=true'));}
  catch(e){adaptiveActionNotice('风控版本回滚失败',e.message);}
}
async function rollbackAdaptiveSelection(accountId){
  var confirmation=await adaptiveConfirm({title:'回滚选股版本',detail:'将恢复该策略上一版模拟盘内部选股权重。',boundary:'只影响该模拟策略，不影响公共选股。',reason:true,defaultReason:'人工复核后回滚'}); if(!confirmation.approved) return;
  var reason=confirmation.reason;
  try{renderAdaptive(await apiPost('/api/adaptive/selection/rollback?account_id='+encodeURIComponent(accountId)+'&reason='+encodeURIComponent(reason)+'&confirmed=true'));}
  catch(e){adaptiveActionNotice('选股版本回滚失败',e.message);}
}
async function rollbackAdaptiveRebalance(accountId){
  var confirmation=await adaptiveConfirm({title:'回滚调仓版本',detail:'将恢复该策略上一版模拟盘调仓参数。',boundary:'只影响该模拟策略，不会改动历史成交。',reason:true,defaultReason:'人工复核后回滚调仓'}); if(!confirmation.approved) return;
  var reason=confirmation.reason;
  try{renderAdaptive(await apiPost('/api/adaptive/rebalance/rollback?account_id='+encodeURIComponent(accountId)+'&reason='+encodeURIComponent(reason)+'&confirmed=true'));}
  catch(e){adaptiveActionNotice('调仓版本回滚失败',e.message);}
}

function activateStrategyWorkspace(pageId){
  document.querySelectorAll('.page').forEach(function(x){x.classList.remove('active');});
  $(pageId).classList.add('active');
  document.querySelectorAll('.tab').forEach(function(x){x.classList.remove('active');x.setAttribute('aria-current','false');});
  var primary = document.querySelector('.tab[data-page="p-select"]');
  if(primary){primary.classList.add('active');primary.setAttribute('aria-current','page');}
  sessionStorage.setItem(APP_PAGE_KEY,'p-select');
  history.replaceState(null,'','#select');
}

function chooseStrategy(strategyId){
  activateStrategyWorkspace('p-select');
  $('selStrategy').value = strategyId;
  var option = $('selStrategy').options[$('selStrategy').selectedIndex];
  $('selStrategyDesc').textContent = option ? (option.dataset.desc || '') : '';
  document.querySelectorAll('#p-select .module-tab').forEach(function(x){
    var selected=x.dataset.strategy===strategyId;
    x.classList.toggle('active', selected);
    x.setAttribute('aria-selected',selected?'true':'false');
  });
  // 切换策略只读取最近一次已完成结果，不在进入页面或切换标签时重复计算。
  loadLatestSelection();
}

function showStrategyWatch(){
  // 选股模块已取消独立“自选观察”页，历史入口统一指向可筛选的策略验证。
  showSelectionValidation();
}

function showSelectionValidation(){ activateStrategyWorkspace('p-selection-evaluation'); loadSelectionEvaluation(); }
function selectionAssessmentClass(state){ return state==='validated' ? 'validated' : (state==='review' ? 'review' : 'watch'); }
function selectionAssessmentCopy(state){
  if(state==='review') return {label:'\u9700\u590d\u6838',advice:'\u6301\u7eed\u8dd1\u8f93\u57fa\u51c6\uff0c\u5148\u505a\u6837\u672c\u5916\u548c\u884c\u4e1a\u62c6\u89e3\uff0c\u4e0d\u81ea\u52a8\u4fee\u6539\u89c4\u5219\u3002'};
  if(state==='validated') return {label:'\u7ee7\u7eed\u9a8c\u8bc1',advice:'\u5f53\u524d\u51fa\u73b0\u6b63\u8d85\u989d\uff0c\u4ecd\u9700\u8986\u76d6\u66f4\u591a\u5e02\u573a\u9636\u6bb5\u3002'};
  if(state==='watch') return {label:'\u7ee7\u7eed\u89c2\u5bdf',advice:'\u6682\u672a\u5f62\u6210\u7a33\u5b9a\u4f18\u52bf\uff0c\u7ee7\u7eed\u79ef\u7d2f\u6837\u672c\u3002'};
  return {label:'\u6837\u672c\u79ef\u7d2f\u4e2d',advice:'\u6837\u672c\u4e0d\u8db3 20 \u4e2a\uff0c\u4e0d\u81ea\u52a8\u8c03\u6574\u7b56\u7565\u89c4\u5219\u3002'};
}
async function loadSelectionEvaluation(){
  var target=$('selectionEvaluationResult'); if(!target) return;
  target.innerHTML='<div class="loading">\u6b63\u5728\u8bfb\u53d6\u6bcf\u65e5\u9009\u80a1\u5feb\u7167\u4e0e\u524d\u77bb\u8868\u73b0\u2026</div>';
  try{
    var sid=$('selectionEvalStrategy').value, d=await api('/api/selection-evaluation?strategy='+encodeURIComponent(sid));
    window._selectionEvaluationPicks=d.latest_picks||[];
    var cards=(d.strategies||[]).map(function(s){ var m=(s.metrics||[]).filter(function(x){return x.horizon===10;})[0] || (s.metrics||[])[0] || {}, a=s.assessment||{}, copy=selectionAssessmentCopy(a.state);
      return '<article class="selection-validation-card '+selectionAssessmentClass(a.state)+'"><h3>'+s.strategy_name+'</h3><p><span class="tag '+(a.state==='review'?'tag-warn':(a.state==='validated'?'tag-ok':'tag-info'))+'">'+copy.label+'</span> '+copy.advice+'</p><div class="selection-validation-stats"><div class="selection-validation-stat">10\u65e5\u6837\u672c<b>'+((m.sample_count===undefined)?'-':m.sample_count)+'</b></div><div class="selection-validation-stat">\u5e73\u5747\u6536\u76ca<b class="'+pctCls(m.avg_return_pct)+'">'+pctTxt(m.avg_return_pct)+'</b></div><div class="selection-validation-stat">\u5e73\u5747\u8d85\u989d<b class="'+pctCls(m.avg_excess_pct)+'">'+pctTxt(m.avg_excess_pct)+'</b></div><div class="selection-validation-stat">\u80dc\u7387<b>'+((m.win_rate_pct===null||m.win_rate_pct===undefined)?'-':fmt(m.win_rate_pct)+'%')+'</b></div></div><div class="selection-validation-timeline">'+(s.metrics||[]).map(function(x){return '<span>'+x.horizon+'\u65e5 '+x.sample_count+'\u6837\u672c \u00b7 '+pctTxt(x.avg_excess_pct)+'</span>';}).join('')+'</div></article>'; }).join('') || '<div class="banner">\u5c1a\u65e0\u81ea\u52a8\u9009\u80a1\u5feb\u7167\u3002\u7cfb\u7edf\u5c06\u5728\u4e0b\u4e00\u4e2a\u4ea4\u6613\u65e5 15:25 \u540e\u5199\u5165\u9996\u6279\u7ed3\u679c\u3002</div>';
    var latestDate=(d.runs&&d.runs[0]) ? d.runs[0].run_date : '-', signalDate=(d.runs&&d.runs[0]) ? (d.runs[0].data_asof_date||'\u672a\u77e5') : '-';
    var pickRows=(d.latest_picks||[]).map(function(p){return '<tr class="selection-evaluation-pick" data-run-date="'+(p.run_date||'')+'" data-strategy="'+(p.strategy||'')+'" data-keyword="'+((p.code||'')+' '+(p.name||'')).toLowerCase()+'" data-return="'+(p.return_pct===null||p.return_pct===undefined?'pending':(p.return_pct>=0?'positive':'negative'))+'" data-holding="'+(p.holding_days||0)+'"><td>'+p.run_date+'</td><td>'+p.strategy_name+'</td><td>'+p.rank+'</td><td><b>'+p.name+'</b><br><span style="color:var(--text-muted);font-size:11px">'+p.code+' \u00b7 '+(p.industry||'-')+'</span></td><td>'+fmt(p.entry_price)+'</td><td>'+p.holding_days+'\u65e5</td><td>'+fmt(p.price)+'</td><td class="'+pctCls(p.return_pct)+'">'+pctTxt(p.return_pct)+'</td><td class="'+pctCls(p.excess_return_pct)+'">'+pctTxt(p.excess_return_pct)+'</td></tr>';}).join('');
    target.innerHTML='<div class="result-toolbar"><span class="tag tag-info">\u6700\u65b0\u5feb\u7167 '+latestDate+'</span><span class="tag tag-info">\u4fe1\u53f7K\u7ebf\u622a\u81f3 '+signalDate+'</span><span class="tag tag-info">\u57fa\u51c6 '+(d.benchmark||'\u6caa\u6df1300')+'</span><span class="tag tag-info">\u8ddf\u8e2a\u4e0a\u9650 '+d.tracking_days+' \u4e2a\u4ea4\u6613\u65e5</span>'+(d.kline_source?'<span class="tag tag-ok">'+d.kline_source+' · 完整日 '+(d.kline_latest_complete_date||'-')+'</span>':'')+'</div><section class="selection-validation-grid">'+cards+'</section>'+(pickRows?'<h3 style="margin-top:20px">\u6700\u8fd1\u5165\u9009\u4e0e\u540e\u7eed\u8868\u73b0</h3>'+tableScroll('<table><tr><th>\u4fe1\u53f7\u65e5</th><th>\u7b56\u7565</th><th>#</th><th>\u80a1\u7968</th><th>\u4fe1\u53f7\u4ef7</th><th>\u5df2\u8ddf\u8e2a</th><th>\u6700\u65b0\u6536\u76d8</th><th>\u6301\u6709\u6536\u76ca</th><th>\u76f8\u5bf9\u6caa\u6df1300</th></tr>'+pickRows+'</table>',920):'')+'<div class="disclaimer">'+(d.note||'')+'</div>'; filterSelectionEvaluationRows();
  }catch(e){ target.innerHTML='<div class="banner">\u8bfb\u53d6\u9a8c\u8bc1\u7ed3\u679c\u5931\u8d25\uff1a'+e.message+'</div>'; }
}
function filterSelectionEvaluationRows(){
  var keyword=(($('selectionEvalKeyword')||{}).value||'').trim().toLowerCase();
  var date=(($('selectionEvalDate')||{}).value||'');
  var result=(($('selectionEvalResult')||{}).value||'all');
  var horizon=Number((($('selectionEvalHorizon')||{}).value||0));
  var strategy=(($('selectionEvalStrategy')||{}).value||'');
  var visible=0,total=0;
  document.querySelectorAll('.selection-evaluation-pick').forEach(function(row){
    total++;
    var ok=(!keyword||row.dataset.keyword.indexOf(keyword)>=0)
      &&(!date||row.dataset.runDate===date)
      &&(!strategy||row.dataset.strategy===strategy)
      &&(result==='all'||row.dataset.return===result)
      &&(!horizon||Number(row.dataset.holding||0)>=horizon);
    row.hidden=!ok; if(ok) visible++;
  });
  var count=$('selectionEvalVisible'); if(count) count.textContent='显示 '+visible+' / '+total+' 条明细';
}
async function refreshSelectionEvaluation(){
  var button=$('selectionEvalRefresh'), status=$('selectionEvalStatus'); button.disabled=true; status.textContent='\u6b63\u5728\u6838\u9a8c\u5f53\u65e5\u6536\u76d8\u5feb\u7167\u2026';
  try{ var d=await apiPost('/api/selection-evaluation/refresh'); status.textContent=d.status==='ok' ? ('\u5df2\u5199\u5165 '+d.observed+' \u6761\u6536\u76d8\u8ddf\u8e2a\u8bb0\u5f55\u3002') : '\u5f53\u524d\u8fd8\u4e0d\u662f\u53ef\u5ba1\u8ba1\u7684\u5f53\u65e5\u6536\u76d8\u5feb\u7167\uff1b\u81ea\u52a8\u4efb\u52a1\u4f1a\u5728\u4e0b\u4e00\u4e2a\u4ea4\u6613\u65e5\u7ee7\u7eed\u3002'; await loadSelectionEvaluation(); }
  catch(e){ status.textContent='\u5237\u65b0\u5931\u8d25\uff1a'+e.message; } finally{ button.disabled=false; }
}

function renderClock(){ $('clock').textContent = new Date().toLocaleString('zh-CN',{hour12:false}); }
renderClock();
setInterval(renderClock,1000);

async function loadStrategies(){
  try{
    var d = await api('/api/strategies');
    var opts = d.strategies.map(function(s){ return '<option value="'+s.id+'" data-desc="'+s.desc+'">'+s.name+' — '+s.desc+'</option>'; }).join('');
    ['selStrategy','btStrategy','opStrategy'].forEach(function(id){ if($(id)) $(id).innerHTML = opts; });
    $('selStrategy').onchange = function(){
      var o = this.options[this.selectedIndex];
      $('selStrategyDesc').textContent = o.dataset.desc || '';
    };
    if($('btStrategy')) $('btStrategy').onchange = setBacktestCycle;
    if($('btStrategy')) setBacktestCycle();
    // 默认选中第一个策略；进入页面只读取最近一次结果，重新计算必须由用户点击按钮触发。
    if(d.strategies.length>0 && $('selStrategy').value===d.strategies[0].id){
      $('selStrategyDesc').textContent = d.strategies[0].desc;
      setTimeout(loadLatestSelection, 0);
    }
  }catch(e){ console.error('loadStrategies failed:', e); }
}

function setBacktestCycle(){
  var sid = $('btStrategy').value;
  $('btReb').value = sid==='ten_day' ? '10' : (sid==='five_day' ? '5' : '3');
}

async function loadOverview(){
  if(!$('idxCards')||!$('breadthChart')) return;
  try{
    var d = await api('/api/overview');
    var groups = {'A股':[], '港股':[], '美股':[]};
    d.indices.forEach(function(x){ if(groups[x.market]) groups[x.market].push(x); });
    var html = '';
    ['A股','港股','美股'].forEach(function(g){
      groups[g].forEach(function(x){
        html += '<div class="card"><div class="nm">'+x.name+' <span style="font-size:11px;color:#c4ccd6">'+g+'</span></div>'
          + '<div class="px '+pctCls(x.pct)+'">'+fmt(x.price)+'</div>'
          + '<div class="chg '+pctCls(x.pct)+'">'+(x.change>0?'+':'')+fmt(x.change)+'  '+pctTxt(x.pct)+'</div></div>';
      });
    });
    $('idxCards').innerHTML = html;
    var b = d.breadth;
    chart('breadthChart').setOption({
      tooltip:{},
      xAxis:{type:'value'},
      yAxis:{type:'category', data:['跌停','下跌','平盘','上涨','涨停']},
      series:[{type:'bar', data:[
        {value:b.limit_down, itemStyle:{color:'#0e6e3e'}},
        {value:b.down, itemStyle:{color:'#1a7f4b'}},
        {value:b.flat, itemStyle:{color:'#b0b8c2'}},
        {value:b.up, itemStyle:{color:'#d4380d'}},
        {value:b.limit_up, itemStyle:{color:'#8f1d05'}}
      ], label:{show:true, position:'right'}}],
      grid:{left:60, right:50, top:10, bottom:25},
      graphic:[{type:'text', left:'center', bottom:0, style:{text:'两市成交额：'+fmt(b.amount_yi,0)+' 亿元 · 共 '+b.total+' 只', fontSize:12, fill:'#7f8c9b'}}]
    });
    renderGate(d.gate);
  }catch(e){
    console.error('loadOverview failed:', e);
    $('idxCards').innerHTML = '<div class="banner">大盘数据加载失败，请稍后刷新</div>';
  }
}

function renderGate(g){
  var badge=$('gateBadge');
  if(!badge) return;
  g=g||{};
  var known = ['green','yellow','red'].indexOf(g.light)>=0;
  var cls = g.light==='red'?'gate-red':(g.light==='yellow'||!known?'gate-yellow':'gate-green');
  var txt = g.light==='red'?'海外风险：红灯':(g.light==='yellow'?'海外风险：黄灯':(g.light==='green'?'海外风险：绿灯':'海外风险：未知（保守）'));
  badge.className = 'gate-badge '+cls;
  badge.innerHTML = '<div class="gate-dot"></div><span>'+adaptiveEsc(txt)+'</span>';
  badge.title = adaptiveEsc(g.advice||'市场门控结果来自本地缓存；未知时按保守规则执行。');
}

async function loadMarketGate(){
  try{
    var overview=await api('/api/overview');
    renderGate(overview&&overview.gate);
  }catch(e){
    renderGate({light:'unknown',advice:'市场门控读取失败，执行层按保守规则处理。'});
  }
}

async function checkInit(){
  if(!$('initBanner')) return;
  try{
    var both = await Promise.all([api('/api/init/status'), api('/api/health')]);
    var st = both[0], h = both[1];
    if(st.status==='running'){
      var pct = st.total? Math.round(st.done/st.total*100) : 0;
      var phases = {preparing:'准备任务',building_universe:'更新全市场名单',downloading:'下载历史K线',retrying:'重试失败代码',finalizing:'写入质量清单'};
      $('initBanner').innerHTML = '<div class="banner" style="background:#e8f7ee;border-color:#a5d6b8;color:#1a7f4b">📡 '+(phases[st.phase]||'增量更新')+'：<b>'+st.done+'/'+st.total+'</b>只（最终失败'+st.errors+'只）'
        + '<div class="progress-bar"><div class="progress-fill" style="width:'+pct+'%"></div></div></div>';
      setTimeout(checkInit, 3000);
      return;
    }
    if(!st.data_ready){
      $('initBanner').innerHTML = '<div class="banner">首次使用需初始化沪深北全市场 3 年历史K线（当前约5500只，耗时取决于网络）。'
        + '<button style="margin-left:12px" onclick="startInit()">开始初始化</button></div>';
      return;
    }
    if(h.warnings && h.warnings.length){
      $('initBanner').innerHTML = '<div class="banner">数据状态：'+h.warnings.join('；')
        +'。最新交易日 '+(h.latest_trade_date||'-')+'，历史覆盖 '+h.kline_files+'/'+h.history_required+'（'+fmt(h.coverage_pct,1)+'%），待上市 '+h.pending_listing_count+' 只；'
        +'选股可用 '+h.selection_usable+' 只，回测可用 '+h.backtest_usable+' 只。'
        +'<button style="margin-left:12px" onclick="startInit()">增量补齐</button></div>';
    }else{
      $('initBanner').innerHTML = '<div style="font-size:12px;color:#6b7280;margin-bottom:10px">数据已就绪 · 全市场 '+h.universe_size+' 只（待上市 '+h.pending_listing_count+'）· 历史覆盖 '+fmt(h.coverage_pct,1)+'% · 选股可用 '+h.selection_usable+' · 回测可用 '+h.backtest_usable+'</div>';
    }
  }catch(e){
    $('initBanner').innerHTML = '<div class="banner">数据健康检查失败：'+e.message+'</div>';
  }
}
async function startInit(){ await apiPost('/api/init?years=3&size=0'); checkInit(); }

function dataValidityTone(value, good, warn){
  var n=Number(value); return n>=good?'good':(n>=warn?'warn':'bad');
}
function renderDataValidity(d){
  var c=d.coverage||{}, live=d.live_snapshot||{}, f=d.factor_cache||{}, u=d.incremental_update||{};
  var status=d.status==='ok'?'正常':(d.status==='degraded'?'需补数据':'不可用');
  var statusClass=d.status==='ok'?'tag-ok':(d.status==='degraded'?'tag-warn':'tag-warn');
  var cards=[
    ['全市场K线覆盖',fmt(c.coverage_pct,1)+'%',dataValidityTone(c.coverage_pct,98,90),c.kline_files+'/'+c.universe+' 只'],
    ['完整日线新鲜度',fmt(c.fresh_pct,1)+'%',dataValidityTone(c.fresh_pct,98,90), '最近完整交易日 '+(d.reference&&d.reference.expected_reference_date||'—')],
    ['盘中行情有效覆盖',fmt(live.valid_today_rows?live.valid_today_rows/Math.max(c.universe,1)*100:0,1)+'%',dataValidityTone(live.valid_today_rows?live.valid_today_rows/Math.max(c.universe,1)*100:0,98,90),live.valid_today_rows+' / '+(live.rows||0)+' 行'],
    ['选股因子缓存',fmt(f.eligible_factor_coverage_pct,1)+'%',dataValidityTone(f.eligible_factor_coverage_pct,98,90), '因子日 '+(f.factor_date||'—')]
  ];
  $('dataValidityStatus').className='tag '+statusClass; $('dataValidityStatus').textContent=status;
  var cancel=$('dataValidityCancel');
  if(cancel) cancel.style.display=['queued','running','cancelling'].indexOf(String(u.status||''))>=0?'inline-flex':'none';
  $('dataValidityCards').innerHTML=cards.map(function(x){return '<div class="data-validity-card '+x[2]+'"><small>'+x[0]+'</small><b>'+x[1]+'</b><em>'+x[3]+'</em></div>';}).join('');
  var warnings=(d.warnings||[]).slice(0,5).map(function(x){return '<li>'+adaptiveEsc(x)+'</li>';}).join('')||'<li>当前没有数据质量告警。</li>';
  var source=d.source_health||{};
  $('dataValidityDetails').innerHTML='<b>数据源：</b>'+adaptiveEsc(source.healthy?'主源与独立源正常':'存在降级或未返回')+' · <b>最新交易日：</b>'+adaptiveEsc((d.reference||{}).latest_trade_date||'—')+' · <b>不复权兜底：</b>'+Number(c.fallback_unadjusted||0)+' 只 · <b>源快照年龄：</b>'+ (live.age_seconds==null?'—':fmt(live.age_seconds,0)+' 秒')
    +'<div style="margin-top:5px"><b>告警：</b><ul style="margin:3px 0 0 18px">'+warnings+'</ul></div>'
    +'<div style="margin-top:6px;color:#7b8d84">人工更新：'+adaptiveEsc(u.status||'idle')+(u.job_id?' · '+adaptiveEsc(u.job_id):'')+(u.error?' · '+adaptiveEsc(u.error):'')+'</div>';
}
async function loadDataValidity(){
  var panel=$('dataValidityCards'); if(!panel) return;
  try{ var d=await api('/api/data-validity'); renderDataValidity(d); return d; }
  catch(e){ panel.innerHTML='<div class="banner">数据有效性读取失败：'+adaptiveEsc(e.message||e)+'</div>'; }
}
async function startManualDataUpdate(){
  var btn=$('dataValidityUpdate'); if(btn) {btn.disabled=true;btn.textContent='已提交，增量更新中…';}
  try{ await apiPost('/api/data-validity/incremental'); await loadDataValidity();
    clearInterval(window._dataValidityTimer);
    window._dataValidityTimer=setInterval(async function(){
      var d=await loadDataValidity();
      var s=d&&d.incremental_update&&d.incremental_update.status;
      if(s&&['completed','partial','failed','cancelled'].indexOf(s)>=0){
        clearInterval(window._dataValidityTimer);
        if(btn){btn.disabled=false;btn.textContent='人工增量更新';}
      }
    },4000);
  }catch(e){ if(btn){btn.disabled=false;btn.textContent='人工增量更新';} alert('增量更新未启动：'+(e.message||e)); }
}
async function startFactorIncrementalUpdate(){
  var btn=$('dataValidityFactor'); if(btn){btn.disabled=true;btn.textContent='因子重建中…';}
  try{
    await apiPost('/api/data-validity/factor/incremental');
    var d=await loadDataValidity();
    clearInterval(window._dataValidityTimer);
    window._dataValidityTimer=setInterval(async function(){
      var state=await loadDataValidity(), u=state&&state.incremental_update||{}, s=String(u.status||'');
      if(['completed','partial','failed','cancelled','idle'].indexOf(s)>=0){
        clearInterval(window._dataValidityTimer);
        if(btn){btn.disabled=false;btn.textContent='重建选股因子';}
      }
    },4000);
  }catch(e){ if(btn){btn.disabled=false;btn.textContent='重建选股因子';} alert('因子重建未启动：'+(e.message||e)); }
}
async function cancelManualDataUpdate(){
  var btn=$('dataValidityCancel'); if(btn){btn.disabled=true;btn.textContent='取消中…';}
  try{ await apiPost('/api/data-validity/incremental/cancel'); await loadDataValidity(); }
  catch(e){ alert('取消增量失败：'+(e.message||e)); }
  finally{ if(btn){btn.disabled=false;btn.textContent='取消增量';} }
}

async function loadSectors(){
  var type = $('sectorType').value;
  $('sectorTable').innerHTML = '<div class="loading">加载中…</div>';
  var d = await api('/api/sectors?type='+type);
  loadSectorEvents();
  var secs = d.sectors.slice(0, 60);
  var top = secs.slice(0, 20).reverse();
  chart('sectorChart').setOption({
    tooltip:{formatter:function(p){ return p.name+'<br/>主力净流入：'+yi(p.value); }},
    xAxis:{type:'value', axisLabel:{formatter:function(v){ return (v/1e8).toFixed(0)+'亿'; }}},
    yAxis:{type:'category', data: top.map(function(s){return s.name;})},
    series:[{type:'bar', data: top.map(function(s){ return {value:s.main_net, itemStyle:{color: s.main_net>0?'#d4380d':'#1a7f4b'}}; }), label:{show:true, position:'right', formatter:function(p){ return yi(p.value); }, fontSize:11}}],
    grid:{left:100, right:80, top:10, bottom:25}
  }, true);
  var rows = secs.map(function(s){
    return '<tr><td>'+s.name+'</td><td class="'+pctCls(s.pct)+'">'+pctTxt(s.pct)+'</td>'
      +'<td class="'+pctCls(s.main_net)+'">'+yi(s.main_net)+'</td>'
        +'<td class="'+pctCls(s.main_pct)+'">'+pctTxt(s.main_pct)+'</td>'
        +'<td class="'+pctCls(s.big_net)+'">'+yi(s.big_net)+'</td>'
        +'<td class="'+pctCls(s.mid_net)+'">'+yi(s.mid_net)+'</td>'
      +'<td class="'+pctCls(s.super_net)+'">'+yi(s.super_net)+'</td>'
      +'<td class="'+pctCls(s.small_net)+'">'+yi(s.small_net)+'</td>'
      +'<td>'+(s.top_stock||'-')+'</td></tr>';
  }).join('');
  $('sectorTable').innerHTML = tableScroll('<table><tr><th>板块</th><th>涨跌幅</th><th>主力净流入</th><th>主力占比</th><th>大单</th><th>中单</th><th>小单</th><th>领涨股</th></tr>'+rows+'</table>',860);
}

async function loadSectorEvents(){
  try{
    var d = await api('/api/sector_events?limit=10');
    var rows = (d.events||[]).map(function(s){
      var hits = s.events||[];
      var hitHtml = hits.length ? hits.map(function(h){
        var cls = h.tone<0?'tag-warn':(h.tone>0?'tag-ok':'tag-info');
        var label = h.tone<0?'风险':(h.tone>0?'催化':'提及');
        return '<div style="margin-top:5px"><span class="tag '+cls+'">'+label+'</span> '+h.summary+'<br/><span class="news-time">'+(h.time||'')+' · '+(h.source||'')+'</span></div>';
      }).join('') : '<span style="color:var(--text-muted)">暂无代表股事件命中</span>';
      return '<tr><td><b>'+s.sector+'</b><br/><span style="font-size:11px;color:var(--text-muted)">代表股 '+(s.top_stock||s.top_stock_code||'-')+'</span></td>'
        +'<td class="'+pctCls(s.pct)+'">'+pctTxt(s.pct)+'</td><td class="'+pctCls(s.main_pct)+'">'+pctTxt(s.main_pct)+'</td><td>'+hitHtml+'</td></tr>';
    }).join('');
    $('sectorEvents').innerHTML = tableScroll('<table><tr><th>板块</th><th>涨跌</th><th>主力占比</th><th>事件摘要</th></tr>'+rows+'</table>',760)+'<div class="disclaimer">'+(d.note||'')+'</div>';
  }catch(e){ $('sectorEvents').innerHTML = '<div style="color:var(--text-muted)">板块事件暂不可用：'+e.message+'</div>'; }
}

function renderSelectionResult(d, fromCache){
    if(d.need_init){ $('selectResult').innerHTML = '<div class="banner">'+d.message+'。请运行 start.bat 完成数据初始化后重试。</div>'; return; }
    if(!d.picks){ $('selectResult').innerHTML = '<div class="banner">暂无已完成的选股结果，请点击“开始选股”。</div>'; return; }
    var rows = d.picks.map(function(p, i){
      var reasons = (p.reasons||[]).map(function(r){ return '<li>'+r+'</li>'; }).join('');
      return '<tr><td>'+(i+1)+'</td><td><b>'+p.name+'</b><br/><span style="color:#9aa5b1;font-size:12px">'+p.code+' · '+(p.industry||'-')+'</span></td>'
        +'<td>'+fmt(p.price)+'</td>'
        +'<td>'+(p.net_profit!==null?yi(p.net_profit):(p.annual_net_profit!==null?yi(p.annual_net_profit):'-'))+'<br/><span style="font-size:11px;color:#9aa5b1">'+(p.report_date||p.annual_report_date||'')+(p.net_profit===null?' · 年报兜底':'')+'</span></td>'
        +'<td class="'+pctCls(p.super_net)+'">'+yi(p.super_net)+'</td>'
        +'<td class="'+pctCls(p.mom5)+'">'+pctTxt(p.mom5)+'</td>'
        +'<td class="'+pctCls(p.mom20)+'">'+pctTxt(p.mom20)+'</td>'
        +'<td class="'+pctCls(p.mom60)+'">'+pctTxt(p.mom60)+'</td>'
        +'<td class="'+pctCls(p.pct)+'">'+pctTxt(p.pct)+'</td>'
        +'<td><ul class="reasons">'+reasons+'</ul></td>'
        +'<td>'+buyTag(p.buy_decision)+'<br/><span style="font-size:11px;color:#9aa5b1">'+p.buy_decision.summary+'</span>'
        +(p.news_check && p.news_check.status!=='clean'?'<br/><span class="tag '+(p.news_check.status==='positive'?'tag-ok':'tag-info')+'" style="font-size:10px;padding:1px 4px">'+(p.news_check.status==='positive'?'利好':'提及')+'('+p.news_check.hits+')</span>':'')+'</td></tr>';
    }).join('');
    function buyTag(b){
      b=b||{};
      var cls = b.executable ? 'tag-ok' : (b.watchlist ? 'tag-info' : 'tag-warn');
      return '<span class="tag '+cls+'">'+(b.tier||'-')+' '+(b.action||'')+'</span>';
    }
    var gateHtml = d.gate? ('<span class="tag '+(d.gate.light==='red'?'tag-warn':(d.gate.light==='yellow'?'tag-warn':'tag-ok'))+'">'+d.gate.advice+'</span>') : '';
    $('selectResult').innerHTML =
      '<div class="result-toolbar">'+(fromCache?'<span class="tag tag-info">最近已完成结果（未重新计算）</span>':'<span class="tag tag-ok">刚刚完成</span>')+gateHtml
      +' <span class="tag tag-info">当前计算覆盖 '+d.universe_size+'/'+d.total_universe+'只</span>'
      +' <span class="tag tag-info">硬规则命中 '+(d.candidate_count||0)+'只</span>'
      +' <span class="tag tag-info">'+(d.flow_source||'超大单净流入排序')+'</span>'
      +' <span class="tag tag-info">最新财报：'+(d.latest_finance_report_date||d.annual_report_date||'-')+'</span></div>'
      +tableScroll('<table><tr><th>#</th><th>股票</th><th>现价</th><th>最新报告期净利润</th><th>超大单净流入</th><th>5日动量</th><th>20日动量</th><th>60日动量</th><th>今日涨跌</th><th>规则命中</th><th>买入决策</th></tr>'+rows+'</table>',1220)
      +'<div style="margin:8px 0"><span class="tag tag-ok">T1/T2 可执行 '+d.executable_count+'</span> <span class="tag tag-info">T3 观察 '+d.watchlist_count+'</span> <span class="tag tag-warn">T4/T5 放弃 '+(d.count-d.executable_count-d.watchlist_count)+'</span>'
      +' <span class="tag tag-info">舆情否决 '+((d.news_scan||{}).vetoed||0)+'</span>'
      +(d.first_board_candidates!==null && d.first_board_candidates!==undefined ? ' <span class="tag tag-ok">昨日首板 '+d.first_board_candidates+'只</span>' : '')
      +'</div>'
      +(d.news_vetoed&&d.news_vetoed.length?'<div style="margin:4px 0;padding:8px;background:#fff3cd;border:1px solid #ffc107;border-radius:4px"><b>舆情否决</b>：以下个股因负面舆情被一票否决，不进入选股结果<ul>'+d.news_vetoed.map(function(v){ return '<li>'+v.name+'('+v.code+') — '+v.reason+'</li>'; }).join('')+'</ul></div>':'')
      +'<div class="disclaimer">'+(d.disclaimer||'')+'</div>';
}

async function loadLatestSelection(){
  var target=$('selectResult'); if(!target||!$('selStrategy')) return;
  target.innerHTML='<div class="loading">正在读取最近一次已完成的选股结果…</div>';
  try{
    var d=await api('/api/select/latest?strategy='+encodeURIComponent($('selStrategy').value)+'&topn='+$('selTopn').value);
    if(d.found && !d.stale){
      renderSelectionResult(d,true);
    }else if(d.found && d.stale){
      // Never silently show an old trading-day snapshot.  A new trading day
      // automatically performs one fresh scan; if the source is unavailable,
      // the API's explicit retry message is shown instead.
      target.innerHTML='<div class="loading">'+(d.stale_reason||'最近结果已过期')+'，正在使用共享历史K线与实时行情重新选股…</div>';
      try{
        var fresh=await api('/api/select?strategy='+encodeURIComponent($('selStrategy').value)+'&topn='+$('selTopn').value);
        renderSelectionResult(fresh,false);
      }catch(refreshError){
        target.innerHTML='<div class="banner">'+(d.stale_reason||'最近结果已过期')+'。自动刷新失败：'+refreshError.message+'；请稍后重试。</div>';
      }
    }else { target.innerHTML='<div class="banner">尚无该策略的已完成结果，请点击“开始选股”生成。</div>'; }
  }catch(e){ target.innerHTML='<div class="banner">读取最近结果失败：'+e.message+'；如需重算请点击“开始选股”。</div>'; }
}

async function runSelect(){
  $('btnSelect').disabled = true;
  $('selectResult').innerHTML = '<div class="loading">正在计算因子并选股（约10-30秒）…</div>';
  try{
    var d = await api('/api/select?strategy='+$('selStrategy').value+'&topn='+$('selTopn').value);
    renderSelectionResult(d,false);
  }catch(e){
    $('selectResult').innerHTML = '<div class="banner">选股失败：'+e+'</div>';
  }finally{ $('btnSelect').disabled = false; }
}

async function compareStrategies(){
  $('selectResult').innerHTML = '<div class="loading">同时计算三大策略选股…</div>';
  var topn = $('selTopn').value;
  var strategies = [
    {id:'three_day',name:'三日策略'},
    {id:'five_day',name:'五日策略'},
    {id:'ten_day',name:'十日策略'}
  ];
  try{
    var results = await Promise.all(strategies.map(function(s){
      return api('/api/select?strategy='+encodeURIComponent(s.id)+'&topn='+encodeURIComponent(topn));
    }));
    var rows = '';
    for(var i=0;i<Math.max.apply(null,results.map(function(r){return r.picks.length;}));i++){
      rows += '<tr><td>'+(i+1)+'</td>';
      results.forEach(function(r){
        var p = r.picks[i];
        if(p){
          rows += '<td><b>'+adaptiveEsc(p.name||p.code)+'</b><br/><span style="color:var(--text-muted);font-size:11px">'+adaptiveEsc(p.code)+'</span></td><td class="'+pctCls(p.pct)+'">'+pctTxt(p.pct)+'</td><td>'+yi(p.super_net)+'</td><td>'+adaptiveEsc(p.buy_decision?p.buy_decision.tier:'-')+'</td>';
        }else{
          rows += '<td colspan="4" style="color:var(--text-muted)">—</td>';
        }
      });
      rows += '</tr>';
    }
    $('selectResult').innerHTML =
      '<div style="margin-bottom:8px"><span class="tag tag-info">Top '+adaptiveEsc(topn)+' 三策略对比</span></div>'
      +tableScroll('<table><tr><th>#</th><th>三日策略 股票</th><th>涨跌</th><th>超大单</th><th>买入</th>'
      +'<th>五日策略 股票</th><th>涨跌</th><th>超大单</th><th>买入</th>'
      +'<th>十日策略 股票</th><th>涨跌</th><th>超大单</th><th>买入</th></tr>'+rows+'</table>',1180)
      +'<div class="disclaimer">三种研究策略分别独立计算，同一只股票可能在多个策略中同时出现。仅供研究参考，不会改变两套模拟账户。</div>';
  }catch(e){ $('selectResult').innerHTML = '<div class="banner">对比失败：'+adaptiveEsc(e&&e.message||e)+'</div>'; }
}

// ---------- 板块联动 ----------
async function loadLinkage(){
  try{
    var d = await api('/api/sector_linkage');
    $('linkageSummary').innerHTML = '<div style="margin:8px 0;color:#5a7cfa;font-size:13px">'+d.summary+' <span class="tag tag-ok">共振中 '+d.resonating_count+'对</span></div>';
    var rows = d.pairs.map(function(p){
      var tag = p.resonance ? '<span class="tag tag-ok">共振中</span>' : (p.co_move_today ? '<span class="tag tag-info">今日同向</span>' : '');
      var fund = (p.a_main_yi!==null && p.b_main_yi!==null) ? ' 主力:'+p.a_main_yi+'亿 / '+p.b_main_yi+'亿' : '';
      return '<tr><td>'+p.a+'</td><td>'+p.b+'</td><td>'+p.corr+'</td><td class="'+pctCls(p.a_pct)+'">'+(p.a_pct!==null?pctTxt(p.a_pct):'-')+'</td><td class="'+pctCls(p.b_pct)+'">'+(p.b_pct!==null?pctTxt(p.b_pct):'-')+'</td><td style="font-size:12px;color:#9aa5b1">'+fund+'</td><td>'+tag+'</td></tr>';
    }).join('');
    $('linkageTable').innerHTML = tableScroll('<table><tr><th>板块A</th><th>板块B</th><th>相关性</th><th>A今日涨幅</th><th>B今日涨幅</th><th>今日主力资金</th><th>状态</th></tr>'+rows+'</table>',780)+'<div class="disclaimer">'+d.note+'</div>';
    // 热力图：取前15个行业构建相关性矩阵可视化
    var inds = [];
    var seen = {};
    for(var i=0;i<d.pairs.length && inds.length<15;i++){
      if(!seen[d.pairs[i].a]){ seen[d.pairs[i].a]=1; inds.push(d.pairs[i].a); }
      if(!seen[d.pairs[i].b] && inds.length<15){ seen[d.pairs[i].b]=1; inds.push(d.pairs[i].b); }
    }
    var hmData = [];
    for(var r=0;r<inds.length;r++){
      for(var c=0;c<inds.length;c++){
        var found = d.pairs.filter(function(p){ return (p.a===inds[r]&&p.b===inds[c])||(p.a===inds[c]&&p.b===inds[r]); });
        hmData.push([c, r, found.length ? found[0].corr : (r===c?1:0)]);
      }
    }
    var hm = chart('linkageChart');
    hm.setOption({
      tooltip:{formatter:function(p){ return inds[p.data[1]]+' <-> '+inds[p.data[0]]+'<br/>相关性: '+p.data[2]; }},
      grid:{left:120, right:30, top:10, bottom:60},
      xAxis:{type:'category', data:inds, axisLabel:{rotate:45,fontSize:11,interval:0}},
      yAxis:{type:'category', data:inds, axisLabel:{fontSize:11}},
      visualMap:{min:0.5, max:1, orient:'horizontal', left:'center', bottom:0, inRange:{color:['#e0e0e0','#ffd54f','#ff7043']}, calculable:true},
      series:[{type:'heatmap', data:hmData, label:{show:function(p){ return p.data[2]>0.75; },fontSize:10}}]
    }, true);
  }catch(e){ $('linkageTable').innerHTML = '<div class="banner">联动分析失败: '+e+'</div>'; }
}

// ---------- 持仓跟踪 ----------
async function trackAdd(code, name, price, strategy, btn){
  if(btn){ btn.disabled = true; btn.textContent = '…'; }
  try{
    var d = await apiPost('/api/track/add?code='+code+'&name='+encodeURIComponent(name)+(price?'&cost='+price:'')+'&strategy='+encodeURIComponent(strategy||''));
    if(btn){ btn.textContent = d.ok ? '已跟踪' : '已在池'; }
  }catch(e){ if(btn){ btn.disabled=false; btn.textContent='＋跟踪'; } alert('加入失败: '+e); }
}
async function trackAddAll(strategy){
  if(!confirm('确定将本次全部选股结果加入跟踪池？')) return;
  var rows = document.querySelectorAll('#selectResult table tr');
  var count = 0;
  for(var i=1;i<rows.length;i++){
    var cells = rows[i].querySelectorAll('td');
    if(!cells[1]) continue;
    var code = cells[1].textContent.match(/(\\d{6})/);
    if(!code) continue;
    var name = cells[1].querySelector('b');
    var nameTxt = name ? name.textContent : code[1];
    try{
      var d = await apiPost('/api/track/add?code='+code[1]+'&name='+encodeURIComponent(nameTxt)+'&strategy='+encodeURIComponent(strategy||''));
      if(d.ok) count++;
    }catch(e){}
    await new Promise(function(r){ setTimeout(r, 100); });
  }
  alert('已加入 '+count+' 只到跟踪池');
}
async function trackRemove(code){
  if(!confirm('移出跟踪池：'+code+'？')) return;
  await apiPost('/api/track/remove?code='+code);
  loadTrack();
}
function sigTag(s){
  var cls = s.level==='sell' ? 'tag-warn' : 'tag-info';
  return '<span class="tag '+cls+'" title="'+s.msg+'">'+s.type+'</span>';
}
function sellTag(sd){
  var cls = (sd.action==='卖出'||sd.auction_matrix.level==='sell') ? 'tag-warn' : (sd.action==='止盈减仓' ? 'tag-ok' : 'tag-info');
  return '<span class="tag '+cls+'">'+sd.action+'｜'+sd.auction_matrix.tier+'</span>';
}
async function loadTrack(){
  $('trackTable').innerHTML = '<div class="loading">检查跟踪池中（拉取实时行情）…</div>';
  try{
    var d = await api('/api/track/check');
    window._trackRaw = d;
    renderTrack(d);
  }catch(e){
    $('trackTable').innerHTML = '<div class="banner">加载失败：'+e+'</div>';
  }
}

function applyTrackFilter(){
  if(window._trackRaw) renderTrack(window._trackRaw);
}

function renderTrack(d){
  if(!d.positions || !d.positions.length){
    $('trackSummary').innerHTML = '';
    $('trackTable').innerHTML = '<div class="loading">跟踪池为空。到「策略选股」页选股后点「＋跟踪」加入。</div>';
    return;
  }
  // 应用筛选
  var strategyFilter = $('trackStrategyFilter') ? $('trackStrategyFilter').value : '';
  var timeFilter = $('trackTimeFilter') ? $('trackTimeFilter').value : 'all';
  var now = new Date();
  function inTime(added){
    if(timeFilter==='all' || !added) return true;
    var d = new Date(added);
    if(timeFilter==='today') return d.toDateString() === now.toDateString();
    if(timeFilter==='week') return (now - d) < 7*24*3600*1000;
    if(timeFilter==='month') return (now - d) < 30*24*3600*1000;
    if(timeFilter==='quarter') return (now - d) < 90*24*3600*1000;
    return true;
  }
  var filtered = d.positions.filter(function(x){
    var sOk = !strategyFilter || x.strategy === strategyFilter;
    var tOk = inTime(x.added_at);
    return sOk && tOk;
  });
  $('trackFilterCount').textContent = '显示 '+filtered.length+' / '+d.positions.length+' 只';
  if(!filtered.length){
    $('trackSummary').innerHTML = '';
    $('trackTable').innerHTML = '<div class="loading">无符合筛选条件的持仓</div>';
    return;
  }
  var p = d.portfolio;
  var brCls = p.breaker.indexOf('熔断')===0 ? 'tag-warn' : (p.breaker.indexOf('警戒')===0 ? 'tag-warn' : 'tag-ok');
  $('trackSummary').innerHTML = '<div class="metrics">'
    +'<div class="metric"><div class="v">'+p.count+'</div><div class="k">跟踪只数</div></div>'
    +'<div class="metric"><div class="v '+pctCls(p.avg_ret_pct)+'">'+pctTxt(p.avg_ret_pct)+'</div><div class="k">平均收益</div></div>'
    +'<div class="metric"><div class="v down">-'+fmt(p.avg_peak_drawdown)+'%</div><div class="k">平均峰值回撤</div></div>'
    +'<div class="metric"><div class="v" style="color:#e74c3c">'+p.sell_signals+'</div><div class="k">卖出提示</div></div>'
    +'<div class="metric"><div class="v" style="color:#d48806">'+p.warn_signals+'</div><div class="k">关注</div></div></div>'
    +'<div style="margin:8px 0"><span class="tag '+brCls+'">'+p.breaker+'</span> '
    +(p.concentration_warnings||[]).map(function(w){return '<span class="tag tag-warn">'+w+'</span>';}).join(' ')+'</div>'
    +(p.suggested_weights_pct? '<div style="font-size:12px;color:#9ca3af;margin-bottom:8px">波动率倒数仓位建议：'+Object.keys(p.suggested_weights_pct).map(function(c){return c+':'+p.suggested_weights_pct[c]+'%';}).join('，')+'</div>':'');
  var rows = filtered.map(function(x){
    var actCls = x.action==='卖出提示' ? 'style="color:#e74c3c;font-weight:600"' : (x.action==='关注' ? 'style="color:#d48806;font-weight:600"' : '');
    var retTag = x.ret_pct!==null ? ('<span class="tag '+(x.ret_pct>=0?'tag-ok':'tag-warn')+'" style="font-size:13px;font-weight:700">'+(x.ret_pct>=0?'+':'')+fmt(x.ret_pct,2)+'%</span>') : '-';
    return '<tr><td><b>'+x.name+'</b><br/><span style="color:#9ca3af;font-size:11px">'+x.code+' · '+(x.industry||'-')+'</span></td>'
      +'<td>'+fmt(x.cost)+'<br/><span style="font-size:11px;color:#9ca3af">'+(x.added_at||'')+'</span></td>'
      +'<td>'+fmt(x.price)+'</td>'
      +'<td class="'+pctCls(x.pct_today)+'">'+pctTxt(x.pct_today)+'</td>'
      +'<td>'+retTag+'</td>'
      +'<td class="down">'+(x.drawdown_from_peak!==null? '-'+fmt(x.drawdown_from_peak)+'%':'-')+'</td>'
      +'<td>'+(x.strategy_name||x.strategy||'-')+'</td>'
      +'<td>'+(x.hold_days!==null? x.hold_days+'日':'-')+'</td>'
      +'<td '+actCls+'>'+x.action+'</td>'
      +'<td>'+((x.signals||[]).map(sigTag).join(' ')||'-')+(x.signals&&x.signals.length? '<div style="font-size:11px;color:#9ca3af;margin-top:2px">'+x.signals.map(function(s){return s.msg;}).join('；')+'</div>':'')+'</td>'
      +'<td>'+sellTag(x.sell_decision)+'<br/><span style="font-size:11px;color:#9ca3af">'+x.sell_decision.summary+'</span></td>'
      +'<td><button class="danger" style="font-size:11px;padding:3px 8px;min-height:30px" onclick="trackRemove(\''+x.code+'\')">移出</button></td></tr>';
    }).join('');
    $('trackTable').innerHTML = tableScroll('<table><tr><th>股票</th><th>成本/加入日</th><th>现价</th><th>今日</th><th>收益</th><th>峰值回撤</th><th>来源策略</th><th>持有</th><th>状态</th><th>信号</th><th>卖出决策</th><th>操作</th></tr>'+rows+'</table>',1180)
      +'<div class="disclaimer">'+d.disclaimer+'（检查时间 '+d.checked_at+'）</div>';
}

// ---------- 策略模拟：独立账本，不与自选跟踪混用 ----------
function cny(v, signed){
  if(v===null||v===undefined||isNaN(v)) return '-';
  return ((signed&&v>0)?'+':'')+'￥'+Number(v).toFixed(2);
}
function paperStatusTag(status){
  var map={
    running:['tag-ok','运行中'],paused:['tag-info','已暂停'],
    pending:['tag-info','待执行'],filled:['tag-ok','已成交'],
    blocked:['tag-warn','已拦截'],rejected:['tag-warn','已拒绝'],
    superseded:['tag-info','已失效'],cancelled:['tag-info','已撤销']
  };
  var view=map[status]||['tag-info',status||'未知'];
  var cls=view[0], text=view[1];
  return '<span class="tag '+cls+'">'+text+'</span>';
}
function paperAuditBlock(title, count, body){
  return '<details class="paper-audit"><summary><span>'+title+'<span class="paper-audit-count">'+count+'</span></span><span class="paper-status-note">点击查看完整记录</span></summary><div class="paper-audit-body">'+body+'</div></details>';
}
function syncPaperCapitalHint(){
  var capital=Number($('paperCapital').value)||0;
  $('paperCapitalHint').textContent='两套策略共享总资金池；输入总金额 ¥'+capital.toLocaleString('zh-CN')+'，两套策略只共享资金，不共享决策和风控规则。';
}
async function startPaper(){
  var capital = Number($('paperCapital').value);
  if(!capital || capital<1000){ alert('请先设置总模拟资金，至少 1,000 元。'); return; }
  if(!confirm('将以总资金池 '+capital.toLocaleString('zh-CN')+' 元归档旧周期并同时启动两套策略。两套策略独立决策，共享现金和总仓位风控；不会连接券商或发送真实订单，是否继续？')) return;
  $('paperStart').disabled = true;
  try{
    var d = await apiPost('/api/paper/start?capital='+encodeURIComponent(capital));
    var note = d.schedule && d.schedule.ok ? '新周期已启动，3分钟监控任务已注册。' : '新周期已启动；计划任务未完全安装时可运行 setup_paper_schedule.bat。';
    alert(note);
    await loadPaper();
  }catch(e){ alert('启用失败：'+e.message); }
  finally{ $('paperStart').disabled = false; }
}
async function resumePaper(){
  try{ await apiPost('/api/paper/resume'); await loadPaper(); }
  catch(e){ alert('恢复失败：'+e.message); }
}
async function pausePaper(){
  try{ await apiPost('/api/paper/pause'); await loadPaper(); }
  catch(e){ alert('暂停失败：'+e.message); }
}
async function resetPaper(){
  var capital = Number($('paperCapital').value);
  if(!capital || capital<1000){ alert('请填写新周期的总模拟资金。'); return; }
  if(!confirm('完全重置会归档当前周期的订单、持仓、盈亏、风控和周报，并创建暂停的新周期；历史不会删除。继续吗？')) return;
  try{ await apiPost('/api/paper/reset?capital='+encodeURIComponent(capital)); await loadPaper(); }
  catch(e){ alert('重置失败：'+e.message); }
}
async function setPaperStyle(accountId, style){
  try{
    await apiPost('/api/paper/style?account_id='+encodeURIComponent(accountId)+'&style='+encodeURIComponent(style));
    await loadPaper();
  }catch(e){ alert('风格切换失败：'+e.message); }
}
async function runPaperNow(slot){
  try{
    $('paperStatus').textContent = '正在执行 '+slot+' 检查…';
    var d = await apiPost('/api/paper/run-now?slot='+encodeURIComponent(slot));
    $('paperStatus').textContent = d.status==='already_done' ? '本时段已执行，未重复下单。' : '检查完成。';
    await loadPaper();
  }catch(e){ $('paperStatus').textContent = '检查失败：'+e.message; }
}
window._paperOrderSide = 'buy';
function setPaperOrderSide(side){
  window._paperOrderSide=side;
  $('paperSideBuy').className=side==='buy'?'active buy':'';
  $('paperSideSell').className=side==='sell'?'active sell':'';
  $('paperSubmitOrder').textContent=side==='buy'?'确认模拟买入':'确认模拟卖出';
  $('paperSubmitOrder').style.background=side==='buy'?'#e14f46':'#238064';
  clearPaperOrderPreview();
}
function togglePaperLimitPrice(){
  $('paperLimitField').style.display=$('paperOrderType').value==='limit'?'block':'none';
  clearPaperOrderPreview();
}
function clearPaperOrderPreview(){
  window._paperOrderPlan=null;
  var box=$('paperOrderPreview');
  if(!box) return;
  var submit=$('paperSubmitOrder');
  if(submit) submit.disabled=false;
  box.className='paper-order-preview';
  box.innerHTML='数量填 0 时按价格、止损距离、现金与风险预算计算建议数量。每次提交都会重新经过模型与 T+1 校验。';
}
function paperPlanHasExecutableQty(plan){
  var qty=Number(plan&&plan.qty)||0;
  return qty>=100 && qty%100===0;
}
function paperOrderForm(){
  var raw=$('paperOrderCode').value.trim(), matched=raw.match(/(\d{6})/);
  return {
    account_id:$('paperOrderAccount').value,
    code:matched?matched[1]:raw,
    side:window._paperOrderSide||'buy',
    qty:Number($('paperOrderQty').value)||0,
    order_type:$('paperOrderType').value,
    limit_price:Number($('paperLimitPrice').value)||0
  };
}
function paperOrderQuery(form){
  var q='account_id='+encodeURIComponent(form.account_id)+'&code='+encodeURIComponent(form.code)+'&side='+form.side+'&qty='+form.qty+'&order_type='+form.order_type;
  if(form.order_type==='limit') q+='&limit_price='+encodeURIComponent(form.limit_price);
  return q;
}
function renderPaperOrderPreview(plan){
  var box=$('paperOrderPreview'), model=(plan.risk||{}).model||{}, reasons=plan.reasons||[];
  var executable=paperPlanHasExecutableQty(plan);
  var headline=plan.allowed&&executable?(plan.triggered?'模型通过，可模拟成交':'模型通过，等待限价触发'):
    (plan.allowed?'模型通过，但当前没有可执行数量':'模型拒绝本次委托');
  var cls=plan.allowed&&executable?'pass':'block';
  var detail='<b>'+headline+'</b><br>行情 '+fmt(plan.quote_price)+' · '+(plan.quote_at||'无时间戳')+' · '+(plan.quote&&plan.quote.quote_source||'-')
    +'<br>建议 '+(plan.recommended_qty||0)+' 股 · 本次 '+(plan.qty||0)+' 股 · 预计金额 '+cny(plan.amount)+' · 费用 '+cny(plan.fees);
  if(model.tier) detail+='<br>模型 '+model.tier+' · '+(model.action||'')+' · 综合分 '+fmt(Number(model.avg_score||0)*100,0)+'/100';
  if(plan.side==='sell') detail+='<br>可卖 '+(plan.available_qty||0)+' 股；当日锁定份额不会被扣减。';
  if(reasons.length) detail+='<br><span style="color:#a3473f">门禁：'+reasons.join('；')+'</span>';
  box.className='paper-order-preview '+cls;
  box.innerHTML=detail;
  var submit=$('paperSubmitOrder');
  if(submit) submit.disabled=!(plan.allowed&&executable);
}
async function previewPaperOrder(){
  var form=paperOrderForm();
  if(!form.account_id||!/^\d{6}$/.test(form.code)){ alert('请选择策略账户并输入六位证券代码。'); return null; }
  if(form.qty<0||form.qty%100){ alert('数量必须为 0 或 100 股的整数倍。'); return null; }
  if(form.order_type==='limit'&&!form.limit_price){ alert('请填写限价。'); return null; }
  $('paperOrderPreview').className='paper-order-preview';
  $('paperOrderPreview').textContent='模型正在检查行情时效、账户风险、仓位、T+1 和交易成本…';
  try{
    var plan=await api('/api/paper/order-preview?'+paperOrderQuery(form));
    window._paperOrderPlan=plan; renderPaperOrderPreview(plan); return plan;
  }catch(e){ $('paperOrderPreview').className='paper-order-preview block'; $('paperOrderPreview').textContent='预检失败：'+e.message; return null; }
}
async function submitPaperOrder(){
  var form=paperOrderForm(), plan=await previewPaperOrder();
  if(!plan||!plan.allowed) return;
  if(!paperPlanHasExecutableQty(plan)){
    renderPaperOrderPreview(Object.assign({},plan,{allowed:true,reasons:(plan.reasons||[]).concat(['当前模型可执行数量为 0 股，释放席位或等待下一轮扫描后再提交'])}));
    return;
  }
  var action=form.side==='buy'?'买入':'卖出';
  var state=plan.triggered?'立即按快照模拟成交':'进入当日限价委托';
  if(!confirm(action+' '+plan.name+' '+plan.qty+' 股，'+state+'。这是纯本地模拟，不会发送到券商，继续吗？')) return;
  $('paperSubmitOrder').disabled=true;
  try{
    var result=await apiPost('/api/paper/order/submit?'+paperOrderQuery(form)+'&confirmed=true');
    alert(result.status==='filled'?'模拟成交已写入账本。':(result.status==='pending_limit'?'限价委托已进入待触发队列。':'委托被模型拒绝。'));
    clearPaperOrderPreview(); await loadPaper();
  }catch(e){ alert('模拟委托失败：'+e.message); }
  finally{ $('paperSubmitOrder').disabled=false; }
}
function preparePaperSell(accountId,code,qty){
  $('paperOrderAccount').value=accountId; $('paperOrderCode').value=code; $('paperOrderQty').value=qty||0;
  $('paperOrderType').value='market'; togglePaperLimitPrice(); setPaperOrderSide('sell');
  $('paperOrderTicket').scrollIntoView({behavior:'smooth',block:'start'});
}
async function cancelPaperOrder(orderId){
  if(!confirm('撤销这笔待触发的模拟限价委托吗？')) return;
  try{ await apiPost('/api/paper/order/cancel?order_id='+encodeURIComponent(orderId)); await loadPaper(); }
  catch(e){ alert('撤单失败：'+e.message); }
}
function paperOrderStatusView(status,reason){
  // deferred_capacity is also used as the retryable queue marker for
  // entry-timing confirmation.  Keep the backend lifecycle status intact,
  // but show the actual reason instead of falsely reporting a cash shortage.
  if(status==='deferred_capacity' && /入场时机|确认中|确认未完成|回踩/.test(String(reason||'')))
    return ['pending','等待入场确认'];
  var map={
    filled:['filled','已成交'],pending_limit:['pending','待触发'],risk_rejected:['rejected','风控拒绝'],deferred_capacity:['pending','容量等待重排'],
    unfilled_limit_down:['rejected','跌停未成交'],cancelled:['cancelled','已撤销'],
    expired:['cancelled','已过期'],ready_to_fill:['pending','待成交']
  };
  return map[status]||['cancelled',status||'-'];
}
function setPaperTerminalFilter(kind,value){
  if(kind==='position') window._paperPositionFilter=value;
  if(kind==='positionState') window._paperPositionStateFilter=value;
  if(kind==='orderAccount') window._paperOrderAccountFilter=value;
  if(kind==='orderDate') window._paperOrderDateFilter=value;
  if(kind==='orderSide') window._paperOrderSideFilter=value;
  if(kind==='orderStatus') window._paperOrderStatusFilter=value;
  filterPaperTerminal();
}
function clearPaperOrderDate(){
  window._paperOrderDateFilter='';
  if($('paperOrderDateFilter')) $('paperOrderDateFilter').value='';
  filterPaperTerminal();
}
function filterPaperTerminal(){
  var positionAccount=window._paperPositionFilter||'all', positionState=window._paperPositionStateFilter||'all';
  var orderAccount=window._paperOrderAccountFilter||'all', orderSide=window._paperOrderSideFilter||'all', orderStatus=window._paperOrderStatusFilter||'all';
  var orderDate=window._paperOrderDateFilter===undefined?'':window._paperOrderDateFilter;
  var positionVisible=0, orderVisible=0;
  document.querySelectorAll('.paper-position-row[data-account]').forEach(function(row){
    var accountMatch=positionAccount==='all'||row.dataset.account===positionAccount;
    var stateMatch=positionState==='all'||row.dataset.pnl===positionState||row.dataset.sellable===positionState;
    var visible=accountMatch&&stateMatch;
    row.hidden=!visible; if(visible) positionVisible++;
  });
  document.querySelectorAll('.paper-order-row[data-account]').forEach(function(row){
    var accountMatch=orderAccount==='all'||row.dataset.account===orderAccount;
    var dateMatch=!orderDate||row.dataset.date===orderDate;
    var sideMatch=orderSide==='all'||row.dataset.side===orderSide;
    var statusMatch=orderStatus==='all'||row.dataset.status===orderStatus;
    var visible=accountMatch&&dateMatch&&sideMatch&&statusMatch;
    row.hidden=!visible; if(visible) orderVisible++;
  });
  if($('paperPositionVisible')) $('paperPositionVisible').textContent=positionVisible+' 只持仓';
  if($('paperOrderVisible')) $('paperOrderVisible').textContent=orderVisible+' 笔操作';
  if($('paperPositionEmpty')) $('paperPositionEmpty').hidden=positionVisible>0;
  if($('paperOrderEmpty')) $('paperOrderEmpty').hidden=orderVisible>0;
}
function riskText(value){
  return String(value===null||value===undefined||value===''?'-':value)
    .replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;').replace(/"/g,'&quot;');
}
function zhRiskText(value){
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
function riskLevelView(level){
  return ({normal:['✓','正常'],watch:['!','关注'],tightened:['↓','收紧'],blocked:['×','禁止开仓']}[level]||['?','未知']);
}
function riskMetric(label,current,limit){
  var c=current===null||current===undefined?'—':fmt(current,2)+'%';
  var l=limit===null||limit===undefined?'—':fmt(limit,2)+'%';
  return '<div class="paper-risk-metric">'+label+'<b>'+c+' / '+l+'</b></div>';
}
async function loadPaperStrategyCenter(){
  var target=$('paperStrategyView');
  if(!target) return;
  target.innerHTML='<div class="loading">正在读取策略规则…</div>';
  try{
    var d=await api('/api/paper/strategy-center');
    var cards=(d.strategies||[]).filter(function(s){return s.supports_new_cycle===true;}).map(function(s){
      return '<article class="paper-strategy-card"><header><b>'+riskText(s.name)+'</b><span>'+riskText(s.mode)+' · '+riskText(s.entry_model)+'</span></header>'
        +'<div class="paper-strategy-section"><label>候选来源</label><p>'+riskText(s.candidate)+'</p></div>'
        +'<div class="paper-strategy-section"><label>入场执行</label><p>'+riskText(s.entry)+'</p></div>'
        +'<div class="paper-strategy-section"><label>退出纪律</label><p>'+riskText(s.exit)+'</p></div>'
        +'<div class="paper-strategy-section"><label>风险边界</label><div class="paper-strategy-metrics"><div>持仓周期<b>'+riskText(s.hold_range)+'</b></div><div>单股 / 总池预算<b>'+fmt(s.max_weight_pct,1)+'% / '+fmt(s.pool_budget_pct,2)+'%</b></div><div>保底 / 风险画像<b>'+fmt(s.pool_floor_pct,2)+'% / '+fmt(s.max_exposure_pct,0)+'%</b></div><div>单日亏损 / 回撤<b>'+fmt(s.daily_loss_pct,1)+'% / '+fmt(s.drawdown_pct,1)+'%</b></div><div>行业 / 冷却<b>'+fmt(s.industry_limit_pct,0)+'% / '+riskText(s.cooldown_days)+'天</b></div></div></div></article>';
    }).join('');
    var guards=(d.shared_guards||[]).map(function(item){return '<li>'+riskText(item)+'</li>';}).join('');
    target.innerHTML='<section class="paper-strategy-intro"><div><h3>模拟盘策略中心</h3><p>这里展示的是当前服务器实际生效的策略定义。策略规则与模拟账户共用同一配置来源；本页只读，查看不会触发下单或改动资金。</p></div></section><section class="paper-strategy-grid">'+cards+'</section><section class="paper-strategy-guards"><b>共同执行边界</b><ul>'+guards+'</ul></section>';
  }catch(e){target.innerHTML='<div class="banner">策略中心读取失败：'+riskText(e.message||e)+'</div>';}
}
function paperResearchStrategyName(id){
  return ({tq_breakout:'短线日内做T',main_force_top10:'超强主力股'})[id]||id||'未知策略';
}
function paperResearchOutcome(metric,horizon){
  if(!metric) return '<span class="paper-research-empty">等待 '+horizon+' 日观察</span>';
  return '<b class="'+pctCls(metric.avg_return_pct)+'">'+pctTxt(metric.avg_return_pct)+'</b><small>'+Number(metric.samples||0)+' 个样本 · 胜率 '+fmt(metric.win_rate_pct,1)+'%</small>';
}
function paperResearchQuality(run){
  var q=(run&&run.data_quality)||{}, usable=Number(q.universe_size||0), stale=Number(q.dropped_stale_rows||0);
  if(!run) return {tone:'empty',label:'尚未记录',detail:'下一次盘后候选生成后自动写入'};
  if(!usable) return {tone:'caution',label:'数据不足',detail:'未形成可核验的因子范围'};
  if(stale>usable) return {tone:'caution',label:'覆盖待补',detail:'可用因子 '+usable+' · 过期剔除 '+stale};
  return {tone:'ready',label:'已记录',detail:'可用因子 '+usable+' · 过期剔除 '+stale};
}
async function loadPaperResearchValidation(){
  var target=$('paperResearchView');
  if(!target) return;
  target.innerHTML='<div class="loading">正在读取模拟盘策略的候选快照与兑现记录…</div>';
  try{
    var d=await api('/api/paper/research-validation?limit=90');
    var latestByStrategy={}, policy=d.backfill_policy||{};
    (d.runs||[]).forEach(function(run){ if(!latestByStrategy[run.account_id]) latestByStrategy[run.account_id]=run; });
    var ids=['tq_breakout','main_force_top10'];
    var cards=ids.map(function(id){
      var run=latestByStrategy[id], quality=paperResearchQuality(run), metrics=(d.metrics||{})[id]||{};
      var horizons=[1,3,5].map(function(h){return '<div class="paper-research-outcome"><span>'+h+'日</span>'+paperResearchOutcome(metrics[String(h)],h)+'</div>';}).join('');
      var q=(run&&run.data_quality)||{};
      var shortCode={tq_breakout:'T',main_force_top10:'主'}[id]||'证';
      return '<article class="paper-research-card '+quality.tone+'"><header><div><span class="paper-research-code">'+shortCode+'</span><h3>'+paperResearchStrategyName(id)+'</h3></div><em>'+quality.label+'</em></header>'
        +'<p>'+quality.detail+'</p><dl><div><dt>信号日</dt><dd>'+riskText((run&&run.signal_date)||'—')+'</dd></div><div><dt>候选快照</dt><dd>'+Number((run&&run.candidate_count)||0)+' 只</dd></div><div><dt>因子截至</dt><dd>'+riskText((run&&run.factor_asof_date)||'—')+'</dd></div></dl>'
        +'<div class="paper-research-outcomes">'+horizons+'</div>'
        +'<footer>财务披露时点：'+(q.financial_point_in_time==='unverified_disclosure_timestamp'?'待点时校验':'已记录')+'</footer></article>';
    }).join('');
    var rows=ids.map(function(id){
      var run=latestByStrategy[id], q=(run&&run.data_quality)||{}, quality=paperResearchQuality(run);
      return '<tr><td><b>'+paperResearchStrategyName(id)+'</b><br><small>'+riskText((run&&run.model_family)||'—')+'</small></td><td>'+riskText((run&&run.signal_date)||'—')+'</td><td>'+Number((run&&run.candidate_count)||0)+' 只</td><td>'+riskText((run&&run.factor_asof_date)||'—')+'</td><td>'+riskText((run&&run.factor_oldest_date)||'—')+'</td><td><span class="paper-research-quality '+quality.tone+'">'+quality.label+'</span><small>'+riskText(quality.detail)+'</small></td></tr>';
    }).join('');
    var runCount=(d.runs||[]).length;
    var manualAllowed=policy.manual_allowed===true;
    var manualLabel=policy.manual_label||'收盘后可补录';
    target.innerHTML='<section class="paper-research-hero"><div><span class="page-kicker">SHADOW EVIDENCE · PAPER ONLY</span><h3>两策略研究证据</h3><p>每个交易日收盘后固定候选、评分构成与可用数据范围，再跟踪后续表现。它不下单、不调参，也不会改动风控。</p></div><div class="paper-research-hero-actions"><span class="tag tag-info">已记录 '+runCount+' 份策略快照</span><span class="paper-research-schedule">自动：'+riskText(policy.scheduled_at||'每个交易日收盘后')+'<small>'+riskText(policy.next_observation||'后续有效收盘快照会补齐观察')+'</small></span><button class="ghost" type="button" onclick="refreshPaperResearchValidation(this)">刷新记录</button><button class="ghost paper-research-backfill" type="button" title="'+riskText(policy.manual_scope||'')+'" onclick="backfillPaperResearch(this)" '+(manualAllowed?'':'disabled')+'>'+riskText(manualLabel)+'</button><span id="paperResearchActionStatus" class="paper-research-action-status" role="status" aria-live="polite"></span></div></section>'
      +'<section class="paper-research-ladder" aria-label="研究兑现周期"><span>候选固定</span><i></i><span>1日观察</span><i></i><span>3日复核</span><i></i><span>5日对比</span><i></i><span>10日验证</span><i></i><span>20日人工复核</span></section>'
      +'<section class="paper-research-grid">'+cards+'</section>'
      +'<section class="paper-research-table"><header><div><h3>最新可核验快照</h3><p>只有收盘后写入的候选才会计入研究；数据不完整会明确标记，不会伪装成有效样本。</p></div><span class="tag tag-warn">影子验证中</span></header>'+tableScroll('<table><thead><tr><th>模拟盘策略</th><th>信号日</th><th>候选</th><th>因子截至</th><th>最早因子</th><th>数据质量</th></tr></thead><tbody>'+rows+'</tbody></table>',900)+'</section>'
      +'<p class="paper-research-note">当前为第一批样本。'+riskText(policy.next_observation||'1 日、3 日、5 日结果会在后续有效收盘快照到达后自动补齐')+'。手动补录只允许使用当日完整收盘快照，不能拿今天数据回写旧候选；样本不足 20 个时，系统只显示积累状态，不允许据此自动修改任何策略。</p>';
  }catch(e){ target.innerHTML='<div class="banner">策略证据读取失败：'+riskText(e.message||e)+'</div>'; }
}
async function refreshPaperResearchValidation(button){
  if(button){button.disabled=true;button.textContent='正在刷新…';}
  try{ await loadPaperResearchValidation(); }
  finally{ if(button){button.disabled=false;button.textContent='刷新记录';} }
}
function setPaperResearchActionStatus(message,tone){
  var target=$('paperResearchActionStatus'); if(!target) return;
  target.textContent=message||'';
  target.className='paper-research-action-status '+(tone||'');
}
async function backfillPaperResearch(button){
  if(!confirm('仅补录当日完整收盘快照与既有样本的当日观察；不会生成买卖信号、委托或风控决策。现在执行吗？')) return;
  if(button){button.disabled=true;button.textContent='补录中…';}
  try{
    var result=await apiPost('/api/paper/research-validation/backfill');
    await loadPaperResearchValidation();
    if(result.status==='completed'){
      var saved=(result.accounts||[]).map(function(item){return paperResearchStrategyName(item.id)+' '+Number(item.candidates||0)+' 只';}).join('、');
      setPaperResearchActionStatus('已补录：'+(saved||'当日观察已刷新'),'ready');
    }else{
      setPaperResearchActionStatus(result.reason||'当前不满足补录条件','caution');
    }
  }catch(e){
    setPaperResearchActionStatus('补录失败：'+(e.message||e),'caution');
  }finally{
    if(button&&button.isConnected){button.disabled=false;button.textContent='补录当日收盘快照';}
  }
}
function showPaperWorkspace(view,button,options){
  options=options||{};
  if(['strategy','research','portfolio','activity','history','risk'].indexOf(view)<0) view='portfolio';
  ['strategy','research','portfolio','activity','history','risk'].forEach(function(key){ var panel=$('paper'+key.charAt(0).toUpperCase()+key.slice(1)+'View'); if(panel) panel.hidden=key!==view; });
  document.querySelectorAll('#p-paper [data-paper-view]').forEach(function(item){ var selected=item.dataset.paperView===view; item.classList.toggle('active',selected); item.setAttribute('aria-selected',selected?'true':'false'); });
  window._paperWorkspace=view;
  sessionStorage.setItem(PAPER_VIEW_KEY,view);
  if(!options.restore) history.replaceState(null,'','#paper/'+view);
  if(view==='strategy') loadPaperStrategyCenter(); else if(view==='research') loadPaperResearchValidation(); else if(view==='risk') loadPaperRisk(false); else loadPaper();
}
function applyPaperRiskAuditFilter(){
  var account=$('paperRiskAccountFilter')?$('paperRiskAccountFilter').value:'';
  var level=$('paperRiskLevelFilter')?$('paperRiskLevelFilter').value:'';
  var date=$('paperRiskDateFilter')?$('paperRiskDateFilter').value:'';
  document.querySelectorAll('#paperRiskAuditRows tr').forEach(function(row){
    row.hidden=!!((account&&row.dataset.account!==account)||(level&&row.dataset.level!==level)||(date&&row.dataset.date!==date));
  });
}
function renderPaperAudit(d){
  // A fast tab switch can finish an overview request that was started from a
  // different workspace.  Never let a missing optional audit payload abort
  // the entire paper dashboard render.
  d=(d&&typeof d==='object')?d:{};
  var options='<option value="">全部策略</option>'+(d.accounts||[]).map(function(a){return '<option value="'+riskText(a.id)+'">'+riskText(a.name)+'</option>';}).join('');
  var audit=(d.alerts||[]).map(function(a){
    var validation=a.quote_validation||'';
    var validationLabel=validation==='cross_source_checked'?'通过':(validation==='cross_source_failed'?'未通过':(validation==='cross_source_unavailable'?'未返回':(validation==='not_applicable'?'不适用':(validation==='range_timestamp_checked'?'主源通过':(validation==='degraded_cross_source'?'降级核验':(validation==='stale'?'主源过期':'未核验'))))));
    var validationClass=validation==='cross_source_checked'?'ok':(validation==='cross_source_failed'||validation==='cross_source_unavailable'?'bad':(validation==='not_applicable'?'info':'warn'));
    var validationText=(a.quote_validation_detail||(validation==='not_applicable'?'账户级风险状态，不涉及个股行情':'未提供独立行情源校验结果'))+(a.cross_price_gap_pct!=null?'；价格差 '+fmt(a.cross_price_gap_pct,3)+'%':'')+(a.cross_pct_gap!=null?'；涨跌幅差 '+fmt(a.cross_pct_gap,3)+'%':'');
    var linked=a.linked_signal, actionText=a.action||'—';
    if(linked) actionText+=' · '+(linked.label||'待执行委托')+' #'+riskText(linked.id)+'（'+riskText(linked.intended_date||'待定')+'）';
    var symbol=[a.name,a.code].filter(function(value){return value!==null&&value!==undefined&&value!=='';}).map(riskText).join(' ')||'—';
    return '<tr data-account="'+riskText(a.account_id)+'" data-level="'+riskText(a.level)+'" data-date="'+riskText(String(a.time||'').slice(0,10))+'"><td>'+riskText(a.time)+'</td><td>'+riskText(a.account_name)+'</td><td>'+symbol+'</td><td><span class="risk-pill '+riskText(a.level)+'">'+riskText(riskLevelView(a.level)[1])+'</span></td><td>'+zhRiskText(a.reason)+'</td><td><span class="quote-check '+validationClass+'" title="'+riskText(validationText)+'">'+validationLabel+'</span><small class="quote-check-detail">'+riskText(validationText)+'</small></td><td>'+zhRiskText(actionText)+'</td><td>'+zhRiskText(a.execution_mode)+'</td><td>'+zhRiskText(a.rule_version)+'</td></tr>';
  }).join('');
  return '<section class="paper-terminal-section paper-risk-audit-section" style="border-top:1px solid var(--border-light)"><div class="paper-risk-toolbar"><h3 style="margin:0;border:0;padding:0">风控审计记录</h3><div class="controls" style="margin:0"><select id="paperRiskAccountFilter" onchange="applyPaperRiskAuditFilter()">'+options+'</select><select id="paperRiskLevelFilter" onchange="applyPaperRiskAuditFilter()"><option value="">全部等级</option><option value="watch">关注</option><option value="tightened">收紧</option><option value="blocked">禁止</option></select><input id="paperRiskDateFilter" type="date" onchange="applyPaperRiskAuditFilter()"></div></div>'+tableScroll('<table><tr><th>时间</th><th>策略</th><th>标的</th><th>等级</th><th>原因</th><th>行情核验</th><th>系统动作</th><th>模式</th><th>版本</th></tr><tbody id="paperRiskAuditRows">'+audit+'</tbody></table>',1180)+'</section>';
}
function renderPaperRisk(d){
  if(d.initializing){
    $('paperRiskResult').innerHTML='<div class="loading">'+riskText(d.message||'正在后台建立风控快照…')+'</div>';
    return;
  }
  var overall=d.overall||{},ov=riskLevelView(overall.level);
  var dynamic=d.dynamic_risk||{},dynamicNews=dynamic.news||{},sourceHealth=d.data_source_health||{},dynamicCodeText=Object.keys(dynamicNews.codes||{}).slice(0,8).map(function(code){var item=dynamicNews.codes[code]||{};return code+' '+(item.verified_negative?'公告否决':'舆情收紧');}).join('、')||'暂无受影响个股';
  var sourceLabel=sourceHealth.healthy?'主源与独立源正常':(sourceHealth.reconnected?'已重连并切换备用源':'等待自动重连');
  var dynamicCard='<section class="paper-risk-dynamic '+riskText(dynamic.mode||'normal')+'"><div><h3>统一动态风控 <span class="risk-pill '+riskText(dynamic.mode||'normal')+'">'+zhRiskText(dynamic.label||'正常')+'</span></h3><p>'+zhRiskText(dynamic.reason||'暂无动态收紧原因')+'</p></div><div class="paper-risk-dynamic-metrics"><span>新增风险额度 <b>'+fmt(dynamic.risk_scale_pct,1)+'%</b></span><span>负面事件 <b>'+riskText(dynamicNews.negative_count||0)+'</b></span><span>核验负面 <b>'+riskText(dynamicNews.verified_negative_count||0)+'</b></span><span>影响标的 <b>'+riskText(dynamicCodeText)+'</b></span></div><small>数据源：'+riskText(sourceLabel)+' · 最近检查 '+riskText(sourceHealth.checked_at||'暂无')+'；'+zhRiskText(dynamic.policy||'核验负面公告按个股禁止新开仓；未核验负面只降级仓位。')+'</small></section>';
  var html='<section class="paper-risk-header risk-'+riskText(overall.level)+'"><div class="paper-risk-state"><div class="paper-risk-icon">'+ov[0]+'</div><div><h3>'+ov[1]+'｜'+zhRiskText(overall.trade_permission)+'</h3><p>'+zhRiskText(overall.summary)+'<br>卖出监控：'+zhRiskText(overall.sell_monitoring)+'</p></div></div><div class="paper-risk-meta"><span class="risk-pill '+riskText(overall.level)+'">'+zhRiskText(d.mode)+'</span><span class="risk-pill">版本 '+zhRiskText(d.rule_version)+'</span><span class="risk-pill">快照 '+riskText(d.asof)+'</span></div></section>'+dynamicCard;
  html+='<section class="paper-risk-account-grid">'+(d.accounts||[]).map(function(a){
    var v=riskLevelView(a.level),m=a.metrics||{};
    return '<article class="paper-risk-account risk-'+riskText(a.level)+'"><div class="paper-risk-account-head"><h3>'+riskText(a.name)+'</h3><span class="risk-pill '+riskText(a.level)+'">'+v[0]+' '+v[1]+'</span></div><p>'+zhRiskText(a.trade_permission)+'<br>'+zhRiskText(a.summary)+'</p><div class="paper-risk-metrics">'
      +riskMetric('策略仓位 / 动态上限',m.position_exposure_pct,m.max_exposure_pct)+riskMetric('总池占用 / 硬上限',m.pool_exposure_pct,m.pool_limit_pct)+riskMetric('最大单票 / 上限',m.largest_position_pct,m.max_position_pct)+riskMetric('最大行业 / 上限',m.largest_industry_pct,m.max_industry_pct)+riskMetric('回撤 / 熔断线',m.rolling_drawdown_pct,m.drawdown_limit_pct)
      +'<div class="paper-risk-budget-note">基础预算 '+fmt(m.strategy_budget_pct,2)+'% · 保底 '+fmt(m.strategy_floor_pct,2)+'% · 可转入 '+fmt(m.redistribution_available_pct,2)+'% · 市场系数 '+fmt(m.market_scale_pct,0)+'%</div>'
      +'</div><ul class="paper-risk-drivers">'+(a.drivers||[]).map(function(x){return '<li>'+zhRiskText(x)+'</li>';}).join('')+'</ul></article>';
  }).join('')+'</section>';
  var market=d.market||{},fund=d.fund_flow||{},sent=d.sentiment||{},crowd=d.crowding||{};
  var marketCard='<section class="panel paper-risk-factor"><h3>大盘与市场状态 <span class="tag tag-info">执行门禁</span></h3><div class="paper-risk-factor-list"><div class="paper-risk-factor-item">沪深300涨跌<b>'+pctTxt(market.live_index_pct)+'</b></div><div class="paper-risk-factor-item">沪深300点位<b>'+fmt(market.live_index_price,2)+'</b></div><div class="paper-risk-factor-item">市场灯号<b>'+zhRiskText(market.light)+'</b></div><div class="paper-risk-factor-item">上涨 / 下跌<b>'+riskText(market.up)+' / '+riskText(market.down)+'</b></div><div class="paper-risk-factor-item">上涨占比<b>'+fmt(market.breadth_up_pct,1)+'%</b></div><div class="paper-risk-factor-item">涨跌中位数<b>'+pctTxt(market.median_pct)+'</b></div><div class="paper-risk-factor-item">MA20结构<b>'+(market.benchmark_above_ma20===null?'\u672a\u77e5':(market.benchmark_above_ma20?'\u4e0a\u65b9':'\u4e0b\u65b9'))+'</b></div><div class="paper-risk-factor-item">沪深300 5日<b>'+pctTxt(market.benchmark_5d_pct)+'</b></div><div class="paper-risk-factor-item">海外风险<b>'+zhRiskText((market.overseas||{}).light)+'</b></div></div><div class="paper-risk-notice">市场宽度是本地全市场快照，仅作参考；不会伪装成实时散户数据。</div></section>';
  var trendMap={};(fund.position_trends||[]).forEach(function(item){trendMap[item.account_id+'|'+item.code]=item;});
  var flowRows=(d.position_queue||[]).map(function(p){var trend=trendMap[p.account_id+'|'+p.code]||{};return '<tr><td>'+riskText(p.account_name)+'</td><td>'+riskText(p.name)+' '+riskText(p.code)+'</td><td class="'+pctCls(p.main_pct)+'">'+pctTxt(p.main_pct)+'</td><td>'+riskText(p.quote_at)+'</td><td>'+riskText(trend.sample_count||0)+'</td><td>'+riskText(trend.trend||'样本不足')+'</td></tr>';}).join('');
  var flowCard='<section class="panel paper-risk-factor"><h3>主力资金代理 <span class="tag tag-info">影子观察</span></h3>'+tableScroll('<table><tr><th>策略</th><th>持仓</th><th>主力占比</th><th>源时间</th><th>样本</th><th>连续性</th></tr>'+flowRows+'</table>',690)+'<div class="paper-risk-notice">'+zhRiskText(fund.notice)+'</div></section>';
  var eventRows=(sent.events||[]).map(function(e){var tag=e.verified?'公司公告':(e.tone<0?'负面快讯':'快讯提示'),cls=e.verified?'tag-info':(e.tone<0?'tag-warn':'tag-ok');return '<div class="news-item"><span class="tag '+cls+'">'+tag+'</span><b>'+riskText(e.name)+' '+riskText(e.code)+'</b><br>'+riskText(e.summary)+'<br><span class="news-time">'+riskText(e.time)+' · '+riskText(e.source)+' · '+riskText((e.keywords||[]).join('、'))+'</span></div>';}).join('')||'<div class="paper-empty">'+(sent.scan_status==='failed'?'事件扫描未完成，不能解释为“无负面”。':'本轮未发现持仓相关事件。')+'</div>';
  var sentimentCard='<section class="panel paper-risk-factor"><h3>舆情与事件 <span class="tag tag-info">动态风控</span></h3><div class="section-note">事件 '+riskText(sent.event_count||0)+' 条 · 负面 '+riskText(sent.warning_count||0)+' 条 · 可追溯公告 '+riskText(sent.verified_event_count||0)+' 条</div>'+eventRows+'<div class="paper-risk-notice">'+zhRiskText(sent.notice)+'</div></section>';
  var crowdCard='<section class="panel paper-risk-factor"><h3>拥挤度与散户行为代理 <span class="tag tag-info">影子观察</span></h3><div class="paper-risk-factor-list"><div class="paper-risk-factor-item">市场宽度<b>'+fmt(crowd.market_width_pct,1)+'%</b></div><div class="paper-risk-factor-item">涨跌中位数<b>'+pctTxt(crowd.median_pct)+'</b></div><div class="paper-risk-factor-item">高换手占比<b>'+fmt(crowd.high_turnover_ratio_pct,1)+'%</b></div><div class="paper-risk-factor-item">小单净额覆盖<b>'+fmt(crowd.small_net_coverage_pct,1)+'%</b></div><div class="paper-risk-factor-item">涨停代理占比<b>'+fmt(crowd.limit_up_proxy_pct,2)+'%</b></div><div class="paper-risk-factor-item">风险代理样本<b>'+fmt(crowd.market_sample_count,0)+' 只</b></div></div><div class="paper-risk-notice">'+zhRiskText(crowd.notice)+'</div></section>';
  html+='<section class="paper-risk-factor-grid">'+marketCard+flowCard+sentimentCard+crowdCard+'</section>';
  var queue=(d.position_queue||[]).map(function(p){var v=riskLevelView(p.level);return '<article class="paper-risk-position"><div><span class="risk-pill '+riskText(p.level)+'">'+v[0]+' '+v[1]+'</span><b>'+riskText(p.name)+' '+riskText(p.code)+'<br>'+riskText(p.account_name)+'</b></div><div>持仓金额 / 仓位<b>'+cny(p.market_value)+' / '+fmt(p.account_weight_pct,2)+'%</b></div><div>浮盈亏<b class="'+pctCls(p.ret_pct)+'">'+cny(p.unrealized_pnl,true)+' · '+pctTxt(p.ret_pct)+'</b></div><div>现价 / 风控线<b>'+fmt(p.price)+' / '+fmt(p.risk_price)+'</b></div><div>可卖 / T+1<b>'+riskText(p.available_qty)+'股 / '+zhRiskText(p.t1_status)+'</b></div><div class="paper-risk-position-reason">处置：<b>'+zhRiskText(p.action)+'</b>'+zhRiskText(p.reason)+'<br><span class="news-time">'+zhRiskText(p.quote_source)+' · '+riskText(p.quote_at)+'</span></div></article>';}).join('')||'<div class="paper-empty">当前无持仓，持仓风险为不适用。</div>';
  html+='<section class="panel"><h3>持仓风险处置队列</h3><div class="paper-risk-queue">'+queue+'</div></section>';
  var qualityRows=(d.data_quality||[]).map(function(q){
    var verification=q.verification||{}, verify=verification.status||'not_independently_verified';
    var note=verification.note||'\u672a\u63d0\u4f9b\u72ec\u7acb\u4ea4\u53c9\u6838\u9a8c';
    return '<tr><td><b>'+riskText(q.name)+'</b><br><small>'+zhRiskText(q.source)+'</small></td><td class="paper-risk-source '+riskText(q.status)+'">'+zhRiskText(q.status)+'</td><td>'+riskText(q.observed_at)+'</td><td>'+(q.age_seconds===null?'\u2014':fmt(q.age_seconds,0)+'\u79d2')+'</td><td>'+(q.coverage_pct===null?'\u2014':fmt(q.coverage_pct,1)+'%')+'</td><td title="'+zhRiskText(note)+'">'+zhRiskText(verify)+'</td><td>'+(q.status==='fresh'?'\u53ef\u6309\u65e2\u6709\u89c4\u5219\u4f7f\u7528':'\u4ec5\u5c55\u793a\u6216\u7981\u6b62\u589e\u52a0\u98ce\u9669')+'</td></tr>';
  }).join('');
  html+='<section class="panel"><h3>数据质量与降级状态</h3>'+tableScroll('<table><tr><th>数据源</th><th>新鲜度</th><th>源时间</th><th>延迟</th><th>覆盖率</th><th>真实性核验</th><th>交易影响</th></tr>'+qualityRows+'</table>',820)+'</section>';
  html+='<div class="disclaimer">'+riskText(d.disclaimer)+'</div>';
  $('paperRiskResult').innerHTML=html;
}
async function loadPaperRisk(forceRefresh){
  if(!forceRefresh&&window._paperRiskDashboard) renderPaperRisk(window._paperRiskDashboard);
  if(window._paperRiskRequest) return window._paperRiskRequest;
  var button=$('paperRiskRefresh');
  if(button){button.disabled=true;button.textContent=forceRefresh?'正在刷新…':'读取中…';}
  var request=forceRefresh?apiPost('/api/paper/risk-refresh'):api('/api/paper/risk-overview');
  window._paperRiskRequest=request.then(function(d){
    window._paperRiskDashboard=d;renderPaperRisk(d);
    if(d.refreshing||d.initializing){
      clearTimeout(window._paperRiskRetryTimer);
      window._paperRiskRetryTimer=setTimeout(function(){window._paperRiskRequest=null;loadPaperRisk(false);},1800);
    }
    return d;
  }).catch(function(e){
    if(!window._paperRiskDashboard) $('paperRiskResult').innerHTML='<div class="banner">风控中心加载失败：'+riskText(e.message||e)+' <button onclick="loadPaperRisk(true)">重试</button></div>';
    else $('paperRiskResult').insertAdjacentHTML('afterbegin','<div class="banner">刷新失败，已保留上次成功快照：'+riskText(e.message||e)+'</div>');
    throw e;
  }).finally(function(){
    window._paperRiskRequest=null;
    if(button){button.disabled=false;button.textContent='刷新风控状态';}
  });
  return window._paperRiskRequest;
}
async function refreshPaperRisk(){await loadPaperRisk(true);}
function selectPaperHistoryQuick(code){ if(code){ $('paperHistoryCode').value=code; loadPaperStockHistory(); } }
function showPaperStockHistory(code){ var tab=document.querySelector('#p-paper [data-paper-view="history"]'); showPaperWorkspace('history',tab); $('paperHistoryCode').value=code; loadPaperStockHistory(); }
async function loadPaperStockHistory(){
  var code=($('paperHistoryCode').value.match(/\d{6}/)||[])[0], account=$('paperHistoryAccount').value, target=$('paperHistoryResult');
  if(!code){ target.innerHTML='<div class="banner">\u8bf7\u8f93\u5165 6 \u4f4d\u80a1\u7968\u4ee3\u7801\uff0c\u6216\u4ece\u5386\u53f2\u4e2a\u80a1\u5feb\u9009\u4e2d\u9009\u62e9\u3002</div>'; return; }
  $('paperHistoryLoad').disabled=true; target.innerHTML='<div class="loading">\u6b63\u5728\u8bfb\u53d6\u8be5\u80a1\u7684\u5168\u90e8\u6a21\u62df\u8d26\u672c\u660e\u7ec6\u2026</div>';
  try{
    var d=await api('/api/paper/stock-history?code='+code+'&account_id='+encodeURIComponent(account)),s=d.summary||{};
    var stockToday=(d.positions||[]).reduce(function(total,p){return p.today_pnl===null||p.today_pnl===undefined?total:total+Number(p.today_pnl||0);},0);
    var stockTodayPct=((d.positions||[])[0]||{}).today_return_pct;
    var stockTodayText=(d.positions||[]).some(function(p){return p.today_pnl!==null&&p.today_pnl!==undefined;})?cny(stockToday,true)+'（'+pctTxt(stockTodayPct)+'）':(s.today_pnl_status||'盘前未开盘');
    var cards=[['\u4eca\u65e5\u76c8\u4e8f',stockTodayText],['\u5386\u53f2\u59d4\u6258',s.order_count||0],['\u5b9e\u9645\u6210\u4ea4',s.filled_orders||0],['\u4e70\u5165 / \u5356\u51fa',String(s.buy_qty||0)+' / '+String(s.sell_qty||0)+' \u80a1'],['\u7d2f\u8ba1\u4e70\u5165',cny(s.buy_amount)],['\u7d2f\u8ba1\u5356\u51fa',cny(s.sell_amount)],['\u5df2\u5b9e\u73b0\u635f\u76ca',cny(s.realized_pnl,true)],['\u7d2f\u8ba1\u8d39\u7528',cny(s.fees)]].map(function(x){return '<div class="paper-history-stat">'+x[0]+'<b>'+x[1]+'</b></div>';}).join('');
    var current=(d.positions||[]).map(function(p){return '<tr><td>'+p.account_name+'</td><td>'+p.qty+' \u80a1</td><td>'+fmt(p.cost)+'</td><td>'+fmt(p.price)+'</td><td>'+cny(p.market_value)+'</td><td class="'+pctCls(p.ret_pct)+'">'+cny(p.unrealized_pnl,true)+'<br><small>'+pctTxt(p.ret_pct)+'</small></td><td>'+p.hold_days+'\u65e5</td></tr>';}).join('');
    var orders=(d.orders||[]).map(function(o){var v=paperOrderStatusView(o.status),when=o.executed_at||o.created_at||'\u2014',price=o.filled_price||o.planned_price,source=o.origin==='manual'?'\u624b\u52a8\u6a21\u62df':'\u7b56\u7565\u81ea\u52a8';return '<tr class="paper-history-order-row" data-side="'+o.side+'" data-status="'+o.status+'" data-date="'+String(when).slice(0,10)+'"><td><b>'+when+'</b><br><small>\u8d26\u52a1\u521b\u5efa '+(o.created_at||'\u2014')+'</small></td><td><button class="paper-stock-link" data-code="'+o.code+'" onclick="showPaperStockHistory(this.dataset.code)">'+(o.name||o.code)+'</button><br><small>'+o.code+'</small></td><td>'+o.account_name+'</td><td class="'+(o.side==='buy'?'up':'down')+'">'+(o.side==='buy'?'\u4e70\u5165':'\u5356\u51fa')+'</td><td><span class="paper-order-status '+v[0]+'">'+v[1]+'</span></td><td>'+o.qty+' \u80a1</td><td>'+fmt(price)+'<br><small>\u59d4\u6258 '+fmt(o.planned_price)+'</small></td><td>'+cny(o.amount)+'</td><td>'+cny(o.fees)+'</td><td class="'+pctCls(o.realized_pnl)+'">'+cny(o.realized_pnl,true)+'</td><td>'+(o.fill_quote_at||'\u2014')+'</td><td>'+source+'<br><small>'+((o.order_type==='limit')?'\u9650\u4ef7':'\u5e02\u4ef7\u5feb\u7167')+'</small></td><td style="font-size:12px;max-width:260px">'+zhRiskText(o.reason||'\u2014')+'</td></tr>';}).join('');
    target.innerHTML='<div class="result-toolbar"><span class="tag tag-info">'+d.name+' '+d.code+'</span><span class="tag tag-info">\u5168\u90e8\u7b56\u7565\u8d26\u672c</span><span class="tag tag-info">\u6700\u65b0\u884c\u60c5 '+(d.quote_at||'\u2014')+'</span><span class="tag tag-info">\u5f53\u524d\u5e95\u4ed3 '+s.active_position_count+' \u4efd</span></div><section class="paper-history-summary">'+cards+'</section>'+(current?'<h3>\u5f53\u524d\u5e95\u4ed3</h3>'+tableScroll('<table><tr><th>\u7b56\u7565</th><th>\u6301\u4ed3</th><th>\u6210\u672c</th><th>\u73b0\u4ef7</th><th>\u5e02\u503c</th><th>\u6d6e\u52a8\u635f\u76ca</th><th>\u6301\u6709</th></tr>'+current+'</table>',760):'')+'<h3 style="margin-top:18px">\u5168\u90e8\u5386\u53f2\u6210\u4ea4\u4e0e\u59d4\u6258\u6d41\u6c34</h3>'+(orders?tableScroll('<table><tr><th>\u6210\u4ea4 / \u59d4\u6258\u65f6\u95f4</th><th>\u7b56\u7565</th><th>\u64cd\u4f5c</th><th>\u72b6\u6001</th><th>\u6570\u91cf</th><th>\u6210\u4ea4\u4ef7 / \u59d4\u6258\u4ef7</th><th>\u6210\u4ea4\u91d1\u989d</th><th>\u8d39\u7528</th><th>\u5df2\u5b9e\u73b0\u635f\u76ca</th><th>\u884c\u60c5\u6e90\u65f6\u95f4</th><th>\u6765\u6e90</th><th>\u8be6\u60c5 / \u539f\u56e0</th></tr>'+orders+'</table>',1480):'<div class="paper-empty">\u8be5\u80a1\u7968\u6682\u65e0\u6a21\u62df\u6210\u4ea4\u6216\u59d4\u6258\u8d26\u672c\u3002</div>')+'<div class="disclaimer">'+zhRiskText(d.note||'')+'</div>';
    var historyTable=target.querySelector('.paper-history-order-row')&&target.querySelector('.paper-history-order-row').closest('table');
    if(historyTable&&historyTable.rows[0]&&historyTable.rows[0].cells.length===12){ var stockHead=document.createElement('th'); stockHead.textContent='\u80a1\u7968\u540d\u79f0'; historyTable.rows[0].insertBefore(stockHead,historyTable.rows[0].cells[1]); }
    filterPaperHistoryRows();
  }catch(e){ target.innerHTML='<div class="banner">\u8bfb\u53d6\u4e2a\u80a1\u6863\u6848\u5931\u8d25\uff1a'+e.message+'</div>'; } finally{ $('paperHistoryLoad').disabled=false; }
}

function filterPaperHistoryRows(){
  var side=$('paperHistorySide')?$('paperHistorySide').value:'all', status=$('paperHistoryStatus')?$('paperHistoryStatus').value:'all', date=$('paperHistoryDate')?$('paperHistoryDate').value:'';
  var visible=0; document.querySelectorAll('.paper-history-order-row').forEach(function(row){ var match=(side==='all'||row.dataset.side===side)&&(status==='all'||row.dataset.status===status)&&(!date||row.dataset.date===date); row.hidden=!match; if(match) visible++; });
  if($('paperHistoryVisible')) $('paperHistoryVisible').textContent=visible+' \u7b14\u660e\u7ec6';
}

function syncCycleControls(cycle, accounts){
  var running=(cycle&&cycle.status)==='running'||(accounts||[]).some(function(a){return a.status==='running';});
  var paused=(cycle&&cycle.status)==='paused'||(!running&&(accounts||[]).some(function(a){return a.status==='paused';}));
  var controls=[
    ['paperCapital',running,'周期运行中，暂停后才能修改资金'],
    ['paperStart',running,'周期运行中，请先暂停'],
    ['paperResume',!paused,'仅暂停周期可恢复'],
    ['paperPause',!running,'仅运行周期可暂停'],
    ['paperReset',running,'周期运行中，请先暂停后重置'],
    ['paperRunNow',!running,'仅运行周期可立即观察']
  ];
  controls.forEach(function(item){
    var el=$(item[0]); if(!el) return;
    el.disabled=!!item[1]; el.title=item[1]?item[2]:'';
    el.setAttribute('aria-disabled',item[1]?'true':'false');
  });
}

function paperAccountDisplayName(account){
  var id=account&&account.id;
  return ({tq_breakout:'短线日内做T',main_force_top10:'超强主力股'})[id] || (account&&account.name) || id || '未知策略';
}
async function loadPaper(options){
  options=options||{};
  // Navigation, the one-minute refresh, and manual actions may all request an
  // overview at the same time.  Let every caller share one in-flight request
  // instead of rendering the large dashboard repeatedly in parallel.
  if(window._paperLoadRequest) return window._paperLoadRequest;
  window._paperLoadRequest=(async function(){
  try{
    // The activity audit is independent from the account overview.  Start it
    // immediately so it can read in parallel with the larger dashboard DOM
    // render instead of extending every browser refresh serially.
    var auditRequest = window._paperWorkspace==='activity'
      ? api('/api/paper/risk-audit?limit=160')
      : null;
    var overviewQuery=[];
    if(options.refresh) overviewQuery.push('refresh=1');
    if(window._paperWorkspace==='activity') overviewQuery.push('activity=1');
    if(window._paperWorkspace==='history') overviewQuery.push('history_symbols=1');
    var d = await api('/api/paper/overview'+(overviewQuery.length?'?'+overviewQuery.join('&'):''));
    var accounts = d.accounts||[];
    var cycle = d.cycle||{};
    var legacyEmpty = String(cycle.cycle_key||'').indexOf('legacy-')===0 && accounts.every(function(a){return !a.trade_count;});
    if(accounts.length && document.activeElement!==$('paperCapital') && !legacyEmpty) $('paperCapital').value = Math.round((d.shared&&d.shared.initial_cash) || accounts.reduce(function(sum,a){return sum+Number(a.initial_cash||0);},0) || 300000);
    syncPaperCapitalHint();
    // Strategy-style selectors were intentionally removed from the page.
    // Do not dereference their old IDs here: a null assignment used to abort
    // the entire paper dashboard render after a browser refresh.
    var running = accounts.filter(function(a){return a.status==='running';}).length;
    syncCycleControls(cycle,accounts);
    window._paperDashboard=d;
    var accountName={};
    accounts.forEach(function(a){accountName[a.id]=paperAccountDisplayName(a);});
    var accountFilterOptions='<option value="all">全部策略</option>'+accounts.map(function(a){return '<option value="'+a.id+'">'+paperAccountDisplayName(a)+'</option>';}).join('');
    if(window._paperPositionFilter===undefined) window._paperPositionFilter='all';
    if(window._paperPositionStateFilter===undefined) window._paperPositionStateFilter='all';
    if(window._paperOrderAccountFilter===undefined) window._paperOrderAccountFilter='all';
    if(window._paperOrderSideFilter===undefined) window._paperOrderSideFilter='all';
    if(window._paperOrderStatusFilter===undefined) window._paperOrderStatusFilter='all';
    if(window._paperOrderDateFilter===undefined){
      window._paperOrderDateFilter=((d.orders||[])[0]||{}).created_at
        ? String(d.orders[0].created_at).slice(0,10)
        : new Date(Date.now()-new Date().getTimezoneOffset()*60000).toISOString().slice(0,10);
    }
    var previousAccount=$('paperOrderAccount').value;
    $('paperOrderAccount').innerHTML=accounts.map(function(a){return '<option value="'+a.id+'">'+paperAccountDisplayName(a)+' · 共享可用 '+cny((d.shared||{}).cash)+'</option>';}).join('');
    if(previousAccount&&accountName[previousAccount]) $('paperOrderAccount').value=previousAccount;
    var shared=d.shared||{};
    var slotAlloc=shared.slot_allocation||{};
    var entryFreeze=shared.entry_freeze||{};
    var entryFreezeText=entryFreeze.enabled
      ? '新增买入：自动冻结 · '+adaptiveEsc(entryFreeze.reason||'数据门禁未通过')
      : '新增买入：自动开放 · 行情、覆盖和因子门禁通过';
    var borrowLast=shared.slot_borrow_last||null;
    var slotText='硬上限 '+(slotAlloc.hard_cap||18)+' · 当前可部署 '+(slotAlloc.deployable_cap||shared.position_limit||18)+' · 已用 '+(shared.dynamic_position_slots_used===undefined?shared.position_count:shared.dynamic_position_slots_used);
    var borrowText=borrowLast?('最近借位：'+(accountName[borrowLast.from]||borrowLast.from)+' → '+(accountName[borrowLast.account_id]||borrowLast.account_id)+' · 候选 '+fmt(borrowLast.candidate_score,1)+' 分'):'本轮暂无席位借用';
    var sharedCard='<article class="paper-account-card shared-pool-card"><div class="paper-account-title"><span>总资金池</span><span class="tag tag-ok">'+(shared.strategy_count||accounts.length)+' 策略共用</span></div><div class="paper-account-nav '+pctCls(shared.return_pct)+'">'+cny(shared.nav)+'</div><div style="margin-top:5px;font-size:13px;font-weight:700" class="'+pctCls(shared.today_return_pct)+'">今日 '+(shared.today_pnl===null||shared.today_pnl===undefined?'暂无完整收益':cny(shared.today_pnl,true)+'（'+pctTxt(shared.today_return_pct)+'）')+'</div><div style="margin-top:3px;font-size:12px" class="'+pctCls(shared.return_pct)+'">累计 '+pctTxt(shared.return_pct)+' · 盈亏 '+cny(shared.nav-shared.initial_cash,true)+'</div><div class="paper-account-meta"><span>总持仓市值<b>'+cny(shared.market_value)+'</b></span><span>资金利用率<b>'+fmt(shared.fund_utilization_pct,1)+'%</b></span><span>持仓/总上限<b>'+shared.position_count+' / '+(shared.position_limit||18)+'</b></span></div><div style="margin-top:6px;font-size:11px;color:var(--text-secondary)">'+slotText+'<br>'+borrowText+'</div><div style="margin-top:4px;font-size:11px;color:var(--text-secondary)">'+entryFreezeText+'</div><div style="margin-top:4px;font-size:11px;color:var(--text-secondary)">买入决策按策略分别运行；满仓后高分候选进入替补池，先卖弱仓再买强仓，不扩大总席位</div></article>';
    $('paperAccountStrip').innerHTML=sharedCard+accounts.map(function(a){
      var tone=a.id==='trend_pullback'?'swing':(a.id==='sector_rotation'?'rotation':'');
      var poolPositionPct=Number(a.strategy_position_pct_pool);
      if(!isFinite(poolPositionPct)) poolPositionPct=Number(a.position_value||0)/Math.max(Number(shared.nav)||1,1)*100;
      var budgetAmount=Number(a.strategy_budget_amount||0);
      var budgetUsagePct=budgetAmount>0?Number(a.position_value||0)/budgetAmount*100:null;
      // 每张策略卡只展示本策略实际持仓的损益；共享资金池归因仅保留在总览。
      // 今日收益率以该策略昨日持仓市值（含当日成交基准）为分母，浮盈率以本策略持仓成本为分母。
      var dayText=a.today_pnl===null||a.today_pnl===undefined
        ? '今日盈亏 '+(a.today_pnl_status||'暂无完整行情')
        : '今日盈亏 '+cny(a.today_pnl,true)+'（'+pctTxt(a.today_return_pct)+'）';
      var holdingText=a.holding_return_pct===null||a.holding_return_pct===undefined
        ? '持仓浮盈亏 '+cny(a.unrealized_pnl,true)
        : '持仓浮盈亏 '+cny(a.unrealized_pnl,true)+'（'+pctTxt(a.holding_return_pct)+'）';
      return '<article class="paper-account-card '+tone+'"><div class="paper-account-title"><span>'+paperAccountDisplayName(a)+' · 持仓市值</span>'+paperStatusTag(a.status)+'</div>'
        +'<div class="paper-account-nav '+pctCls(a.holding_return_pct)+'">'+cny(a.position_value)+'</div>'
        +'<div style="margin-top:5px;font-size:13px;font-weight:700" class="'+pctCls(a.today_return_pct)+'">'+dayText+'</div>'
        +'<div style="margin-top:3px;font-size:12px" class="'+pctCls(a.holding_return_pct)+'">'+holdingText+'</div>'
        +'<div style="margin-top:3px;font-size:12px;color:var(--text-secondary)">已实现 '+cny(a.realized_pnl,true)+' · 策略累计 '+cny(a.total_pnl,true)+'</div>'
        +'<div style="margin-top:6px;font-size:11px;color:var(--text-secondary)">'+a.entry_model_name+' - '+a.risk_profile_name+'</div>'
        +'<div class="paper-account-meta"><span>持仓成本<b>'+cny(a.position_cost_value)+'</b></span><span>动态预算使用<b>'+(budgetUsagePct===null?'—':fmt(budgetUsagePct,1)+'%')+'</b></span><span>持仓/动态上限<b>'+a.position_count+' / '+a.max_positions+'</b></span></div>'
        +'<div style="margin-top:6px;font-size:11px;color:var(--text-secondary)">累计盈亏 = 历史已实现盈亏 + 当前持仓浮盈亏；今日盈亏按昨收/当日买入成本核算</div></article>';
    }).join('');
    var qualityActionMap={
      consolidation_exit:'择强换股', capacity_exit:'压缩持仓', capacity_exit_pending_quote:'等待行情压缩', permission_scope_exit:'权限范围调仓', permission_scope_exit_pending_quote:'等待行情退出', risk_exit:'风险退出', watch:'继续观察', hold:'继续持有',
      queued:'排队复评', t1_locked:'T+1锁定', new_position:'新建观察', quote_pending:'等待行情核验',
      review_pending:'等待评分'
    };
    var qualityGradeMap={核心:'核心',观察:'观察',减仓:'减仓',淘汰:'淘汰',建仓复核:'建仓复核'};
    var quickPositions=(d.positions||[]).map(function(p){
      var dayText=p.today_pnl===null||p.today_pnl===undefined?'今日 '+(p.today_pnl_status||'暂无当日收益'):'今日 '+cny(p.today_pnl,true)+'（'+pctTxt(p.today_return_pct)+'）';
      var qualityScore=p.quality_score===null||p.quality_score===undefined?'—':fmt(p.quality_score,1);
      var qualityGrade=qualityGradeMap[p.quality_grade]||'待评分';
      var qualityAction=qualityActionMap[p.quality_action]||p.quality_action||'待复评';
      var replacement=p.quality_replacement_code?'后备候选 '+riskText(p.quality_replacement_code):'暂无替换候选';
      var qualityPhase=p.quality_review_phase||'持仓复核';
      var qualityDetail='入场 '+(p.quality_model_score===null||p.quality_model_score===undefined?'—':fmt(p.quality_model_score,1))
        +' · 资金 '+(p.quality_flow_score===null||p.quality_flow_score===undefined?'—':fmt(p.quality_flow_score,1))
        +' · 动量 '+(p.quality_momentum_score===null||p.quality_momentum_score===undefined?'—':fmt(p.quality_momentum_score,1));
      return '<div class="paper-position-row" data-account="'+p.account_id+'" data-pnl="'+(p.ret_pct>0?'profit':(p.ret_pct<0?'loss':'flat'))+'" data-sellable="'+(p.available_qty>=100?'sellable':'locked')+'"><div class="paper-position-symbol"><button class="paper-stock-link" onclick="showPaperStockHistory(\''+p.code+'\')">'+p.name+'</button><span>'+p.code+' - '+(accountName[p.account_id]||p.account_id)+'</span></div>'
        +'<div class="paper-position-cell">\u6301\u4ed3\u80a1\u6570 / \u53ef\u5356<b>'+p.qty+'\u80a1 / '+p.available_qty+'\u80a1</b></div><div class="paper-position-cell">\u6301\u4ed3\u5e02\u503c / \u4ed3\u4f4d<b>'+cny(p.market_value)+' / '+fmt(p.account_weight_pct,2)+'%</b></div><div class="paper-position-cell">\u644a\u8584\u6210\u672c / \u73b0\u4ef7<b>'+fmt(p.display_cost===undefined?p.cost:p.display_cost)+' / '+fmt(p.price)+'</b><small>风控成本 '+fmt(p.settlement_cost===undefined?p.cost:p.settlement_cost)+'</small></div>'
        +'<div class="paper-position-cell paper-pnl-cell"><span>持仓浮盈亏</span><b class="'+pctCls(p.ret_pct)+'">'+cny(p.unrealized_pnl,true)+'（'+pctTxt(p.ret_pct)+'）</b><span>今日变化</span><b class="'+pctCls(p.today_return_pct)+'">'+dayText+'</b><small title="'+(p.t1_reason||'')+'">'+(p.t1_status||p.price_state||'')+'</small></div>'
        +'<div class="paper-position-cell paper-quality-cell"><span>'+qualityPhase+' · 守仓评分</span><b>'+qualityScore+' · '+qualityGrade+'</b><small>'+qualityDetail+'</small><small>'+qualityAction+' · '+replacement+'</small></div>'
        +'<button class="paper-mini-btn sell" '+(p.available_qty<100?'disabled':'')+' onclick="preparePaperSell(\''+p.account_id+'\',\''+p.code+'\','+p.available_qty+')">'+(p.available_qty<100?'T+1\u9501\u5b9a':'\u6a21\u62df\u5356\u51fa')+'</button></div>';
    }).join('');
    var recentOrders=(d.orders||[]).map(function(o){
      var view=paperOrderStatusView(o.status,o.reason), cancel=(!o.archived_cycle&&o.status==='pending_limit')?'<button class="paper-mini-btn cancel" onclick="cancelPaperOrder('+o.id+')">撤单</button>':'';
      return '<div class="paper-order-row" data-account="'+o.account_id+'" data-date="'+String(o.created_at||'').slice(0,10)+'" data-side="'+o.side+'" data-status="'+o.status+'"><span>'+String(o.created_at||'').slice(5,16)+'</span><span><button class="paper-stock-link" onclick="showPaperStockHistory(\''+o.code+'\')">'+o.name+'</button><br><small>'+o.code+' · '+(o.account_name||accountName[o.account_id]||o.account_id)+'</small></span>'
        +'<span class="'+(o.side==='buy'?'up':'down')+'">'+(o.side==='buy'?'买入':'卖出')+' '+o.qty+'</span><span>'+fmt(o.filled_price||o.planned_price)+'</span>'
        +'<span class="paper-order-status '+view[0]+'">'+view[1]+'</span><span>'+cancel+'</span></div>';
    }).join('');
    var riskFeed=(d.risk_decisions||[]).slice(0,5).map(function(r){
      return '<div style="padding:8px 0;border-bottom:1px solid #edf1ef;font-size:12px"><b>'+(r.account_name||r.account_id)+' · '+(r.side==='buy'?'买入':'卖出')+' '+(r.code||'')+'</b><br><span style="color:var(--text-secondary)">'+zhRiskText(r.reason||r.decision)+'</span></div>';
    }).join('');
    var signalsByAccount={};
    (d.signals||[]).forEach(function(s){ (signalsByAccount[s.account_id]||(signalsByAccount[s.account_id]=[])).push(s); });
    var candidateCards=accounts.map(function(a){
      var list=(signalsByAccount[a.id]||[]).slice(0,5);
      var rows=list.map(function(s){
        var pick=(s.payload&&s.payload.pick)||{}, heat=pick.sector_heat||{};
    var stateMap={pending:'待开盘审批',filled:'已成交',blocked:'风控拦截',rejected:'已拒绝',deferred_capacity:'容量等待重排',superseded:'已失效',cancelled:'已撤销'};
        var state=stateMap[s.status]||s.status;
        var detail=(s.reason||'等待下一次检查').replace(/</g,'&lt;');
        var stateClass=s.status==='pending'?'pending':(s.status==='filled'?'filled':(s.status==='superseded'||s.status==='cancelled'?'cancelled':'rejected'));
        var audit=s.audit||{}, quotePct=audit.signal_quote_pct;
        var quoteMove=(typeof quotePct==='number'?(quotePct>=0?'+':'')+fmt(quotePct,2)+'%':'\u2014');
        var signalMarket=audit.signal_quote_at||'\u2014';
        var plannedReview=audit.planned_review_date||s.intended_date||'\u2014';
        var executionText=audit.execution_status==='filled'
          ? ('\u5b9e\u9645\u6210\u4ea4 '+(audit.executed_at||'\u2014')+' \u00b7 \u884c\u60c5 '+(audit.execution_quote_at||'\u2014'))
          : (s.status==='blocked'||s.status==='rejected'
              ? '\u672a\u6267\u884c\uff1a\u98ce\u63a7\u5728\u4fe1\u53f7\u65f6\u70b9\u5df2\u62e6\u622a'
              : '\u672a\u6210\u4ea4\uff1a\u7b49\u5f85\u8ba1\u5212\u5ba1\u6838');
        var timeTrace='\u4fe1\u53f7\u884c\u60c5 '+signalMarket+'\uff08'+quoteMove+'\uff09 \u00b7 \u8ba1\u5212\u5ba1\u6838 '+plannedReview+' \u00b7 '+executionText;
        return '<div class="paper-candidate-item"><div class="paper-candidate-symbol"><b>'+s.name+' '+s.code+'</b><span>'+(s.industry||'-')+(heat.rank?' · 板块第'+heat.rank:'')+'</span></div>'
          +'<div class="paper-candidate-decision"><span class="paper-order-status '+stateClass+'">'+state+'</span><br><span style="color:#738078">模型 '+fmt(s.t_score,2)+' / 排名 '+fmt(s.rank_score,2)+'</span></div>'
          +'<div class="paper-candidate-reason" title="'+detail+'">'+detail+'<br><span class="paper-signal-trace">'+timeTrace+'</span></div></div>';
      }).join('');
      return '<article class="paper-candidate-card"><div class="paper-candidate-head"><b>'+paperAccountDisplayName(a)+'</b><span>'+a.entry_model_name+' · '+list.length+' 个候选</span></div>'+(rows||'<div class="paper-empty">本时段尚未生成候选。</div>')+'</article>';
    }).join('');
    var overlapNote=(d.candidate_overlap||[]).map(function(x){ return x.left_name+' / '+x.right_name+' 重合 '+x.count+' 只（'+fmt(x.jaccard_pct,1)+'%）'; }).join('；') || '尚无可比较候选。';
    var latestMonitor=(d.monitor_runs||[])[0], monitorDetail=(latestMonitor&&latestMonitor.detail)||{};
    var monitorReason=monitorDetail.error||(monitorDetail.bootstrap&&monitorDetail.bootstrap.reason)||monitorDetail.reason;
    var monitorState=latestMonitor&&latestMonitor.status;
    var monitorLabel=monitorState==='completed'?'已完成':(monitorState==='running'?'检查中':'异常');
    var monitorText=latestMonitor
      ? '最近监控 '+String(latestMonitor.started_at||'').slice(5,16)+' · '+monitorLabel+' · '+(monitorReason||('检查 '+(monitorDetail.observed||0)+' 个底仓'))
      : '尚未收到3分钟监控心跳';
    $('paperTerminalBoard').innerHTML='<div class="paper-terminal-head"><h3>\u5f53\u524d\u6301\u4ed3</h3><span style="color:var(--text-secondary);font-size:12px">'+running+' / '+(accounts.length||4)+' \u8d26\u6237\u8fd0\u884c \u00b7 '+monitorText+'</span></div>'
      +'<div class="paper-terminal-section"><div class="paper-terminal-section-title"><span>\u53ef\u64cd\u4f5c\u5e95\u4ed3</span><div class="paper-filter-bar"><label>\u7b56\u7565</label><select id="paperPositionFilter" onchange="setPaperTerminalFilter(\'position\',this.value)">'+accountFilterOptions+'</select><label>\u72b6\u6001</label><select id="paperPositionStateFilter" onchange="setPaperTerminalFilter(\'positionState\',this.value)"><option value="all">\u5168\u90e8</option><option value="profit">\u6d6e\u76c8</option><option value="loss">\u6d6e\u4e8f</option><option value="sellable">\u53ef\u5356</option><option value="locked">T+1\u9501\u5b9a</option></select><span id="paperPositionVisible" class="paper-filter-count"></span></div></div><div class="paper-position-list">'+quickPositions+'<div id="paperPositionEmpty" class="paper-empty" hidden>\u8be5\u7b56\u7565\u5f53\u524d\u6ca1\u6709\u6301\u4ed3\u3002</div></div></div>';
    $('paperActivityBoard').innerHTML='<div class="paper-terminal-head"><h3>\u59d4\u6258\u64cd\u4f5c\u8bb0\u5f55</h3><span style="color:var(--text-secondary);font-size:12px">\u6309\u65e5\u671f\u4e0e\u7b56\u7565\u7b5b\u9009</span></div><div class="paper-terminal-section"><div class="paper-terminal-section-title"><span>\u6700\u8fd1\u59d4\u6258</span><div class="paper-filter-bar"><label>\u65e5\u671f</label><input id="paperOrderDateFilter" type="date" value="'+window._paperOrderDateFilter+'" onchange="setPaperTerminalFilter(\'orderDate\',this.value)"><button class="paper-filter-clear" onclick="clearPaperOrderDate()">\u5168\u90e8\u65e5\u671f</button><label>\u7b56\u7565</label><select id="paperOrderAccountFilter" onchange="setPaperTerminalFilter(\'orderAccount\',this.value)">'+accountFilterOptions+'</select><label>\u65b9\u5411</label><select id="paperOrderSideFilter" onchange="setPaperTerminalFilter(\'orderSide\',this.value)"><option value="all">\u5168\u90e8</option><option value="buy">\u4e70\u5165</option><option value="sell">\u5356\u51fa</option></select><label>\u7ed3\u679c</label><select id="paperOrderStatusFilter" onchange="setPaperTerminalFilter(\'orderStatus\',this.value)"><option value="all">\u5168\u90e8</option><option value="filled">\u5df2\u6210\u4ea4</option><option value="pending_limit">\u5f85\u89e6\u53d1</option><option value="risk_rejected">\u98ce\u63a7\u62d2\u7edd</option><option value="cancelled">\u5df2\u64a4\u9500</option><option value="expired">\u5df2\u8fc7\u671f</option></select><span id="paperOrderVisible" class="paper-filter-count"></span></div></div><div class="paper-order-scroll"><div class="paper-order-list">'+recentOrders+'<div id="paperOrderEmpty" class="paper-empty" hidden>\u6240\u9009\u65e5\u671f\u548c\u7b56\u7565\u6ca1\u6709\u59d4\u6258\u64cd\u4f5c\u3002</div></div></div></div>';
    if(window._paperWorkspace==='activity'){
      try{
        // The shared overview request may have started while another tab was
        // active, in which case auditRequest is null.  Fetch it now instead of
        // awaiting null and passing that value into renderPaperAudit().
        var auditDashboard=await (auditRequest||api('/api/paper/risk-audit?limit=160'));
        var auditBoard=$('paperActivityBoard');
        if(auditBoard){
          // loadPaper() may overlap after a fast refresh/navigation. Keep one audit section.
          auditBoard.querySelectorAll('.paper-risk-audit-section').forEach(function(node){node.remove();});
          auditBoard.insertAdjacentHTML('beforeend',renderPaperAudit(auditDashboard));
        }
      }catch(e){
        if($('paperActivityBoard')) $('paperActivityBoard').insertAdjacentHTML('beforeend','<div class="paper-terminal-section"><div class="banner">风控审计记录读取失败：'+riskText(e.message||e)+'</div></div>');
      }
    }
    $('paperHistoryAccount').innerHTML='<option value="">\u5168\u90e8\u7b56\u7565</option>'+accounts.map(function(a){return '<option value="'+a.id+'">'+a.name+'</option>';}).join('');
    $('paperHistoryQuick').innerHTML='<option value="">\u9009\u62e9\u4e00\u53ea\u6709\u6a21\u62df\u8d26\u672c\u8bb0\u5f55\u7684\u4e2a\u80a1</option>'+(d.history_symbols||[]).map(function(p){return '<option value="'+p.code+'">'+(p.name||p.code)+' '+p.code+' ? '+(p.order_count||0)+' \u7b14\u5386\u53f2</option>';}).join('');
    $('paperPositionFilter').value=window._paperPositionFilter;
    $('paperPositionStateFilter').value=window._paperPositionStateFilter;
    $('paperOrderAccountFilter').value=window._paperOrderAccountFilter;
    $('paperOrderSideFilter').value=window._paperOrderSideFilter;
    $('paperOrderStatusFilter').value=window._paperOrderStatusFilter;
    $('paperOrderDateFilter').value=window._paperOrderDateFilter;
    filterPaperTerminal();
    $('paperStatus').innerHTML = running
      ? '<span class="tag tag-ok">'+running+' / '+(accounts.length||4)+' 策略运行中</span> 周期 '+(cycle.cycle_key||'-')+'；每3分钟观察，满足全部条件才交易。'
      : (legacyEmpty
        ? '<span class="tag tag-info">待创建新周期</span> 旧 ¥20,000 空配置仍在归档前；输入框的 ¥100,000 会在点击“保存并启动新周期”后写入账本。'
        : '<span class="tag tag-info">当前周期已暂停</span> 资金已锁定；可恢复，或归档后新建周期。');
    // The aggregate "today P&L" banner repeated information already shown
    // in the strategy cards and squeezed the comparison chart vertically.
    // Keep the API summary available for other views, but do not render it
    // above the chart.
    // A tab switch or an activity/history refresh does not need a hidden
    // ECharts instance plus several wide audit tables.  Avoiding that render
    // keeps the visible workspace responsive while retaining the portfolio
    // view's complete comparison when it is actually selected.
    if(window._paperWorkspace==='portfolio'){
    var todayBand='';
    var compareRows = accounts.map(function(a){
      var markClass = a.id==='trend_pullback' ? 'swing' : (a.id==='sector_rotation' ? 'rotation' : '');
      var batch = cycle.started_at ? String(cycle.started_at).slice(0,10).replace(/-/g,'年').replace(/年(\d\d)$/,'月$1日')+' 起' : '等待资金确认';
      var todayHoldingPnl=a.today_pnl===null||a.today_pnl===undefined?'—':cny(a.today_pnl,true)+'<br/><small>'+pctTxt(a.today_return_pct)+'</small>';
      return '<tr><td><div class="paper-strategy-name"><span class="paper-strategy-mark '+markClass+'"></span>'+a.name+'</div></td>'
        +'<td>'+batch+'</td><td class="'+pctCls(a.holding_return_pct)+'">'+pctTxt(a.holding_return_pct)+'</td><td class="'+pctCls(a.today_return_pct)+'">'+todayHoldingPnl+'</td>'
        +'<td>'+pctTxt(a.max_drawdown_pct)+'</td><td>'+(a.win_rate_pct===null?'—':fmt(a.win_rate_pct,1)+'%')+'</td><td>'+(a.profit_loss_ratio===null?'—':fmt(a.profit_loss_ratio,2))+'</td>'
        +'<td>'+a.trade_count+'</td><td>'+fmt(a.strategy_position_pct_pool===undefined?a.fund_utilization_pct:a.strategy_position_pct_pool,1)+'%</td></tr>';
    }).join('');
    var positions = (d.positions||[]).map(function(p){
      var qualityScore=p.quality_score===null||p.quality_score===undefined?'—':fmt(p.quality_score,1);
      var qualityGrade=qualityGradeMap[p.quality_grade]||'待评分';
      var qualityAction=qualityActionMap[p.quality_action]||p.quality_action||'待复评';
      var replacement=p.quality_replacement_code?'后备 '+riskText(p.quality_replacement_code):'暂无后备候选';
      var qualityPhase=p.quality_review_phase||'持仓复核';
      return '<tr><td>'+(accountName[p.account_id]||p.account_id)+'</td><td><b>'+p.name+'</b><br/><span style="font-size:11px;color:var(--text-muted)">'+p.code+' · '+(p.industry||'-')+'</span></td>'
        +'<td>'+p.qty+' 股</td><td>'+cny(p.market_value)+'<br/><small>'+fmt(p.account_weight_pct,2)+'%</small></td><td>'+fmt(p.cost)+'</td><td>'+fmt(p.price)+'</td><td class="'+pctCls(p.ret_pct)+'">'+cny(p.unrealized_pnl,true)+'<br/><small>'+pctTxt(p.ret_pct)+'</small></td>'
        +'<td><b>'+qualityPhase+' · '+qualityScore+' · '+qualityGrade+'</b><br/><small>入场 '+(p.quality_model_score===null||p.quality_model_score===undefined?'—':fmt(p.quality_model_score,1))+' · 资金 '+(p.quality_flow_score===null||p.quality_flow_score===undefined?'—':fmt(p.quality_flow_score,1))+' · 动量 '+(p.quality_momentum_score===null||p.quality_momentum_score===undefined?'—':fmt(p.quality_momentum_score,1))+'</small><br/><small>'+qualityAction+' · '+replacement+'</small></td>'
        +'<td>'+p.hold_days+'日</td><td>'+p.available_qty+' 可卖 / '+p.locked_qty+' 锁定<br/><span style="font-size:11px;color:var(--text-muted)" title="'+(p.t1_reason||'')+'">'+(p.t1_status||'-')+'</span></td><td><span class="tag '+(p.asset_type==='etf_t0'?'tag-ok':'tag-info')+'">'+(p.asset_type==='etf_t0'?'ETF T+0':'股票 T+1')+'</span><br/><span style="font-size:11px;color:var(--text-muted)">风控价 '+fmt(p.risk_price)+' · '+(p.price_state||'-')+'</span></td><td>'+p.available_date+'<br/><span style="font-size:11px;color:var(--text-muted)">'+(p.quote_at||'')+'</span></td></tr>';
    }).join('');
    var signals = (d.signals||[]).map(function(s){
      var model=(s.payload&&s.payload.decision&&s.payload.decision.entry_model)||{},audit=s.audit||{};
      var quotePct=audit.signal_quote_pct;
      var marketText=(audit.signal_quote_at||'\u2014')+(typeof quotePct==='number'?' ? '+(quotePct>=0?'+':'')+fmt(quotePct,2)+'%':'');
      var actual=audit.execution_status==='filled'
        ? ((audit.executed_at||'\u2014')+'<br><small>\u884c\u60c5 '+(audit.execution_quote_at||'\u2014')+'</small>')
        : '\u672a\u6210\u4ea4<br><small>'+(s.status==='blocked'||s.status==='rejected'?'\u4fe1\u53f7\u65f6\u70b9\u98ce\u63a7\u62e6\u622a':'\u5c1a\u672a\u6267\u884c')+'</small>';
      return '<tr><td>'+(accountName[s.account_id]||s.account_id)+'</td><td><b>'+s.name+'</b><br/><span style="font-size:11px;color:var(--text-muted)">'+s.code+'</span></td><td>'+(audit.factor_date||s.signal_date||'\u2014')+'</td><td>'+marketText+'</td><td>'+(audit.planned_review_date||s.intended_date||'\u2014')+'</td><td>'+actual+'</td><td>'+(model.name||'\u72ec\u7acb\u5165\u573a\u6a21\u578b')+'<br><small>'+fmt(s.t_score,2)+'</small></td><td>'+paperStatusTag(s.status)+'</td><td style="font-size:12px">'+(s.reason||'\u5f85\u5b9e\u65f6\u884c\u60c5\u4e0e\u8d26\u6237\u98ce\u63a7\u590d\u6838')+'</td></tr>';
    }).join('');
    var orders = (d.orders||[]).map(function(o){
      var view=paperOrderStatusView(o.status), cancel=(!o.archived_cycle&&o.status==='pending_limit')?'<button class="paper-mini-btn cancel" onclick="cancelPaperOrder('+o.id+')">撤单</button>':'';
      return '<tr><td>'+o.created_at+'</td><td>'+(o.account_name||accountName[o.account_id]||o.account_id)+'</td><td>'+(o.origin==='manual'?'手动模拟':'策略自动')+'<br><small>'+(o.order_type==='limit'?'限价':'市价')+'</small></td>'
        +'<td class="'+(o.side==='buy'?'up':'down')+'">'+(o.side==='buy'?'买入':'卖出')+'</td><td><b>'+o.name+'</b> '+o.code+'</td>'
        +'<td>'+o.qty+'</td><td>'+fmt(o.filled_price||o.planned_price)+'</td><td>'+cny(o.realized_pnl,true)+'</td><td><span class="paper-order-status '+view[0]+'">'+view[1]+'</span></td><td style="font-size:12px">'+(o.reason||'-')+cancel+'</td></tr>';
    }).join('');
    var fills = (d.fills||[]).map(function(f){
      return '<tr><td>'+f.fill_date+'</td><td>'+(f.account_name||accountName[f.account_id]||f.account_id)+'</td><td class="'+(f.side==='buy'?'up':'down')+'">'+(f.side==='buy'?'买入':'卖出')+'</td><td>'+f.code+'</td><td>'+f.qty+'</td><td>'+fmt(f.price)+'</td><td>'+cny(f.amount)+'</td><td>'+cny(f.fees)+'</td><td style="font-size:12px">'+f.assumption+'</td></tr>';
    }).join('');
    var reviews = (d.reviews||[]).map(function(r){ return '<details style="margin:6px 0"><summary><b>'+r.account_id+'</b> · '+r.week_key+' · '+r.recommendation+'</summary><pre style="white-space:pre-wrap;font:12px Microsoft YaHei;color:var(--text-secondary);padding:8px">'+r.report+'</pre></details>'; }).join('');
    var observationNames={scan:'候选扫描',observe:'观察',t_sell:'日内高抛',t_rebuy:'日内回补'};
    var observations = (d.observations||[]).map(function(o){ return '<tr><td>'+o.observed_at+'</td><td>'+(accountName[o.account_id]||o.account_id)+'</td><td>'+(o.code||'候选池')+'</td><td>'+(o.price===null?'—':fmt(o.price))+'</td><td><span class="tag '+(['observe','scan'].indexOf(o.action)>=0?'tag-info':'tag-ok')+'">'+(observationNames[o.action]||o.action)+'</span></td><td>'+o.reason+'</td></tr>'; }).join('');
    var params = (d.parameter_versions||[]).map(function(v){ return '<tr><td>'+v.created_at+'</td><td>'+v.account_id+'</td><td>'+v.version+'</td><td>'+v.style+'</td><td>'+v.effective_date+'</td><td>'+v.reason+'</td></tr>'; }).join('');
    var archives = (d.archives||[]).map(function(a){ return '<li>'+a.created_at+' · '+a.cycle_key+' · '+a.reason+'</li>'; }).join('');
    var exposure = Object.keys(d.industry_exposure||{}).map(function(k){return '<span class="tag tag-info">'+k+' '+cny(d.industry_exposure[k])+'</span>';}).join(' ') || '暂无行业暴露';
    var schedule = d.schedule||{};
    var curve=d.equity_curve||{},curvePoints=(curve.dates||[]).length;
    var strategyCount=accounts.length||0;
    var challengeMsg = curvePoints>=2
      ? '曲线按各策略绩效参考本金归一化；策略启用前保持空值，并与沪深300收盘快照对比。'
      : (running?'本周期已启动；净值点不足两个，后续有效快照会自动补齐曲线。':'挑战将在确认资金并启动新周期后开始。');
    var signalsAudit = signals
      ? tableScroll('<table><tr><th>策略</th><th>标的</th><th>信号日</th><th>执行日</th><th>独立模型评分</th><th>状态</th><th>说明</th></tr>'+signals+'</table>',980)
      : '<div class="paper-empty">暂无信号。两套策略会按各自模型、行情时间戳和仓位上限分别审批。</div>';
    var positionsAudit = positions
      ? tableScroll('<table><tr><th>策略决策</th><th>标的</th><th>持仓股数</th><th>持仓市值 / 总池占比</th><th>成本</th><th>现价</th><th>浮盈亏</th><th>质量评分 / 处置</th><th>持有</th><th>份额状态</th><th>交易制度</th><th>最早可卖 / 报价</th></tr>'+positions+'</table>',1260)
      : '<div class="paper-empty">暂无模拟持仓。</div>';
    var ordersAudit = orders
      ? tableScroll('<table><tr><th>时间</th><th>策略</th><th>来源</th><th>方向</th><th>标的</th><th>数量</th><th>成交/委托价</th><th>已实现盈亏</th><th>状态</th><th>模型结论</th></tr>'+orders+'</table>',1080)
      : '<div class="paper-empty">暂无订单；每笔成交、挂单、拒单与撤单都会在此留痕。</div>';
    var fillsAudit = fills
      ? tableScroll('<table><tr><th>成交日</th><th>策略</th><th>方向</th><th>代码</th><th>数量</th><th>成交价</th><th>成交额</th><th>费用</th><th>成交假设</th></tr>'+fills+'</table>',900)
      : '<div class="paper-empty">暂无成交记录。</div>';
    var observationsAudit = observations
      ? tableScroll('<table><tr><th>时间</th><th>策略</th><th>标的</th><th>报价</th><th>结论</th><th>原因</th></tr>'+observations+'</table>',820)
      : '<div class="paper-empty">暂无日内观察；仅在交易时段内每3分钟检查。</div>';
    var paramsAudit = params
      ? tableScroll('<table><tr><th>记录时间</th><th>策略</th><th>版本</th><th>风格</th><th>生效日</th><th>原因</th></tr>'+params+'</table>',820)
      : '<div class="paper-empty">尚无参数版本记录。</div>';
    $('paperResult').innerHTML = todayBand+'<section class="paper-challenge"><div class="paper-challenge-head"><h2>'+strategyCount+'策略归一化收益对比</h2><p>折线为策略累计收益（按绩效参考本金），持仓浮盈率按实际持仓成本计算；两种口径不混用。委托和个股历史已移至上方专页。</p></div><div id="paperCompareChart" class="paper-compare-chart" role="img" aria-label="'+strategyCount+'套策略与沪深300的归一化收益对比曲线"></div><div class="paper-challenge-table"><table><tr><th>策略名称</th><th>挑战批次</th><th>当前持仓浮盈率</th><th>今日持仓盈亏</th><th>最大回撤</th><th>胜率</th><th>盈亏比</th><th>成交笔数</th><th>占总资金池</th></tr>'+compareRows+'</table></div><div class="paper-challenge-note">'+challengeMsg+'</div></section><div class="panel"><h3>当前行业风险暴露</h3><div class="result-toolbar">'+exposure+'</div></div><div class="disclaimer">'+d.disclaimer+'</div>';
    renderPaperCompareChart(curve);
    }
  }catch(e){
    var message=riskText((e&&e.message)||e||'未知错误');
    // A dashboard failure must never leave the visible workspace permanently
    // saying "正在读取".  Surface the exact failure in every affected panel so
    // the user can refresh or report it, while risk exits continue server-side.
    if($('paperResult')) $('paperResult').innerHTML='<div class="banner">模拟盘加载失败：'+message+'</div>';
    if($('paperTerminalBoard')) $('paperTerminalBoard').innerHTML='<div class="paper-empty">持仓与委托状态读取失败：'+message+'。请刷新页面重试。</div>';
    if($('paperActivityBoard')) $('paperActivityBoard').innerHTML='<div class="paper-empty">委托与风控审计读取失败：'+message+'。请刷新页面重试。</div>';
    if($('paperStatus')) $('paperStatus').innerHTML='<span class="tag tag-warn">读取异常</span> '+message;
  } finally {
    window._paperLoadRequest=null;
  }
  })();
  return window._paperLoadRequest;
}

async function runBacktest(){
  $('btnBt').disabled = true;
  $('btResult').innerHTML = '<div class="loading">回测运行中（策略信号、成交约束与风险指标计算）…</div>';
  try{
    var d = await api('/api/backtest?strategy='+$('btStrategy').value+'&topn='+$('btTopn').value+'&rebalance='+$('btReb').value+'&gate='+$('btGate').checked);
    if(d.need_init){ $('btResult').innerHTML = '<div class="banner">'+d.message+'</div>'; return; }
    if(d.error){ $('btResult').innerHTML = '<div class="banner">'+d.error+'</div>'; return; }
    var m = d.metrics;
    var mhtml = '<div class="metrics">'
      +'<div class="metric"><div class="v '+pctCls(m.total_return)+'">'+pctTxt(m.total_return)+'</div><div class="k">区间总收益</div></div>'
      +'<div class="metric"><div class="v '+pctCls(m.annual_return)+'">'+pctTxt(m.annual_return)+'</div><div class="k">年化收益</div></div>'
      +'<div class="metric"><div class="v down">'+pctTxt(m.max_drawdown)+'</div><div class="k">最大回撤</div></div>'
      +'<div class="metric"><div class="v">'+fmt(m.sharpe)+'</div><div class="k">夏普比率</div></div>'
      +'<div class="metric"><div class="v">'+fmt(m.sortino)+'</div><div class="k">Sortino</div></div>'
      +'<div class="metric"><div class="v">'+fmt(m.calmar)+'</div><div class="k">Calmar</div></div>'
      +'<div class="metric"><div class="v">'+fmt(m.annual_volatility,1)+'%</div><div class="k">年化波动率</div></div>'
      +'<div class="metric"><div class="v">'+fmt(m.daily_win_rate,1)+'%</div><div class="k">日胜率</div></div>'
      +'<div class="metric"><div class="v '+pctCls(m.excess_return)+'">'+pctTxt(m.excess_return)+'</div><div class="k">超额收益(vs沪深300)</div></div>'
      +'<div class="metric"><div class="v">'+d.trades+'</div><div class="k">交易次数</div></div></div>';
    // 门控历史条
    var gateHtml = '';
    if(d.gate_log && d.gate_log.length){
      var gateColors = {green:'#27ae60', yellow:'#f39c12', red:'#e74c3c'};
      gateHtml = '<div style="display:flex;gap:2px;margin-bottom:12px;align-items:center"><span style="font-size:12px;color:#7f8c9b;margin-right:8px">风险门控：</span>'
        + d.gate_log.slice(-120).map(function(g){
          var c = gateColors[g.light]||'#ccc';
          return '<span title="'+g.date+': '+g.light+'" style="display:inline-block;width:5px;height:14px;background:'+c+';border-radius:1px"></span>';
        }).join('') + '</div>';
    }
    var holdings = (d.recent_holdings||[]).map(function(h){
      return '<div style="font-size:12px;color:#5a6b7d;margin-top:4px"><b>'+h.signal_date+'</b> 产生信号，'+h.execution_date+' 执行 → '+(h.target.length?h.target.join('、'):'空仓')+'</div>';
    }).join('');
    var ex = d.execution || {};
    var rej = ex.rejected_orders || {};
    var quality = '<div class="banner" style="margin-top:12px">执行统计：换手 '+fmt(ex.turnover_multiple,2)+' 倍，估算成本 '+fmt(ex.estimated_cost_pct_of_initial,2)+'%；拒单 '
      +(rej.limit_up||0)+' 笔涨停买入 / '+(rej.limit_down||0)+' 笔跌停卖出 / '+(rej.suspended||0)+' 笔停牌缺价。'
      +'基本面模式：'+(d.params.fundamentals_mode==='disabled_no_pit_data'?'已关闭历史回填（防前视）':'最新快照近似')+'；当前上市基础库缺少完整退市历史成分，仍有幸存者偏差。</div>';
    $('btResult').innerHTML = gateHtml + mhtml + '<div id="btChart" class="chart"></div>'
      +'<div style="margin-top:10px"><b style="font-size:13px">最近调仓记录</b>'+holdings+'</div>'
      +quality
      +'<div class="disclaimer">'+d.disclaimer+'</div>';
    var series = [{name:'策略净值', type:'line', data:d.equity, showSymbol:false, lineStyle:{width:2, color:'#d4380d'}}];
    if(d.benchmark) series.push({name:'沪深300', type:'line', data:d.benchmark, showSymbol:false, lineStyle:{width:1.5, color:'#7f8c9b'}});
    chart('btChart').setOption({
      tooltip:{trigger:'axis'},
      legend:{data:series.map(function(s){return s.name;})},
      xAxis:{type:'category', data:d.dates},
      yAxis:{type:'value', scale:true},
      dataZoom:[{type:'inside'},{type:'slider'}],
      grid:{left:55, right:20, top:35, bottom:60},
      series:series
    }, true);
  }catch(e){
    $('btResult').innerHTML = '<div class="banner">回测失败：'+e+'</div>';
  }finally{ $('btnBt').disabled = false; }
}

// ---------- 个股分析 ----------
var _searchTimer;
async function searchStock(){
  clearTimeout(_searchTimer);
  var q = $('stockCode').value.trim();
  if(q.length<1){ $('stockSuggest').style.display='none'; return; }
  _searchTimer = setTimeout(async function(){
    try{
      var d = await api('/api/stock_search?q='+encodeURIComponent(q));
      if(!d.results.length){ $('stockSuggest').style.display='none'; return; }
      $('stockSuggest').innerHTML = d.results.map(function(r){
        return '<div style="padding:6px 12px;cursor:pointer;font-size:13px;border-bottom:1px solid #f2f4f8" onmouseover="this.style.background=\'#f0f5ff\'" onmouseout="this.style.background=\'\'" onclick="selectStock(decodeURIComponent(\''+adaptiveJsArg(r.code)+'\'),decodeURIComponent(\''+adaptiveJsArg(r.name)+'\'))">'+adaptiveEsc(r.name)+' <span style="color:#9aa5b1">'+adaptiveEsc(r.code)+'</span> <span style="color:#7f8c9b;font-size:11px">'+adaptiveEsc(r.industry||'')+'</span></div>';
      }).join('');
      $('stockSuggest').style.display='block';
    }catch(e){}
  }, 200);
}
function selectStock(code, name){
  $('stockCode').value = name + ' ' + code;
  $('stockSuggest').style.display = 'none';
  window._stockCode = code;
  analyzeStock();
}
async function analyzeStockLegacy(){
  var code = window._stockCode || $('stockCode').value.trim();
  // 如果输入的是 "名称 代码" 格式，提取代码
  var m = code.match(/(\d{6})/);
  if(m) code = m[1];
  if(!code){ alert('请输入股票代码或名称'); return; }
  $('stockBasic').innerHTML = '<div class="loading">加载中…</div>';
  $('stockFinance').innerHTML = '';
  $('stockNews').innerHTML = '';
  $('stockDecision').innerHTML = '';
  try{
    var d = await api('/api/stock_detail?code='+encodeURIComponent(code));
    if(d.error){ $('stockBasic').innerHTML = '<div class="banner">'+d.error+'</div>'; return; }
    if(d.need_init){ $('stockBasic').innerHTML = '<div class="banner">'+d.message+'</div>'; return; }
    // 基本信息卡
    $('stockBasic').innerHTML =
      '<div class="metrics">'
      +'<div class="metric"><div class="k">现价</div><div class="v">'+fmt(d.price)+'</div></div>'
      +'<div class="metric"><div class="k">今日涨跌</div><div class="v '+pctCls(d.pct)+'">'+pctTxt(d.pct)+'</div></div>'
      +'<div class="metric"><div class="k">PE</div><div class="v">'+fmt(d.pe,1)+'</div></div>'
      +'<div class="metric"><div class="k">PB</div><div class="v">'+fmt(d.pb,2)+'</div></div>'
      +'<div class="metric"><div class="k">ROE</div><div class="v">'+fmt(d.roe,1)+'%</div></div>'
      +'<div class="metric"><div class="k">行业</div><div class="v" style="font-size:14px">'+(d.industry||'-')+'</div></div>'
      +'<div class="metric"><div class="k">市值</div><div class="v">'+yi(d.mktcap)+'</div></div>'
      +'<div class="metric"><div class="k">主力占比</div><div class="v '+pctCls(d.main_pct)+'">'+(d.main_pct!==null?pctTxt(d.main_pct):'-')+'</div></div>'
      +'</div>';
    // K线图 — 4面板：蜡烛图 + 成交量 + MACD + RSI
    var kc = chart('stockKline');
    var ohlc = d.kline.dates.map(function(_,i){
      return [d.kline.open[i], d.kline.close[i], d.kline.low[i], d.kline.high[i]];
    });
    var upColor='#e74c3c', downColor='#27ae60';
    kc.setOption({
      tooltip:{trigger:'axis', axisPointer:{type:'cross'}},
      legend:{data:['MA5','MA20','BOLL上轨','BOLL下轨','MACD(DIF)','MACD(DEA)','MACD柱','RSI(14)'], top:5},
      grid:[
        {left:60, right:20, top:40, height:'42%'},
        {left:60, right:20, top:'62%', height:'10%'},
        {left:60, right:20, top:'74%', height:'12%'},
        {left:60, right:20, top:'87%', height:'12%'}
      ],
      xAxis:[
        {type:'category', data:d.kline.dates, gridIndex:0, axisLabel:{show:false}},
        {type:'category', data:d.kline.dates, gridIndex:1, axisLabel:{show:false}},
        {type:'category', data:d.kline.dates, gridIndex:2, axisLabel:{show:false}},
        {type:'category', data:d.kline.dates, gridIndex:3, axisLabel:{rotate:30,fontSize:10}}
      ],
      yAxis:[
        {type:'value', scale:true, gridIndex:0, splitLine:{lineStyle:{color:'#f0f0f5'}}},
        {type:'value', gridIndex:1, axisLabel:{show:false}},
        {type:'value', gridIndex:2, splitLine:{lineStyle:{color:'#f0f0f5'}}},
        {type:'value', gridIndex:3, min:0, max:100, splitLine:{lineStyle:{color:'#f0f0f5'}}}
      ],
      dataZoom:[{type:'inside', xAxisIndex:[0,1,2,3]},{type:'slider', xAxisIndex:[0,1,2,3], bottom:0}],
      series:[
        {name:'K线', type:'candlestick', data:ohlc, xAxisIndex:0, yAxisIndex:0,
         itemStyle:{color:upColor, color0:downColor, borderColor:upColor, borderColor0:downColor}},
        {name:'MA5', type:'line', data:d.kline.ma5, xAxisIndex:0, yAxisIndex:0, symbol:'none', lineStyle:{color:'#f39c12',width:1}},
        {name:'MA20', type:'line', data:d.kline.ma20, xAxisIndex:0, yAxisIndex:0, symbol:'none', lineStyle:{color:'#3498db',width:1}},
        {name:'BOLL上轨', type:'line', data:d.kline.bb_upper, xAxisIndex:0, yAxisIndex:0, symbol:'none', lineStyle:{color:'rgba(142,68,173,.6)',width:1,type:'dashed'}},
        {name:'BOLL下轨', type:'line', data:d.kline.bb_lower, xAxisIndex:0, yAxisIndex:0, symbol:'none', lineStyle:{color:'rgba(142,68,173,.6)',width:1,type:'dashed'}},
        {name:'成交量', type:'bar', data:d.kline.volume.map(function(v,i){
          return {value:v, itemStyle:{color:d.kline.pcts[i]>=0?upColor:downColor}};
        }), xAxisIndex:1, yAxisIndex:1},
        {name:'MACD(DIF)', type:'line', data:d.kline.macd_dif, xAxisIndex:2, yAxisIndex:2, symbol:'none', lineStyle:{color:'#e74c3c',width:1}},
        {name:'MACD(DEA)', type:'line', data:d.kline.macd_dea, xAxisIndex:2, yAxisIndex:2, symbol:'none', lineStyle:{color:'#3498db',width:1}},
        {name:'MACD柱', type:'bar', data:d.kline.macd_bar, xAxisIndex:2, yAxisIndex:2},
        {name:'RSI(14)', type:'line', data:d.kline.rsi, xAxisIndex:3, yAxisIndex:3, symbol:'none', lineStyle:{color:'#8b5cf6',width:1.5},
         markLine:{silent:true, data:[{yAxis:70,lineStyle:{color:'#e74c3c'}},{yAxis:30,lineStyle:{color:'#27ae60'}}]}}
      ]
    }, true);
    // 财务
    $('stockFinance').innerHTML = '<table><tr><th>净利润同比</th><th>营收同比</th><th>报告期</th><th>流通市值</th><th>换手率</th></tr>'
      +'<tr><td>'+pctTxt(d.profit_yoy)+'</td><td>'+pctTxt(d.rev_yoy)+'</td><td>'+(d.report_date||'-')+'</td><td>'+yi(d.float_cap)+'</td><td>'+fmt(d.turnover,2)+'%</td></tr></table>';
    // 买卖决策
    var bd=d.buy_decision, sd=d.sell_decision;
    $('stockDecision').innerHTML =
      '<div style="margin-bottom:8px"><b>买入决策：</b><span class="tag '+(bd.executable?'tag-ok':(bd.watchlist?'tag-info':'tag-warn'))+'">'+bd.tier+' '+bd.action+'</span> '+bd.summary+'</div>'
      +'<div style="margin-bottom:8px"><b>六维核查：</b>'+bd.six_dim.map(function(dim){ return '<span style="margin-right:8px;font-size:12px">'+dim.name+' <span style="color:'+(dim.status==='green'?'#27ae60':(dim.status==='yellow'?'#f39c12':'#e74c3c'))+'">'+dim.status+'</span></span>'; }).join('')+'</div>'
      +'<div style="margin-bottom:6px"><b>卖出决策：</b>'+sd.summary+'</div>'
      +'<div style="font-size:12px;color:#9aa5b1">止盈策略：'+sd.take_profit+' | 强制止损：'+sd.forced_stop+'</div>';
    // 新闻
    if(d.news && d.news.length){
      $('stockNews').innerHTML = d.news.map(function(n){
        var tag = n.tone>0?'<span class="tag tag-ok">利好</span>':(n.tone<0?'<span class="tag tag-warn">风险</span>':'<span class="tag tag-info">中性</span>');
        return '<div class="news-item">'+tag+(n.keywords&&n.keywords.length?' 关键词：'+n.keywords.join('、'):'')+'<br/>'+n.summary+'<br/><span class="news-time">'+(n.time||'')+' · '+n.source+'</span></div>';
      }).join('');
    }else{
      $('stockNews').innerHTML = '<div style="color:var(--text-muted);padding:10px 0">近期快讯中暂无该股消息</div>';
    }
    // 同行业对比
    try{
      var peers = await api('/api/industry_peers?code='+encodeURIComponent(code)+'&topn=8');
      if(peers.peers && peers.peers.length){
        $('stockPeers').innerHTML = '<div style="font-size:12px;color:var(--text-muted);margin-bottom:6px">行业：'+peers.industry+'，共'+peers.count+'只同行业个股</div><table><tr><th>名称</th><th>代码</th><th>现价</th><th>涨跌</th><th>PE</th><th>PB</th><th>市值</th></tr>'
        + peers.peers.map(function(p){
          return '<tr onmouseover="this.style.background=\'var(--surface-hover)\'" onmouseout="this.style.background=\'\'" style="cursor:pointer" onclick="selectStock(decodeURIComponent(\''+adaptiveJsArg(p.code)+'\'),decodeURIComponent(\''+adaptiveJsArg(p.name)+'\'))"><td><b>'+adaptiveEsc(p.name)+'</b></td><td>'+adaptiveEsc(p.code)+'</td><td>'+fmt(p.price)+'</td><td class="'+pctCls(p.pct)+'">'+pctTxt(p.pct)+'</td><td>'+fmt(p.pe,1)+'</td><td>'+fmt(p.pb,2)+'</td><td>'+yi(p.mktcap)+'</td></tr>';
        }).join('')+'</table>';
      } else {
        $('stockPeers').innerHTML = '<div style="color:var(--text-muted)">无同行业对比数据</div>';
      }
    }catch(e){ $('stockPeers').innerHTML = ''; }
  }catch(e){ $('stockBasic').innerHTML = '<div class="banner">分析失败：'+e+'</div>'; }
}

// ---------- 个股研究页（本地行情快照，不宣称实时） ----------
function stockMA(values, period){
  return values.map(function(_,i){ if(i<period-1) return null; var s=0; for(var j=i-period+1;j<=i;j++) s+=Number(values[j]||0); return +(s/period).toFixed(3); });
}
function stockLast(values){ for(var i=values.length-1;i>=0;i--) if(values[i]!==null && values[i]!==undefined) return Number(values[i]); return null; }
function stockCross(a,b){
  var n=a.length-1, now=Number(a[n])-Number(b[n]), prev=Number(a[n-1])-Number(b[n-1]);
  if(now>=0 && prev<0) return '金叉'; if(now<=0 && prev>0) return '死叉'; return '无新交叉';
}
function stockTrend(close, ma5, ma20){
  var p=stockLast(close), m5=stockLast(ma5), m20=stockLast(ma20);
  if(p>=m20 && m5>=m20) return '震荡偏强';
  if(p<m20 && m5<m20) return '震荡偏弱';
  return '震荡整理';
}
function switchStockSection(button){
  document.querySelectorAll('.stock-research-tab').forEach(function(x){ x.classList.remove('active'); });
  button.classList.add('active');
  var target = {kline:'stockKline', decision:'stockDecisionPanel', news:'stockNewsPanel', finance:'stockFinancePanel', peers:'stockPeersPanel'}[button.dataset.stockSection];
  if(target){ $(target).scrollIntoView({behavior:'smooth',block:'start'}); }
}
function refreshStockResearch(){ analyzeStock(true); }
function toggleStockWatch(){
  window._stockWatching = !window._stockWatching;
  $('stockWatchBtn').textContent = window._stockWatching ? '移出自选' : '加入自选';
}
function stockZoom(mode){
  if(!window._stockLastData) return;
  var current=window._stockZoom || {start:45,end:100}, width=current.end-current.start;
  if(mode==='in'){ width=Math.max(12,width-14); current.start=Math.max(0,current.end-width); }
  if(mode==='out'){ width=Math.min(100,width+14); current.start=Math.max(0,current.end-width); }
  if(mode==='earlier'){ current.start=Math.max(0,current.start-20); current.end=Math.max(Math.min(100,current.start+width),width); }
  if(mode==='latest'){ current={start:45,end:100}; }
  if(mode==='all'){ current={start:0,end:100}; }
  window._stockZoom=current;
  chart('stockKline').dispatchAction({type:'dataZoom',dataZoomIndex:0,start:current.start,end:current.end});
}
async function analyzeStock(refresh){
  var code = window._stockCode || $('stockCode').value.trim(), match = code.match(/(\d{6})/);
  if(match) code=match[1];
  if(!code){ alert('请输入股票代码或名称'); return; }
  $('stockBasic').innerHTML='<div class="loading">正在整理行情、技术信号与研究资料…</div>';
  $('stockFinance').innerHTML=''; $('stockNews').innerHTML=''; $('stockDecision').innerHTML=''; $('stockPeers').innerHTML='';
  try{
    var d=await api('/api/stock_detail?code='+encodeURIComponent(code)+(refresh?'&refresh=true':''));
    if(d.error || d.need_init){ $('stockBasic').innerHTML='<div class="banner">'+(d.error||d.message)+'</div>'; return; }
    window._stockLastData=d; window._stockLoaded=true; window._stockCode=d.code; window._stockZoom={start:45,end:100};
    $('stockCode').value=d.name+' '+d.code;
    $('stockTitle').textContent=d.code+' '+d.name;
    $('stockDataBanner').classList.add('show');
    var k=d.kline, close=k.close, ma5=k.ma5, ma10=stockMA(close,10), ma20=k.ma20, ma60=stockMA(close,60), rsi=stockLast(k.rsi), score=Math.round(Number(d.buy_decision.avg_score||0)*100);
    var trend=stockTrend(close,ma5,ma20), macdState=stockCross(k.macd_dif,k.macd_dea), maState=stockCross(ma5,ma10);
    var trendCls=trend.indexOf('强')>=0?'positive':(trend.indexOf('弱')>=0?'negative':'neutral');
    $('stockBasic').innerHTML='<div class="stock-signal-grid">'
      +'<article class="stock-signal-card"><div class="stock-signal-label">趋势</div><div class="stock-signal-value '+trendCls+'">'+trend+'</div></article>'
      +'<article class="stock-signal-card"><div class="stock-signal-label">综合分</div><div class="stock-signal-value">'+score+' / 100</div></article>'
      +'<article class="stock-signal-card"><div class="stock-signal-label">MACD</div><div class="stock-signal-value '+(macdState==='死叉'?'negative':'neutral')+'">'+macdState+'</div></article>'
      +'<article class="stock-signal-card"><div class="stock-signal-label">5日/10日线</div><div class="stock-signal-value '+(maState==='死叉'?'negative':(maState==='金叉'?'positive':'neutral'))+'">'+maState+'</div></article>'
      +'<article class="stock-signal-card"><div class="stock-signal-label">RSI(14)</div><div class="stock-signal-value">'+(rsi===null?'-':fmt(rsi,1))+'</div></article>'
      +'</div>';
    var ohlc=k.dates.map(function(_,i){ return [k.open[i],k.close[i],k.low[i],k.high[i]]; });
    var buyPoints=[], sellPoints=[], volma=stockMA(k.volume,5);
    for(var i=1;i<close.length;i++){
      if(ma5[i]!==null&&ma10[i]!==null&&ma5[i-1]!==null&&ma10[i-1]!==null){
        if(ma5[i]>=ma10[i]&&ma5[i-1]<ma10[i-1]) buyPoints.push([k.dates[i],k.low[i]]);
        if(ma5[i]<=ma10[i]&&ma5[i-1]>ma10[i-1]) sellPoints.push([k.dates[i],k.high[i]]);
      }
    }
    var kc=chart('stockKline'), red='#d84c42', green='#238266', gridLine='#e3e8ec';
    kc.setOption({
      animation:false,
      tooltip:{trigger:'axis',axisPointer:{type:'cross'},backgroundColor:'rgba(34,45,40,.92)',borderWidth:0,textStyle:{color:'#fff'}},
      legend:{top:10,left:'center',width:'94%',itemWidth:24,itemHeight:12,itemGap:24,textStyle:{color:'#515a60',fontSize:13},data:['日K','5日线','10日线','20日线','60日线','技术参考买点','技术参考卖点','成交量','成交量MA5','MACD柱','DIF','DEA','金叉','死叉风险','放量上涨']},
      grid:[{left:72,right:26,top:82,height:'54%'},{left:72,right:26,top:'68%',height:'10%'},{left:72,right:26,top:'81%',height:'10%'}],
      xAxis:[0,1,2].map(function(idx){ return {type:'category',data:k.dates,gridIndex:idx,boundaryGap:true,axisLine:{lineStyle:{color:'#cbd5d1'}},axisTick:{show:false},axisLabel:{show:idx===2,color:'#84909b',fontSize:11,rotate:0}}; }),
      yAxis:[{type:'value',scale:true,gridIndex:0,name:'价格',nameTextStyle:{color:'#84909b'},splitLine:{lineStyle:{color:gridLine}}},{type:'value',gridIndex:1,axisLabel:{show:false},splitLine:{show:false}},{type:'value',gridIndex:2,axisLabel:{show:false},splitLine:{lineStyle:{color:gridLine}}}],
      dataZoom:[{type:'inside',xAxisIndex:[0,1,2],start:45,end:100},{type:'slider',xAxisIndex:[0,1,2],bottom:4,height:16,start:45,end:100,borderColor:'#d8e2dd',fillerColor:'rgba(37,116,86,.12)',handleStyle:{color:'#6ba98f'}}],
      series:[
        {name:'日K',type:'candlestick',data:ohlc,xAxisIndex:0,yAxisIndex:0,itemStyle:{color:red,color0:green,borderColor:red,borderColor0:green}},
        {name:'5日线',type:'line',data:ma5,xAxisIndex:0,yAxisIndex:0,symbol:'none',lineStyle:{color:'#e79a24',width:1.5}},
        {name:'10日线',type:'line',data:ma10,xAxisIndex:0,yAxisIndex:0,symbol:'none',lineStyle:{color:'#367bd4',width:1.5}},
        {name:'20日线',type:'line',data:ma20,xAxisIndex:0,yAxisIndex:0,symbol:'none',lineStyle:{color:'#9a55d9',width:1.5}},
        {name:'60日线',type:'line',data:ma60,xAxisIndex:0,yAxisIndex:0,symbol:'none',lineStyle:{color:'#68737f',width:1.5}},
        {name:'技术参考买点',type:'scatter',data:buyPoints,xAxisIndex:0,yAxisIndex:0,symbol:'triangle',symbolRotate:0,symbolSize:13,itemStyle:{color:green}},
        {name:'技术参考卖点',type:'scatter',data:sellPoints,xAxisIndex:0,yAxisIndex:0,symbol:'triangle',symbolRotate:180,symbolSize:13,itemStyle:{color:red}},
        {name:'成交量',type:'bar',data:k.volume.map(function(v,i){return {value:v,itemStyle:{color:k.pcts[i]>=0?red:green}};}),xAxisIndex:1,yAxisIndex:1,barMaxWidth:7},
        {name:'成交量MA5',type:'line',data:volma,xAxisIndex:1,yAxisIndex:1,symbol:'none',lineStyle:{color:'#e79a24',width:1.2}},
        {name:'MACD柱',type:'bar',data:k.macd_bar,xAxisIndex:2,yAxisIndex:2,itemStyle:{color:function(p){return p.value>=0?red:green;}},barMaxWidth:7},
        {name:'DIF',type:'line',data:k.macd_dif,xAxisIndex:2,yAxisIndex:2,symbol:'none',lineStyle:{color:'#e84c4b',width:1}},
        {name:'DEA',type:'line',data:k.macd_dea,xAxisIndex:2,yAxisIndex:2,symbol:'none',lineStyle:{color:'#357bd7',width:1}},
        {name:'金叉',type:'scatter',data:[],xAxisIndex:0,yAxisIndex:0,symbol:'triangle',itemStyle:{color:red}},
        {name:'死叉风险',type:'scatter',data:[],xAxisIndex:0,yAxisIndex:0,symbol:'triangle',itemStyle:{color:green}},
        {name:'放量上涨',type:'scatter',data:[],xAxisIndex:0,yAxisIndex:0,symbol:'triangle',itemStyle:{color:red}}
      ]
    },true);
    var bd=d.buy_decision, sd=d.sell_decision, tierClass=bd.executable?'tag-ok':(bd.watchlist?'tag-info':'tag-warn');
    $('stockDecision').innerHTML='<div style="font-size:16px;line-height:1.75"><b>模型结论：</b><span class="tag '+tierClass+'">'+bd.tier+' · '+bd.action+'</span><br><span style="color:var(--text-secondary)">'+bd.summary+'</span></div>'
      +'<div style="display:flex;flex-wrap:wrap;gap:9px;margin:16px 0">'+bd.six_dim.map(function(x){ var color=x.status==='green'?'#237456':(x.status==='yellow'?'#b98216':'#c84f45'); return '<span style="border:1px solid #dce6e1;border-radius:10px;padding:7px 9px;font-size:12px">'+x.name+' <b style="color:'+color+'">'+x.status+'</b></span>'; }).join('')+'</div>'
      +'<div style="border-top:1px solid #e7eeea;padding-top:14px;line-height:1.8"><b>卖出风控：</b>'+sd.action+'<br><span style="font-size:12px;color:var(--text-secondary)">'+sd.summary+'<br>止盈策略：'+(sd.take_profit?sd.take_profit.msg:'未触发')+'；强制止损：'+sd.forced_stop+'</span></div>';
    $('stockFinance').innerHTML='<table><tr><th>净利润同比</th><th>营收同比</th><th>报告期</th><th>流通市值</th><th>换手率</th></tr><tr><td class="'+pctCls(d.profit_yoy)+'">'+pctTxt(d.profit_yoy)+'</td><td class="'+pctCls(d.rev_yoy)+'">'+pctTxt(d.rev_yoy)+'</td><td>'+(d.report_date||'-')+'</td><td>'+yi(d.float_cap)+'</td><td>'+fmt(d.turnover,2)+'%</td></tr></table>';
    $('stockNews').innerHTML=d.news&&d.news.length?d.news.map(function(n){var t=n.tone>0?'利好':(n.tone<0?'风险':'中性'),cl=n.tone>0?'tag-ok':(n.tone<0?'tag-warn':'tag-info');return '<div class="news-item"><span class="tag '+cl+'">'+t+'</span> '+n.summary+'<br><span class="news-time">'+(n.time||'')+' · '+(n.source||'本地资料')+'</span></div>';}).join(''):'<div style="color:var(--text-muted);padding:10px 0">当前本地资料中暂无该股近期事件；后续刷新将补充可用公开来源。</div>';
    try{ var peers=await api('/api/industry_peers?code='+encodeURIComponent(d.code)+'&topn=8'); $('stockPeers').innerHTML=peers.peers&&peers.peers.length?'<div style="font-size:12px;color:var(--text-muted);margin-bottom:10px">行业：'+adaptiveEsc(peers.industry)+'，共 '+Number(peers.count||0)+' 只同行业个股</div><table><tr><th>名称</th><th>代码</th><th>现价</th><th>涨跌</th><th>PE</th><th>PB</th><th>市值</th></tr>'+peers.peers.map(function(p){return '<tr style="cursor:pointer" onclick="selectStock(decodeURIComponent(\''+adaptiveJsArg(p.code)+'\'),decodeURIComponent(\''+adaptiveJsArg(p.name)+'\'))"><td><b>'+adaptiveEsc(p.name)+'</b></td><td>'+adaptiveEsc(p.code)+'</td><td>'+fmt(p.price)+'</td><td class="'+pctCls(p.pct)+'">'+pctTxt(p.pct)+'</td><td>'+fmt(p.pe,1)+'</td><td>'+fmt(p.pb,2)+'</td><td>'+yi(p.mktcap)+'</td></tr>';}).join('')+'</table>':'<div style="color:var(--text-muted)">无同行业对比数据</div>'; }catch(e){ $('stockPeers').innerHTML=''; }
  }catch(e){ $('stockBasic').innerHTML='<div class="banner">分析失败：'+e+'</div>'; }
}

async function runOptimize(){
  $('btnOp').disabled = true;
  $('opResult').innerHTML = '<div class="loading">网格搜索运行中（18组参数 × 双段回测，请耐心等待2-5分钟）…</div>';
  try{
    var d = await api('/api/optimize?strategy='+$('opStrategy').value);
    if(d.need_init){ $('opResult').innerHTML = '<div class="banner">'+d.message+'</div>'; return; }
    if(d.error){ $('opResult').innerHTML = '<div class="banner">'+d.error+'</div>'; return; }
    var rows = d.results.map(function(r){
      var isBest = d.best && r.topn===d.best.topn && r.rebalance===d.best.rebalance && r.use_gate===d.best.use_gate;
      return '<tr style="'+(isBest?'background:#fff7e6':'')+'">'
        +'<td>'+(isBest?'⭐ ':'')+r.topn+'</td><td>'+r.rebalance+'</td><td>'+(r.use_gate?'开':'关')+'</td>'
        +'<td class="'+pctCls(r.in_return)+'">'+pctTxt(r.in_return)+'</td><td>'+fmt(r.in_sharpe)+'</td><td class="down">'+pctTxt(r.in_dd)+'</td><td class="'+pctCls(r.in_excess)+'">'+pctTxt(r.in_excess)+'</td>'
        +'<td class="'+pctCls(r.out_return)+'">'+pctTxt(r.out_return)+'</td><td>'+fmt(r.out_sharpe)+'</td><td class="down">'+pctTxt(r.out_dd)+'</td><td class="'+pctCls(r.out_excess)+'">'+pctTxt(r.out_excess)+'</td>'
        +'<td>'+(r.overfit_warning?'<span class="tag tag-warn">过拟合警告</span>':'<span class="tag tag-ok">通过</span>')+'</td></tr>';
    }).join('');
    var bestHtml = d.best? ('<div class="banner" style="background:#e8f7ee;border-color:#a5d6b8;color:#1a7f4b">推荐参数：持仓 '+d.best.topn+' 只 · '+d.best.rebalance+' 日调仓 · 门控'+(d.best.use_gate?'开':'关')
      +'（样本外年化 '+pctTxt(d.best.out_return)+'，夏普 '+fmt(d.best.out_sharpe)+'）— 可回到「回测」页用该参数复核'
      +(d.best.adjusted_by_tracking?('<br/><span style="color:#dc3545">跟踪调整：'+d.best.adjusted_by_tracking+'</span>'):'')+'</div>') : '';
    var fbHtml = '';
    if(d.tracking_feedback && d.tracking_feedback.count>0){
      var fb = d.tracking_feedback;
      fbHtml = '<div style="margin:8px 0;padding:8px;background:#e3f2fd;border:1px solid #2196f3;border-radius:4px"><b>持仓跟踪反馈（'+fb.strategy+'）</b>：跟踪 '+fb.count+' 只 · 平均收益 '+(fb.avg_ret_pct!==null?pctTxt(fb.avg_ret_pct):'-')+' · 胜率 '+(fb.win_rate_pct!==null?fb.win_rate_pct+'%':'-')+' · 均持 '+fb.avg_hold_days+' 天<ul style="margin:4px 0 0 16px;font-size:12px">'+fb.advice.map(function(a){ return '<li>'+a+'</li>'; }).join('')+'</ul></div>';
    }
    var rebHtml = '';
    if(d.rebalance_compare){
      var keys = Object.keys(d.rebalance_compare);
      rebHtml = '<div style="margin:8px 0;font-size:13px"><b>调仓周期对比：</b>'+keys.map(function(k){ return k+' 样本外夏普均值 '+fmt(d.rebalance_compare[k].avg_out_sharpe)+' / 最优 '+fmt(d.rebalance_compare[k].best_out_sharpe); }).join(' ｜ ')+'</div>';
    }
    $('opResult').innerHTML = bestHtml + fbHtml + rebHtml
      +'<div style="margin-bottom:8px;font-size:13px;color:#5a6b7d">样本内：'+d.split.in_sample+' ｜ 样本外：'+d.split.out_sample+'</div>'
      +tableScroll('<table><tr><th>持仓数</th><th>调仓周期</th><th>门控</th><th>样本内年化</th><th>内夏普</th><th>内回撤</th><th>内超额</th><th>样本外年化</th><th>外夏普</th><th>外回撤</th><th>外超额</th><th>过拟合检验</th></tr>'+rows+'</table>',1020)
      +'<div class="disclaimer">'+d.note+'</div>';
  }catch(e){
    $('opResult').innerHTML = '<div class="banner">优化失败：'+e+'</div>';
  }finally{ $('btnOp').disabled = false; }
}

async function loadNews(){
  if(!$('newsList')||!$('newsHits')||!$('hotTable')) return;
  try{
    var d = await api('/api/news');
    $('newsList').innerHTML = d.news.map(function(n){
      return '<div class="news-item"><span class="news-time">'+(n.time||'')+'</span>'+n.summary+'</div>';
    }).join('') || '暂无';
    $('newsHits').innerHTML = d.stock_hits.length? d.stock_hits.map(function(h){
      var tag = h.tone>0?'<span class="tag tag-ok">利好</span>':(h.tone<0?'<span class="tag tag-warn">风险</span>':'<span class="tag tag-info">中性</span>');
      return '<div class="news-item">'+tag+'<b>'+h.name+'</b>（'+h.code+'）'
        +(h.keywords.length?' 关键词：'+h.keywords.join('、'):'')
        +'<br/><span style="color:#5a6b7d">'+h.summary+'</span><br/><span class="news-time">'+(h.time||'')+' · '+h.source+'</span></div>';
    }).join('') : '<div style="color:#9aa5b1;padding:10px 0">当前快讯中暂无股票池个股命中</div>';
    try{
      var hot = await api('/api/hot');
      $('hotTable').innerHTML = '<table><tr><th>排名</th><th>股票</th><th>现价</th><th>涨跌幅</th><th>行业</th><th>排名变化</th></tr>'
        + hot.hot.slice(0,50).map(function(h){
        return '<tr><td>'+h.rank+'</td><td>'+(h.name||h.code)+'</td><td>'+fmt(h.price)+'</td>'
          +'<td class="'+pctCls(h.pct)+'">'+pctTxt(h.pct)+'</td><td>'+(h.industry||'-')+'</td>'
          +'<td>'+(h.rank_chg>0?'<span class="up">↑'+h.rank_chg+'</span>':(h.rank_chg<0?'<span class="down">↓'+Math.abs(h.rank_chg)+'</span>':'-'))+'</td></tr>';
      }).join('') + '</table>';
    }catch(e){
      $('hotTable').innerHTML = '<div style="color:#9aa5b1;padding:10px 0">人气榜加载失败</div>';
    }
  }catch(e){
    $('newsList').innerHTML = '加载失败：'+e;
  }
}

loadStrategies();
loadDataValidity();
loadMarketGate();
installWorkspaceTabRails();
restoreAppNavigation();
document.addEventListener('click', function(e){
  var adaptiveTab=e.target.closest && e.target.closest('#p-adaptive .adaptive-section-tab');
  if(adaptiveTab){
    e.preventDefault();
    setAdaptiveSection(adaptiveTab.dataset.section,adaptiveTab);
    return;
  }
  if(!e.target.closest('#stockCode') && !e.target.closest('#stockSuggest')){
    $('stockSuggest').style.display = 'none';
  }
  if(!e.target.closest('#headerSearch') && !e.target.closest('#headerSuggest')){
    $('headerSuggest').style.display = 'none';
  }
});

// Legacy adaptive/paper fragments can still arrive from a cached API payload.
// Normalize only the visible workspace copy so the public UI consistently
// describes the two active strategies while historical replay data stays
// untouched in the backend.
function normalizeActiveStrategyCopy(root){
  if(!root) return;
  var walker=document.createTreeWalker(root,NodeFilter.SHOW_TEXT), node;
  while((node=walker.nextNode())){
    var value=node.nodeValue||'';
    var normalized=value.replace(/四套/g,'两套').replace(/三套/g,'两套').replace(/四策略/g,'两策略').replace(/三策略/g,'两策略');
    if(normalized!==value) node.nodeValue=normalized;
  }
}
if(typeof MutationObserver!=='undefined'){
  var strategyCopyObserver=new MutationObserver(function(){
    normalizeActiveStrategyCopy($('p-paper'));
    normalizeActiveStrategyCopy($('p-adaptive'));
  });
  strategyCopyObserver.observe(document.body,{childList:true,subtree:true});
}

// 暗色模式切换
async function refreshApp(){
  if(window._manualRefreshInFlight) return;
  window._manualRefreshInFlight=true;
  var button=document.querySelector('.page-refresh'), original=button&&button.textContent;
  if(button){ button.disabled=true; button.textContent='刷新中…'; }
  try{
    await loadMarketGate();
    var page=(document.querySelector('.page.active')||{}).id;
    if(page==='p-paper'){
      var view=window._paperWorkspace||'portfolio';
      if(view==='risk') await loadPaperRisk(true);
      else if(view==='strategy') await loadPaperStrategyCenter();
      else if(view==='research') await loadPaperResearchValidation();
      else await loadPaper({refresh:true});
    }else if(page==='p-select'){
      var jobs=[];
      if(typeof loadStrategies==='function') jobs.push(loadStrategies());
      if(typeof loadDataValidity==='function') jobs.push(loadDataValidity());
      await Promise.all(jobs);
    }else if(page==='p-sector'&&typeof loadSectors==='function'){
      await Promise.all([loadSectors(),typeof loadLinkage==='function'?loadLinkage():Promise.resolve()]);
    }else if(page==='p-adaptive'&&typeof loadAdaptive==='function'){
      await loadAdaptive();
    }else if(page==='p-stock'&&typeof analyzeStock==='function'){
      await analyzeStock();
    }else{
      window.location.reload();
    }
  }finally{
    window._manualRefreshInFlight=false;
    if(button){ button.disabled=false; button.textContent=original||'刷新页面'; }
  }
}
function toggleDark(){
  document.body.classList.toggle('dark');
  localStorage.setItem('darkMode', document.body.classList.contains('dark'));
  syncThemeControl();
  // ECharts 需要重绘
  Object.keys(charts).forEach(function(k){ charts[k].resize(); });
}
if(localStorage.getItem('darkMode')==='true') document.body.classList.add('dark');
function syncThemeControl(){
  var dark=document.body.classList.contains('dark'),button=$('themeToggle');
  if(!button) return;
  button.setAttribute('aria-pressed',dark?'true':'false');
  button.setAttribute('aria-label',dark?'切换浅色模式':'切换深色模式');
  button.title=dark?'切换浅色模式':'切换深色模式';
}
syncThemeControl();

// Header 快搜
var _headerTimer;
function searchStockHeader(){
  clearTimeout(_headerTimer);
  var q = $('headerSearch').value.trim();
  if(q.length<1){ $('headerSuggest').style.display='none'; return; }
  _headerTimer = setTimeout(async function(){
    try{
      var d = await api('/api/stock_search?q='+encodeURIComponent(q));
      if(!d.results.length){ $('headerSuggest').style.display='none'; return; }
      $('headerSuggest').innerHTML = d.results.map(function(r){
        return '<div style="padding:6px 12px;cursor:pointer;font-size:13px;border-bottom:1px solid var(--border-light)" onmousedown="document.querySelector(\'[data-page=p-stock]\').click();selectStock(decodeURIComponent(\''+adaptiveJsArg(r.code)+'\'),decodeURIComponent(\''+adaptiveJsArg(r.name)+'\'));$(\'headerSearch\').value=\'\';$(\'headerSuggest\').style.display=\'none\'">'+adaptiveEsc(r.name)+' <span style="color:var(--text-muted)">'+adaptiveEsc(r.code)+'</span> <span style="color:var(--text-secondary);font-size:11px">'+adaptiveEsc(r.industry||'')+'</span></div>';
      }).join('');
      $('headerSuggest').style.display='block';
    }catch(e){}
  }, 200);
}
function gotoStockDetail(){
  var q = $('headerSearch').value.trim();
  if(!q) return;
  var m = q.match(/(\d{6})/);
  if(m) window._stockCode = m[1];
  document.querySelector('[data-page=p-stock]').click();
  $('stockCode').value = q;
  $('headerSearch').value = '';
  $('headerSuggest').style.display = 'none';
  analyzeStock();
}
// Keep the two live ledger views current without requiring a full-page reload.
// loadPaper() already shares an in-flight request, so a slow response cannot
// create overlapping overview calls or overwrite a newer render.  Risk and
// research keep their own refresh policies and are intentionally not polled
// here.
setInterval(function(){
  var paper=$('p-paper');
  if(!paper || !paper.classList.contains('active')) return;
  if(window._paperWorkspace==='risk') loadPaperRisk(false);
  else if(window._paperWorkspace==='portfolio'||window._paperWorkspace==='activity') loadPaper();
}, 180000);
setInterval(loadMarketGate,180000);
window.onresize = function(){ Object.keys(charts).forEach(function(k){ charts[k].resize(); }); };

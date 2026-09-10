/* PR-55：由 frontend/app.js 拆分（纯搬运，逻辑/文案未改） */
// 跨模块依赖（由原单文件作用域推导）
import { api, apiPost } from "../core/api.js";
import { $, tableScroll } from "../core/dom.js";
import { adaptiveEsc, dataValidityTone, fmt, pctCls, pctTxt, sellTag, sigTag, yi } from "../core/format.js";
import { APP_PAGE_KEY } from "../core/navigation.js";
import { PAPER_NAV_TTL_MS } from "../core/state.js";

export function activateStrategyWorkspace(pageId){
  document.querySelectorAll('.page').forEach(function(x){x.classList.remove('active');});
  $(pageId).classList.add('active');
  document.querySelectorAll('.tab').forEach(function(x){x.classList.remove('active');x.setAttribute('aria-current','false');});
  var primary = document.querySelector('.tab[data-page="p-select"]');
  if(primary){primary.classList.add('active');primary.setAttribute('aria-current','page');}
  sessionStorage.setItem(APP_PAGE_KEY,'p-select');
  history.replaceState(null,'','#select');
}

export function chooseStrategy(strategyId){
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

/* ---------- 策略选股：模拟盘已启用策略的盘后自动选股（分组展示） ---------- */
export var paperStrategyFilter='';

export var paperSelectionCache=null;

export var paperSelectionCacheAt=0;

// 始终缓存全量五组结果，筛选只在本地做：否则第一次取到的（过滤后）响应会被
// 后续 tab 当成完整缓存，切换策略时看到的是上一次的旧分组。
export function paperSelectionView(){
  if(!paperSelectionCache) return null;
  var groups=paperSelectionCache.strategies||[];
  if(paperStrategyFilter){
    groups=groups.filter(function(g){return g.strategy_id===paperStrategyFilter;});
  }
  return Object.assign({},paperSelectionCache,{strategies:groups});
}

export function choosePaperStrategy(strategyId, el){
  activateStrategyWorkspace('p-select');
  paperStrategyFilter=strategyId||'';
  document.querySelectorAll('#p-select .module-tab').forEach(function(x){
    var selected=(x.dataset.strategy||'')===paperStrategyFilter;
    x.classList.toggle('active', selected);
    x.setAttribute('aria-selected',selected?'true':'false');
  });
  // 切换只重渲染已有结果；没有缓存时才读取一次，不触发重新计算。
  var view=paperSelectionView();
  if(view) renderPaperSelection(view);
  else loadPaperSelection();
}

export async function loadPaperSelection(){
  var target=$('selectResult'); if(!target) return;
  // 60 秒内重复进入选股页直接复用上次结果，避免每次切页都重新请求。
  if(paperSelectionCache&&paperSelectionCacheAt&&Date.now()-paperSelectionCacheAt<PAPER_NAV_TTL_MS){
    renderPaperSelection(paperSelectionView()||paperSelectionCache);
    return;
  }
  target.innerHTML='<div class="loading">正在读取最近一个交易日的策略选股结果…</div>';
  try{
    var d=await api('/api/paper-selection');
    paperSelectionCache=d;
    paperSelectionCacheAt=Date.now();
    renderPaperSelection(paperSelectionView()||d);
  }catch(e){ target.innerHTML='<div class="banner">读取策略选股结果失败：'+adaptiveEsc(e.message||e)+'</div>'; }
}

export function renderPaperSelection(d){
  var target=$('selectResult'); if(!target) return;
  var groups=d.strategies||[];
  if(!d.found||!groups.length){
    target.innerHTML='<div class="banner">尚无策略选股结果。盘后 17:25 会自动运行，也可点击「立即重跑选股」。</div>';
    return;
  }
  var statusTag=function(s){
    if(s.status==='ok') return '<span class="tag tag-ok">已入选 '+arguments[1]+' 只</span>';
    if(s.status==='empty') return '<span class="tag tag-warn">候选不足 · 0 只</span>';
    if(s.status==='blocked') return '<span class="tag tag-warn">数据门禁未通过</span>';
    if(s.status==='error') return '<span class="tag tag-warn">运行异常</span>';
    return '<span class="tag">未运行</span>';
  };
  var html='<div class="result-toolbar"><span class="tag tag-info">交易日 '+adaptiveEsc(d.trade_date||'—')+'</span>'
    +'<span class="tag tag-info">去重规则：同一只股票被多策略选中时分别归属各策略，不合并</span>'
    +'<span class="tag tag-info">重跑按「交易日 + 策略编号」覆盖当天结果</span></div>';
  groups.forEach(function(g){
    var picks=g.picks||[];
    var rows=picks.map(function(p){
      var reasons=(p.reasons||[]).slice(0,3).map(function(r){return '<li>'+adaptiveEsc(r)+'</li>';}).join('');
      return '<tr><td>'+Number(p.rank_no)+'</td>'
        +'<td><b>'+adaptiveEsc(p.name||p.code)+'</b><br/><span style="color:#9aa5b1;font-size:12px">'+adaptiveEsc(p.code)+' · '+adaptiveEsc(p.industry||'-')+'</span></td>'
        +'<td>'+fmt(p.price)+'</td>'
        +'<td class="'+pctCls(p.pct)+'">'+pctTxt(p.pct)+'</td>'
        +'<td>'+(p.score==null?'—':Number(p.score).toFixed(3))+'</td>'
        +'<td class="'+pctCls(p.super_net)+'">'+yi(p.super_net)+'</td>'
        +'<td><ul class="reasons">'+reasons+'</ul></td></tr>';
    }).join('');
    var body=rows||('<tr><td colspan="7" style="text-align:center;color:#9aa5b1">'
      +(g.status==='empty'?'当日该策略没有通过门禁的候选（允许少于 5 只，也允许为 0）'
        :(g.status==='blocked'?'数据尚未就绪：'+adaptiveEsc(g.message||'')
          :(g.status==='error'?'运行异常：'+adaptiveEsc(g.message||''):'该策略当天未运行')))+'</td></tr>');
    html+='<section class="paper-selection-group"><header><div><span>'+adaptiveEsc(g.label||'')+' · '+adaptiveEsc(g.strategy_name||g.strategy_id)+'</span>'
      +'<h4>'+adaptiveEsc(g.label||'')+' '+adaptiveEsc(g.strategy_name||'')+'<small style="margin-left:8px;font-weight:400;color:#9aa5b1">'+adaptiveEsc(g.strategy_id)+'</small></h4></div>'
      +'<div class="group-tags">'+statusTag(g.status, picks.length)
      +(g.updated_at?'<span class="tag">'+adaptiveEsc(String(g.updated_at).replace("T"," ").substring(0,19))+'</span>':'')+'</div></header>'
      +tableScroll('<table><tr><th>#</th><th>股票</th><th>现价</th><th>涨跌</th><th>评分</th><th>超大单净流入</th><th>入选理由</th></tr>'+body+'</table>',1180)
      +'</section>';
  });
  html+='<div class="disclaimer">选股结果来自各策略模型族的评分排序，仅供研究参考，不构成投资建议；结果不会生成订单或改动模拟盘账本。</div>';
  target.innerHTML=html;
}

export async function runPaperSelection(){
  var btn=$('btnSelect'); if(btn) btn.disabled=true;
  var target=$('selectResult');
  if(target) target.innerHTML='<div class="loading">正在按已启用策略评分重跑选股（约 30-90 秒）…</div>';
  try{
    var d=await apiPost('/api/paper-selection/run?topn='+encodeURIComponent(($('selTopn')||{}).value||5)+'&confirmed=true');
    paperSelectionCache=await api('/api/paper-selection');
    paperSelectionCacheAt=Date.now();
    renderPaperSelection(paperSelectionView()||paperSelectionCache);
    var done=(d.strategies||[]).map(function(s){return s.label+' '+s.status+'('+s.picks.length+')';}).join(' · ');
    if(target) target.insertAdjacentHTML('afterbegin','<div class="tag tag-ok">已重跑：'+adaptiveEsc(done)+'</div>');
  }catch(e){
    if(target) target.innerHTML='<div class="banner">重跑失败：'+adaptiveEsc(e.message||e)+'</div>';
  }finally{ if(btn) btn.disabled=false; }
}

export function showStrategyWatch(){
  // 选股模块已取消独立“自选观察”页，历史入口统一指向可筛选的策略验证。
  showSelectionValidation();
}

export function showSelectionValidation(){ activateStrategyWorkspace('p-selection-evaluation'); loadSelectionEvaluation(); }

export function selectionAssessmentClass(state){ return state==='validated' ? 'validated' : (state==='review' ? 'review' : 'watch'); }

export function selectionAssessmentCopy(state){
  if(state==='review') return {label:'\u9700\u590d\u6838',advice:'\u6301\u7eed\u8dd1\u8f93\u57fa\u51c6\uff0c\u5148\u505a\u6837\u672c\u5916\u548c\u884c\u4e1a\u62c6\u89e3\uff0c\u4e0d\u81ea\u52a8\u4fee\u6539\u89c4\u5219\u3002'};
  if(state==='validated') return {label:'\u7ee7\u7eed\u9a8c\u8bc1',advice:'\u5f53\u524d\u51fa\u73b0\u6b63\u8d85\u989d\uff0c\u4ecd\u9700\u8986\u76d6\u66f4\u591a\u5e02\u573a\u9636\u6bb5\u3002'};
  if(state==='watch') return {label:'\u7ee7\u7eed\u89c2\u5bdf',advice:'\u6682\u672a\u5f62\u6210\u7a33\u5b9a\u4f18\u52bf\uff0c\u7ee7\u7eed\u79ef\u7d2f\u6837\u672c\u3002'};
  return {label:'\u6837\u672c\u79ef\u7d2f\u4e2d',advice:'\u6837\u672c\u4e0d\u8db3 20 \u4e2a\uff0c\u4e0d\u81ea\u52a8\u8c03\u6574\u7b56\u7565\u89c4\u5219\u3002'};
}

export async function loadSelectionEvaluation(){
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

export function filterSelectionEvaluationRows(){
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

export async function refreshSelectionEvaluation(){
  var button=$('selectionEvalRefresh'), status=$('selectionEvalStatus'); button.disabled=true; status.textContent='\u6b63\u5728\u6838\u9a8c\u5f53\u65e5\u6536\u76d8\u5feb\u7167\u2026';
  try{ var d=await apiPost('/api/selection-evaluation/refresh'); status.textContent=d.status==='ok' ? ('\u5df2\u5199\u5165 '+d.observed+' \u6761\u6536\u76d8\u8ddf\u8e2a\u8bb0\u5f55\u3002') : '\u5f53\u524d\u8fd8\u4e0d\u662f\u53ef\u5ba1\u8ba1\u7684\u5f53\u65e5\u6536\u76d8\u5feb\u7167\uff1b\u81ea\u52a8\u4efb\u52a1\u4f1a\u5728\u4e0b\u4e00\u4e2a\u4ea4\u6613\u65e5\u7ee7\u7eed\u3002'; await loadSelectionEvaluation(); }
  catch(e){ status.textContent='\u5237\u65b0\u5931\u8d25\uff1a'+e.message; } finally{ button.disabled=false; }
}

export async function loadStrategies(){
  try{
    // PR-45：/api/strategies 已归 Strategy Admin（注册表）；扫描页用静态扫描策略专属接口。
    var d = await api('/api/scanner-strategies');
    var opts = d.strategies.map(function(s){ return '<option value="'+s.id+'" data-desc="'+s.desc+'">'+s.name+' — '+s.desc+'</option>'; }).join('');
    if($('selStrategy')) $('selStrategy').innerHTML = opts;
    $('selStrategy').onchange = function(){
      var o = this.options[this.selectedIndex];
      $('selStrategyDesc').textContent = o.dataset.desc || '';
    };
    // 默认选中第一个策略；进入页面只读取最近一次结果，重新计算必须由用户点击按钮触发。
    if(d.strategies.length>0 && $('selStrategy').value===d.strategies[0].id){
      $('selStrategyDesc').textContent = d.strategies[0].desc;
      setTimeout(loadLatestSelection, 0);
    }
  }catch(e){ console.error('loadStrategies failed:', e); }
}

export function renderGate(g){
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

export async function loadMarketGate(){
  try{
    var overview=await api('/api/overview');
    renderGate(overview&&overview.gate);
  }catch(e){
    renderGate({light:'unknown',advice:'市场门控读取失败，执行层按保守规则处理。'});
  }
}

export async function checkInit(){
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

export async function startInit(){ await apiPost('/api/init?years=3&size=0'); checkInit(); }

export function renderDataValidity(d){
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

export async function loadDataValidity(){
  var panel=$('dataValidityCards'); if(!panel) return;
  try{ var d=await api('/api/data-validity'); renderDataValidity(d); return d; }
  catch(e){ panel.innerHTML='<div class="banner">数据有效性读取失败：'+adaptiveEsc(e.message||e)+'</div>'; }
}

export async function startManualDataUpdate(){
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

export async function startFactorIncrementalUpdate(){
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

export async function cancelManualDataUpdate(){
  var btn=$('dataValidityCancel'); if(btn){btn.disabled=true;btn.textContent='取消中…';}
  try{ await apiPost('/api/data-validity/incremental/cancel'); await loadDataValidity(); }
  catch(e){ alert('取消增量失败：'+(e.message||e)); }
  finally{ if(btn){btn.disabled=false;btn.textContent='取消增量';} }
}

export function renderSelectionResult(d, fromCache){
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

export async function loadLatestSelection(){
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

export async function runSelect(){
  $('btnSelect').disabled = true;
  $('selectResult').innerHTML = '<div class="loading">正在计算因子并选股（约10-30秒）…</div>';
  try{
    var d = await api('/api/select?strategy='+$('selStrategy').value+'&topn='+$('selTopn').value);
    renderSelectionResult(d,false);
  }catch(e){
    $('selectResult').innerHTML = '<div class="banner">选股失败：'+e+'</div>';
  }finally{ $('btnSelect').disabled = false; }
}

export async function compareStrategies(){
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
      +'<div class="disclaimer">三种研究策略分别独立计算，同一只股票可能在多个策略中同时出现。仅供研究参考，不会改变已启用的模拟策略。</div>';
  }catch(e){ $('selectResult').innerHTML = '<div class="banner">对比失败：'+adaptiveEsc(e&&e.message||e)+'</div>'; }
}

// ---------- 持仓跟踪 ----------
export async function trackAdd(code, name, price, strategy, btn){
  if(btn){ btn.disabled = true; btn.textContent = '…'; }
  try{
    var d = await apiPost('/api/track/add?code='+code+'&name='+encodeURIComponent(name)+(price?'&cost='+price:'')+'&strategy='+encodeURIComponent(strategy||''));
    if(btn){ btn.textContent = d.ok ? '已跟踪' : '已在池'; }
  }catch(e){ if(btn){ btn.disabled=false; btn.textContent='＋跟踪'; } alert('加入失败: '+e); }
}

export async function trackAddAll(strategy){
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

export async function trackRemove(code){
  if(!confirm('移出跟踪池：'+code+'？')) return;
  await apiPost('/api/track/remove?code='+code);
  loadTrack();
}

export async function loadTrack(){
  $('trackTable').innerHTML = '<div class="loading">检查跟踪池中（拉取实时行情）…</div>';
  try{
    var d = await api('/api/track/check');
    window._trackRaw = d;
    renderTrack(d);
  }catch(e){
    $('trackTable').innerHTML = '<div class="banner">加载失败：'+e+'</div>';
  }
}

export function applyTrackFilter(){
  if(window._trackRaw) renderTrack(window._trackRaw);
}

export function renderTrack(d){
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

/* PR-55：由 frontend/app.js 拆分（纯搬运，逻辑/文案未改） */
// 跨模块依赖（由原单文件作用域推导）
import { api, apiPost } from "../core/api.js";
import { $, tableScroll } from "../core/dom.js";
import { cny, fmt, pctCls, pctTxt, riskLevelView, riskMetric, riskText, zhRiskText } from "../core/format.js";
import { PAPER_NAV_TTL_MS } from "../core/state.js";

export function paperAuditBlock(title, count, body){
  return '<details class="paper-audit"><summary><span>'+title+'<span class="paper-audit-count">'+count+'</span></span><span class="paper-status-note">点击查看完整记录</span></summary><div class="paper-audit-body">'+body+'</div></details>';
}

export function applyPaperRiskAuditFilter(){
  var account=$('paperRiskAccountFilter')?$('paperRiskAccountFilter').value:'';
  var level=$('paperRiskLevelFilter')?$('paperRiskLevelFilter').value:'';
  var date=$('paperRiskDateFilter')?$('paperRiskDateFilter').value:'';
  document.querySelectorAll('#paperRiskAuditRows tr').forEach(function(row){
    row.hidden=!!((account&&row.dataset.account!==account)||(level&&row.dataset.level!==level)||(date&&row.dataset.date!==date));
  });
}

export function renderPaperAudit(d){
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

export function renderPaperRisk(d){
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

export async function loadPaperRisk(forceRefresh){
  if(!forceRefresh&&window._paperRiskDashboard
     &&!window._paperRiskDashboard.initializing&&!window._paperRiskDashboard.refreshing){
    renderPaperRisk(window._paperRiskDashboard);
    // 60 秒内的切页直接用上次快照，不再每次都发 risk-overview 请求。
    if(Date.now()-(window._paperRiskDashboardAt||0)<PAPER_NAV_TTL_MS) return;
  }
  if(window._paperRiskRequest) return window._paperRiskRequest;
  var button=$('paperRiskRefresh');
  if(button){button.disabled=true;button.textContent=forceRefresh?'正在刷新…':'读取中…';}
  var request=forceRefresh?apiPost('/api/paper/risk-refresh'):api('/api/paper/risk-overview');
  window._paperRiskRequest=request.then(function(d){
    window._paperRiskDashboard=d;window._paperRiskDashboardAt=Date.now();renderPaperRisk(d);
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

export async function refreshPaperRisk(){await loadPaperRisk(true);}

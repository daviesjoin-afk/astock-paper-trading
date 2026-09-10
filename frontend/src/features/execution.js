/* PR-55：由 frontend/app.js 拆分（纯搬运，逻辑/文案未改） */
// 跨模块依赖（由原单文件作用域推导）
import { api, apiPost } from "../core/api.js";
import { $ } from "../core/dom.js";
import { adaptiveEsc, fmt, riskText } from "../core/format.js";
import { PAPER_NAV_TTL_MS } from "../core/state.js";
import { adaptiveValue } from "./adaptive.js";

export function allocationActionPanel(d){
  var alloc=(d&&d.allocation)||{},latest=alloc.latest_decision||{},active=alloc.active||{};
  var rows=Object.keys(active).map(function(id){var a=active[id]||{};return '<li><div><b>'+adaptiveEsc(id)+'</b><small>当前分摊 '+adaptiveValue(a.weight_pct,'%',1)+' · 决策 #'+adaptiveEsc(String(a.decision_id||'—'))+' · 生效 '+adaptiveEsc(String(a.effective_date||'—'))+'</small></div><button class="ghost" onclick="rollbackAdaptiveAllocation(\''+adaptiveEsc(id)+'\')">回滚分摊</button></li>';}).join('');
  var canApply=latest&&latest.can_apply;
  var applyBar=canApply?'<div class="adaptive-human-actions"><span>最新决策 #'+Number(latest.id)+' · 阶段 '+adaptiveEsc(latest.stage||'')+'</span><button class="ghost" onclick="applyAdaptiveAllocation('+Number(latest.id)+')">人工批准权重分摊</button></div>':(latest&&latest.status==='applied'?'<div class="adaptive-notice">最新决策已应用为资金分摊覆盖。</div>':'');
  if(!canApply&&!rows){return '<div class="adaptive-notice">资金分摊覆盖未启用：继续使用各策略 max_exposure 基准权重。需要决策进入 advisory/eligible 阶段且人工批准后才生效。</div>';}
  return applyBar+(rows?'<ul class="adaptive-run-log">'+rows+'</ul>':'');
}

export async function loadPaperExecution(forceRefresh){
  var target=$('paperExecutionView');
  if(!target) return;
  var cached=window._paperExecutionCenterCache;
  if(!forceRefresh&&cached){
    renderPaperExecution(cached.data);
    if(Date.now()-cached.at<PAPER_NAV_TTL_MS) return;
  }else{
    window._paperExecutionCenterCache=null;
    target.innerHTML='<div class="loading">正在读取执行画像与执行队列…</div>';
  }
  try{
    var d=await api('/api/paper/execution-profiles');
    window._paperExecutionCenterCache={data:d,at:Date.now()};
    renderPaperExecution(d);
  }catch(err){
    target.innerHTML='<div class="paper-empty">执行画像读取失败：'+riskText(err.message||err)+'<button class="ghost" style="margin-left:10px" onclick="loadPaperExecution()">重试</button></div>';
  }
}

export function execText(v){ return (v===null||v===undefined||v==='')?'—':riskText(String(v)); }

export function execOrderTypeLabel(v){ return v==='market'?'市价':(v==='limit'?'限价':execText(v)); }

export function execQueueRow(row,actions){
  return '<tr>'
    +'<td>'+execText(row.code)+'</td>'
    +'<td>'+execText(row.name)+'</td>'
    +'<td>'+execText(row.account_id)+'</td>'
    +'<td>'+fmt(row.qty,0)+'</td>'
    +'<td>'+execText(row.planned_price)+'</td>'
    +'<td>'+execText(row.profile_label)+'</td>'
    +'<td><span class="tag '+(row.status==='pending_verification'?'tag-warn':'tag-info')+'">'+execText(row.status)+'</span></td>'
    +'<td class="exec-queue-reason">'+execText(row.reason)+'</td>'
    +'<td>'+(actions?'<div class="exec-queue-actions">'
        +'<button onclick="verifyExecutionOrder('+Number(row.order_id)+',1)">放行</button>'
        +'<button class="exec-reject" onclick="verifyExecutionOrder('+Number(row.order_id)+',0)">驳回</button></div>':'—')+'</td>'
    +'</tr>';
}

export function execQueueTable(rows,emptyText,actions){
  if(!rows||!rows.length) return '<div class="paper-empty">'+riskText(emptyText)+'</div>';
  return '<div class="table-scroll"><table class="exec-queue-table"><thead><tr>'
    +'<th>代码</th><th>名称</th><th>策略</th><th>股数</th><th>委托价</th><th>画像</th><th>状态</th><th>说明</th><th>操作</th>'
    +'</tr></thead><tbody>'+rows.map(function(row){return execQueueRow(row,actions);}).join('')+'</tbody></table></div>';
}

export function renderPaperExecution(d){
  var target=$('paperExecutionView'); if(!target) return;
  d=(d&&typeof d==='object')?d:{};
  var catalog=(d.catalog||[]).map(function(p){
    var flags=[];
    if(p.batch) flags.push('批量窗口');
    if(p.verification_required) flags.push('人工核验');
    if(p.strict_ttl) flags.push('严格时限');
    if(!flags.length) flags.push('—');
    return '<tr><td><b>'+execText(p.family)+'</b></td>'
      +'<td>'+execText(p.label)+'</td>'
      +'<td>'+execText(p.urgency)+'</td>'
      +'<td>'+execOrderTypeLabel(p.order_type)+'</td>'
      +'<td>'+(p.limit_offset_pct===null||p.limit_offset_pct===undefined?'—':fmt(p.limit_offset_pct,1)+'%')+'</td>'
      +'<td>'+(p.ttl_minutes===null||p.ttl_minutes===undefined?'不限':fmt(p.ttl_minutes,0)+' 分钟')+'</td>'
      +'<td>'+riskText(flags.join(' · '))+'</td>'
      +'</tr>';
  }).join('');
  var accounts=(d.accounts||[]).map(function(a){
    var note=a.fallback_from?('未知画像 '+execText(a.fallback_from)+' 保守回落'):'';
    return '<tr'+(a.active?'':' class="exec-inactive"')+'><td><b>'+execText(a.name)+'</b></td>'
      +'<td>'+execText(a.id)+'</td>'
      +'<td>'+execText(a.risk_profile)+'</td>'
      +'<td>'+execText(a.label)+(note?'<small>（'+note+'）</small>':'')+'</td>'
      +'<td>'+execOrderTypeLabel(a.order_type)+'</td>'
      +'<td>'+(a.ttl_minutes===null||a.ttl_minutes===undefined?'不限':fmt(a.ttl_minutes,0)+' 分钟')+'</td>'
      +'<td>'+(a.active?'<span class="tag tag-ok">启用中</span>':'<span class="tag">未启用</span>')+'</td>'
      +'</tr>';
  }).join('');
  var dispatch=(d.dispatch||{});
  var windows=(dispatch.windows||{});
  var settings=(dispatch.settings||{});
  var batchRows=dispatch.batch_queue||[];
  var verifyRows=dispatch.verification_queue||[];
  var windowBadge=windows.in_window
    ? '<span class="tag tag-ok">窗口内 '+(windows.current_window?execText(windows.current_window.start)+'–'+execText(windows.current_window.end):'')+'</span>'
    : (windows.next_window?'<span class="tag tag-info">下一窗口 '+execText(windows.next_window.start)+'–'+execText(windows.next_window.end)+'</span>':'<span class="tag">今日已无窗口</span>');
  var switches=['batch','verification','ttl'].map(function(key){
    var on=key==='batch'?settings.execution_batch_gate:(key==='verification'?settings.execution_verification_gate:settings.execution_ttl_sweep);
    var label=key==='batch'?'批量撮合窗口':(key==='verification'?'事件人工核验':'执行时限清扫');
    return '<div class="exec-switch"><span>'+riskText(label)+'</span><span class="tag '+(on?'tag-ok':'')+'">'+(on?'开启':'关闭')+'</span></div>';
  }).join('');
  target.innerHTML='<section class="panel exec-panel"><div class="exec-head"><div><h3>执行器开关与批量窗口</h3>'
    +'<p>开关在「设置中心 → 执行」中调整；本页只读展示当前生效状态。</p></div>'
    +'<div class="exec-head-state">'+windowBadge+'<button class="ghost" onclick="loadPaperExecution(true)">刷新</button></div></div>'
    +'<div class="exec-switches">'+switches+'</div>'
    +(batchRows.length
      ? '<h4>批量等待队列（'+batchRows.length+'）</h4>'+execQueueTable(batchRows,'没有等待批量窗口的委托。',false)
      : '<h4>批量等待队列</h4><div class="paper-empty">没有等待批量窗口的委托。</div>')
    +'<h4>人工核验队列（'+verifyRows.length+'）</h4>'
    +execQueueTable(verifyRows,'核验闸门关闭或暂无待核验委托。放行后进入重试管道，驳回则终态作废。',true)
    +'</section>'
    +'<section class="panel exec-panel"><h3>各策略账户的执行画像映射</h3>'
    +'<p class="exec-hint">画像按账户 risk_profile 自动选择；未识别的画像 fail-closed 回落「保守组合」。</p>'
    +'<div class="table-scroll"><table class="exec-queue-table"><thead><tr>'
    +'<th>策略账户</th><th>账户 ID</th><th>风险画像</th><th>执行画像</th><th>订单类型</th><th>TTL</th><th>状态</th>'
    +'</tr></thead><tbody>'+accounts+'</tbody></table></div></section>'
    +'<section class="panel exec-panel"><h3>七档执行画像参数</h3>'
    +'<div class="table-scroll"><table class="exec-queue-table"><thead><tr>'
    +'<th>画像族</th><th>名称</th><th>紧急度</th><th>订单类型</th><th>限价让价</th><th>TTL</th><th>特性</th>'
    +'</tr></thead><tbody>'+catalog+'</tbody></table></div></section>';
}

export async function verifyExecutionOrder(orderId,approved){
  var verb=approved?'放行':'驳回';
  var note='';
  if(!approved){
    note=window.prompt('驳回原因（可选）：')||'';
    if(note===null) return;
  }
  if(!window.confirm('确认'+verb+'委托 #'+orderId+'？')) return;
  try{
    var query='?order_id='+Number(orderId)+'&approved='+(approved?1:0)+'&operator='+encodeURIComponent('运营台')+'&note='+encodeURIComponent(note);
    await apiPost('/api/paper/execution-dispatch/verify'+query);
    window._paperExecutionCenterCache=null;
    await loadPaperExecution();
  }catch(err){
    window.alert('核验失败：'+(err.message||err));
  }
}

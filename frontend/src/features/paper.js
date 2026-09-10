/* PR-55：由 frontend/app.js 拆分（纯搬运，逻辑/文案未改） */
// 跨模块依赖（由原单文件作用域推导）
import { api, apiPost } from "../core/api.js";
import { $, chart, tableScroll } from "../core/dom.js";
import { adaptiveEsc, cny, fmt, paperStatusTag, pctCls, pctTxt, riskText, zhRiskText } from "../core/format.js";
import { showPaperWorkspace } from "../core/navigation.js";
import { PAPER_NAV_TTL_MS } from "../core/state.js";
import { renderPaperAudit } from "./risk.js";
import { openInStrategyWorkbench, wbStatusBadge } from "./strategies.js";
import { toast, inlineError, confirmDialog } from "../ui/dialog.js";

/** PR-57：把原生 alert 统一换成应用内反馈；危险/失败给 danger 语义，其余 warn。 */
function paperNotice(message){
  var text=String(message==null?'':message);
  toast(text, { tone: /失败|错误|异常|拒绝|不可/.test(text) ? 'danger' : 'warn' });
}

export function renderPaperCompareChart(curve){
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
  // used to dominate the y-axis and made the five strategy curves hard to
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

export function syncPaperCapitalHint(){
  var capital=Number($('paperCapital').value)||0;
  $('paperCapitalHint').textContent='已启用策略共享总资金池；输入总金额 ¥'+capital.toLocaleString('zh-CN')+'，已启用策略只共享资金，不共享决策和风控规则。';
}

export async function startPaper(){
  var capital = Number($('paperCapital').value);
  if(!capital || capital<1000){ paperNotice('请先设置总模拟资金，至少 1,000 元。'); return; }
  var startAnswer=await confirmDialog({
    kicker:'模拟交易 · 启动新周期',
    title:'启动新周期？',
    detail:'总资金池 ¥'+capital.toLocaleString('zh-CN'),
    bullets:[
      '当前周期会被归档（历史不会删除）。',
      '各策略独立决策，共享现金与总仓位风控。',
      '不会连接券商、不会发送真实订单。',
    ],
    confirmText:'启动新周期',
  });
  if(!startAnswer.approved) return;
  $('paperStart').disabled = true;
  try{
    var d = await apiPost('/api/paper/start?capital='+encodeURIComponent(capital));
    var note = d.schedule && d.schedule.ok ? '新周期已启动，3分钟监控任务已注册。' : '新周期已启动；计划任务未完全安装时可运行 setup_paper_schedule.bat。';
    paperNotice(note);
    await loadPaper({force:true});
  }catch(e){ paperNotice('启用失败：'+e.message); }
  finally{ $('paperStart').disabled = false; }
}

export async function resumePaper(){
  try{ await apiPost('/api/paper/resume'); await loadPaper({force:true}); }
  catch(e){ paperNotice('恢复失败：'+e.message); }
}

export async function pausePaper(){
  try{ await apiPost('/api/paper/pause'); await loadPaper({force:true}); }
  catch(e){ paperNotice('暂停失败：'+e.message); }
}

export async function resetPaper(){
  var capital = Number($('paperCapital').value);
  if(!capital || capital<1000){ paperNotice('请填写新周期的总模拟资金。'); return; }
  var resetAnswer=await confirmDialog({
    kicker:'模拟交易 · 高风险操作',
    title:'完全重置并归档当前周期？',
    danger:true,
    bullets:[
      '当前周期的订单、持仓、盈亏、风控与周报会被归档。',
      '历史记录不会被删除，归档后仍可查阅。',
      '重置后会创建一个「暂停」状态的新周期，需要你手动恢复。',
    ],
    confirmText:'重置并归档',
  });
  if(!resetAnswer.approved) return;
  try{ await apiPost('/api/paper/reset?capital='+encodeURIComponent(capital)); await loadPaper({force:true}); }
  catch(e){ paperNotice('重置失败：'+e.message); }
}

export async function setPaperStyle(accountId, style){
  try{
    await apiPost('/api/paper/style?account_id='+encodeURIComponent(accountId)+'&style='+encodeURIComponent(style));
    await loadPaper({force:true});
  }catch(e){ paperNotice('风格切换失败：'+e.message); }
}

export async function runPaperNow(slot){
  try{
    $('paperStatus').textContent = '正在执行 '+slot+' 检查…';
    var d = await apiPost('/api/paper/run-now?slot='+encodeURIComponent(slot));
    $('paperStatus').textContent = d.status==='already_done' ? '本时段已执行，未重复下单。' : '检查完成。';
    await loadPaper({force:true});
  }catch(e){ $('paperStatus').textContent = '检查失败：'+e.message; }
}

export function setPaperOrderSide(side){
  window._paperOrderSide=side;
  $('paperSideBuy').className=side==='buy'?'active buy':'';
  $('paperSideSell').className=side==='sell'?'active sell':'';
  $('paperSubmitOrder').textContent=side==='buy'?'确认模拟买入':'确认模拟卖出';
  $('paperSubmitOrder').style.background=side==='buy'?'#e14f46':'#238064';
  clearPaperOrderPreview();
}

export function togglePaperLimitPrice(){
  $('paperLimitField').style.display=$('paperOrderType').value==='limit'?'block':'none';
  clearPaperOrderPreview();
}

export function clearPaperOrderPreview(){
  window._paperOrderPlan=null;
  var box=$('paperOrderPreview');
  if(!box) return;
  var submit=$('paperSubmitOrder');
  if(submit) submit.disabled=false;
  box.className='paper-order-preview';
  box.innerHTML='数量填 0 时按价格、止损距离、现金与风险预算计算建议数量。每次提交都会重新经过模型与 T+1 校验。';
}

export function paperPlanHasExecutableQty(plan){
  var qty=Number(plan&&plan.qty)||0;
  return qty>=100 && qty%100===0;
}

export function paperOrderForm(){
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

export function paperOrderQuery(form){
  var q='account_id='+encodeURIComponent(form.account_id)+'&code='+encodeURIComponent(form.code)+'&side='+form.side+'&qty='+form.qty+'&order_type='+form.order_type;
  if(form.order_type==='limit') q+='&limit_price='+encodeURIComponent(form.limit_price);
  return q;
}

export function renderPaperOrderPreview(plan){
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

export async function previewPaperOrder(){
  var form=paperOrderForm();
  if(!form.account_id||!/^\d{6}$/.test(form.code)){ paperNotice('请选择策略账户并输入六位证券代码。'); return null; }
  if(form.qty<0||form.qty%100){ paperNotice('数量必须为 0 或 100 股的整数倍。'); return null; }
  if(form.order_type==='limit'&&!form.limit_price){ paperNotice('请填写限价。'); return null; }
  $('paperOrderPreview').className='paper-order-preview';
  $('paperOrderPreview').textContent='模型正在检查行情时效、账户风险、仓位、T+1 和交易成本…';
  try{
    var plan=await api('/api/paper/order-preview?'+paperOrderQuery(form));
    window._paperOrderPlan=plan; renderPaperOrderPreview(plan); return plan;
  }catch(e){ window._paperOrderPlan=null; $('paperOrderPreview').className='paper-order-preview block'; $('paperOrderPreview').textContent='预检失败：'+e.message; return null; }
}

export async function submitPaperOrder(){
  // 二次确认链路的防重保护：submitPaperOrder 是 /order/submit 的唯一入口，
  // 但旧实现在"预检请求 → confirm 弹窗"期间按钮仍可点击。预检要跨一次
  // 网络往返，快速双击会叠加两个 confirm 弹窗，若都确认就会提交两笔委托。
  // 现在入口处立即置 in-flight 守卫并禁用按钮，finally 里按预检终态恢复。
  if(window._paperOrderSubmitting) return;
  window._paperOrderSubmitting=true;
  var submit=$('paperSubmitOrder');
  var wasEnabled=!!submit&&!submit.disabled;
  if(wasEnabled) submit.disabled=true;
  try{
    var form=paperOrderForm(), plan=await previewPaperOrder();
    if(!plan||!plan.allowed) return;
    if(!paperPlanHasExecutableQty(plan)){
      renderPaperOrderPreview(Object.assign({},plan,{allowed:true,reasons:(plan.reasons||[]).concat(['当前模型可执行数量为 0 股，释放席位或等待下一轮扫描后再提交'])}));
      return;
    }
    var action=form.side==='buy'?'买入':'卖出';
    var state=plan.triggered?'立即按快照模拟成交':'进入当日限价委托';
    var orderAnswer=await confirmDialog({
    kicker:'模拟交易 · 手工下单',
    title:action+' '+plan.name,
    detail:'数量 '+plan.qty+' 股',
    bullets:['这是纯本地模拟，不会发送到券商，继续吗？','提交前会重新经过模型校验、T+1 与涨跌停门禁。'],
    confirmText:'提交模拟委托',
  });
  if(!orderAnswer.approved) return;
    try{
      var result=await apiPost('/api/paper/order/submit?'+paperOrderQuery(form)+'&confirmed=true');
      paperNotice(result.status==='filled'?'模拟成交已写入账本。':(result.status==='pending_limit'?'限价委托已进入待触发队列。':'委托被模型拒绝。'));
      clearPaperOrderPreview(); await loadPaper({force:true});
    }catch(e){ paperNotice('模拟委托失败：'+e.message); }
  }finally{
    window._paperOrderSubmitting=false;
    // 恢复按钮时沿用 renderPaperOrderPreview 的语义：阻断态预检保持禁用，
    // 成功/失败/取消（clearPaperOrderPreview 已清空 plan）恢复可用。
    if(wasEnabled&&submit){
      var endPlan=window._paperOrderPlan;
      submit.disabled=!!(endPlan&&!(endPlan.allowed&&paperPlanHasExecutableQty(endPlan)));
    }
  }
}

export function preparePaperSell(accountId,code,qty){
  $('paperOrderAccount').value=accountId; $('paperOrderCode').value=code; $('paperOrderQty').value=qty||0;
  $('paperOrderType').value='market'; togglePaperLimitPrice(); setPaperOrderSide('sell');
  $('paperOrderTicket').scrollIntoView({behavior:'smooth',block:'start'});
}

export async function cancelPaperOrder(orderId){
  var cancelAnswer=await confirmDialog({
    kicker:'模拟交易 · 撤单',
    title:'撤销这笔模拟委托？',
    bullets:['撤单只影响模拟盘，不涉及真实券商。','已成交部分不会被回滚。'],
    confirmText:'确认撤单',
  });
  if(!cancelAnswer.approved) return;
  try{ await apiPost('/api/paper/order/cancel?order_id='+encodeURIComponent(orderId)); await loadPaper({force:true}); }
  catch(e){ paperNotice('撤单失败：'+e.message); }
}

export function paperOrderStatusView(status,reason){
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

export function setPaperTerminalFilter(kind,value){
  if(kind==='position') window._paperPositionFilter=value;
  if(kind==='positionState') window._paperPositionStateFilter=value;
  if(kind==='orderAccount') window._paperOrderAccountFilter=value;
  if(kind==='orderDate') window._paperOrderDateFilter=value;
  if(kind==='orderSide') window._paperOrderSideFilter=value;
  if(kind==='orderStatus') window._paperOrderStatusFilter=value;
  filterPaperTerminal();
}

export function clearPaperOrderDate(){
  window._paperOrderDateFilter='';
  if($('paperOrderDateFilter')) $('paperOrderDateFilter').value='';
  filterPaperTerminal();
}

export function filterPaperTerminal(){
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

/* ================= PR-52：模拟交易 → 运行策略（只读运行时面板） =================
   策略定义的唯一编辑入口是主导航「策略工坊」（DSL / 版本 / 生命周期）。
   本页只回答「当前周期里每个策略跑得怎么样」：参与、额度、阶段、席位占用、
   等待原因。这里没有 DSL 编辑、没有版本保存、没有第二套 clone、没有第二套
   生命周期编辑——需要改定义就跳转到策略工坊。 */
export var PAPER_STAGE_LABELS={shadow:'影子（不部署）',pilot:'试点',standard:'标准',mature:'成熟',quarantined:'隔离（不部署）'};

export async function loadPaperStrategyCenter(){
  var target=$('paperStrategyView');
  if(!target) return;
  var cached=window._paperStrategyCenterCache;
  if(cached){
    renderPaperStrategyCenter(cached.data);
    if(Date.now()-cached.at<PAPER_NAV_TTL_MS) return;   // 缓存新鲜：零请求零重绘
  }else{
    target.innerHTML='<div class="loading">正在读取运行策略状态…</div>';
  }
  try{
    // 三个只读来源各司其职：分配解释=运行时参与/额度/阶段/等待原因；
    // 注册表=规范身份与不可变版本；strategy-center=内置策略的风险边界摘要。
    var results=await Promise.all([
      api('/api/paper/allocation-explain'),
      api('/api/strategies?include_archived=true&_='+Date.now()),
      api('/api/paper/strategy-center').catch(function(){ return null; })
    ]);
    var d={allocation:results[0]||{},registry:results[1]||{},boundaries:results[2]||{}};
    window._paperStrategyCenterCache={data:d,at:Date.now()};
    renderPaperStrategyCenter(d);
  }catch(e){ if(!cached) target.innerHTML='<div class="banner">运行策略读取失败：'+riskText(e.message||e)+'</div>'; }
}

export function paperRunningStageLabel(row){
  var stage=row&&row.capital_scale&&row.capital_scale.lifecycle_stage;
  if(!stage) return '—';
  return PAPER_STAGE_LABELS[stage]||stage;
}

export function paperRunningStrategyCard(id,item,runtime,boundary){
  item=item||{}; runtime=runtime||{};
  var name=item.name||runtime.name||id;
  var origin=item.origin==='builtin'?'内置':'自定义';
  var originClass=item.origin==='builtin'?'strategy-card-origin-builtin':'strategy-card-origin-user';
  var badge=(typeof wbStatusBadge==='function')?wbStatusBadge(item.status):riskText(item.status||'—');
  var version=(item.current_version===null||item.current_version===undefined)?'—':item.current_version;
  var checksum=String(item.current_checksum||'').slice(0,8);
  var scale=(runtime.capital_scale&&runtime.capital_scale.factor!==null&&runtime.capital_scale.factor!==undefined)
    ?Math.round(runtime.capital_scale.factor*100)+'%':'—';
  var target=(runtime.target_budget&&runtime.target_budget.target_amount!==null&&runtime.target_budget.target_amount!==undefined)
    ?cny(runtime.target_budget.target_amount):'—';
  var deployable=(runtime.deployment&&runtime.deployment.deployable_amount!==null&&runtime.deployment.deployable_amount!==undefined)
    ?cny(runtime.deployment.deployable_amount):'—';
  var waiting=(runtime.deployment&&runtime.deployment.waiting_capital!==null&&runtime.deployment.waiting_capital!==undefined)
    ?cny(runtime.deployment.waiting_capital):'—';
  var blocked=(runtime.deployment&&runtime.deployment.blocked_reason)
    ||(runtime.waiting_reason&&runtime.waiting_reason.reason)
    ||(runtime.running?'正常运行，无阻塞':'未参与当前周期');
  var limits='';
  if(boundary){
    limits='<div class="paper-strategy-section"><label>风险 / 执行边界（摘要）</label><div class="paper-strategy-metrics">'
      +'<div>持仓周期<b>'+riskText(boundary.hold_range||'—')+'</b></div>'
      +'<div>单股 / 总池预算<b>'+fmt(boundary.max_weight_pct,1)+'% / '+fmt(boundary.pool_budget_pct,2)+'%</b></div>'
      +'<div>保底 / 风险画像<b>'+fmt(boundary.pool_floor_pct,2)+'% / '+fmt(boundary.max_exposure_pct,0)+'%</b></div>'
      +'<div>单日亏损 / 回撤<b>'+fmt(boundary.daily_loss_pct,1)+'% / '+fmt(boundary.drawdown_pct,1)+'%</b></div>'
      +'<div>行业 / 冷却<b>'+fmt(boundary.industry_limit_pct,0)+'% / '+riskText(boundary.cooldown_days)+'天</b></div>'
      +'<div>入场模型<b>'+riskText(boundary.entry_model||'—')+'</b></div>'
      +'</div></div>';
  }
  return '<article class="paper-strategy-card" data-testid="paper-runtime-card-'+adaptiveEsc(id)+'" data-strategy-id="'+adaptiveEsc(id)+'" data-runtime-only="1">'
    +'<header><b>'+riskText(name)+'</b><span>'+badge+' <span class="strategy-card-origin '+originClass+'">'+origin+'</span></span></header>'
    +'<div class="paper-strategy-section"><label>当前周期运行时</label><div class="paper-strategy-metrics">'
    +'<div>参与本周期<b>'+(runtime.running?'是':'否')+'</b></div>'
    +'<div>生命周期阶段<b>'+riskText(paperRunningStageLabel(runtime))+'</b></div>'
    +'<div>资金系数<b>'+scale+'</b></div>'
    +'<div>持仓席位<b>'+fmt(runtime.position_count,0)+' / '+fmt(runtime.position_limit,0)+'</b></div>'
    +'<div>目标额度<b>'+target+'</b></div>'
    +'<div>可部署<b>'+deployable+'</b></div>'
    +'<div>未部署余额<b>'+waiting+'</b></div>'
    +'</div></div>'
    +limits
    +'<div class="paper-strategy-section"><label>不可变版本</label><p>v'+riskText(version)
    +(checksum?(' · '+riskText(checksum)):'')+(item.has_dsl?' · DSL':' · 原生')+'</p></div>'
    +'<div class="paper-strategy-section"><label>等待 / 阻塞原因</label><p>'+riskText(blocked)+'</p></div>'
    +'<div class="strategy-builder-toolbar"><button type="button" data-testid="paper-open-workbench-'+adaptiveEsc(id)+'" onclick="openInStrategyWorkbench(\''+adaptiveEsc(id)+'\')">在策略工坊打开</button></div>'
    +'</article>';
}

export function renderPaperStrategyCenter(d){
  var target=$('paperStrategyView'); if(!target) return;
  d=d||{};
  var registry={}; (((d.registry||{}).items)||[]).forEach(function(item){ registry[item.id]=item; });
  var boundaries={}; ((((d.boundaries||{}).strategies)||[])).forEach(function(row){ boundaries[row.id]=row; });
  var runtime={}; (((d.allocation||{}).strategies)||[]).forEach(function(row){ runtime[row.strategy_id]=row; });
  var ids=Object.keys(runtime);
  Object.keys(registry).forEach(function(id){ if(ids.indexOf(id)<0) ids.push(id); });
  var cards=ids.map(function(id){ return paperRunningStrategyCard(id,registry[id],runtime[id],boundaries[id]); }).join('');
  if(!cards) cards='<div class="paper-empty">还没有任何已注册策略。</div>';
  var allocation=d.allocation||{}, plan=allocation.allocation_plan||{};
  var headline=[];
  if(allocation.nav!==null&&allocation.nav!==undefined) headline.push('总资金池净值 '+cny(allocation.nav));
  if(allocation.pool_limit!==null&&allocation.pool_limit!==undefined) headline.push('池硬上限 '+fmt(allocation.pool_limit,0)+' 席');
  if(allocation.market_light) headline.push('市场灯 '+riskText(allocation.market_light));
  if(plan.total_deployable_amount!==null&&plan.total_deployable_amount!==undefined) headline.push('本轮可部署 '+cny(plan.total_deployable_amount));
  var guards=(((d.boundaries||{}).shared_guards)||[]).map(function(item){return '<li>'+riskText(item)+'</li>';}).join('');
  target.innerHTML='<section class="paper-strategy-intro"><div><h3>运行策略</h3>'
    +'<p>本页只读展示当前周期里各策略的运行状态：参与情况、资金额度、生命周期阶段、席位占用与等待原因。策略定义、DSL、版本与生命周期统一在主导航「策略工坊」维护，本页不提供任何修改入口。</p>'
    +'<div class="strategy-builder-toolbar"><button type="button" onclick="openInStrategyWorkbench()">打开策略工坊</button>'
    +'<span class="strategy-builder-hint">'+(headline.join(' · ')||'—')+'</span></div></div></section>'
    +'<section class="paper-strategy-grid">'+cards+'</section>'
    +(guards?('<section class="paper-strategy-guards"><b>共同执行边界</b><ul>'+guards+'</ul></section>'):'');
}

export function paperResearchStrategyName(id){
  return ({tq_breakout:'短线日内做T',trend_pullback:'趋势波段优选',sector_rotation:'板块轮动先锋',reported_profit_breakout:'三日策略',main_force_top10:'超强主力股'})[id]||id||'未知策略';
}

export function paperResearchOutcome(metric,horizon){
  if(!metric) return '<span class="paper-research-empty">等待 '+horizon+' 日观察</span>';
  return '<b class="'+pctCls(metric.avg_return_pct)+'">'+pctTxt(metric.avg_return_pct)+'</b><small>'+Number(metric.samples||0)+' 个样本 · 胜率 '+fmt(metric.win_rate_pct,1)+'%</small>';
}

export function paperResearchQuality(run){
  var q=(run&&run.data_quality)||{}, usable=Number(q.universe_size||0), stale=Number(q.dropped_stale_rows||0);
  if(!run) return {tone:'empty',label:'尚未记录',detail:'下一次盘后候选生成后自动写入'};
  if(!usable) return {tone:'caution',label:'数据不足',detail:'未形成可核验的因子范围'};
  if(stale>usable) return {tone:'caution',label:'覆盖待补',detail:'可用因子 '+usable+' · 过期剔除 '+stale};
  return {tone:'ready',label:'已记录',detail:'可用因子 '+usable+' · 过期剔除 '+stale};
}

export async function loadPaperResearchValidation(){
  var target=$('paperResearchView');
  if(!target) return;
  var cached=window._paperResearchCache;
  if(cached){
    renderPaperResearchValidation(cached.data);
    if(Date.now()-cached.at<PAPER_NAV_TTL_MS) return;   // 缓存新鲜：零请求零重绘
  }else{
    target.innerHTML='<div class="loading">正在读取模拟盘策略的候选快照与兑现记录…</div>';
  }
  try{
    var d=await api('/api/paper/research-validation?limit=90');
    window._paperResearchCache={data:d,at:Date.now()};
    renderPaperResearchValidation(d);
  }catch(e){ if(!cached) target.innerHTML='<div class="banner">策略证据读取失败：'+riskText(e.message||e)+'</div>'; }
}

export function renderPaperResearchValidation(d){
  var target=$('paperResearchView'); if(!target) return;
  var latestByStrategy={}, policy=d.backfill_policy||{};
    (d.runs||[]).forEach(function(run){ if(!latestByStrategy[run.account_id]) latestByStrategy[run.account_id]=run; });
    var ids=['tq_breakout','trend_pullback','sector_rotation','reported_profit_breakout','main_force_top10'];
    var cards=ids.map(function(id){
      var run=latestByStrategy[id], quality=paperResearchQuality(run), metrics=(d.metrics||{})[id]||{};
      var horizons=[1,3,5].map(function(h){return '<div class="paper-research-outcome"><span>'+h+'日</span>'+paperResearchOutcome(metrics[String(h)],h)+'</div>';}).join('');
      var q=(run&&run.data_quality)||{};
      var shortCode={tq_breakout:'T',trend_pullback:'趋',sector_rotation:'板',reported_profit_breakout:'盈',main_force_top10:'主'}[id]||'证';
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
    target.innerHTML='<section class="paper-research-hero"><div><span class="page-kicker">SHADOW EVIDENCE · PAPER ONLY</span><h3>策略集合研究证据</h3><p>每个交易日收盘后固定候选、评分构成与可用数据范围，再跟踪后续表现。它不下单、不调参，也不会改动风控。</p></div><div class="paper-research-hero-actions"><span class="tag tag-info">已记录 '+runCount+' 份策略快照</span><span class="paper-research-schedule">自动：'+riskText(policy.scheduled_at||'每个交易日收盘后')+'<small>'+riskText(policy.next_observation||'后续有效收盘快照会补齐观察')+'</small></span><button class="ghost" type="button" onclick="refreshPaperResearchValidation(this)">刷新记录</button><button class="ghost paper-research-backfill" type="button" title="'+riskText(policy.manual_scope||'')+'" onclick="backfillPaperResearch(this)" '+(manualAllowed?'':'disabled')+'>'+riskText(manualLabel)+'</button><span id="paperResearchActionStatus" class="paper-research-action-status" role="status" aria-live="polite"></span></div></section>'
      +'<section class="paper-research-ladder" aria-label="研究兑现周期"><span>候选固定</span><i></i><span>1日观察</span><i></i><span>3日复核</span><i></i><span>5日对比</span><i></i><span>10日验证</span><i></i><span>20日人工复核</span></section>'
      +'<section class="paper-research-grid">'+cards+'</section>'
      +'<section class="paper-research-table"><header><div><h3>最新可核验快照</h3><p>只有收盘后写入的候选才会计入研究；数据不完整会明确标记，不会伪装成有效样本。</p></div><span class="tag tag-warn">影子验证中</span></header>'+tableScroll('<table><thead><tr><th>模拟盘策略</th><th>信号日</th><th>候选</th><th>因子截至</th><th>最早因子</th><th>数据质量</th></tr></thead><tbody>'+rows+'</tbody></table>',900)+'</section>'
      +'<p class="paper-research-note">当前为第一批样本。'+riskText(policy.next_observation||'1 日、3 日、5 日结果会在后续有效收盘快照到达后自动补齐')+'。手动补录只允许使用当日完整收盘快照，不能拿今天数据回写旧候选；样本不足 20 个时，系统只显示积累状态，不允许据此自动修改任何策略。</p>';
}

export async function refreshPaperResearchValidation(button){
  if(button){button.disabled=true;button.textContent='正在刷新…';}
  try{ await loadPaperResearchValidation(); }
  finally{ if(button){button.disabled=false;button.textContent='刷新记录';} }
}

export function setPaperResearchActionStatus(message,tone){
  var target=$('paperResearchActionStatus'); if(!target) return;
  target.textContent=message||'';
  target.className='paper-research-action-status '+(tone||'');
}

export async function backfillPaperResearch(button){
  var snapAnswer=await confirmDialog({
    kicker:'模拟交易 · 手动执行',
    title:'补录当日收盘快照？',
    bullets:['只补录当日完整收盘快照与既有样本的当日观察。','不会生成买卖信号、委托或风控决策。'],
    confirmText:'开始补录',
  });
  if(!snapAnswer.approved) return;
  if(button){button.disabled=true;button.textContent='补录中…';}
  try{
    var result=await apiPost('/api/paper/research-validation/backfill');
    window._paperResearchCache=null;   // 补录刚写入了新样本，绕过缓存强制刷新
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

export function selectPaperHistoryQuick(code){ if(code){ $('paperHistoryCode').value=code; loadPaperStockHistory(); } }

export function showPaperStockHistory(code){ var tab=document.querySelector('#p-paper [data-paper-view="history"]'); showPaperWorkspace('history',tab); $('paperHistoryCode').value=code; loadPaperStockHistory(); }

export async function loadPaperStockHistory(){
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

export function filterPaperHistoryRows(){
  var side=$('paperHistorySide')?$('paperHistorySide').value:'all', status=$('paperHistoryStatus')?$('paperHistoryStatus').value:'all', date=$('paperHistoryDate')?$('paperHistoryDate').value:'';
  var visible=0; document.querySelectorAll('.paper-history-order-row').forEach(function(row){ var match=(side==='all'||row.dataset.side===side)&&(status==='all'||row.dataset.status===status)&&(!date||row.dataset.date===date); row.hidden=!match; if(match) visible++; });
  if($('paperHistoryVisible')) $('paperHistoryVisible').textContent=visible+' \u7b14\u660e\u7ec6';
}

export function syncCycleControls(cycle, accounts){
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

export function paperAccountDisplayName(account){
  var id=account&&account.id;
  return ({tq_breakout:'短线日内做T',trend_pullback:'趋势波段优选',sector_rotation:'板块轮动先锋',reported_profit_breakout:'三日策略',main_force_top10:'超强主力股'})[id] || (account&&account.name) || id || '未知策略';
}

export function paperOverviewVariant(){
  return window._paperWorkspace==='activity'?'activity'
    :(window._paperWorkspace==='history'?'history':'portfolio');
}

export function paperOverviewSignature(d){
  if(!d||typeof d!=='object') return 'null';
  var s=d.shared||{},c=d.cycle||{},curve=d.equity_curve||{},mon=(d.monitor_runs||[])[0]||{};
  return [s.nav,s.cash,s.position_count,s.dynamic_position_slots_used,
    (d.orders||[]).length,(d.positions||[]).length,(d.signals||[]).length,
    (d.observations||[]).length,(d.risk_decisions||[]).length,
    (d.history_symbols||[]).length,(d.parameter_versions||[]).length,
    c.cycle_key,(curve.dates||[]).length,mon.status].join('|');
}

export async function loadPaper(options){
  options=options||{};
  var variant=paperOverviewVariant();
  var cache=window._paperOverviewCache;
  if(!options.refresh&&!options.force&&cache&&cache.variant===variant&&!window._paperLoadRequest){
    renderPaperDashboard(cache.data,null);
    if(Date.now()-cache.at<PAPER_NAV_TTL_MS) return;   // 缓存仍新鲜：零网络、零重绘
    window._paperLoadRequest=(async function(){        // 已过期：旧数据先顶着，后台刷新
      try{
        var fresh=await api('/api/paper/overview'
          +(variant==='activity'?'?activity=1':variant==='history'?'?history_symbols=1':''));
        if(paperOverviewSignature(fresh)!==paperOverviewSignature(cache.data)){
          window._paperOverviewCache={variant:variant,data:fresh,at:Date.now()};
          renderPaperDashboard(fresh,null);
        }else window._paperOverviewCache.at=Date.now();
      }catch(ignore){/* 静默刷新失败时保留当前画面，不打断浏览 */}
      finally{ window._paperLoadRequest=null; }
    })();
    return window._paperLoadRequest;
  }
  // Navigation, the one-minute refresh, and manual actions may all request an
  // overview at the same time.  Let every caller share one in-flight request
  // instead of rendering the large dashboard repeatedly in parallel.
  if(window._paperLoadRequest) return window._paperLoadRequest;
  window._paperLoadRequest=(async function(){
  try{
    // The activity audit is independent from the account overview.  Start it
    // immediately so it can read in parallel with the larger dashboard DOM
    // render instead of extending every browser refresh serially.
    var auditRequest = variant==='activity'
      ? api('/api/paper/risk-audit?limit=160')
      : null;
    var overviewQuery=[];
    if(options.refresh) overviewQuery.push('refresh=1');
    if(variant==='activity') overviewQuery.push('activity=1');
    if(variant==='history') overviewQuery.push('history_symbols=1');
    var d = await api('/api/paper/overview'+(overviewQuery.length?'?'+overviewQuery.join('&'):''));
    window._paperOverviewCache={variant:variant,data:d,at:Date.now()};
    renderPaperDashboard(d,auditRequest);
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

export async function renderPaperDashboard(d,auditRequest){
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
    // 组合视图曾用 signals 拼候选卡与重合度摘要，页面改版后这些 HTML
    // 已无消费点，但每次 loadPaper 仍对 120 条 signals 做字符串拼接。
    // 死代码已删除，activity 工作区不再为隐藏 DOM 付费。
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
        var auditDashboard=auditRequest||window._paperAuditCache;
        if(!auditDashboard){
          auditDashboard=await api('/api/paper/risk-audit?limit=160');
          window._paperAuditCache=auditDashboard;
        }
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
      ? '<span class="tag tag-ok">'+running+' / '+(accounts.length||5)+' 策略运行中</span> 周期 '+(cycle.cycle_key||'-')+'；每3分钟观察，满足全部条件才交易。'
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
      : '<div class="paper-empty">暂无信号。已启用策略会按各自模型、行情时间戳和仓位上限分别审批。</div>';
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
}

/* PR-55：由 frontend/app.js 拆分（纯搬运，逻辑/文案未改） */
// 跨模块依赖（由原单文件作用域推导）
import { api, apiJson, apiPostJson } from "../core/api.js";
import { $ } from "../core/dom.js";
import { adaptiveEsc } from "../core/format.js";
import { activatePage } from "../core/navigation.js";
import { STRATEGY_STATUS_LABELS, strategyStatusBadge, strategyStatusLabelTech } from "../core/strategy_labels.js";
import { toast, inlineError, confirmDialog, promptDialog } from "../ui/dialog.js";

// 生命周期状态中文名：设置中心的「启用策略」分组也在用（PR-52 从已删除的旧构建器里
// 提升为模块级共享常量，避免设置页与运行策略页各写一份）。
// PR-57：状态标签的唯一来源在 core/strategy_labels.js（上面已显式 import），
// 这里只做兼容再导出，避免 Strategy / Settings 各维护一份 map。
export { STRATEGY_STATUS_LABELS };

export function openInStrategyWorkbench(strategyId){
  // PR-52：本页唯一的"去改定义"出口——跳到策略工坊；带 id 时直接打开该策略详情。
  activatePage('p-strategies');
  if(strategyId&&typeof wbOpenDetail==='function'){ wbOpenDetail(strategyId); return; }
  if(typeof wbShowView==='function') wbShowView('list');
}

// Legacy adaptive/paper fragments can still arrive from a cached API payload.
// Normalize only the visible workspace copy so a stale cached payload cannot
// reintroduce the retired two/three/four-strategy wording. Historical replay
// data stays untouched in the backend.
export function normalizeActiveStrategyCopy(root){
  if(!root) return;
  var walker=document.createTreeWalker(root,NodeFilter.SHOW_TEXT), node;
  while((node=walker.nextNode())){
    var value=node.nodeValue||'';
    var normalized=value.replace(/(二|两|三|四|五)套(当前)?(模拟账户|策略)/g,'已启用策略').replace(/五策略/g,'已启用策略');
    if(normalized!==value) node.nodeValue=normalized;
  }
}

/* ================= PR-46：策略工坊（Strategy Workbench） =================
   Registry 驱动的策略生命周期工作台：列表 → 新建/编辑（条件构建器 + 高级
   DSL）→ 验证 → 风险与资金预览 → 保存草稿 → 激活 → 版本/事件 → 克隆/归档。
   规则全部来自后端（strategy_registry / DSL compiler），前端只做 UX。 */
export var WB_DSL_FIELDS=['close','open','high','low','volume','amount','pe','pb','roe','revenue_yoy','profit_yoy','gross_margin','debt_ratio','main_net_inflow','main_net_inflow_pct','northbound_net_inflow','turnover_rate'];

export var WB_DSL_INDICATORS=['ma','ema','rsi','atr','volume_mean'];

export var WB_DSL_COMPARISONS=[['gt','高于'],['gte','不低于'],['lt','低于'],['lte','不高于']];

export var WB_FIELD_LABELS={close:'收盘价',open:'开盘价',high:'最高价',low:'最低价',volume:'成交量',amount:'成交额',pe:'市盈率',pb:'市净率',roe:'ROE',revenue_yoy:'营收同比',profit_yoy:'利润同比',gross_margin:'毛利率',debt_ratio:'负债率',main_net_inflow:'主力净流入',main_net_inflow_pct:'主力净流入%',northbound_net_inflow:'北向净流入',turnover_rate:'换手率'};

export var WB_INDICATOR_LABELS={ma:'均线 MA',ema:'指数均线 EMA',rsi:'RSI',atr:'ATR',volume_mean:'均量'};

export var WB_STATUS_BADGES={draft:['DRAFT','strategy-status-draft'],validated:['VALIDATED','strategy-status-validated'],active:['ACTIVE','strategy-status-active'],paused:['PAUSED','strategy-status-paused'],retiring:['RETIRING','strategy-status-retiring'],archived:['ARCHIVED','strategy-status-archived']};

export var WB_STATE={items:[],summary:null,originFilter:'all',statusFilter:'all',view:'list',editingId:null,editingVersion:null,editorMode:'builder'};

export async function loadStrategyWorkbench(force){
  var target=$('wbList'); if(!target) return;
  if(window._wbLoading&&!force) return window._wbLoading;
  window._wbLoading=(async function(){
    try{
      var data=await api('/api/strategies?include_archived=true&_='+Date.now());
      WB_STATE.items=data.items||[]; WB_STATE.summary=data.summary||{};
      // 下一周期是否参与由设置里的 enabled_strategies 决定，不能只看生命周期资格。
      // 读不到配置时退化为"符合条件"措辞，绝不宣称已启用。
      try{
        var cfg=await api('/api/settings/?_='+Date.now());
        var sim=(cfg&&cfg.settings&&cfg.settings.simulation)||{};
        WB_STATE.enabledIds=Array.isArray(sim.enabled_strategies)?sim.enabled_strategies.slice():null;
      }catch(e){ WB_STATE.enabledIds=null; }
      wbRenderSummary(); wbRenderList();
      // 路由意图必须在 registry 就绪之后应用（详情渲染依赖 items/接口）。
      var routeId=window._strategiesRouteId;
      if(routeId){
        window._strategiesRouteId=null;
        wbOpenDetail(routeId);
      }
      return data;
    }catch(e){
      if(target) target.innerHTML='<div class="strategy-workbench-error"><b>无法加载策略</b><span>'+adaptiveEsc(e.message||e)+'</span><button type="button" onclick="loadStrategyWorkbench(true)">重试</button></div>';
      throw e;
    }finally{ window._wbLoading=null; }
  })();
  return window._wbLoading;
}

export function wbRenderSummary(){
  var s=WB_STATE.summary||{}; var box=$('wbSummary');
  if(box) box.innerHTML='<b>'+(s.total||0)+'</b> 个策略 <span>·</span> '+(s.active||0)+' Active <span>·</span> '+(s.draft||0)+' Draft <span>·</span> '+(s.builtin||0)+' 内置 <span>·</span> '+(s.user||0)+' 自定义';
}

export function wbSetOriginFilter(value,btn){
  WB_STATE.originFilter=value;
  document.querySelectorAll('[data-wb-origin]').forEach(function(x){x.classList.toggle('active',x.getAttribute('data-wb-origin')===value);});
  wbRenderList();
}

export function wbSetStatusFilter(value,btn){
  WB_STATE.statusFilter=value;
  document.querySelectorAll('[data-wb-status]').forEach(function(x){x.classList.toggle('active',x.getAttribute('data-wb-status')===value);});
  wbRenderList();
}

export function wbStatusBadge(status){
  // PR-57：主文案是中文生命周期标签 + 非颜色信号（符号），技术枚举放 title。
  return strategyStatusBadge(status);
}

export function wbMatches(item){
  if(WB_STATE.originFilter!=='all'&&item.origin!==WB_STATE.originFilter) return false;
  if(WB_STATE.statusFilter!=='all'&&item.status!==WB_STATE.statusFilter) return false;
  var q=($('wbSearch')&&$('wbSearch').value||'').trim().toLowerCase();
  if(q&&(item.id||'').toLowerCase().indexOf(q)<0&&(item.name||'').toLowerCase().indexOf(q)<0) return false;
  return true;
}

export function wbCard(item){
  var userCard=item.origin==='user';
  var meta=item.metadata||{};
  var style=adaptiveEsc(meta.style||'—');
  var hold=meta.hold?meta.hold+' 日':'—';
  var actions='';
  if(userCard){
    var sid='\''+adaptiveEsc(item.id)+'\'';
    var st=item.status;
    // PR-49：按注册表合法迁移矩阵（draft→validated/archived、
    // validated→draft/active/archived、active→paused/retiring、
    // paused→active/retiring/archived、retiring→archived）补齐按钮，
    // 让 Pause / Clone / Retire 在界面上真正闭环，不必再手搓 curl。
    if(st==='draft'||st==='validated') actions+='<button type="button" data-testid="strategy-edit" onclick="wbOpenEditor('+sid+')">编辑</button>';
    actions+='<button type="button" data-testid="strategy-clone" onclick="wbCloneStrategy('+sid+')">'+(st==='archived'?'复制并编辑':'复制')+'</button>';
    if(st==='draft') actions+='<button type="button" data-testid="strategy-transition-validated" onclick="wbValidateAndMark('+sid+')">验证并标记可激活</button>';
    if(st==='validated') actions+='<button type="button" data-testid="strategy-transition-draft" onclick="wbTransition('+sid+',\'draft\')">退回草稿</button>';
    if(st==='validated') actions+='<button type="button" class="strategy-workbench-primary" data-testid="strategy-transition-active" onclick="wbTransition('+sid+',\'active\')">激活策略</button>';
    if(st==='active') actions+='<button type="button" data-testid="strategy-transition-paused" onclick="wbTransition('+sid+',\'paused\')">暂停</button>';
    if(st==='paused') actions+='<button type="button" data-testid="strategy-transition-resume" onclick="wbTransition('+sid+',\'active\')">恢复</button>';
    if(st==='active'||st==='paused') actions+='<button type="button" data-testid="strategy-transition-retiring" onclick="wbTransition('+sid+',\'retiring\')">退役</button>';
    if(st==='retiring') actions+='<button type="button" data-testid="strategy-transition-archived" onclick="wbTransition('+sid+',\'archived\')">完成归档</button>';
    if(st==='draft'||st==='validated'||st==='paused') actions+='<button type="button" data-testid="strategy-transition-archive" onclick="wbTransition('+sid+',\'archived\')">归档</button>';
    if(st==='draft') actions+='<button type="button" class="strategy-card-danger" onclick="wbDeleteDraft('+sid+')">删除草稿</button>';
  }else{
    actions='<button type="button" data-testid="strategy-clone" onclick="wbCloneStrategy(\''+adaptiveEsc(item.id)+'\')">复制并编辑</button>';
  }
  actions+='<button type="button" data-testid="strategy-open-detail" onclick="wbOpenDetail(\''+adaptiveEsc(item.id)+'\')">版本与详情</button>';
  // PR-57：卡片分两层。主视觉 = 用户判断"这是什么、现在什么状态、能不能参与"所需的信息；
  // 技术标识（id/校验和/实现键/原始指纹）收进 <details> 披露区，降低默认认知负荷。
  var runtime=item.runtime||{};
  var riskLabel=item.risk_archetype||item.fingerprint_archetype||(meta.style?style:'')||'—';
  var stageLabel=runtime.stage_label||runtime.lifecycle_stage||'—';
  var scale=null;
  if(runtime.capital_scale&&runtime.capital_scale.factor!=null) scale=Math.round(runtime.capital_scale.factor*100)+'%';
  else if(runtime.capital_scale!=null&&typeof runtime.capital_scale==='number') scale=Math.round(runtime.capital_scale*100)+'%';
  var seats=runtime.position_limit!=null?runtime.position_limit:(meta.max_positions!=null?meta.max_positions:null);
  var eligible=!!item.supports_new_cycle&&item.status==='active';
  var enabledIds=WB_STATE.enabledIds;
  var participates;
  var participation;
  if(eligible&&Array.isArray(enabledIds)){
    participates=enabledIds.indexOf(item.id)>=0;
    participation=participates?'下一周期：已启用':'下一周期：未勾选';
  }else if(eligible){
    participates=false;
    participation='下一周期：符合条件（以设置为准）';
  }else{
    participates=false;
    participation=item.status==='active'?'下一周期：不支持':'下一周期：不可参与';
  }
  var checksum=String(item.current_checksum||item.checksum||'');
  var dslChecksum=String(item.dsl_checksum||item.dsl_checksum_sha256||'');
  var implKey=item.implementation_key||item.implemented_by||item.native_key||(item.has_dsl?'dsl':'native');
  var rawFingerprint=item.risk_fingerprint?JSON.stringify(item.risk_fingerprint):(item.fingerprint?JSON.stringify(item.fingerprint):'');
  return '<article class="strategy-card" data-testid="strategy-card-'+adaptiveEsc(item.id)+'" data-strategy-id="'+adaptiveEsc(item.id)+'" data-status="'+adaptiveEsc(item.status||'')+'">'
    +'<header><h3>'+adaptiveEsc(item.name||item.id)+'</h3>'+wbStatusBadge(item.status)+'</header>'
    +'<p class="strategy-card-identity"><span class="strategy-card-origin '+(userCard?'strategy-card-origin-user':'strategy-card-origin-builtin')+'">'+(userCard?'自定义':'内置')+'</span>'
    +'<span>v'+(item.current_version||item.version||1)+'</span>'
    +'<span>'+(item.has_dsl?'DSL':'原生')+'</span></p>'
    +'<ul class="strategy-card-facts">'
    +'<li><span class="fact-label">风险画像</span><b>'+adaptiveEsc(riskLabel)+'</b></li>'
    +'<li><span class="fact-label">资金阶段</span><b>'+adaptiveEsc(stageLabel)+(scale?' · '+scale:'')+'</b></li>'
    +'<li><span class="fact-label">席位</span><b>'+(seats!=null?seats+' 席':'—')+'</b></li>'
    +'<li class="'+(participates?'strategy-card-participation-ok':'strategy-card-participation-off')+'">'+adaptiveEsc(participation)+'</li>'
    +'</ul>'
    +'<p class="strategy-card-desc">'+adaptiveEsc(item.description||'')+'</p>'
    +'<details class="strategy-tech-details" data-testid="strategy-tech-details">'
    +'<summary>技术详情</summary>'
    +'<dl class="strategy-tech-grid">'
    +'<dt>策略 ID</dt><dd>'+adaptiveEsc(item.id)+'</dd>'
    +'<dt>生命周期（枚举）</dt><dd>'+adaptiveEsc(strategyStatusLabelTech(item.status))+'</dd>'
    +'<dt>当前版本</dt><dd>v'+(item.current_version||item.version||1)+'</dd>'
    +'<dt>版本校验和</dt><dd>'+(checksum?adaptiveEsc(checksum.slice(0,24)):'—')+'</dd>'
    +'<dt>DSL 校验和</dt><dd>'+(dslChecksum?adaptiveEsc(dslChecksum.slice(0,24)):'—')+'</dd>'
    +'<dt>实现键</dt><dd>'+adaptiveEsc(String(implKey))+'</dd>'
    +'<dt>风格 / 持有</dt><dd>'+adaptiveEsc(style)+' · '+adaptiveEsc(hold)+'</dd>'
    +'<dt>原始指纹</dt><dd>'+(rawFingerprint?adaptiveEsc(rawFingerprint.slice(0,120)):'—')+'</dd>'
    +'</dl></details>'
    +'<footer class="strategy-card-actions">'+actions+'</footer></article>';
}

export function wbRenderList(){
  var list=$('wbList'); if(!list) return;
  if(WB_STATE.view!=='list') return;
  var matched=WB_STATE.items.filter(wbMatches);
  var users=matched.filter(function(x){return x.origin==='user';});
  var builtins=matched.filter(function(x){return x.origin==='builtin';});
  if(!matched.length){
    // PR-49：区分「筛选没命中」与「一条自定义策略都还没有」两种空态。
    list.innerHTML=WB_STATE.originFilter==='user'
      ? '<div class="strategy-workbench-empty" data-testid="strategy-empty-user"><b>还没有自定义策略</b><p>从空白策略开始，或复制一套内置策略后修改。</p>'
        +'<div class="strategy-workbench-empty-actions">'
        +'<button type="button" class="strategy-workbench-primary" data-testid="strategy-empty-create" onclick="wbNewStrategy()">创建第一个策略</button>'
        +'<button type="button" class="ghost" data-testid="strategy-empty-clone-builtin" onclick="wbCloneFirstBuiltin()">复制内置策略</button>'
        +'</div></div>'
      : '<div class="strategy-workbench-empty"><b>没有匹配的策略</b><p>换个来源/状态筛选，或清空搜索词再试。</p><button type="button" class="ghost" onclick="wbResetFilters()">重置筛选</button></div>';
    return;
  }
  var html='';
  if(users.length){
    html+='<h3 class="strategy-list-group">我的策略</h3><div class="strategy-card-grid">'+users.map(wbCard).join('')+'</div>';
  }else if(WB_STATE.originFilter==='all'&&WB_STATE.statusFilter==='all'&&!($('wbSearch')&&$('wbSearch').value.trim())){
    html+='<div class="strategy-workbench-empty strategy-workbench-empty-inline"><b>还没有自定义策略</b><p>内置策略可直接复制成自定义策略后修改。</p><button type="button" class="strategy-workbench-primary" onclick="wbNewStrategy()">创建第一个策略</button></div>';
  }
  if(builtins.length) html+='<h3 class="strategy-list-group">内置策略（平台自带，可复制后修改）</h3><div class="strategy-card-grid">'+builtins.map(wbCard).join('')+'</div>';
  list.innerHTML=html;
}

export function wbResetFilters(){
  WB_STATE.originFilter='all'; WB_STATE.statusFilter='all';
  var search=$('wbSearch'); if(search) search.value='';
  document.querySelectorAll('[data-wb-origin]').forEach(function(x){x.classList.toggle('active',x.getAttribute('data-wb-origin')==='all');});
  document.querySelectorAll('[data-wb-status]').forEach(function(x){x.classList.toggle('active',x.getAttribute('data-wb-status')==='all');});
  wbRenderList();
}

export function wbShowView(view){
  WB_STATE.view=view;
  if($('wbList')) $('wbList').hidden=view!=='list';
  if($('wbEditor')) $('wbEditor').hidden=view!=='editor';
  if($('wbDetail')) $('wbDetail').hidden=view!=='detail';
  if($('wbSummary')) $('wbSummary').hidden=view!=='list';
  var toolbar=$('wbSearch'); if(toolbar) toolbar.parentElement.hidden=view!=='list';
}

export function wbNewStrategy(){
  WB_STATE.editingId=null; WB_STATE.editingVersion=null;
  wbRenderEditor(null); wbShowView('editor');
  history.replaceState(null,'','#strategies/new');
}

export async function wbOpenEditor(strategyId){
  var item=null;
  try{ item=await api('/api/strategies/'+encodeURIComponent(strategyId)+'?_='+Date.now()); }
  catch(e){ inlineError($('wbList'), e, { title: '读取策略失败', retryLabel: '重试', onRetry: function(){ wbOpenEditor(strategyId); } }); return; }
  if(item.origin!=='user'){ toast('内置策略不可直接修改；请先“复制并编辑”生成自定义副本。', { tone: 'warn' }); return; }
  WB_STATE.editingId=strategyId; WB_STATE.editingVersion=item.version||item.current_version;
  wbRenderEditor(item); wbShowView('editor');
  history.replaceState(null,'','#strategies/'+encodeURIComponent(strategyId));
}

export function wbConditionRowHtml(row){
  row=row||{};
  var fields=WB_DSL_FIELDS.map(function(f){return '<option value="'+f+'"'+(row.leftField===f?' selected':'')+'>'+(WB_FIELD_LABELS[f]||f)+'</option>';}).join('');
  var ops=WB_DSL_COMPARISONS.map(function(p){return '<option value="'+p[0]+'"'+(row.op===p[0]?' selected':'')+'>'+p[1]+'</option>';}).join('');
  var inds=WB_DSL_INDICATORS.map(function(f){return '<option value="'+f+'"'+(row.rightIndicator===f?' selected':'')+'>'+(WB_INDICATOR_LABELS[f]||f)+'</option>';}).join('');
  return '<div class="strategy-condition-row" data-wb-row>'
    +'<label>字段 <select data-wb-field>'+fields+'</select></label>'
    +'<label>运算符 <select data-wb-op>'+ops+'</select></label>'
    +'<label>右侧 <select data-wb-right-kind>'
      +'<option value="field"'+(row.rightKind==='field'?' selected':'')+'>字段</option>'
      +'<option value="indicator"'+(row.rightKind==='indicator'?' selected':'')+'>指标</option>'
      +'<option value="const"'+(row.rightKind==='const'?' selected':'')+'>数值</option></select></label>'
    +'<span data-wb-right-value class="strategy-condition-value">'
      +'<select data-wb-indicator'+(row.rightKind==='indicator'?'':' hidden')+'>'+inds+'</select>'
      +'<input type="number" data-wb-window value="'+(row.rightWindow!=null?row.rightWindow:20)+'" min="2" max="250" aria-label="窗口"'+(row.rightKind==='indicator'?'':' hidden')+'>'
      +'<input type="number" data-wb-const value="'+(row.rightConst!=null?row.rightConst:'')+'" step="any" aria-label="数值"'+(row.rightKind==='const'?'':' hidden')+'></span>'
    +'<button type="button" class="strategy-condition-remove" aria-label="删除该条件" onclick="wbRemoveCondition(this)">✕</button>'
    +'</div>';
}

export function wbAstToConditionRow(node){
  if(!node||typeof node!=='object') return {};
  var op=node.op;
  if(op==='and'||op==='or'){ node=node.args&&node.args[0]?node.args[0]:(node.conditions&&node.conditions[0]||{}); op=node.op; }
  if(op!=='gt'&&op!=='gte'&&op!=='lt'&&op!=='lte') return {};
  var left=node.left||{}, right=node.right||{};
  var row={op:op,rightKind:'const'};
  if(left.op==='field') row.leftField=left.name;
  else if(left.op==='indicator'){ row.leftField='close'; }
  else return {};
  if(right.op==='field'){ row.rightKind='field'; }
  else if(right.op==='indicator'){ row.rightKind='indicator'; row.rightIndicator=right.name; row.rightWindow=(right.window&&right.window.value)!=null?right.window.value:(typeof right.window==='number'?right.window:20); }
  else if(right.value!=null){ row.rightKind='const'; row.rightConst=right.value; }
  return row;
}

export function wbRenderEditor(item){
  var editor=$('wbEditor'); if(!editor) return;
  var isNew=!item;
  var dsl=item&&item.dsl_ast?JSON.stringify(item.dsl_ast,null,2):'';
  var conditions=[];
  try{
    var ast=item&&item.dsl_ast;
    if(ast&&ast.op==='and') conditions=(ast.args||ast.conditions||[]).map(wbAstToConditionRow);
    else if(ast) conditions=[wbAstToConditionRow(ast)];
  }catch(e){ conditions=[]; }
  conditions=conditions.filter(function(x){return x&&x.leftField;});
  if(!conditions.length) conditions=[{}];
  var meta=item&&item.metadata||{};
  editor.innerHTML='<header class="strategy-editor-head"><h3>'+(isNew?'新建自定义策略':'编辑策略 · '+adaptiveEsc(item.name||item.id))+'</h3>'
    +'<div><button type="button" onclick="wbBackToList()">返回列表</button></div></header>'
    +'<div class="strategy-editor-columns"><section class="strategy-editor-form">'
    +'<h4>基本信息</h4>'
    +'<label>策略名称 <input data-testid="strategy-name" id="wbName" type="text" maxlength="64" value="'+adaptiveEsc(item?item.name:'')+'" placeholder="例如：趋势放量突破"></label>'
    +(isNew?'<label>策略 ID <input data-testid="strategy-id" id="wbStrategyId" type="text" maxlength="64" placeholder="例如 trend_volume_breakout"><small>3–64 字符，小写字母开头，仅 a-z 0-9 _（创建后不能改）</small></label>'
           :'<p class="strategy-editor-static">ID：<b>'+adaptiveEsc(item.id)+'</b> · 当前版本 v'+(item.version||item.current_version||1)+'</p>')
    +'<label>策略说明 <textarea id="wbDescription" rows="2" maxlength="300">'+adaptiveEsc(item?item.description:'')+'</textarea></label>'
    +'<h4>策略条件</h4>'
    +'<div class="strategy-editor-modes"><button type="button" class="active" data-testid="strategy-mode-builder" id="wbModeBuilder" onclick="wbSetMode(\'builder\')">可视化模式</button><button type="button" data-testid="strategy-mode-dsl" id="wbModeDsl" onclick="wbSetMode(\'dsl\')">高级 DSL / JSON</button></div>'
    +'<p class="strategy-editor-note" id="wbCombineNote">条件之间以 AND 组合</p>'
    +'<div id="wbConditions" class="strategy-conditions" data-testid="strategy-condition-builder">'+conditions.map(wbConditionRowHtml).join('')+'</div>'
    +'<button type="button" class="strategy-condition-add" data-testid="strategy-add-condition" onclick="wbAddCondition()">+ 添加条件</button>'
    +'<div id="wbDslPane" hidden><p class="strategy-editor-note">这里只接受声明式策略 DSL（JSON），不执行 Python / SQL / Shell。</p>'
    +'<textarea data-testid="strategy-dsl" id="wbDslText" class="strategy-dsl-input" rows="10" spellcheck="false" placeholder=\'{"op":"and","args":[…]}\''+'>'+adaptiveEsc(dsl)+'</textarea>'
    +'<div class="strategy-editor-dsl-tools"><button type="button" onclick="wbFormatDsl()">格式化</button><button type="button" onclick="wbFromConditionsToDsl()">从条件生成</button></div></div>'
    +'<h4>运行参数</h4>'
    +'<label>候选数量 <input id="wbCandidateTopn" type="number" min="1" max="50" value="'+(meta.candidate_topn!=null?meta.candidate_topn:10)+'"></label>'
    +'<label>持有周期（天） <input id="wbHold" type="number" min="1" max="60" value="'+(meta.hold!=null?meta.hold:8)+'"></label>'
    +(isNew?'':'<label>变更说明（保存后生成新版本）<input id="wbChangeNote" type="text" maxlength="120" value="Web editor update"></label>')
    +'<footer class="strategy-editor-footer">'
    +'<button type="button" class="strategy-workbench-primary" data-testid="strategy-save" onclick="wbSaveDraft()">保存草稿</button>'
    +'<button type="button" data-testid="strategy-validate" onclick="wbValidateDraft()">验证策略</button>'
    +'<button type="button" data-testid="strategy-preview" onclick="wbPreviewDraft()">预览风险与资金</button>'
    +'</footer>'
    +'<div id="wbValidateResult" class="strategy-preview-result" role="status" aria-live="polite"></div>'
    +'</section>'
    +'<aside class="strategy-preview-panel" id="wbPreviewPanel"><h4>风险与资金预览</h4><p class="strategy-editor-note">编辑条件后点击“预览风险与资金”。画像由后端 Runtime 编译，前端不复制规则。</p></aside>'
    +'</div>';
  wbBindConditionRows();
  WB_STATE.editorMode='builder';
}

export function wbBindConditionRows(){
  document.querySelectorAll('#wbConditions [data-wb-row]').forEach(function(row){
    var kind=row.querySelector('[data-wb-right-kind]');
    var sync=function(){
      var v=kind.value;
      row.querySelector('[data-wb-indicator]').hidden=v!=='indicator';
      row.querySelector('[data-wb-window]').hidden=v!=='indicator';
      row.querySelector('[data-wb-const]').hidden=v!=='const';
    };
    kind.onchange=sync; sync();
  });
}

export function wbAddCondition(){
  var box=$('wbConditions'); if(!box) return;
  box.insertAdjacentHTML('beforeend',wbConditionRowHtml({}));
  wbBindConditionRows();
}

export function wbRemoveCondition(btn){
  var box=$('wbConditions');
  if(box&&box.querySelectorAll('[data-wb-row]').length<=1){ toast('至少保留一条条件。', { tone: 'warn' }); return; }
  var row=btn.closest('[data-wb-row]'); if(row) row.remove();
}

export function wbSetMode(mode){
  WB_STATE.editorMode=mode;
  if($('wbModeBuilder')) $('wbModeBuilder').classList.toggle('active',mode==='builder');
  if($('wbModeDsl')) $('wbModeDsl').classList.toggle('active',mode==='dsl');
  if($('wbConditions')) $('wbConditions').hidden=mode!=='builder';
  if($('wbCombineNote')) $('wbCombineNote').hidden=mode!=='builder';
  var addBtn=document.querySelector('.strategy-condition-add'); if(addBtn) addBtn.hidden=mode!=='builder';
  if($('wbDslPane')) $('wbDslPane').hidden=mode!=='dsl';
}

export function wbCollectConditions(){
  var rows=document.querySelectorAll('#wbConditions [data-wb-row]');
  var conditions=[];
  rows.forEach(function(row){
    var field=row.querySelector('[data-wb-field]').value;
    var op=row.querySelector('[data-wb-op]').value;
    var kind=row.querySelector('[data-wb-right-kind]').value;
    var left={op:'field',name:field};
    var right;
    if(kind==='field') right={op:'field',name:field};
    else if(kind==='indicator') right={op:'indicator',name:row.querySelector('[data-wb-indicator]').value,window:{value:Number(row.querySelector('[data-wb-window]').value)||20}};
    else right={op:'const',value:Number(row.querySelector('[data-wb-const]').value)||0};
    conditions.push({op:op,left:left,right:right});
  });
  return conditions;
}

export function wbBuildAst(){
  if(WB_STATE.editorMode==='dsl'){
    try{ return JSON.parse($('wbDslText').value||'null'); }catch(e){ throw new Error('DSL JSON 解析失败：'+e.message); }
  }
  var conditions=wbCollectConditions();
  if(!conditions.length) return null;
  return conditions.length===1?conditions[0]:{op:'and',args:conditions};
}

export function wbCollectDraft(){
  var name=($('wbName')&&$('wbName').value||'').trim();
  var id=null;
  if(WB_STATE.editingId) id=WB_STATE.editingId;
  else{
    id=($('wbStrategyId')&&$('wbStrategyId').value||'').trim();
    if(!/^[a-z][a-z0-9_]{2,63}$/.test(id)) throw new Error('策略 ID 不合法：3–64 字符，小写字母开头，仅 a-z 0-9 _');
  }
  if(!name) throw new Error('请填写策略名称。');
  var ast=wbBuildAst();
  var metadata={style:'trend',candidate_topn:Number($('wbCandidateTopn').value)||10,hold:Number($('wbHold').value)||8};
  return {id:id,name:name,description:($('wbDescription')&&$('wbDescription').value||'').trim(),metadata:metadata,dsl_ast:ast};
}

export function wbSetFeedback(box,ok,title,bodyHtml){
  box.className='strategy-preview-result '+(ok?'strategy-preview-ok':'strategy-preview-bad');
  box.innerHTML='<b>'+title+'</b>'+(bodyHtml||'');
}

export async function wbValidateDraft(){
  var box=$('wbValidateResult'); if(!box) return;
  var draft;
  try{ draft=wbCollectDraft(); }catch(e){ wbSetFeedback(box,false,'输入有误',adaptiveEsc(e.message)); return; }
  if(!draft.dsl_ast){ wbSetFeedback(box,false,'尚无条件','请先添加条件或填写 DSL。'); return; }
  try{
    var result=await apiPostJson('/api/strategies/validate',{dsl_ast:draft.dsl_ast,metadata:draft.metadata});
    if(result.valid){
      var warnings=(result.warnings||[]).map(function(x){return '<li>'+adaptiveEsc(x)+'</li>';}).join('');
      wbSetFeedback(box,true,'✓ DSL 有效'+(result.checksum?' · checksum '+adaptiveEsc(String(result.checksum).slice(0,12)):''),warnings?'<ul>'+warnings+'</ul>':'');
    }else{
      wbSetFeedback(box,false,'DSL 无效','<ul>'+(result.errors||[]).map(function(x){return '<li>'+adaptiveEsc(x)+'</li>';}).join('')+'</ul>');
    }
  }catch(e){ wbSetFeedback(box,false,'验证失败',adaptiveEsc(e.message||e)); }
}

export async function wbPreviewDraft(){
  var panel=$('wbPreviewPanel'); if(!panel) return;
  var draft;
  try{ draft=wbCollectDraft(); }catch(e){ panel.innerHTML='<h4>风险与资金预览</h4><p class="strategy-preview-bad">'+adaptiveEsc(e.message)+'</p>'; return; }
  panel.innerHTML='<h4>风险与资金预览</h4><div class="loading">正在编译运行时画像…</div>';
  try{
    var payload={id:draft.id,name:draft.name,description:draft.description,metadata:draft.metadata,dsl_ast:draft.dsl_ast};
    var result=await apiPostJson('/api/strategies/preview',payload);
    if(!result.valid){ panel.innerHTML='<h4>风险与资金预览</h4><div class="strategy-preview-bad"><b>预览失败</b><ul>'+(result.errors||[]).map(function(x){return '<li>'+adaptiveEsc(x)+'</li>';}).join('')+'</ul></div>'; return; }
    var fp=result.risk_fingerprint||{};
    var rp=result.risk_profile||{};
    var limits=rp.limits||rp.soft_limits||{};
    var ep=result.execution_profile||{};
    var alloc=result.allocation||{};
    var warnings=(result.warnings||[]).concat(result.high_risk_overrides||[]);
    var conservative=result.fail_closed||String(fp.archetype||'').indexOf('composite')>=0;
    // PR-57：预览拆成三段，职责分明——
    //   A 系统硬边界（后端 hard_rules，策略无法覆盖）
    //   B 策略风控（后端 soft_limits/limits + 执行画像，可在画像内调整）
    //   C 可进化参数（后端 parameter_schema.tunable，仅展示真实存在的可调参数）
    var hard=rp.hard_rules||result.hard_rules||{};
    var schemaDeclared=!!(result.parameter_schema&&result.parameter_schema.tunable);
    var evolvable=(schemaDeclared?result.parameter_schema.tunable:(rp.evolvable_params||result.evolvable_params||{}));
    function kv(map, formatter){
      var keys=Object.keys(map||{});
      if(!keys.length) return '<p class="risk-preview-empty">后端未返回该段数据。</p>';
      return '<dl class="strategy-preview-grid">'+keys.map(function(k){
        var v=map[k]; var text=(formatter&&formatter[k])?formatter[k](v):(v==null?'—':String(v));
        return '<dt>'+adaptiveEsc(k)+'</dt><dd>'+adaptiveEsc(text)+'</dd>';
      }).join('')+'</dl>';
    }
    var softFormatter={
      risk_per_trade:function(v){return v==null?'—':(Number(v)*100).toFixed(2)+'%';},
      max_exposure:function(v){return v==null?'—':(Number(v)*100).toFixed(1)+'%';},
      max_exposure_pct:function(v){return v==null?'—':(Number(v)*100).toFixed(1)+'%';},
    };
    var hardList=Object.keys(hard);
    panel.innerHTML='<h4>风险与资金预览</h4>'
      +'<p class="strategy-preview-note">风险画像：'+adaptiveEsc(rp.recommended_profile_label||rp.template||fp.archetype||'—')
      +'　置信度：'+(fp.confidence!=null?Math.round(fp.confidence*100)+'%':'—')+'</p>'
      +'<div class="risk-preview-sections">'
      +'<section class="risk-preview-section risk-preview-system" data-testid="risk-preview-system">'
      +'<h5><span class="risk-preview-lock" aria-hidden="true">🔒</span>系统硬边界</h5>'
      +(hardList.length?('<dl class="strategy-preview-grid">'+hardList.map(function(k){return '<dt>'+adaptiveEsc(k)+'</dt><dd>'+adaptiveEsc(hard[k]==null?'—':String(hard[k]))+'</dd>';}).join('')+'</dl>'):('<p class="risk-preview-empty">后端未返回硬边界明细。</p>'))
      +'<p class="risk-preview-note">这些是平台与交易所层面的硬规则，策略定义无法覆盖或放宽。</p></section>'
      +'<section class="risk-preview-section risk-preview-strategy" data-testid="risk-preview-strategy">'
      +'<h5>策略风控</h5>'
      +kv(limits, softFormatter)
      +'<dl class="strategy-preview-grid"><dt>最大持有</dt><dd>'+adaptiveEsc(String(draft.metadata.hold))+' 日</dd>'
      +'<dt>订单类型</dt><dd>'+adaptiveEsc(ep.order_type||'—')+'</dd><dt>紧急度</dt><dd>'+adaptiveEsc(ep.urgency||'—')+'</dd>'
      +'<dt>TTL</dt><dd>'+(ep.ttl_minutes!=null?ep.ttl_minutes+' 分钟':'—')+'</dd></dl>'
      +'<dl class="strategy-preview-grid"><dt>资金阶段</dt><dd>'+adaptiveEsc(alloc.stage_label||alloc.lifecycle_stage||'—')+'</dd>'
      +'<dt>资金系数</dt><dd>'+(alloc.capital_scale!=null?Math.round(alloc.capital_scale*100)+'%':'—')+'</dd>'
      +'<dt>预计可部署</dt><dd>'+(alloc.estimated_capital!=null?'¥'+Number(alloc.estimated_capital).toLocaleString('zh-CN'):'—')+'</dd></dl></section>'
      +'<section class="risk-preview-section risk-preview-evolvable" data-testid="risk-preview-evolvable">'
      +'<h5>'+(schemaDeclared?'可进化参数':'风险模板可调参数')+'</h5>'
      +kv(evolvable)
      +(schemaDeclared
        ?'<p class="risk-preview-note">来源：本草稿编译出的参数 Schema；未声明的项不允许自进化调整。</p>'
        :'<p class="risk-preview-note">来源：风险画像模板声明的可调项（后端 preview 当前未返回 parameter_schema），不代表本草稿 DSL 的可调集合。</p>')
      +'</section>'
      +'</div>'
      +(conservative?'<p class="strategy-preview-warn">保守画像：系统按最安全口径处理，请确认条件符合预期。</p>':'')
      +(warnings.length?'<ul class="strategy-preview-warn">'+warnings.map(function(x){return '<li>'+adaptiveEsc(typeof x==='string'?x:(x.note||x.label||x.reason||x.code||''))+'</li>';}).join('')+'</ul>':'');
  }catch(e){ panel.innerHTML='<h4>风险与资金预览</h4><div class="strategy-preview-bad">预览失败：'+adaptiveEsc(e.message||e)+'</div>'; }
}

export function wbFormatDsl(){
  var box=$('wbDslText'); if(!box) return;
  try{ box.value=JSON.stringify(JSON.parse(box.value||'null'),null,2); }
  catch(e){ toast('JSON 解析失败：'+(e.message||e), { tone: 'danger' }); }
}

export function wbFromConditionsToDsl(){
  var ast=wbBuildAst(); var box=$('wbDslText');
  if(box&&ast) box.value=JSON.stringify(ast,null,2);
}

export async function wbSaveDraft(){
  var draft;
  try{ draft=wbCollectDraft(); }catch(e){ inlineError($('wbEditor')||$('wbPreviewPanel'), e, { title: '无法保存', testid: 'strategy-save-error' }); return; }
  try{
    if(WB_STATE.editingId){
      var saved=await apiJson('/api/strategies/'+encodeURIComponent(WB_STATE.editingId),'PATCH',{
        changes:{name:draft.name,description:draft.description,metadata:draft.metadata,dsl_ast:draft.dsl_ast},
        expected_version:WB_STATE.editingVersion,change_note:($('wbChangeNote')&&$('wbChangeNote').value||'Web editor update'),actor:'strategy-workbench',
      });
      toast('保存成功 · v'+(saved.version||saved.current_version||'?')+'（旧版本保留）');
    }else{
      var created=await apiPostJson('/api/strategies',Object.assign({actor:'strategy-workbench'},draft));
      toast('保存成功 · '+adaptiveEsc(created.id||draft.id)+' v'+(created.version||created.current_version||1));
    }
    await loadStrategyWorkbench(true);
    wbBackToList();
  }catch(e){
    // 版本竞争（expected_version 不匹配）必须显式告知，绝不静默覆盖。
    var msg=String((e&&e.message)||e);
    if(/version|版本|conflict|409/i.test(msg)){
      inlineError($('wbEditor'), '当前版本已变化：'+msg+'。请重新载入最新版本后再提交，本次修改未写入。',
        { title: '版本冲突', testid: 'strategy-version-conflict', retryLabel: '重新载入最新', onRetry: function(){ loadStrategyWorkbench(true); wbBackToList(); } });
    }else{
      inlineError($('wbEditor'), msg, { title: '保存失败', testid: 'strategy-save-error', retryLabel: '重试保存', onRetry: function(){ wbSaveDraft(); } });
    }
  }
}

export async function wbValidateAndMark(strategyId){
  var ok=await confirmDialog({
    kicker:'生命周期 · 标记已验证',
    title:'标记为已验证？',
    detail:strategyId,
    bullets:['标记后仍不参与任何周期，也不会开仓。','可继续修改定义；修改会生成新版本。','只有已验证的策略才能被激活。'],
    confirmText:'标记为已验证',
  });
  if(!ok.approved) return;
  try{
    await apiPostJson('/api/strategies/'+encodeURIComponent(strategyId)+'/transition',{to_status:'validated',reason:'Web workbench 验证通过'});
    await loadStrategyWorkbench(true);
    toast('已标记为已验证：'+strategyId);
  }catch(e){ inlineError($('wbList'), e, { title:'标记失败', retryLabel:'重试', onRetry:function(){ wbValidateAndMark(strategyId); } }); }
}

export async function wbTransition(strategyId,toStatus){
  var item=WB_STATE.items.filter(function(x){return x.id===strategyId;})[0]||{};
  // PR-57：危险/高影响动作改用应用内模态，并明确说明各自的边界语义。
  var DIALOGS={
    draft:{ title:'退回草稿？', danger:false, confirmText:'退回草稿',
      bullets:['退回后不参与未来周期，可继续修改定义。','已产生的订单、成交与审计记录不会回滚或删除。'] },
    active:{ title:'激活策略？', danger:false, confirmText:'激活策略',
      bullets:['激活不会把策略加入正在运行的周期。','请在「设置 → 模拟盘与资金」勾选它参与下一周期。','激活本身不改动历史版本与账本。'] },
    paused:{ title:'暂停策略？', danger:false, confirmText:'暂停',
      bullets:['暂停后不再产生新的开仓信号（停止新开仓）。','当前周期的经济账本归属保持不变，不会被删除。','存量持仓继续由系统风控执行退出。'] },
    retiring:{ title:'退役策略？', danger:false, confirmText:'进入退役',
      bullets:['退役表示不再参与未来周期、不再新开仓。','存量持仓按风控逐步退出。','退役不等于删除：历史版本、订单、成交与审计都保留。','退出完成后可选择归档。'] },
    archived:{ title:'归档策略？', danger:true, confirmText:'确认归档',
      bullets:['归档后该策略不再加入任何未来周期。','历史与详情仍可查看（版本、订单、成交、审计记录都不会删除）。','归档是高影响动作，但可逆性有限：如需再启用需重新创建或复制。'] },
  };
  var dlg=DIALOGS[toStatus]||{ title:'迁移到 '+toStatus+'？', confirmText:'确认迁移', bullets:[] };
  var answer=await confirmDialog({
    kicker:'生命周期 · '+strategyStatusLabelTech(toStatus),
    title:dlg.title,
    detail:strategyId,
    bullets:dlg.bullets,
    danger:dlg.danger,
    confirmText:dlg.confirmText,
  });
  if(!answer.approved) return;
  try{
    await apiPostJson('/api/strategies/'+encodeURIComponent(strategyId)+'/transition',{to_status:toStatus,expected_status:item.status,reason:'Web workbench 操作'});
    await loadStrategyWorkbench(true);
    toast('已更新生命周期：'+strategyId+' → '+strategyStatusLabelTech(toStatus));
  }catch(e){ inlineError($('wbList'), e, { title:'生命周期迁移失败', retryLabel:'重试', onRetry:function(){ wbTransition(strategyId,toStatus); } }); }
}

export async function wbCloneFirstBuiltin(){
  // 空态 CTA：复制第一套内置策略（复用既有克隆流程与确认模态）。
  var builtin=(WB_STATE.items||[]).filter(function(x){return x.origin==='builtin';})[0];
  if(!builtin){ toast('当前没有可复制的内置策略。', { tone: 'warn' }); return; }
  await wbCloneStrategy(builtin.id);
}

export async function wbCloneStrategy(strategyId){
  var suffix=Date.now().toString(36).slice(-4);
  var answer=await promptDialog({
    kicker:'复制策略',
    title:'复制为新草稿',
    detail:'源策略不会被修改；副本以 draft 起步，版本从 v1 开始。',
    label:'新策略 ID（3–64 字符，小写字母开头）',
    value:strategyId+'_clone_'+suffix,
  });
  if(!answer.approved||!answer.value) return;
  var newId=answer.value;
  try{
    await apiPostJson('/api/strategies/'+encodeURIComponent(strategyId)+'/clone',{id:newId,actor:'strategy-workbench'});
    await loadStrategyWorkbench(true);
    toast('已复制为草稿：'+newId);
  }catch(e){ inlineError($('wbList'), e, { title:'复制失败', retryLabel:'重试', onRetry:function(){ wbCloneStrategy(strategyId); } }); }
}

export async function wbDeleteDraft(strategyId){
  var answer=await confirmDialog({
    kicker:'物理删除 · 不可撤销',
    title:'删除这个草稿？',
    detail:strategyId,
    danger:true,
    confirmText:'永久删除',
    bullets:[
      '只有**从未离开草稿状态且从未被使用**的草稿才能被物理删除。',
      '一旦该策略产生过正式生命周期记录（例如验证、激活、回退），删除会被后端拒绝。',
      '那种情况下请改用「归档」：历史版本、订单、成交与审计都会保留。',
    ],
  });
  if(!answer.approved) return;
  try{
    await apiJson('/api/strategies/'+encodeURIComponent(strategyId),'DELETE');
    await loadStrategyWorkbench(true);
    toast('已删除草稿：'+strategyId);
  }catch(e){
    inlineError($('wbList'), '删除被拒绝：'+((e&&e.message)||e)+'（若该策略已进入过正式生命周期，请使用归档。）',
      { title:'无法物理删除', testid:'strategy-delete-rejected' });
  }
}

export function wbRenderNotFound(strategyId,message){
  var box=$('wbDetail'); if(!box) return;
  box.innerHTML='<header class="strategy-editor-head"><h3>策略不存在</h3>'
    +'<div><button type="button" onclick="wbBackToList()">返回列表</button></div></header>'
    +'<div class="strategy-editor-note" data-testid="strategy-not-found">'
    +'找不到策略 <code>'+adaptiveEsc(strategyId)+'</code>。它可能已被删除，或链接里的 ID 拼错了。'
    +(message?'<br><small>'+adaptiveEsc(message)+'</small>':'')+'</div>';
  wbShowView('detail');
}

export async function wbOpenDetail(strategyId){
  var item;
  try{ item=await api('/api/strategies/'+encodeURIComponent(strategyId)+'?_='+Date.now()); }
  catch(e){
    // 未知/不可读的策略：给出可读状态，不弹 modal、不抛异常、不写坏 hash。
    wbRenderNotFound(strategyId,e&&e.message);
    return null;
  }
  var versions=[],events=[];
  try{ versions=(await api('/api/strategies/'+encodeURIComponent(strategyId)+'/versions')).items||[]; }catch(e){}
  try{ events=(await api('/api/strategies/'+encodeURIComponent(strategyId)+'/events')).items||[]; }catch(e){}
  var box=$('wbDetail'); if(!box) return;
  WB_STATE.editingId=strategyId; WB_STATE.editingVersion=item.version||item.current_version;
  var currentVersion=item.version||item.current_version||1;
  var runtime=item.runtime||{};
  var timeline=events.slice().reverse().map(function(ev){
    return '<li><span class="strategy-version-time">'+adaptiveEsc(ev.created_at||'')+'</span> '+adaptiveEsc(ev.from_status||'—')+' → <b>'+adaptiveEsc(ev.to_status||ev.status||'')+'</b>'
      +(ev.reason?'<small> · '+adaptiveEsc(ev.reason)+'</small>':'')+'</li>';
  }).join('');
  var versionRows=versions.slice().reverse().map(function(v){
    return '<li data-testid="strategy-version-'+v.version+'" class="strategy-version-row'+(v.version===currentVersion?' current':'')+'">'
      +'<b>v'+v.version+'</b>'+(v.version===currentVersion?' <span class="strategy-status-badge strategy-status-validated">当前</span>':'')
      +'<span class="strategy-version-time">'+adaptiveEsc(v.created_at||'')+'</span>'
      +'<small>'+adaptiveEsc(v.change_note||v.created_by||'')+'</small>'
      +'<code>'+adaptiveEsc(String(v.checksum||'').slice(0,12))+'</code></li>';
  }).join('');
  box.innerHTML='<header class="strategy-editor-head"><h3>'+adaptiveEsc(item.name||item.id)+' '+wbStatusBadge(item.status)+'</h3>'
    +'<div><button type="button" onclick="wbBackToList()">返回列表</button></div></header>'
    +'<div class="strategy-detail-columns"><section><h4>概况</h4><dl class="strategy-preview-grid">'
    +'<dt>ID</dt><dd>'+adaptiveEsc(item.id)+'</dd><dt>来源</dt><dd>'+(item.origin==='user'?'自定义':'内置')+'</dd>'
    +'<dt>当前版本</dt><dd>v'+currentVersion+'</dd><dt>支持新周期</dt><dd>'+(item.supports_new_cycle?'是':'否')+'</dd>'
    +'<dt>生命周期阶段</dt><dd>'+(runtime.runtime_ready?adaptiveEsc(runtime.lifecycle_stage||'—'):'—')+'</dd>'
    +'<dt>资金系数</dt><dd>'+(runtime.runtime_ready&&runtime.capital_scale!=null?Math.round(runtime.capital_scale*100)+'%':'—')+'</dd>'
    +'<dt>运行时就绪</dt><dd>'+(item.runtime_ready===false?'否 · '+adaptiveEsc(item.runtime_error||''):'是')+'</dd></dl>'
    +'<h4>生命周期时间线</h4><ul class="strategy-version-list">'+(timeline||'<li>暂无事件。</li>')+'</ul></section>'
    +'<section><h4>版本历史（只读）</h4><ul class="strategy-version-list" data-testid="strategy-version-list">'+(versionRows||'<li>暂无版本。</li>')+'</ul></section></div>';
  wbShowView('detail');
  window._strategiesRouteId=null;
  history.replaceState(null,'','#strategies/'+encodeURIComponent(strategyId));
  return item;
}

export function wbBackToList(){
  WB_STATE.view='list'; wbShowView('list');
  history.replaceState(null,'','#strategies');
  wbRenderList();
}

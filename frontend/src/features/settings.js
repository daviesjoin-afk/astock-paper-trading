/* PR-55：由 frontend/app.js 拆分（纯搬运，逻辑/文案未改） */
// 跨模块依赖（由原单文件作用域推导）
import { api, apiPostJson, apiJson, getOperatorToken, setOperatorToken, clearOperatorToken } from "../core/api.js";
import { $ } from "../core/dom.js";
import { adaptiveEsc, riskText } from "../core/format.js";
import { SETTINGS_SECTION_KEY, activatePage } from "../core/navigation.js";
import { loadPaper } from "./paper.js";
import { STRATEGY_STATUS_LABELS, wbStatusBadge } from "./strategies.js";
import { settingsConfirm, toast, inlineError, confirmDialog } from "../ui/dialog.js";

// ---------- 操作员授权（PR-2）----------
// 只管理**本标签页**的浏览器凭据：不调用任何后端 login / session / token 校验
// 接口，只做 sessionStorage 的读写与状态展示。凭据本身永不回显。
export function operatorUnlockStateHtml(){
  var unlocked=!!getOperatorToken();
  return '<span class="setting-value-preview" id="operatorUnlockState" data-testid="operator-unlock-state">'+(unlocked?'本标签页已授权':'未授权')+'</span>';
}

export function renderOperatorUnlockPanel(){
  var unlocked=!!getOperatorToken();
  return '<div class="settings-panel" style="margin-top:14px;padding:14px" data-testid="operator-unlock-panel">'
    +'<h4>操作员授权</h4>'
    +'<p>写操作（下单、启停、改设置等）需要操作员凭据。凭据只保存在<strong>本标签页</strong>，关闭标签页即失效；刷新本页保留。授权不会回显凭据内容。</p>'
    +'<div class="setting-control"><label for="operatorTokenInput">操作员凭据</label>'
    +'<input id="operatorTokenInput" data-testid="operator-token-input" type="password" autocomplete="off" spellcheck="false" placeholder="粘贴操作员凭据"></div>'
    +'<div class="settings-actions">'
    +'<button type="button" data-testid="operator-unlock-btn" onclick="unlockOperatorTab()">本标签页解锁</button>'
    +'<button type="button" class="ghost" data-testid="operator-clear-btn" onclick="clearOperatorTab()">清除授权</button>'
    +'</div>'
    +'<div class="setting-row"><div class="setting-label"><b>当前状态</b><small>授权只影响本标签页；其它标签页需各自解锁。</small></div>'
    +'<div class="setting-control">'+operatorUnlockStateHtml()+'</div></div>'
    +'</div>';
}

export function unlockOperatorTab(){
  var input=$('operatorTokenInput');
  var value=input?String(input.value||'').trim():'';
  if(!value){ toast('请输入操作员凭据。', { tone: 'danger' }); return; }
  setOperatorToken(value);
  if(input) input.value='';
  refreshOperatorUnlockState();
  toast('本标签页已授权，可以执行写操作。');
}

export function clearOperatorTab(){
  clearOperatorToken();
  var input=$('operatorTokenInput'); if(input) input.value='';
  refreshOperatorUnlockState();
  toast('已清除本标签页授权。');
}

export function refreshOperatorUnlockState(){
  var node=$('operatorUnlockState');
  if(node) node.textContent=getOperatorToken()?'本标签页已授权':'未授权';
}

// ---------- 统一设置中心 ----------
export var SETTINGS_STRATEGY_NAMES={tq_breakout:'短线日内做T',trend_pullback:'趋势波段优选',sector_rotation:'板块轮动先锋',reported_profit_breakout:'财报突破质量',main_force_top10:'超强主力股'};

export var SETTINGS_STYLE_NAMES={strong:'强势接力',pullback:'趋势回踩',sector:'板块轮动',quality:'质量突破',main_force:'主力跟随'};

export var SETTINGS_DURATION_OPTIONS=[15,30,60,90,180,0];

export function settingsCurrency(v){ return '￥'+Number(v||0).toLocaleString('zh-CN',{maximumFractionDigits:0}); }

export function settingsDurationLabel(v){ return Number(v)===0?'长期':Number(v)+' 个交易日'; }

export function settingsChecked(value){ return value?' checked':''; }

export function settingsInputRow(key,label,help,control){
  return '<div class="setting-row"><div class="setting-label"><b>'+adaptiveEsc(label)+'</b><small>'+adaptiveEsc(help||'')+'</small></div><div class="setting-control">'+control+'</div></div>';
}

export function settingsAuditHtml(items){
  if(!Array.isArray(items)||!items.length) return '<div class="settings-note">暂无人工修改记录；首次保存后会记录旧值、新值、操作者和时间。</div>';
  return '<div class="settings-audit-list">'+items.slice(0,20).map(function(item){
    var oldValue=typeof item.old_value==='object'?JSON.stringify(item.old_value):String(item.old_value==null?'—':item.old_value);
    var newValue=typeof item.new_value==='object'?JSON.stringify(item.new_value):String(item.new_value==null?'—':item.new_value);
    return '<div class="settings-audit-item"><b>'+adaptiveEsc(item.key)+'</b>：'+adaptiveEsc(oldValue)+' → '+adaptiveEsc(newValue)+'<small>'+adaptiveEsc(item.updated_by||'human-ui')+' · '+adaptiveEsc(item.created_at||'')+'</small></div>';
  }).join('')+'</div>';
}

export function settingsPreviewHtml(data,section){
  var risk=(data.settings||{}).risk||{}, effective=(data.effective||{}), current=effective.current_cycle||{}, next=effective.next_cycle||{};
  var minimum=effective.dynamic_minimum_order_amount;
  return '<aside class="settings-panel settings-preview"><h3>本次修改影响</h3><p>保存后这里会同步显示运行时读取到的边界；带“下一周期”的项目不会改写当前账本。</p>'
    +'<div class="settings-kpi"><small>当前周期</small><b>'+adaptiveEsc(current.status||'—')+' · '+settingsCurrency(current.capital)+'</b><span class="setting-help">'+adaptiveEsc(current.duration_label||'长期')+'</span></div>'
    +'<div class="settings-kpi"><small>下一周期</small><b>'+settingsCurrency(next.capital)+' · '+adaptiveEsc(next.duration_label||'长期')+'</b><span class="setting-help">启用 '+(next.enabled_strategies||[]).length+' 套策略</span></div>'
    +'<div class="settings-kpi"><small>动态最小建仓金额</small><b id="settingsMinimumPreview">'+settingsCurrency(minimum)+'</b><span class="setting-help">按周期金额 × 敞口 ÷ 席位 × 利用率向下取整</span></div>'
    +'<div class="settings-note"><strong>生效边界：</strong>共享池敞口、席位上限、单票绝对上限和建仓利用率在下一次扫描读取；资金、启用策略和策略参数在创建新周期时采用。</div>'
    +'<div><h4>最近设置审计</h4>'+settingsAuditHtml(data.audit)+'</div></aside>';
}

export function renderSettings(data){
  window._settingsPayload=data||{};
  var target=$('settingsResult'); if(!target) return;
  var section=window._settingsSection||'simulation', settings=(data&&data.settings)||{}, defaults=(data&&data.defaults)||{}, html='';
  var sim=settings.simulation||{}, risk=settings.risk||{}, strat=settings.strategy||{}, evo=settings.evolution||{}, ai=(data&&data.ai)||{}, cur=((data||{}).effective||{}).current_cycle||{};
  if(section==='simulation'){
    var durationOptions=SETTINGS_DURATION_OPTIONS.map(function(option){return '<option value="'+option+'"'+(Number(sim.cycle_duration_days)===option?' selected':'')+'>'+settingsDurationLabel(option)+'</option>';}).join('');
    var strategyChecks=(sim.enabled_strategies||[]);
    var strategyCheckbox=function(item){
      var checkable=item.status==='active'&&item.supports_new_cycle;
      var checked=strategyChecks.indexOf(item.id)>=0;
      var badge=wbStatusBadge(item.status);
      var extra='';
      if(item.status==='paused') extra='<button type="button" class="settings-strategy-link" onclick="activatePage(\'p-strategies\')">去策略工坊恢复</button>';
      else if(!checkable&&item.status!=='active') extra='<small class="setting-help">'+adaptiveEsc((STRATEGY_STATUS_LABELS[item.status]||item.status)+' · 不可勾选')+'</small>';
      return '<label data-testid="settings-strategy-'+adaptiveEsc(item.id)+'" class="settings-strategy-item'+(checkable?'':' settings-strategy-item-disabled')+'">'
        +'<input data-testid="settings-strategy-checkbox-'+adaptiveEsc(item.id)+'" type="checkbox" class="setting-strategy-enabled" value="'+adaptiveEsc(item.id)+'"'+settingsChecked(checked)+(checkable?'':' disabled')+'>'
        +'<span><b>'+adaptiveEsc(item.name||item.id)+'</b>'
        +(item.origin==='user'?'<small class="setting-help">自定义 · v'+(item.current_version||1)+'</small>':'')
        +'</span>'+badge+extra+'</label>';
    };
    var items=settingsStrategyItems();
    var builtinItems=items.filter(function(x){return x.origin==='builtin';});
    var userItems=items.filter(function(x){return x.origin==='user';});
    var strategyHtml='<p class="setting-help" style="margin:2px 0 6px">内置策略</p><div class="settings-strategy-group">'
      +builtinItems.map(strategyCheckbox).join('')+'</div>';
    if(userItems.length) strategyHtml+='<p class="setting-help" style="margin:8px 0 6px">我的策略（在策略工坊创建）</p><div class="settings-strategy-group">'+userItems.map(strategyCheckbox).join('')+'</div>';
    if(window._registryError) strategyHtml+='<p class="setting-help" style="color:var(--danger)">'+adaptiveEsc(window._registryError)+'</p>';
    strategyHtml+='<p class="setting-help" data-testid="settings-idle-note" style="margin-top:8px">一个都不勾 = 下一周期零策略 idle：不产生新开仓，风险扫描、存量退出与系统调度照常工作。</p>';
    html='<div class="settings-grid"><section class="settings-panel"><h3>模拟盘与资金</h3><p>这些项目决定下一次新周期的初始资金、观察时长和参与策略。当前周期的本金、成交与归档不会被覆盖。</p><div class="settings-form">'
      +settingsInputRow('default_starting_capital','默认启动金额','创建新周期时的共享资金池预填值。','<input id="settingDefaultCapital" data-settings-preview-input type="number" min="1000" max="10000000" step="1000" value="'+Number(sim.default_starting_capital||300000)+'"> <span class="setting-value-preview">元</span>')
      +settingsInputRow('cycle_duration_days','模拟周期','可选 15/30/60/90/180 个交易日或长期。','<select id="settingCycleDuration" data-settings-preview-input>'+durationOptions+'</select>')
      +'<div class="setting-row"><div class="setting-label"><b>下一周期启用策略</b><small>只有 Active 且支持新周期的策略可勾选；只对下一周期生效。</small></div><div class="setting-control settings-strategy-checks">'+strategyHtml+'</div></div>'
      +'</div><div class="settings-actions"><button data-testid="settings-save-simulation" onclick="saveSettingsSection(\'simulation\')">保存资金与周期</button><button class="ghost" onclick="resetSettingsSection(\'simulation\')">恢复默认</button></div><div class="settings-note">当前周期：'+adaptiveEsc(cur.status||'—')+'；已启用 '+(cur.enabled_strategies||[]).length+' 套策略。保存后点击“保存并启动新周期”才会切换账本。</div></section>'+settingsPreviewHtml(data,section)+'</div>';
  }else if(section==='risk'){
    html='<div class="settings-grid"><section class="settings-panel"><h3>仓位与风控</h3><p>风险边界设有后端白名单。系统永远不会因界面输入突破单票、共享池、现金、T+1 或交易所涨跌停门禁。</p><div class="settings-form">'
      +settingsInputRow('shared_pool_position_limit','共享池持仓上限','总池有效持仓席位，范围 1–30。','<input id="settingPoolLimit" data-settings-preview-input type="number" min="1" max="30" step="1" value="'+Number(risk.shared_pool_position_limit||15)+'"> <span class="setting-value-preview">席</span>')
      +settingsInputRow('shared_pool_exposure_cap','共享池敞口上限','持仓与待成交金额合计的硬上限，范围 35%–95%。','<input id="settingExposureCap" data-settings-preview-input type="number" min="35" max="95" step="1" value="'+(Number(risk.shared_pool_exposure_cap||.82)*100).toFixed(0)+'"> <span class="setting-value-preview">%</span>')
      +settingsInputRow('single_position_max_amount','单票最大金额','0 为自动按策略权重；大于 0 为额外绝对上限。','<input id="settingSingleMax" data-settings-preview-input type="number" min="0" max="10000000" step="100" value="'+Number(risk.single_position_max_amount||0)+'"> <span class="setting-value-preview">元</span>')
      +settingsInputRow('minimum_entry_slot_utilization','最小建仓席位利用率','动态建仓金额比例，范围 30%–80%；剩余空间交给风控加仓。','<input id="settingSlotUtilization" data-settings-preview-input type="number" min="30" max="80" step="1" value="'+(Number(risk.minimum_entry_slot_utilization||.6)*100).toFixed(0)+'"> <span class="setting-value-preview">%</span>')
      +'</div><div class="settings-actions"><button onclick="saveSettingsSection(\'risk\')">保存风控设置</button><button class="ghost" onclick="resetSettingsSection(\'risk\')">恢复默认</button></div><div class="settings-note">动态最小建仓金额会随周期金额、敞口上限、席位上限和利用率实时变化；不是固定的 10,000 元门槛。</div></section>'+settingsPreviewHtml(data,section)+'</div>';
  }else if(section==='strategy'){
    var overrides=strat.strategy_overrides||{};
    var builtinCard=function(item){
      var id=item.id;
      var itemOverride=overrides[id]||{};
      return '<article class="strategy-setting-card" data-strategy-id="'+adaptiveEsc(id)+'"><header><div><b>'+adaptiveEsc(item.name||id)+'</b><small>'+adaptiveEsc(id)+'</small></div><span class="tag tag-info">下一周期</span></header>'
        +'<div class="setting-control"><label>风格</label><select class="strategy-style"><option value="strong"'+(itemOverride.style==='strong'?' selected':'')+'>强势接力</option><option value="pullback"'+(itemOverride.style==='pullback'?' selected':'')+'>趋势回踩</option><option value="sector"'+(itemOverride.style==='sector'?' selected':'')+'>板块轮动</option><option value="quality"'+(itemOverride.style==='quality'?' selected':'')+'>质量突破</option><option value="main_force"'+(itemOverride.style==='main_force'?' selected':'')+'>主力跟随</option></select></div>'
        +'<div class="setting-control"><label>最大席位</label><input class="strategy-max-positions" type="number" min="1" max="6" step="1" value="'+Number(itemOverride.max_positions||3)+'"><span class="setting-value-preview">席</span></div>'
        +'<div class="setting-control"><label>单票权重</label><input class="strategy-max-weight" type="number" min="8" max="36" step="1" value="'+Number(itemOverride.max_weight_pct||32)+'"><span class="setting-value-preview">%</span></div>'
        +'<div class="setting-control"><label>策略敞口</label><input class="strategy-max-exposure" type="number" min="35" max="96" step="1" value="'+Number(itemOverride.max_exposure_pct||90)+'"><span class="setting-value-preview">%</span></div></article>';
    };
    var userCard=function(item){
      var runtime=item.runtime||{};
      var stage=runtime.runtime_ready?adaptiveEsc(runtime.lifecycle_stage||'—'):'—';
      var scale=(runtime.runtime_ready&&runtime.capital_scale!=null)?Math.round(runtime.capital_scale*100)+'%':'—';
      return '<article class="strategy-user-card" data-strategy-user-id="'+adaptiveEsc(item.id)+'"><header><div><b>'+adaptiveEsc(item.name||item.id)+'</b><small>'+adaptiveEsc(item.id)+' · v'+(item.current_version||1)+'</small></div>'+wbStatusBadge(item.status)+'</header>'
        +'<div class="strategy-preview-grid"><dt>生命周期阶段</dt><dd>'+stage+'</dd><dt>资金系数</dt><dd>'+scale+'</dd></div>'
        +'<p class="setting-help">风险画像与参数由策略工坊统一管理，这里只控制组合参与。</p>'
        +'<div class="settings-actions"><button class="ghost" onclick="activatePage(\'p-strategies\')">去策略工坊编辑</button></div></article>';
    };
    var allItems=settingsStrategyItems();
    var builtinItems=allItems.filter(function(x){return x.origin==='builtin';});
    var userItems=allItems.filter(function(x){return x.origin==='user';});
    var cardsHtml=builtinItems.map(builtinCard).join('');
    if(userItems.length) cardsHtml+='<h4 style="grid-column:1/-1;margin:4px 0 0">我的策略（参数在策略工坊维护）</h4>'+userItems.map(userCard).join('');
    html='<div class="settings-grid"><section class="settings-panel"><h3>策略运行参数</h3><p>内置策略保留独立模型和审计身份。风格、席位和权重参数在下个新周期初始化，当前持仓不会被强行改写。</p><div class="strategy-settings-grid">'+cardsHtml+'</div><div class="settings-actions"><button onclick="saveSettingsSection(\'strategy\')">保存策略参数</button><button class="ghost" onclick="resetSettingsSection(\'strategy\')">恢复默认</button></div></section>'+settingsPreviewHtml(data,section)+'</div>';
  }else if(section==='execution'){
    var exe=settings.execution||{}, exeDefaults=(data.defaults||{}).execution||{};
    var execSwitch=function(key,label,help){ var value=(exe[key]===undefined?exeDefaults[key]:exe[key]); return '<div class="setting-row"><div class="setting-label"><b>'+riskText(label)+'</b><small>'+riskText(help)+'</small></div><div class="setting-control"><label class="settings-exec-toggle"><input id="settingExec_'+key+'" type="checkbox"'+settingsChecked(!!value)+'><span>'+(value?'开启':'关闭')+'</span></label></div></div>'; };
    html='<div class="settings-grid"><section class="settings-panel"><h3>执行画像执行器</h3><p>控制批量窗口、人工核验与执行时限清扫三个执行器开关。关闭批量窗口后轮动委托按普通限价路径即时撮合；开启人工核验后事件画像的每笔买入都需要在「策略模拟 → 执行」页放行。</p><div class="settings-form">'
      +execSwitch('execution_batch_gate','批量撮合窗口','轮动画像委托挂起至收盘前批量窗口统一撮合；窗口内到达的委托仍立即成交。')
      +execSwitch('execution_verification_gate','事件人工核验','开启后事件画像的每笔买入都需人工放行；默认关闭，避免无人值守时账户停摆。')
      +execSwitch('execution_ttl_sweep','执行时限清扫','清扫到期挂起委托：严格时限画像作废，其余自动放回重试管道。')
      +'</div><div class="settings-actions"><button onclick="saveSettingsSection(\'execution\')">保存执行开关</button><button class="ghost" onclick="resetSettingsSection(\'execution\')">恢复默认</button></div>'
      +'<div class="settings-note">挂起委托不占用资金与席位；所有放行/驳回都写入审计。开关立即生效，无需重启扫描。</div></section>'+settingsPreviewHtml(data,section)+'</div>';
  }else if(section==='operator'){
    html='<div class="settings-grid"><section class="settings-panel"><h3>操作员授权</h3>'
      +'<p>本页只管理<strong>本标签页</strong>的写操作凭据，不涉及账户体系。凭据保存在 sessionStorage：本标签页刷新后保留，关闭标签页即消失。'
      +'只读接口（看板、行情、健康检查）不需要授权；写接口在未授权时会返回 401，前端不会自动重放，请解锁后重新点击原操作。</p>'
      +renderOperatorUnlockPanel()
      +'<div class="settings-note"><strong>边界说明：</strong>这是一道单操作员共享密钥边界，不是 RBAC；它不替代既有的风险与人工确认门禁，也不会把凭据写入日志或页面。</div>'
      +'</section>'+settingsPreviewHtml(data,section)+'</div>';
  }else{
    var ais=ai.settings||{}, review=ai.review||{}, reviewSlots=review.slots||{}, slotOrder=review.slot_order||['ai1','ai2'], reviewMode=review.review_mode==='single'?'single':'dual';
    var aiSlotCards=slotOrder.map(function(slot){
      var s=reviewSlots[slot]||{}, enabled=!!s.enabled;
      return '<div class="settings-panel" data-testid="ai-slot-'+slot+'" style="margin-top:14px;padding:14px">'
        +'<h4>'+adaptiveEsc(s.display_name||slot)+' <small class="setting-help">'+adaptiveEsc(slot)+'</small></h4>'
        +'<div class="setting-row"><div class="setting-label"><b>配置状态</b><small>页面永不回显明文 Key；Key 留空表示保持现有值。</small></div>'
        +'<div class="setting-control"><span data-testid="ai-slot-'+slot+'-status" class="setting-value-preview">'+(s.configured?('已配置 '+adaptiveEsc(s.key_preview||'')):'未配置')+'</span></div></div>'
        +'<div class="setting-control"><label for="aiSlot_'+slot+'_displayName">显示名称</label><input data-testid="ai-slot-'+slot+'-display-name" id="aiSlot_'+slot+'_displayName" type="text" maxlength="40" value="'+adaptiveEsc(s.display_name||'')+'" placeholder="自定义名称，与稳定 id 解耦"></div>'
        +'<div class="setting-control"><label for="aiSlot_'+slot+'_baseUrl">Base URL</label><input data-testid="ai-slot-'+slot+'-base-url" id="aiSlot_'+slot+'_baseUrl" type="url" maxlength="500" value="'+adaptiveEsc(s.base_url||'')+'" placeholder="https://example.com/v1"></div>'
        +'<div class="setting-control"><label for="aiSlot_'+slot+'_model">模型名称</label><input data-testid="ai-slot-'+slot+'-model" id="aiSlot_'+slot+'_model" type="text" maxlength="100" value="'+adaptiveEsc(s.model||'')+'" placeholder="由你填写，后端不做任何厂商假设"></div>'
        +'<div class="setting-control"><label for="aiSlot_'+slot+'_apiKey">API Key</label><input data-testid="ai-slot-'+slot+'-api-key" id="aiSlot_'+slot+'_apiKey" type="password" autocomplete="new-password" placeholder="留空表示保持现有 Key"></div>'
        +'<div class="setting-control"><label for="aiSlot_'+slot+'_timeout">超时（秒）</label><input data-testid="ai-slot-'+slot+'-timeout" id="aiSlot_'+slot+'_timeout" type="number" min="5" max="300" step="1" value="'+Number(s.timeout_seconds||40)+'"></div>'
        +'<div class="setting-control"><label class="settings-exec-toggle"><input data-testid="ai-slot-'+slot+'-enabled" id="aiSlot_'+slot+'_enabled" type="checkbox"'+settingsChecked(enabled)+'><span>'+(enabled?'启用':'停用')+'</span></label></div>'
        +'<div class="settings-actions">'
        +'<button data-testid="ai-slot-'+slot+'-save" onclick="saveAiSlot(\''+slot+'\')">保存该槽位</button>'
        +'<button class="ghost" data-testid="ai-slot-'+slot+'-test" onclick="testAiSlot(\''+slot+'\')">测试连接</button>'
        +'<button class="ghost" data-testid="ai-slot-'+slot+'-clear" onclick="clearAiSlotKey(\''+slot+'\')">清除 Key</button>'
        +'</div>'
        +'<div class="setting-help" data-testid="ai-slot-'+slot+'-feedback"></div>'
        +'</div>';
    }).join('');
    var singleSlotOptions=slotOrder.map(function(slot){
      var s=reviewSlots[slot]||{};
      return '<option value="'+slot+'"'+(review.single_reviewer_slot===slot?' selected':'')+'>'+adaptiveEsc((s.display_name||slot)+' · '+slot)+'</option>';
    }).join('');
    html='<div class="settings-grid"><section class="settings-panel"><h3>AI 审核</h3>'
      +'<p>这里管理<strong>两个完全独立</strong>的 AI 槽位与审核模式。名称、地址、模型全部由你填写；后端不认识任何厂商身份，请求体是最小公共的 OpenAI 兼容格式。</p>'
      +'<div class="settings-form">'
      +settingsInputRow('evolution_interval_hours','自进化周期','收盘学习任务之间的最短间隔，范围 1–168 小时。','<input id="settingEvolutionInterval" type="number" min="1" max="168" step="1" value="'+Number(evo.evolution_interval_hours||24)+'"> <span class="setting-value-preview">小时</span>')
      +'<div class="setting-row"><div class="setting-label"><b>审核模式</b><small>单AI：只用选定槽位，结果仅供参考、永不构成共识。双AI：两个槽位独立分析后过共识门禁，缺一个即整体拦截，绝不降级成单AI。</small></div>'
      +'<div class="setting-control"><label><input data-testid="ai-review-mode-single" type="radio" name="settingReviewMode" value="single"'+(reviewMode==='single'?' checked':'')+'><span>单AI审阅</span></label>'
      +'<label><input data-testid="ai-review-mode-dual" type="radio" name="settingReviewMode" value="dual"'+(reviewMode==='dual'?' checked':'')+'><span>双AI共识</span></label></div></div>'
      +'<div class="setting-row" id="settingSingleSlotRow" data-testid="ai-review-single-slot-row"><div class="setting-label"><b>单AI审阅槽位</b><small>只在“单AI审阅”模式下生效，双AI模式不使用该项。</small></div>'
      +'<div class="setting-control"><select data-testid="ai-review-single-slot" id="settingSingleReviewerSlot">'+singleSlotOptions+'</select>'
      +'<span data-testid="ai-review-readiness" class="setting-value-preview">'+(reviewMode==='single'?(review.single_ready?'就绪':'未就绪'):(review.dual_ready?'双AI就绪':'双AI未就绪'))+'</span></div></div>'
      +'<div class="setting-row"><div class="setting-label"><b>AI运行开关</b><small>只影响审阅/候选生成，不直接下单。</small></div><div class="setting-control"><label><input id="settingAiAdvisor" type="checkbox"'+settingsChecked(ais.llm_advisor_enabled)+'><span>启用 AI 审阅</span></label><label><input id="settingAiRealtime" type="checkbox"'+settingsChecked(ais.llm_realtime_tuning_enabled)+'><span>启用有界调参</span></label><label><input type="checkbox" checked disabled><span>人工确认候选（强制）</span></label></div></div>'
      +'<div class="setting-row"><div class="setting-label"><b>调参模式</b><small>shadow 最稳，intraday 更及时，close 只在收盘运行。</small></div><div class="setting-control"><select id="settingAiMode"><option value="shadow"'+(ais.llm_realtime_mode==='shadow'?' selected':'')+'>shadow · 只观察</option><option value="intraday"'+(ais.llm_realtime_mode==='intraday'?' selected':'')+'>intraday · 盘中候选</option><option value="close"'+(ais.llm_realtime_mode==='close'?' selected':'')+'>close · 收盘候选</option></select></div></div>'
      +'</div><div class="settings-actions"><button data-testid="settings-save-ai-review" onclick="saveAiReviewMode()">保存审核模式</button><button class="ghost" data-testid="settings-save-ai-switches" onclick="saveSettingsSection(\'evolution\')">保存 AI 开关</button><button class="ghost" onclick="resetSettingsSection(\'evolution\')">恢复默认</button></div>'
      +'<div class="settings-note"><strong>安全边界：</strong>单AI审阅的运行状态是 <code>single_review</code>，不是 <code>consensus</code>，因此永远无法穿过人工应用门禁；双AI的共识提案仍需人工确认后才生效。自进化周期只控制后台学习频率，不改变已启用策略的交易硬规则。</div>'
      + aiSlotCards
      +'</section>'+settingsPreviewHtml(data,section)+'</div>';
  }
  target.innerHTML=html;
  document.querySelectorAll('#p-settings .settings-section-tab').forEach(function(item){var active=item.dataset.settingsSection===section;item.classList.toggle('active',active);item.setAttribute('aria-selected',active?'true':'false');});
  document.querySelectorAll('#p-settings [data-settings-preview-input]').forEach(function(item){item.addEventListener('input',updateSettingsPreview);item.addEventListener('change',updateSettingsPreview);});
  document.querySelectorAll('#p-settings input[name="settingReviewMode"]').forEach(function(item){item.addEventListener('change',refreshAiReviewModeUi);});
  refreshAiReviewModeUi();
  updateSettingsPreview();
  var head=$('settingsHeadState'); if(head) head.textContent='默认安全边界已加载 · 最近审计 '+(Array.isArray(data.audit)?data.audit.length:0)+' 条';
}

export function setSettingsSection(section,button){
  if(['simulation','risk','strategy','evolution','execution','operator'].indexOf(section)<0) section='simulation';
  window._settingsSection=section; sessionStorage.setItem(SETTINGS_SECTION_KEY,section);
  document.querySelectorAll('#p-settings .settings-section-tab').forEach(function(item){var active=item.dataset.settingsSection===section;item.classList.toggle('active',active);item.setAttribute('aria-selected',active?'true':'false');});
  if(window._settingsPayload) renderSettings(window._settingsPayload);
  refreshOperatorUnlockState();
  history.replaceState(null,'','#settings/'+section);
}

export function updateSettingsPreview(){
  var node=$('settingsMinimumPreview'); if(!node) return;
  var data=window._settingsPayload||{}, current=((data.effective||{}).current_cycle)||{}, capital=Number(($('settingDefaultCapital')||{}).value)||Number(current.capital)||300000;
  var slots=Number(($('settingPoolLimit')||{}).value)||Number(((data.settings||{}).risk||{}).shared_pool_position_limit)||15;
  var exposure=Number(($('settingExposureCap')||{}).value)/100||Number(((data.settings||{}).risk||{}).shared_pool_exposure_cap)||.82;
  var utilization=Number(($('settingSlotUtilization')||{}).value)/100||Number(((data.settings||{}).risk||{}).minimum_entry_slot_utilization)||.6;
  var amount=Math.max(100,Math.floor((capital*exposure/Math.max(1,slots)*utilization)/100)*100);
  node.textContent=settingsCurrency(amount);
}

export async function loadSettings(force){
  if(window._settingsLoading&&!force) return window._settingsLoading;
  window._settingsLoading=(async function(){
    try{
      // PR-47：策略集合与设置并行读取（Registry 是策略集合唯一权威）。
      // PR-49：并行取后端发布标识，用于界面版本核验。
      var results=await Promise.all([
        api('/api/settings/?_='+Date.now()),
        api('/api/strategies?include_archived=true&_='+Date.now()).catch(function(){ return null; }),
        api('/api/version?_='+Date.now()).catch(function(){ return null; }),
      ]);
      window._appBuild=(results[2]&&results[2].build)||'';
      var data=results[0];
      if(results[1]&&Array.isArray(results[1].items)){
        window._registryItems=results[1].items;
        window._registrySummary=results[1].summary||{};
        window._registryError=null;
      }else{
        window._registryItems=null; window._registryError='策略注册表读取失败，已回退到内置策略展示。';
      }
      renderSettings(data);
      return data;
    }catch(e){
      var target=$('settingsResult');if(target) target.innerHTML='<div class="settings-note">设置读取失败：'+adaptiveEsc(e.message||e)+'</div>';throw e;
    }finally{window._settingsLoading=null;}
  })();
  return window._settingsLoading;
}

// PR-47：Registry 不可用时的兜底显示名单（仅用于渲染，不再是策略集合权威）。
export function settingsStrategyItems(){
  if(Array.isArray(window._registryItems)&&window._registryItems.length){
    return window._registryItems.filter(function(item){return item.status!=='archived';});
  }
  return Object.keys(SETTINGS_STRATEGY_NAMES).map(function(id){
    return {id:id,name:SETTINGS_STRATEGY_NAMES[id],origin:'builtin',status:'active',supports_new_cycle:true,current_version:1,metadata:{},description:''};
  });
}

export function collectStrategyOverrides(){
  var result={}; document.querySelectorAll('#p-settings .strategy-setting-card').forEach(function(card){var id=card.dataset.strategyId;result[id]={style:card.querySelector('.strategy-style').value,max_positions:Number(card.querySelector('.strategy-max-positions').value),max_weight_pct:Number(card.querySelector('.strategy-max-weight').value),max_exposure_pct:Number(card.querySelector('.strategy-max-exposure').value)};}); return result;
}

export async function saveSettingsSection(section){
  var saveAnswer=await confirmDialog({
    kicker:'设置 · 写入审计',
    title:'保存“'+({simulation:'模拟盘与资金',risk:'仓位与风控',strategy:'策略参数',evolution:'AI与自进化',execution:'执行画像执行器'}[section]||section)+'”设置？',
    bullets:['后端会校验取值范围，越界会被拒绝。','本次修改只影响下一周期，不改写当前账本。','保存会写入审计记录。'],
    confirmText:'保存',
  });
  if(!saveAnswer.approved) return;
  var payload={};
  if(section==='simulation') payload.simulation={default_starting_capital:Number($('settingDefaultCapital').value),cycle_duration_days:Number($('settingCycleDuration').value),enabled_strategies:Array.prototype.slice.call(document.querySelectorAll('.setting-strategy-enabled:checked')).map(function(item){return item.value;})};
  if(section==='risk') payload.risk={shared_pool_position_limit:Number($('settingPoolLimit').value),shared_pool_exposure_cap:Number($('settingExposureCap').value)/100,single_position_max_amount:Number($('settingSingleMax').value),minimum_entry_slot_utilization:Number($('settingSlotUtilization').value)/100};
  if(section==='strategy') payload.strategy={strategy_overrides:collectStrategyOverrides()};
  if(section==='evolution') payload.evolution={evolution_interval_hours:Number($('settingEvolutionInterval').value)};
  if(section==='execution') payload.execution={execution_batch_gate:!!($('settingExec_execution_batch_gate')||{}).checked,execution_verification_gate:!!($('settingExec_execution_verification_gate')||{}).checked,execution_ttl_sweep:!!($('settingExec_execution_ttl_sweep')||{}).checked}; payload.ai=section==='evolution'?{llm_advisor_enabled:$('settingAiAdvisor').checked,llm_realtime_tuning_enabled:$('settingAiRealtime').checked,llm_realtime_mode:$('settingAiMode').value}:undefined;
  try{var data=await apiPostJson('/api/settings/?confirmed=true',payload);renderSettings(data);toast('设置已保存并记录审计。');if(section==='risk'&&typeof loadPaper==='function') loadPaper({force:true});}catch(e){toast('保存失败：'+((e&&e.message)||e), { tone: 'danger' });}
}

export async function resetSettingsSection(section){
  var defaults=(window._settingsPayload||{}).defaults||{};
  var resetAnswer=await confirmDialog({
    kicker:'设置 · 恢复默认',
    title:'恢复该分组的默认安全设置？',
    bullets:['仅当前分组回到默认值，其他分组不受影响。','确认后会立即写入后端并留下审计（无需再点保存）。'],
    confirmText:'恢复默认',
  });
  if(!resetAnswer.approved) return;
  var value=defaults[section]; if(!value) return;
  var payload={}; if(section==='strategy') payload.strategy={strategy_overrides:value.strategy_overrides}; else payload[section]=value;
  try{var data=await apiPostJson('/api/settings/?confirmed=true',payload);renderSettings(data);toast('已恢复该分组默认值。');}catch(e){inlineError($('settingsResult'), e, { title:'恢复默认失败', retryLabel:'重试', onRetry:function(){ resetSettingsSection(section); } });}
}

// ---------- 通用 AI 槽位（ai1 / ai2）----------
// 全部走 /api/settings/ai-review*：GET 永不回显明文 Key，保存时空 Key = 保持旧 Key，
// 清空必须显式走“清除 Key”按钮（后端 clear_api_key=true）。
export function aiSlotFeedback(slot,message,tone){
  var node=document.querySelector('[data-testid="ai-slot-'+slot+'-feedback"]');
  if(!node) return;
  node.textContent=message||'';
  node.style.color=tone==='danger'?'var(--danger)':(tone==='ok'?'var(--up,#c0392b)':'');
}

export function refreshAiReviewModeUi(){
  var single=document.querySelector('#p-settings input[name="settingReviewMode"][value="single"]');
  var row=$('settingSingleSlotRow');
  if(row) row.style.display=(single&&single.checked)?'':'none';
}

export async function saveAiReviewMode(){
  var picked=document.querySelector('#p-settings input[name="settingReviewMode"]:checked');
  var mode=picked?picked.value:'dual';
  var slotNode=$('settingSingleReviewerSlot');
  var answer=await confirmDialog({
    kicker:'设置 · AI 审核模式',
    title:'切换为“'+(mode==='single'?'单AI审阅':'双AI共识')+'”？',
    bullets:['单AI审阅结果标记为 single_review，仅供参考，永远不构成共识。','双AI共识要求两个槽位都配置且启用；缺一个就整体 fail-closed。','不会关闭人工确认门禁，也不会改变交易硬规则。'],
    confirmText:'切换模式',
  });
  if(!answer.approved) return;
  var body={review_mode:mode};
  if(mode==='single'&&slotNode&&slotNode.value) body.single_reviewer_slot=slotNode.value;
  try{await apiJson('/api/settings/ai-review?confirmed=true','PUT',body);await loadSettings(true);toast('AI 审核模式已保存。');}
  catch(e){toast('保存失败：'+((e&&e.message)||e), { tone: 'danger' });}
}

export async function saveAiSlot(slot){
  var read=function(suffix){var node=$('aiSlot_'+slot+'_'+suffix);return node?String(node.value||'').trim():'';};
  var enabledNode=$('aiSlot_'+slot+'_enabled');
  var body={
    display_name:read('displayName'),
    base_url:read('baseUrl'),
    model:read('model'),
    enabled:!!(enabledNode&&enabledNode.checked),
  };
  var timeoutRaw=read('timeout');
  if(timeoutRaw) body.timeout_seconds=Number(timeoutRaw);
  var apiKey=read('apiKey');
  if(apiKey) body.api_key=apiKey;
  var answer=await confirmDialog({
    kicker:'设置 · AI 槽位 '+slot,
    title:'保存槽位 '+slot+' 的接口配置？',
    bullets:['API Key 只写入后端安全存储，页面和日志不回显明文。','留空 Key 表示保持现有 Key；要清空请用“清除 Key”。','两个槽位完全独立，保存本槽位不会改动另一个。'],
    confirmText:'保存槽位',
  });
  if(!answer.approved) return;
  try{
    await apiJson('/api/settings/ai-review/slots/'+slot+'?confirmed=true','PUT',body);
    await loadSettings(true);
    toast('槽位 '+slot+' 已保存（页面仅显示掩码状态）。');
  }catch(e){
    aiSlotFeedback(slot,'保存失败：'+((e&&e.message)||e),'danger');
    toast('保存失败：'+((e&&e.message)||e), { tone: 'danger' });
  }
}

export async function clearAiSlotKey(slot){
  var answer=await confirmDialog({
    kicker:'设置 · 清除 Key',
    title:'清除槽位 '+slot+' 的 API Key？',
    bullets:['清除后该槽位变为未配置：单AI模式 fail-closed，双AI模式整体阻断。','地址、模型、开关与超时保持不变。','清除立即生效，无需重启扫描。'],
    confirmText:'清除 Key',
  });
  if(!answer.approved) return;
  try{
    await apiJson('/api/settings/ai-review/slots/'+slot+'?confirmed=true','PUT',{clear_api_key:true});
    await loadSettings(true);
    toast('已清除槽位 '+slot+' 的 API Key。');
  }catch(e){toast('清除失败：'+((e&&e.message)||e), { tone: 'danger' });}
}

export async function testAiSlot(slot){
  aiSlotFeedback(slot,'正在测试连接…');
  try{
    var result=await apiJson('/api/settings/ai-review/slots/'+slot+'/test?confirmed=true','POST');
    if(result&&result.ok){
      aiSlotFeedback(slot,'连接正常 · '+Number(result.latency_ms||0)+' ms（模型 '+(result.model||'—')+'）','ok');
      toast('槽位 '+slot+' 连接正常。');
    }else{
      aiSlotFeedback(slot,'连接失败：'+String((result&&result.error)||'unknown'),'danger');
      toast('槽位 '+slot+' 连接失败。', { tone: 'danger' });
    }
  }catch(e){
    aiSlotFeedback(slot,'测试失败：'+((e&&e.message)||e),'danger');
    toast('测试失败：'+((e&&e.message)||e), { tone: 'danger' });
  }
}

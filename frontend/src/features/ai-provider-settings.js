import { apiJson as api } from "../core/api.js";
import { $ } from "../core/dom.js";
import { adaptiveEsc as esc } from "../core/format.js";
import { confirmDialog, toast } from "../ui/dialog.js";

function reviewSettings(){ return ((window._settingsPayload||{}).ai||{}).review||{}; }
function providers(){ return reviewSettings().providers||[]; }
function providerById(id){ return providers().find(function(p){ return p.id===id; }); }

function options(items,value,empty){
  return '<option value="">'+esc(empty)+'</option>'+items.map(function(item){
    var id=typeof item==='string'?item:item.id;
    var label=typeof item==='string'?item:item.display_name;
    return '<option value="'+esc(id)+'"'+(id===value?' selected':'')+'>'+esc(label)+'</option>';
  }).join('');
}

export function renderProviderLibrary(review){
  return '<section class="ai-provider-library" data-testid="ai-provider-library"><h4>1. 保存提供商</h4>'
    +'<p class="setting-help">在这里统一保存名称、接口地址和密钥，供 AI1、AI2 选择。接口需支持 OpenAI 兼容格式，也就是使用相同的请求方式。</p>'
    +'<div class="ai-slot-form">'
    +'<div class="setting-row"><div class="setting-label"><label for="aiProviderEditor"><b>编辑提供商</b></label><small>可保存多个提供商，随时切换编辑。</small></div><div class="setting-control"><select id="aiProviderEditor" data-testid="ai-provider-editor" onchange="editAiProvider()">'+options(review.providers||[],'','新增提供商')+'</select></div></div>'
    +'<div class="setting-row"><div class="setting-label"><label for="aiProviderName"><b>提供商名称</b></label></div><div class="setting-control"><input id="aiProviderName" data-testid="ai-provider-name" type="text" maxlength="40" placeholder="填写便于识别的名称"></div></div>'
    +'<div class="setting-row"><div class="setting-label"><label for="aiProviderUrl"><b>接口地址</b></label></div><div class="setting-control"><input id="aiProviderUrl" data-testid="ai-provider-url" type="url" maxlength="500" placeholder="填写提供商的完整接口地址"></div></div>'
    +'<div class="setting-row"><div class="setting-label"><label for="aiProviderKey"><b>API Key（密钥）</b></label><small>修改时留空可保留已保存的密钥。</small></div><div class="setting-control"><input id="aiProviderKey" data-testid="ai-provider-key" type="password" autocomplete="new-password" maxlength="200" placeholder="输入密钥；页面不会回显"><span id="aiProviderKeyStatus" class="setting-value-preview">未配置密钥</span></div></div>'
    +'<div class="setting-row"><div class="setting-label"><label for="aiProviderTimeout"><b>超时（秒）</b></label></div><div class="setting-control"><input id="aiProviderTimeout" data-testid="ai-provider-timeout" type="number" min="5" max="300" step="1" value="40"></div></div>'
    +'<div class="setting-row"><div class="setting-label"><label for="aiProviderModels"><b>可选模型</b></label><small>每行一个模型名称。保存提供商后，也可点击“获取模型”。</small></div><div class="setting-control"><textarea id="aiProviderModels" data-testid="ai-provider-models" rows="3" placeholder="每行填写一个模型名称"></textarea></div></div>'
    +'<div class="settings-actions"><button type="button" data-testid="ai-provider-save" onclick="saveAiProvider()">保存提供商</button><button type="button" class="ghost" id="aiProviderRefresh" data-testid="ai-provider-refresh" disabled onclick="refreshAiProviderModels()">获取模型</button><button type="button" class="ghost" id="aiProviderClear" disabled onclick="clearAiProviderKey()">清除密钥</button></div>'
    +'<div id="aiProviderFeedback" class="setting-help" role="status" aria-live="polite"></div></div></section>';
}

export function renderAiSlotCards(review){
  return '<section class="ai-slot-selection"><h4>2. 为 AI 选择提供商与模型</h4>'
    +'<p class="setting-help">先保存上方提供商和模型，再分别选择。两路 AI 可以使用相同或不同的提供商。</p>'
    +(review.slot_order||['ai1','ai2']).map(function(slot){
      var s=(review.slots||{})[slot]||{}, provider=(review.providers||[]).find(function(p){return p.id===s.provider_id;});
      var models=provider?(provider.models||[]).slice():[];
      if(provider&&s.model&&models.indexOf(s.model)<0) models.push(s.model);
      return '<div class="settings-panel ai-slot-form" data-testid="ai-slot-'+slot+'"><h4>'+esc(slot.toUpperCase())+' 设置</h4>'
        +'<div class="setting-row"><div class="setting-label"><label for="aiSlot_'+slot+'_provider"><b>提供商</b></label></div><div class="setting-control"><select id="aiSlot_'+slot+'_provider" data-testid="ai-slot-'+slot+'-provider" onchange="changeAiSlotProvider(\''+slot+'\')">'+options(review.providers||[],s.provider_id,'请选择已保存的提供商')+'</select></div></div>'
        +'<div class="setting-row"><div class="setting-label"><label for="aiSlot_'+slot+'_model"><b>模型</b></label></div><div class="setting-control"><select id="aiSlot_'+slot+'_model" data-testid="ai-slot-'+slot+'-model"'+(!models.length?' disabled':'')+'>'+options(models,s.model,provider?'请选择模型':'请先选择提供商')+'</select></div></div>'
        +'<div class="setting-row"><div class="setting-label"><b>启用 AI</b></div><div class="setting-control"><label class="settings-exec-toggle"><input id="aiSlot_'+slot+'_enabled" data-testid="ai-slot-'+slot+'-enabled" type="checkbox"'+(s.enabled?' checked':'')+'>启用</label><span data-testid="ai-slot-'+slot+'-status" class="setting-value-preview">'+(s.ready?'已就绪':(s.enabled?'未就绪':'已停用'))+'</span></div></div>'
        +'<div class="settings-actions"><button type="button" data-testid="ai-slot-'+slot+'-save" onclick="saveAiSlot(\''+slot+'\')">保存 '+esc(slot.toUpperCase())+'</button><button type="button" class="ghost" data-testid="ai-slot-'+slot+'-test" onclick="testAiSlot(\''+slot+'\')">测试已保存配置</button></div>'
        +'<div class="setting-help" data-testid="ai-slot-'+slot+'-feedback" role="status" aria-live="polite"></div></div>';
    }).join('')+'</section>';
}

function feedback(message){ var node=$('aiProviderFeedback'); if(node) node.textContent=message; }

export function applyAiReviewSettings(review,savedSlot,rerender){
  var data=window._settingsPayload||{};
  var previous=reviewSettings(), drafts=[];
  ['ai1','ai2'].forEach(function(slot){
    var p=$('aiSlot_'+slot+'_provider'), m=$('aiSlot_'+slot+'_model'), e=$('aiSlot_'+slot+'_enabled');
    var saved=(previous.slots||{})[slot]||{};
    if(p&&m&&e&&slot!==savedSlot&&(p.value!==(saved.provider_id||'')||m.value!==(saved.model||'')||e.checked!==!!saved.enabled)){
      drafts.push({slot:slot,provider:p.value,model:m.value,enabled:e.checked});
    }
  });
  rerender(Object.assign({},data,{ai:Object.assign({},data.ai,{review:review})}));
  drafts.forEach(function(draft){
    $('aiSlot_'+draft.slot+'_provider').value=draft.provider;
    changeAiSlotProvider(draft.slot);
    $('aiSlot_'+draft.slot+'_model').value=draft.model;
    $('aiSlot_'+draft.slot+'_enabled').checked=draft.enabled;
  });
}

export function editAiProvider(){
  var p=providerById($('aiProviderEditor').value)||{};
  $('aiProviderName').value=p.display_name||'';
  $('aiProviderUrl').value=p.base_url||'';
  $('aiProviderKey').value='';
  $('aiProviderKeyStatus').textContent=p.configured?'已保存密钥（不回显）':'未配置密钥';
  $('aiProviderTimeout').value=p.timeout_seconds||40;
  $('aiProviderModels').value=(p.models||[]).join('\n');
  $('aiProviderRefresh').disabled=!p.id;
  $('aiProviderClear').disabled=!p.configured;
  feedback(p.id?'修改提供商后，使用它的 AI 会同步采用新地址、密钥和超时。':'填写配置后点击“保存提供商”。');
}

export function changeAiSlotProvider(slot){
  var p=providerById($('aiSlot_'+slot+'_provider').value), select=$('aiSlot_'+slot+'_model');
  var models=p?(p.models||[]).slice():[], previous=select.value;
  var saved=(reviewSettings().slots||{})[slot]||{};
  if(p&&p.id===saved.provider_id&&saved.model&&models.indexOf(saved.model)<0) models.push(saved.model);
  select.innerHTML=options(models,models.indexOf(previous)>=0?previous:(models.length===1?models[0]:''),p?'请选择模型':'请先选择提供商');
  select.disabled=!models.length;
  var node=document.querySelector('[data-testid="ai-slot-'+slot+'-feedback"]');
  if(node) node.textContent=p?(!models.length?'请先在提供商中填写或获取模型。':'选择后点击保存生效。'):'';
}

export async function persistAiProvider(onSaved){
  var id=$('aiProviderEditor').value;
  var body={display_name:$('aiProviderName').value.trim(),base_url:$('aiProviderUrl').value.trim(),
    timeout_seconds:Number($('aiProviderTimeout').value),models:$('aiProviderModels').value.split(/\r?\n/).map(function(s){return s.trim();}).filter(Boolean)};
  var key=$('aiProviderKey').value.trim(); if(key) body.api_key=key;
  if(!body.display_name||!body.base_url){ feedback('请填写提供商名称和接口地址。'); return; }
  if(!Number.isInteger(body.timeout_seconds)||body.timeout_seconds<5||body.timeout_seconds>300){ feedback('超时请填写5–300之间的整数。'); return; }
  var answer=await confirmDialog({title:'保存提供商配置？',detail:'使用该提供商的 AI 将同步采用保存后的地址、密钥和超时。',confirmText:'保存提供商'});
  if(!answer.approved) return;
  var button=document.querySelector('[data-testid="ai-provider-save"]'); button.disabled=true;
  feedback('正在保存提供商…');
  try{
    var result=await api('/api/settings/ai-review/providers'+(id?'/'+encodeURIComponent(id):'')+'?confirmed=true',id?'PUT':'POST',body);
    onSaved(result.review);
    $('aiProviderEditor').value=result.provider.id; editAiProvider();
    feedback('提供商已保存；现在可获取模型，或为 AI1、AI2 选择提供商和模型。');
  }catch(e){ feedback('保存失败：'+e.message); }
  finally{ if(button.isConnected) button.disabled=false; }
}

export async function refreshAiProviderModels(){
  var id=$('aiProviderEditor').value; if(!id) return;
  var p=providerById(id);
  if($('aiProviderUrl').value.trim()!==p.base_url||$('aiProviderKey').value.trim()){
    feedback('请先保存修改后的地址和密钥，再获取模型。'); return;
  }
  var button=$('aiProviderRefresh'); button.disabled=true; feedback('正在读取已保存提供商的可用模型…');
  try{
    var result=await api('/api/settings/ai-review/providers/'+encodeURIComponent(id)+'/models/refresh?confirmed=true','POST');
    if(!result.ok){ feedback('获取模型失败：'+result.error+'。可在上方手动填写模型并保存。'); return; }
    p.models=result.models;
    // 仅更新相应下拉列表，保留其它未保存的选择和编辑内容。
    if($('aiProviderEditor').value===id){
      var manual=$('aiProviderModels').value.split(/\r?\n/).map(function(s){return s.trim();}).filter(Boolean);
      $('aiProviderModels').value=Array.from(new Set(manual.concat(result.models))).join('\n');
      feedback(result.models.length?'已读取 '+result.models.length+' 个模型。':'上游未返回模型，请手动填写模型并保存。');
    }
    ['ai1','ai2'].forEach(function(slot){if($('aiSlot_'+slot+'_provider').value===id) changeAiSlotProvider(slot);});
  }catch(e){ if($('aiProviderEditor').value===id) feedback('获取模型失败：'+e.message+'。可手动填写模型并保存。'); }
  finally{ if(button.isConnected) button.disabled=!$('aiProviderEditor').value; }
}

export async function clearAiProviderSecret(onSaved){
  var id=$('aiProviderEditor').value; if(!id) return;
  var answer=await confirmDialog({title:'清除该提供商的密钥？',detail:'所有使用该提供商的 AI 都将无法调用；其它提供商保持原状。',confirmText:'清除密钥'});
  if(!answer.approved) return;
  try{
    var result=await api('/api/settings/ai-review/providers/'+encodeURIComponent(id)+'?confirmed=true','PUT',{clear_api_key:true});
    onSaved(result.review); $('aiProviderEditor').value=id; editAiProvider(); toast('提供商密钥已清除。');
  }catch(e){ feedback('清除失败：'+e.message); }
}

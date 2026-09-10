/* PR-55：由 frontend/app.js 拆分（纯搬运，逻辑/文案未改） */
// 跨模块依赖（由原单文件作用域推导）
import { adaptiveEsc } from "../core/format.js";

export function adaptiveConfirm(options){
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

export function adaptiveActionNotice(title,detail){
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

export function settingsConfirm(message){ return window.confirm(message); }

/* PR-55：由 frontend/app.js 拆分（纯搬运，逻辑/文案未改）
   PR-57：扩展为应用内**唯一**的对话/反馈层。
   - toast(...)          成功或非危险动作的轻量反馈（aria-live）
   - confirmDialog(...)  危险/高影响动作的应用内模态（焦点陷阱 + Escape + 语义按钮）
   - inlineError(...)    校验/版本/风险错误的内联错误面板（可带重试）
   - promptDialog(...)   需要用户输入一个值的模态（替代 window.prompt）
   adaptiveConfirm / adaptiveActionNotice / settingsConfirm 保留为兼容包装，
   内部全部走这里，避免出现第二套对话框实现。 */
import { adaptiveEsc } from "../core/format.js";

var FOCUSABLE = 'button:not([disabled]),[href],input:not([disabled]),select:not([disabled]),textarea:not([disabled]),[tabindex]:not([tabindex="-1"])';

function trapFocus(mask) {
  function onKeydown(event) {
    if (event.key === 'Escape') {
      event.preventDefault();
      var cancel = mask.querySelector('[data-action="cancel"]') || mask.querySelector('[data-action="close"]');
      if (cancel) cancel.click();
      return;
    }
    if (event.key !== 'Tab') return;
    var nodes = Array.prototype.slice.call(mask.querySelectorAll(FOCUSABLE)).filter(function (n) { return n.offsetParent !== null; });
    if (!nodes.length) return;
    var first = nodes[0], last = nodes[nodes.length - 1];
    if (event.shiftKey && document.activeElement === first) { event.preventDefault(); last.focus(); }
    else if (!event.shiftKey && document.activeElement === last) { event.preventDefault(); first.focus(); }
  }
  mask.addEventListener('keydown', onKeydown);
  return function () { mask.removeEventListener('keydown', onKeydown); };
}

/** 轻量成功/信息提示。不阻塞、可关闭、对读屏友好。 */
export function toast(message, options) {
  options = options || {};
  var tone = options.tone === 'danger' ? 'danger' : (options.tone === 'warn' ? 'warn' : 'ok');
  var host = document.getElementById('appToastHost');
  if (!host) {
    host = document.createElement('div');
    host.id = 'appToastHost';
    host.className = 'app-toast-host';
    host.setAttribute('role', 'status');
    host.setAttribute('aria-live', 'polite');
    document.body.appendChild(host);
  }
  var node = document.createElement('div');
  node.className = 'app-toast app-toast-' + tone;
  node.setAttribute('data-testid', 'app-toast');
  node.innerHTML = '<span class="app-toast-glyph" aria-hidden="true">' + (tone === 'ok' ? '✓' : tone === 'warn' ? '!' : '✕') + '</span>'
    + '<span class="app-toast-text">' + adaptiveEsc(message || '') + '</span>'
    + '<button type="button" class="app-toast-close" aria-label="关闭提示">×</button>';
  node.querySelector('.app-toast-close').onclick = function () { node.remove(); };
  host.appendChild(node);
  var ttl = options.timeout == null ? 4200 : options.timeout;
  if (ttl > 0) window.setTimeout(function () { node.remove(); }, ttl);
  return node;
}

/** 内联错误面板：错误文案 + 可选重试。绝不静默失败、绝不白屏。 */
export function inlineError(container, message, options) {
  options = options || {};
  var target = typeof container === 'string' ? document.getElementById(container) : container;
  var text = typeof message === 'string' ? message : (message && message.message) || '操作失败';
  if (!target) { toast(text, { tone: 'danger' }); return null; }
  var panel = document.createElement('div');
  panel.className = 'app-inline-error';
  panel.setAttribute('role', 'alert');
  panel.setAttribute('data-testid', options.testid || 'app-inline-error');
  panel.innerHTML = '<span class="app-inline-error-glyph" aria-hidden="true">!</span>'
    + '<div class="app-inline-error-body"><b>' + adaptiveEsc(options.title || '操作未完成') + '</b><p>' + adaptiveEsc(text) + '</p></div>'
    + (options.retryLabel ? '<button type="button" class="app-inline-error-retry">' + adaptiveEsc(options.retryLabel) + '</button>' : '')
    + '<button type="button" class="app-inline-error-close" aria-label="关闭错误提示">×</button>';
  panel.querySelector('.app-inline-error-close').onclick = function () { panel.remove(); };
  if (options.retryLabel && typeof options.onRetry === 'function') {
    panel.querySelector('.app-inline-error-retry').onclick = function () { panel.remove(); options.onRetry(); };
  }
  if (options.prepend) target.insertBefore(panel, target.firstChild);
  else target.appendChild(panel);
  return panel;
}

/**
 * 应用内确认模态（危险动作默认样式）。
 * 返回 Promise<{approved:boolean, reason?:string, value?:string}>。
 */
export function confirmDialog(options) {
  options = options || {};
  return new Promise(function (resolve) {
    var prior = document.getElementById('appConfirmModal'); if (prior) prior.remove();
    var needsInput = !!options.input;
    var needsReason = !!options.reason;
    var mask = document.createElement('div');
    mask.id = 'appConfirmModal';
    mask.className = 'app-modal-mask';
    mask.setAttribute('data-testid', 'app-confirm-dialog');
    var bullets = (options.bullets || []).map(function (b) { return '<li>' + adaptiveEsc(b) + '</li>'; }).join('');
    mask.innerHTML = '<section class="app-modal' + (options.danger ? ' app-modal-danger' : '') + '" role="' + (options.danger ? 'alertdialog' : 'dialog') + '" aria-modal="true" aria-labelledby="appConfirmTitle">'
      + '<span class="app-modal-kicker">' + adaptiveEsc(options.kicker || '请确认') + '</span>'
      + '<h3 id="appConfirmTitle" data-testid="app-confirm-title">' + adaptiveEsc(options.title || '确认操作') + '</h3>'
      + (options.detail ? '<p>' + adaptiveEsc(options.detail) + '</p>' : '')
      + (bullets ? '<ul class="app-modal-list">' + bullets + '</ul>' : '')
      + (needsInput ? '<label class="app-modal-field">' + adaptiveEsc(options.input.label || '输入') + '<input type="text" data-role="input" value="' + adaptiveEsc(options.input.value || '') + '" placeholder="' + adaptiveEsc(options.input.placeholder || '') + '"></label>' : '')
      + (needsReason ? '<label class="app-modal-field">确认说明<textarea data-role="reason" maxlength="300" placeholder="' + adaptiveEsc(options.placeholder || '请填写原因') + '">' + adaptiveEsc(options.defaultReason || '') + '</textarea></label>' : '')
      + '<footer><button type="button" class="ghost" data-action="cancel">' + adaptiveEsc(options.cancelText || '取消') + '</button>'
      + '<button type="button" class="' + (options.danger ? 'danger' : 'primary') + '" data-action="approve">' + adaptiveEsc(options.confirmText || '确认') + '</button></footer></section>';
    function close(result) { releaseTrap(); mask.remove(); resolve(result); }
    var releaseTrap = trapFocus(mask);
    mask.addEventListener('click', function (event) { if (event.target === mask) close({ approved: false }); });
    mask.querySelector('[data-action="cancel"]').onclick = function () { close({ approved: false }); };
    mask.querySelector('[data-action="approve"]').onclick = function () {
      var reason = needsReason ? (mask.querySelector('[data-role="reason"]').value || '').trim() : '';
      if (needsReason && !reason) { mask.querySelector('[data-role="reason"]').focus(); return; }
      var value = needsInput ? (mask.querySelector('[data-role="input"]').value || '').trim() : '';
      if (needsInput && !value) { mask.querySelector('[data-role="input"]').focus(); return; }
      close({ approved: true, reason: reason, value: value });
    };
    document.body.appendChild(mask);
    window.setTimeout(function () {
      var target = needsInput ? mask.querySelector('[data-role="input"]')
        : needsReason ? mask.querySelector('[data-role="reason"]')
        : mask.querySelector('[data-action="cancel"]');
      if (target) target.focus();
    }, 0);
  });
}

/** 替代 window.prompt：返回 Promise<{approved, value}>。 */
export function promptDialog(options) {
  options = options || {};
  return confirmDialog({
    kicker: options.kicker || '需要输入',
    title: options.title || '请输入',
    detail: options.detail || '',
    confirmText: options.confirmText || '确定',
    input: { label: options.label || '值', value: options.value || '', placeholder: options.placeholder || '' },
  }).then(function (result) {
    if (!result.approved) return { approved: false, value: '' };
    return { approved: true, value: result.value };
  });
}

/* ---------------------------------------------------------------- 兼容包装 */

export function adaptiveConfirm(options) {
  options = options || {};
  return confirmDialog({
    kicker: '人工确认 · 模拟盘',
    title: options.title || '确认操作',
    detail: options.detail || '此操作仅作用于模拟盘。',
    bullets: options.boundary ? [options.boundary] : ['不会连接券商、不会发送真实订单。'],
    reason: !!options.reason,
    placeholder: options.placeholder,
    defaultReason: options.defaultReason,
    confirmText: '确认执行',
  }).then(function (result) {
    return { approved: !!result.approved, reason: result.reason || '' };
  });
}

export function adaptiveActionNotice(title, detail) {
  return confirmDialog({
    kicker: '操作未执行',
    title: title || '自进化操作失败',
    detail: detail || '本次操作没有写入任何调参或交易数据。',
    bullets: ['请刷新证据后重试；若问题持续，请保留当前提示供审计排查。'],
    confirmText: '知道了',
    cancelText: '关闭',
  });
}

/** PR-57：设置项确认也走应用内模态（不再依赖浏览器原生 confirm）。 */
export function settingsConfirm(message) {
  return confirmDialog({ kicker: '设置确认', title: '确认修改设置？', detail: String(message || ''), confirmText: '确认' })
    .then(function (result) { return !!result.approved; });
}

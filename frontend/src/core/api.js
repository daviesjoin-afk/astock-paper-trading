/* PR-55：由 frontend/app.js 拆分（纯搬运，逻辑/文案未改） */
/* PR-2：写请求统一携带操作员凭据。
 *
 * 边界要求（见 backend/operator_auth.py）：
 *   - 唯一凭据形式是标准 authorization 头，Bearer 方案（值形如 `Bearer <凭据>`）。
 *     绝不使用私有 header，也绝不把 token 放进 URL/query（避免落入访问日志、
 *     浏览器历史、Referer）。
 *   - **只对写方法**（POST/PUT/PATCH/DELETE）附加。GET/HEAD/OPTIONS 是只读
 *     控制面，绝不发送 operator 凭据——即使本标签页已经解锁。
 *   - 凭据存在 **sessionStorage**（key `astock.operatorToken.v1`），语义是
 *     "本标签页本次会话"：同一 tab 刷新保留，关闭 tab 即消失。它不是身份
 *     系统，只是一道本机/内网边界；不要在共享浏览器上解锁。刻意不用持久化
 *     存储，避免凭据长期驻留在磁盘上。
 *   - 未解锁时不抛错、不阻断：请求照发，由服务端按契约返回 401/403/503，
 *     前端把后端的中文 detail 原样展示，避免客户端猜测策略。
 *   - **401 不自动重放**：授权失败只提示，用户须主动重新点击原操作。
 */
export var OPERATOR_TOKEN_KEY='astock.operatorToken.v1';

// 只读方法：绝不附加凭据。
var READ_METHODS=['GET','HEAD','OPTIONS'];

function storage(){
  try { return sessionStorage; } catch(err){ return null; }
}

export function getOperatorToken(){
  var store=storage();
  if(!store) return '';
  try { return store.getItem(OPERATOR_TOKEN_KEY)||''; } catch(err){ return ''; }
}
export function setOperatorToken(value){
  var store=storage();
  if(!store) return;
  try {
    var v=String(value==null?'':value).trim();
    if(v) store.setItem(OPERATOR_TOKEN_KEY, v); else store.removeItem(OPERATOR_TOKEN_KEY);
  } catch(err){ /* 隐私模式/存储被禁：静默降级，写请求将由服务端拒绝 */ }
}
export function clearOperatorToken(){ setOperatorToken(''); }
export function isOperatorUnlocked(){ return !!getOperatorToken(); }

/**
 * 按方法决定是否附加凭据。
 * - mutation（POST/PUT/PATCH/DELETE）→ 追加标准 authorization 头（Bearer 方案）
 * - read（GET/HEAD/OPTIONS）、未知方法 → 原样返回，绝不附加
 */
export function operatorAuthorizationHeaders(method, headers){
  var out=headers||{};
  var verb=String(method||'GET').toUpperCase();
  if(READ_METHODS.indexOf(verb)>=0) return out;
  var token=getOperatorToken();
  if(token) out['Authorization']='Bearer '+token;
  return out;
}

export async function api(path, options){
  options=options||{};
  var r;
  // A container restart can briefly close the upstream connection and make
  // nginx return 502/503. GETs are safe to retry; keep POST semantics intact.
  // 2026-09-03 P3：旧退避仅 250/500ms，短于一次容器重启的拒连窗口（实测
  // 约 2~3 秒），所以重新部署时前端仍会抛出硬错误“请求失败 HTTP 502”。
  // 放宽到 4 次 / 累计约 4.8 秒，让部署重启对只读请求透明；非 5xx 首次
  // 即跳出，正常请求不受影响。POST 保持不重试，避免重复下单类副作用。
  var MAX_GET_ATTEMPTS = 4;
  var method=options.method||'GET';
  for(var attempt=0; attempt<MAX_GET_ATTEMPTS; attempt++){
    var controller=typeof AbortController==='undefined'?null:new AbortController();
    var timeout=controller?setTimeout(function(){controller.abort();},Number(options.timeout)||25000):null;
    try { r = await fetch(path,{signal:controller&&controller.signal,cache:'no-store',method:method,headers:operatorAuthorizationHeaders(method,options.headers)}); }
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

export async function apiPost(path){
  var r = await fetch(path,{method:'POST',headers:operatorAuthorizationHeaders('POST')});
  var d = await r.json().catch(function(){ return {}; });
  if(!r.ok) throw new Error(d.detail || d.error || ('请求失败 HTTP '+r.status));
  return d;
}

export async function apiPostJson(path, payload){
  var r=await fetch(path,{method:'POST',headers:operatorAuthorizationHeaders('POST',{'Content-Type':'application/json'}),body:JSON.stringify(payload||{})});
  var d=await r.json().catch(function(){return {};});
  if(!r.ok) throw new Error(d.detail||d.error||('请求失败 HTTP '+r.status));
  return d;
}

export async function apiJson(path, method, payload){
  var verb=method||'GET';
  var r=await fetch(path,{method:verb,headers:operatorAuthorizationHeaders(verb,{'Content-Type':'application/json'}),body:(payload===undefined?undefined:JSON.stringify(payload||{}))});
  var d=await r.json().catch(function(){return {};});
  if(!r.ok) throw new Error(d.detail||d.error||('请求失败 HTTP '+r.status));
  return d;
}

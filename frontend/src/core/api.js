/* PR-55：由 frontend/app.js 拆分（纯搬运，逻辑/文案未改） */
/* PR-2：写请求统一携带操作员凭据。
 *
 * 边界要求（见 backend/operator_auth.py）：
 *   - 凭据只走请求头 X-Operator-Token，绝不进 URL/query（避免落入访问日志、
 *     浏览器历史、Referer）。
 *   - 只对写方法（POST/PUT/PATCH/DELETE）附加；GET 是只读控制面，不需要凭据，
 *     也不应带——这样普通看板访问无需任何密钥。
 *   - 凭据存在 localStorage（operatorToken），由操作员本机一次性写入。它不是
 *     身份系统，只是一道本机/内网边界；不要在共享浏览器上保存。
 *   - 未配置凭据时不抛错、不阻断：请求照发，由服务端按 fail-closed 返回 401/
 *     403/503，前端再把后端的中文 detail 原样展示，避免客户端猜测策略。
 */
export function getOperatorToken(){
  try { return localStorage.getItem('operatorToken')||''; } catch(err){ return ''; }
}
export function setOperatorToken(value){
  try {
    var v=String(value==null?'':value).trim();
    if(v) localStorage.setItem('operatorToken', v); else localStorage.removeItem('operatorToken');
  } catch(err){ /* 隐私模式/存储被禁：静默降级，写请求将由服务端拒绝 */ }
}
function operatorHeaders(base){
  var headers=base||{};
  var token=getOperatorToken();
  if(token) headers['X-Operator-Token']=token;
  return headers;
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
  for(var attempt=0; attempt<MAX_GET_ATTEMPTS; attempt++){
    var controller=typeof AbortController==='undefined'?null:new AbortController();
    var timeout=controller?setTimeout(function(){controller.abort();},Number(options.timeout)||25000):null;
    try { r = await fetch(path,{signal:controller&&controller.signal,cache:'no-store',headers:operatorHeaders(options.headers)}); }
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
  var r = await fetch(path,{method:'POST',headers:operatorHeaders()});
  var d = await r.json().catch(function(){ return {}; });
  if(!r.ok) throw new Error(d.detail || d.error || ('请求失败 HTTP '+r.status));
  return d;
}

export async function apiPostJson(path, payload){
  var r=await fetch(path,{method:'POST',headers:operatorHeaders({'Content-Type':'application/json'}),body:JSON.stringify(payload||{})});
  var d=await r.json().catch(function(){return {};});
  if(!r.ok) throw new Error(d.detail||d.error||('请求失败 HTTP '+r.status));
  return d;
}

export async function apiJson(path, method, payload){
  var r=await fetch(path,{method:method||'GET',headers:operatorHeaders({'Content-Type':'application/json'}),body:(payload===undefined?undefined:JSON.stringify(payload||{}))});
  var d=await r.json().catch(function(){return {};});
  if(!r.ok) throw new Error(d.detail||d.error||('请求失败 HTTP '+r.status));
  return d;
}

/* PR-55：由 frontend/app.js 拆分（纯搬运，逻辑/文案未改） */
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
    try { r = await fetch(path,{signal:controller&&controller.signal,cache:'no-store'}); }
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
  var r = await fetch(path,{method:'POST'});
  var d = await r.json().catch(function(){ return {}; });
  if(!r.ok) throw new Error(d.detail || d.error || ('请求失败 HTTP '+r.status));
  return d;
}

export async function apiPostJson(path, payload){
  var r=await fetch(path,{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(payload||{})});
  var d=await r.json().catch(function(){return {};});
  if(!r.ok) throw new Error(d.detail||d.error||('请求失败 HTTP '+r.status));
  return d;
}

export async function apiJson(path, method, payload){
  var r=await fetch(path,{method:method||'GET',headers:{'Content-Type':'application/json'},body:(payload===undefined?undefined:JSON.stringify(payload||{}))});
  var d=await r.json().catch(function(){return {};});
  if(!r.ok) throw new Error(d.detail||d.error||('请求失败 HTTP '+r.status));
  return d;
}

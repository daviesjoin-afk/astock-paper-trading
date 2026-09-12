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
/* PR-8：请求契约收敛（frontend API request contract）。
 *
 * 背景：本模块此前有**四份各自为政**的 fetch 实现（api / apiPost /
 * apiPostJson / apiJson），它们在三个既有缺陷上互相漂移：
 *
 *   1. **头对象所有权错位**。`operatorAuthorizationHeaders` 用
 *      `out = headers || {}` 直接拿调用方的对象当输出，写方法再把
 *      Authorization 写进去。调用方只要复用同一个 headers 对象，之后发出的
 *      只读请求就会**带着凭据一起发出去**——而"GET 绝不携带凭据"正是
 *      `backend/operator_auth.py` 的边界前提（凭据一旦进入只读请求，就会落进
 *      访问日志/Referer 面）。
 *   2. **Headers 实例被当成普通对象**。调用方传 `new Headers(...)` 时，
 *      `out['Authorization'] = ...` 只是给实例挂了一个普通属性，fetch 根本
 *      不会把它当作请求头发出去 → 写请求静默失去凭据 → 401。
 *   3. **四份实现漂移**。只有 `api()` 有超时 / AbortController 与
 *      `cache:'no-store'`；`apiJson()` 在**没有 body** 的 GET/DELETE 上也
 *      会附上 Content-Type；错误对象只有 message，没有 status/detail/payload，
 *      调用方无法区分 401/403/409/503。
 *
 * 收敛方式：**唯一 HTTP 原语 `request()`**，四个公开 helper 只是它的薄封装。
 * 所有权规则：本模块只**读**调用方传入的对象，绝不改写（调用方的 options 与
 * headers 在调用前后必须逐键相等）。这条规则由
 * `frontend/tests/api-request-contract.test.mjs` 用行为断言锁住，并由同文件里的
 * "单一 fetch 调用点"源码守卫防止再次漂移。
 */
export var OPERATOR_TOKEN_KEY='astock.operatorToken.v1';

// 只读方法：绝不附加凭据。
var READ_METHODS=['GET','HEAD','OPTIONS'];

// 只读请求的重试上限。一次容器重启的拒连窗口实测约 2~3 秒，4 次 / 累计约
// 4.8 秒足以让部署重启对只读请求透明；非 5xx 首次即跳出，正常请求不受影响。
// 写方法永远只尝试 1 次（见 request()）。
var MAX_READ_ATTEMPTS=4;

// 只读请求的重试白名单：只有这三种状态码代表"上游暂时不可用"。
var RETRYABLE_STATUS=[502,503,504];

// 单次请求的默认超时（毫秒）。所有方法共享——此前只有 api() 有超时，
// 写请求可以永久挂起。
var DEFAULT_TIMEOUT_MS=25000;

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
 * 把一个 HeadersInit 归一化成**全新的**普通对象。
 *
 * 三种入参形态都必须支持，且**绝不改写调用方的对象**：
 *   - `Headers` 实例（必须显式枚举，否则附上的头对 fetch 不可见）；
 *   - 二元组数组 `[[k, v], ...]`；
 *   - 普通对象。
 * 缺省/无法识别 → 空对象（不抛错：请求照发，由服务端按契约拒绝）。
 */
function copyHeaders(headers){
  var out={};
  if(!headers) return out;
  // Headers 实例：同时具备 forEach 与 get。必须先于"普通对象"分支处理。
  if(typeof headers.forEach==='function' && typeof headers.get==='function'){
    try {
      headers.forEach(function(value,key){ out[String(key)]=String(value); });
      return out;
    } catch(err){ /* 落到下面的兜底分支 */ }
  }
  if(Array.isArray(headers)){
    for(var i=0;i<headers.length;i++){
      var pair=headers[i];
      if(pair && pair.length>=2) out[String(pair[0])]=String(pair[1]);
    }
    return out;
  }
  for(var key in headers){
    if(Object.prototype.hasOwnProperty.call(headers,key)) out[key]=headers[key];
  }
  return out;
}

/** 头名大小写不敏感地判断某个头是否已存在。 */
function hasHeader(headers,name){
  var target=String(name).toLowerCase();
  for(var key in headers){
    if(Object.prototype.hasOwnProperty.call(headers,key) && String(key).toLowerCase()===target){
      return true;
    }
  }
  return false;
}

/**
 * 按方法决定是否附加凭据。
 * - mutation（POST/PUT/PATCH/DELETE）→ 追加标准 authorization 头（Bearer 方案）
 * - read（GET/HEAD/OPTIONS）、未知方法 → 原样返回，绝不附加
 *
 * 返回值永远是**新对象**：调用方传入的 headers 不会被改写（PR-8 缺陷 1）。
 */
export function operatorAuthorizationHeaders(method, headers){
  var out=copyHeaders(headers);
  var verb=String(method||'GET').toUpperCase();
  if(READ_METHODS.indexOf(verb)>=0) return out;
  var token=getOperatorToken();
  if(token) out['Authorization']='Bearer '+token;
  return out;
}

/**
 * 请求失败时抛出的错误。
 *
 * 保留 HTTP 语义（status / detail / payload），让调用方能区分
 * 401/403/409/503，而不再只能对 message 做正则。**不携带任何凭据**：
 * 请求头从不进入错误对象。
 */
export class ApiError extends Error {
  constructor(message, info){
    super(message);
    this.name='ApiError';
    info=info||{};
    this.status=Number(info.status)||0;
    this.detail=typeof info.detail==='string'?info.detail:'';
    this.payload=info.payload&&typeof info.payload==='object'?info.payload:{};
    this.method=info.method||'';
    this.path=info.path||'';
  }
}

/** 只接受非空字符串，避免把 `0` / `false` / `[]` 当成 detail。 */
function _text(value){
  return (typeof value==='string' && value.trim()) ? value : '';
}

/**
 * 从错误响应体里抽出可展示的 detail。
 *
 * 兼容四种真实形态：字符串 detail、FastAPI 校验错误的数组 detail
 * （`[{loc,msg,type}, ...]`）、嵌套对象 detail、以及 `{error: ...}`。
 * 无法识别 → 空串（由调用方回退到状态码文案），绝不抛错、绝不产出
 * `[object Object]`。
 */
function extractDetail(payload){
  if(!payload || typeof payload!=='object') return '';
  var detail=payload.detail;
  if(_text(detail)) return detail;
  if(Array.isArray(detail)){
    var msgs=[];
    for(var i=0;i<detail.length;i++){
      var item=detail[i];
      var text=item&&typeof item==='object'?(_text(item.msg)||_text(item.message)):_text(item);
      if(text) msgs.push(text);
    }
    if(msgs.length) return msgs.join('；');
  }
  if(detail && typeof detail==='object' && !Array.isArray(detail)){
    var nested=_text(detail.message)||_text(detail.msg);
    if(nested) return nested;
  }
  var error=payload.error;
  if(_text(error)) return error;
  if(error && typeof error==='object'){
    var nestedError=_text(error.message)||_text(error.detail);
    if(nestedError) return nestedError;
  }
  return '';
}

/** 5xx 时补一句可操作提示；其余状态码只回状态码本身。 */
function _statusHint(status){
  return status>=500 ? '（后端可能正在重启或过载，请稍后重试）' : '';
}

function buildApiError(status, payload, method, path){
  var detail=extractDetail(payload);
  var message=detail||('请求失败 HTTP '+status+_statusHint(status));
  return new ApiError(message, {
    status: status, detail: detail, payload: payload, method: method, path: path,
  });
}

/** 退避：800 / 1600 / 2400 毫秒（attempt = 0 / 1 / 2）。 */
function _backoff(attempt){
  return new Promise(function(resolve){ setTimeout(resolve, 800*(attempt+1)); });
}

/**
 * 唯一的 HTTP 原语：**全模块只有这里调用 fetch**。
 *
 * 每次尝试都用一个新的 AbortController 做超时；timer 在 fetch 结算时立刻
 * 清掉（不能留到退避期间——那时 controller 变量已经被下一轮覆盖）。
 */
async function _sendOnce(verb, path, headers, body, timeoutMs){
  var controller=typeof AbortController==='undefined'?null:new AbortController();
  var init={method:verb, cache:'no-store', headers:headers};
  if(controller) init.signal=controller.signal;
  if(body!==undefined && body!==null) init.body=body;
  var timer=controller?setTimeout(function(){controller.abort();},timeoutMs):null;
  try {
    return await fetch(path, init);
  } finally {
    if(timer) clearTimeout(timer);
  }
}

/**
 * 解析响应并产出结果或抛出 ApiError。
 * JSON 解析失败不算错误本身：成功路径回退到 `{}`，失败路径回退到状态码文案。
 */
async function _settle(response, verb, path){
  var payload;
  try {
    payload = (response && typeof response.json==='function') ? await response.json() : undefined;
  } catch(err){ payload=undefined; }
  if(!response || !response.ok){
    var status=response?Number(response.status)||0:0;
    var body=(payload && typeof payload==='object') ? payload : {};
    throw buildApiError(status, body, verb, path);
  }
  return payload===undefined ? {} : payload;
}

/**
 * 所有公开 helper 的唯一实现。
 *
 * - 只读方法：网络异常与 502/503/504 最多重试 MAX_READ_ATTEMPTS 次；
 * - 写方法与未知方法：**永远只尝试 1 次**（一次 502/503/504 被自动重放会带来
 *   重复副作用：重复下单 / 重复启动）。
 * - Content-Type 只在真的带 body 时补默认值。
 */
async function request(method, path, options){
  options=options||{};
  var verb=String(method||'GET').toUpperCase();
  var isRead=READ_METHODS.indexOf(verb)>=0;
  var attempts=isRead?MAX_READ_ATTEMPTS:1;
  var hasBody=options.body!==undefined && options.body!==null;
  var headers=operatorAuthorizationHeaders(verb, options.headers);
  if(hasBody && !hasHeader(headers,'Content-Type')) headers['Content-Type']='application/json';
  var timeoutMs=Number(options.timeout)>0?Number(options.timeout):DEFAULT_TIMEOUT_MS;

  var response;
  for(var attempt=0; attempt<attempts; attempt++){
    try {
      response=await _sendOnce(verb, path, headers, options.body, timeoutMs);
    } catch(err){
      if(attempt===attempts-1) throw err;
      await _backoff(attempt);
      continue;
    }
    if(!isRead || RETRYABLE_STATUS.indexOf(response.status)<0) break;
    if(attempt<attempts-1) await _backoff(attempt);
  }
  return _settle(response, verb, path);
}

/**
 * 通用入口。`options` 只被读取，绝不改写。
 * `{method, headers, body, timeout}` 均透传给唯一原语。
 */
export async function api(path, options){
  options=options||{};
  return request(options.method||'GET', path, {
    headers: options.headers, body: options.body, timeout: options.timeout,
  });
}

/** POST，无 body（参数走 query string）。 */
export async function apiPost(path, options){
  options=options||{};
  return request('POST', path, {
    headers: options.headers, body: options.body, timeout: options.timeout,
  });
}

/** POST + JSON body。payload 缺省等价于 `{}`。 */
export async function apiPostJson(path, payload, options){
  options=options||{};
  return request('POST', path, {
    headers: options.headers, body: JSON.stringify(payload||{}), timeout: options.timeout,
  });
}

/**
 * 任意方法 + 可选 JSON body。
 * `payload === undefined` → **不发 body**（也不再附 Content-Type）。
 */
export async function apiJson(path, method, payload, options){
  options=options||{};
  var body=payload===undefined?undefined:JSON.stringify(payload||{});
  return request(method||'GET', path, {
    headers: options.headers, body: body, timeout: options.timeout,
  });
}

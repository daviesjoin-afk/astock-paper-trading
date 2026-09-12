// PR-8：前端 **请求契约** 的行为测试（Node，无浏览器）。
//
// 为什么需要它：`backend/test_operator_boundary.py` 里的前端检查只能做
// **源码存在性**断言（"文本里有没有某个字符串"）。本次收敛的三类缺陷都属于
// "名字没变、语义变了"：
//
//   1. 头对象所有权错位 —— `operatorAuthorizationHeaders` 曾经把 Authorization
//      写进**调用方传入的对象**。函数名、签名、返回类型全都没变，只有"入参
//      是否被改写"变了 → 存在性检查永远绿。
//   2. `Headers` 实例被当成普通对象 —— 同上，改名换姓看不出来。
//   3. 四份 fetch 实现漂移 —— 只能靠"行为 + 单一调用点源码守卫"钉住。
//
// 因此本文件直接 import 真实的 `src/core/api.js`，调用真实函数、拦截真实
// `fetch`，断言**可观测行为**：调用方对象是否被改写、真正发出去的头是什么、
// 重试了几次、错误对象带不带 HTTP 语义。
//
// 运行：node --test tests/api-request-contract.test.mjs
// 由 CI 的 frontend job 执行（`npm run test:unit` 覆盖 tests/*.test.mjs）。

import assert from "node:assert/strict";
import test from "node:test";
import { readFileSync, readdirSync, statSync } from "node:fs";
import { fileURLToPath } from "node:url";
import path from "node:path";

// ─── 在 import 之前装好 sessionStorage 替身（与凭据测试同一手法）───
function installStorage() {
  const map = new Map();
  globalThis.sessionStorage = {
    getItem: (k) => (map.has(k) ? map.get(k) : null),
    setItem: (k, v) => map.set(k, String(v)),
    removeItem: (k) => map.delete(k),
    clear: () => map.clear(),
    key: (i) => Array.from(map.keys())[i] ?? null,
    get length() {
      return map.size;
    },
  };
  return map;
}

const store = installStorage();
const API = await import("../src/core/api.js");

const TOKEN = "zz-contract-operator-placeholder-value";
const READ_METHODS = ["GET", "HEAD", "OPTIONS"];
const WRITE_METHODS = ["POST", "PUT", "PATCH", "DELETE"];

const HERE = path.dirname(fileURLToPath(import.meta.url));
const SRC_DIR = path.resolve(HERE, "..", "src");
const API_JS = path.join(SRC_DIR, "core", "api.js");

const HAS_HEADERS = typeof globalThis.Headers === "function";

function reset() {
  store.clear();
  API.clearOperatorToken();
}

// ─────────────────────────────────────────────
// fetch 替身基础设施
// ─────────────────────────────────────────────

const OK = (body = { ok: true }) => ({
  ok: true,
  status: 200,
  json: async () => body,
});
const FAIL = (status, body) => ({
  ok: false,
  status,
  json: async () => body,
});
const FAIL_NON_JSON = (status) => ({
  ok: false,
  status,
  json: async () => {
    throw new Error("response body is not JSON");
  },
});

/** 装上 fetch 替身并记录每次调用；返回记录数组。 */
function recordFetch(handler) {
  const calls = [];
  globalThis.fetch = async (url, init) => {
    calls.push({ url, init, headers: init && init.headers });
    return handler(calls.length, url, init);
  };
  return calls;
}

/**
 * 在替身生效期间执行 fn，结束后一定还原真实 fetch。
 * 返回**本次调用记录**（`[{url, init, headers}]`），断言只看真实发出去的东西。
 */
async function withFetch(handler, fn) {
  const original = globalThis.fetch;
  const calls = recordFetch(handler);
  try {
    await fn(calls);
  } finally {
    globalThis.fetch = original;
  }
  return calls;
}

/**
 * 压缩退避等待：api.js 的退避恰为 800/1600/2400ms（见 800*(attempt+1)）。
 * 只把这三个值改成 0，其余定时器（AbortController 的超时）保持真实，
 * 这样"超时"仍然是被真实测到的行为。退避公式若变化，测试只会变慢，断言不变。
 */
const BACKOFF_DELAYS = new Set([800, 1600, 2400]);

async function withFastBackoff(fn) {
  const realSetTimeout = globalThis.setTimeout;
  globalThis.setTimeout = (cb, ms, ...rest) =>
    realSetTimeout(cb, BACKOFF_DELAYS.has(ms) ? 0 : ms, ...rest);
  try {
    return await fn();
  } finally {
    globalThis.setTimeout = realSetTimeout;
  }
}

async function rejectionOf(fn) {
  try {
    await fn();
  } catch (err) {
    return err;
  }
  throw new Error("预期该调用抛错，但它成功返回了");
}

/** 在 fetch 替身下捕获一次调用抛出的错误（调用必须真的抛错）。 */
async function captureError(handler, fn) {
  let captured = null;
  await withFetch(handler, async () => {
    try {
      await fn();
    } catch (err) {
      captured = err;
    }
  });
  assert.ok(captured, "预期该调用抛错，但它成功返回了");
  return captured;
}

/** 大小写不敏感地读一个头（记录下来的头是普通对象）。 */
function headerValue(headers, name) {
  if (!headers) return undefined;
  const target = String(name).toLowerCase();
  for (const key of Object.keys(headers)) {
    if (String(key).toLowerCase() === target) return headers[key];
  }
  return undefined;
}

// ─────────────────────────────────────────────
// 1. 头对象所有权：绝不改写调用方
// ─────────────────────────────────────────────

test("operatorAuthorizationHeaders 绝不改写调用方传入的 headers", () => {
  reset();
  API.setOperatorToken(TOKEN);
  const caller = { "Content-Type": "application/json" };
  const before = JSON.stringify(caller);

  for (const method of [...WRITE_METHODS, ...READ_METHODS]) {
    API.operatorAuthorizationHeaders(method, caller);
  }

  assert.equal(JSON.stringify(caller), before, "入参必须逐键不变");
  assert.ok(
    !("Authorization" in caller),
    "写方法的凭据绝不能被写回调用方的对象（否则该对象复用到 GET 上就泄漏）",
  );
});

test("helper 返回的是新对象，与入参不共享引用", () => {
  reset();
  API.setOperatorToken(TOKEN);
  const caller = { "X-Trace-Id": "t-1" };
  const out = API.operatorAuthorizationHeaders("POST", caller);
  assert.notEqual(out, caller, "必须返回副本而不是同一个引用");
  out["X-Trace-Id"] = "mutated";
  assert.equal(caller["X-Trace-Id"], "t-1", "改写返回值不得影响入参");
});

test("只读方法返回的也是副本（不是入参本身）", () => {
  reset();
  API.setOperatorToken(TOKEN);
  const caller = { "X-Trace-Id": "t-1" };
  const out = API.operatorAuthorizationHeaders("GET", caller);
  assert.notEqual(out, caller);
  assert.equal(out["X-Trace-Id"], "t-1", "入参的其它头必须保留");
  assert.ok(!("Authorization" in out));
});

test("api() 绝不改写调用方的 options 与其 headers", async () => {
  reset();
  API.setOperatorToken(TOKEN);
  const options = {
    method: "POST",
    headers: { "Content-Type": "application/json", "X-Trace-Id": "t-2" },
    timeout: 5000,
  };
  const before = JSON.stringify(options);

  await withFetch(OK, () => API.api("/api/paper/start", options));

  assert.equal(JSON.stringify(options), before, "options 必须逐键不变");
  assert.ok(!("Authorization" in options.headers));
});

test("apiPost / apiPostJson / apiJson 同样不改写 options", async () => {
  reset();
  API.setOperatorToken(TOKEN);
  const payload = { name: "draft" };
  const payloadBefore = JSON.stringify(payload);

  const cases = [
    () => API.apiPost("/api/paper/pause", { headers: { "X-A": "1" } }),
    () => API.apiPostJson("/api/strategies", payload, { headers: { "X-B": "2" } }),
    () => API.apiJson("/api/strategies/x", "PATCH", payload, { headers: { "X-C": "3" } }),
  ];
  for (const run of cases) {
    await withFetch(OK, run);
  }

  assert.equal(JSON.stringify(payload), payloadBefore, "payload 不得被改写");
});

test("写请求复用同一个 headers 对象后，只读请求不得带出凭据（根因 1 回归）", async () => {
  reset();
  API.setOperatorToken(TOKEN);
  const shared = { "Content-Type": "application/json" };

  await withFetch(OK, () => API.apiPostJson("/api/strategies", { id: "s1" }, { headers: shared }));

  assert.deepEqual(
    shared,
    { "Content-Type": "application/json" },
    "写请求结束后，共享的 headers 对象必须原样",
  );

  const calls = await withFetch(OK, () => API.api("/api/version", { headers: shared }));
  assert.equal(
    headerValue(calls[0].headers, "Authorization"),
    undefined,
    "复用共享 headers 的 GET 绝不能被带出 Authorization",
  );
});

// ─────────────────────────────────────────────
// 2. Headers 实例 / 数组形态的归一化（根因 2）
// ─────────────────────────────────────────────

test(
  "Headers 实例被真正枚举（写方法带出 Bearer，且其它头保留）",
  { skip: HAS_HEADERS ? false : "本运行时没有全局 Headers" },
  async () => {
    reset();
    API.setOperatorToken(TOKEN);
    const instance = new Headers({ "X-Trace-Id": "t-3" });

    const calls = await withFetch(OK, () =>
      API.apiPostJson("/api/strategies", { id: "s2" }, { headers: instance }),
    );

    assert.equal(
      headerValue(calls[0].headers, "Authorization"),
      "Bearer " + TOKEN,
      "Headers 实例上的凭据必须真的成为请求头（旧实现会静默丢掉）",
    );
    assert.equal(headerValue(calls[0].headers, "X-Trace-Id"), "t-3");
  },
);

test(
  "Headers 实例用于只读请求时不得带出凭据",
  { skip: HAS_HEADERS ? false : "本运行时没有全局 Headers" },
  async () => {
    reset();
    API.setOperatorToken(TOKEN);
    const instance = new Headers({ "X-Trace-Id": "t-4" });

    const calls = await withFetch(OK, () => API.api("/api/version", { headers: instance }));

    assert.equal(headerValue(calls[0].headers, "Authorization"), undefined);
    assert.equal(headerValue(calls[0].headers, "X-Trace-Id"), "t-4");
  },
);

test(
  "helper 直接接收 Headers 实例时也归一化成普通对象",
  { skip: HAS_HEADERS ? false : "本运行时没有全局 Headers" },
  () => {
    reset();
    API.setOperatorToken(TOKEN);
    const out = API.operatorAuthorizationHeaders("POST", new Headers({ "X-Trace-Id": "t-5" }));
    assert.equal(out.Authorization, "Bearer " + TOKEN);
    assert.equal(headerValue(out, "X-Trace-Id"), "t-5");
    assert.ok(!(out instanceof Headers), "返回值必须是普通对象，保持既有契约");
  },
);

test("数组形态的 HeadersInit 也被归一化", () => {
  reset();
  API.setOperatorToken(TOKEN);
  const out = API.operatorAuthorizationHeaders("POST", [["X-Trace-Id", "t-6"]]);
  assert.equal(out.Authorization, "Bearer " + TOKEN);
  assert.equal(out["X-Trace-Id"], "t-6");
});

test("无法识别的 headers 入参不抛错（请求照发，由服务端按契约拒绝）", () => {
  reset();
  API.setOperatorToken(TOKEN);
  for (const weird of [null, undefined, 0, "", "x-raw-string"]) {
    const out = API.operatorAuthorizationHeaders("POST", weird);
    assert.equal(out.Authorization, "Bearer " + TOKEN, `入参 ${String(weird)} 不应中断归一化`);
  }
});

// ─────────────────────────────────────────────
// 3. 写方法永不重放（mutation non-replay）
// ─────────────────────────────────────────────

for (const method of [...WRITE_METHODS, "TRACE", "trace"]) {
  for (const status of [502, 503, 504]) {
    test(`${method} 遇 ${status} 只尝试 1 次（绝不重放）`, async () => {
      reset();
      API.setOperatorToken(TOKEN);
      const calls = await withFetch(
        (n) => FAIL(status, { detail: "stub" }),
        () =>
          withFastBackoff(() =>
            rejectionOf(() => API.api("/api/paper/start", { method })),
          ),
      );
      assert.equal(calls.length, 1, `${method} 是写/未知方法，不得重放`);
    });
  }
}

test("写方法的网络异常同样不重放", async () => {
  reset();
  API.setOperatorToken(TOKEN);
  const calls = await withFetch(
    () => {
      throw new Error("network down");
    },
    () => withFastBackoff(() => rejectionOf(() => API.apiPost("/api/paper/pause"))),
  );
  assert.equal(calls.length, 1);
});

test("写方法超时后也不重放", async () => {
  reset();
  API.setOperatorToken(TOKEN);
  const calls = await withFetch(
    (n, url, init) =>
      new Promise((_resolve, reject) => {
        init.signal.addEventListener("abort", () => reject(new Error("aborted")));
      }),
    () =>
      withFastBackoff(() =>
        rejectionOf(() => API.apiPostJson("/api/strategies", {}, { timeout: 5 })),
      ),
  );
  assert.equal(calls.length, 1, "写方法超时只允许尝试 1 次");
});

// ─────────────────────────────────────────────
// 4. 只读方法的重试作用域
// ─────────────────────────────────────────────

test("GET 遇 502/503/504 会重试，上限 4 次", async () => {
  for (const status of [502, 503, 504]) {
    reset();
    const calls = await withFetch(
      () => FAIL(status, { detail: "stub" }),
      () => withFastBackoff(() => rejectionOf(() => API.api("/api/version"))),
    );
    assert.equal(calls.length, 4, `GET 遇 ${status} 应尝试 4 次`);
  }
});

test("GET 在重试后成功则停止重试", async () => {
  reset();
  const calls = await withFetch(
    (n) => (n === 1 ? FAIL(503, { detail: "stub" }) : OK({ app: "astock" })),
    () => withFastBackoff(() => API.api("/api/version")),
  );
  assert.equal(calls.length, 2);
});

test("GET 遇非白名单状态码只尝试 1 次", async () => {
  for (const status of [500, 501, 409, 404, 401, 403]) {
    reset();
    const calls = await withFetch(
      () => FAIL(status, { detail: "stub" }),
      () => withFastBackoff(() => rejectionOf(() => API.api("/api/version"))),
    );
    assert.equal(calls.length, 1, `${status} 不在重试白名单内`);
  }
});

test("GET 的网络异常会重试", async () => {
  reset();
  const calls = await withFetch(
    () => {
      throw new Error("network down");
    },
    () => withFastBackoff(() => rejectionOf(() => API.api("/api/version"))),
  );
  assert.equal(calls.length, 4);
});

test("HEAD / OPTIONS 与 GET 同属只读族（同样带重试、同样不带凭据）", async () => {
  reset();
  API.setOperatorToken(TOKEN);
  for (const method of ["HEAD", "OPTIONS"]) {
    const calls = await withFetch(
      () => FAIL(503, { detail: "stub" }),
      () =>
        withFastBackoff(() =>
          rejectionOf(() => API.api("/api/version", { method })),
        ),
    );
    assert.equal(calls.length, 4, `${method} 应重试`);
    assert.equal(headerValue(calls[0].headers, "Authorization"), undefined);
  }
});

// ─────────────────────────────────────────────
// 5. body 序列化与 Content-Type
// ─────────────────────────────────────────────

test("apiPost 不发 body、不附 Content-Type", async () => {
  reset();
  const calls = await withFetch(OK, () => API.apiPost("/api/paper/pause"));
  assert.equal(calls[0].init.body, undefined);
  assert.equal(headerValue(calls[0].headers, "Content-Type"), undefined);
});

test("apiPostJson 总是发 JSON body 并附 Content-Type", async () => {
  reset();
  const calls = await withFetch(OK, () => API.apiPostJson("/api/strategies", { id: "s3" }));
  assert.equal(calls[0].init.body, JSON.stringify({ id: "s3" }));
  assert.equal(headerValue(calls[0].headers, "Content-Type"), "application/json");
});

test("apiPostJson 的 payload 缺省等价于 {}", async () => {
  reset();
  const calls = await withFetch(OK, () => API.apiPostJson("/api/strategies"));
  assert.equal(calls[0].init.body, "{}");
});

test("apiJson 无 payload 时不发 body、不附 Content-Type（旧实现在这里漂移）", async () => {
  reset();
  for (const method of ["GET", "DELETE", "POST"]) {
    const calls = await withFetch(OK, () => API.apiJson("/api/strategies/x", method));
    assert.equal(calls[0].init.body, undefined, `${method} 不应有 body`);
    assert.equal(
      headerValue(calls[0].headers, "Content-Type"),
      undefined,
      `${method} 无 body 时不得附 Content-Type（无 body 的 JSON 头只会触发多余预检）`,
    );
  }
});

test("apiJson 有 payload 时序列化并附 Content-Type", async () => {
  reset();
  const calls = await withFetch(OK, () =>
    API.apiJson("/api/strategies/x", "PATCH", { changes: { name: "n" } }),
  );
  assert.equal(calls[0].init.body, JSON.stringify({ changes: { name: "n" } }));
  assert.equal(headerValue(calls[0].headers, "Content-Type"), "application/json");
});

test("apiJson(payload=null) 与 undefined 语义一致地序列化为 {}", async () => {
  reset();
  const calls = await withFetch(OK, () => API.apiJson("/api/strategies/x", "PATCH", null));
  assert.equal(calls[0].init.body, "{}");
});

test("调用方显式提供的 Content-Type 被保留且不重复", async () => {
  reset();
  const calls = await withFetch(OK, () =>
    API.apiJson("/api/strategies/x", "PATCH", { a: 1 }, { headers: { "content-type": "application/merge-patch+json" } }),
  );
  const ctKeys = Object.keys(calls[0].headers).filter(
    (k) => k.toLowerCase() === "content-type",
  );
  assert.equal(ctKeys.length, 1, "不得出现两个 Content-Type 键");
  assert.equal(headerValue(calls[0].headers, "Content-Type"), "application/merge-patch+json");
});

test("api(path, options) 的 body 被透传（通用入口不再丢弃 body）", async () => {
  reset();
  API.setOperatorToken(TOKEN);
  const calls = await withFetch(OK, () =>
    API.api("/api/strategies", { method: "PUT", body: JSON.stringify({ a: 1 }) }),
  );
  assert.equal(calls[0].init.body, JSON.stringify({ a: 1 }));
  assert.equal(headerValue(calls[0].headers, "Content-Type"), "application/json");
});

test("只读请求带 cache:'no-store'（旧实现只有 api() 有）", async () => {
  reset();
  const calls = await withFetch(OK, () => API.api("/api/version"));
  assert.equal(calls[0].init.cache, "no-store");
});

// ─────────────────────────────────────────────
// 6. 超时
// ─────────────────────────────────────────────

test("四个公开 helper 都会传 AbortSignal（旧实现只有 api() 有超时）", async () => {
  reset();
  API.setOperatorToken(TOKEN);
  const runners = [
    () => API.api("/api/version"),
    () => API.apiPost("/api/paper/pause"),
    () => API.apiPostJson("/api/strategies", {}),
    () => API.apiJson("/api/strategies/x", "PATCH", {}),
  ];
  for (const run of runners) {
    const calls = await withFetch(OK, run);
    assert.ok(calls[0].init.signal, "每个请求都必须有 AbortSignal");
  }
});

test("请求超时会被 abort，而不是永久挂起", async () => {
  reset();
  const aborts = [];
  const calls = await withFetch(
    (n, url, init) =>
      new Promise((_resolve, reject) => {
        init.signal.addEventListener("abort", () => {
          aborts.push(url);
          reject(new Error("aborted"));
        });
      }),
    () => withFastBackoff(() => rejectionOf(() => API.api("/api/version", { timeout: 5 }))),
  );
  assert.ok(calls.length >= 1);
  assert.ok(aborts.length >= 1, "超时必须真的 abort 掉 fetch");
});

test("默认超时是 25000ms（未显式指定 timeout 时）", async () => {
  reset();
  const seen = [];
  const realSetTimeout = globalThis.setTimeout;
  globalThis.setTimeout = (cb, ms, ...rest) => {
    seen.push(ms);
    return realSetTimeout(cb, ms, ...rest);
  };
  try {
    await withFetch(OK, () => API.api("/api/version"));
  } finally {
    globalThis.setTimeout = realSetTimeout;
  }
  assert.ok(seen.includes(25000), `默认超时应为 25000ms，实际排队的定时器：${seen}`);
});

// ─────────────────────────────────────────────
// 7. ApiError：HTTP 语义 + 不泄漏凭据
// ─────────────────────────────────────────────

test("失败响应抛出 ApiError，带 status / detail / payload / method / path", async () => {
  reset();
  const err = await captureError(
    () => FAIL(409, { detail: "版本冲突：expected_version 不匹配" }),
    () => API.apiJson("/api/strategies/s1", "PATCH", { a: 1 }),
  );
  assert.ok(err instanceof Error, "必须仍然是 Error（调用方 catch 不受影响）");
  assert.ok(err instanceof API.ApiError, "必须是 ApiError 实例");
  assert.equal(err.name, "ApiError");
  assert.equal(err.status, 409);
  assert.equal(err.detail, "版本冲突：expected_version 不匹配");
  assert.deepEqual(err.payload, { detail: "版本冲突：expected_version 不匹配" });
  assert.equal(err.message, "版本冲突：expected_version 不匹配");
  assert.equal(err.method, "PATCH");
  assert.equal(err.path, "/api/strategies/s1");
});

test("message 保持既有文案契约（detail 优先，其次 error）", async () => {
  reset();
  const withDetail = await captureError(
    () => FAIL(400, { detail: "参数不合法", error: "ignored" }),
    () => API.apiPost("/api/paper/pause"),
  );
  assert.equal(withDetail.message, "参数不合法");

  const withError = await captureError(
    () => FAIL(400, { error: "只有 error 字段" }),
    () => API.apiPost("/api/paper/pause"),
  );
  assert.equal(withError.message, "只有 error 字段");
});

test("响应体不是 JSON 时回退到状态码文案，且不抛解析错", async () => {
  reset();
  const err = await captureError(
    () => FAIL_NON_JSON(502),
    () => withFastBackoff(() => API.api("/api/version")),
  );
  assert.ok(err instanceof API.ApiError);
  assert.equal(err.status, 502);
  assert.equal(err.detail, "");
  assert.deepEqual(err.payload, {});
  assert.match(err.message, /^请求失败 HTTP 502/);
  assert.match(err.message, /稍后重试/, "5xx 应给出可操作提示");
});

test("4xx 的状态码文案不带 5xx 提示", async () => {
  reset();
  const err = await captureError(
    () => FAIL_NON_JSON(404),
    () => API.api("/api/version"),
  );
  assert.equal(err.status, 404);
  assert.equal(err.message, "请求失败 HTTP 404");
});

test("FastAPI 的数组 detail 被渲染成可读文本，绝不出现 [object Object]", async () => {
  reset();
  const err = await captureError(
    () =>
      FAIL(422, {
        detail: [
          { loc: ["body", "dsl_ast"], msg: "field required", type: "missing" },
          { loc: ["body", "id"], msg: "string too short", type: "value_error" },
        ],
      }),
    () => API.apiPostJson("/api/strategies", {}),
  );
  assert.equal(err.status, 422);
  assert.equal(err.detail, "field required；string too short");
  assert.ok(!err.message.includes("[object Object]"));
  assert.equal(err.message, err.detail);
});

test("字符串/数字/空 detail 一律回退到状态码文案", async () => {
  reset();
  for (const body of [{ detail: "" }, { detail: 0 }, { detail: false }, { detail: [] }, {}]) {
    const err = await captureError(
      () => FAIL(400, body),
      () => API.apiPost("/api/paper/pause"),
    );
    assert.equal(err.message, "请求失败 HTTP 400", `body=${JSON.stringify(body)}`);
    assert.equal(err.detail, "");
  }
});

test("ApiError 绝不携带操作员凭据", async () => {
  reset();
  API.setOperatorToken(TOKEN);
  const err = await captureError(
    () => FAIL(401, { detail: "需要操作员授权" }),
    () => API.apiPost("/api/paper/pause"),
  );
  const blob = [
    err.message,
    err.detail,
    String(err),
    err.stack || "",
    JSON.stringify(err),
    JSON.stringify(err.payload),
  ].join("\n");
  assert.ok(!blob.includes(TOKEN), "错误对象不得泄漏凭据");
  assert.ok(!blob.includes("Bearer"), "错误对象不得回显 Authorization 头");
});

test("ApiError 不把请求头带进 payload（服务端没说的一律不带）", async () => {
  reset();
  API.setOperatorToken(TOKEN);
  const err = await captureError(
    () => FAIL(503, { detail: "上游不可用" }),
    () => API.apiPostJson("/api/strategies", {}),
  );
  assert.deepEqual(Object.keys(err.payload), ["detail"]);
});

// ─────────────────────────────────────────────
// 8. 源码守卫：单一 fetch 调用点 + 公开面不缩水
// ─────────────────────────────────────────────

/**
 * 单遍扫描，去掉注释与字符串/模板字面量，只留下**可执行标识符**。
 * 这样注释里写 "fetch" 不会影响守卫，而真正的 `fetch(...)` 调用会留下。
 */
function stripCommentsAndStrings(source) {
  let out = "";
  let i = 0;
  while (i < source.length) {
    const ch = source[i];
    const next = source[i + 1];
    if (ch === "/" && next === "/") {
      while (i < source.length && source[i] !== "\n") i += 1;
      continue;
    }
    if (ch === "/" && next === "*") {
      i += 2;
      while (i < source.length && !(source[i] === "*" && source[i + 1] === "/")) i += 1;
      i += 2;
      continue;
    }
    if (ch === '"' || ch === "'" || ch === "`") {
      const quote = ch;
      i += 1;
      while (i < source.length) {
        if (source[i] === "\\") {
          i += 2;
          continue;
        }
        if (source[i] === quote) {
          i += 1;
          break;
        }
        i += 1;
      }
      out += '""';
      continue;
    }
    out += ch;
    i += 1;
  }
  return out;
}

function walkJs(dir) {
  const found = [];
  for (const name of readdirSync(dir)) {
    const full = path.join(dir, name);
    if (statSync(full).isDirectory()) found.push(...walkJs(full));
    else if (name.endsWith(".js")) found.push(full);
  }
  return found;
}

test("core/api.js 只有一处 fetch 调用点（单一原语，防止再次漂移）", () => {
  const code = stripCommentsAndStrings(readFileSync(API_JS, "utf8"));
  assert.match(code, /function\s+request\s*\(/, "扫描器必须真的看到了代码（防自身失效）");
  const hits = code.match(/\bfetch\s*\(/g) || [];
  assert.equal(
    hits.length,
    1,
    `core/api.js 必须只有一处 fetch(（四份实现漂移的根因），实际 ${hits.length} 处`,
  );
});

test("其它前端模块不得绕过 core/api.js 直接 fetch", () => {
  const offenders = [];
  for (const file of walkJs(SRC_DIR)) {
    if (file === API_JS) continue;
    const code = stripCommentsAndStrings(readFileSync(file, "utf8"));
    if (/\bfetch\s*\(/.test(code)) offenders.push(path.relative(SRC_DIR, file));
  }
  assert.deepEqual(offenders, [], "所有 HTTP 请求必须经由 core/api.js");
});

test("公开面不缩水（既有 helper 与常量都必须保留）", () => {
  for (const name of [
    "OPERATOR_TOKEN_KEY",
    "getOperatorToken",
    "setOperatorToken",
    "clearOperatorToken",
    "isOperatorUnlocked",
    "operatorAuthorizationHeaders",
    "api",
    "apiPost",
    "apiPostJson",
    "apiJson",
    "ApiError",
  ]) {
    assert.ok(name in API, `core/api.js 必须继续导出 ${name}`);
  }
});

test("源码保留只读短路与固定存储 key（后端存在性守卫的锚点）", () => {
  const text = readFileSync(API_JS, "utf8");
  assert.ok(text.includes("indexOf(verb)>=0"), "只读短路分支必须存在");
  assert.ok(text.includes("READ_METHODS"));
  assert.ok(text.includes("astock.operatorToken.v1"));
  assert.ok(!text.includes("X-Operator-Token"));
});

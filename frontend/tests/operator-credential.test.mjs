// PR-2：操作员凭据的**行为**契约测试（Node，无浏览器）。
//
// 为什么需要它：Python 侧的 `test_operator_boundary.py` 只能检查源码里"有没有
// 某个字符串"，那是**存在性**断言，删掉关键分支也照样通过（负向验证 N4 实测
// 证实了这一点）。本文件直接 import 真实的 `src/core/api.js`，调用真实函数，
// 断言真实返回值——因此能真正锁住"GET 绝不带凭据"这条语义。
//
// 运行：node --test tests/operator-credential.test.mjs
// 由 CI 的 frontend job 执行（见 .github/workflows/ci.yml）。

import assert from "node:assert/strict";
import test from "node:test";

// ─── 在 import 之前装好 sessionStorage 替身 ───
// api.js 的 storage() 会 try/catch 访问 sessionStorage；Node 里没有这个全局，
// 所以必须提前注入，否则所有 getter 都返回空串，测不出真实行为。
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
  // 同时提供 localStorage 替身，用于断言"凭据绝不写进 localStorage"。
  const local = new Map();
  globalThis.localStorage = {
    getItem: (k) => (local.has(k) ? local.get(k) : null),
    setItem: (k, v) => local.set(k, String(v)),
    removeItem: (k) => local.delete(k),
    clear: () => local.clear(),
    key: (i) => Array.from(local.keys())[i] ?? null,
    get length() {
      return local.size;
    },
  };
  return { session: map, local };
}

const store = installStorage();
const API = await import("../src/core/api.js");

const TOKEN = "zz-node-test-operator-placeholder";
const READ_METHODS = ["GET", "HEAD", "OPTIONS"];
const WRITE_METHODS = ["POST", "PUT", "PATCH", "DELETE"];

function reset() {
  store.session.clear();
  store.local.clear();
  API.clearOperatorToken();
}

// ─────────────────────────────────────────────
// 存储位置与 key
// ─────────────────────────────────────────────

test("凭据 key 是固定的 astock.operatorToken.v1", () => {
  assert.equal(API.OPERATOR_TOKEN_KEY, "astock.operatorToken.v1");
});

test("setOperatorToken 写入 sessionStorage 的固定 key", () => {
  reset();
  API.setOperatorToken(TOKEN);
  assert.equal(store.session.get("astock.operatorToken.v1"), TOKEN);
});

test("凭据绝不写入 localStorage", () => {
  reset();
  API.setOperatorToken(TOKEN);
  assert.equal(store.local.size, 0, "localStorage 必须保持为空");
  assert.equal(globalThis.localStorage.getItem("operatorToken"), null);
});

test("清除授权后 sessionStorage 不再有凭据", () => {
  reset();
  API.setOperatorToken(TOKEN);
  API.clearOperatorToken();
  assert.equal(store.session.get("astock.operatorToken.v1"), undefined);
  assert.equal(API.getOperatorToken(), "");
  assert.equal(API.isOperatorUnlocked(), false);
});

test("setOperatorToken 会 trim，空值等价于清除", () => {
  reset();
  API.setOperatorToken("   ");
  assert.equal(API.getOperatorToken(), "");
});

// ─────────────────────────────────────────────
// 核心：GET 绝不带凭据（N4 的行为级防线）
// ─────────────────────────────────────────────

test("GET / HEAD / OPTIONS 绝不携带 Authorization（即使已解锁）", () => {
  reset();
  API.setOperatorToken(TOKEN); // 先解锁
  for (const method of READ_METHODS) {
    const headers = API.operatorAuthorizationHeaders(method, {});
    assert.ok(
      !("Authorization" in headers),
      `${method} 不得携带 Authorization，实际 headers=${JSON.stringify(headers)}`,
    );
    assert.ok(
      !("authorization" in headers),
      `${method} 不得携带小写 authorization`,
    );
  }
});

test("只读方法的大小写变体同样不带凭据", () => {
  reset();
  API.setOperatorToken(TOKEN);
  for (const method of ["get", "Get", "head", "Head", "options", "OPTIONS"]) {
    const headers = API.operatorAuthorizationHeaders(method, {});
    assert.ok(!("Authorization" in headers), `${method} 不得携带 Authorization`);
  }
});

test("未提供 method 时默认按只读处理，不带凭据", () => {
  reset();
  API.setOperatorToken(TOKEN);
  const headers = API.operatorAuthorizationHeaders(undefined, {});
  assert.ok(!("Authorization" in headers), "缺省方法不得携带凭据");
});

// ─────────────────────────────────────────────
// 写方法必须带 Bearer
// ─────────────────────────────────────────────

test("POST / PUT / PATCH / DELETE 携带标准 authorization 头（Bearer 方案）", () => {
  reset();
  API.setOperatorToken(TOKEN);
  for (const method of WRITE_METHODS) {
    const headers = API.operatorAuthorizationHeaders(method, {});
    assert.equal(
      headers.Authorization,
      "Bearer " + TOKEN,
      `${method} 必须携带 Bearer 凭据`,
    );
  }
});

test("写方法的大小写变体同样携带 Bearer", () => {
  reset();
  API.setOperatorToken(TOKEN);
  for (const method of ["post", "Put", "patch", "Delete"]) {
    const headers = API.operatorAuthorizationHeaders(method, {});
    assert.equal(headers.Authorization, "Bearer " + TOKEN, method);
  }
});

test("未解锁时写方法不带凭据（由服务端按契约拒绝）", () => {
  reset();
  for (const method of WRITE_METHODS) {
    const headers = API.operatorAuthorizationHeaders(method, {});
    assert.ok(!("Authorization" in headers), `${method} 未解锁时不应带凭据`);
  }
});

test("保留调用方传入的其它 header", () => {
  reset();
  API.setOperatorToken(TOKEN);
  const headers = API.operatorAuthorizationHeaders("POST", {
    "Content-Type": "application/json",
  });
  assert.equal(headers["Content-Type"], "application/json");
  assert.equal(headers.Authorization, "Bearer " + TOKEN);
});

test("绝不使用私有 header X-Operator-Token", () => {
  reset();
  API.setOperatorToken(TOKEN);
  for (const method of [...READ_METHODS, ...WRITE_METHODS]) {
    const headers = API.operatorAuthorizationHeaders(method, {});
    assert.ok(
      !("X-Operator-Token" in headers) && !("x-operator-token" in headers),
      `${method} 不得使用私有 header`,
    );
  }
});

// ─────────────────────────────────────────────
// 端到端：真正发起 fetch 时也不泄漏（拦截 fetch）
// ─────────────────────────────────────────────

test("真实 api() 调用 GET 时不发送 Authorization", async () => {
  reset();
  API.setOperatorToken(TOKEN);
  const seen = [];
  const original = globalThis.fetch;
  globalThis.fetch = async (url, options) => {
    seen.push({ url, method: (options && options.method) || "GET", headers: options && options.headers });
    return {
      ok: true,
      status: 200,
      json: async () => ({ ok: true }),
    };
  };
  try {
    await API.api("/api/version");
  } finally {
    globalThis.fetch = original;
  }
  assert.equal(seen.length, 1);
  const headers = seen[0].headers || {};
  assert.ok(
    !("Authorization" in headers),
    `真实 GET 不得携带 Authorization，实际=${JSON.stringify(headers)}`,
  );
});

test("真实 apiJson(POST) 调用会发送 Authorization", async () => {
  reset();
  API.setOperatorToken(TOKEN);
  const seen = [];
  const original = globalThis.fetch;
  globalThis.fetch = async (url, options) => {
    seen.push({ method: (options && options.method) || "GET", headers: options && options.headers });
    return { ok: true, status: 200, json: async () => ({ ok: true }) };
  };
  try {
    await API.apiJson("/api/paper/start", "POST", { confirmed: true });
  } finally {
    globalThis.fetch = original;
  }
  assert.equal(seen.length, 1);
  assert.equal(seen[0].headers.Authorization, "Bearer " + TOKEN);
});

// ─────────────────────────────────────────────
// 重试作用域：只读方法才重试，写方法永不重放
// ─────────────────────────────────────────────
//
// 复审 advisory：api(path, options) 是通用入口，之前重试上限与 method 无关，
// 传 options.method='POST' 时一次 502/503/504 会被自动重放最多 4 次，可能造成
// 重复副作用（重复下单/重复启动）。这里用**行为**断言锁住"写方法只尝试 1 次"。
//
// 加速：api.js 的退避时长恰为 800/1600/2400ms（见 800*(attempt+1)）。把它们
// 压缩为 0 以缩短测试时间；其余定时器（AbortController 的 25000ms 超时）保持
// 真实。若将来退避公式变化，测试只会变慢，断言仍然有效。

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

/** 用固定状态序列驱动 fetch，返回被调用的次数与方法。 */
function stubFetch(statuses) {
  const seen = [];
  let i = 0;
  globalThis.fetch = async (url, options) => {
    const status = statuses[Math.min(i, statuses.length - 1)];
    i += 1;
    seen.push({ url, method: (options && options.method) || "GET" });
    return { ok: status < 400, status, json: async () => ({ detail: "stub" }) };
  };
  return seen;
}

test("GET 遇 503 会重试（最终成功）", async () => {
  reset();
  const original = globalThis.fetch;
  const seen = stubFetch([503, 200]);
  try {
    await withFastBackoff(() => API.api("/api/version"));
  } finally {
    globalThis.fetch = original;
  }
  assert.equal(seen.length, 2, "GET 应在 503 后重试一次");
});

test("GET 持续 503 时尝试次数为 4 且最终抛错", async () => {
  reset();
  const original = globalThis.fetch;
  const seen = stubFetch([503]);
  let err = null;
  try {
    await withFastBackoff(() => API.api("/api/version"));
  } catch (e) {
    err = e;
  } finally {
    globalThis.fetch = original;
  }
  assert.ok(err, "持续 503 必须抛错，不能静默返回");
  assert.equal(seen.length, 4, "GET 重试上限应为 4 次");
});

test("GET 遇 500（非 502/503/504）只尝试 1 次", async () => {
  reset();
  const original = globalThis.fetch;
  const seen = stubFetch([500]);
  let err = null;
  try {
    await withFastBackoff(() => API.api("/api/version"));
  } catch (e) {
    err = e;
  } finally {
    globalThis.fetch = original;
  }
  assert.ok(err);
  assert.equal(seen.length, 1, "500 不在重试白名单内");
});

for (const method of WRITE_METHODS) {
  for (const status of [502, 503, 504]) {
    test(`${method} 遇 ${status} 绝不重放（只尝试 1 次）`, async () => {
      reset();
      API.setOperatorToken(TOKEN);
      const original = globalThis.fetch;
      const seen = stubFetch([status]);
      let err = null;
      try {
        await withFastBackoff(() => API.api("/api/paper/start", { method }));
      } catch (e) {
        err = e;
      } finally {
        globalThis.fetch = original;
      }
      assert.ok(err, `${method} ${status} 必须抛错`);
      assert.equal(seen.length, 1, `${method} 是写方法，不得重放（实际 ${seen.length} 次）`);
    });
  }
}

test("写方法遇网络异常（fetch reject）也不重放", async () => {
  reset();
  API.setOperatorToken(TOKEN);
  const original = globalThis.fetch;
  let calls = 0;
  globalThis.fetch = async () => {
    calls += 1;
    throw new Error("network down");
  };
  let err = null;
  try {
    await withFastBackoff(() => API.api("/api/paper/start", { method: "POST" }));
  } catch (e) {
    err = e;
  } finally {
    globalThis.fetch = original;
  }
  assert.ok(err);
  assert.equal(calls, 1, "写方法的网络异常不得触发重试");
});

test("写方法重放场景下凭据仍只发送标准 Bearer 头", async () => {
  reset();
  API.setOperatorToken(TOKEN);
  const original = globalThis.fetch;
  const headers = [];
  globalThis.fetch = async (url, options) => {
    headers.push(options && options.headers);
    return { ok: false, status: 503, json: async () => ({ detail: "stub" }) };
  };
  try {
    await withFastBackoff(() => API.api("/api/paper/start", { method: "DELETE" }));
  } catch (e) {
    /* 预期抛错 */
  } finally {
    globalThis.fetch = original;
  }
  assert.equal(headers.length, 1);
  assert.equal(headers[0].Authorization, "Bearer " + TOKEN);
  assert.ok(!("X-Operator-Token" in headers[0]));
});

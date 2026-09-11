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

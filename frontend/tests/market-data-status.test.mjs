// R24：前端**只渲染**后端给出的行情事实状态。
//
// 为什么需要它：R24 把 fresh/stale/degraded/unavailable 的判决收敛到后端
// Market Data Authority。前端的职责只有两件——把后端给的维度渲染出来，
// 以及**不重算**新鲜度。这两条都是"名字没变、语义变了"的类型：
//
//   1. 如果前端自己写 `Date.now() - timestamp > 240000`，它在 240s 这个
//      后端 policy 上制造了第二份权威；后端改 policy 时前端会静默漂移。
//   2. 如果前端在 reason 缺失时自己推断原因（"加载失败"），用户看到的
//      解释就与后端的事实状态脱钩。
//
// 因此本文件直接 import 真实的 `src/features/paper.js`，调用真实函数、
// 断言可观测输出：状态标签、as-of、reason 是否原样来自后端 payload。
//
// 运行：node --test tests/market-data-status.test.mjs
// 由 CI 的 frontend job 执行（`npm run test:unit` 覆盖 tests/*.test.mjs）。

import assert from "node:assert/strict";
import test from "node:test";
import { readFileSync } from "node:fs";
import { fileURLToPath } from "node:url";
import path from "node:path";

const here = path.dirname(fileURLToPath(import.meta.url));
const SRC = path.join(here, "..", "src", "features", "paper.js");
const source = readFileSync(SRC, "utf8");

// paper.js 在模块顶层只做 import；DOM/网络访问都在函数体内，可直接 import。
const paper = await import(
  new URL("../src/features/paper.js", import.meta.url).href
);

const STATUS_CASES = [
  ["fresh", "可信"],
  ["stale", "已过期"],
  ["degraded", "降级"],
  ["unverified", "未通过核验"],
  ["unavailable", "不可用"],
];

test("五种后端状态各自渲染成人可读标签，并带可断言的 data-market-status", () => {
  for (const [status, label] of STATUS_CASES) {
    const html = paper.paperMarketDataHtml({ status });
    assert.equal(
      html.includes(`data-market-status="${status}"`),
      true,
      `${status} 未渲染成 data-market-status`,
    );
    assert.equal(html.includes(label), true, `${status} 未渲染成「${label}」`);
  }
});

test("as-of 与 reason 原样取自后端 payload，不由前端推断", () => {
  const html = paper.paperMarketDataHtml({
    status: "stale",
    as_of: "2026-08-28",
    observed_at: "2026-08-28T10:28:03+08:00",
    reason: "provider_unavailable",
    verification: "single_source",
  });
  assert.equal(html.includes("2026-08-28T10:28:03+08:00"), true, "未展示最后可信时间");
  assert.equal(html.includes("行情源不可用"), true, "reason 未按后端语义渲染");
  assert.equal(html.includes("single_source"), true, "verification 未如实展示");
});

test("未知状态/reason 直接透出而不是猜一个更好听的词", () => {
  const html = paper.paperMarketDataHtml({
    status: "brand_new_state",
    reason: "some_new_reason",
  });
  assert.equal(html.includes("brand_new_state"), true, "未知状态被前端改写");
  assert.equal(html.includes("some_new_reason"), true, "未知 reason 被前端改写");
});

test("缺失 payload 时默认 unavailable，绝不默认可信", () => {
  for (const missing of [undefined, null, {}]) {
    const html = paper.paperMarketDataHtml(missing);
    assert.equal(
      html.includes("data-market-status=\"unavailable\""),
      true,
      `缺失 payload（${JSON.stringify(missing)}）未降级为 unavailable`,
    );
    assert.equal(html.includes("可信"), false, "缺失 payload 被当成可信");
  }
});

test("前端不重算 freshness：源码里没有本机时钟与毫秒阈值比较", () => {
  // 真实风险是"拿 Date.now() 与一个毫秒阈值比较"。注释里提到这个反例不算。
  for (const forbidden of [
    "Date.now()-timestamp",
    "Date.now() - timestamp",
    "240 * 1000",
    ">240000",
    "> 240000",
  ]) {
    assert.equal(
      source.includes(forbidden),
      false,
      `前端复制了后端 freshness 阈值：${forbidden}`,
    );
  }
});

test("前端不理解 provider 机制：普通 UI 不出现重试/熔断/缓存键", () => {
  for (const leaked of [
    "eastmoney_retry",
    "tencent_fallback",
    "cache_key",
    "circuit_open",
    "retry_after_seconds",
  ]) {
    assert.equal(
      source.includes(leaked),
      false,
      `普通运行 UI 泄露了 provider 细节：${leaked}`,
    );
  }
});

test("两个用户可见入口都渲染行情状态", () => {
  // 运行策略页与持仓页总资金池卡片。
  const occurrences = source.split("paperMarketDataHtml(").length - 1;
  assert.ok(
    occurrences >= 3,
    `paperMarketDataHtml 只有 ${occurrences} 处引用（定义 + 2 个渲染点）`,
  );
});

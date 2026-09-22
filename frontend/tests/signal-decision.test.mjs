// R25：前端**只渲染**后端给出的 signal 裁决与证据，不复制 signal 业务规则。
//
// 为什么需要它：R25 把"这条 signal 为什么被写进系统"的答案收敛到后端
// Signal Pipeline（Candidate → Evidence → Decision → Ledger）。前端的职责只有
// 渲染后端投影出来的 outcome / reason / evidence 状态。两条风险都是
// "名字没变、语义变了"的类型：
//
//   1. 如果前端自己写 `verification === 'verified'` 就显示"双源验证通过"，
//      它在后端 policy 之上制造了第二份权威 —— 而 R24 已明确 ``verified``
//      只表示"该 kind 的 policy 通过"（coverage_integrity 也是 verified，
//      却**不是**双源核验）。后端改语义时前端会静默漂移。
//   2. 如果前端自己按 status 推断"是否通过"，用户看到的裁决就与后端落库的
//      裁决脱钩。
//
// 因此本文件 import 真实的 `src/core/format.js`，调用真实函数、断言可观测输出。
//
// 运行：node --test tests/signal-decision.test.mjs
// 由 CI 的 frontend job 执行（`npm run test:unit` 覆盖 tests/*.test.mjs）。

import assert from "node:assert/strict";
import test from "node:test";
import { readFileSync } from "node:fs";
import { fileURLToPath } from "node:url";
import path from "node:path";

const here = path.dirname(fileURLToPath(import.meta.url));
const CORE = path.join(here, "..", "src", "core", "format.js");
const PAPER = path.join(here, "..", "src", "features", "paper.js");
const coreSource = readFileSync(CORE, "utf8");
const paperSource = readFileSync(PAPER, "utf8");

const format = await import(new URL("../src/core/format.js", import.meta.url).href);

test("后端给出的 outcome/reason 被原样渲染，不由前端推断", () => {
  const approved = format.signalDecisionView({
    outcome: "approved",
    status: "pending",
    reason: "",
    evidence: { verification: "verified", verification_method: "cross_source", cross_source_verified: true },
  });
  assert.equal(approved.outcomeText, "已通过");
  assert.equal(approved.crossSourceVerified, true);

  const blocked = format.signalDecisionView({
    outcome: "blocked",
    status: "blocked",
    reason: "实时行情未通过独立交叉核验",
    evidence: { verification: "single_source", verification_method: "cross_source", cross_source_verified: false },
  });
  assert.equal(blocked.outcomeText, "未通过");
  assert.equal(blocked.reason, "实时行情未通过独立交叉核验");
  assert.equal(blocked.crossSourceVerified, false);
});

test("未知 outcome 直接透出状态而不是猜一个更好听的说法", () => {
  const view = format.signalDecisionView({ outcome: "some_future_outcome", status: "x" });
  assert.equal(view.outcomeText, "待复核", "未知 outcome 被前端改写成了确定结论");
  assert.equal(view.crossSourceVerified, false, "未知 outcome 被当成双源通过");
});

test("双源结论只认后端的 cross_source_verified，而不是 verification==='verified'", () => {
  // coverage_integrity 的 verified 绝不是双源核验（R24 §3）。后端会下发
  // cross_source_verified=false；前端必须照此渲染，不得因为 verification 是
  // "verified" 就显示"双源可信"。
  const coverageOnly = format.signalDecisionView({
    outcome: "approved",
    evidence: { verification: "verified", verification_method: "coverage_integrity", cross_source_verified: false },
  });
  assert.equal(coverageOnly.crossSourceVerified, false);
  assert.equal(
    coverageOnly.evidenceText.includes("双源可信"),
    false,
    "verification=verified + coverage_integrity 被前端渲染成『双源可信』",
  );
});

test("缺失 payload 时不得默认可信", () => {
  for (const missing of [undefined, null, {}]) {
    const view = format.signalDecisionView(missing);
    assert.equal(view.crossSourceVerified, false, `缺失 payload（${JSON.stringify(missing)}）被当成双源可信`);
    assert.equal(view.evidenceText.includes("双源可信"), false, "缺失 payload 被渲染成双源可信");
  }
});

test("前端不重算 signal 规则：源码里没有 verified 与双源的等价判断", () => {
  // 真实风险是"把 verification === 'verified' 当成双源"。注释里提到这个反例不算。
  for (const forbidden of [
    "verification==='verified'",
    'verification === "verified"',
    "verification=='verified'",
  ]) {
    assert.equal(
      coreSource.includes(forbidden),
      false,
      `前端把 verified 等同成双源核验：${forbidden}`,
    );
    assert.equal(
      paperSource.includes(forbidden),
      false,
      `运行页把 verified 等同成双源核验：${forbidden}`,
    );
  }
});

test("signal 生命周期状态渲染成人可读标签，而不是 raw token", () => {
  const cases = [
    ["deferred_capacity", "容量等待"],
    ["entry_frozen_waitlist", "冻结待买"],
    ["recheck_capacity", "容量复核"],
    ["expired", "已过期"],
  ];
  for (const [status, label] of cases) {
    const html = format.paperStatusTag(status);
    assert.equal(html.includes(label), true, `${status} 未渲染成「${label}」`);
    assert.equal(
      html.includes(`>${status}<`),
      false,
      `${status} 仍以 raw token 直接展示`,
    );
  }
});

test("信号审计表展示裁决/证据列，且说明来自后端", () => {
  assert.equal(
    paperSource.includes("signalDecisionView("),
    true,
    "运行页没有渲染后端 signal 裁决投影",
  );
  assert.equal(
    paperSource.includes("裁决/证据"),
    true,
    "信号审计表没有裁决/证据列",
  );
  assert.equal(coreSource.includes("export function signalDecisionView"), true);
});

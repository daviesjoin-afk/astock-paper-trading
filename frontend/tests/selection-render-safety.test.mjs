// 选股/跟踪渲染的对抗性回归：
//   1. 后端/行情源下发的自由文本（股票名、行业、入选理由、买入摘要、舆情否决原因、
//      门控说明、信号消息、卖出摘要、免责声明）进入 innerHTML 前必须转义 ——
//      股票名来自外部行情源，是典型的存储型 XSS 入口。
//   2. 缺失字段是 undefined 而不是 null。旧代码用 `!==null` 判断，缺行情时会把
//      "undefined日"、"-%"、"--%" 和亏损配色渲染给用户，把"没有数据"伪装成
//      "确定的坏结果"。缺失必须显示 '—'。
import assert from "node:assert/strict";
import test from "node:test";
import { readFileSync } from "node:fs";
import { fileURLToPath } from "node:url";
import path from "node:path";

const here = path.dirname(fileURLToPath(import.meta.url));
const selection = await import(new URL("../src/features/selection.js", import.meta.url).href);

function withMockDocument(nodes, fn) {
  const previous = globalThis.document;
  globalThis.document = {
    getElementById: (id) => nodes[id] || null,
    querySelectorAll: () => [],
  };
  try {
    return fn();
  } finally {
    if (previous === undefined) delete globalThis.document;
    else globalThis.document = previous;
  }
}

const XSS = '<img src=x onerror=alert(1)>';

test("选股结果中的后端自由文本被转义，不产生可执行标签", () => {
  const target = { innerHTML: "" };
  withMockDocument({ selectResult: target }, () => {
    selection.renderSelectionResult({
      picks: [{
        name: XSS,
        code: "<script>code</script>",
        industry: "<b>行业</b>",
        reasons: ["<svg onload=alert(1)>"],
        price: 10,
        pct: 1,
        super_net: 1e8,
        mom5: 1, mom20: 2, mom60: 3,
        net_profit: null,
        annual_net_profit: null,
        report_date: "<i>2026-01-01</i>",
        buy_decision: { tier: "<em>T1</em>", action: "<u>买入</u>", summary: "<img src=y>" },
      }],
      news_vetoed: [{ name: XSS, code: "<x>", reason: "<script>veto</script>" }],
      gate: { light: "green", advice: "<img src=gate>" },
      disclaimer: "<img src=disclaimer>",
      universe_size: null,
      total_universe: null,
    });
  });
  const html = target.innerHTML;
  assert.equal(html.includes("<img"), false, `未转义 <img>：${html.slice(0, 300)}`);
  assert.equal(html.includes("<script"), false, "未转义 <script>");
  assert.equal(html.includes("<svg"), false, "未转义 <svg>");
  assert.equal(html.includes("<b>行业</b>"), false, "行业字段未转义");
  assert.equal(html.includes("&lt;img src=x onerror=alert(1)&gt;"), true, "股票名未按实体输出");
});

test("选股汇总缺失计数不显示 undefined/NaN", () => {
  const target = { innerHTML: "" };
  withMockDocument({ selectResult: target }, () => {
    selection.renderSelectionResult({
      picks: [{ name: "平安银行", code: "000001", price: 10, pct: 0 }],
      count: undefined,
      executable_count: undefined,
      watchlist_count: undefined,
    });
  });
  const html = target.innerHTML;
  assert.equal(html.includes("undefined"), false, "缺失计数渲染成 undefined");
  assert.equal(html.includes("NaN"), false, "缺失计数做减法渲染成 NaN");
  assert.equal(html.includes("T4/T5"), false, "总数缺失时仍展示推算出的 T4/T5 数量");
});

test("跟踪池缺失收益/回撤/持有天数显示占位符，不显示 -% / --% / undefined日", () => {
  const table = { innerHTML: "" };
  const summary = { innerHTML: "" };
  const count = { textContent: "" };
  withMockDocument({ trackTable: table, trackSummary: summary, trackFilterCount: count }, () => {
    selection.renderTrack({
      positions: [{
        name: "测试股",
        code: "600000",
        cost: 10,
        price: 11,
        pct_today: 1,
        ret_pct: undefined,
        drawdown_from_peak: undefined,
        hold_days: undefined,
        action: "关注",
        signals: [{ level: "sell", type: "T", msg: XSS }],
        sell_decision: undefined,
        strategy: "tq_breakout",
      }],
      portfolio: {
        count: 1,
        avg_ret_pct: 0,
        avg_peak_drawdown: null,
        sell_signals: null,
        warn_signals: null,
        breaker: undefined,
        concentration_warnings: [XSS],
      },
      disclaimer: XSS,
      checked_at: "2026-09-01 15:00:00",
    });
  });
  const html = table.innerHTML + summary.innerHTML;
  for (const bad of ["undefined", "NaN", "--%", "null"]) {
    assert.equal(html.includes(bad), false, `缺失值渲染出了 ${bad}`);
  }
  assert.equal(html.includes("<img"), false, "跟踪池自由文本未转义");
  assert.equal(html.includes("&lt;img src=x onerror=alert(1)&gt;"), true, "信号消息/免责声明未转义");
  assert.match(html, /—/, "缺失值应显示占位符 —");
  assert.equal(html.includes("undefined｜undefined"), false, "缺失卖出决策被印成 undefined 标签");
});

test("selection.js 不再使用浏览器原生 alert/confirm（对话框层唯一）", () => {
  const source = readFileSync(path.join(here, "..", "src", "features", "selection.js"), "utf8");
  // confirmDialog(...) 合法；只有原生 confirm( / alert( 才是违约。
  for (const forbidden of ["alert(", "window.alert(", "confirm(", "window.confirm("]) {
    assert.equal(source.includes(forbidden), false, `selection.js 仍在使用原生 ${forbidden}`);
  }
  assert.equal(source.includes("confirmDialog("), true, "批量/移出操作未走应用内确认对话框");
});

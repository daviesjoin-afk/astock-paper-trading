import { adaptiveEsc, fmt, paperStatusTag, riskText, signalDecisionView, zhRiskText } from "../core/format.js";

export const SIGNAL_AUDIT_COLUMNS = Object.freeze([
  "策略",
  "标的",
  "信号日",
  "行情快照",
  "计划执行日",
  "实际执行",
  "独立模型评分",
  "状态",
  "裁决/证据",
  "说明",
]);

export function renderSignalAuditTable(signals, accountName) {
  const rows = (signals || []).map(function (signal) {
    const model = (signal.payload && signal.payload.decision && signal.payload.decision.entry_model) || {};
    const audit = signal.audit || {};
    const quotePct = audit.signal_quote_pct;
    // 分隔符此前是一个字面量 " ? "（历史编码损坏），行情快照列会显示成
    // "2026-09-01 10:00 ? +1.23%"，操作员读起来像缺值。恢复成间隔点。
    const marketText = adaptiveEsc(audit.signal_quote_at || "—")
      + (typeof quotePct === "number" ? " · " + (quotePct >= 0 ? "+" : "") + fmt(quotePct, 2) + "%" : "");
    const actual = audit.execution_status === "filled"
      ? (adaptiveEsc(audit.executed_at || "—") + "<br><small>行情 " + adaptiveEsc(audit.execution_quote_at || "—") + "</small>")
      : "未成交<br><small>" + (signal.status === "blocked" || signal.status === "rejected" ? "信号时点风控拦截" : "尚未执行") + "</small>";
    const decision = signalDecisionView(signal.signal_decision);
    const decisionText = "<b>" + riskText(decision.outcomeText) + "</b>"
      + "<br><small>" + riskText(decision.evidenceText) + "</small>";
    // zhRiskText 内部已经做过 HTML 转义；再包一层 riskText 会把 `&` 变成
    // `&amp;amp;`，页面上直接显示 "&amp;"。这里只做一次。
    const reasonText = zhRiskText(decision.reason || signal.reason || "待实时行情与账户风控复核");
    /* 股票名/代码/账户名/模型名来自外部行情源与后端投影，不是前端常量：
       本表此前把这些字段裸拼进 innerHTML，与 selection.js 被修掉的是同一类注入面。 */
    return "<tr><td>" + adaptiveEsc(accountName[signal.account_id] || signal.account_id)
      + "</td><td><b>" + adaptiveEsc(signal.name) + "</b><br/><span style=\"font-size:11px;color:var(--text-muted)\">" + adaptiveEsc(signal.code)
      + "</span></td><td>" + adaptiveEsc(audit.factor_date || signal.signal_date || "—")
      + "</td><td>" + marketText
      + "</td><td>" + adaptiveEsc(audit.planned_review_date || signal.intended_date || "—")
      + "</td><td>" + actual
      + "</td><td>" + adaptiveEsc(model.name || "独立入场模型") + "<br><small>" + fmt(signal.t_score, 2)
      + "</small></td><td>" + paperStatusTag(signal.status)
      + "</td><td>" + decisionText
      + "</td><td style=\"font-size:12px\">" + reasonText + "</td></tr>";
  }).join("");
  const headers = SIGNAL_AUDIT_COLUMNS.map(function (label) {
    return "<th>" + label + "</th>";
  }).join("");

  return "<table class=\"signal-audit-table\"><thead><tr>" + headers
    + "</tr></thead><tbody>" + rows + "</tbody></table>";
}

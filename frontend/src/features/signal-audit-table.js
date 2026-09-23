import { fmt, paperStatusTag, riskText, signalDecisionView, zhRiskText } from "../core/format.js";

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
    const marketText = (audit.signal_quote_at || "—")
      + (typeof quotePct === "number" ? " ? " + (quotePct >= 0 ? "+" : "") + fmt(quotePct, 2) + "%" : "");
    const actual = audit.execution_status === "filled"
      ? ((audit.executed_at || "—") + "<br><small>行情 " + (audit.execution_quote_at || "—") + "</small>")
      : "未成交<br><small>" + (signal.status === "blocked" || signal.status === "rejected" ? "信号时点风控拦截" : "尚未执行") + "</small>";
    const decision = signalDecisionView(signal.signal_decision);
    const decisionText = "<b>" + riskText(decision.outcomeText) + "</b>"
      + "<br><small>" + riskText(decision.evidenceText) + "</small>";
    const reasonText = riskText(zhRiskText(decision.reason || signal.reason || "待实时行情与账户风控复核"));
    return "<tr><td>" + (accountName[signal.account_id] || signal.account_id)
      + "</td><td><b>" + signal.name + "</b><br/><span style=\"font-size:11px;color:var(--text-muted)\">" + signal.code
      + "</span></td><td>" + (audit.factor_date || signal.signal_date || "—")
      + "</td><td>" + marketText
      + "</td><td>" + (audit.planned_review_date || signal.intended_date || "—")
      + "</td><td>" + actual
      + "</td><td>" + (model.name || "独立入场模型") + "<br><small>" + fmt(signal.t_score, 2)
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

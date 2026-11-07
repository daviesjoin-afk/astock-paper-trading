// Presentation helpers. Labels translate kernel identifiers into business
// wording; raw identifiers stay out of the page (technical details panel only).
// No valuation, return or price is derived here.

export const money = (cents: number): string =>
  "¥" + (cents / 100).toLocaleString("zh-CN", {
    minimumFractionDigits: 2,
    maximumFractionDigits: 2,
  });

export const integer = (value: number): string => value.toLocaleString("zh-CN");

export const experimentStatus = (status: string): string => {
  if (status === "active") return "进行中";
  if (status === "completed") return "已完成";
  return "未知状态";
};

export const strategyName = (id: string): string => {
  if (id === "research-momentum-v1") return "动量策略";
  if (id === "research-reversion-v1") return "均值回归策略";
  return "自定义策略";
};

export const inputName = (id: string): string =>
  id === "offline-research-input-v1" ? "固定离线研究输入" : "自定义研究输入";

export const packName = (id: string): string =>
  id.startsWith("cn-offline-fixture-pack-") ? "离线样本包" : "自定义数据包";

export const policyName = (id: string): string =>
  id === "v2-paper-policy-6" ? "默认执行规则" : "自定义执行规则";

export const sideLabel = (side: string): string => {
  if (side === "buy") return "买入";
  if (side === "sell") return "卖出";
  return side;
};

export const resultLabel = (status: string): string => {
  if (status === "filled") return "已成交";
  if (status === "rejected") return "已拒绝";
  return status;
};

export const signalName = (signalId: string): string => {
  const match = /-(\d{4})-/.exec(signalId);
  return match ? `第 ${Number(match[1])} 条信号` : "模拟信号";
};

const REASONS: Record<string, string> = {
  filled: "已按显式执行价与容量成交",
  quote_evidence_missing: "缺少行情证据",
  quote_from_future: "行情时点晚于决策时点",
  quote_not_yet_available: "行情在决策时点尚不可见",
  stale_quote: "行情超过新鲜度上限",
  weekend_market_closed: "周末休市",
  holiday_market_closed: "节假日休市",
  unknown_trading_date: "日历未覆盖的交易日",
  midday_break: "午间休市",
  outside_trading_session: "不在连续竞价时段",
  opening_auction_not_supported: "不支持开盘集合竞价",
  closing_auction_not_supported: "不支持收盘集合竞价",
  not_tradable: "该标的当前不可交易",
  limit_up_buy_blocked: "涨停禁止买入",
  limit_down_sell_blocked: "跌停禁止卖出",
  execution_evidence_missing: "缺少执行方向证据",
  execution_side_mismatch: "执行证据方向不一致",
  stale_execution_evidence: "执行证据过期",
  position_already_open: "已有持仓，拒绝再买",
  insufficient_budget_for_board_lot_and_fees: "预算不足整手及费用",
  no_t1_sellable_lot: "没有 T+1 可卖持仓",
  unsupported_odd_lot_position: "不支持零股持仓",
  order_quantity_exceeds_instrument_limit: "超过该标的数量上限",
  insufficient_execution_liquidity: "容量不足，整笔拒绝",
  unsupported_instrument_board: "不支持的板块",
  unsupported_security_type: "不支持的类型",
  instrument_identity_missing: "缺少证券身份",
  instrument_date_not_covered: "不在证券覆盖日期内",
  instrument_identity_conflict: "证券身份冲突",
};

export const reasonLabel = (code: string): string => REASONS[code] ?? "其它原因";

/** Fixed-format decision time from the stored RFC3339 string (no timezone math). */
export const decisionTime = (value: string): string => {
  const match = /^(\d{4})-(\d{2})-(\d{2})T(\d{2}):(\d{2})/.exec(value);
  return match ? `${match[2]}-${match[3]} ${match[4]}:${match[5]}` : value;
};

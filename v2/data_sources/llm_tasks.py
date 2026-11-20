#!/usr/bin/env python3
"""The LLM task register: one prompt and one output contract per job.

Every job in this pipeline is a *different* task with a different shape, and
conflating them is how LLM features turn into a single prompt that does a bit
of everything badly. So each entry declares, explicitly:

* what it is given (and refuses to run without),
* what it must return (a schema the caller validates),
* what it may NOT conclude (the boundary that keeps it advisory),
* what happens when it fails (a degraded answer, never a fabricated one).

Two hard rules apply to every job:

1. **No numbers may be invented.** Every figure in an answer must come from
   the input payload the caller supplied. A model that reports "主力净流入
   3.2 亿" when no flow data was provided is producing a hallucination that
   looks exactly like analysis.
2. **Nothing here may produce a trade.** Jobs may describe, rank and explain;
   the ledger only accepts orders from deterministic rules.

A job whose required data is unavailable reports `data_unavailable` instead of
running on a substitute.
"""
from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any, Callable


class TaskError(Exception):
    """A structured failure, not a string. Callers branch on `code`."""

    def __init__(self, code: str, detail: str = "") -> None:
        super().__init__(f"{code}: {detail}" if detail else code)
        self.code = code
        self.detail = detail


# ---------------------------------------------------------------------------
# Output contracts
# ---------------------------------------------------------------------------

def _require_keys(payload: dict, required: tuple[str, ...]) -> None:
    missing = [key for key in required if key not in payload]
    if missing:
        raise TaskError("schema_validation_failed", f"missing {missing}")


def _check_number(value: Any, where: str) -> None:
    """Numbers must be numbers — a numeric string would silently sort wrong."""
    if value is None:
        return
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise TaskError("schema_validation_failed", f"{where} must be numeric, got {type(value).__name__}")


def validate_news_impact(payload: dict) -> dict:
    """News → impact assessment. Every claim cites the item it came from."""
    _require_keys(payload, ("items",))
    items = payload["items"]
    if not isinstance(items, list):
        raise TaskError("schema_validation_failed", "items must be a list")
    for index, item in enumerate(items):
        _require_keys(item, ("ref", "sentiment", "impact", "reason"))
        if item["sentiment"] not in ("positive", "negative", "neutral", "unclear"):
            raise TaskError("schema_validation_failed", f"items[{index}].sentiment invalid")
        if item["impact"] not in ("high", "medium", "low", "unknown"):
            raise TaskError("schema_validation_failed", f"items[{index}].impact invalid")
        if not str(item["reason"]).strip():
            # A conclusion with no cited reason is not usable evidence.
            raise TaskError("schema_validation_failed", f"items[{index}].reason empty")
    return payload


def validate_flow_reading(payload: dict) -> dict:
    """Order-flow → a description of what the numbers show. No forecasting."""
    _require_keys(payload, ("observations", "summary"))
    if not isinstance(payload["observations"], list):
        raise TaskError("schema_validation_failed", "observations must be a list")
    for index, item in enumerate(payload["observations"]):
        _require_keys(item, ("metric", "direction", "note"))
        if item["direction"] not in ("inflow", "outflow", "flat", "unknown"):
            raise TaskError("schema_validation_failed", f"observations[{index}].direction invalid")
    return payload


def validate_signal_review(payload: dict) -> dict:
    """A deterministic signal, reviewed. Verdict may only agree or object."""
    _require_keys(payload, ("verdict", "reasons"))
    if payload["verdict"] not in ("agree", "object", "insufficient_evidence"):
        raise TaskError("schema_validation_failed", "verdict invalid")
    if not isinstance(payload["reasons"], list) or not payload["reasons"]:
        raise TaskError("schema_validation_failed", "reasons must be a non-empty list")
    return payload


def validate_pnl_review(payload: dict) -> dict:
    """P&L → attribution. Must separate skill from luck from cost."""
    _require_keys(payload, ("attribution", "notes"))
    attribution = payload["attribution"]
    if not isinstance(attribution, list) or not attribution:
        raise TaskError("schema_validation_failed", "attribution must be non-empty")
    total = 0.0
    for index, item in enumerate(attribution):
        _require_keys(item, ("source", "bps", "evidence"))
        _check_number(item["bps"], f"attribution[{index}].bps")
        total += float(item["bps"])
        if not str(item["evidence"]).strip():
            raise TaskError("schema_validation_failed", f"attribution[{index}].evidence empty")
    # Attribution that does not sum to the observed result is a story, not an
    # explanation. Allow rounding slack of 5 bps, nothing more.
    reported = payload.get("total_bps")
    if reported is not None:
        _check_number(reported, "total_bps")
        if abs(total - float(reported)) > 5:
            raise TaskError(
                "schema_validation_failed",
                f"attribution sums to {total:.0f} bps but total is {reported}",
            )
    return payload


def validate_evolution_proposal(payload: dict) -> dict:
    """A proposed change to parameters. Must be testable and bounded."""
    _require_keys(payload, ("proposals",))
    proposals = payload["proposals"]
    if not isinstance(proposals, list) or not proposals:
        raise TaskError("schema_validation_failed", "proposals must be non-empty")
    for index, item in enumerate(proposals):
        _require_keys(item, ("parameter", "current", "proposed", "hypothesis", "test_plan"))
        if item["parameter"] not in ALLOWED_PARAMETERS:
            # A model may not invent a new knob to turn.
            raise TaskError("schema_validation_failed", f"proposals[{index}].parameter not allowed")
        for key in ("current", "proposed"):
            _check_number(item[key], f"proposals[{index}].{key}")
        if item["current"] == item["proposed"]:
            raise TaskError("schema_validation_failed", f"proposals[{index}] changes nothing")
        if not str(item["test_plan"]).strip():
            raise TaskError("schema_validation_failed", f"proposals[{index}].test_plan empty")
    return payload


# Parameters a proposal is allowed to touch. Anything else is out of scope:
# an unconstrained knob is how a self-evolution loop wanders into nonsense.
ALLOWED_PARAMETERS = {
    "entry_score",
    "choppy_entry_score",
    "stop_loss_bps",
    "target_weight_bps",
    "min_cash_bps",
    "max_positions",
    "min_entry_cents",
}

# ---------------------------------------------------------------------------
# Job definitions
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class Task:
    name: str
    title: str
    system_prompt: str
    user_template: str
    validator: Callable[[dict], dict]
    required_inputs: tuple[str, ...]
    forbidden: tuple[str, ...]
    temperature: float = 0.2
    max_output_tokens: int = 4000
    notes: str = ""
    # Optional: the input fields this task needs but the project may not be
    # able to supply. Naming them lets a caller report precisely what is
    # missing instead of a vague "no data".
    required_fields: tuple[str, ...] = ()
    unavailable_reason: str = ""


NO_FABRICATION = (
    "严禁编造输入中没有的数字、日期、机构名或新闻。"
    "输入里没有的字段，就在输出里写 null 或 \"unknown\"，不要推测。"
)

NO_TRADE = (
    "你只能描述、评估和解释，不能给出可以直接执行的买卖指令，"
    "也不能建议绕过任何风控规则。"
)


TASKS: dict[str, Task] = {
    "news_impact": Task(
        name="news_impact",
        title="新闻/公告影响评估",
        system_prompt=(
            "你是 A 股研究员，负责判断一条新闻或公告对该标的的潜在影响。"
            "你的输出会被人审阅，不是自动执行。\n"
            f"{NO_FABRICATION}\n{NO_TRADE}\n"
            "判断标准：影响力度看是否能改变该公司的基本面预期；"
            "情绪看文字本身，不看你对该公司的整体看法。"
            "信息不足以判断时，sentiment 用 unclear、impact 用 unknown，并说明缺什么。"
        ),
        user_template=(
            "标的：{symbol}（{name}）\n"
            "公告/新闻材料（JSON）：\n{materials}\n\n"
            "对每一条材料给出：\n"
            '{{"items":[{{"ref":"材料编号","sentiment":"positive|negative|neutral|unclear",'
            '"impact":"high|medium|low|unknown","reason":"引用材料中的具体依据，一句话"}}]}}\n'
            "只输出 JSON。"
        ),
        validator=validate_news_impact,
        required_inputs=("symbol", "name", "materials"),
        forbidden=("future_price", "target_price", "buy_sell_advice"),
        temperature=0.1,
        notes="材料来自巨潮公告或新闻抓取；没有材料时不运行。",
    ),

    "flow_reading": Task(
        name="flow_reading",
        title="资金流向解读",
        # Public providers expose size-bucketed net-flow statistics. Keep their
        # derived/vendor definition distinct from raw Level-2 trade prints;
        # daily OHLCV and turnover alone never satisfy these required fields.
        required_fields=(
            "super_large_net_inflow_cents",
            "large_net_inflow_cents",
            "medium_net_inflow_cents",
            "retail_net_inflow_cents",
        ),
        unavailable_reason=(
            "本次未取得带来源、日期和金额单位的分档净流入数据；"
            "日线成交量与成交额不能替代资金流。公开网站的分档统计可以使用，"
            "但须标明供应商计算口径，不能冒充交易所 Level-2 原始逐笔。"
        ),
        system_prompt=(
            "你是 A 股交易员，负责解读资金流向数据。\n"
            f"{NO_FABRICATION}\n{NO_TRADE}\n"
            "重要：你只能描述数据呈现什么，不能预测后续走势。"
            "如果输入不含资金流向数据，直接返回 data_unavailable，不要用成交量替代。"
            "网站提供的分档净流入按其来源口径解释，明确属于供应商统计，不能称为原始 Level-2 逐笔。"
        ),
        user_template=(
            "标的：{symbol}\n"
            "资金流向数据（JSON）：\n{flows}\n\n"
            "输出：\n"
            '{{"observations":[{{"metric":"指标名","direction":"inflow|outflow|flat|unknown",'
            '"note":"这个数字说明什么"}}],"summary":"两三句话，只描述已发生的事实"}}\n'
            "只输出 JSON。"
        ),
        validator=validate_flow_reading,
        required_inputs=("symbol", "flows"),
        forbidden=("prediction", "target_price"),
        temperature=0.1,
        notes=(
            "由公共数据层提供真实分档资金流；缺材料时跳过。"
            "保留来源、时间、单位和统计口径，不用成交量冒充净流入。"
        ),
    ),

    "signal_review": Task(
        name="signal_review",
        title="买卖信号复核",
        system_prompt=(
            "你是风控复核员。策略已经按确定性规则产生了信号，你的任务是找出其中"
            "可能被忽略的风险，而不是重新发明一个信号。\n"
            f"{NO_FABRICATION}\n{NO_TRADE}\n"
            "如果你认为证据不足，就返回 insufficient_evidence 并说明缺什么；"
            "不要为了显得有用而强行表态。"
        ),
        user_template=(
            "标的：{symbol}\n"
            "策略信号：{signal}\n"
            "信号依据（因子/评分/技术位）：\n{basis}\n"
            "当前持仓与市场状态：\n{context}\n\n"
            "输出：\n"
            '{{"verdict":"agree|object|insufficient_evidence",'
            '"reasons":["具体理由，引用输入中的哪一项"],'
            '"risks":["你可能担心的点，没有就空数组"]}}\n'
            "只输出 JSON。"
        ),
        validator=validate_signal_review,
        required_inputs=("symbol", "signal", "basis", "context"),
        forbidden=("override_signal", "new_order"),
        temperature=0.2,
        notes="复核结论只记录，不改变订单；执行仍由确定性规则决定。",
    ),

    "pnl_review": Task(
        name="pnl_review",
        title="盈亏归因复盘",
        system_prompt=(
            "你是投资组合分析师，负责把一段时间的盈亏拆解归因。\n"
            f"{NO_FABRICATION}\n"
            "严格要求：归因的各项 bps 之和必须等于输入的 total_bps（允许 5bps 以内舍入误差）。"
            "对不上就说明你的归因是编故事。\n"
            "必须区分：选股（排序是否选对）、仓位（大小是否合适）、"
            "时机（成交时点）、成本（手续费）、运气（无法归因的残差）。"
        ),
        user_template=(
            "区间：{period}\n"
            "总收益：{total_bps} bps\n"
            "成交与持仓数据（JSON）：\n{activity}\n"
            "分项统计：\n{breakdown}\n\n"
            "输出：\n"
            '{{"total_bps":{total_bps},"attribution":[{{"source":"选股|仓位|时机|成本|运气",'
            '"bps":0,"evidence":"引用输入中的具体数据"}}],"notes":"两三句话总结"}}\n'
            "只输出 JSON。"
        ),
        validator=validate_pnl_review,
        required_inputs=("period", "total_bps", "activity"),
        forbidden=("future_prediction",),
        temperature=0.15,
        notes="归因和对不上账会被校验器拒绝，防止生成看起来合理但算不平的解释。",
    ),

    "evolution_proposal": Task(
        name="evolution_proposal",
        title="参数进化提案",
        system_prompt=(
            "你是策略研究员，负责根据回测与实盘证据提出参数调整建议。\n"
            f"{NO_FABRICATION}\n{NO_TRADE}\n"
            "硬性要求：\n"
            "1. 只能调整白名单内的参数，不得发明新参数；\n"
            "2. 每条提案必须写明当前值、建议值、假设，以及如何验证（test_plan）；\n"
            "3. 不得以「近期表现不好」为由要求放宽风控；\n"
            "4. 建议值必须能说明为什么就是这个数，而不是大概方向。"
        ),
        user_template=(
            "当前参数：\n{parameters}\n"
            "近期表现数据：\n{performance}\n"
            "已知的样本外状态：{out_of_sample_status}\n\n"
            "输出：\n"
            '{{"proposals":[{{"parameter":"参数名","current":0,"proposed":0,'
            '"hypothesis":"为什么这样改","test_plan":"如何验证，必须包含样本外"}}]}}\n'
            "只输出 JSON。"
        ),
        validator=validate_evolution_proposal,
        required_inputs=("parameters", "performance"),
        forbidden=("new_parameter", "unbounded_change", "skip_validation"),
        temperature=0.3,
        notes="提案进 P4 治理流程，不会自动生效；参数白名单在 ALLOWED_PARAMETERS 里。",
    ),

    "factor_hypothesis": Task(
        name="factor_hypothesis",
        title="因子假设生成",
        system_prompt=(
            "你是量化研究员。只使用给定的表达式语法提出因子，不得发明算子。\n"
            f"{NO_FABRICATION}"
            f"{NO_TRADE}"
        ),
        user_template=(
            "研究方向：{theme}\n"
            "可用语法：\n{grammar}\n\n"
            '输出 {{"hypotheses":[{{"expr":{{...}},"rationale":"一句话"}}]}}\n'
            "只输出 JSON。"
        ),
        validator=lambda payload: payload,
        required_inputs=("theme",),
        forbidden=("new_operator",),
        temperature=0.4,
        notes="语法校验在 llm_hypothesis.py，本任务只负责提出。",
    ),
}


def get(name: str) -> Task:
    task = TASKS.get(name)
    if task is None:
        raise TaskError("unknown_task", name)
    return task


def missing_inputs(task: Task, payload: dict) -> list[str]:
    """Which declared inputs the caller did not supply.

    A job that needs flow data cannot run on volume as a stand-in; calling that
    out is the difference between a gap and a lie.
    """
    return [key for key in task.required_inputs if payload.get(key) in (None, "", [], {})]


def render(task: Task, payload: dict) -> tuple[str, str]:
    missing = missing_inputs(task, payload)
    if missing:
        raise TaskError("data_unavailable", ",".join(missing))
    values = {}
    for key, value in payload.items():
        values[key] = value if isinstance(value, str) else json.dumps(value, ensure_ascii=False, indent=2)
    try:
        return task.system_prompt, task.user_template.format(**values)
    except KeyError as error:
        raise TaskError("data_unavailable", f"template slot {error}") from error

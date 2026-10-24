# -*- coding: utf-8 -*-
"""业绩公告事件（业绩预告 / 业绩快报）的**事实核验核心**。

本模块只回答一个问题：

    某个抽取器（AI 或任何上游）声称"这份公告里的归母净利润是 X~Y 万元、
    同比 A%~B%"，这句话是否**忠实于公告原文**？

它不抓取、不落库、不绑定研究链路，也不持有任何"这只股票会不会涨"的判断。

──────────────────────── 为什么需要一层独立核验 ────────────────────────

`news_learning` 的 owner 核验闭集只有三态且**没有 verified**（见该模块
R27-B2C-5 一节）：它只回答"这条事实能不能追溯到一条来源"。可追溯 ≠ 内容已核验 ——
有链接的公告也可能被抽错数字。因此"业绩公告 → 支持型研究证据"这条资格链在今天
是断的（`strategy_ai_candidate_service` 要求 `supported` 研究）。

补这一层的正确做法**不是**给 news owner 加一个 `verified` 常量，而是先让
"核验"这件事有一份真的能失败、能复算的过程。本模块就是那个过程。

──────────────────── 核验为什么不是同义反复（关键） ────────────────────

如果"事实"和"核验"都由本模块的同一个解析器产出，核验就只是把它自己跑了两遍 ——
那种 verified 是伪造 provenance。所以本模块的输入刻意分成两半：

```text
claim      调用方（AI 抽取器）给出的断言 —— EarningsClaim，**由外部构造**
body       公告原文（owner 归档的原始文本）
meta       公告身份（标题分类：kind / 报告期 / 是否修正 / 是否英文镜像）
```

核验 = 用**确定性解析器**从 `body` 复算出数字，再逐项断言与 `claim` 相等。
两边来自不同路径，所以"AI 把 -22,800 看成 22,800""把增加额当成净利润额"
这两类真实错误会**真的**被判成 `claim_figure_mismatch`，而不是被同一条解析链
自动抹平。

──────────────────────── 三条 fail-closed 不变量 ────────────────────────

1. **拿不到原文原文就没有核验**：`body` 为空 → `body_unavailable`，绝不因为
   "标题里有业绩预告"就认为数字已核验。
2. **解析不出就不算核验过**：公告版式不在本模块声明的形状族里 → `body_unparsable`。
   覆盖不足的代价是"这条候选不可用"，不是"猜一个数字"。
3. **被修正公告取代的原文不再核验通过**：`superseded` 的判定**先于**数字比较 ——
   一份已被更正的数据即使自身自洽，也已经是过期事实。

──────────────────────── 本轮**没有**做的部分 ────────────────────────

```text
durable ledger / 首次获取时间（first_seen_at）       NOT IN THIS MODULE
typed fact projection + 不可构造的签发路径            NOT IN THIS MODULE
research adapter（evidence_ref_from_*）与登记         NOT IN THIS MODULE
持续编排（研究→候选→验证→筛选）                      NOT IN THIS MODULE
```

因此本模块**不产生**任何可以进研究链路的证据；它产出的 `ClaimVerification`
只有"这份 claim 相对于这份原文的结论"。PIT、身份、去重、修正链的持久化是下一层
的事，把它们塞进这里会让"核验"与"可用性"两种语义混成一个字段。
"""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from typing import Any

__all__ = (
    "EARNINGS_FACT_CONTRACT_VERSION",
    "ANNOUNCEMENT_KIND_FORECAST",
    "ANNOUNCEMENT_KIND_EXPRESS",
    "ANNOUNCEMENT_KINDS",
    "METRIC_NET_PROFIT_ATTRIBUTABLE",
    "SUPPORTED_METRICS",
    "VERIFICATION_STATUSES",
    "STATUS_VERIFIED",
    "STATUS_CLAIM_KIND_MISMATCH",
    "STATUS_CLAIM_PERIOD_MISMATCH",
    "STATUS_CLAIM_METRIC_UNSUPPORTED",
    "STATUS_CLAIM_FIGURE_MISMATCH",
    "STATUS_SUPERSEDED",
    "STATUS_BODY_UNAVAILABLE",
    "STATUS_BODY_UNPARSABLE",
    "STATUS_ANNOUNCEMENT_UNSUPPORTED",
    "UNSUPPORTED_NOT_EARNINGS",
    "UNSUPPORTED_ENGLISH_MIRROR",
    "UNSUPPORTED_MISSING_PERIOD",
    "UNSUPPORTED_THIRD_PARTY_SUBJECT",
    "EarningsFactError",
    "AnnouncementMeta",
    "EarningsClaim",
    "ReportedFigures",
    "ClaimVerification",
    "classify_announcement",
    "parse_reported_figures",
    "verify_earnings_claim",
)

#: 本模块声明的事实契约版本。``ReportedFigures`` / ``ClaimVerification`` 都带它，
#: 将来接 typed projection 时版本漂移在构造期就暴露，而不是在下游审计时才发现。
EARNINGS_FACT_CONTRACT_VERSION = "earnings-disclosure-fact-v1"

# ---------------------------------------------------------------------------
# 词汇：公告类型 / 指标 / 核验结论
# ---------------------------------------------------------------------------

#: 业绩预告：区间预计，通常早于定期报告数周至数月披露。
ANNOUNCEMENT_KIND_FORECAST = "forecast"
#: 业绩快报：初步核算的确定值（部分公司经内部审计，未经会计师事务所审计）。
ANNOUNCEMENT_KIND_EXPRESS = "express"

ANNOUNCEMENT_KINDS = (ANNOUNCEMENT_KIND_FORECAST, ANNOUNCEMENT_KIND_EXPRESS)

#: 本轮只核验**归属于上市公司股东的净利润**（含"归属于母公司所有者""归属于本行"
#: 等等同措辞）。刻意只开一个指标：扣非净利润的措辞族更长（"扣除非经常性损益后的
#: 归属于上市公司股东的净利润"），且它在公告正文里经常跨行折断 —— 把它和归母
#: 混在一张词表里，最容易出现的错不是"解析失败"而是"静默串行取到另一个指标"。
#: 需要扣非时应当**新增**一个指标常量与一条独立解析分支，而不是放宽本表。
METRIC_NET_PROFIT_ATTRIBUTABLE = "net_profit_attributable"

SUPPORTED_METRICS = (METRIC_NET_PROFIT_ATTRIBUTABLE,)

#: 核验结论闭集。**每一种都对应一个真的能到达的分支**，没有占位状态。
STATUS_VERIFIED = "verified"
#: claim 声称的公告类型与标题分类不符（把快报当预告 / 把预告当快报）。
STATUS_CLAIM_KIND_MISMATCH = "claim_kind_mismatch"
#: claim 声称的报告期与标题（或正文）不符 —— 跨期错配会让"同比"落到错误基数上。
STATUS_CLAIM_PERIOD_MISMATCH = "claim_period_mismatch"
#: claim 用了本轮不核验的指标（例如扣非净利润）。
STATUS_CLAIM_METRIC_UNSUPPORTED = "claim_metric_unsupported"
#: 原文复算出的数字与 claim 不一致 —— 这是核验**真正**的业务失败态。
STATUS_CLAIM_FIGURE_MISMATCH = "claim_figure_mismatch"
#: 原文已被同一 (代码, 报告期, 公告类型) 的修正/更正公告取代。
STATUS_SUPERSEDED = "superseded"
#: 没有原文 —— 无从核验（**不**等于"核验不通过"）。
STATUS_BODY_UNAVAILABLE = "body_unavailable"
#: 有原文，但版式不在本模块声明的形状族里。
STATUS_BODY_UNPARSABLE = "body_unparsable"
#: 标题分类就没有成功（不是业绩公告 / 英文镜像 / 缺报告期）。
STATUS_ANNOUNCEMENT_UNSUPPORTED = "announcement_unsupported"

VERIFICATION_STATUSES = (
    STATUS_VERIFIED,
    STATUS_CLAIM_KIND_MISMATCH,
    STATUS_CLAIM_PERIOD_MISMATCH,
    STATUS_CLAIM_METRIC_UNSUPPORTED,
    STATUS_CLAIM_FIGURE_MISMATCH,
    STATUS_SUPERSEDED,
    STATUS_BODY_UNAVAILABLE,
    STATUS_BODY_UNPARSABLE,
    STATUS_ANNOUNCEMENT_UNSUPPORTED,
)

#: 标题无法归入业绩公告族的三种原因。刻意是稳定 machine code：入库/审计/回归都靠它，
#: 中文档名会随措辞修改而漂移。
UNSUPPORTED_NOT_EARNINGS = "not_earnings_announcement"
UNSUPPORTED_ENGLISH_MIRROR = "english_mirror"
UNSUPPORTED_MISSING_PERIOD = "missing_report_period"
#: 披露对象不是本公司（子公司 / 参股公司），见 :data:`_THIRD_PARTY_SUBJECT_RE`。
UNSUPPORTED_THIRD_PARTY_SUBJECT = "third_party_subject"


class EarningsFactError(ValueError):
    """原文复算被拒绝 —— fail closed。

    ``reason`` 是稳定 machine code：

    * ``body_unavailable`` —— 原文为空；
    * ``unknown_kind`` —— 公告类型不在闭集里；
    * ``unparsable_forecast`` / ``unparsable_express`` —— 版式不在形状族里；
    * ``unknown_unit`` —— 金额单位无法识别（**不**假设"元"或"万元"）；
    * ``unparsable_amount`` —— 数字无法转成 Decimal；
    * ``inverted_range`` —— 复算出的区间上下界颠倒（说明取错了字段）。
    """

    def __init__(self, reason: str, detail: str = "") -> None:
        self.reason = str(reason)
        super().__init__(self.reason if not detail else f"{self.reason}: {detail}")


# ---------------------------------------------------------------------------
# 标题分类 —— 公告身份的唯一入口
# ---------------------------------------------------------------------------

_YEAR_RE = re.compile(r"(20\d{2})\s*年")
_QUARTER_TO_PERIOD = {
    "一": "03-31", "1": "03-31",
    "二": "06-30", "2": "06-30",
    "三": "09-30", "3": "09-30",
    "四": "12-31", "4": "12-31",
}
_QUARTER_RE = re.compile(r"第?\s*([一二三四1-4])\s*季度")
#: 修正/更正/修订 —— 都表示"同一 (代码, 报告期, 公告类型) 的既有事实被改写"。
_CORRECTION_RE = re.compile(r"(?:修正|更正|修订)")
#: 英文镜像公告与中文原文是**同一条事实**，不能各建一个事件（否则去重率与
#: 首次发现时间都会被稀释）。判定在这里，不在入库层，因为"这是不是镜像"是标题语义。
_ENGLISH_MIRROR_RE = re.compile(r"(?:英文版|英文版本|\(\s*english\s*\)|（\s*english\s*）)", re.IGNORECASE)
#: "关于<别的公司>业绩快报/预告的公告"—— 披露主体是子公司或参股公司，数字**不是**
#: 上市公司的。真实例子：中信证券披露控股子公司华夏基金的半年度业绩快报。
#: 这类公告如果当成本公司业绩事件，会直接污染候选池（用子公司利润给母公司估值）。
#:
#: 例外：紧接"关于"的是年份或"公司/本公司/本行"，说明披露对象就是自己。
_THIRD_PARTY_SUBJECT_RE = re.compile(
    r"关于(?!\s*(?:20\d{2}|公司|本公司|本行))(.{0,20}?)业绩(?:预告|快报)"
)


@dataclass(frozen=True, slots=True)
class AnnouncementMeta:
    """一条业绩公告的**身份**分类（不含任何数字）。

    字段刻意分离，因为它们的失败代价不同：

    * ``kind`` 错会让核验拿错形状族（预告区间 vs 快报定值）；
    * ``report_period`` 错会让同比落到错误基数上；
    * ``is_correction`` 错会让过期数据继续冒充当前事实；
    * ``english_mirror`` 错会让同一条事实被记成两条独立发现。

    ``unsupported_reason`` 非空时其余业务字段均为 ``None`` / ``False`` ——
    不发布半份身份。
    """

    kind: str | None
    report_period: str | None
    is_correction: bool
    english_mirror: bool
    unsupported_reason: str | None

    @property
    def supported(self) -> bool:
        return self.unsupported_reason is None


def _period_from_title(title: str) -> str | None:
    """从标题抽出报告期末日（``YYYY-MM-DD``）；抽不到就返回 ``None``。

    顺序不是随意的：``半年度`` 必须先于 ``年度``（"半年度"里含"年度"），
    ``前三季度`` 必须先于 ``第X季度``（它同时含"季度"）。这类先后关系是**语义**
    而不是风格，改动前请先看 ``test_earnings_disclosure_facts`` 的负例。
    """
    year_match = _YEAR_RE.search(title)
    if not year_match:
        return None
    year = year_match.group(1)
    if re.search(r"半年度|半年报|中期", title):
        return f"{year}-06-30"
    if re.search(r"前三季度|三季度", title):
        return f"{year}-09-30"
    quarter = _QUARTER_RE.search(title)
    if quarter:
        return f"{year}-{_QUARTER_TO_PERIOD[quarter.group(1)]}"
    if re.search(r"年度|年报|全年", title):
        return f"{year}-12-31"
    return None


def classify_announcement(title: Any) -> AnnouncementMeta:
    """把公告标题归入业绩公告族；归不进去就**明确说不**（带原因）。

    这是整条链路的身份入口：只有它说 supported，后面的解析与核验才有意义。
    它刻意不做"尽力而为"的猜测 —— 一个把"业绩说明会"认成"业绩预告"的分类器，
    会让核验去比对一份根本不含预告数字的原文。
    """
    text = str(title or "").strip()
    if not text:
        return AnnouncementMeta(None, None, False, False, UNSUPPORTED_NOT_EARNINGS)
    if "业绩预告" in text:
        kind: str | None = ANNOUNCEMENT_KIND_FORECAST
    elif "业绩快报" in text:
        kind = ANNOUNCEMENT_KIND_EXPRESS
    elif re.search(r"业绩预(?:增|减|亏|盈)", text):
        # 部分公司以"业绩预增/预亏公告"命名，实质仍是预告。区间数字仍由正文解析，
        # 报告期仍**必须**能从标题证明 —— 抽不到就 fail closed，不猜。
        kind = ANNOUNCEMENT_KIND_FORECAST
    else:
        return AnnouncementMeta(None, None, False, False, UNSUPPORTED_NOT_EARNINGS)

    is_correction = bool(_CORRECTION_RE.search(text))
    english_mirror = bool(_ENGLISH_MIRROR_RE.search(text))
    if english_mirror:
        # 英文镜像不建独立事件，也不做核验：原文与中文公告同源同事实。
        return AnnouncementMeta(kind, None, is_correction, True, UNSUPPORTED_ENGLISH_MIRROR)
    if _THIRD_PARTY_SUBJECT_RE.search(text):
        # 披露对象不是本公司（子公司/参股公司），数字不能当本公司的业绩事实。
        return AnnouncementMeta(kind, None, is_correction, False,
                               UNSUPPORTED_THIRD_PARTY_SUBJECT)
    report_period = _period_from_title(text)
    if report_period is None:
        return AnnouncementMeta(kind, None, is_correction, False, UNSUPPORTED_MISSING_PERIOD)
    return AnnouncementMeta(kind, report_period, is_correction, False, None)


# ---------------------------------------------------------------------------
# 正文复算 —— 核验的 authority
# ---------------------------------------------------------------------------
#
# 形状族刻意**只有两种**，且都被真实公告验证过（见 test 里的四份原文夹具）：
#
#   express    行式表格：``<指标标签>  本期值  上期值  增减幅度``，单位在表头声明
#   forecast   （a）表格式：``亏损：-22,800 万元～-18,000 万元``
#              （b）叙述式：``预计…净利润 202,165 万元到 230,732 万元``
#
# 不在族里的版式一律 ``unparsable``。这不是保守，而是**可证伪**：每加一种形状，
# 都必须同时加一份真实原文回归，否则"解析成功"可能只是匹配到了相邻的另一个指标。

#: 金额单位 → 折算到万元的系数。**匹配顺序即优先级**：``亿元`` 必须先于 ``元``，
#: ``百万元`` 必须先于 ``万元``，否则"百万元"会被"元"抢先匹配成 1:1。
_UNIT_FACTORS = (
    ("亿元", Decimal("10000")),
    ("百万元", Decimal("100")),
    ("千万元", Decimal("1000")),
    ("万元", Decimal("1")),
    ("千元", Decimal("0.1")),
    ("元", Decimal("0.0001")),
)

_UNIT_PATTERN = "|".join(unit for unit, _ in _UNIT_FACTORS)
#: 数字：千分位允许**半角与全角**逗号。真实公告里两种都出现
#: （"4,081.97 万元" 与 "4，081.97 万元" 同时存在），只认半角会让第一个锚点匹配失败，
#: 于是解析"跳过它继续找"——最终取到的是后面「上年同期业绩情况」那一段的数字。
_NUMBER_PATTERN = r"-?\d[\d,\uFF0C]*(?:\.\d+)?"

#: 归母净利润标签的**锚点**（只到"归属于X"为止），而不是完整标签。
#:
#: 为什么不能要求完整标签：公告表格经 PDF 转文本后，标签是被数值列**打断**的 ——
#:
#:     归属于上市公司 亏损：-22,800 万元～-18,000 万元      亏损：-23,892.72 万元
#:     股东的净利润   比上年同期增长：5%～25%
#:
#: 完整标签"归属于上市公司股东的净利润"在原文里根本不是一个连续串，压平后顺序变成
#: "…归属于上市公司 亏损：-22,800… 上年同期增长：5%～25% 扣除非经常性损…益后的净利润"。
#: 要求完整标签的结果是**所有表格式预告都解析失败**，而不是"更安全"。
#:
#: 因此改为：锚点 + 跳过标签续词 + **后面必须紧跟数值**。这个组合仍然拒绝错对象 ——
#: 招商银行快报里有更早出现的"归属于本行普通股股东的基本每股收益"，锚点后面是
#: "普通股股东的基本…"，"基本"不在续词表里，数值形状匹配不上，于是被跳过。
_NET_PROFIT_ANCHOR_RE = re.compile(r"归属于(?:上市公司|母公司|本公司|本行|公司)")
#: 标签续词（锚点到数值之间允许出现的成分）。刻意是**闭集**：续词之外出现任何字符，
#: 就说明这个锚点指向的是另一个指标（每股收益 / 净资产收益率 / 扣非）。
#:
#: 单字 ``净`` / ``润`` / ``股`` 必须在表里：公告表格按列宽折断后会出现
#: ``归属于上市公司股东的净`` / ``利润  310,648.47`` 或
#: ``归属于上市公司股`` / ``东的净利润  亏损…`` 这种"半个词在下一行"的版式，
#: 不认单字就只能把整份公告判成 unparsable。多字词排前面，避免被单字抢先吃掉。
_LABEL_CONTINUATION_RE = re.compile(
    r"^(?:净利润|净利|利润|普通股|所有者|股东|的|净|润|股)*"
)
#: 数值的合法起始（可带"亏损："/"盈利："/"约/为/达到"前缀）。快报与预告共用。
_LEVEL_START_RE = re.compile(rf"^(?:约|为|达到)?(?:亏损|盈利)?[：:]?\s*{_NUMBER_PATTERN}")
#: 数字**自带单位**的形状（"净利润 31.06 亿元"）。快报表格里单元格是裸数字，
#: 单位由表头声明；凡是自带单位的都说明这是叙述句，不是表格行 —— 拿它乘表头单位
#: 会把 31.06 亿元算成 31.06 万元（真实发生过，见
#: ``test_narrative_amount_is_not_taken_as_a_table_cell``）。
_EXPRESS_CELL_UNIT_RE = re.compile(rf"^{_NUMBER_PATTERN}\s*(?:{_UNIT_PATTERN})")
#: 排除窗口：扣非标签的"净利润"前面挂着"扣除非经常性损益后的"，若不排除，
#: ``扣除非经常性损益后的归属于上市公司股东的净利润`` 会被当成归母净利润命中。
_EXCLUSION_WINDOW = 12

#: 区间分隔符。**刻意不含裸 ``-``**："-85%～-65%" 这类负区间里"-"同时是负号，
#: 把它当分隔符会让区间切分依赖偶然的字符位置。
_RANGE_SEPARATOR_PATTERN = r"(?:～|~|到|至|—|–|－)"

#: 夹在"数字或单位"与"数字"之间的破折号一律归一成 ``～``。
#:
#: 真实公告的区间分隔符远远不止一种：``～`` / ``到`` / ``至`` / ``-`` / ``–``（en dash）
#: / ``—``。漏掉 en dash 的后果不是"解析失败"，而是**区间塌成单点值**：
#: 五粮液的"盈利：873,000 万元–920,000 万元"会被读成"873,000 万元"，
#: 于是核验会给一个只有下界的 claim 背书。
#:
#: 归一化只在三种位置生效（数字后、``元`` 后、``亿`` 后），因此不会碰到真正的负号 ——
#: 负号前面是冒号、括号或另一个分隔符。
_DASH_BETWEEN_AMOUNTS_RE = re.compile(
    r"(?:(?<=[\d%])|(?<=元)|(?<=亿))\s*[-–—－]\s*(?=[-－]?\d)"
)

_FORECAST_RANGE_RE = re.compile(
    rf"^(?:约|为|达到)?(?:亏损|盈利)?[：:]?\s*"
    rf"(?P<low>{_NUMBER_PATTERN})\s*(?P<low_unit>{_UNIT_PATTERN})"
    rf"(?:\s*{_RANGE_SEPARATOR_PATTERN}\s*"
    rf"(?P<high>{_NUMBER_PATTERN})\s*(?P<high_unit>{_UNIT_PATTERN})?)?"
)
#: 单点值后面还跟着数字 —— 说明这其实是一个**没能切开的区间**，而不是定值。
#:
#: 判据刻意收窄成"到下一个数字之间没有任何中文"：中文一出现就意味着已经进入描述性
#: 文字（"，与上年同期相比…"），那才是真正的定值表述。这样既能拦住 en dash / ``※``
#: 这类未登记的间隔符，也不会把"256,900 万元，与上年同期相比增长…"误判成区间。
def _looks_like_a_collapsed_range(tail: str) -> bool:
    next_digit = re.search(r"\d", tail)
    if next_digit is None:
        return False
    return not re.search(r"[\u4e00-\u9fff]", tail[:next_digit.start()])


#: 锚点之后**紧跟一个数**（可带"约/为/达到/亏损："前缀）。用于把"形状没认出来"
#: 与"这里根本不是数值句"分开：前者必须 fail closed，后者才允许继续往后找。
_ANCHOR_FOLLOWED_BY_NUMBER_RE = re.compile(
    rf"^(?:约|为|达到)?(?:亏损|盈利)?[：:]?\s*{_NUMBER_PATTERN}"
)
_YOY_RE = re.compile(
    rf"(?:比上年同期|同比|较上年同期)(?P<direction>增长|下降|减少|增加)?[：:]?\s*"
    rf"(?P<low>-?\d+(?:\.\d+)?)\s*%"
    rf"(?:\s*{_RANGE_SEPARATOR_PATTERN}\s*(?P<high>-?\d+(?:\.\d+)?)\s*%)?"
)
#: 声明"负向"的措辞。公告写"同比下降 45%到 55%"时，公告正文里的 45/55 是**降幅**，
#: 真实的同比区间是 ``[-55%, -45%]``。按字面取值会把方向搞反 —— 那会让一个声称
#: "同比 +45%" 的错误 claim 通过核验。
_YOY_NEGATIVE_DIRECTIONS = ("下降", "减少")
_EXPRESS_UNIT_RE = re.compile(
    rf"单位\s*[：:]?\s*(?:人民币)?\s*(?P<unit>{_UNIT_PATTERN})"
)
_PERCENT_RE = re.compile(r"(?P<pct>-?\d+(?:\.\d+)?)\s*%")
_PERIOD_RANGE_RE = re.compile(
    r"(20\d{2})\s*年\s*(\d{1,2})\s*月\s*(\d{1,2})\s*日\s*至\s*"
    r"(20\d{2})\s*年\s*(\d{1,2})\s*月\s*(\d{1,2})\s*日"
)
_PERIOD_WORD_RE = re.compile(
    r"(20\d{2})\s*年\s*(半年度|年度|中期|度|第\s*[一二三四1-4]\s*季度)"
)
_QUARTER_MONTH_DAY = {"一": (3, 31), "1": (3, 31), "二": (6, 30), "2": (6, 30),
                      "三": (9, 30), "3": (9, 30), "四": (12, 31), "4": (12, 31)}


def _unit_factor(unit: str) -> Decimal:
    for name, factor in _UNIT_FACTORS:
        if unit == name:
            return factor
    raise EarningsFactError("unknown_unit", repr(unit))


def _to_decimal(text: str) -> Decimal:
    try:
        return Decimal(str(text).replace(",", "").replace("\uFF0C", ""))
    except (InvalidOperation, ValueError) as exc:
        raise EarningsFactError("unparsable_amount", repr(text)) from exc


def _yoy_interval(match: Any) -> tuple[Decimal, Decimal]:
    """同比 → **带符号且升序**的区间 ``(low, high)``。

    "同比下降 45%到 55%" 的降幅要变成 ``(-55, -45)``：不取符号会让方向反掉，
    而且降幅区间的书写顺序天生是"先小后大"，取符号后必须重新排序才是区间。

    正文里已经写成负数的（"下降：-85%～-65%"）不再二次取负 —— 那类写法本身
    已经把符号写进去了，再乘一次就变成正增长。
    """
    direction = match.group("direction") or ""
    sign = Decimal("-1") if direction in _YOY_NEGATIVE_DIRECTIONS else Decimal("1")
    values: list[Decimal] = []
    for group in ("low", "high"):
        text = match.group(group)
        if text is None:
            continue
        number = _to_decimal(text)
        values.append(number * sign if number > 0 else number)
    if len(values) == 1:
        values.append(values[0])
    return min(values), max(values)


def _wan(amount: str, unit: str) -> Decimal:
    return _to_decimal(amount) * _unit_factor(unit)


def _period_from_body(body: str) -> str | None:
    """从正文复算报告期末日；复算不出返回 ``None``（**不**从标题借）。

    显式日期区间优先：``2026 年 1 月 1 日至 2026 年 6 月 30 日`` 里的结束日就是
    报告期末日，比"半年度"这类词更接近原文事实。
    """
    match = _PERIOD_RANGE_RE.search(body)
    if match:
        year, month, day = match.group(4), int(match.group(5)), int(match.group(6))
        try:
            return f"{year}-{month:02d}-{day:02d}"
        except (TypeError, ValueError):
            return None
    word = _PERIOD_WORD_RE.search(body)
    if not word:
        return None
    year, label = word.group(1), word.group(2)
    if "半年度" in label or "中期" in label:
        return f"{year}-06-30"
    if label == "度" or "年度" in label:
        # "2025 年度" 里的"年"已被上文消费掉，剩下的"度"就是年度口径；
        # 若要求"年度"整词，所有"XXXX 年度"的正文都会解析失败。
        return f"{year}-12-31"
    quarter = _QUARTER_RE.search(label)
    if quarter:
        month, day = _QUARTER_MONTH_DAY[quarter.group(1)]
        return f"{year}-{month:02d}-{day:02d}"
    return None


def _squash(text: str) -> str:
    """把换行/空格全部压掉，让被列宽折断的标签重新连成一片。

    公告表格在 PDF 转文本后普遍是"归属于上市公司\\n股东的净利润"，
    不压平就永远匹配不到标签；压平后仍然要求标签**后面紧跟数字**，
    所以不会把不相邻的数字粘成一次核验。
    """
    return re.sub(r"\s+", "", text)


def _net_profit_label_tails(text: str):
    """逐个产出「归母净利润锚点 → 跳过标签续词之后的剩余文本」。

    扣非排除在这里做（而不是在调用方），因为它对两种形状族是**同一条**语义：
    锚点前面挂着``扣除``就说明这一行说的是扣非净利润。
    """
    for anchor in _NET_PROFIT_ANCHOR_RE.finditer(text):
        if "扣除" in text[max(0, anchor.start() - _EXCLUSION_WINDOW):anchor.start()]:
            continue
        yield _LABEL_CONTINUATION_RE.sub("", text[anchor.end():])


def _labels_at_line_start(line: str):
    """产出**行首**锚点之后的剩余文本（快报专用；表格行必须顶格）。"""
    stripped = line.strip()
    for anchor in _NET_PROFIT_ANCHOR_RE.finditer(stripped):
        if anchor.start() != 0:
            continue
        if "扣除" in stripped[max(0, anchor.start() - _EXCLUSION_WINDOW):anchor.start()]:
            continue
        yield stripped[anchor.end():]


def _continuation_line(lines: list[str], index: int) -> str | None:
    """取"标签被折断到下一行"时的续行文本；续行不以续词开头就不认。

    允许跳过空行（PDF 转文本会在折断处插空行），但**最多两行**：再远就不可能是
    同一行折断了，继续找只会把无关数字接进来。
    """
    for offset in (1, 2):
        position = index + offset
        if position >= len(lines):
            return None
        text = lines[position].strip()
        if not text:
            continue
        tail = _LABEL_CONTINUATION_RE.sub("", text)
        return None if tail == text else tail
    return None


def _parse_forecast(body: str) -> tuple[Decimal, Decimal, Decimal | None, Decimal | None, str]:
    """业绩预告：区间预计。返回 ``(low_wan, high_wan, yoy_low, yoy_high, unit)``。"""
    squashed = _DASH_BETWEEN_AMOUNTS_RE.sub("～", _squash(body))
    for rest in _net_profit_label_tails(squashed):
        window = rest[:160]
        range_match = _FORECAST_RANGE_RE.match(window)
        if not range_match:
            if _ANCHOR_FOLLOWED_BY_NUMBER_RE.match(window):
                # 锚点后面**确实是一个数**，只是整体形状没被认出（少见的单位写法、
                # 未登记的措辞…）。此时若继续往后找，下一个"归母净利润"多半已经落在
                # 「上年同期业绩情况」那一段 —— 于是把上一个报告期的数字当本期。
                # 真实案例：区间里的千分位是全角逗号（4，081.97 万元）时，第一个锚点
                # 匹配失败，解析器悄悄取到了后面的 9,071.05 万元（上年同期值）。
                raise EarningsFactError(
                    "unparsable_forecast",
                    f"锚点后出现数字但形状未识别：{window[:40]!r}",
                )
            # "与上年同期相比，将增加 X 万元到 Y 万元"在语法上紧跟标签，但那说的是
            # **增加额**而不是净利润额。它匹配不上上面的"值在首位"形状，因此被跳过。
            continue
        low_unit = range_match.group("low_unit")
        low = _wan(range_match.group("low"), low_unit)
        high_group = range_match.group("high")
        if high_group is None:
            if _looks_like_a_collapsed_range(window[range_match.end():]):
                # 单位后面还有数字（中间不出中文词）：这是一个没能切开的区间，
                # 不是定值。塌成单点值会让核验给半个区间背书。
                raise EarningsFactError(
                    "unparsable_forecast",
                    f"疑似区间但未能切分：{window[:40]!r}",
                )
            high = low
        else:
            high = _wan(high_group, range_match.group("high_unit") or low_unit)
        if high < low:
            raise EarningsFactError(
                "inverted_range", f"low={low} > high={high}（疑似取到了相邻指标）",
            )
        yoy = _YOY_RE.search(window)
        yoy_low = yoy_high = None
        if yoy:
            yoy_low, yoy_high = _yoy_interval(yoy)
        return low, high, yoy_low, yoy_high, low_unit
    raise EarningsFactError(
        "unparsable_forecast", "正文中找不到『归母净利润 + 区间值』的形状",
    )


def _parse_express(body: str) -> tuple[Decimal, Decimal, Decimal | None, Decimal | None, str]:
    """业绩快报：行式表格定值。返回 ``(value, value, yoy, yoy, unit)``。

    快报的全部风险都在"**哪一行才是归母净利润行**"上，数字转换本身很简单。
    三条限定合起来才够：

    1. 锚点必须在**行首** —— 表格行的标签永远顶格，而"报告期内，公司实现归属于
       上市公司股东的净利润 31.06 亿元"这类叙述句里锚点在句子中间；
    2. 数字**不得自带单位** —— 表格单元格是裸数字，单位在表头声明；
    3. 标签**允许跨行折断** —— ``归属于上市公司股东的净`` / ``利润  310,648.47``
       是真实公告的常见版式，但它只能由"本行标签后没有任何数字"触发，且续行必须
       以续词开头，否则不认。

    少任何一条，就会出现"解析成功但数字是别的指标/别的单位"这种最坏的失败 ——
    它不会报错，只会让核验去给一个错数字背书。
    """
    unit_match = _EXPRESS_UNIT_RE.search(body)
    if not unit_match:
        raise EarningsFactError("unknown_unit", "正文没有声明金额单位")
    unit = unit_match.group("unit")
    factor = _unit_factor(unit)
    lines = body.splitlines()
    for index, line in enumerate(lines):
        for tail in _labels_at_line_start(line):
            rest = _LABEL_CONTINUATION_RE.sub("", tail)
            if not _LEVEL_START_RE.match(rest.lstrip()):
                if re.search(r"\d", rest):
                    # 本行标签后已有数字，只是形状不对 —— 说明取错了指标，不是跨行。
                    continue
                rest = _continuation_line(lines, index)
                if rest is None or not _LEVEL_START_RE.match(rest.lstrip()):
                    continue
            if _EXPRESS_CELL_UNIT_RE.match(rest.lstrip()):
                continue
            amounts = re.findall(_NUMBER_PATTERN, rest)
            if not amounts:
                continue
            value = _to_decimal(amounts[0]) * factor
            percent = _PERCENT_RE.search(rest)
            yoy = _to_decimal(percent.group("pct")) if percent else None
            return value, value, yoy, yoy, unit
    raise EarningsFactError(
        "unparsable_express", "表格里找不到『归母净利润』行或其数字",
    )


@dataclass(frozen=True, slots=True, init=False)
class ReportedFigures:
    """从**公告原文**复算出来的数字 —— 核验的权威侧。

    **没有公开构造器。** 唯一签发路径是 :func:`parse_reported_figures`：否则任何
    调用方都能自述 ``low_wan`` / ``body_sha256`` 然后声称"这是原文复算的结果"，
    核验就会退化成"比对我自己写的两个数"。

    ``body_sha256`` 把结论绑到**具体的原文**上：同一份数字配另一份原文，
    指纹不同 —— 这是"可回查原文"能被机械检查的最小形式。
    """

    version: str
    kind: str
    metric: str
    report_period: str | None
    low_wan: Decimal
    high_wan: Decimal
    yoy_low_pct: Decimal | None
    yoy_high_pct: Decimal | None
    unit: str
    body_sha256: str

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        raise TypeError(
            "ReportedFigures has no public constructor: 原文复算结果不能由调用方自述。"
            "请使用 earnings_disclosure_facts.parse_reported_figures(...)"
        )

    def __post_init__(self) -> None:
        if self.version != EARNINGS_FACT_CONTRACT_VERSION:
            raise ValueError(
                f"ReportedFigures version {self.version!r} != {EARNINGS_FACT_CONTRACT_VERSION!r}"
            )
        if self.kind not in ANNOUNCEMENT_KINDS:
            raise EarningsFactError("unknown_kind", repr(self.kind))
        if self.metric not in SUPPORTED_METRICS:
            raise EarningsFactError("claim_metric_unsupported", repr(self.metric))
        if self.high_wan < self.low_wan:
            raise EarningsFactError("inverted_range", f"{self.low_wan} > {self.high_wan}")
        for name in ("body_sha256", "unit"):
            if not str(getattr(self, name) or "").strip():
                raise ValueError(f"ReportedFigures.{name} must be non-empty")


def _issue_reported_figures(**fields: Any) -> ReportedFigures:
    """签发一次原文复算结果。只有本模块的解析函数调用它。"""
    figures = object.__new__(ReportedFigures)
    for name, value in fields.items():
        object.__setattr__(figures, name, value)
    figures.__post_init__()
    return figures


def parse_reported_figures(kind: Any, body: Any) -> ReportedFigures:
    """从公告原文复算归母净利润。

    ``kind`` 由 :func:`classify_announcement` 给出（它决定用哪个形状族）；
    ``body`` 是**原文**，不是摘要、不是标题、不是抓取器的二次加工。

    原文为空 → ``body_unavailable``；版式不在形状族里 → ``unparsable_*``。
    两种都必须让上游看见，因为它们的处置完全不同（前者去补原文，后者补解析器）。
    """
    kind_text = str(kind or "")
    if kind_text not in ANNOUNCEMENT_KINDS:
        raise EarningsFactError("unknown_kind", repr(kind))
    text = str(body or "")
    if not text.strip():
        raise EarningsFactError("body_unavailable", "公告原文为空")
    if kind_text == ANNOUNCEMENT_KIND_FORECAST:
        low, high, yoy_low, yoy_high, unit = _parse_forecast(text)
    else:
        low, high, yoy_low, yoy_high, unit = _parse_express(text)
    return _issue_reported_figures(
        version=EARNINGS_FACT_CONTRACT_VERSION,
        kind=kind_text,
        metric=METRIC_NET_PROFIT_ATTRIBUTABLE,
        report_period=_period_from_body(text),
        low_wan=low,
        high_wan=high,
        yoy_low_pct=yoy_low,
        yoy_high_pct=yoy_high,
        unit=unit,
        body_sha256=hashlib.sha256(text.encode("utf-8")).hexdigest(),
    )


# ---------------------------------------------------------------------------
# claim 核验 —— 唯一判定入口
# ---------------------------------------------------------------------------

#: 金额比较容差。单位万元：绝对 1 万元兜住"四舍五入到万元"的表述差异，
#: 相对 0.1% 兜住"14.19 亿元"这类按亿元引用的合法近似。
#: **两个都是声明过的容差**，不是"差不多就行"：越界即 mismatch，见回归用例。
_AMOUNT_ABS_TOLERANCE_WAN = Decimal("1")
#: 同比百分点的绝对容差（0.05 个百分点）。
_PCT_ABS_TOLERANCE = Decimal("0.05")
#: 相对容差，金额与百分点共用。
_RELATIVE_TOLERANCE = Decimal("0.001")


@dataclass(frozen=True, slots=True)
class EarningsClaim:
    """**抽取器**给出的断言（AI 或任何上游）。

    这是本模块里唯一允许外部自由构造的类型 —— 它声明的是"某人认为原文说了什么"，
    而不是"原文说了什么"。它**没有** ``verified`` / ``confidence`` / ``score`` 字段：
    核验结论只能由 :func:`verify_earnings_claim` 签发，不能由 claim 自述。
    """

    kind: str
    report_period: str
    metric: str
    low_wan: Decimal
    high_wan: Decimal
    yoy_low_pct: Decimal | None = None
    yoy_high_pct: Decimal | None = None

    def __post_init__(self) -> None:
        for name in ("kind", "report_period", "metric"):
            if not str(getattr(self, name) or "").strip():
                raise ValueError(f"EarningsClaim.{name} must be non-empty")
        for name in ("low_wan", "high_wan"):
            value = getattr(self, name)
            if not isinstance(value, Decimal):
                raise TypeError(
                    f"EarningsClaim.{name} must be Decimal, got {type(value).__name__}"
                    " — 浮点数会在这里引入不可复现的比较结果"
                )
        if self.high_wan < self.low_wan:
            raise ValueError("EarningsClaim.high_wan must be >= low_wan")
        for name in ("yoy_low_pct", "yoy_high_pct"):
            value = getattr(self, name)
            if value is not None and not isinstance(value, Decimal):
                raise TypeError(f"EarningsClaim.{name} must be Decimal or None")


@dataclass(frozen=True, slots=True, init=False)
class ClaimVerification:
    """一条 claim 相对于一份公告原文的核验结论。

    ``disagreements`` 逐字段列出不一致项（``low_wan`` / ``high_wan`` /
    ``yoy_low_pct`` / ``yoy_high_pct``）—— "数字不对"必须能定位到是哪一个数字不对，
    否则复盘只能得到"AI 说错了"这种无法改进的信息。

    ``status == STATUS_VERIFIED`` 时 ``disagreements`` 必须为空；反过来，
    ``disagreements`` 非空的结论**不可能**是 verified（构造期强制）。
    """

    version: str
    status: str
    disagreements: tuple[str, ...]
    figures: ReportedFigures | None
    body_sha256: str

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        raise TypeError(
            "ClaimVerification has no public constructor: 核验结论不能由调用方自述。"
            "请使用 earnings_disclosure_facts.verify_earnings_claim(...)"
        )

    def __post_init__(self) -> None:
        if self.version != EARNINGS_FACT_CONTRACT_VERSION:
            raise ValueError(
                f"ClaimVerification version {self.version!r} "
                f"!= {EARNINGS_FACT_CONTRACT_VERSION!r}"
            )
        if self.status not in VERIFICATION_STATUSES:
            raise ValueError(
                f"unknown verification status {self.status!r}; "
                f"allowed: {VERIFICATION_STATUSES}"
            )
        object.__setattr__(self, "disagreements", tuple(self.disagreements))
        if self.status == STATUS_VERIFIED and self.disagreements:
            raise ValueError(
                "verified 结论不得携带 disagreements："
                f"{self.disagreements}"
            )
        if self.status == STATUS_CLAIM_FIGURE_MISMATCH and not self.disagreements:
            raise ValueError(
                "claim_figure_mismatch 必须指明是哪个字段不一致 —— "
                "'数字不对'但说不出哪一项，等于没有核验"
            )

    @property
    def is_verified(self) -> bool:
        """是否判定 claim **忠实于原文**。这是本模块唯一的"通过"判据。"""
        return self.status == STATUS_VERIFIED


def _issue_claim_verification(**fields: Any) -> ClaimVerification:
    """签发一条核验结论。唯一调用点是 :func:`verify_earnings_claim`。"""
    verification = object.__new__(ClaimVerification)
    for name, value in fields.items():
        object.__setattr__(verification, name, value)
    verification.__post_init__()
    return verification


def _within_tolerance(claimed: Decimal, parsed: Decimal, abs_tolerance: Decimal) -> bool:
    """声明过的容差比较：``|差| <= max(绝对容差, 原值 × 相对容差)``。"""
    return abs(claimed - parsed) <= max(abs_tolerance, abs(parsed) * _RELATIVE_TOLERANCE)


def verify_earnings_claim(
    claim: Any,
    meta: Any,
    body: Any,
    *,
    superseded_by: Any = None,
) -> ClaimVerification:
    """核验一条 claim 是否忠实于这份公告原文。**唯一判定入口。**

    判定顺序是**语义顺序**，不是性能顺序 —— 前面每一步都会让后面的比较失去意义：

    ```text
    标题分类不可用           → announcement_unsupported（连身份都没有，不谈数字）
    已被修正公告取代          → superseded（过期事实即使自洽也不算通过）
    没有原文                 → body_unavailable（无从核验 ≠ 核验不通过）
    claim 的类型/报告期/指标不符 → claim_*_mismatch（比错了对象，不比数字）
    原文解析不出              → body_unparsable
    数字逐项比对             → verified / claim_figure_mismatch
    ```

    ``superseded_by`` 由持久层给出（它才知道有没有更晚的修正公告）；本模块
    不判断"谁取代谁"，只接受一个已确定的结论 —— 否则这里会出现第二套修正链
    语义，与将来的 ledger 漂移。
    """
    if type(claim) is not EarningsClaim:
        raise TypeError(
            "claim must be a typed EarningsClaim, got "
            f"{type(claim).__name__} — dict / 伪对象不得冒充抽取器断言"
        )
    if type(meta) is not AnnouncementMeta:
        raise TypeError(
            "meta must be a typed AnnouncementMeta from classify_announcement(), got "
            f"{type(meta).__name__}"
        )
    text = str(body or "")
    digest = hashlib.sha256(text.encode("utf-8")).hexdigest()

    def _verdict(status: str, disagreements: tuple[str, ...] = (),
                 figures: ReportedFigures | None = None) -> ClaimVerification:
        return _issue_claim_verification(
            version=EARNINGS_FACT_CONTRACT_VERSION,
            status=status,
            disagreements=disagreements,
            figures=figures,
            body_sha256=digest,
        )

    if not meta.supported:
        return _verdict(STATUS_ANNOUNCEMENT_UNSUPPORTED)
    if superseded_by is not None:
        return _verdict(STATUS_SUPERSEDED)
    if not text.strip():
        return _verdict(STATUS_BODY_UNAVAILABLE)
    if claim.kind != meta.kind:
        return _verdict(STATUS_CLAIM_KIND_MISMATCH)
    if claim.report_period != meta.report_period:
        return _verdict(STATUS_CLAIM_PERIOD_MISMATCH)
    if claim.metric not in SUPPORTED_METRICS:
        return _verdict(STATUS_CLAIM_METRIC_UNSUPPORTED)

    try:
        figures = parse_reported_figures(meta.kind, text)
    except EarningsFactError:
        return _verdict(STATUS_BODY_UNPARSABLE)

    if figures.report_period is not None and figures.report_period != claim.report_period:
        # 标题与正文对报告期的说法不一致：这已经不是"数字对不对"，而是"比对了错的期间"。
        return _verdict(STATUS_CLAIM_PERIOD_MISMATCH, figures=figures)

    disagreements: list[str] = []
    if not _within_tolerance(claim.low_wan, figures.low_wan, _AMOUNT_ABS_TOLERANCE_WAN):
        disagreements.append("low_wan")
    if not _within_tolerance(claim.high_wan, figures.high_wan, _AMOUNT_ABS_TOLERANCE_WAN):
        disagreements.append("high_wan")
    for name, claimed, parsed in (
        ("yoy_low_pct", claim.yoy_low_pct, figures.yoy_low_pct),
        ("yoy_high_pct", claim.yoy_high_pct, figures.yoy_high_pct),
    ):
        if claimed is None:
            # claim 没声称同比 → 不构成不一致（少说不算说错）。
            continue
        if parsed is None:
            # claim 声称了同比但原文复算不出来 → 这条 claim 无法被证实。
            disagreements.append(name)
            continue
        if not _within_tolerance(claimed, parsed, _PCT_ABS_TOLERANCE):
            disagreements.append(name)

    if disagreements:
        return _verdict(STATUS_CLAIM_FIGURE_MISMATCH, tuple(disagreements), figures)
    return _verdict(STATUS_VERIFIED, (), figures)

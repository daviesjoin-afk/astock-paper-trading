"""Explicit capability registry for the pinned upstream public-data source.

Counts follow the upstream README's reconciliation: 88 inventory rows, minus the
industry-report row sharing the Eastmoney reportapi endpoint, minus the local
northbound cache row, plus the second endpoint inside the anomaly-pool row =
87 capability endpoints (82 primary + 5 backups).  Each capability keeps its
real callable(s), temporal semantics, credentials, optional dependencies and
known operational blockers.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from .source_snapshot import (SKILL_SHA256, UPSTREAM_COMMIT, UPSTREAM_LICENSE,
                              UPSTREAM_NAME, UPSTREAM_SKILL_SHA256, UPSTREAM_URL,
                              UPSTREAM_VERSION)

CURRENT = "current_snapshot"
SAME_DAY = "same_trading_day"
HISTORY = "historical_date_window"
PIT = "point_in_time_history"
LOCAL = "local_computation"
CALENDAR = "calendar"
UNKNOWN = "unknown"

CORE_HTTP = ("requests",)
PANDAS = ("pandas",)
EXCEL = ("pandas", "openpyxl", "xlrd")
MOOTDX = ("mootdx",)
BAOSTOCK = ("baostock", "pandas")
NUMPY = ("numpy", "pandas")
IWENCAI = ("IWENCAI_API_KEY", "IWENCAI_BASE_URL")
BSE_ANON = ("anonymous_cookie",)

KNOWN_BLOCKERS: dict[str, dict[str, Any]] = {
    "eastmoney_ip_risk": {
        "status": "documented_rate_limit",
        "detail": "Upstream documents IP-level WAF risk; em_get serializes Eastmoney requests with >=1s interval and jitter. These are community observations, not an official SLA.",
        "permanent": False,
        "evidence": "SKILL.md 数据源优先级 & 东财防封",
    },
    "tencent_501_anti_scrape": {
        "status": "unexplained_one_machine_transient_failures",
        "detail": "A later 2026-11-14 audit corrected the earlier claim: the 64-symbol speed test was 64/64 successful; HTTP 501 and timeouts occurred on later calls on that machine. Start time, root cause and permanence are undetermined; this is not a permanent block.",
        "permanent": False,
        "evidence": "v2/docs/PUBLIC-DATA-AUDIT-2026-11-14.md:38, PUBLIC-DATA-EXECUTION-2026-11-14.md:28",
    },
    "tencent_kline_rate_limit": {
        "status": "documented_rate_limit",
        "detail": "A single Tencent K-line host may return empty JSON after roughly 600 requests; upstream rotates three hosts.",
        "permanent": False,
        "evidence": "SKILL.md §1.2 / README.md endpoint inventory",
    },
    "tdx_tcp_bars_empty": {
        "status": "persistent_tcp_capability_failure_http_package_unaffected",
        "detail": "Since 2026-09 public TDX TCP servers return empty bars/quotes/transactions across ten tested servers, while finance/F10 remain available. This is distinct from the TDX HTTP after-close package, measured working locally on 2026-11-14 (5,578 rows in 44-62 s).",
        "permanent": None,
        "evidence": "SKILL.md §1.7, docs/source-integration-v3.9.0.md #52, v2/docs/PUBLIC-DATA-LAYER-2026-11-14.md §二",
    },
    "baostock_second_symbol_blocking": {
        "status": "unexplained_runtime_timeout_anomaly",
        "detail": "A later audit found both successful BaoStock queries and subsequent timeouts, but no server-side evidence of a queue or rate limit. Use bounded timeouts and statistics; cause and permanence remain unknown.",
        "permanent": False,
        "evidence": "v2/docs/PUBLIC-DATA-AUDIT-2026-11-14.md:39, PUBLIC-DATA-EXECUTION-2026-11-14.md:28",
    },
    "baostock_no_bse": {
        "status": "provider_capability_restriction",
        "detail": "BaoStock rejects Beijing 4/8/92 code ranges (server error 10004011); the adapter refuses before login.",
        "permanent": True,
        "evidence": "SKILL.md §6.5 / v2/data_sources/public_layer.py",
    },
    "sse_company_candidate_failed": {
        "status": "candidate_endpoint_not_in_registry",
        "detail": "The SSE companyGeneralization candidate returned an HTTP 200 business error in the 2026-11-12 research; cause was not proven. This is not the SSE margin/ETF-share endpoint used by the registry.",
        "permanent": False,
        "evidence": "v2/docs/MARKET-SOURCES-RESEARCH-2026-11-12.md §其他资料接口实测",
    },
    "cninfo_webapi_unauthorized": {
        "status": "credentialed_candidate_api_not_in_registry",
        "detail": "CNINFO p_sysapi1133 returned business code 451 without a signed request; no signing or bypass is implemented. CNINFO announcement paging is a different endpoint and succeeded separately.",
        "permanent": None,
        "evidence": "v2/docs/MARKET-SOURCES-RESEARCH-2026-11-12.md §其他资料接口实测",
    },
    "no_exact_publication_time": {
        "status": "temporal_precision_limitation",
        "detail": "CNINFO sample timestamps are Shanghai midnight and publication_precision=date; they do not prove pre-open availability or intraday first publication.",
        "permanent": True,
        "evidence": "v2/docs/MARKET-SOURCES-RESEARCH-2026-11-12.md, PUBLIC-MARKET-DATA.md",
    },
    "dce_official_daily_unavailable": {
        "status": "not_integrated_due_to_anti_scrape",
        "detail": "DCE official daily pages returned HTTP 412 / JS challenge; DCE products use Sina futures realtime and daily K-line instead.",
        "permanent": None,
        "evidence": "README.md §期货与大宗商品层, docs/source-integration-v3.9.0.md",
    },
}

SOURCE_CATALOG = (
    "腾讯财经", "mootdx", "东财 datacenter", "东财 push2/push2his", "iwencai",
    "东财 reportapi/PDF", "同花顺热点", "同花顺 hsgtApi", "百度股市通", "新浪财经",
    "同花顺 basic", "财联社", "巨潮 cninfo", "上交所官方", "深交所官方", "baostock",
    "申万研究", "人民银行", "国家统计局", "中证指数", "国证指数", "北交所官方",
    "通达信官网盘后包", "华尔街见闻", "央视网", "上证e互动", "中债", "中国货币网",
    "上期所", "上期能源", "郑商所", "中金所", "广期所", "上金所",
)


@dataclass(frozen=True)
class Capability:
    id: str
    name: str
    layer: str
    description: str
    source: str
    functions: tuple[str, ...]
    temporal: str = CURRENT
    temporal_note: str = ""
    credentials: tuple[str, ...] = ()
    optional_dependencies: tuple[str, ...] = ()
    markets: tuple[str, ...] = ("SH", "SZ", "BJ")
    blockers: tuple[str, ...] = ()
    backup: bool = False
    counted: bool = True
    endpoint_count: int = 1
    notes: str = ""

    def as_dict(self) -> dict[str, Any]:
        return dict(id=self.id, name=self.name, layer=self.layer,
                    description=self.description, source=self.source,
                    functions=list(self.functions), temporal=self.temporal,
                    temporal_note=self.temporal_note,
                    credentials=list(self.credentials),
                    optional_dependencies=list(self.optional_dependencies),
                    markets=list(self.markets), blockers=[
                        dict(id=name, **KNOWN_BLOCKERS[name]) for name in self.blockers],
                    backup=self.backup, counted=self.counted,
                    endpoint_count=self.endpoint_count, notes=self.notes)


def _c(*args: Any, **kwargs: Any) -> Capability:
    return Capability(*args, **kwargs)


# 88 inventory rows in the exact upstream README order.  ``counted=False``
# implements the README's two subtraction rows; the anomaly row has count=2.
CAPABILITIES: tuple[Capability, ...] = (
    _c("quote.tencen_snapshot", "腾讯财经", "行情层", "实时价/PE/PB/市值/换手/涨跌停/指数/ETF", "腾讯财经", ("tencent_quote",), SAME_DAY, "网站快照；quote_time 与 fetched_at 分开，不承诺交易级实时性"),
    _c("bars.tencent_kline", "腾讯 K 线", "行情层", "日/周/月前后复权 + 1~60 分钟 K 线", "腾讯财经", ("tencent_kline",), HISTORY, "分钟线仅最近 <=320 根；日周月按窗口分段；不含北交所", markets=("SH", "SZ"), blockers=("tencent_501_anti_scrape", "tencent_kline_rate_limit")),
    _c("bars.tdx_daily_package", "通达信盘后包", "行情层", "某交易日沪深北全市场日线含成交额", "通达信官网盘后包", ("tdx_daily_package",), HISTORY, "实测 2022-01-04、2023-01-03 可取，2021-01-04 已 404；2022-05-06 前无北交所文件；包通常收盘后数小时发布", optional_dependencies=PANDAS),
    _c("trades.tencent_ticks", "腾讯逐笔成交", "行情层", "最近一个交易日分笔成交", "腾讯财经", ("tencent_ticks",), SAME_DAY, "仅最近交易日；约 3 秒一笔，非 Level-2；盘中不保证完整", markets=("SH", "SZ"), blockers=("tencent_501_anti_scrape",), optional_dependencies=PANDAS),
    _c("bars.baidu_kline_ma", "百度K线", "行情层", "日 K 线 + MA5/10/20", "百度股市通", ("baidu_kline_with_ma",), HISTORY),
    _c("adjust.sina_factor", "新浪复权因子", "行情层", "qfq/hfq 因子序列并套用 K 线", "新浪财经", ("sina_adjust_factor", "apply_adjust"), HISTORY, "复权口径不是当时撮合价；因子版本/as_of 必须保留"),
    _c("bars.mootdx_legacy", "mootdx 行情（留档）", "行情层", "K 线 / 五档 / 逐笔 / 报价（留档）", "mootdx", ("tdx_client",), UNKNOWN, "2026-09 起行情命令普遍空；财务/F10 仍可用", optional_dependencies=MOOTDX, blockers=("tdx_tcp_bars_empty",)),
    _c("reports.eastmoney_stock", "东财 reportapi", "研报层", "个股研报 + 评级 + 三年 EPS", "东财 reportapi/PDF", ("eastmoney_reports",), HISTORY, "默认近两年；返回当前检索列表", blockers=("eastmoney_ip_risk",)),
    _c("reports.eastmoney_industry_same_endpoint", "东财 行业研报", "研报层", "同一 reportapi 的 qType=1 行业研报", "东财 reportapi/PDF", ("eastmoney_industry_reports",), HISTORY, counted=False, notes="README 计数口径：与东财 reportapi 是同一端点，不重复计数", blockers=("eastmoney_ip_risk",)),
    _c("reports.eastmoney_pdf", "东财 PDF 下载", "研报层", "研报 PDF 文件", "东财 reportapi/PDF", ("download_pdf",), CURRENT, "下载原始 PDF；会写调用者指定目录", blockers=("eastmoney_ip_risk",)),
    _c("reports.ths_eps", "同花顺一致预期", "研报层", "机构一致预期 EPS", "同花顺 basic", ("ths_eps_forecast",), CURRENT, "当前一致预期，不是历史时点预测", optional_dependencies=PANDAS),
    _c("reports.iwencai", "iwencai NL 搜索", "研报层", "自然语言跨主题研报检索/选股", "iwencai", ("iwencai_search", "iwencai_query"), CURRENT, credentials=IWENCAI),
    _c("reports.sina_list", "**新浪研报列表**", "研报层", "标题/类型/机构/研究员/日期", "新浪财经", ("sina_research_reports",), HISTORY, "无评级和目标价；强制请求间隔并复核空页", optional_dependencies=PANDAS),
    _c("signals.ths_hot", "同花顺热点", "信号层", "当日强势股 + 题材归因", "同花顺热点", ("ths_hot_reason",), SAME_DAY, optional_dependencies=PANDAS),
    _c("signals.hsgt_realtime", "同花顺北向（实时）", "信号层", "沪股通/深股通分钟资金流", "同花顺 hsgtApi", ("hsgt_realtime",), SAME_DAY, "深股通上游披露收紧仅作参考；北向权威统计见 HKEX 备胎说明", optional_dependencies=PANDAS),
    _c("signals.hsgt_local_cache", "同花顺北向（历史）", "信号层", "本地自缓存日级历史", "本地计算", ("_save_northbound_snapshot", "_load_northbound_history"), LOCAL, "README 计数口径：本地缓存，不是独立端点", counted=False),
    _c("signals.em_concepts", "东财板块归属", "信号层", "个股板块/概念/地域 + BK 码", "东财 push2/push2his", ("eastmoney_concept_blocks",), CURRENT, "当前板块归属，不可回填历史分类", blockers=("eastmoney_ip_risk",)),
    _c("signals.em_fund_flow_minute", "东财资金流向", "信号层", "分钟级主力/四档资金流", "东财 push2/push2his", ("eastmoney_fund_flow_minute",), SAME_DAY, blockers=("eastmoney_ip_risk",)),
    _c("signals.dragon_tiger", "龙虎榜席位", "信号层", "个股上榜 + 买卖席位 TOP5", "东财 datacenter", ("dragon_tiger_board",), HISTORY, "按回看窗口查历史；空窗口是明确空结果", blockers=("eastmoney_ip_risk",)),
    _c("signals.daily_dragon_tiger", "全市场龙虎榜", "信号层", "每日全市场上榜股票", "东财 datacenter", ("daily_dragon_tiger",), HISTORY, blockers=("eastmoney_ip_risk",)),
    _c("signals.lockup_expiry", "限售解禁日历", "信号层", "历史解禁 + 未来 90 天待解禁", "东财 datacenter", ("lockup_expiry",), HISTORY, blockers=("eastmoney_ip_risk",)),
    _c("signals.industry_rank", "行业板块排名", "信号层", "行业涨跌/上涨下跌家数", "东财 push2/push2his", ("industry_comparison",), SAME_DAY, blockers=("eastmoney_ip_risk",)),
    _c("signals.board_fund_flow", "板块资金流向", "信号层", "行业/概念/地域 × 今日/5日/10日", "东财 push2/push2his", ("board_fund_flow",), HISTORY, blockers=("eastmoney_ip_risk",)),
    _c("capital.margin", "**融资融券明细**", "资金面 / 筹码层", "融资融券日级明细", "东财 datacenter", ("margin_trading",), HISTORY, blockers=("eastmoney_ip_risk",)),
    _c("capital.block_trade", "**大宗交易**", "资金面 / 筹码层", "成交价量 + 买卖营业部", "东财 datacenter", ("block_trade",), HISTORY, blockers=("eastmoney_ip_risk",)),
    _c("capital.holder_num", "**股东户数变化**", "资金面 / 筹码层", "季度股东数 + 环比", "东财 datacenter", ("holder_num_change",), HISTORY, blockers=("eastmoney_ip_risk",)),
    _c("capital.dividend", "**分红送转历史**", "资金面 / 筹码层", "派息/送股/转增 + 进度", "东财 datacenter", ("dividend_history",), HISTORY, blockers=("eastmoney_ip_risk",)),
    _c("capital.fund_flow_120d", "**个股资金流120日**", "资金面 / 筹码层", "主力/四档日级净流入", "东财 push2/push2his", ("stock_fund_flow_120d",), HISTORY, blockers=("eastmoney_ip_risk",)),
    _c("capital.chip_distribution", "**筹码分布 CYQ**", "资金面 / 筹码层", "获利比例/平均成本/成本区间/筹码峰", "本地计算", ("chip_distribution",), LOCAL, "由 OHLC + 换手率本地推演；初始筹码播种为首日流通盘", optional_dependencies=NUMPY),
    _c("capital.etf_shares", "**ETF 份额**", "资金面 / 筹码层", "上交所按日归档 / 深交所当前快照", "上交所官方,深交所官方", ("etf_shares",), HISTORY, "上交所可按日归档（实测 2022-01-04 仍有），深交所仅当前快照", optional_dependencies=EXCEL),
    _c("news.em_stock", "个股新闻", "新闻层", "个股新闻流", "东财 push2/push2his", ("eastmoney_stock_news",), CURRENT, blockers=("eastmoney_ip_risk",)),
    _c("news.cls", "财联社电报", "新闻层", "全市场实时快讯", "财联社", ("cls_telegraph",), SAME_DAY),
    _c("news.em_global", "全球资讯", "新闻层", "东财 7×24 快讯", "东财 datacenter", ("eastmoney_global_news",), SAME_DAY, blockers=("eastmoney_ip_risk",)),
    _c("news.wscn_lives", "**华尔街见闻快讯**", "新闻层", "频道 7×24 快讯 + 游标", "华尔街见闻", ("wallstreetcn_lives",), SAME_DAY, optional_dependencies=PANDAS),
    _c("news.cctv", "**新闻联播**", "新闻层", "当日条目 + 文字稿", "央视网", ("cctv_news",), SAME_DAY, "当晚约 20:00 后更新；之前请求是真实无数据", optional_dependencies=PANDAS),
    _c("fundamentals.tdx_finance", "季报快照", "基础数据 / 公告层", "37 字段季报快照", "mootdx", ("tdx_client",), CURRENT, "F10/财务仍可用；需 finance 验活", optional_dependencies=MOOTDX, blockers=("tdx_tcp_bars_empty",)),
    _c("fundamentals.tdx_f10", "F10 文本", "基础数据 / 公告层", "最新提示类 F10 文本", "mootdx", ("tdx_client",), CURRENT, "2026-09 起服务端只剩最新提示；不能当公告全文", optional_dependencies=MOOTDX),
    _c("fundamentals.em_info", "东财个股信息", "基础数据 / 公告层", "行业/股本/市值/上市日", "东财 push2/push2his", ("eastmoney_stock_info",), CURRENT, "当前快照；不能作为历史时点成员资格", blockers=("eastmoney_ip_risk",)),
    _c("fundamentals.sina_statements", "新浪财报三表", "基础数据 / 公告层", "资产负债/利润/现金流量表", "新浪财经", ("sina_financial_report",), HISTORY, "报告期不等于当时可见日；无 PIT 修订档案"),
    _c("fundamentals.cninfo_announcements", "巨潮公告", "基础数据 / 公告层", "公告全文检索 + PDF 链接", "巨潮 cninfo", ("cninfo_announcements",), HISTORY, "上海日期精度；不证明盘中首发时刻；PDF 正文未下载验证", blockers=("no_exact_publication_time",)),
    _c("fundamentals.baostock_valuation", "估值历史", "基础数据 / 公告层", "日频 PE/PB/PS/PCF + 换手 + 停牌 + ST", "baostock", ("baostock_valuation_history",), HISTORY, "逐日历史序列，不保证历史修订版本的首次可得性；拒绝北交所", markets=("SH", "SZ"), optional_dependencies=BAOSTOCK, blockers=("baostock_second_symbol_blocking", "baostock_no_bse")),
    _c("fundamentals.baostock_basic", "上市/退市日", "基础数据 / 公告层", "ipoDate / outDate / 状态", "baostock", ("baostock_stock_basic",), HISTORY, "当前下载版本含上市退市日期，不证明当年可知版本；不支持北交所", markets=("SH", "SZ"), optional_dependencies=BAOSTOCK, blockers=("baostock_no_bse",)),
    _c("fundamentals.sw_industry_history", "申万行业变迁史", "基础数据 / 公告层", "历次行业调整 + as-of 查询", "申万研究", ("sw_industry_history", "sw_industry_as_of"), HISTORY, "带分类生效日期；当前下载版本可按历史日查询，不等同当时发布版本的 PIT 档案；仅行业代码", optional_dependencies=EXCEL),
    _c("fundamentals.st_list", "ST 名单", "基础数据 / 公告层", "沪深京 ST/*ST 当日名单", "东财 push2/push2his,baostock", ("st_stock_list",), CURRENT, "东财不可达时才退 baostock；备胎只有沪深、无价格", optional_dependencies=BAOSTOCK + PANDAS, blockers=("baostock_no_bse",)),
    _c("limit.em_zt", "东财涨停池", "打板层", "连板/封板资金/炸板次数", "东财 push2/push2his", ("em_zt_pool",), SAME_DAY, blockers=("eastmoney_ip_risk",)),
    _c("limit.em_zb", "东财炸板池", "打板层", "曾涨停后开板", "东财 push2/push2his", ("em_zb_pool",), SAME_DAY, blockers=("eastmoney_ip_risk",)),
    _c("limit.em_dt", "东财跌停池", "打板层", "封单/连续跌停/开板次数", "东财 push2/push2his", ("em_dt_pool",), SAME_DAY, blockers=("eastmoney_ip_risk",)),
    _c("limit.em_yzt", "东财昨日涨停池", "打板层", "昨涨停今表现", "东财 push2/push2his", ("em_yzt_pool",), SAME_DAY, blockers=("eastmoney_ip_risk",)),
    _c("limit.ths_reason", "同花顺涨停揭秘", "打板层", "涨停原因/封板成功率/板型", "同花顺热点", ("ths_limit_up_pool",), HISTORY),
    _c("limit.em_monitor", "东财重点监控池", "打板层", "风险警示/重点监控 + 生效窗口", "东财 push2/push2his", ("em_stock_monitor",), CURRENT, blockers=("eastmoney_ip_risk",)),
    _c("limit.em_anomaly", "东财日内异动池", "打板层", "严重异常波动明细 + 聚合统计 + 规则码", "东财 push2/push2his", ("em_price_anomaly", "em_price_anomaly_count"), SAME_DAY, endpoint_count=2, notes="README 计数口径：一行含 list/count 两个端点", blockers=("eastmoney_ip_risk",)),
    _c("options.sina_codes", "期权合约清单", "ETF 期权层", "ETF 期权认购认沽合约", "新浪财经", ("sina_option_codes",), CURRENT),
    _c("options.sina_tquote", "T型报价", "ETF 期权层", "五档/持仓量/行权价/最新价", "新浪财经", ("sina_option_tquote",), CURRENT),
    _c("options.sina_greeks", "希腊字母 + IV", "ETF 期权层", "Delta/Gamma/Theta/Vega/IV/理论价", "新浪财经", ("sina_option_greeks",), CURRENT),
    _c("interaction.cninfo_irm", "互动易问答", "舆情互动层", "深市提问 + 公司回复", "巨潮 cninfo", ("cninfo_irm",), HISTORY, markets=("SZ",)),
    _c("interaction.sse_e", "上证e互动", "舆情互动层", "沪市提问 + 公司回复", "上证e互动", ("sse_e_interaction",), HISTORY, "相对/绝对时间按北京时间换算；公司近 1 月无回复返回空表", markets=("SH",), optional_dependencies=PANDAS),
    _c("interaction.ths_hot", "同花顺热榜", "舆情互动层", "人气值/概念标签/排名变化", "同花顺热点", ("ths_hot_list",), SAME_DAY),
    _c("interaction.em_hot_rank", "东财人气榜", "舆情互动层", "排名/变化/名称价格", "东财 push2/push2his", ("em_hot_rank",), SAME_DAY, blockers=("eastmoney_ip_risk",)),
    _c("interaction.em_hot_concept", "东财个股概念命中", "舆情互动层", "个股概念热度", "东财 push2/push2his", ("em_hot_concept",), CURRENT, blockers=("eastmoney_ip_risk",)),
    _c("macro.pboc_social_financing", "人民银行社融", "宏观与利率层", "月度社融增量 12 列", "人民银行", ("pboc_social_financing",), HISTORY, "月度；未发布月份整行丢弃，不填 NaN", optional_dependencies=PANDAS + ("xlrd",)),
    _c("macro.nbs_pmi", "国家统计局 PMI", "宏观与利率层", "制造业/非制造业/综合 PMI", "国家统计局", ("nbs_pmi",), HISTORY),
    _c("macro.chinabond_curve", "中债收益率曲线", "宏观与利率层", "国债/银行 AAA/中短票 AAA 曲线", "中债", ("chinabond_yield_curve",), HISTORY, "起点：国债 2006-03-01、中短票 2006-12-25、银行 AAA 2009-12-24；单次 >1 年按 360 天切片", optional_dependencies=PANDAS),
    _c("macro.repo_fixing", "回购定盘利率", "宏观与利率层", "FR001/007/014 与 FDR", "中国货币网", ("repo_fixing_rates",), HISTORY, "FR 约近 3 年、FDR 约近 1 年", optional_dependencies=PANDAS),
    _c("macro.lpr", "LPR", "宏观与利率层", "1 年/5 年全历史", "东财 datacenter", ("lpr_history",), HISTORY, "含 2013~2019 旧机制逐日报价", optional_dependencies=PANDAS, blockers=("eastmoney_ip_risk",)),
    _c("macro.calendar", "全球宏观日历", "宏观与利率层", "公布值/预期/前值/修正值/重要性", "华尔街见闻", ("macro_calendar",), HISTORY, "单次最多 92 个自然日；整周空结果视为源错误", optional_dependencies=PANDAS),
    _c("index.constituents", "指数成分", "指数与交易日历层", "中证/国证最近公布成分", "中证指数,国证指数", ("index_constituents",), CURRENT, "保留源文件日期；国证是月末快照，不提供历史调样回填", optional_dependencies=EXCEL),
    _c("index.weights", "指数权重", "指数与交易日历层", "中证/国证最近公布权重", "中证指数,国证指数", ("index_weights",), CURRENT, "百分数口径；不假定与成分同日", optional_dependencies=EXCEL),
    _c("index.valuation", "指数估值", "指数与交易日历层", "两种股本口径 PE + 股息率", "中证指数", ("index_valuation",), CURRENT, "近期文件；不含 PB、不承诺全历史", optional_dependencies=EXCEL),
    _c("index.trading_calendar", "官方交易日历", "指数与交易日历层", "深交所完整自然月交易标志", "深交所官方", ("trading_calendar",), CALENDAR, "缺日/未发布/未知标志抛错；周末调休不视为交易日", optional_dependencies=PANDAS),
    _c("futures.daily", "期货日行情", "期货与大宗商品层", "五家期货交易所官方收盘数据", "上期所,上期能源,郑商所,中金所,广期所", ("futures_daily",), HISTORY, "上期所最早 2002-01-07、中金所 2010-04-16；大商所官方日行情未接入", optional_dependencies=PANDAS, blockers=("dce_official_daily_unavailable",)),
    _c("futures.options_daily", "期权日行情", "期货与大宗商品层", "商品/股指期权日行情 + Delta/IV", "上期所,上期能源,郑商所,中金所,广期所", ("options_daily",), HISTORY, "中金所不公布 Delta/IV；上期能源期权首日 2021-06-21", optional_dependencies=PANDAS),
    _c("futures.position_rank", "会员持仓排名", "期货与大宗商品层", "成交/持买/持卖前 20 名", "上期所,上期能源,郑商所,中金所", ("futures_position_rank",), HISTORY, optional_dependencies=PANDAS),
    _c("futures.realtime", "实时期货", "期货与大宗商品层", "新浪实时价/买卖一档", "新浪财经", ("futures_realtime",), SAME_DAY, "覆盖六家交易所；大商所品种走这里", optional_dependencies=PANDAS, blockers=("dce_official_daily_unavailable",)),
    _c("futures.kline", "期货日 K", "期货与大宗商品层", "逐日 K 线 / 主力连续", "新浪财经", ("futures_kline",), HISTORY, "主力连续最早到 2005；具体合约约 2022 年起到期；结算价不全", optional_dependencies=PANDAS),
    _c("futures.a50", "A50 期指", "期货与大宗商品层", "富时中国 A50 连续合约", "新浪财经", ("a50_futures",), SAME_DAY, optional_dependencies=PANDAS),
    _c("futures.sge_spot", "上海金现货", "期货与大宗商品层", "黄金/白银/铂金现货日线", "上金所", ("sge_spot",), HISTORY, "无成交时段填 0 并剔除；空值/NaN 报错", optional_dependencies=PANDAS),
    _c("events.earnings_forecast", "**业绩预告**", "事件驱动层", "预告类型/金额/变动区间/原因", "东财 datacenter", ("earnings_forecast",), HISTORY, optional_dependencies=PANDAS, blockers=("eastmoney_ip_risk",)),
    _c("events.institution_survey", "**机构调研**", "事件驱动层", "调研汇总或逐机构明细", "东财 datacenter", ("institution_survey",), HISTORY, optional_dependencies=PANDAS, blockers=("eastmoney_ip_risk",)),
    _c("events.holder_trades", "**股东增减持**", "事件驱动层", "方向/股数/比例/均价/渠道", "东财 datacenter", ("holder_trades",), HISTORY, optional_dependencies=PANDAS, blockers=("eastmoney_ip_risk",)),
    _c("events.share_buyback", "**股票回购**", "事件驱动层", "方案区间/已回购/进度", "东财 datacenter", ("share_buyback",), HISTORY, optional_dependencies=PANDAS, blockers=("eastmoney_ip_risk",)),
    _c("events.equity_pledge", "**股权质押**", "事件驱动层", "中国结算周度质押比例", "东财 datacenter", ("equity_pledge",), HISTORY, "仅沪深；周度口径", markets=("SH", "SZ"), optional_dependencies=PANDAS, blockers=("eastmoney_ip_risk",)),
    _c("events.ipo_calendar", "**新股申购日历**", "事件驱动层", "申购/中签/缴款/上市日", "东财 datacenter", ("ipo_calendar",), HISTORY, optional_dependencies=PANDAS, blockers=("eastmoney_ip_risk",)),
    _c("bonds.convertible", "**可转债**", "可转债层", "条款/转股价/转股价值/溢价率", "东财 datacenter", ("convertible_bonds",), CURRENT, "上市日/摘牌日与今天比较判定状态；退市板块按交易市场", optional_dependencies=PANDAS, blockers=("eastmoney_ip_risk",)),
    _c("backup.dragon_tiger", "官方龙虎榜备胎", "备用源", "沪深交易所官方龙虎榜含营业部", "上交所官方,深交所官方", ("dragon_tiger_backup",), HISTORY, backup=True),
    _c("backup.fund_flow", "资金流备胎", "备用源", "新浪日度四档单净额", "新浪财经", ("fund_flow_backup",), HISTORY, backup=True),
    _c("backup.announcements", "公告备胎", "备用源", "深交所官方/东财沪市公告 + PDF 直链", "深交所官方,东财 datacenter", ("announcements_backup",), HISTORY, backup=True, blockers=("no_exact_publication_time",)),
    _c("backup.margin", "官方两融备胎", "备用源", "沪深分交易所融资融券明细", "上交所官方,深交所官方", ("margin_trading_backup",), HISTORY, "必须核对交易日；单所结果不得标为沪深全市场", markets=("SH", "SZ"), backup=True, optional_dependencies=EXCEL),
    _c("backup.bse_quote", "北交所行情备胎", "备用源", "全板/单票 OHLC + 五档快照", "北交所官方", ("bse_quote_backup",), CURRENT, "当前快照；请求日必须与源侧每行一致，不提供历史回填，盘中延迟未标定", markets=("BJ",), credentials=BSE_ANON, backup=True, optional_dependencies=PANDAS),
)

# Local helpers and route-table entries that are callable but deliberately not
# counted as capability endpoints by upstream.
LOCAL_HELPERS = (
    ("ticker.norm", "norm_ticker", "代码归一化"),
    ("ticker.joinquant", "to_joinquant", "转聚宽代码"),
    ("ticker.prefix", "get_prefix", "市场前缀"),
    ("ticker.em_market", "em_market_code", "东财市场号"),
    ("ticker.em_secid", "em_secid", "东财 secid"),
    ("valuation.forward_pe", "forward_pe", "前向 PE"),
    ("valuation.pe_digestion", "pe_digestion", "PE 消化年数"),
    ("valuation.peg", "calc_peg", "PEG"),
    ("valuation.full", "full_valuation", "单票估值全景"),
    ("helper.eastmoney_datacenter", "eastmoney_datacenter", "东财数据中心旧 helper"),
    ("helper.eastmoney_datacenter_strict", "_em_datacenter_strict", "东财严格分页 helper"),
    ("helper.tdx_client", "tdx_client", "mootdx 客户端验活"),
)


def capability_count() -> dict[str, int]:
    counted = [c for c in CAPABILITIES if c.counted]
    return dict(inventory_rows=len(CAPABILITIES),
                capability_endpoints=sum(c.endpoint_count for c in counted),
                primary=sum(c.endpoint_count for c in counted if not c.backup),
                backups=sum(c.endpoint_count for c in counted if c.backup),
                route_table_rows=82,
                sources=len(SOURCE_CATALOG),
                layers=15)


def upstream_metadata() -> dict[str, Any]:
    return dict(name=UPSTREAM_NAME, version=UPSTREAM_VERSION, commit=UPSTREAM_COMMIT,
                license=UPSTREAM_LICENSE, url=UPSTREAM_URL,
                upstream_skill_sha256=UPSTREAM_SKILL_SHA256,
                publication_skill_sha256=SKILL_SHA256,
                mirror_kind="publication_derivative",
                counts=capability_count())

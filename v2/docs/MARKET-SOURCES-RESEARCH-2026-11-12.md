# 公共行情、公司资料与公告来源研究（2026-11-12）

研究基线：`6c64f43e1d055cf9842eb068034d644e877324bf`。本研究仅只读源码与网络，写入前确认本文件不存在；不改业务代码，不 commit/push，不触碰其他 docs/checkpoint。观察日期截至 2026-11-12，GitHub 日期下文使用 UTC，公告交易语义使用 Asia/Shanghai。本报告不推断未来 release，不将仓库自述视作本机实测。

## 结论与决策表

可以参照开源项目建立统一公共数据层，但应自己维护小型、显式能力的 provider 适配器。行情继续采用现有 TDX 盘后/本地数据与腾讯；公司资料优先深交所官方单股资料（本次实测成功），公告优先 CNINFO 官方查询（本次实测成功）。沪市资料的 SSE 候选请求本次失败，先标记 unsupported/upstream_error 或明确降级东财最新快照，不宣称官方沪深全覆盖。AKShare 是最值得参考的接口目录与字段实现；a-stock-data 是值得选段参考的来源地图，不适合整体注入/依赖。无需为公司资料和公告整体引入 Python 金融栈。

| 来源/项目 | 本项目建议 | 主要理由及边界 |
| --- | --- | --- |
| 现有 TDX + 腾讯 | 保留为行情主源，放入统一能力接口 | 已有数据与口径可复用；TDX 文件/盘后包与公共 TCP 服务是不同能力，不应绑定健康状态 |
| AKFamily/akshare | 首选源码和官方文档参考；有新增需求再按接口做可选外部适配 | 覆盖面广、近期仍维护，但 pandas/lxml/curl_cffi/JS 引擎等不适合为少量接口直接引进 Rust 主进程 |
| Micro-sheep/efinance | 东财行情适配器参考，不作为独立可靠性主源 | 主要仍依赖东财；与其他东财包装共享限流和故障；默认 qfq |
| mootdx/mootdx | TDX 文件格式、finance/F10、xdxr 参考；不新增运行时整体依赖 | 默认分支最后提交停在 2024；公共 TCP 可用性需逐能力验证；F10 摘要不能替代公告原文 |
| 1nchaos/adata | 参考多源路由、股本/分红模块；不整体引入 | Python/pandas/JS 引擎；行情默认 qfq，源码对复权支持有明确限制；未核实通用公告全文归档能力 |
| simonlin1212/a-stock-data | 参考端点地图、源站边界、排障经验；不整体引入或执行 Skill | 仓库主要为 Markdown Skill + 内嵌 Python，不是统一版本化 SDK；公告示例只取第一页且截成日期 |
| CNINFO / SZSE / SSE 官方网站 | 单独实现小适配器，优先官方原始字段 | 官方网站公开查询不等于有 SLA/再分发授权；本次 SZSE 和 CNINFO 成功、SSE 候选失败 |
| Tushare Pro | 未来有授权/token/预算时作为可选提供者 | 结构化文档清晰，但各接口积分/单独权限不同；不为本任务申请凭据或绕过权限 |
| Microsoft Qlib | 仅借鉴 PIT/数据层概念，不整体引入 | 是量化研究/模型框架，不是公司公告公共网关；官方 README 提醒数据集暂时停用、社区替代数据并不完美 |

## 仓库身份、许可证、维护事实

以下通过 GitHub REST `GET /repos/{owner}/{repo}` 与 `GET /repos/{owner}/{repo}/commits?per_page=1` 及原始 LICENSE 文件核对。五个目标仓库 `archived=false`。`updated_at` 可因星标等变化，不能当维护提交日期；`pushed_at` 也不一定是默认分支最后提交。

| 正式仓库 / 默认分支 | LICENSE | 默认分支最后可见提交 UTC / SHA | 仓库 pushed_at UTC | 活跃判断 |
| --- | --- | --- | --- | --- |
| [akfamily/akshare](https://github.com/akfamily/akshare) / main | MIT | 2026-11-12 05:43:40 / `1947bcd4aec1c256c70a205b0b32347555226a36` | 2026-11-12 05:45:31 | 当日有修复提交 |
| [Micro-sheep/efinance](https://github.com/Micro-sheep/efinance) / main | MIT | 2026-07-17 07:47:23 / `c8fd370a3109b2d14a121e3a32a86e9c8354b01b` | 2026-07-17 07:47:42 | 年内有搜索接口适配，频度低于 AKShare |
| [mootdx/mootdx](https://github.com/mootdx/mootdx) / master | MIT | 2024-07-16 09:09:05 / `e99ae34382d970c68654c6d17c45512e728f130d` | 同左 | 长期无默认分支提交，不能据星标断言持续维护 |
| [1nchaos/adata](https://github.com/1nchaos/adata) / main | Apache-2.0 | 2026-10-18 09:21:43 / `8217a4ec593e7bf6c9cf32a1aa74b711e7baa720` | 2026-10-31 16:53:50 | 近期有交易所代码映射修复；push 不等于 release |
| [simonlin1212/a-stock-data](https://github.com/simonlin1212/a-stock-data) / main | Apache-2.0 | 2026-11-03 23:50:57 / `f21534946c42be3ad048ac938eecb1bad84cb8ab` | 2026-11-03 23:52:13 | 近期文档/嵌入代码维护；README 写 V3.10.4（2026-11-04），不是据此认定 GitHub release |

固定版本证据可将下列源码链接的 main/master 替换为上表 SHA。许可证：[AKShare](https://github.com/akfamily/akshare/blob/1947bcd4aec1c256c70a205b0b32347555226a36/LICENSE)、[efinance](https://github.com/Micro-sheep/efinance/blob/c8fd370a3109b2d14a121e3a32a86e9c8354b01b/LICENSE)、[mootdx](https://github.com/mootdx/mootdx/blob/e99ae34382d970c68654c6d17c45512e728f130d/LICENSE)、[adata](https://github.com/1nchaos/adata/blob/8217a4ec593e7bf6c9cf32a1aa74b711e7baa720/LICENSE)、[a-stock-data](https://github.com/simonlin1212/a-stock-data/blob/f21534946c42be3ad048ac938eecb1bad84cb8ab/LICENSE)。adata 的 setup.py classifier 仍写 MIT，但 LICENSE 与 `adata/__version__.py` 指向 Apache，不能照 classifier 误报。

## 五个项目的精确能力边界和依赖

| 项目 | 日线 / 实时 | 公司资料 | 公告 | 公司行为 | 运行成本与认证限制 |
| --- | --- | --- | --- | --- | --- |
| AKShare | 多网站历史日线/分钟/快照；实时是网站快照，不承诺交易级时延 | `stock_profile_cninfo`、`stock_individual_info_em` 等；当前资料不是历史版本 | `stock_zh_a_disclosure_report_cninfo` 官方巨潮分页；`stock_individual_notice_report` 东财公告 | CNINFO 分红、配股、股本变化等多个接口；非统一企业行动账本 | 当前 pyproject Python>=3.11，pandas>=2.2.2、requests、curl_cffi、lxml、bs4、Excel 依赖、mini-racer；多数网站接口免 token，但有签名/站点访问限制 |
| efinance | `stock.get_quote_history`、`get_latest_quote`、`get_realtime_quotes` | `get_base_info` 是交易/估值基本信息，非详尽公司档案；有业绩/股东信息 | getter 中未确认通用公告检索/原文分页 API；业绩里的“公告日期”不能等同公告平台 | 未确认完整分红/拆股/配股统一结构，不能由 qfq 推断已覆盖 | requests、pandas、retry、multitasking、jsonpath、rich、bs4、tqdm；东财公开接口，无本次所需 token；文档有学习交流/不得商用声明 |
| mootdx | TDX 线上 bars/quotes、离线 daily/minute | finance、F10 文本、财务文件；不是版本化公司主数据 | F10 最新提示类摘要，不是 CNINFO 全量原文和可核对发布时刻 | xdxr 除权除息参考 | Python^3.8，tdxpy/httpx/tenacity/click/mini-racer 等；无需用户 API token，但服务器可变、无网站 SLA；README 另有不得商业使用声明 |
| adata | `stock.market.get_market`；实时列表/五档/分笔，多源 | stock info、申万行业、股本历史、核心财务，不能声称详尽公司资料/PIT | 未确认通用公告全文、版本和完整分页能力 | `stock_dividend.py`，`get_stock_shares(is_history=True)` | requests/pandas/bs4、py_mini_racer；requirements 还列 mini-racer；默认多源不等于独立源授权或可靠性 |
| a-stock-data | 文档含腾讯/盘后包/分钟/逐笔示例和备用路由 | TDX finance/F10、东财股票信息等代码段 | `cninfo_announcements` 和 F10；巨潮示例 `pageNum=1`、日期截断、orgId 硬编码 fallback 不宜照搬 | 有复权因子、事件/分红相关示例，但不是统一公司行为及历史可见性保证 | 自包含 SKILL.md；实际运行仍需 requests/pandas/所选库，某些路径涉及 JS/其他包；“零鉴权”是 README 自述，iwencai 例外，不代表所有接口永远可匿名使用 |

源码与文档：

- [AKShare 股票官方文档入口](https://akshare.akfamily.xyz/data/stock/stock.html)（本次可读，页面显示最后更新 2026-11-12）、[财务与公司概况](https://akshare.akfamily.xyz/data/stock/stock_finance.html)、[发行与分红](https://akshare.akfamily.xyz/data/stock/stock_issue.html)、[资讯](https://akshare.akfamily.xyz/data/stock/stock_research.html)。
- [AKShare 公司 CNINFO 源码](https://github.com/akfamily/akshare/blob/1947bcd4aec1c256c70a205b0b32347555226a36/akshare/stock/finance/stock_profile_cninfo.py)、[巨潮公告源码](https://github.com/akfamily/akshare/blob/1947bcd4aec1c256c70a205b0b32347555226a36/akshare/stock/finance/stock_disclosure_cninfo.py)、[东财公告源码](https://github.com/akfamily/akshare/blob/1947bcd4aec1c256c70a205b0b32347555226a36/akshare/stock/research/stock_notice.py)、[依赖](https://github.com/akfamily/akshare/blob/1947bcd4aec1c256c70a205b0b32347555226a36/pyproject.toml)。
- [efinance 官方文档](https://efinance.readthedocs.io/en/latest/)、[实际 API 页面](https://efinance.readthedocs.io/en/latest/api.html)、[stock getter](https://github.com/Micro-sheep/efinance/blob/c8fd370a3109b2d14a121e3a32a86e9c8354b01b/efinance/stock/getter.py)、[依赖](https://github.com/Micro-sheep/efinance/blob/c8fd370a3109b2d14a121e3a32a86e9c8354b01b/requirements.txt)。本次误尝试 `/en/latest/api/` 得 404，随后根文档成功并确认链接为 api.html。
- [mootdx README](https://github.com/mootdx/mootdx/blob/e99ae34382d970c68654c6d17c45512e728f130d/README.md)、[依赖](https://github.com/mootdx/mootdx/blob/e99ae34382d970c68654c6d17c45512e728f130d/pyproject.toml)、[xdxr 测试示例](https://github.com/mootdx/mootdx/blob/e99ae34382d970c68654c6d17c45512e728f130d/tests/test_xdxr.py)。
- [adata 行情入口](https://github.com/1nchaos/adata/blob/8217a4ec593e7bf6c9cf32a1aa74b711e7baa720/adata/stock/market/stock_market/stock_market.py)、[股本/行业源码](https://github.com/1nchaos/adata/blob/8217a4ec593e7bf6c9cf32a1aa74b711e7baa720/adata/stock/info/stock_info.py)、[分红模块](https://github.com/1nchaos/adata/blob/8217a4ec593e7bf6c9cf32a1aa74b711e7baa720/adata/stock/market/stock_dividend.py)、[setup.py](https://github.com/1nchaos/adata/blob/8217a4ec593e7bf6c9cf32a1aa74b711e7baa720/setup.py)。曾尝试缺一层目录的 stock_market.py URL 得 404，后通过官方 tree 找到正确路径。
- [a-stock-data README](https://github.com/simonlin1212/a-stock-data/blob/f21534946c42be3ad048ac938eecb1bad84cb8ab/README.md)、[SKILL.md](https://github.com/simonlin1212/a-stock-data/blob/f21534946c42be3ad048ac938eecb1bad84cb8ab/SKILL.md)。其中“87 端点/34 来源”为仓库自述，不等于本研究逐个验证。README 自述 2026-09 TDX 公共服务器 K 线命令失效；本研究未连接 TDX TCP 验证，不据此判定项目本地盘后数据失效。其公告示例代码只取第一页；日期转换使用进程本地时区再截成日期；互动易某些错误被转成空数组，这些行为不应进入公共层契约。

## 实测 A：SZSE 官方单股公司资料

公开页面接口（网站内部接口，未找到承诺稳定的正式 API 契约）：

```http
GET https://www.szse.cn/api/report/index/companyGeneralization?random=0.123&secCode=000001
User-Agent: Mozilla/5.0
Referer: https://www.szse.cn/
```

2026-11-12 本次只查询平安银行 000001，HTTP 200、JSON `code:"0"`、`message:"成功"`，返回 `data` 对象、`cols` 字段中文解释、`plate:"XA"`。`random` 是此次请求参数，不作为业务字段或时间戳。

可复用字段与本次值：

| 源字段 | 建议 schema | 实测值 / 解释 |
| --- | --- | --- |
| agdm | symbol（加交易所 SZ） | `000001` |
| agjc / agdjc | name / short_name | 平安银行 |
| gsqc | company_name | 平安银行股份有限公司 |
| ywqc | english_name | Ping An Bank Co., Ltd. |
| agssrq | listed_on（date） | 1991-04-03 |
| sshymc | industry_name | 金融业；附 industry_standard/source，不与其他分类体系静默混合 |
| zcdz | registered_address | 广东省深圳市罗湖区深南东路5047号 |
| sheng / shi / dldq | province / city / region | 广东 / 深圳市 / 华南 |
| http | website | bank.pingan.com；原值无 scheme，原样存储并在展示层安全补全 |
| agzgb / agltgb | raw 股本字段 | `1,940,592` / `1,940,568`；响应 cols 未写单位，不贸然作为“股”入库；需另核网页单位后转换 |

分页：单股返回一个对象，无分页字段。时间：没有 `published_at`、`updated_at`、历史版本参数或变更游标。只能做“本次观察到的最新快照”；`listed_on` 是上市日期，不是公司资料版本日期。`observed_at/fetched_at` 必须写本次抓取时间。无历史证据时 `valid_from/available_at` 为 null；回测请求过去日期必须返回 `HistoricalSnapshotUnavailable`，不可静默拿今天资料补齐。

增量：只能低频按需查单股，对规范化内容做 hash，变化时存新观察版本。新版本的首次观察时间只能证明“此时已经见到”，无法追溯它此前首次公开的时刻。本次无凭据、无验证码绕过。

## 实测 B：CNINFO 官方公告检索、字段、分页、时间

### 单股身份查询

不要下载全市场映射，更不要根据代码硬编码 orgId：

```http
POST https://www.cninfo.com.cn/new/information/topSearch/query
Content-Type: application/x-www-form-urlencoded
Referer: https://www.cninfo.com.cn/

keyWord=000001&maxNum=1
```

本次 HTTP 200：

```json
[{"code":"000001","pinyin":"payh","sjstsBond":"false","category":"A股","type":"shj","delisted":"false","orgId":"gssz0000001","zwjc":"平安银行"}]
```

`delisted` 等为字符串，不能直接按语言 truthy 解释为布尔。检查 code 精确匹配与市场/类别；查询返回第一个候选不一定是目标证券。缓存 `(market, code)->orgId`；映射缺失返回 `SymbolMappingNotFound`，不能猜 orgId 后把错误映射的 0 条当真实无公告。这里只实测一个深圳 A 股，未声称沪京映射均验证。

### 日期窗与分页

```http
POST https://www.cninfo.com.cn/new/hisAnnouncement/query
Content-Type: application/x-www-form-urlencoded
User-Agent: Mozilla/5.0
Referer: https://www.cninfo.com.cn/

pageNum=1&pageSize=3&column=szse&tabName=fulltext&plate=&stock=000001%2Cgssz0000001&searchkey=&secid=&category=&trade=&seDate=2026-10-20~2026-10-27&sortName=time&sortType=desc&isHLtitle=false
```

本次响应 HTTP 200，`totalAnnouncement=3`、`totalRecordNum=3`、`totalpages=1`、`hasMore=false`，三条记录为：

| announcementId | announcementTitle | announcementTime | adjunctUrl |
| --- | --- | --- | --- |
| 1225620612 | 关联交易公告 | 1792771200000 | finalpage/2026-10-24/1225620612.PDF |
| 1225620611 | 董事会决议公告 | 1792771200000 | finalpage/2026-10-24/1225620611.PDF |
| 1225620609 | 2026年三季度报告 | 1792771200000 | finalpage/2026-10-24/1225620609.PDF |

原始记录另有 `secCode:"000001"`、`secName:"平安银行"`、`orgId:"gssz0000001"`、`adjunctType:"PDF"`、`adjunctSize`、`announcementType`（可能为 `||` 拼接多值）、`announcementTypeName:null`、`storageTime:null`、`announcementContent:""`。空正文说明检索响应不是公告全文。下载链接按 AKShare 对应静态站惯例拼 `https://static.cninfo.com.cn/` + adjunctUrl；本研究未下载 PDF 或验证其正文/响应头，不能将 URL 已构造写成 PDF 下载已验证。详情页可使用原始 ID 与 orgId 构造，保留源 ID 不依赖标题作主键。

额外只读分页验证：同一股票、同一短窗，`pageSize=2,pageNum=1` 得 IDs `1225620612/1225620611`、`hasMore=true`，但 `totalpages=1`；`pageNum=2,pageSize=2` 得 ID `1225620609`、`hasMore=false`，`totalpages` 仍 1。这说明小 pageSize 下不能单独相信 totalpages。源码常用默认 pageSize=30；生产优先保持源站默认 30，结合 hasMore、去重后的累计数与 totalAnnouncement 校验，设页数/条数上限和重复页检测。中途失败返回 incomplete/PartialResult，不能把前几页作为完整成功结果。这里没有测试最大 pageSize 或全市场分页。

第一次短窗 `2026-10-29~2026-10-31`（另带 plate=sz）HTTP 200，`totalAnnouncement=0, announcements:null, hasMore=false`；此为该请求的空结果，不是网络失败，也不足以由本次对比单独证明 plate 参数含义。后续成功请求使用 plate 留空并缩到另一个短窗。累计公告请求仅同一只股票、两个短窗及两页验证，未下载全市场映射或公告正文。

### 发布时间能力与 PIT 限制

`announcementTime` 是 Unix epoch 毫秒，样本 `1792771200000` 精确换算为 UTC `2026-10-23T16:00:00Z`、上海时区 `2026-10-24T00:00:00+08:00`。字段能装下时刻，并不证明它表达真实首次披露时刻；样本三条全是上海零点、storageTime 全为空，所以本次仅证实公告“日期”语义，未证实盘中/盘后精确发布时间。不可按 UTC 日期错误归到 10-23，也不可把 10-24 零点当当日开盘前已知。

AKShare 当前代码对该字段用 utc=True 后 tz_convert("Asia/Shanghai")，再去掉时区并转字符串；a-stock-data 示例则截成日期。公共层应同时保留原始毫秒、解释后的源日期、时区及 `publication_precision=date|datetime|unknown`，本次样本应标记 date。`first_seen_at` 为本系统首次观察时间，`fetched_at` 为本次抓取时间，与公告日期分别存储。

查询支持 `seDate` 日期范围和页码；本次未发现/验证按秒的 last_updated 游标或可靠服务端变更序号。增量应按日期窗口重叠抓取、以 `(provider, announcementId)` 去重、内容 hash 检测修订/撤回、只在全部页完成后推进 watermark。日期窗口只能作为查询条件，不能保证不会迟到补录。建议重叠 3–7 天是工程起点，非官方保证；定期人工/小范围对账。日级历史信号若要使用，应明确采用保守规则（例如公告日期之后的下一交易日才可用），标记该规则是推定而非已验证精确发布时间；严格盘中 PIT 或有同日时序要求时，应拒绝低精度记录或另购/建设可核验发布时刻档案。

## 其他资料接口实测与错误证据

| 候选 | 请求 / 本次结果 | 实现含义 |
| --- | --- | --- |
| SSE 官方公司概况候选 | `GET https://query.sse.com.cn/commonQuery.do?isPagination=false&sqlId=COMMON_SSE_CP_GPJCTPZ_GPLB_GPGK_GSGK_C&productid=600000`，Referer https://www.sse.com.cn/；HTTP 200，正文 `({"jsonCallBack":"null","success":"false","error":"System Error, Please try again later...","errorType":"ExceptionInterceptor"})` | 是业务失败且为括号包裹响应，不是成功 JSON 空资料。未证明是 SQL ID、参数或站点防护哪一种原因；暂不能列为已验证可用 provider |
| CNINFO 公司 webapi | `POST https://webapi.cninfo.com.cn/api/sysapi/p_sysapi1133?scode=000001` 空 body，无签名；HTTP 200，`{"resultmsg":"ApiFilter 未经授权的访问,code:003 token null ","resultcode":451}` | 451 是响应体业务码，不是 HTTP 状态。AKShare 源码使用 JS 计算 Accept-Enckey，本研究未运行签名或绕过；不能将“网页可看”写成匿名 API 无限制 |
| 东财 F10 公司概况补充 | `GET https://datacenter.eastmoney.com/securities/api/data/v1/get`，`reportName=RPT_F10_ORG_BASICINFO&columns=ALL&filter=(SECUCODE="000001.SZ")&pageNumber=1&pageSize=1&source=HSF10&client=PC`；HTTP 200，`success:true,code:0,message:"ok",result.pages:1,count:1` | 可作最新资料的非官方备源，不能替代官方公告/历史版本；只测一家公司 |

东财实测字段：`SECUCODE, SECURITY_CODE, SECURITY_NAME_ABBR, ORG_NAME, ORG_NAME_EN, ORG_CODE, FOUND_DATE, LISTING_DATE, ORG_WEB, ORG_EMAIL, ORG_TEL, REG_ADDRESS, ADDRESS, MAIN_BUSINESS, BUSINESS_SCOPE, CSRC_INDUSTRY_NAME, TRADE_MARKET`。另外 `ORG_PROFIE`（源键即此拼写）是长公司介绍，`ORG_PROFILE` 为另一简短描述，两者不要混淆。`MAX_DATE=2026-09-30 00:00:00` 是该响应中业务相关日期，不能当整份公司资料发布时间或历史版本生效时间；没有本次可验证的更新时间/增量游标。`result.pages/count/data` 是分页元数据，但单公司本次只有一条，未验证多页。生产应改为明确列白名单减少体积，保留原始 nullable 值，不将 `--` 解释为 0。

## 公共层可实现的最小契约

下面是研究建议，不是已改代码或既定迁移计划。

- `DailyBars(symbol, date_range, adjustment)`：明确 `adjustment=none/qfq/hfq`，原始成交量单位、价格精度、交易所、时区、source、抓取时间；未复权数据可用于撮合，qfq/hfq 必须独立查询/缓存键/存储口径。一个 provider 缺数据时，不能用另一种复权填洞。
- `QuoteSnapshot(symbol)`：`quote_time`（源时刻可空）、`observed_at`、`is_delayed/age`（无法判定为 unknown）、价格/成交量/成交额/状态；网站“实时”不能自动标成零延迟。
- `CompanyProfileSnapshot(symbol)`：`company_name, short_name, english_name?, listed_on?, industry{name,standard,source}?, registered_address?, website?, source, source_url, observed_at, content_hash, valid_from:null?, publication_precision:unknown`。避免为了所谓统一把所有来源的股本单位及行业口径抹平。
- `AnnouncementPage(symbol, date_range, cursor)`：`items, next_page?, total_reported?, complete, warnings`；item 保留 `source_id, symbol, org_id, title, category_raw, source_date, source_timestamp_raw, source_timestamp_unit, published_at?, publication_precision, first_seen_at, fetched_at, source_url, attachment_url?, content_hash?`。本次 CNINFO 样本 published_at 不应被解释成已知精确首次披露时刻。
- `CorporateAction`：另设 `event_type, announcement_date/available_at, record_date, ex_date, payment_date, cash/share_ratio, status(proposed/approved/implemented), source`。qfq 因子不能替代分红/配股/送转动作；公司股本“变更日期”不等于公告可见日期。
- 能力表按 provider 与市场声明 `daily/quote/profile/announcement/corporate_action/historical_as_of`；SZSE 单股资料只先宣称 SZ 支持，不因 symbol 统一就宣称沪京全覆盖。

错误语义建议：

| 情况 | 建议语义 | 可否返回空列表 |
| --- | --- | --- |
| HTTP 200 且字段合法、total=0 | NoData，附请求范围/source | 可以；例如 CNINFO announcements:null + total=0 |
| HTTP 200 且业务 success=false / resultcode 非成功 | UpstreamBusinessError，带 HTTP 与业务码、短错误摘要 | 不可以；SSE/CNINFO webapi 本次属于此类 |
| 401/403 或认证墙 | AuthRequired / AccessDenied | 不可以，不自动更换身份/绕过 |
| 429 | RateLimited，尊重 Retry-After | 不可以；本次未遇到 429，不能声称已测限流阈值 |
| 超时/连接/TLS 错误/5xx | TransportError/Unavailable，可有限重试 | 不可以 |
| JSON/JSONP/字段形态变化、code 不匹配 | DecodeError/SchemaChanged/SymbolMismatch | 不可以；HTML 验证码页也不是 NoData |
| 未知 orgId | SymbolMappingNotFound | 不可以 |
| 翻页中断/重复页/总数不一致 | PartialResult/Incomplete，禁止推进完整性水位 | 不可伪装完整 |
| 未提供历史版本/精确时刻 | Unsupported / HistoricalSnapshotUnavailable / PublicationTimeUnknown | 不能降级为当前快照或猜时刻 |

缓存/访问建议：同主机低并发、去重合并相同请求；公司资料日级甚至更长 TTL，公告小窗口增量；429/5xx/网络错误有限指数退避加抖动，认证失败停止。初始可设每 host 并发 1、每秒至多 1 请求，这是本系统保守配置建议，**不是查到的官方限额或授权保证**。不绕过验证码、封禁、认证，不下载全市场来满足单股按需需求。跨库降级时仍要识别底层相同的 eastmoney 域名来源，避免把多个包装库误当独立容灾。

## 时间与复权：必须延续现有约束

本次只读看到 v2/src/domain/market.rs 明确仅 `adjust="none"` 可进入对应路径，v2/src/store/mod.rs 注释拒绝 walk-forward 中的 qfq，且保留 source/adjust 元数据。公共层应延续这个边界，不因统一 API 放宽。

- efinance `get_quote_history` 默认 `fqt=1`（前复权），0 为不复权，2 为后复权；调用者必须显式传 0 并验证输出口径。
- adata `get_market` 默认 `adjust_type=1`；源码 docstring 虽列 0/1/2，又写“目前：只有前复权”，因此不能只传 0 就宣称该源已支持未复权。必须按实际后端和样本核验，未通过时能力应禁用。
- qfq 值会因后续公司行为及锚点重算，不是当时撮合价格。即便统一 schema，也必须将 factor version/as_of、source、adjustment 纳入缓存与血缘；不拿今天 qfq 数据替代历史 raw 数据。
- 公司资料、行业、ST、股本、财务报表、公告分别有不同的观察日/业务日/发布日/修订日。抓取当天看到的内容不能直接用于过去的选股/回测；报告期也不等于可见日期。今天首次抓到历史公告只能证明今天可访问，不能证明其精确首次公开时刻。

## Tushare 与 Qlib：边界参考

[Tushare daily 官方文档](https://tushare.pro/document/2?doc_id=27)：未复权日线，停牌无记录，交易日 15–16 点入库；当前文档称最低 120 积分可试用、每分钟 50 次。不能把它和 daily_basic 的权限混为一谈。

[Tushare stock_basic](https://tushare.pro/document/2?doc_id=25)：2000 积分起、每分钟 50 次、单次最多 6000 行；有上市/退市日期和状态，当前列表不是历史时点股票池。

[Tushare stock_company](https://tushare.pro/document/2?doc_id=112)：至少 120 积分，单次 4500 条，可按 ts_code/exchange；字段含公司名称、统一信用代码、成立日、行业/地址/官网/经营范围相关信息。权限随平台政策可能变化，实际接入以账户权限为准。

[Tushare anns_d](https://tushare.pro/document/2?doc_id=176)：官方文档称始于 1999-01-01、每 2 小时更新，单次最多 2000 条，公告数据需单独开通权限；输入 ts_code/ann_date/start_date/end_date，输出 ann_date/ts_code/name/title/url，另有可选 `rec_time datetime`，文档标为“发布时间”。本任务无 token、未请求该 API，未验证 rec_time 精度、时区、完整性或修订语义。若业务必须精确发布时刻，可作为后续付费/授权验证候选，而非目前已经满足的 PIT 数据。

Tushare 是有权限的数据服务，不能因 [开源客户端 BSD-3-Clause](https://github.com/waditu/tushare/blob/master/LICENSE) 就认定 Pro 数据免费无限/可商用转售；旧客户端仓库最后提交日期也不能用来推断 Pro 服务停止维护。

[Qlib README](https://github.com/microsoft/qlib/blob/main/README.md) 与 [MIT LICENSE](https://github.com/microsoft/qlib/blob/main/LICENSE) 本次可读，README 有 Point-in-Time 数据库能力入口，同时提醒官方数据集暂时不可用、Yahoo 采集数据可能不完美；GitHub 元数据补查曾发生 TLS EOF，未据此报 Qlib 最新提交日。Qlib 是研究训练/回测框架和存储表达参考，不能代替官方资料/公告 provider，也不能自动补齐历史时点可见性。

## 代码许可与数据使用权

MIT / Apache-2.0 / BSD-3-Clause 管的是相应开源代码；复制源码须保留相应版权、许可证、必要 notice/变更说明，Apache 还有其条款要求。它们不替 CNINFO/SSE/SZSE/东财/腾讯/TDX 授予行情数据、公告正文、PDF、数据库或再分发权。

efinance 官方文档及 mootdx README 都另写学习交流/不得商业用途，与宽松代码 LICENSE 并存；本研究如实列示，不据此作法律效力裁决。AKShare 的 acknowledgements 页面本次 WebFetch 失败、直接请求 HTTP 403，故没有把未读到的具体免责声明条文当已核验事实。公开可访问、免 API key、一次请求成功，均不构成商用/批量抓取/缓存再发布/转售授权；后续若产品商业化或对外转发原始资料，应按实际供应商/网站条款获得相应授权。当前研究未查到上述网站为这些内部页面接口公布的固定速率和 SLA，因此不可编造“官方每秒 N 次”。

## 研究限制及交接

完成：五仓库身份/当前默认分支提交/实际 LICENSE/依赖与目标能力核验；官方 AKShare/efinance/Tushare 文档；SZSE 单股资料、CNINFO 单股身份和短窗公告及两页验证；东财单股公司资料补充；SSE/CNINFO webapi 业务失败被显式记录。

未完成且未声称完成：沪京全市场资料与公告覆盖、TDX 公共 TCP 当前可用性、公告 PDF 正文下载、全部历史分页/迟到更正、精确首次发布时间、API 最大分页限制、官方限流阈值、数据商用授权、公司行为全类型/PIT 档案。WebSearch 已实际调用：首个多仓库混合查询返回泛 GitHub 结果、CNINFO 查询因搜索引擎超时失败；后续直接 GitHub API/raw 与官方文档完成主体证据。Tushare 定向搜索成功且已用官方页面交叉核对。未把搜索失败、HTTP 200 业务失败或源码包装能力当联网成功。

给实现方下一步：把 `CompanyProfileSnapshot(SZSE)` 与 `AnnouncementPage(CNINFO)` 做成独立可选 provider，先覆盖单股按需与明确错误语义；维持 TDX/腾讯行情主源及 raw/qfq 隔离。首批验证应用已有测试设施覆盖业务错误≠空结果、CNINFO null 空页/分页元数据异常、上海零点日期精度、orgId 不可猜测、当前快照不可冒充历史版本。是否扩到沪市官方资料及授权精确公告时刻，留作后续范围/成本决定；不阻塞本轮公共层契约。

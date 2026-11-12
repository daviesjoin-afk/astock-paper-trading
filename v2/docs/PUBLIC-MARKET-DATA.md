# 公共行情、公司资料与公告数据层

更新：2026-11-12。选型证据见 [开源项目与来源研究](MARKET-SOURCES-RESEARCH-2026-11-12.md)。现有 TDX/腾讯行情入口继续使用；参考 AKShare 等项目的接口与字段经验，独立实现小型适配器，没有整体引入 AKShare、efinance、adata、mootdx、Qlib 或新增服务。

## 入口与能力

公共入口是 `data_sources/public_data.py` 的 `PublicDataClient` 和 CLI，均可直接使用默认提供者，无需编写 hook。

| 数据 | 默认来源 | 已验证及边界 |
| --- | --- | --- |
| 日线 `bars` | TDX 官方盘后包；可显式选 `local` 只读库 | 不复权、整数分/股，保留停牌 `tradable=false`；全市场日包被不同标的请求复用。远端多日必须显式给交易日列表 |
| 报价 `quotes` | 腾讯 | 批量标的，源报价时刻和抓取时刻分开；缓存最多 30 秒；属于网站快照，不承诺交易级实时性 |
| 公司资料 `company` | SZ 选深交所；SH/BJ 选东财 | 深交所仅 SZ；东财为明确标记的非官方公司资料快照，可显式选。沪深单股已联网验证；BJ 路由只有离线覆盖，源无资料时仍明确失败 |
| 公告 `announcements` | 巨潮 CNINFO | 按单股、日期范围完整分页，核对官方组织身份、累计唯一 ID 与总数；本轮深圳单股两页实测。沪京仅 fixture 覆盖 |

仅接受显式 `000001.SZ` / `600000.SH` / `920001.BJ` 等普通 A 股标识。未知市场、来源或复权方式明确拒绝。默认选源是预先声明的市场路由，某源失败后不会自动换源、改价格或把错误当无数据。

## 所有者与调用关系

| 所有者 | 责任 | 消费者 |
| --- | --- | --- |
| `public_data.py` | 查询、标的/单位/时间/来源/完整性语义及明确能力路由 | 研究脚本与命令行调用者 |
| `source_http.py` | 有界 HTTP、语义校验后缓存、SHA256/来源/时间校验、原子文件替换 | 公共客户端及三个既有取数器 |
| `company_disclosures.py` | 深交所/东财公司资料、巨潮身份/公告解析与分页；注入 fetch | 公共客户端 |
| `ingest_market.py` / `a_stock_data.py` / `realtime_quotes.py` | 保留原摄取、双源审核、报价职责，复用共享传输 | 原有离线采集流程 |
| Rust `store/` | 原有账本、事务、幂等与结算 | Rust HTTP 与离线回放 |

Rust 核心不联网，domain 不做文件/网络/数据库访问。公共缓存是源响应/查询快照，不是账户账本。公司资料和公告尚未自动参与策略评分、公司行为入账或订单执行。

## 命令行

以下在仓库根执行。缓存必须显式启用；`data_cache/` 不进 Git。

```bash
# 同一日期日包在不同股票、不同采集脚本间可复用
python v2/data_sources/public_data.py bars --symbol 600000.SH \
  --start-day 2026-11-11 --end-day 2026-11-11 --cache-dir data_cache/public

# 跨周末不猜交易日；清单严格升序、唯一、在起止范围内
python v2/data_sources/public_data.py bars --symbol 600000.SH \
  --start-day 2026-11-06 --end-day 2026-11-09 \
  --days 2026-11-06,2026-11-09 --cache-dir data_cache/public

# 只读已经存在的 bar 表；不会创建库、迁移列或读取账本表
python v2/data_sources/public_data.py bars --source local \
  --bar-db work/real-history/db/research-market-halt-boundary.sqlite3 \
  --symbol 000001.SZ --start-day 2026-11-10 --end-day 2026-11-11

python v2/data_sources/public_data.py quotes --symbol 600000.SH,000001.SZ \
  --cache-dir data_cache/public

python v2/data_sources/public_data.py company --symbol 000001.SZ \
  --cache-dir data_cache/public
python v2/data_sources/public_data.py company --symbol 600000.SH \
  --source eastmoney --cache-dir data_cache/public

python v2/data_sources/public_data.py announcements --symbol 000001.SZ \
  --start-day 2026-10-20 --end-day 2026-10-27 --cache-dir data_cache/public
```

成功返回 `status=ok` JSON。公司和公告记录位于 `data.rows`；行情记录位于 `rows`。运行/来源错误返回 `status=error`、明确 `code`、退出码 2；参数解析错误由 argparse 报错。旧库日期范围只提供已有行，缺口标为 `partial` 和 `missing_calendar_days`，不会把自然日缺口猜成交易日缺失，也不会前填；调用者需要严格完整性时应传入明确 `days` 并检查状态。只查询 `adjust=none`；同表的合法 qfq 行不会混入，多个未复权来源在同日冲突则拒绝。

## Python 按需调用

从仓库根运行 Python：

```python
from v2.data_sources.public_data import PublicDataClient
from v2.data_sources.source_http import SourceHttp

client = PublicDataClient(SourceHttp("data_cache/public", ttl=86400))
company = client.company("000001.SZ")
announcements = client.announcements("000001.SZ", "2026-10-20", "2026-10-27")
quotes = client.quotes(["000001.SZ", "600000.SH"])
```

已有 `ingest_market.py`、`a_stock_data.py fetch` 也接受 `--cache-dir`、`--cache-ttl`、`--offline`、`--as-of`。报价函数可注入同一个 `SourceHttp`。缓存身份包含方法、URL、请求体、来源、复权口径、标的和日期；TDX 身份按整日包定义，公司/公告结果还区分适配器版本。有限网络重试不重试认证错误，429 尊重可接受的 Retry-After。HTTP 200 的业务失败不是空数据；不完整公告不会留下成功快照。原双源审核和人工 `reviewed` 发布门禁保持。

## 时间、缓存与历史使用

- `observed_at` / `quote_time` 描述市场观察；TDX 日线的 15:00 是规范化收盘时刻，不是下载或首次发布时刻。
- `fetched_at` 是实际抓取时间；旧本地 bar 缺此证据，返回 null，并另给 `read_at`。不能把读取时刻冒充旧数据首次可见时刻。
- 公司名称、行业、上市日期、地址等是最新资料快照；上市日、成立日和源 `MAX_DATE` 均不代表资料版本发布时间。
- 巨潮保留原始毫秒和上海日期，但 `publication_precision=date`、`published_at=null`；样本的上海零点不能被解释成开盘前已知。附件仅提供官方 HTTPS 链接，本轮不下载或分析 PDF。
- `--as-of` 只允许命中当时已冻结且满足 TTL 的快照；过去未抓取、已过期或已被新快照覆盖均明确失败，不会联网抓今天内容补过去。本地旧 bar 不支持此严格历史查询。
- 当前每个请求只保留最新缓存，不是多版本 PIT 档案。无状态适配器的 `first_seen_at` 为本次观察；后续持久化档案必须按 ID/修订保留最早观察与版本可见时间。

因此，这一层可以统一按需取用当前资料和已有行情，但不代表完整历史基本面、精确公告披露时刻、财务 PIT、公司行为权益和分钟/tick 全覆盖。这些能力需要独立来源与验收，不从 qfq 因子或一条公告标题推断。

## 验证

离线回归包括真实消费者共享日包、身份隔离、缓存损坏/过期/离线、失败不缓存、30 秒报价边界、raw/qfq 隔离、只读库与明确缺口、公司标的/字段/错误、公告完整分页及低精度时间。源站小样本已验证深交所平安银行资料、东财浦发银行资料和巨潮三条公告；不据单股成功声称全市场可用。

```bash
python -B -m unittest discover -s v2/data_sources -p 'test_*.py'
python -B -m unittest discover -s v2/research -p 'test_*.py'
```

源码许可与数据来源/使用权分别核实，详情见研究报告。缓存和源站请求均不是自动审核、生产发布或实盘执行授权。

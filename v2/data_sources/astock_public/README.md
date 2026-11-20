# AStock 公共数据适配器

接入完成核对：2026-11-15。原则是先完整保留上游能力，再由选股、研究、回测、监控和 LLM 按用途取数。

## 统一入口

项目使用 `public_data.PublicDataClient`，现有 bars、quotes、company、announcements 路径继续工作。新增 datasets()/dataset() 调用本包；Rust 核心继续消费既有数据契约，不直接联网。

在仓库根执行：

```bash
python v2/data_sources/public_data.py datasets
python v2/data_sources/public_data.py datasets --category 宏观与利率层
python v2/data_sources/public_data.py dataset --name cls_telegraph --params '{"page_size":3}'
python v2/data_sources/public_data.py dataset --name repo_fixing_rates --params '{"kind":"FR"}'
```

Python：

```python
from v2.data_sources.public_data import PublicDataClient
from v2.data_sources.source_http import SourceHttp

client = PublicDataClient(SourceHttp("data_cache/public"))
catalog = client.datasets()
flows = client.dataset("stock_fund_flow_120d", code="600519")
news = client.dataset("eastmoney_stock_news", code="600519", page_size=12)
valuation = client.dataset("valuation.forward_pe", price=100, eps_forecast=5)
```

目录同时提供能力 ID、原函数名别名与参数清单。别名不是新数据端点；不要拿目录行数当独立来源数。旧四类接口的参数与各源原函数参数不同，取数前使用目录中 parameters。

## 覆盖与计数

固定上游为 simonlin1212/a-stock-data v3.10.4，提交 `f21534946c42be3ad048ac938eecb1bad84cb8ab`。91 个 Python 代码块、182 次顶层函数定义、181 个不同函数名全部保留并检查。104 个公开名字包含辅助工具，不能计为 104 个远程数据端点。

注册表对应上游 15 类、34 个来源/服务标识、88 项能力清单。按上游去重规则计算为 87 个端点（82 主项、5 备用）；同一公司旗下不同服务不等于独立供应商。能力覆盖行情/复权、研报、信号、资金与筹码、新闻、基础与公告、涨停异动、ETF 期权、互动舆情、宏观利率、指数与日历、期货商品、公司事件、可转债与备用源。

旧 TCP 接口的财务、F10、K 线/报价/成交查询会真正调用对应客户端方法并释放连接；原生客户端工具仍可通过低层 `PublicClient.call("tdx_client", ...)` 使用，其生命周期由调用者负责。TCP 行情在上游有失效记录；本机缺少可选 mootdx，因此只完成假连接行为验证，没有宣称公网可用。

## 查询结果与时间

公共 SDK 返回 status、rows、data、source、temporal、provenance、fetched_at 与缓存摘要。保留原生字段单位和复权语义；既有 bars 仍是整数分/股、不复权。资金流面向 LLM 时额外转换为整数分，保留原始元值并明确是供应商统计；筹码分布是本地估算。

当前/当日快照缓存最多 30 秒，历史数据沿用调用者 TTL。缓存只有最新版本，不是历史版本档案。as_of 只能读取当时已冻结且未过期的结果；历史未抓取时拒绝联网补造。财报报告期、行业生效日不等于首次可知时间。

上游的网络失败打印、缺失字段和空表不能当成功。公共门面捕获警告，失败/未确认空结果/不完整结果不进入成功缓存；缺失分档资金流 `-` 修正为 null，不能变成 0。部分成功和失败均带明确信息，调用者可以继续处理其它标的或任务。

## 安全复用与边界

本包从校验过 SHA-256 的发布副本抽取定义与静态常量，禁止执行顶层演示、登录、取数或文件写入。原始上游摘要与本地发布副本摘要分别记录；副本只清理联系方式、固定 TCP 地址，并将模拟网址改为保留示例域名。默认 TCP peer 由 mootdx 发现；显式覆盖可用 ASTOCK_TDX_SERVERS 的 JSON `[ [host, port] ]`，错误配置在连接前拒绝。HTTP 注入项目 SourceHttp；原始未验证响应不被公共门面写入成功缓存。HTTP 状态码和异常类别保留。模块注入的装载阶段有进程内锁，避免并发客户端混入别人的传输。

HTTP 与供应商 TCP 协议是两种路径；BaoStock/mootdx 的会话、可达性不等同于 SourceHttp 的 HTTP 超时/缓存。问财需要用户配置自身服务地址和凭据，目录只报告配置状态；北交所匿名会话能否建立依赖源站运行状态。不要由一次 timeout/501 断言源永久不可用。

## 每日消费

llm_daily.py 已消费真实新闻和分档资金流。auto 默认仅对当天报告启用；最多 2 个报告相关标的、4 次数据查询，模型调用仍受原预算约束。历史报告默认不联网抓当前快照，显式 historical 使用日期窗口和冻结 as_of。可用 `--public-data-cache-dir` 或 `ASTOCK_PUBLIC_DATA_CACHE_DIR` 指定缓存；当时未冻结的历史不能在今天重建。

数据采集、离线行为验证和外网源可用性分别记录。联网样本与计时见 `v2/docs/assets/public-data-live-smoke-2026-11-14.json`；其中包含初次参数错误及按源码签名修正后的记录，不能把脚本 exit 0 当作源站通过。

## 更新方式

`_upstream/` 是带明确修改记录的发布副本，保留许可证、测试与复权夹具，运行时不拉取最新代码。固定版本可以从已有原始 checkout 重建并逐文件比较（以下在仓库根执行）：

```bash
# 默认只读比较，所有派生文件一致才退出 0
python v2/data_sources/astock_public/refresh_upstream.py --source-root work/ref-a-stock-data
# 显式写入一个全新的目录供审查，不覆盖当前组件
python v2/data_sources/astock_public/refresh_upstream.py --source-root work/ref-a-stock-data --output-root work/public-data-overlay-new
```

脚本严格核验原始上游摘要，转换时只解析 Python 语法、不执行示例；原始/派生摘要、区块及全部函数名均核对。仓库根目录的 `scripts/update_astock_public_snapshot.py <trusted-checkout>` 使用同一转换刷新包内文件，并在输出中把原始摘要、发布副本摘要和区块形状分开列出；维护者还要按输出更新 loader 常量。两条路径都只重建当前固定版本；升级新提交不能跳过输入摘要保护。升级时：

1. 在已有参考 checkout 中审查新提交的 SKILL.md、测试及许可差异，核对新增/删除/改参的能力。
2. 换入确认过的上游文件，更新 source_snapshot.py 的版本、提交、SHA-256、行数/代码块与定义计数；同时调整 registry_data.py 的能力、参数和边界。
3. 复核 AST 过滤和必要常量，保留本地缺失值、HTTP、可选依赖与连接生命周期修正；检查本地 modifications/THIRD_PARTY_NOTICES。
4. 跑公共数据、每日消费和研究回归，按需小样本联网；通过后更新执行记录。源码校验失败必须停在失败状态，不能临时跳过摘要检查。

本轮精确区块/函数映射在 `v2/docs/assets/public-data-source-inventory-2026-11-14.json`；代码许可为 Apache-2.0，完整许可与改动说明见 `_upstream/LICENSE` 和 `_upstream/THIRD_PARTY_NOTICES.md`。

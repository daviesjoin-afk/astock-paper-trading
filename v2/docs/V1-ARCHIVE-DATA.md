# V1 归档数据盘点（2026-11-11）

服务器上 `/opt/astock-v1-archive-2026-11-07/` 是 16 GB 的 V1 全量归档。
本文记录里面有什么、哪些能用在 V3、以及必须注意的陷阱。

---

## 1. 归档总览

| 项目 | 值 |
| --- | --- |
| 路径 | `/opt/astock-v1-archive-2026-11-07/` |
| 总大小 | 16 GB |
| 主交易库 | `db/paper_trading.sqlite3` — 2.9 GB，85 张表 |
| 学习库 | `db/adaptive_learning.sqlite3` — 1.1 GB |
| K 线数据 | `source/data_cache/klines/` — 357 MB，5574 个 CSV |
| 源代码 | `source/backend/` — 778 个 .py |

**已下载到本地**：`.data/v1-klines/`（360 MB，5574 CSV + manifest），
`.gitignore` 已排除 `.data/`，不进 Git。

---

## 2. K 线数据（最有价值的部分）

### 2.1 内容

| 项目 | 值 |
| --- | --- |
| 文件数 | 5573 只有效（另 1 个是 `BENCH_000300` 基准指数） |
| 时间范围 | **2023-01-03 → 2026-11-06** |
| 总行数 | **4,916,688**（491 万） |
| 中位长度 | 931 个交易日 |
| 字段 | `date,open,close,high,low,volume,amount,amplitude` |
| 缺失 | 无 `prev_close`，需从上一行推导 |

### 2.2 数据质量（抽样 100 只、约 9300 行）

| 检查 | 结果 |
| --- | --- |
| 空值 / 不可解析 | 0 |
| 非正价格 | 0 |
| OHLC 逻辑矛盾（high<low 等） | 0 |

数据本身干净。**但干净不等于可用。**

### 2.3 ⚠️ 陷阱：这是前复权（qfq）数据

`kline_manifest.json` 明确记录：

```
"source": "eastmoney", "adjustment": "qfq"   ← 5568 只
"adjustment": "none"                          ← 5 只
"adjustment": "unknown"                       ← 2 只
```

**前复权意味着什么**：它用「后来发生的分红送股」去重写「历史价格」。
比如 2024 年的价格，是用 2026 年为止的所有除权事件调整过的。

**对回测的后果**：

- 你看到的 2024 年价格，是**2026 年才能算出来的**价格
- 用这个序列做 walk-forward，等于站在未来看过去 —— 典型未来函数
- 更隐蔽的是：它不会报错，只会让你的回测**显得比实际更好**

**证据**：600036 有 850/930 行超过两位小数，起始价 `29.854`。
真实股票报价只有两位小数，三位小数只可能来自复权计算。

### 2.4 正确用法

| 场景 | 能用吗 | 说明 |
| --- | --- | --- |
| 画 K 线图、给用户看历史走势 | ✅ 可以 | 前复权图对看盘反而更好（除权不跳空） |
| 计算形态类因子（均线/动量/突破） | ⚠️ 谨慎 | 形态本身连续，但幅度被历史除权扭曲 |
| 跨股票因子排序（如动量排名） | ❌ 不要 | 复权口径不一致会污染横截面排序 |
| 严格 walk-forward 回测 | ❌ **绝对不要** | 未来函数，回测结果全部作废 |
| 验证「信号是否能预测未来」 | ❌ 不要 | 同上 |

**要做严格回测，必须用不复权数据 + 复权因子自己算。**
通达信盘后包（`ingest_market.py`）给的是不复权原始价，那条路才是对的。

### 2.5 代码里已经做了的事

`bar` 表加了 `adjust` 和 `source` 两列：

- 从 V1 导入的行：`adjust='qfq'`, `source='v1-qfq'`
- 从通达信摄入的行：`adjust='none'`, `source='tdx'`

这样任何策略代码都可以在入口拒绝 qfq 数据：

```sql
SELECT ... FROM bar WHERE symbol = ? AND adjust = 'none'
```

**永远不会把两种口径混进同一条时间序列。**

---

## 3. V1 交易记录（样本量小，但有参考价值）

### 3.1 规模

| 表 | 行数 | 有用吗 |
| --- | --- | --- |
| `paper_fills` | 52 | ✅ 真实成交，含费用 |
| `paper_orders` | 881 | 部分有用（含拒单原因） |
| `paper_nav` | 53 | ✅ 净值曲线 |
| `paper_signals` | 821 | ⚠️ 信号记录，看策略逻辑 |
| `paper_risk_decisions` | 大量 | ❌ 296 MB 审计日志 |
| `paper_positions` | **0** | 已清空 |

### 3.2 五个内置策略的实际战绩

区间约 10 个交易日（2026-10-26 → 2026-11-06），本金各 6-15 万：

| 策略 | 起点 | 终点 | 收益 | 最大回撤 |
| --- | --- | --- | --- | --- |
| `main_force_top10` 主力强攻 | 60,000 | 60,000 | **0.00%** | 0.00% |
| `sector_rotation` 板块轮动 | 60,000 | 60,000 | **0.00%** | 0.00% |
| `tq_breakout` 短线日内做T | 60,000 | 60,493 | **+0.82%** | 3.31% |
| `reported_profit_breakout` 业绩爆发 | 90,000 | 88,539 | **-1.62%** | 1.62% |
| `trend_pullback` 趋势波段优选 | 150,000 | 147,841 | **-1.44%** | 6.91% |

**怎么读这组数字**：

1. **样本太小，不能下任何结论**。10 个交易日、52 笔成交，
   统计上跟抛硬币没有区别。
2. 两个 0.00% 的策略说明**根本没交易** —— 不是策略好，是没触发。
3. 唯一正收益的 `tq_breakout` 最大回撤 3.31%，比收益还大 4 倍。
   这是纯粹的运气 + 波动，不是 alpha。
4. `trend_pullback` 本金最大（15 万），亏得也最多（-2,159）。

**这正是我在方案 11.2 节说的：看曲线不能只看方向。**
如果只看「tq_breakout 是唯一赚钱的」，就会得出一条完全错误的结论。

### 3.3 策略定义

5 个都是 `builtin`，`lifecycle_status: active`，无 DSL AST（代码硬编码）：

- `tq_breakout` — 短线日内做T（动量 · 资金 · 量能）
- `trend_pullback` — 趋势波段优选（价值 · 质量 · 回踩）
- `sector_rotation` — 板块轮动先锋（情绪 · 资金 · 动量）
- `reported_profit_breakout` — 业绩爆发
- `main_force_top10` — 主力强攻

风控参数（从成交记录 JSON 里提取）：

```json
{
  "evolvable_params": {
    "entry_score": 0.78, "confirmation_window": 2,
    "hard_stop": -0.04, "trailing_stop": 0.04,
    "trailing_trigger": 0.03, "holding_days": 5
  },
  "soft_limits": {
    "max_exposure": 0.65, "max_industry": 0.30,
    "max_positions": 3, "max_weight": 0.22,
    "risk_per_trade": 0.008
  }
}
```

这组参数是 V1 长期迭代后的结果，可以直接作为 V3 策略的**初始默认值**参考。

---

## 4. V1 因子代码（值得借鉴）

`source/backend/factors.py`（624 行）里有三块值得抄：

### 4.1 PIT 安全校验（`_adjustment_pit_safe`）

V1 自己就意识到了前复权的问题，写得比我预想的严谨：

```python
def _adjustment_pit_safe(meta, asof_moment):
    """该股票的价格序列在 asof 时点是否被证明是 PIT-safe 的。
    * 不复权（adjustment == "none"）：原始 OHLCV 不会被公司行为重写 → safe
    * 前复权（qfq）：qfq 是按"抓取时已知的全部公司行为"算出来的，
      只有在证明抓取时间（manifest 的 updated_at）早于 asof 时才算当时可用的同样调整
      —— 无法证明 → unsafe
    """
```

**这条判据是对的，V3 应该直接继承这个思路。**

### 4.2 因子分类

`compute_price_factors` / `compute_fundamental_factors` / `compute_sentiment_factors`
三层划分清楚，配合 `zscore`（截断到 ±3，防极值）和 `pit_provenance`（记录来源）。

### 4.3 涨停判断

`limit_up_threshold(code)` / `find_first_board_candidates(klines)`
—— 按代码段区分主板/创业板/科创板/北交所的涨跌停幅度。
这个逻辑在 V3 的风控门禁里要用到。

---

## 5. 结论：这批数据怎么用

### 立刻能用

1. **`bar` 表灌入 491 万行历史**（已完成）
   - 用途：给前端画 K 线、算形态、看历史
   - 限制：标注 `adjust='qfq'`，严格回测必须过滤

2. **V1 的 5 个策略参数作为初始默认值**
   - `hard_stop=-4%`, `trailing_stop=4%`, `max_weight=22%` 等
   - 已被 V1 实战跑过，比拍脑袋的默认值强

3. **`factors.py` 的三个思路**
   - PIT 安全校验逻辑直接继承
   - zscore 截断
   - 分板块涨跌停阈值

### 不能直接用

1. **52 笔成交** — 样本太小，只能当「格式参考」，不能当绩效基准
2. **296 MB 的 `paper_risk_decisions`** — 纯审计日志，无分析价值
3. **前复权数据做严格回测** — 会得出错误的乐观结论

### 需要补的

要做真正的 V3 回测，缺的是 **2023-2026 的不复权日线**。
通达信盘后包一天一个包，补 930 个交易日需要跑 930 次（约 2.3 小时）。
这个可以后台批量跑，`ingest_market.py --days` 已经支持区间补数。

---

## 6. 复现步骤

```bash
# 服务器：打包（已完成，/tmp/klines.tar.gz 141 MB）
sudo tar -czf /tmp/klines.tar.gz \
  -C /opt/astock-v1-archive-2026-11-07/source/data_cache \
  klines kline_manifest.json

# 本地：解压到 .data/v1-klines/
tar -xzf klines.tar.gz -C .data/v1-klines

# 本地：导入 bar 表（约 2 分钟，491 万行）
python v2/data_sources/import_v1_klines.py \
  --db .data/v2bars.sqlite3 \
  --dir .data/v1-klines

# 验证
sqlite3 .data/v2bars.sqlite3 \
  "SELECT count(*), count(DISTINCT symbol), min(day), max(day) FROM bar;"
# → 4916688 | 5573 | 2023-01-03 | 2026-11-06
```

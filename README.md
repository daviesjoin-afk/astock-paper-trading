# AStock V2 — A 股模拟盘（paper-only）

> 📍 **[路线图 →](ROADMAP.md)** — 做到哪了、下一步做什么、当前的坑在哪
> 📊 当前状态：全市场 **5980 只**股票、**933 个交易日**（2023-01-03 起）、纯模拟不接券商

本仓库只有一个应用：`v2/` 下的 AStock V2。旧的 V1 应用已从当前分支移除，
原始代码由 Git 标签保留（见 [V1 退役记录](docs/V1_RETIREMENT.md)）。

## 它是什么

一个账户、一笔本金，系统自动分给「稳健」和「激进」两个策略，按真实收盘价逐日结算，
根据两个策略的相对表现自动调整资金分配。

- **全市场**：通达信盘后包一次拿沪深北 5576 只当日 OHLCV，9 秒
- **收盘价驱动**：T+1、100 股整手、固定费率，不做盘中撮合
- **自进化**：按「收益 − 回撤」评分在 20%–80% 边界内调整两槽预算，不转移已持仓股票
- **多级控制台**：3 模块 / 9 视图，含净值曲线（账户 + 两槽可切）

**不接券商、不接实盘、不打真实资金。**

## 它不是什么

- ❌ 不是能直接上线的公网服务（**没有鉴权**，8600 端口完全公开）
- ❌ 不是日频因子挖掘平台（公开因子的 alpha 早被套利完了）
- ❌ 不是回测框架的替代品（前复权数据不能做 walk-forward，见 `adjust` 标记）

## 快速开始

```bash
cd v2
cargo test --locked                 # Rust 内核测试
cargo build --release --locked
cargo run --locked                  # 本地 HTTP 网关，仅 127.0.0.1:8765

# 另一个终端（仓库根目录）
python3 -m unittest discover -s v2/research -p 'test_*.py' -v   # 研究契约与重做测试
bash v2/scripts/smoke.sh                                        # Rust + Python + HTTP/SQLite 集成烟测
```

`v2/scripts/smoke.sh` 会启动真实服务、跑完整决策与回放、重启进程后再核对一次，然后退出。

## 目录

```text
v2/                    唯一应用
  src/                 Rust 内核：domain（纯规则）、store（唯一写入口）、main（HTTP）
  research/            Python 标准库研究生产者与研究契约测试
  scripts/             演示与端到端烟测
  docs/                ADR、模块边界与运行规则说明
  web/                 TypeScript 只读面板（Vite）
scripts/security/      敏感数据扫描器（CI 与本地 pre-push 共用）
.github/workflows/     v2.yml、security-leak-scan.yml
docs/V1_RETIREMENT.md  V1 退役记录、归档标签与恢复方法
```

## 文档入口

- V2 总览与数据边界：[v2/README.md](v2/README.md)
- 模块边界：[v2/docs/MODULE_BOUNDARIES.md](v2/docs/MODULE_BOUNDARIES.md)
- 决策记录：ADR 001（独立实验）、ADR 002（固定数据包）、ADR 003（可核验策略工件与冻结研究输入）见 [v2/docs](v2/docs)
- 退役与恢复：[docs/V1_RETIREMENT.md](docs/V1_RETIREMENT.md)
- 安全边界：[SECURITY.md](SECURITY.md)
- 贡献与验证：[CONTRIBUTING.md](CONTRIBUTING.md)

## 不做什么

不提供实盘或券商接口，不承诺收益，不做策略比较/排名，不做自动晋级、AI 自进化、行情供应商接入或完整订单生命周期。V2 的成交价、容量和费用都是显式的离线假设，用来验证规则，不用来估计投资回报。

# AStock V2 — 独立 A 股模拟盘内核（paper-only）

本仓库现在**只有一个应用**：`v2/` 下的 AStock V2。旧的 V1 应用（`backend/`、`frontend/`、`deploy/`、`plans/`、根目录 `docs/` 与旧 Python/Docker 入口）已从当前分支移除，原始代码由 Git 标签完整保留（见 [V1 退役记录](docs/V1_RETIREMENT.md)）。

V2 是一个**本机离线研究内核**，不是可部署的公网服务：

- 单一 Rust Cargo 项目、单一 Rust 服务（`127.0.0.1:8765`），Store 是唯一 SQLite 写入者。
- Python 只做离线研究信号，不接触账本；TypeScript 只读展示。
- 全部行情、成交价与容量都是**合成的离线测试输入**；不接券商、不接行情供应商、不打真实资金。
- 没有调度器、没有自动晋级、没有实盘入口、没有 V1 兼容层。

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

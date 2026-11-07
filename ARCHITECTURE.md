# 架构说明

本仓库的架构权威在 V2 内部，不在根目录复制一份：

- 模块所有权、依赖方向、不变量与拆分门槛：[v2/docs/MODULE_BOUNDARIES.md](v2/docs/MODULE_BOUNDARIES.md)
- 运行规则：日历/时段、证券资格、证据契约、来源信任、历史状态、执行证据：[v2/docs](v2/docs)
- 决策记录：ADR 001（独立实验与输入版本）、ADR 002（固定离线市场数据包）、ADR 003（可核验策略工件与冻结研究输入）
- V2 能力与边界：[v2/README.md](v2/README.md)

固定约束（任何改动都必须保持）：

```text
v2/web/  TypeScript + Vite —— 本机只读展示
   |
   v
v2/src/main.rs —— Rust localhost API（127.0.0.1:8765）
   |
   v
v2/src/store/  —— 唯一 SQLite 写入者、只读投影与回放/重做校验
   |
   v
v2/src/domain/ —— 纯规则：契约、组合、日历、证券、执行
   ^
   | 显式版本化 JSON；研究侧永远拿不到账本连接
v2/research/   —— 离线研究生产者（Python 标准库）
```

- 单 Cargo、单服务、单 SQLite 写入者。
- 依赖方向固定：HTTP → Store → Domain；Domain 无 IO、无时钟、无网络。
- 不新增微服务、消息队列、ORM、通用插件框架或兼容层；新模块必须有独立业务事实所有权。
- V1 已退役：`backend/`、`frontend/`、`deploy/`、`plans/` 与旧运行入口不在当前分支，原始代码见 [V1 退役记录](docs/V1_RETIREMENT.md) 的归档标签。

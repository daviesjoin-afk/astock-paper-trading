# 贡献指南（V2）

本仓库只维护 AStock V2。提 PR 前先读 [README](README.md)、[模块边界](v2/docs/MODULE_BOUNDARIES.md) 和 [V2 说明](v2/README.md)。V1 代码不在当前分支，也不要重新引入。

## 开发流程

1. 从 `master` 建短分支，说明要解决的**单个**问题。
2. 保持单 Cargo、单 Rust 服务、Store 唯一 SQLite 写入者；不要新增服务、消息队列、ORM 或插件框架。
3. 领域规则只在 `v2/src/domain/`，持久化只在 `v2/src/store/`，HTTP 只在 `v2/src/main.rs`；研究侧只产出信号。

## 验证（按改动范围跑，收尾跑一次全量）

```bash
# Rust 内核（含单元测试、release 构建、benchmark 示例编译）
cd v2 && cargo test --locked && cargo build --release --locked

# Python 研究契约与重做测试
python3 -m unittest discover -s v2/research -p 'test_*.py' -v

# 只读面板
cd v2/web && npm ci && npm run build

# 端到端：真实服务 + 决策 + 回放 + 研究重做 + 进程重启
bash v2/scripts/smoke.sh

# 敏感数据扫描（与 CI 同一工具）
python3 scripts/security/scan-sensitive-data.py --repo . --scope all
```

PR 描述里写清改动模块、跑过的命令与结果、以及没有跑的部分和原因。

## 行为与内容要求

- 新规则至少一条**行为**测试；移除可能的旁路时加反向控制。
- 显式版本、显式时点、整数分与整手数量；不得隐式读取当前时间、最新行情或最新策略。
- 回放与研究重做是两种证明，不能互相代替，也不能用其中之一的通过冒充另一个。
- 不提交真实行情、账户、密钥、本机绝对路径或运行时数据库；合成样例必须明确标注。
- 保持 PR 单一目的；不自动合并、不部署、不在 PR 里改线上参数。

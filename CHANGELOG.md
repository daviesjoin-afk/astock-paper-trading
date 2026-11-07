# Changelog

本文件只记录当前分支（V2 唯一应用）之后的变更。

## 当前分支

- V1 应用退役：`backend/`、`frontend/`、`deploy/`、`plans/`、根目录 V1 文档与旧 Python/Docker 入口已从工作树移除，CI 只保留 V2 与安全扫描。见 [docs/V1_RETIREMENT.md](docs/V1_RETIREMENT.md)。
- V2 合并历史（master）：#355 固定离线市场数据包与证据命名空间；#356 可核验策略工件与冻结研究输入（ADR 003）。

## V1 历史

V1 的最后状态冻结在标签 `v1-retirement-baseline-2026-11-07`（提交 `96a79ff64111109d5c258a965cc0dd8048f88426`）。更早的发布记录（v0.x、v1.x、v2.0.0）保留在 Git 标签与历史提交中，不再在本文件维护。

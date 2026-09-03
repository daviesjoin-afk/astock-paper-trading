# 架构硬化交接记录

更新时间：2026-09-04（Asia/Shanghai）

## 当前本地状态

- 分支：`codex/architecture-hardening-local`
- 本地验收：`python -W error::ResourceWarning -m unittest discover -s backend -p 'test*.py'`，198 项通过；`python -m compileall -q backend` 与 `git diff --check` 通过。
- 最近完成：东财 `clist` 分页、概念成员分页、个股概念板块引用解析迁入 `backend/marketdata_providers.py`，旧 `data_fetcher.py` 入口仍为兼容包装。
- 服务器与 GitHub 状态必须分开判断；当前分支尚未推送 GitHub。

## 服务器初检（只读）

- 服务容器 healthy，`/api/paper/overview`、`/api/paper/strategy-center` 返回 200。
- 当前为非交易时段；健康接口因新交易日尚无实时快照而 `degraded`，不能代替模拟周期验收。
- `/root/codex` 的 Git 源工作区为 `22762e2`，含 100 个已修改和 37 个未跟踪文件。不得直接覆盖。
- 系统盘约剩余 12GB，而 `/root/codex` 约 13GB，其中 `data_cache` 约 8.5GB。因此同盘不能安全创建完整工作区副本。

## 交接与恢复边界

1. 仅在服务器上保留源码、Git 差异及未跟踪文件归档；不将运行数据库和缓存重复复制到同一块空间不足的磁盘。
2. 本地 Git bundle 应包含当前架构硬化分支，以便另一台电脑离线获取该分支；bundle 的准确路径和 SHA-256 需在生成后填入本文件。
3. 下一步先在交易时段做服务器数据库只读完整性与模拟周期验收；验证通过后才部署本地新版本。
4. 服务器验收通过且用户明确确认前，不得推送 GitHub 或创建/更新 PR。

## 禁止事项

- 不覆盖、删除或清理服务器现有工作区及其运行数据。
- 不执行数据库迁移或恢复脚本。
- 不接真实券商；系统始终保持模拟盘（paper-only）。

# V2 构建依赖固定

本批只固定构建解析结果，不修改交易规则、来源信任或数据库权威。
没有新增业务模块、服务、依赖或 Python 第三方包。

- `Cargo.lock` 固定 Rust 直接与传递依赖及校验和。
- `web/package-lock.json` 固定前端依赖及校验和，CI 使用 `npm ci`。
- `rust-toolchain.toml` 和两个 Rust CI 作业固定 **Rust 1.99.1**。
  该版本来自已验收 #348 的实际成功 CI：`rustc 1.99.1 (58cf8f9c3 2026-10-13)`。
  Cargo 版本为 `1.99.1 (5f94df478 2026-08-27)`；新 CI 继续打印实际版本供核验。
- CI 的 Rust tests、Release build 和真实 HTTP smoke 中的 `cargo run` 均使用 `--locked`。
  缺少锁文件或解析必须修改锁文件时直接失败，不在 CI 静默更新版本。
- Python 研究继续只使用标准库，不增加 requirements 或新运行时。

从 `v2/` 验证：

```bash
cargo test --locked --all-targets
cargo build --release --locked
python3 -m unittest discover -s research -p 'test_*.py' -v
(cd web && npm ci --no-audit --no-fund && npm run build)
bash scripts/smoke.sh
```

锁文件只在有意更新依赖时重新生成，并将差异、工具链版本及完整 CI 一并提交。
这固定了包解析与 Rust 版本，**不承诺不同 OS/CPU 或不同时期 runner 镜像生成逐字节相同的二进制**。
`ubuntu-latest`、Node 22、Python 3.13 和既有 Actions 标签仍按当前 CI 配置运行；
若以后要求这些环境也逐项固定，应另行定义镜像/工具更新范围。

前端锁文件仅去除了与构建无关的 `funding` 赞助元数据；所有包版本、下载地址、integrity、
依赖关系均保留 npm 生成值，以适应本仓库的公开域名扫描策略。更新锁文件时同样去除该字段，
再用 npm ci/build 和完整 CI 验证，禁止手改版本或校验和来绕过解析。

工具链版本选择参考 [Rust 工具链 Action](https://github.com/dtolnay/rust-toolchain#example-workflow)。

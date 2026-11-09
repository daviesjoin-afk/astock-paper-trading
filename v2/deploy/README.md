# 部署 AStock V2（单机纯模拟）

V2 只有两个进程：Rust 内核（HTTP API + SQLite 写入）+ 静态控制台（nginx 或任意静态服务器）。
没有券商连接、没有后台守护线程，调度由 cron/systemd timer 从外部驱动。

## 1. 构建产物

```bash
cd v2 && cargo build --release --locked                 # target/release/astock-v2
cd web && npm ci && npm run build                       # web/dist
```

## 2. 安装（root）

```bash
sudo bash v2/deploy/install.sh
```

安装内容：

| 路径 | 内容 |
| --- | --- |
| `/opt/astock-v2/bin/astock-v2` | 内核二进制 |
| `/opt/astock-v2/web` | 控制台静态文件 |
| `/etc/astock-v2.env` | 环境变量（首次安装从示例复制） |
| `/etc/systemd/system/astock-v2.service` | 单元文件 |
| `/var/lib/astock-v2` | SQLite 数据目录（服务用户 `astock`） |
| `/var/backups/astock-v2` | 备份目录（可选挂到归档面板） |

安装脚本**不会**自动 enable/start，也不会覆盖已有 `/etc/astock-v2.env`。

## 3. 配置

```bash
sudoedit /etc/astock-v2.env
```

| 变量 | 作用 |
| --- | --- |
| `ASTOCK_V2_DB` | SQLite 路径；schema 不匹配会按设计拒绝启动（当前 v15） |
| `ASTOCK_PRICE_SETS_DIR` | reviewed 价格集合目录；为空则估值/绩效保持 `unavailable` |
| `ASTOCK_ARCHIVE_DIR` | 只读列出的备份目录（控制台“归档清单”） |

不要把 `synthetic` 集合放进生产目录。

## 4. 反向代理

```bash
sudo cp v2/deploy/nginx.conf /etc/nginx/conf.d/astock-v2.conf
sudo nginx -t && sudo systemctl reload nginx
```

控制台与 `/v2` API 同源，不需要 CORS；静态资源走 `try_files` 回落到 `index.html`。

## 5. 启动与验证

```bash
sudo systemctl daemon-reload
sudo systemctl enable --now astock-v2
curl -fsS http://127.0.0.1:8765/v2/health
curl -fsS http://127.0.0.1:8765/v2/paper-accounts
```

`health` 里应看到 `schema_version: 15`、`paper_only: true`、账户/会话计数与 `archive_dir_configured`。

## 6. 调度

```cron
30 15 * * 1-5  ASTOCK_API=http://127.0.0.1:8765 /opt/astock-v2/bin/../../v2/scripts/daily_advance.sh --account paper-001 --day "$(TZ=Asia/Shanghai date +%F)"
```

脚本随仓库提供；部署时把 `v2/scripts/` 一并复制到合适位置，或直接调用 API：

```bash
curl -fsS -X POST http://127.0.0.1:8765/v2/paper-accounts/paper-001/advance \
  -H 'content-type: application/json' -d '{"day":"2026-11-02"}'
```

`--dry-run` 会先调只读预览（`/advance/preview`）再打印将执行多少步。

## 7. 备份与归档

```bash
python v2/scripts/db_tool.py backup --db /var/lib/astock-v2/astock-v2.sqlite3 --out /var/backups/astock-v2/
curl -fsS "http://127.0.0.1:8765/v2/evolution/events/export" -o /var/backups/astock-v2/export.json
python v2/scripts/verify_archive.py --dir /var/backups/astock-v2
```

## 8. 边界

- 纯模拟：没有实盘账户、没有券商 API；
- 单写者：只有内核进程写 SQLite，控制台只读；
- 数据缺失即拒绝：没有 reviewed 集合时不会估算收益；
- 升级 schema 需要新库（旧库会被拒绝），先备份再迁移。

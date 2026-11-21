# 部署 AStock V2（甲骨文单机，纯模拟）

V2 只有两个进程：Rust 内核（HTTP API + SQLite 写入）和静态网页。
没有券商连接、没有后台自动交易守护线程；是否每天推进由 cron 或手工按钮决定。

## 1. 先备份，再安装

如果机器上已有旧 V2 数据库或旧版本目录，先保留，不要覆盖。下面以本机
实际的旧目录 `/opt/astock-v2` 和旧库 `/opt/astock-v2/data/v2.sqlite3`
为例；路径不同时按实际部署位置替换。

```bash
STAMP=$(date -u +%Y%m%dT%H%M%SZ)
BACKUP=/opt/astock-v2/backups/pre-v2-rewrite-$STAMP
sudo install -d -m 750 "$BACKUP"
sudo cp -a /opt/astock-v2 "$BACKUP/source-tree-before-rewrite"
sudo python3 - "$BACKUP/v2-schema15-$STAMP.sqlite3" <<'PY'
import sqlite3
import sys

source = sqlite3.connect("file:/opt/astock-v2/data/v2.sqlite3?mode=ro", uri=True)
target = sqlite3.connect(sys.argv[1])
try:
    source.backup(target)
finally:
    target.close()
    source.close()
PY
```

不要直接 `cp` 正在写入的 SQLite 主文件；上面的 Python 在线备份会把 WAL
中的已提交内容一起写进备份，再用 `PRAGMA integrity_check` 验收。

旧库 `PRAGMA user_version > 0` 会被新内核明确拒绝；这不是 bug，而是防止把
旧账本误读成新交易。保留旧文件，使用新的 `ASTOCK_V2_DB` 启动。

## 2. 构建产物

在仓库的 `v2/` 目录：

```bash
cargo build --release --locked        # target/release/astock-v2
cd web
npm ci
npm run build                         # web/dist
```

## 3. 安装（root）

```bash
sudo bash v2/deploy/install.sh
```

安装内容：

| 路径 | 内容 |
| --- | --- |
| `/opt/astock-v2-rewrite/bin/astock-v2` | 内核二进制 |
| `/opt/astock-v2-rewrite/web` | 网页静态文件 |
| `/etc/astock-v2-rewrite.env` | 环境变量（首次从示例复制） |
| `/etc/systemd/system/astock-v2-rewrite.service` | systemd 单元 |
| `/var/lib/astock-v2-rewrite` | SQLite 数据目录（用户 `astock`） |
| `/var/backups/astock-v2-rewrite` | 备份目录 |

安装脚本不会自动启动，也不会覆盖已有 `/etc/astock-v2-rewrite.env`。

## 4. 配置

```bash
sudoedit /etc/astock-v2-rewrite.env
```

| 变量 | 作用 |
| --- | --- |
| `ASTOCK_V2_DB` | 新数据库路径，例如 `/var/lib/astock-v2-rewrite/v2.sqlite3` |
| `ASTOCK_PRICE_SETS_DIR` | 已审核价格集合目录，例如 `/var/lib/astock-v2-rewrite/price-sets` |

`ASTOCK_ARCHIVE_DIR` 已不再是新内核的功能，可保留旧值但不会被读取。
不要把 `synthetic` 演示数据放进生产目录。

## 5. 反向代理

```bash
sudo cp v2/deploy/nginx.conf /etc/nginx/conf.d/astock-v2-rewrite.conf
sudo nginx -t && sudo systemctl reload nginx
```

重写版使用 **8600** 作为公开入口，Rust API 只监听 `127.0.0.1:18765`。
切换前先停掉旧的 `astock-v2.service`，否则 nginx 会因端口冲突无法加载；
旧程序、旧库和备份仍保留在 `/opt/astock-v2`，但不再对外提供页面。
不要把重写版的静态目录指回 `/opt/astock-v2/web`。网页和 `/v2` API 同源，
不需要 CORS。

## 6. 启动与验收

```bash
sudo systemctl daemon-reload
sudo systemctl enable --now astock-v2-rewrite
curl -fsS http://127.0.0.1:18765/v2/health
```

期望看到 `schema_version: 2`、`paper_only: true`。然后打开网页：

1. 输入本金，确认稳健/激进各 50%；
2. 在已安装 reviewed 行情的情况下点“推进到最新交易日”；
3. 再点一次，确认提示“没有重复成交”。

## 7. 每日调度（可选）

```cron
30 15 * * 1-5 ASTOCK_API=http://127.0.0.1:18765 \
  /opt/astock-v2-rewrite/scripts/daily_auto.sh --day "$(TZ=Asia/Shanghai date +%F)"
```

`daily_auto.sh` 只调用 `POST /v2/account/run`；没有对应已审核日期时服务会
拒绝，不会编造价格。`daily_advance.sh --dry-run --day ...` 只读查看当前覆盖。
这两个脚本由 `install.sh` 复制到 `/opt/astock-v2-rewrite/scripts/`，所以更新
源码后要重新执行一次 `install.sh`，cron 才能使用新版本。

服务器需要放行 TCP 8600。OCI 安全列表和主机防火墙是两层独立检查：
只有 `curl http://127.0.0.1:8600/` 成功不代表公网可用，必须从外部网络或
第三方探测节点确认返回 200。

## 8. 备份

```bash
sudo /opt/astock-v2-rewrite/bin/astock-v2 db backup \
  --db /var/lib/astock-v2-rewrite/v2.sqlite3 \
  --out /var/backups/astock-v2-rewrite/v2-$(date +%F).sqlite3
```

`astock-v2 db` 提供在线备份、校验和恢复。备份和恢复包含源库已提交的 WAL 数据。
现有定时任务若使用旧 Python 备份入口，请更新为上面的命令，再校验新备份。

## 9. 边界

- 纯模拟：没有实盘账户、没有券商 API；
- 单写者：只有 Rust 内核写 SQLite，网页只读；
- 行情 fail-closed：没有 reviewed 价格时不下单、不估值；
- 旧 schema 不迁移：保留旧库，新库重新开始。


## 线上盘中服务与收盘研究

线上盘中内核是 `/opt/astock-live/bin/astock-v2`，监听 `127.0.0.1:18765`。
nginx 的 8600 入口从 `/opt/astock-v2-rewrite/web` 提供静态文件，并将 `/v2/` 转发给内核。
更新线上盘中组件时，内核和静态文件分别部署到这两个路径。前文的 `install.sh`
用于独立的日线环境，线上盘中服务使用本目录的 `astock-live.*` 单元。

`astock-live-evolution.service` 通过 `--kernel /opt/astock-live/bin/astock-v2`
调用 `replay-intraday`。回放只从标准输入读取研究样本，使用内存账本，跟随内核版本。
进化脚本和对应的 `live_ai.py`、`live_quote_feed.py` 一起安装到
`/opt/astock-live-evolution/scripts/`。现有研究文件仍放在 `/var/lib/astock-live-evolution`。

正常交易时段，先用线上股票池和日历观察 Rust 接受的有源时间行情数量：

```bash
sudo -u astock /opt/astock-live/bin/astock-v2 live-loop --once --observe-only \
  --calendar /opt/astock-live/data/calendar-2026-v1.json \
  --universe /opt/astock-live/live-a-universe.txt \
  --log /opt/astock-live/work/live/quote-preflight.log
```

比对 `sample_count` 和 `universe_count`，并确认早盘、午后覆盖符合实际行情。
通过后，将现有盘中循环单元的 `ExecStart` 换成下面的命令，确认只有一个盘中循环在提交轮次：

```ini
ExecStart=/opt/astock-live/bin/astock-v2 live-loop --api http://127.0.0.1:18765 --universe /opt/astock-live/live-a-universe.txt --history-db /opt/astock-live/history-closes.json --calendar /opt/astock-live/data/calendar-2026-v1.json --interval 180 --ai on --ai-script /opt/astock-live/scripts/live_ai.py --log /opt/astock-live/work/live/intraday-loop.log
```

通过 systemd drop-in 替换 `ExecStart` 时，先添加空的 `ExecStart=`。模型设置继续从
`/etc/astock-live.env` 读取。部署 `live_ai.py` 时在同目录提供 `live_quote_feed.py`。
模型超时或不可用时使用空建议，行情在模型返回时已经过期则跳过轮次。

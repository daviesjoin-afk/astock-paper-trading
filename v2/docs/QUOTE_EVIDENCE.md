# V2-2 第一条业务链：固定行情证据再执行

本批只完成“独立导入离线证据 → 研究引用 → Rust 决策 → 原子落账 → 固定输入回放”。
没有新增业务模块、crate、进程或依赖；没有接入真实行情商。

## 事实归属与依赖

| 现有所有者 | 唯一事实或职责 | 消费者与依赖方向 |
| --- | --- | --- |
| Domain contract | 证据与引用的版本化数据形状 | execution、Store、HTTP 使用；仅依赖 serde/标准库 |
| Domain execution | 给定证据在决策时是否可用，以及现有成交规则 | Store 调用纯函数；依赖 contract/portfolio，不访问数据库或机器时间 |
| Store 写入口 | 某来源和报价身份对应的唯一证据，以及每次决策固定的输入 | HTTP 调用 Store；Store 向下调用 Domain，并独占事务与持久化写入 |
| Store query | 保存的输入、结果和成交能否确定性重演 | API/控制台消费，只读 Store/Domain |

报价入库与成交落账具有不同事实身份，但由现有 Store 写入口直接承担，
不增加转发层。API 只解析 JSON、调用 Store、转换状态码，不复制判断。

## 契约与使用

先启动 Rust API，使用一个全新 V2 数据文件：

```bash
cd v2
export ASTOCK_V2_DB=./v2-evidence-paper.sqlite3
cargo run --locked
```

在另一终端、仓库根目录执行：

```bash
curl --fail -H 'Content-Type: application/json' \
  --data-binary @v2/fixtures/quote.json http://127.0.0.1:8765/v2/quotes
python3 v2/research/worker.py --output /tmp/v2-proposal.json --send
curl --fail http://127.0.0.1:8765/v2/replay
```

证据输入格式见 `fixtures/quote.json`，证据自身 `schema_version=1`：

- `quote.observed_at`：样本声称的行情发生时间。
- `available_at`：样本声明的可见时间，即该历史模拟何时能知道它。
- 导入命令仍为版本 1；返回的落库证据为版本 4，包含 Store 记录的实际接收时间、离线信任标记、可选执行块及Store解析的证券身份，见 [SOURCE_TRUST.md](SOURCE_TRUST.md) 与 [EXECUTION_EVIDENCE.md](EXECUTION_EVIDENCE.md)。
- 两个时间必须带中国 `+08:00` 时区；发生时间不得晚于可见时间。
- 只接受明确的 `offline-fixture` 来源；其他来源返回 422。
- `(source, quote_id)` 唯一；所有字段完全相同的重复导入成功，
  同身份修改价格、时间、标的或交易标志返回 409。

研究提案升级为 `schema_version=2`，仅包含 `quote_ref: {source, quote_id}`。
内嵌报价、引用中的价格、signal 中的 quantity/shares/amount 都严格拒绝。
Python 不导入证据、不计算成交数量、不打开账本。

## 拒绝与历史一致性

- 没有对应证据：保存 `quote_evidence_missing` 拒绝；不写成交。
- 可见时间晚于决策：保存 `quote_not_yet_available` 拒绝；不写成交。
- 恰好相等可用；晚 1 毫秒不可用。行情新鲜度等现有规则继续适用。
- 同一信号身份重复提交返回原始决策；改变引用属于冲突，返回 409。
- 决策、当次证据快照（可以为缺失）、结果和成交在同一个事务中提交。
  入库证据本身不改现金或持仓。
- 回放只使用当次固定的快照。缺失证据后来补入，原拒绝和重试结果保持。
  当次有证据时，还要与证据表核对；证据被改动或删除时回放失败。
- 当前规则为 `v2-paper-policy-6`，SQLite schema version 5。旧策略文件在任何 WAL/DDL 初始化之前拒绝打开；
  本批不迁移旧账本，也不重新解释旧成交。

## 测试证据

Rust 行为测试覆盖正常引用、身份冲突、非法导入、缺失/迟到拒绝、毫秒边界、
重复导入与重复执行、来源隔离、后补证据、证据篡改/删除、事务回滚和旧库拒绝。
正常与拒绝决策都反复回放。Python 测试确认提案与独立样本引用一致且不携带价格。

`scripts/smoke.sh` 用真实 HTTP、Python 研究进程、Rust API 和 SQLite 验证：
正常成交、重复请求、报价冲突、未知来源、价格/数量绕过、缺失后补、迟到和回放。
完整验收命令：

```bash
cd v2
cargo test --locked --all-targets
cargo build --release --locked
python3 -m unittest discover -s research -p 'test_*.py' -v
(cd web && npm ci --no-audit --no-fund && npm run build)
bash scripts/smoke.sh
```

## 本批边界

`available_at` 是离线样本声明，不是服务器收到真实供应商数据的认证时间。
任何能调用本地导入接口的程序仍可提交样本；来源白名单不等于身份认证。
本批提供可追溯且不可覆盖的模拟证据，不能证明市场价格真实。
供应商认证、接收时间证明、交易日历、流动性与部分成交继续独立建设。

# 安全边界

本仓库只包含 AStock V2：一个本机离线、paper-only 的研究内核。没有公网服务、没有券商接口、没有真实资金、没有生产凭据。V1 应用已退役，其代码仅存在于 Git 历史与归档标签中（见 [V1 退役记录](docs/V1_RETIREMENT.md)）。

## 禁止提交的内容（P0）

以下内容一律不得进入仓库、提交信息、Issue、PR、CI 日志或截图：

- 私人邮箱（提交身份使用 `*@users.noreply.github.com`）
- 公网 IP、真实服务器地址、SSH 登录信息与私钥
- 密码、Token、API Key、Cookie、Session 凭据
- 真实账户号 / 券商账户 ID
- 数据库、导出数据、日志、备份文件
- 本机绝对路径（`C:\Users\<name>`）与服务器部署绝对路径

## 强制扫描

CI（`.github/workflows/security-leak-scan.yml`）对**工作树与全部可达 Git 历史**运行 [扫描器](scripts/security/scan-sensitive-data.py)；本地推送前可执行：

```bash
python3 scripts/security/scan-sensitive-data.py --repo . --scope all
bash scripts/security/scan-before-push.sh        # 或 scan-before-push.ps1
```

放行规则只允许**逐值**登记在 [.security-allowlist](.security-allowlist)，每条必须写明"为什么可以公开"（例如已审核的合成 SHA-256 或公开提交 id）。不允许为了通过扫描而做路径级、目录级或规则级豁免；真实凭据永远不能放行。

历史中的 V1 代码不因退役而改写：Git 历史保持原样，扫描继续覆盖它，因此历史里已登记的公开值（公开提交 id、公开摘要）继续留在 allowlist 中。

## V2 的运行时边界

- 服务只绑定 `127.0.0.1:8765`，没有鉴权层，也没有公网暴露方案；不要把 V2 直接放到公网。
- 研究侧（`v2/research/`）只能产出信号，不能获得账本连接、不能决定数量/价格/资金。
- 所有行情、成交价与容量都是显式离线合成输入；扫描器不能替代数据真实性审查。
- 不接券商、不接实时行情、不读取 V1 运行库、不自动迁移历史数据库。

## 报告

发现泄露或安全问题时，请不要在公开 Issue 中贴出真实值；用仓库所有者的私密渠道报告，并同时说明受影响的路径与提交范围。

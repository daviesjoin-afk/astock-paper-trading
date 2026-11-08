# Changelog

本文件只记录当前分支（V2 唯一应用）之后的变更。

## 当前分支

- V2 确定性模拟会话（ADR 005 第二阶段起步）：新增 `PaperSessionSpec`（显式 `start_at` + 冻结信号列表，必须按时间非降序且不早于起点）与状态机 `created → running ⇄ paused → stopped/completed`；`POST /v2/paper-sessions`、`GET /v2/paper-sessions/{id}`、`POST /v2/paper-sessions/{id}/{start|pause|resume|stop|step}`。`step` 每次只提交一笔槽位订单并把显式时钟推进到该决策时刻；暂停/停止/完成后拒绝成交；会话订单以 `session_id:signal_id` 作为幂等身份。Rust 测试 118 passed（新增 4 项：逐步时钟、暂停/继续/停止、双账户确定性、非法 spec）。schema v10 → v11（`v2_paper_sessions`）。
- V2 模拟账户控制台（ADR 005 第三阶段）：新增 `GET /v2/paper-accounts` 目录与前端「模拟账户」视图（`#/account`，深链 `?account=`）——展示初始本金、准备金、账户现金、两槽位预算/现金/持仓/订单数，以及按槽位归属的最近订单（时间按中国时区、成交或拒绝原因逐条可回溯）；账户目录为空、账户不存在、读取失败为三种独立状态，页面只发 GET。夹具新增账户与一笔槽位 A 成交；浏览器测试 6 项（拆分展示、空目录、404、深链、只读、390px），axe 增加账户视图扫描。
- V2 账户订单路径（ADR 005 第二阶段）：新增 `PaperOrderRequest` 与 `POST /v2/paper-accounts/{id}/orders`、`GET /v2/paper-accounts/{id}/orders`；决策复用 `domain::decide`，槽位独立重建 `PortfolioState`，槽位内 append-only + 幂等键（同请求 duplicate、异载荷冲突），证据缺失按规则拒绝且不改账本；账户视图的现金与持仓改为从槽位 fill 派生，现金守恒 `reserve + Σslot.cash = initial − 净流出` 由测试与冒烟双重断言。schema v9 → v10（新增 `v2_paper_orders` / `v2_paper_fills`）。Rust 114 passed；冒烟覆盖下单、守恒、幂等与订单列表。
- V2 模拟总账户核心（ADR 005，第一阶段）：新增 `PaperAccountSpec` 纯域契约（A/B 两槽位、权重 basis points、显式 `created_at`、每槽独立的策略工件/数据包/执行规则）；整数分划分把除不尽的余数全部留给准备金，`reserve + Σ budget = initial` 恒成立；Store 新增 `v2_paper_accounts`（schema v9）与 `create_paper_account` / `paper_account`，同 spec 幂等、异 spec 冲突、重开文件视图不变；HTTP 新增 `POST /v2/paper-accounts`、`GET /v2/paper-accounts/{account_id}`（幂等/只读，不产生交易事实）。**schema v8 → v9，旧本地数据库需重建。** 成交、自动运行与前端留在后续 PR。
- V2 校验控件生命周期修复（PR #360 复审）：`invalidateVerification()` 在刷新、切换实验或进入通知态时统一递增请求序列号、恢复按钮可用并移除 `aria-busy`；迟到的旧响应既不改变结果也不会把新实验的按钮锁死；刷新过程中启动的校验在数据代次变更后被丢弃并提示重新验证。新增 4 项生命周期测试（刷新中校验、切换实验、互不锁定、失败恢复）。
- V2 看板数据一致性门禁（PR #360 审查修复）：资产读取改为单口径请求并按响应 `cycle_id`/`asof_day` 校验，历史查询失败保留上次成功口径并在概览与持仓同时标注；决策时间固定按 `Asia/Shanghai` 显示（UTC/+08:00/-08:00 结果一致，跨日期正确）；回放/重做校验收紧为 `ok === true` + 报告实验身份匹配 + 数据 epoch 失效 + 请求序列号 + 单次在途；实验目录按 500 上限读取并对目录外 `cycle_id` 直接核验，区分“未列出”“不存在”“记录损坏”，部分损坏保留可用实验并提示；数据包显示可区分编号、未知信号给出稳定短标识、决策行显示记录编号、技术标识可键盘复制。新增 `e2e/consistency.spec.ts`（14 项）与对应 a11y/截图证据。
- V2 看板质量收尾：空表状态图标居中、去纹理背景；无障碍扫描扩展到空目录、服务错误、未知实验、日终口径、筛选空结果与暗色校验错误等 9 个额外状态。新增空表截图证据。
- V2 看板可分享状态：决策筛选写入 URL（`?result=filled|rejected`）并在刷新与切换实验后保持；上下文栏新增“复制链接”复制当前实验+视图+筛选的深链；刷新进行中顶栏刷新按钮旋转并标记 `data-busy`。补充深链往返、跨实验保持、剪贴板与刷新态测试。
- V2 看板交互补完：新增键盘快捷键（`1`–`6` 切视图、`/` 聚焦实验选择器、`r` 刷新、`t` 切主题、`?` 打开说明、`Esc` 关闭）与可发现的快捷键说明对话框；未存储主题时跟随系统 `prefers-color-scheme` 并可被手动选择覆盖；新增打印样式（去导航/控件、强制浅色、面板不跨页、释放表格滚动区）；窄屏按钮按内容分配宽度、320px 数字单列。补充快捷键、系统主题、对话框无障碍与打印/320px 截图用例。
- V2 只读看板界面重做（`v2/web`）：新设计令牌与亮/暗双主题；顶部实验选择器 + 刷新 + 主题切换；侧边二级导航在窄屏改为横条与小屏三列网格；粘性实验上下文栏；概览新增“最近决策”与“质量校验”两张镜像卡；决策表支持“全部/已成交/已拒绝”筛选并显示计数；持仓与决策表在桌面端面板内滚动并保持表头可见；空状态、服务错误、未知实验改为带图标的通知并给出可复制的本地命令；加载改为骨架屏。新增 `v2/docs/UI_NOTES.md` 说明展示层边界、令牌与测试契约，并补充长列表、320px、筛选、镜像卡、导航禁用等行为测试与截图证据。
- V1 应用退役：`backend/`、`frontend/`、`deploy/`、`plans/`、根目录 V1 文档与旧 Python/Docker 入口已从工作树移除，CI 只保留 V2 与安全扫描。见 [docs/V1_RETIREMENT.md](docs/V1_RETIREMENT.md)。
- V2 合并历史（master）：#355 固定离线市场数据包与证据命名空间；#356 可核验策略工件与冻结研究输入（ADR 003）。

## V1 历史

V1 的最后状态冻结在标签 `v1-retirement-baseline-2026-11-07`（提交 `96a79ff64111109d5c258a965cc0dd8048f88426`）。更早的发布记录（v0.x、v1.x、v2.0.0）保留在 Git 标签与历史提交中，不再在本文件维护。

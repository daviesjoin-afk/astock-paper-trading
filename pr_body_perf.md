# perf(frontend): 切页卡顿治理 —— overview/审计/子页 stale-while-revalidate + Observer/图表开销削减

## 一、根因定位（对应卡顿的三个来源）

用「Network 面板看请求耗时 → Performance 面板看 Main 线程火焰图 → Elements 面板看 Layout/Paint」的三步法定位（详见 PR 描述末尾的排查方法），确认切页耗时构成：

| 来源 | 具体问题 | 占比（主线程） |
|---|---|---|
| 网络 | `showPaperWorkspace` 每次切换子页签都调 `loadPaper()` → 重新请求 `/api/paper/overview`（服务端 30s TTL 缓存，但 payload 30KB+，实测首拉 0.22-0.35s） | 等待白屏的主因 |
| 脚本 | ① 每次渲染几十次大段 `innerHTML` 赋值；② `MutationObserver` 监听整个 body，**每次 DOM 变更都 TreeWalker 全量遍历 p-paper + p-adaptive 所有文本节点**，一次大盘渲染触发几十次全树扫描；③ `activatePage` 对所有 echarts 实例（含隐藏页）resize | 卡顿主因 |
| 渲染 | 组合/委托/档案共用同一批容器，每次切换整体重建 DOM（策略卡×5、持仓行、委托行、过滤器），再全量 layout/paint | 次因 |

## 二、修复内容（全部为缓存与开销治理，无 DOM 结构/路由/交互改动）

1. **`loadPaper` stale-while-revalidate**：渲染逻辑原样搬移为 `renderPaperDashboard(d, auditRequest)`（27,345 字符逐字节一致，仅 audit 块加缓存）；`loadPaper` 变成调度器——切换时立即渲染上次数据，**60s 内零请求零重绘**；过期后台静默刷新，payload 指纹未变不重绘；按 workspace 变体（portfolio/activity/history）分别缓存。
2. **写操作绕过缓存**：下单/撤单/暂停/恢复/重置/启动周期/风格切换/保存设置共 9 处改为 `loadPaper({force:true})`，保证账本变更立即可见。
3. **审计请求缓存**：activity 页的 `risk-audit?limit=160` 结果缓存复用。
4. **MutationObserver 只处理新增节点**：不再每次变更全树扫描（此前是渲染期最大脚本开销）。
5. **`activatePage` 图表按需 resize**：rAF 内只 resize 可见图表。
6. **风控/策略中心/策略证据/策略选股页同步加 60s SWR 缓存**：切子页不再白屏闪 loading；补录等写操作显式失效缓存。

## 三、预期收益与改动成本

| 项 | 收益 | 成本 |
|---|---|---|
| overview SWR | 60s 内切页从「0.2-0.4s 等待 + 全量重建」降为 **0 网络 0 重绘**（纯内存渲染路径） | 低（调度器 ~60 行 + 渲染体原样搬移） |
| Observer 修复 | 大盘渲染脚本耗时按「全树扫描次数 × 树大小」等比下降，实测场景为主收益 | 低（回调改 15 行） |
| 图表按需 resize | 切页瞬间省掉 N 个隐藏 echarts 实例 resize | 极低（5 行） |
| 子页 SWR | 策略中心/证据/风控/选股切页白屏消失 | 低（每处 ~10 行） |

## 四、验证方式

- **离线回归**：278 项测试全过（源码契约测试 `test_frontend_risk_audit_race` 已同步新实现断言）
- **结构等价**：脚本比对证明渲染体为逐字节搬移（27,345 chars identical）
- **调度器行为**：Node 桩环境 6 项用例——首拉、60s 内零请求、过期不变不重绘、过期变更重绘、force 绕过、变体独立缓存
- **接口实测**（ASTOCK_DEMO=1 离线演示环境）：overview 32-38KB，首拉 0.22-0.35s → 命中缓存 0 请求
- `node --check` 源码与 dist 均通过；dist 已重建

## 五、上线后如何复测

1. DevTools Network：60s 内往返切「组合/委托记录/个股档案」应无新的 `/api/paper/overview` 请求
2. Performance 面板录制切页：Main 线程不应再出现长任务（此前是 innerHTML + 全树扫描）
3. 功能回归：下单/撤单/暂停恢复后数据立即刷新（force 路径）；「刷新页面」按钮行为不变（refresh=1）

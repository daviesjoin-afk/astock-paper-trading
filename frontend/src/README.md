# frontend/src —— 前端模块地图

单文件 `frontend/app.js` 在 PR-55 按 **feature ownership** 拆到 `src/`；PR-57 在策略工作流上做了可读性与交互层级重构，并落地了共享的标签/对话框模块。本文件描述**当前**结构。

样式在 `frontend/styles/`（`styles/index.css` 按原顺序 `@import` 各片段），构建产物是 `frontend/dist/app.js` 与 `frontend/dist/app.css`。

## 目录与依赖（由 import 推导）

| 模块 | 行数 | 依赖（同仓库） |
| --- | --- | --- |
| `app.js` | 6 | — |
| `boot.js` | 120 | `core/api.js`, `core/dom.js`, `core/navigation.js`, `features/adaptive.js`, `features/paper.js`, `features/risk.js`, `features/selection.js`, `features/strategies.js` |
| `bridge.js` | 111 | `core/format.js`, `core/navigation.js`, `features/adaptive.js`, `features/execution.js`, `features/paper.js`, `features/risk.js`, `features/selection.js`, `features/settings.js`, `features/strategies.js` |
| `core/api.js` | 46 | — |
| `core/dom.js` | 30 | — |
| `core/format.js` | 101 | — |
| `core/navigation.js` | 202 | `core/dom.js`, `features/*.js`（各页面装载入口 + hash 路由意图） |
| `core/state.js` | 8 | — |
| `core/strategy_labels.js` | 77 | — （PR-57：状态/技术标签的唯一口径） |
| `features/adaptive.js` | 749 | `core/api.js`, `core/dom.js`, `core/format.js`, `features/execution.js`, `ui/dialog.js` |
| `features/execution.js` | 144 | `core/api.js`, `core/dom.js`, `core/format.js`, `core/state.js`, `features/adaptive.js` |
| `features/paper.js` | 869 | `core/api.js`, `core/dom.js`, `core/format.js`, `core/navigation.js`, `core/state.js`, `features/risk.js`, `features/strategies.js`, `ui/dialog.js` |
| `features/risk.js` | 107 | `core/api.js`, `core/dom.js`, `core/format.js`, `core/state.js` |
| `features/selection.js` | 547 | `core/api.js`, `core/dom.js`, `core/format.js`, `core/navigation.js`, `core/state.js` |
| `features/settings.js` | 252 | `core/api.js`, `core/dom.js`, `core/format.js`, `core/navigation.js`, `features/paper.js`, `features/strategies.js`, `ui/dialog.js` |
| `features/strategies.js` | 712 | `core/api.js`, `core/dom.js`, `core/format.js`, `core/navigation.js`, `core/strategy_labels.js`, `ui/dialog.js` |
| `ui/dialog.js` | 174 | `core/format.js` |

## 功能归属（feature ownership）

| 功能 | 归属模块 | 可以依赖 | 不应依赖 |
| --- | --- | --- | --- |
| 策略工坊（定义 / DSL / 预览 / 版本 / 生命周期 / 克隆） | `features/strategies.js` | `core/*`、`ui/dialog.js` | `features/settings.js` |
| 设置中心（运行参数与下一周期参与集合） | `features/settings.js` | `core/*`、`ui/dialog.js`、`features/paper.js`、`features/strategies.js`（只读列表与跳转） | 不实现第二套 DSL 编辑器 |
| 纸盘（运行策略 / 持仓 / 委托 / 档案 / 研究证据） | `features/paper.js` | `core/*`、`ui/dialog.js`、`features/risk.js`、`features/strategies.js` | `features/settings.js` |
| 执行与人工核验 | `features/execution.js` | `core/*`、`features/adaptive.js` | 纸盘写操作 |
| 风控中心与审计 | `features/risk.js` | `core/*` | — |
| 选股研究 | `features/selection.js` | `core/*` | — |
| 进化中心 / AI 研究 | `features/adaptive.js` | `core/*`、`ui/dialog.js`、`features/execution.js` | — |
| 页面装载与路由 | `core/navigation.js` | 各 `features/*` 装载入口 | 不复制业务规则 |
| 标签枚举与徽章 | `core/strategy_labels.js` | — | 任何 feature（纯函数） |
| 确认/提示对话框 | `ui/dialog.js` | `core/format.js` | 任何 feature |

规则：**跨模块引用必须显式 import**；模块之间不共享作用域，漏一个 import 不会让构建失败，只会在浏览器里 `ReferenceError`。

## 装载顺序（等于语义）

1. `app.js` 先声明 build id（`__ASTOCK_ADAPTIVE_UI_BUILD__`，与 `backend/build_info.py` 一致）；
2. `bridge.js` 把 inline handler 需要的函数挂到 `window`；
3. `boot.js` 执行全部顶层语句（保持原文件顺序）。

## 两个"缝"：迁移缝，不是目标架构

拆分成模块时保留了两处兼容结构。**它们是过渡手段，不是期望的长期架构**：

| 文件 | 现状作用 | 为什么存在 | 目标方向 |
| --- | --- | --- | --- |
| `bridge.js` | 把 `onclick="fn(...)"` 需要的函数显式挂到 `window`，使模块化的同时不改变既有 HTML 契约 | 现有模板字符串大量使用 inline handler；一次性全改会把这轮"纯搬运"变成行为重写 | 逐步改为事件委托 / `addEventListener`，随 handler 数量下降最终删除 `bridge.js` |
| `boot.js` | 集中执行顶层语句，保持与拆分前单文件一致的就绪顺序 | 函数声明会提升，只有顶层语句与 `var` 初始化有顺序语义；集中放置可逐条等价 | 待顶层语句被拆进各模块的显式 `init()` 后，`boot.js` 退化为一个普通入口装配点 |

新增 inline handler **不是**推荐做法：它需要同步维护 `bridge.js`，并由契约测试强制（见下）。新代码优先使用显式 `init()` + 事件绑定。

## 约束（由 `backend/test_frontend_module_contract.py` 强制）

- inline handler 调用的每个本仓库函数都必须挂到 `window`（`bridge.js`），且 `bridge.js` 不能挂不存在的名字、不能赋值未 import 的名字；
- 跨模块引用必须有 import（漏了不报构建错，只在运行时 `ReferenceError`）；
- 入口必须声明 `__ASTOCK_ADAPTIVE_UI_BUILD__` 且与 `backend/build_info.py` 一致；
- `frontend/app.js` / `frontend/app.css` 不再存在；产物仍是 `dist/app.js` / `dist/app.css`，且 dist 必须包含与源一致的 build id 与全部桥接全局；
- `styles/index.css` 必须按顺序 `@import` 全部样式片段，片段行区间连续无缝（拆分前后 CSS 等价）。

浏览器侧的同一契约还有 Playwright 覆盖：`frontend/e2e/specs/bridge-contract.spec.js` 直接采集页面 DOM 上的 inline handler 并断言都在 `window` 上存在。

## 改前端时的固定动作

```bash
cd frontend && npm ci && npm run build     # 生成 dist/**
git add frontend/src frontend/styles frontend/dist
```

`frontend/dist` 是**提交物**，CI 会重跑构建并 `git diff --exit-code -- frontend/dist`。浏览器 E2E 用 `frontend/e2e/server.py` 起一个合成数据实例（`ASTOCK_DATA_DIR` 指向临时目录、`ASTOCK_DEMO=1`）。

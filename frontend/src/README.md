# frontend/src —— 前端模块地图（PR-55）

单文件 `frontend/app.js`（3359 行 / 311 KB）按 **feature ownership** 拆到这里。
本轮是**纯搬运**：文案、业务语义、API contract、生命周期、视觉与之前完全一致
（CSS 产物逐字节不变，见下）。

## 目录

```
src/
  app.js            入口：build id（第 2 行，PR-49 契约）+ 装载顺序
  bridge.js         inline handler 的 window.* 兼容桥（由脚本生成，勿手工删）
  boot.js           顶层语句与 var 初始化：**保持原始顺序**，最后执行
  core/
    api.js          唯一 HTTP 出口（api/apiPost/apiPostJson/apiJson + 重试与错误解析）
    dom.js          DOM/图表基础件（$ / setEl / tableScroll / chart 注册表）
    format.js       展示格式化与文本净化（fmt / pctTxt / cny / riskText / …）
    navigation.js   页面与工作区导航、主题、手动刷新
    state.js        确实共享的少量 SPA 状态（导航 TTL 等）
  features/
    strategies.js   Strategy Workbench（策略工坊）：定义 / DSL / 版本 / 生命周期
    settings.js     设置中心（模拟盘、风控、策略参数、AI、执行开关）
    selection.js    选股研究、跟踪、评估
    paper.js        模拟交易：运行策略、持仓、委托、档案、研究证据
    execution.js    执行画像与人工核验
    risk.js         风控中心与审计
    adaptive.js     进化中心 / AI 研究
  ui/
    dialog.js       确认框与操作提示
```

## 装载顺序（等于语义）

1. `app.js` 先声明 build id；
2. `bridge.js` 把 inline handler 需要的函数挂到 `window`；
3. `boot.js` 执行全部顶层语句（保持原文件顺序）。

**为什么把顶层语句集中在 boot.js**：函数声明会被提升，只有顶层语句与 `var`
初始化有执行顺序；把它们按原顺序放在最后执行，与拆分前的单文件行为逐条等价。

## 依赖概览（由 import 生成）

| 模块 | 行数 | 依赖（同仓库） |
| --- | --- | --- |
| `app.js` | 7 | — |
| `boot.js` | 120 | `core/api.js`, `core/dom.js`, `core/navigation.js`, `features/adaptive.js`, `features/paper.js`, `features/risk.js`, `features/selection.js`, `features/strategies.js` |
| `bridge.js` | 111 | `core/format.js`, `core/navigation.js`, `features/adaptive.js`, `features/execution.js`, `features/paper.js`, `features/risk.js`, `features/selection.js`, `features/settings.js`, `features/strategies.js` |
| `core/api.js` | 47 | — |
| `core/dom.js` | 31 | — |
| `core/format.js` | 102 | — |
| `core/navigation.js` | 192 | `core/dom.js`, `features/adaptive.js`, `features/execution.js`, `features/paper.js`, `features/risk.js`, `features/selection.js`, `features/settings.js`, `features/strategies.js` |
| `core/state.js` | 9 | — |
| `features/adaptive.js` | 750 | `core/api.js`, `core/dom.js`, `core/format.js`, `features/execution.js`, `ui/dialog.js` |
| `features/execution.js` | 145 | `core/api.js`, `core/dom.js`, `core/format.js`, `core/state.js`, `features/adaptive.js` |
| `features/paper.js` | 822 | `core/api.js`, `core/dom.js`, `core/format.js`, `core/navigation.js`, `core/state.js`, `features/risk.js`, `features/strategies.js` |
| `features/risk.js` | 108 | `core/api.js`, `core/dom.js`, `core/format.js`, `core/state.js` |
| `features/selection.js` | 548 | `core/api.js`, `core/dom.js`, `core/format.js`, `core/navigation.js`, `core/state.js` |
| `features/settings.js` | 234 | `core/api.js`, `core/dom.js`, `core/format.js`, `core/navigation.js`, `features/paper.js`, `features/strategies.js`, `ui/dialog.js` |
| `features/strategies.js` | 514 | `core/api.js`, `core/dom.js`, `core/format.js`, `core/navigation.js` |
| `ui/dialog.js` | 48 | `core/format.js` |

## 约束（由 backend/test_frontend_module_contract.py 强制）

- inline handler 调用的每个函数都必须在 `bridge.js` 里挂到 `window`；
- 跨模块引用必须有 import（漏了不会构建失败，只会在浏览器里 ReferenceError）；
- 入口必须声明 `__ASTOCK_ADAPTIVE_UI_BUILD__` 且与 `backend/build_info.py` 一致；
- `frontend/app.js` / `frontend/app.css` 不再存在，产物仍是 `dist/app.js` / `dist/app.css`。

## 改前端时的固定动作

```bash
cd frontend && npm ci && npm run build     # 生成 dist/**
git add frontend/src frontend/styles frontend/dist
```

`frontend/dist` 是**提交物**，CI 会重跑构建并 `git diff --exit-code -- frontend/dist`。

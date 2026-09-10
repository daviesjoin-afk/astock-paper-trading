// PR-56 / PR-56b：Playwright 共用夹具。
//
// 1) 浏览器错误策略：每个页面收集 pageerror 与 console.error，测试结束若非空 → 失败。
//    allowlist 保持为空（不允许为了过测试而放宽）。
// 2) 真实确认框：产品用 window.confirm 做生命周期/删除确认，必须与真实弹窗交互
//    （这里统一 accept 并记录文案），不允许用 JS 绕过。
// 3) 共享小工具：唯一 ID、导航、UI 建草稿、只读 API 回读、卡片动作定位。
import { test as base, expect } from "@playwright/test";

const ALLOWED_NOISE = [
  // /favicon\.ico/,
];

function isAllowedNoise(text) {
  return ALLOWED_NOISE.some((re) => re.test(text));
}

export const test = base.extend({
  page: async ({ page }, use, testInfo) => {
    const errors = [];
    const dialogs = [];
    page.on("pageerror", (err) => {
      const text = `${err.name}: ${err.message}`;
      if (!isAllowedNoise(text)) errors.push(`pageerror -> ${text}`);
    });
    page.on("console", (msg) => {
      if (msg.type() !== "error") return;
      const text = msg.text();
      const location = (msg.location() && msg.location().url) || "";
      // 唯一放行项（已文档化）：未知策略 id 的深链接会真实请求
      // GET /api/strategies/<unknown> 并得到 404，浏览器必然把该 404 记为
      // console.error。这不是产品缺陷（C 用例断言的就是"未知 id 要有可读状态"），
      // 因此仅对**该路径**的 404 放行，其他 404/错误仍会让测试失败。
      const isExpectedUnknownStrategy404 =
        /status of 404/.test(text) && /\/api\/strategies\/does_not_exist/.test(location);
      if (isExpectedUnknownStrategy404) return;
      if (!isAllowedNoise(text)) errors.push(`console.error -> ${text}`);
    });
    // 真实弹窗交互：confirm 接受；prompt 返回 test 预设的答案（默认克隆用 ID）。
    page.__promptAnswer = null;
    page.on("dialog", async (dialog) => {
      dialogs.push({ type: dialog.type(), message: dialog.message() });
      if (dialog.type() === "prompt") {
        // 未指定答案时接受产品给出的默认值（等价于真人直接点确定）
        await dialog.accept(page.__promptAnswer == null ? dialog.defaultValue() : page.__promptAnswer);
        return;
      }
      await dialog.accept();
    });
    page.__dialogs = dialogs;

    await use(page);

    if (errors.length) {
      await testInfo.attach("browser-errors.txt", {
        body: errors.join("\n"),
        contentType: "text/plain",
      });
    }
    expect(errors, `浏览器运行时错误（必须先修好，不允许忽略）：\n${errors.join("\n")}`).toEqual([]);
  },
});

export { expect };

let seq = 0;
/** 唯一 ID：共享同一临时库时避免测试之间互相污染。 */
export function uniqueId(prefix) {
  seq += 1;
  return `${prefix}_${process.pid}_${seq}`;
}

export const SIMPLE_DSL = {
  op: "gt",
  left: { op: "field", name: "close" },
  right: { op: "indicator", name: "ma", window: 20 },
};

export const SIMPLE_DSL_V2 = {
  op: "gt",
  left: { op: "field", name: "close" },
  right: { op: "indicator", name: "ma", window: 21 },
};

/**
 * 带参数 Schema 的 DSL：一个未锁定可调参数（risk_per_trade）+ 一个锁定参数
 * （holding_days）。用于验证预览"可进化参数"段只展示 DSL Schema 声明的可调项。
 */
export const PARAM_DSL = {
  op: "strategy",
  rule: {
    op: "gt",
    left: { op: "field", name: "close" },
    right: { op: "indicator", name: "ma", window: 20 },
  },
  parameters: [
    {
      op: "parameter", parameter_id: "risk_per_trade", type: "number",
      value: 0.02, min: 0.002, max: 0.02, max_step: 0.002,
      locked: false, risk_direction: "higher_is_riskier", min_evidence: 10,
    },
    {
      op: "parameter", parameter_id: "holding_days", type: "integer",
      value: 5, min: 1, max: 20, max_step: 1,
      locked: true, risk_direction: "neutral", min_evidence: 0,
    },
  ],
};

/** 主菜单导航（稳定 testid）。 */
export async function gotoPage(page, testid) {
  await page.goto("/");
  await page.getByTestId(testid).click();
  await expect(page.getByTestId(testid)).toHaveClass(/active/);
}

/** 打开策略工坊并等待 registry 渲染完成。 */
export async function openWorkbench(page) {
  await gotoPage(page, "main-nav-strategies");
  await expect(page.getByTestId("strategy-summary")).not.toContainText("正在读取");
  await expect(page.getByTestId("strategy-list")).not.toContainText("正在读取");
}

/** 等待某个 API 响应（真实 HTTP 断言）。 */
export function waitForApi(page, matcher, method = "POST") {
  return page.waitForResponse(
    (res) => res.request().method() === method && matcher.test(new URL(res.url()).pathname),
  );
}

/** 只读 API 回读。 */
export async function apiJson(page, path) {
  const res = await page.request.get(path);
  expect(res.ok(), `GET ${path} 应返回 2xx`).toBeTruthy();
  return res.json();
}

/** 通过 UI 新建草稿（真实按钮 + 真实表单 + 真实 HTTP）。 */
export async function createDraftViaUi(page, { id, name = "E2E 策略", dsl = SIMPLE_DSL }) {
  await page.getByTestId("strategy-new").click();
  await expect(page.getByTestId("strategy-editor")).toBeVisible();
  await page.getByTestId("strategy-id").fill(id);
  await page.getByTestId("strategy-name").fill(name);
  await page.getByTestId("strategy-mode-dsl").click();
  await page.getByTestId("strategy-dsl").fill(JSON.stringify(dsl));
  const created = waitForApi(page, /\/api\/strategies$/);
  await page.getByTestId("strategy-save").click();
  const res = await created;
  expect(res.status(), "保存草稿必须真实落库").toBeLessThan(300);
  await expect(page.getByTestId(`strategy-card-${id}`)).toBeVisible();
  return id;
}

/** 列表卡片内部的动作按钮（生命周期按钮都在卡片上）。 */
export function cardAction(page, strategyId, testid) {
  return page.getByTestId(`strategy-card-${strategyId}`).getByTestId(testid);
}

/** 从 draft 推进到 active（全部 UI 点击 + 真实 HTTP + 真实确认框）。 */
export async function promoteToActive(page, strategyId, { from = "draft" } = {}) {
  const transition = /\/api\/strategies\/[^/]+\/transition$/;
  if (from === "draft") {
    const validated = await clickAndApprove(page, cardAction(page, strategyId, "strategy-transition-validated"), transition);
    expect(validated.ok(), "draft→validated 必须 2xx").toBeTruthy();
    await expect(page.getByTestId(`strategy-card-${strategyId}`)).toHaveAttribute("data-status", "validated");
  }
  const activated = await clickAndApprove(page, cardAction(page, strategyId, "strategy-transition-active"), transition);
  expect(activated.ok(), "validated→active 必须 2xx").toBeTruthy();
  await expect(page.getByTestId(`strategy-card-${strategyId}`)).toHaveAttribute("data-status", "active");
  return strategyId;
}

/** 打开设置中心的"模拟盘与资金"子页（下一周期策略集合所在处）。 */
export async function openSettings(page) {
  await gotoPage(page, "settings-nav");
  await expect(page.getByTestId("settings-result")).not.toContainText("正在读取");
  return page.getByTestId("settings-result");
}

/** 读取当前"下一周期启用策略"集合（只读 API 回读）。 */
export async function enabledStrategies(page) {
  // 契约：GET /api/settings/ -> { settings: { simulation: { enabled_strategies: [...] } } }
  const body = await apiJson(page, "/api/settings/");
  const sim = (body && body.settings && body.settings.simulation) || {};
  return sim.enabled_strategies || [];
}

/**
 * PR-57：危险/高影响动作改为应用内模态（DOM，不是浏览器原生 dialog）。
 * 测试必须像真人一样点击模态里的确认按钮——不允许用 JS 绕过。
 */
export async function approveModal(page, label) {
  const modal = page.getByTestId("app-confirm-dialog");
  await expect(modal).toBeVisible();
  const approve = modal.locator('[data-action="approve"]');
  if (label) await expect(approve).toContainText(label);
  await approve.click();
  await expect(modal).toHaveCount(0);
}

/** 打开模态并确认的同时等待其触发的 API 响应。 */
export async function clickAndApprove(page, clickTarget, matcher) {
  await clickTarget.click();
  const response = waitForApi(page, matcher);
  await approveModal(page);
  return response;
}

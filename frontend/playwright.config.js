// PR-56：真实浏览器 E2E（Playwright + Chromium）。
//
// 设计要点：
// - 只跑 Chromium（本轮不做 Firefox/WebKit 矩阵，控制 CI 成本）。
// - 由 frontend/e2e/server.py 起**真实 FastAPI 应用**（同一份 backend 代码），
//   指向独立临时数据目录（ASTOCK_DATA_DIR），不碰仓库/生产库。
// - 静态前端由同一个应用伺服（/、/app.js、/app.css），所以测的是真实产物。
// - 失败必留证据：screenshot + trace，CI 上再加 video。
// - 浏览器运行时错误（pageerror / console.error）由 fixtures 统一收集并让测试失败。
import { defineConfig, devices } from "@playwright/test";
import { fileURLToPath } from "node:url";
import { existsSync } from "node:fs";
import path from "node:path";

// package.json 是 "type": "module"，配置也走 ESM，所以没有 __dirname。
const here = path.dirname(fileURLToPath(import.meta.url));

/**
 * 后端解释器必须是**项目虚拟环境**里的那一个：系统 Python 的 chinese-calendar
 * 版本可能覆盖不到当前年份，会让离线启动失败。优先 ASTOCK_E2E_PYTHON，
 * 否则用仓库 .venv，再否则退回 PATH 的 python。
 */
function resolvePython() {
  if (process.env.ASTOCK_E2E_PYTHON) return process.env.ASTOCK_E2E_PYTHON;
  const candidates = [
    path.resolve(here, "..", ".venv", "Scripts", "python.exe"),
    path.resolve(here, "..", ".venv", "bin", "python"),
  ];
  return candidates.find((candidate) => existsSync(candidate)) || "python";
}

const PYTHON = resolvePython();

// PR-2：必须与 e2e/server.py 的 OPERATOR_TOKEN 完全一致——服务端起应用时
// 用它配置边界，这里用它给浏览器注入凭据。
const OPERATOR_TOKEN = "zz-e2e-operator-placeholder-value";

const PORT = Number(process.env.ASTOCK_E2E_PORT || 8611);
const BASE_URL = `http://127.0.0.1:${PORT}`;
const isCI = !!process.env.CI;

export default defineConfig({
  testDir: "./e2e/specs",
  // 每个 spec 文件串行，避免多个 worker 抢同一份临时账本；文件之间可并行。
  fullyParallel: false,
  workers: isCI ? 1 : undefined,
  forbidOnly: isCI,
  retries: isCI ? 1 : 0,
  timeout: 60_000,
  expect: { timeout: 15_000 },
  reporter: isCI ? [["list"], ["html", { open: "never" }]] : [["list"]],
  outputDir: "./e2e/.artifacts",
  use: {
    baseURL: BASE_URL,
    // PR-2：page.request.*（Playwright 的 API 请求上下文）不共享浏览器的
    // localStorage，所以直接调写接口的用例必须显式带 operator 凭据。
    // 这里按头统一注入，值与 e2e/server.py 的 OPERATOR_TOKEN 一致。
    extraHTTPHeaders: { "X-Operator-Token": OPERATOR_TOKEN },
    // 失败证据
    screenshot: "only-on-failure",
    trace: "retain-on-failure",
    video: isCI ? "retain-on-failure" : "off",
    actionTimeout: 15_000,
    navigationTimeout: 30_000,
  },
  projects: [
    {
      name: "chromium",
      use: {
        ...devices["Desktop Chrome"],
        // PR-2：把 operator token 注入到页面的 localStorage，前端写请求会
        // 从那里读取并放进 X-Operator-Token 头。这与真实运维"本机写入一次
        // 凭据"的用法一致，而不是在测试里绕过鉴权。
        // 值必须与 e2e/server.py 的 OPERATOR_TOKEN 保持一致。
        storageState: {
          cookies: [],
          origins: [
            {
              origin: BASE_URL,
              localStorage: [
                { name: "operatorToken", value: OPERATOR_TOKEN },
              ],
            },
          ],
        },
      },
    },
  ],
  webServer: {
    command: `"${PYTHON}" e2e/server.py --port ${PORT}`,
    cwd: here,
    url: `${BASE_URL}/api/health`,
    // 永远新建：每个 run 一份独立临时数据目录（ASTOCK_DATA_DIR），
    // 复用旧进程会带上前一次运行的账本状态，破坏隔离。
    reuseExistingServer: false,
    timeout: 180_000,
    stdout: "pipe",
    stderr: "pipe",
  },
});

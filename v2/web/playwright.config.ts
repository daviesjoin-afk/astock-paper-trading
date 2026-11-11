import { defineConfig } from "@playwright/test";

const baseURL = "http://127.0.0.1:4173";
const apiURL = "http://127.0.0.1:18766";

export default defineConfig({
  testDir: "e2e",
  fullyParallel: false,
  workers: 1,
  timeout: 45_000,
  expect: { timeout: 15_000 },
  reporter: process.env.CI ? [["list"], ["line"]] : [["list"]],
  use: {
    baseURL,
    viewport: { width: 1280, height: 800 },
    trace: "retain-on-failure",
    screenshot: "only-on-failure",
  },
  webServer: [
    {
      // Real Rust kernel with an isolated SQLite file: never the developer DB.
      command: "node e2e/start-server.mjs",
      url: `${apiURL}/v2/health`,
      reuseExistingServer: false,
      timeout: 120_000,
      stdout: "pipe",
      stderr: "pipe",
      env: {
        ASTOCK_V2_ADDR: new URL(apiURL).host,
        ASTOCK_V2_E2E_INSTANCE: "rewrite",
      },
    },
    {
      command: "npx vite preview --host 127.0.0.1 --port 4173 --strictPort",
      url: baseURL,
      reuseExistingServer: false,
      timeout: 60_000,
      env: { ASTOCK_V2_API_URL: apiURL },
    },
  ],
});

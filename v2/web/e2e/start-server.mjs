// Boot the real Rust kernel for browser tests on an isolated database file.
// The kernel has no test mode: the UI tests hit the same localhost API as users.
import { spawn } from "node:child_process";
import { existsSync, mkdirSync, rmSync, writeFileSync } from "node:fs";
import { dirname } from "node:path";
import { join } from "node:path";

const root = new URL("..", import.meta.url).pathname.replace(/^\/([A-Za-z]:)/, "$1");
const candidates = [
  process.env.ASTOCK_V2_BIN,
  join(root, "..", "target", "release", "astock-v2.exe"),
  join(root, "..", "target", "release", "astock-v2"),
].filter(Boolean);

const binary = candidates.find((path) => existsSync(path));
if (!binary) {
  console.error("astock-v2 binary not found; build it with: cd v2 && cargo build --release --locked");
  process.exit(1);
}

const instance = process.env.ASTOCK_V2_E2E_INSTANCE;
if (!instance) {
  console.error("ASTOCK_V2_E2E_INSTANCE is required");
  process.exit(1);
}
const database = process.env.ASTOCK_V2_E2E_DB || join(root, ".e2e", `v2-e2e-${instance}.sqlite3`);
const priceSets =
  process.env.ASTOCK_PRICE_SETS_DIR || join(root, ".e2e", `price-sets-${instance}`);
if (!process.env.ASTOCK_V2_E2E_DB) {
  mkdirSync(dirname(database), { recursive: true });
  for (const suffix of ["", "-wal", "-shm"]) {
    rmSync(database + suffix, { force: true });
  }
}
mkdirSync(priceSets, { recursive: true });
for (const name of ["e2e-reviewed.json"]) {
  rmSync(join(priceSets, name), { force: true });
}

// Deterministic browser-test data, deliberately marked synthetic. The kernel
// requires an explicit test opt-in and this directory is isolated from any
// production catalog.
const start = new Date(Date.UTC(2026, 6, 1));
const tradingDays = [];
while (tradingDays.length < 70) {
  const weekday = start.getUTCDay();
  if (weekday !== 0 && weekday !== 6) {
    tradingDays.push(start.toISOString().slice(0, 10));
  }
  start.setUTCDate(start.getUTCDate() + 1);
}
const entries = [];
for (const [symbol, base, slope] of [
  ["600000.SH", 1000, 3],
  ["000001.SZ", 1200, 4],
]) {
  tradingDays.forEach((day, index) => {
    const price = base + index * slope;
    entries.push({
      symbol,
      day,
      price_cents: price,
      source: "e2e-synthetic",
      observed_at: `${day}T15:00:00+08:00`,
    });
  });
}
writeFileSync(
  join(priceSets, "e2e-reviewed.json"),
  JSON.stringify({
    schema_version: 1,
    price_set_id: "e2e-synthetic",
    market_data_pack_id: "e2e-pack",
    trust: "synthetic",
    coverage_start: tradingDays[0],
    coverage_end: tradingDays[tradingDays.length - 1],
    entries,
    source_digest: "a".repeat(64),
  }),
);

const child = spawn(binary, {
  env: {
    ...process.env,
    ASTOCK_V2_DB: database,
    ASTOCK_V2_ADDR: process.env.ASTOCK_V2_ADDR || "127.0.0.1:18766",
    ASTOCK_PRICE_SETS_DIR: priceSets,
    ASTOCK_ALLOW_SYNTHETIC: "1",
  },
  stdio: "inherit",
});

const stop = (signal) => child.kill(signal);
process.on("SIGTERM", () => stop("SIGTERM"));
process.on("SIGINT", () => stop("SIGINT"));
child.on("exit", (code) => process.exit(code ?? 0));

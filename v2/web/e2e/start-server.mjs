// Boot the real Rust kernel for browser tests on an isolated database file.
// The kernel has no test mode: the UI tests hit the same localhost API as users.
import { spawn } from "node:child_process";
import { existsSync, mkdirSync, rmSync } from "node:fs";
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

const database = process.env.ASTOCK_V2_E2E_DB || join(root, ".e2e", "v2-e2e.sqlite3");
if (!process.env.ASTOCK_V2_E2E_DB) {
  mkdirSync(dirname(database), { recursive: true });
  for (const suffix of ["", "-wal", "-shm"]) {
    rmSync(database + suffix, { force: true });
  }
}

const child = spawn(binary, {
  env: { ...process.env, ASTOCK_V2_DB: database },
  stdio: "inherit",
});

const stop = (signal) => child.kill(signal);
process.on("SIGTERM", () => stop("SIGTERM"));
process.on("SIGINT", () => stop("SIGINT"));
child.on("exit", (code) => process.exit(code ?? 0));

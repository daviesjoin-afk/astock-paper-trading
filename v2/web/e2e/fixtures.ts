// Deterministic fixtures for the browser tests.
//
// Everything is created through the public read/write API against an isolated
// SQLite file (see start-server.mjs). Nothing here is injected into the page:
// the UI reads the same endpoints a user reads.
import type { APIRequestContext } from "@playwright/test";
import { readFileSync } from "node:fs";
import { fileURLToPath } from "node:url";
import { join } from "node:path";

const dataDir = fileURLToPath(new URL("../../data/", import.meta.url));

export const PACK = "cn-offline-fixture-pack-1";

export const PAPER_ACCOUNT = "e2e-account";
export const SESSION_ACCOUNT = "e2e-session-account";
export const PAPER_SESSION = "e2e-session";

export const CYCLES = {
  holdings: "e2e-holdings",
  flat: "e2e-flat",
  repro: "e2e-repro",
  empty: "e2e-empty",
  rejected: "e2e-rejected",
} as const;

type Level = { price_cents: number; available_quantity: number };
type Entry = {
  sequence: number;
  decision_at: string;
  available_at: string;
  quote: {
    symbol: string;
    price_cents: number;
    observed_at: string;
    source: string;
    quote_id: string;
    tradable: boolean;
    limit_up: boolean;
    limit_down: boolean;
  };
  buy: Level;
  sell: Level;
};
type Artifact = { strategy_version_id: string } & Record<string, unknown>;
type Manifest = { entries: Entry[]; market_data_pack_id: string };
type Output = {
  signals: Array<{
    sequence: number;
    signal_id: string;
    decision_at: string;
    symbol: string;
    side: "buy" | "sell";
    source: string;
    quote_id: string;
    market_data_pack_id: string;
  }>;
};

const read = <T>(name: string): T =>
  JSON.parse(readFileSync(join(dataDir, name), "utf8")) as T;

export const artifact = (): Artifact => read("strategy-momentum-v1.json");
const manifest = (): Manifest => read("research-input-offline-v1.json");
const output = (): Output => read("research-output-momentum-v1.json");

const entryFor = (quoteId: string): Entry => {
  const entry = manifest().entries.find((item) => item.quote.quote_id === quoteId);
  if (!entry) throw new Error("fixture entry missing: " + quoteId);
  return entry;
};

const evidence = (entry: Entry, side: "buy" | "sell") => {
  const level = entry[side];
  return {
    market_data_pack_id: PACK,
    schema_version: 2,
    available_at: entry.available_at,
    quote: entry.quote,
    execution: {
      schema_version: 2,
      quote_ref: { market_data_pack_id: PACK, source: entry.quote.source, quote_id: entry.quote.quote_id },
      order_side: side,
      price_cents: level.price_cents,
      available_quantity: level.available_quantity,
      observed_at: entry.quote.observed_at,
    },
  };
};

const request = (cycle: string, signal: Output["signals"][number]) => ({
  schema_version: 4,
  cycle_id: cycle,
  strategy_version_id: artifact().strategy_version_id,
  signal_id: signal.signal_id,
  decision_at: signal.decision_at,
  signal: { symbol: signal.symbol, side: signal.side },
  quote_ref: {
    market_data_pack_id: signal.market_data_pack_id,
    source: signal.source,
    quote_id: signal.quote_id,
  },
});

const create = async (api: APIRequestContext, cycle: string, cash: number) => {
  const response = await api.post("/v2/experiments", {
    data: {
      schema_version: 1,
      cycle_id: cycle,
      initial_cash_cents: cash,
      strategy: artifact(),
      market_data_pack_id: PACK,
      execution_policy_version: "v2-paper-policy-6",
    },
  });
  if (!response.ok()) {
    throw new Error(`create ${cycle}: ${response.status()} ${await response.text()}`);
  }
};

const realise = async (api: APIRequestContext, cycle: string, signals: Output["signals"]) => {
  for (const signal of signals) {
    const entry = entryFor(signal.quote_id);
    const quote = await api.post("/v2/quotes", { data: evidence(entry, signal.side) });
    if (!quote.ok()) throw new Error(`quote ${signal.quote_id}: ${quote.status()}`);
    const run = await api.post("/v2/simulations", { data: request(cycle, signal) });
    if (!run.ok()) throw new Error(`run ${signal.signal_id}: ${run.status()} ${await run.text()}`);
  }
};

let ready = false;

/** Create the fixed experiments once per worker. Idempotent on the API side. */
export async function ensureFixtures(api: APIRequestContext): Promise<void> {
  if (ready) return;
  const signals = output().signals;
  await create(api, CYCLES.holdings, 10_000_000);
  await create(api, CYCLES.flat, 10_000_000);
  await create(api, CYCLES.repro, 10_000_000);
  await create(api, CYCLES.empty, 10_000_000);
  await create(api, CYCLES.rejected, 10_000_000);
  await realise(api, CYCLES.holdings, signals.slice(0, 1));
  await realise(api, CYCLES.flat, signals);
  await realise(api, CYCLES.repro, signals);
  // Selling without a T+1 lot is a deterministic rejection worth showing.
  // Reuse the frozen sell-side entry so the evidence identity stays identical
  // to the other experiments (one payload per quote identity).
  await realise(api, CYCLES.rejected, [{ ...signals[1], signal_id: "e2e-rejected-sell" }]);
  await ensurePaperAccount(api, signals);
  await ensureSessionAccount(api, signals);
  ready = true;
}

/** A second account dedicated to session control, so console tests stay stable. */
async function ensureSessionAccount(
  api: APIRequestContext,
  signals: Output["signals"],
): Promise<void> {
  const created = await api.post("/v2/paper-accounts", {
    data: {
      schema_version: 1,
      account_id: SESSION_ACCOUNT,
      initial_cash_cents: 10_000_000,
      reserve_bps: 1_000,
      created_at: "2026-11-08T09:00:00+08:00",
      slots: [
        {
          slot: "A", strategy_version_id: "research-momentum-v1", market_data_pack_id: PACK,
          execution_policy_version: "v2-paper-policy-6", weight_bps: 4_500,
        },
        {
          slot: "B", strategy_version_id: "research-reversion-v1", market_data_pack_id: PACK,
          execution_policy_version: "v2-paper-policy-6", weight_bps: 4_500,
        },
      ],
    },
  });
  if (!created.ok() && created.status() !== 409) {
    throw new Error(`session account: ${created.status()} ${await created.text()}`);
  }
  const session = await api.post("/v2/paper-sessions", {
    data: {
      schema_version: 1,
      session_id: PAPER_SESSION,
      account_id: SESSION_ACCOUNT,
      start_at: "2026-10-30T00:00:00+08:00",
      signals: signals.map((signal) => ({
        slot: "A",
        signal_id: `session-${signal.signal_id}`,
        decision_at: signal.decision_at,
        signal: { symbol: signal.symbol, side: signal.side },
        quote_ref: {
          market_data_pack_id: signal.market_data_pack_id,
          source: signal.source,
          quote_id: signal.quote_id,
        },
      })),
    },
  });
  if (!session.ok() && session.status() !== 409) {
    throw new Error(`paper session: ${session.status()} ${await session.text()}`);
  }
}

/** One user-funded account with two slots and one filled slot-A order. */
async function ensurePaperAccount(
  api: APIRequestContext,
  signals: Output["signals"],
): Promise<void> {
  const created = await api.post("/v2/paper-accounts", {
    data: {
      schema_version: 1,
      account_id: PAPER_ACCOUNT,
      initial_cash_cents: 10_000_000,
      reserve_bps: 1_000,
      created_at: "2026-11-08T09:00:00+08:00",
      slots: [
        {
          slot: "A", strategy_version_id: "research-momentum-v1", market_data_pack_id: PACK,
          execution_policy_version: "v2-paper-policy-6", weight_bps: 4_500,
        },
        {
          slot: "B", strategy_version_id: "research-reversion-v1", market_data_pack_id: PACK,
          execution_policy_version: "v2-paper-policy-6", weight_bps: 4_500,
        },
      ],
    },
  });
  if (!created.ok() && created.status() !== 409) {
    throw new Error(`paper account: ${created.status()} ${await created.text()}`);
  }
  const signal = signals[0];
  const order = await api.post(`/v2/paper-accounts/${PAPER_ACCOUNT}/orders`, {
    data: {
      schema_version: 1,
      account_id: PAPER_ACCOUNT,
      slot: "A",
      signal_id: "paper-e2e-buy",
      decision_at: signal.decision_at,
      signal: { symbol: signal.symbol, side: signal.side },
      quote_ref: {
        market_data_pack_id: signal.market_data_pack_id,
        source: signal.source,
        quote_id: signal.quote_id,
      },
    },
  });
  if (!order.ok() && order.status() !== 409) {
    throw new Error(`paper order: ${order.status()} ${await order.text()}`);
  }
}

#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Transport-only scheduling for the intraday Rust paper kernel.

This script decides *when to send* a round, never *what to trade*. It has no
confidence floor, no score threshold, no bias limit, no symbol whitelist and no
order filter. The two scheduling skips are: outside configured session hours, and
an empty quote snapshot. Everything else, including AI advice validation,
clamping and fail-closed risk gates, belongs to ``/v2/live/round`` in Rust.

The system clock only decides when to poll and when to stop. The trading day and
the round timestamp come from the market data source's ``quote_time``; when that
time is absent it is explicitly marked estimated by the feed and never presented
as a source observation.
"""
from __future__ import annotations

import argparse
from collections import Counter
from datetime import datetime, time as time_cls, timedelta, timezone
import json
import math
import os
from pathlib import Path
import sys
import time
import urllib.error
import urllib.request
from typing import Any, Callable, Mapping, Sequence
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

sys.path.insert(0, str(Path(__file__).resolve().parent))
import live_quote_feed as feed  # noqa: E402
from live_ai import (
    DIGEST_CANDIDATES, DIGEST_POSITIONS, LIVE_SYSTEM_PROMPT, RECENT_TRADES,
    Log, _parse_model_json, held_positions, json_line, market_digest,
    ranked_candidates, raw_advice,
)

DEFAULT_API = "http://127.0.0.1:8765"
DEFAULT_INTERVAL = 180
DEFAULT_CLOSE = "15:00"
DEFAULT_TIMEZONE = "Asia/Shanghai"
DEFAULT_LOG_PATH = (Path(__file__).resolve().parent.parent
                    / "work" / "live" / "intraday-loop.log")
MORNING_OPEN, MORNING_CLOSE = time_cls(9, 30), time_cls(11, 30)
# Safety valve: if the clock never reaches a session after this many
# sleep-to-open cycles, exit and let the supervisor restart us.
MAX_CONSECUTIVE_WAITS = 5
AFTERNOON_OPEN, AFTERNOON_CLOSE = time_cls(13, 0), time_cls(15, 0)


class LoopError(Exception):
    """Configuration or transport failure requiring a clear CLI error."""


# ---- Scheduling clock (wall clock only: "when to run") --------------------

def parse_clock(value: str) -> time_cls:
    try:
        parts = value.split(":")
        if len(parts) != 2:
            raise ValueError("expected HH:MM")
        return time_cls(int(parts[0]), int(parts[1]))
    except ValueError as error:
        raise LoopError(f"invalid --close time {value!r}, expected HH:MM") from error


def load_zone(name: str) -> ZoneInfo:
    try:
        return ZoneInfo(name)
    except (ZoneInfoNotFoundError, ValueError) as error:
        raise LoopError(f"invalid --timezone {name!r}: {error}") from error


def local_now(now: Callable[[], datetime], zone: ZoneInfo) -> datetime:
    """Normalize injected clock readings; a naive reading is UTC, not local."""
    value = now()
    if not isinstance(value, datetime):
        raise LoopError("clock must return a datetime")
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    return value.astimezone(zone)


def in_session(at: datetime, close: time_cls) -> bool:
    """Weekday monotonic session check; no holiday calendar is maintained."""
    if at.weekday() >= 5:
        return False
    wall = at.timetz().replace(tzinfo=None)
    afternoon_close = min(AFTERNOON_CLOSE, close)
    return ((MORNING_OPEN <= wall <= MORNING_CLOSE)
            or (AFTERNOON_OPEN <= wall <= afternoon_close))


def seconds_to_next_boundary(at: datetime, interval: int) -> float:
    """Sleep to the next absolute interval boundary, not a fixed interval.

    Never returns 0. Sitting exactly on a boundary would otherwise mean
    "do not sleep", so the loop would re-poll the same instant forever --
    burning CPU and, under a virtual clock, never advancing.
    """
    epoch = at.timestamp()
    remainder = math.fmod(epoch, interval)
    wait = interval - remainder if remainder > 0 else interval
    return float(wait)


# ---- JSON logging ---------------------------------------------------------

# ---- HTTP transport ------------------------------------------------------

def http_json(url: str, payload: Mapping[str, Any] | None = None, *,
              timeout: float = 30.0,
              opener: Callable[..., Any] = urllib.request.urlopen) -> Any:
    """One JSON request/response. Caller handles HTTP failures explicitly."""
    data = None if payload is None else json.dumps(
        payload, ensure_ascii=False, allow_nan=False).encode("utf-8")
    request = urllib.request.Request(
        url, data=data,
        headers={"Content-Type": "application/json"} if data else {},
        method="POST" if data else "GET")
    with opener(request, timeout=timeout) as response:
        body = response.read()
    return json.loads(body.decode("utf-8"))


class Kernel:
    def __init__(self, api: str, *, opener: Callable[..., Any] = urllib.request.urlopen,
                 timeout: float = 30.0):
        self.api = api.rstrip("/")
        self.opener = opener
        self.timeout = timeout

    def _url(self, path: str) -> str:
        return self.api + path

    def get(self, path: str) -> Any:
        return http_json(self._url(path), timeout=self.timeout,
                         opener=self.opener)

    def post(self, path: str, payload: Mapping[str, Any]) -> Any:
        return http_json(self._url(path), payload, timeout=self.timeout,
                         opener=self.opener)


# ---- Round identity and response reporting -------------------------------

def round_identity(snapshot: Mapping[str, Any]) -> tuple[str, str]:
    """``(round_id, trading_day)`` derived from the data-source quote_time."""
    text = snapshot.get("quote_time")
    parsed = feed.source_datetime(text)
    if parsed is None:
        raise LoopError(f"quote_time cannot name a trading day: {text!r}")
    local = parsed.astimezone(feed.SHANGHAI)
    day = local.date().isoformat()
    return f"{day}-{local:%H%M%S}", day


def blocked_top3(decisions: Any) -> list[list[Any]]:
    reasons = Counter(
        item.get("blocked_reason")
        for item in decisions or []
        if isinstance(item, Mapping) and item.get("blocked_reason")
    )
    return [[reason, count] for reason, count in reasons.most_common(3)]


def round_log_fields(response: Any) -> dict[str, Any]:
    body = response if isinstance(response, Mapping) else {}
    decisions = body.get("decisions")
    trades = body.get("trades")
    ai = body.get("ai") if isinstance(body.get("ai"), Mapping) else {}
    equity = body.get("equity") if isinstance(body.get("equity"), Mapping) else {}
    return {
        "decisions": len(decisions) if isinstance(decisions, list) else None,
        "trades": len(trades) if isinstance(trades, list) else None,
        "equity": equity.get("equity_cents"),
        "ai": {"ok": ai.get("ok"), "error": ai.get("error")},
        "blocked_top3": blocked_top3(decisions if isinstance(decisions, list) else []),
    }


def equity_cents(value: Any) -> int | None:
    if isinstance(value, Mapping):
        candidate = value.get("equity_cents")
        return candidate if isinstance(candidate, int) and not isinstance(candidate, bool) else None
    return None


# ---- One round and the resident loop --------------------------------------

class RoundState:
    """Transport bookkeeping for the day's observed rounds and equity."""

    def __init__(self):
        self.day: str | None = None
        self.round_ids: list[str] = []
        self.equities: list[int] = []
        self.start_equity: int | None = None
        self.last_equity: int | None = None
        self.evolved: set[str] = set()

    def start_day(self, day: str) -> None:
        if day != self.day:
            self.day = day
            self.round_ids = []
            self.equities = []
            self.start_equity = None
            self.last_equity = None

    def record(self, round_id: str, response: Any) -> None:
        if round_id not in self.round_ids:
            self.round_ids.append(round_id)
        value = equity_cents(response.get("equity") if isinstance(response, Mapping) else None)
        if value is not None:
            self.equities.append(value)
            if self.start_equity is None:
                self.start_equity = value
            self.last_equity = value

    def summary(self) -> dict[str, Any]:
        profit = (None if self.last_equity is None or self.start_equity is None
                  else self.last_equity - self.start_equity)
        max_drawdown = 0
        peak = self.equities[0] if self.equities else 0
        for value in self.equities:
            peak = max(peak, value)
            if peak > 0:
                max_drawdown = max(max_drawdown, (peak - value) * 10_000 // peak)
        return {
            "round_count": len(self.round_ids),
            "round_ids": list(self.round_ids),
            "profit_cents": profit,
            "equity_cents": self.last_equity,
            "max_drawdown_bps": max_drawdown,
            "source": "posted_round_responses",
        }


def run_round(*, config: "Config", kernel: Kernel, log: Log,
              state: RoundState, snapshot: Mapping[str, Any] | None = None) -> bool:
    """Execute one scheduled round. Returns whether a round was POSTed."""
    if snapshot is None:
        try:
            snapshot = config.feed_builder(
                config.universe, batch_size=config.batch,
                history_db=config.history_db)
        except feed.FeedError as error:
            log.emit("feed_failed", reason=str(error))
            return False
    quotes = snapshot.get("quotes")
    if not isinstance(quotes, list) or not quotes:
        log.emit("round_skipped", reason="empty_quote_snapshot",
                 quote_time=snapshot.get("quote_time"))
        return False
    try:
        round_id, day = round_identity(snapshot)
    except LoopError as error:
        log.emit("round_skipped", reason=str(error),
                 quote_time=snapshot.get("quote_time"))
        return False
    state.start_day(day)

    advice = None
    if config.ai_on:
        account = trades = None
        try:
            account = kernel.get("/v2/live/account")
        except Exception as error:  # noqa: BLE001 - digest state is optional
            log.emit("account_read_failed", reason=str(error))
        try:
            trades = kernel.get("/v2/live/trades")
        except Exception as error:  # noqa: BLE001 - digest state is optional
            log.emit("trades_read_failed", reason=str(error))
        digest = market_digest(snapshot, account=account, trades=trades)
        settings_used = False
        try:
            settings_resp = kernel.post("/v2/settings/ai-advice", digest)
            if isinstance(settings_resp, dict) and (settings_resp.get("ok") or settings_resp.get("disabled")):
                advice = settings_resp.get("advice")
                settings_used = True
        except Exception:  # noqa: BLE001 - fall back to direct raw_advice if endpoint unavailable
            settings_used = False
        if not settings_used:
            advice = raw_advice(digest, base_url=config.llm_base,
                                api_key=config.llm_key, model=config.llm_model,
                                opener=config.opener, log=log, round_id=round_id)

    payload = {
        "round_id": round_id,
        "quote_time": snapshot.get("quote_time"),
        "fetched_at": snapshot.get("fetched_at"),
        "quotes": quotes,
        "advice": advice,
        "note": "live_paper_loop transport" if not config.ai_on
                else "live_paper_loop transport with raw LLM advice",
    }
    try:
        response = kernel.post("/v2/live/round", payload)
    except Exception as error:  # noqa: BLE001 - transport reports and continues
        log.emit("round_post_failed", round_id=round_id,
                 reason=f"{type(error).__name__}: {error}")
        return False
    state.record(round_id, response)
    log.emit("round", round_id=round_id, quote_time=payload["quote_time"],
             quote_time_estimated=bool(snapshot.get("quote_time_estimated")),
             sample_count=len(quotes), advice=advice is not None,
             **round_log_fields(response))
    return True


def close_evolve(*, config: "Config", kernel: Kernel, log: Log,
                 state: RoundState, now: datetime) -> None:
    """POST an inert daily evolution proposal once; never send accept=true."""
    if state.day is None or state.day in state.evolved:
        return
    payload = {
        "day": state.day,
        "created_at": now.isoformat(),
        "summary": state.summary(),
        "accept": False,
    }
    try:
        response = kernel.post("/v2/live/evolve", payload)
    except Exception as error:  # noqa: BLE001 - log, do not activate anything
        log.emit("evolve_failed", day=state.day,
                 reason=f"{type(error).__name__}: {error}")
        return
    preview = response.get("preview") if isinstance(response, Mapping) else None
    log.emit("evolve", day=state.day, accepted=False,
             reason=preview.get("reason") if isinstance(preview, Mapping) else None,
             proposal=preview.get("proposal") if isinstance(preview, Mapping) else None,
             summary=payload["summary"])
    state.evolved.add(state.day)


class Config:
    def __init__(self, *, api: str, universe: str, history_db: str | None,
                 interval: int, close: time_cls, ai_on: bool, max_rounds: int,
                 once: bool, zone: ZoneInfo, llm_base: str, llm_key: str,
                 llm_model: str, batch: int = feed.DEFAULT_BATCH_SIZE,
                 log_path: str | Path | None = DEFAULT_LOG_PATH,
                 opener: Callable[..., Any] = urllib.request.urlopen,
                 clock: Callable[[], datetime] = lambda: datetime.now(timezone.utc),
                 sleep: Callable[[float], None] = time.sleep,
                 feed_builder: Callable[..., Mapping[str, Any]] | None = None):
        self.api, self.universe, self.history_db = api, universe, history_db
        self.interval, self.close = interval, close
        self.ai_on, self.max_rounds, self.once = ai_on, max_rounds, once
        self.zone, self.llm_base = zone, llm_base
        self.llm_key, self.llm_model = llm_key, llm_model
        self.batch, self.log_path = batch, log_path
        self.opener, self.clock, self.sleep = opener, clock, sleep
        self.feed_builder = feed_builder or feed.build_snapshot


def next_session_open(after: datetime, zone: ZoneInfo) -> datetime:
    """The next 09:30 in `zone` strictly after `after`.

    Used to sleep across the close instead of exiting. Exiting would hand the
    process to systemd's Restart=always, which turns every closed-market minute
    into a fresh start-exit cycle and floods the journal with noise.
    """
    local = after.astimezone(zone)
    candidate = local.replace(hour=MORNING_OPEN.hour, minute=MORNING_OPEN.minute,
                              second=0, microsecond=0)
    if candidate <= local:
        candidate += timedelta(days=1)
    while candidate.weekday() >= 5:
        candidate += timedelta(days=1)
    return candidate


def run_loop(config: Config, *, log: Log | None = None) -> int:
    """Resident scheduler. Returns an exit code; all clocks can be injected."""
    log = log or Log(config.log_path)
    kernel = Kernel(config.api, opener=config.opener)
    state = RoundState()
    rounds = 0
    waits = 0
    last_idle_reason: str | None = None
    try:
        while True:
            now = local_now(config.clock, config.zone)
            if in_session(now, config.close):
                last_idle_reason = None
                waits = 0
                if config.max_rounds is None or rounds < config.max_rounds:
                    posted = run_round(config=config, kernel=kernel, log=log, state=state)
                    if config.once:
                        return 0 if posted else 1
                    if posted:
                        rounds += 1
                    # A round may have crossed the next interval boundary.
                    # Schedule from completion rather than the start time.
                    now = local_now(config.clock, config.zone)
                else:
                    log.emit("round_limit_reached", rounds=rounds)
            elif now.weekday() >= 5:
                # Log once per state change, not once per poll: a weekend or a
                # lunch break would otherwise write an identical line forever.
                if last_idle_reason != "weekend":
                    last_idle_reason = "weekend"
                    log.emit("outside_session", reason="weekend",
                             local_time=now.isoformat())
            else:
                if last_idle_reason != "not_in_session":
                    last_idle_reason = "not_in_session"
                    log.emit("outside_session", reason="not_in_session",
                             local_time=now.isoformat())
            if config.once:
                return 0
            past_close = (now.timetz().replace(tzinfo=None) >= config.close
                          or now.weekday() >= 5 )
            if past_close:
                # Settle the day's record once, then wait for the next open
                # rather than exiting.
                if state.day is not None and state.day not in state.evolved:
                    close_evolve(config=config, kernel=kernel, log=log,
                                 state=state, now=now)
                if config.max_rounds is not None and rounds >= config.max_rounds:
                    return 0
                target = next_session_open(now, config.zone)
                delay = max(1.0, (target - now).total_seconds())
                waits += 1
                if waits > MAX_CONSECUTIVE_WAITS:
                    # The clock never reached the computed open. Give the
                    # process back to the supervisor rather than spinning.
                    log.emit("wait_limit_reached", waits=waits,
                             until=target.isoformat())
                    return 1
                log.emit("waiting_for_open", until=target.isoformat(),
                         seconds=round(delay))
                state.evolved.clear()  # a new trading day may evolve again
                config.sleep(delay)
                continue
            if config.max_rounds is not None and rounds >= config.max_rounds:
                return 0
            delay = seconds_to_next_boundary(now, config.interval)
            config.sleep(delay)
    except KeyboardInterrupt:
        log.emit("interrupted", rounds=rounds)
        return 130


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--api", default=DEFAULT_API)
    parser.add_argument("--universe", default=str(feed.DEFAULT_UNIVERSE))
    parser.add_argument("--history-db",
                        default=os.environ.get("ASTOCK_HISTORY_DB"),
                        help="optional sqlite bar DB; missing means no history")
    parser.add_argument("--interval", type=int, default=DEFAULT_INTERVAL)
    parser.add_argument("--close", default=DEFAULT_CLOSE)
    parser.add_argument("--ai", choices=("on", "off"), default="off")
    parser.add_argument("--max-rounds", type=int, default=None)
    parser.add_argument("--once", action="store_true")
    parser.add_argument("--timezone", default=DEFAULT_TIMEZONE)
    parser.add_argument("--log", default=str(DEFAULT_LOG_PATH))
    return parser


def config_from_args(args: argparse.Namespace) -> Config:
    if args.interval <= 0:
        raise LoopError("--interval must be positive")
    if args.max_rounds is not None and args.max_rounds <= 0:
        raise LoopError("--max-rounds must be positive")
    return Config(
        api=args.api, universe=args.universe, history_db=args.history_db,
        interval=args.interval, close=parse_clock(args.close),
        ai_on=args.ai == "on", max_rounds=args.max_rounds, once=args.once,
        zone=load_zone(args.timezone),
        llm_base=os.environ.get("ASTOCK_LLM_BASE", feed.DEFAULT_LLM_BASE),
        llm_key=os.environ.get("ASTOCK_LLM_KEY", ""),
        llm_model=os.environ.get("ASTOCK_LLM_MODEL", feed.DEFAULT_LLM_MODEL),
        log_path=args.log,
    )


def main(argv: Sequence[str] | None = None) -> int:
    try:
        config = config_from_args(build_parser().parse_args(argv))
        return run_loop(config)
    except LoopError as error:
        print(f"live_paper_loop: {error}", file=sys.stderr)
        return 2
    except Exception as error:  # noqa: BLE001 - CLI must report, not traceback-only
        print(f"live_paper_loop: unexpected {type(error).__name__}: {error}",
              file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())

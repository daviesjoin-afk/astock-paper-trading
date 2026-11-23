# -*- coding: utf-8 -*-
"""Offline behavioural tests for the transport-only intraday scheduler.

Feed, HTTP, LLM, wall clock and sleep are injected. No network, real clock,
filesystem logging or real trading rule is exercised.
"""
from __future__ import annotations

from datetime import datetime, time as time_cls, timedelta, timezone
import io
import json
import os
import subprocess
from pathlib import Path
import sys
import unittest
from unittest import mock
from zoneinfo import ZoneInfo

sys.path.insert(0, str(Path(__file__).resolve().parent))
import live_paper_loop as loop  # noqa: E402

SHANGHAI = ZoneInfo("Asia/Shanghai")
QUOTE_TIME = "2026-11-13T10:03:00+08:00"  # Friday
FETCHED_AT = "2026-11-13T02:03:05+00:00"


def snapshot(*, quotes=None, quote_time=QUOTE_TIME, estimated=False):
    if quotes is None:
        quotes = [
            {"symbol": "600000.SH", "price_cents": 1010,
             "prev_close_cents": 1000, "high_cents": 1020, "low_cents": 990,
             "trailing_closes": [], "tradable": True},
            {"symbol": "000001.SZ", "price_cents": 990,
             "prev_close_cents": 1000, "high_cents": 1000, "low_cents": 980,
             "trailing_closes": [], "tradable": True},
        ]
    result = {"quotes": quotes, "quote_time": quote_time,
              "fetched_at": FETCHED_AT, "count": len(quotes),
              "source": "tencent_quote"}
    if estimated:
        result["quote_time_estimated"] = True
    return result


def round_response(*, equity=10_100_000):
    return {
        "round": {"round_id": "2026-11-13-100300", "equity_cents": equity},
        "decisions": [
            {"blocked_reason": "cash_budget"},
            {"blocked_reason": "cash_budget"},
            {"blocked_reason": "t_plus_one"},
        ],
        "trades": [{"symbol": "600000.SH", "side": "buy"}],
        "equity": {"equity_cents": equity, "account": {}},
        "ai": {"ok": False, "error": "no_advice", "application": {}},
    }


class AdviceAdapterTests(unittest.TestCase):
    def test_subprocess_returns_structured_neutral_advice_without_credentials(self):
        env = {key: value for key, value in os.environ.items() if not key.startswith("ASTOCK_LLM_")}
        script = Path(__file__).with_name("live_ai.py")
        result = subprocess.run(
            [sys.executable, "-B", "-X", "utf8", str(script)],
            input=json.dumps({"snapshot": snapshot(), "account": None, "trades": None,
                              "round_id": "fixture"}),
            encoding="utf-8", capture_output=True, env=env, check=True, timeout=10,
        )
        reply = json.loads(result.stdout)
        self.assertIsNone(reply["advice"])
        self.assertEqual(reply["events"], [{"event": "ai_unavailable", "round_id": "fixture",
                                            "reason": "missing_llm_credentials", "attempts": 0}])


class FakeOpener:
    """Mock urlopen; records JSON requests and returns scripted responses."""

    def __init__(self, *, get=None, post=None, llm=None, fail_get=()):
        self.get = get or {}
        self.post = post or {}
        self.llm = llm
        self.fail_get = set(fail_get)
        self.calls = []

    def __call__(self, request, timeout=None):
        url = request.full_url
        payload = (json.loads(request.data.decode("utf-8"))
                   if request.data else None)
        self.calls.append((request.get_method(), url, payload))
        if "/chat/completions" in url:
            if isinstance(self.llm, Exception):
                raise self.llm
            content = self.llm if isinstance(self.llm, str) else json.dumps(
                self.llm if self.llm is not None else {"confidence": 95,
                                                       "bias": 9999})
            body = {"choices": [{"message": {"content": content}}]}
            return io.BytesIO(json.dumps(body).encode("utf-8"))
        path = url.split("/v2/live")[-1]
        if request.get_method() == "GET":
            if path in self.fail_get:
                raise OSError(f"kernel unavailable: {path}")
            return io.BytesIO(json.dumps(self.get.get(path, {})).encode("utf-8"))
        return io.BytesIO(json.dumps(self.post.get(path, round_response())).encode("utf-8"))


class Clock:
    """Scripted system clock; each reading advances to the next instant."""

    def __init__(self, moments):
        self.moments = list(moments)
        self.index = 0

    def __call__(self):
        value = self.moments[min(self.index, len(self.moments) - 1)]
        self.index += 1
        return value


class SimulationOver(Exception):
    """Raised by AdvancingClock when the scripted window is exhausted."""


class AdvancingClock:
    """A clock whose virtual time actually moves when the loop sleeps.

    A static scripted clock cannot express "slept across the close and woke the
    next morning", so a sleep-to-open loop would look like an infinite spin.
    Here `sleep` advances the same instant the loop will read next.
    """

    def __init__(self, start, *, stop_at=None, max_reads=5000):
        self.now = start
        self.stop_at = stop_at
        self.reads = 0
        self.max_reads = max_reads

    def __call__(self):
        if self.stop_at is not None and self.now >= self.stop_at:
            raise SimulationOver(self.now)
        self.reads += 1
        if self.reads > self.max_reads:
            raise AssertionError("loop never terminated under the virtual clock")
        return self.now

    def sleep(self, seconds):
        self.now = self.now + timedelta(seconds=seconds)


def beijing(day: str, hour: int, minute: int, second: int = 0) -> datetime:
    return datetime.fromisoformat(f"{day}T{hour:02d}:{minute:02d}:{second:02d}"
                                  ).replace(tzinfo=SHANGHAI)


def make_config(opener, *, feed_result=None, clock=None, ai_on=False,
                once=False, max_rounds=None, interval=180,
                close=time_cls(15, 0), sleep=None, llm_key="test-key"):
    outputs = (feed_result if isinstance(feed_result, list)
               else [feed_result or snapshot()])
    # A resident loop keeps asking for snapshots after the last scripted one;
    # pad generously so a test measures the loop, not an exhausted mock.
    if len(outputs) < 3000:
        outputs = outputs + [outputs[-1]] * (3000 - len(outputs))
    return loop.Config(
        api="http://kernel.invalid", universe="unused.txt", history_db=None,
        interval=interval, close=close, ai_on=ai_on, max_rounds=max_rounds,
        once=once, zone=SHANGHAI,
        llm_base="http://llm.invalid/v1", llm_key=llm_key,
        llm_model="deepseek-v4.1-flash", log_path=None, opener=opener,
        clock=clock or Clock([beijing("2026-11-13", 10, 3, 5)]),
        sleep=sleep or (lambda seconds: None),
        feed_builder=mock.Mock(side_effect=outputs),
    )


class LivePaperLoopTests(unittest.TestCase):
    def test_round_id_is_source_day_and_time_and_retry_is_idempotent(self):
        day, stamp = loop.round_identity(snapshot())
        self.assertEqual(day, "2026-11-13-100300")
        self.assertEqual(stamp, "2026-11-13")
        self.assertEqual(loop.round_identity(snapshot())[0], day)

    def test_round_identity_converts_offset_to_shanghai_including_midnight(self):
        for value in ("2026-11-19T17:15:02Z", "2026-11-19T17:15:02.123+00:00",
                      "2026-11-20T01:15:02+08:00", "2026-11-20 01:15:02"):
            with self.subTest(value=value):
                self.assertEqual(loop.round_identity({"quote_time": value}),
                                 ("2026-11-20-011502", "2026-11-20"))

    def test_empty_snapshot_skips_kernel_without_posting(self):
        opener = FakeOpener()
        config = make_config(opener, feed_result=snapshot(quotes=[]))
        output = io.StringIO()
        with loop.Log(None, stdout=output) as log:
            loop.run_round(config=config, kernel=loop.Kernel(config.api, opener=opener),
                           log=log, state=loop.RoundState(),
                           snapshot=config.feed_builder())
        self.assertEqual([call for call in opener.calls
                          if call[1].endswith("/v2/live/round")], [])
        self.assertIn("empty_quote_snapshot", output.getvalue())

    def test_round_posts_snapshot_and_logs_kernel_outcome(self):
        opener = FakeOpener(post={"/round": round_response()})
        config = make_config(opener)
        output = io.StringIO()
        with loop.Log(None, stdout=output) as log:
            posted = loop.run_round(
                config=config, kernel=loop.Kernel(config.api, opener=opener),
                log=log, state=loop.RoundState(),
                snapshot=config.feed_builder())
        self.assertTrue(posted)
        methods = [(call[0], call[1], call[2]) for call in opener.calls]
        self.assertEqual(len(methods), 1)
        self.assertEqual(methods[0][1], "http://kernel.invalid/v2/live/round")
        self.assertEqual(methods[0][2]["round_id"], "2026-11-13-100300")
        self.assertEqual(methods[0][2]["quote_time"], QUOTE_TIME)
        self.assertEqual(methods[0][2]["fetched_at"], FETCHED_AT)
        self.assertEqual(methods[0][2]["quotes"], snapshot()["quotes"])
        self.assertIsNone(methods[0][2]["advice"])
        record = json.loads(output.getvalue().strip().splitlines()[-1])
        self.assertEqual(record["decisions"], 3)
        self.assertEqual(record["trades"], 1)
        self.assertEqual(record["equity"], 10_100_000)
        self.assertEqual(record["ai"], {"ok": False, "error": "no_advice"})
        self.assertEqual(record["blocked_top3"],
                         [["cash_budget", 2], ["t_plus_one", 1]])

    def test_a_stale_quote_is_not_posted(self):
        # The Rust live-loop already refuses a snapshot it cannot place in the
        # session; the Python scheduler accepted one whose source time was
        # missing. A price that is an hour old is not the current market.
        opener = FakeOpener(post={"/round": round_response()})
        config = make_config(opener)
        stale = {**snapshot(), "quote_time": "2026-11-13T09:03:00+08:00",
                 "fetched_at": "2026-11-13T02:03:05+00:00"}  # 1h old at fetch
        output = io.StringIO()
        with loop.Log(None, stdout=output) as log:
            posted = loop.run_round(
                config=config, kernel=loop.Kernel(config.api, opener=opener),
                log=log, state=loop.RoundState(), snapshot=stale)
        self.assertFalse(posted)
        self.assertEqual(opener.calls, [], "no round may be submitted")
        self.assertIn("quote_stale", output.getvalue())

    def test_an_undated_quote_is_refused_rather_than_treated_as_now(self):
        opener = FakeOpener(post={"/round": round_response()})
        config = make_config(opener)
        estimated = {**snapshot(), "quote_time_estimated": True}
        output = io.StringIO()
        with loop.Log(None, stdout=output) as log:
            posted = loop.run_round(
                config=config, kernel=loop.Kernel(config.api, opener=opener),
                log=log, state=loop.RoundState(), snapshot=estimated)
        self.assertFalse(posted)
        self.assertEqual(opener.calls, [])
        self.assertIn("quote_time_estimated", output.getvalue())

    def test_a_quote_that_ages_out_during_the_ai_call_is_not_posted(self):
        # The AI call is the long pole. Freshness has to be re-checked at the
        # moment of posting, not only when the snapshot was ingested.
        opener = FakeOpener(get={"/account": {}, "/trades": []},
                            post={"/round": round_response()},
                            llm=json.dumps({"bias": 0, "blocked_symbols": []}))
        config = make_config(opener, ai_on=True)
        fresh = config.feed_builder()
        # The AI call took 15 minutes: measure the held time, not the clock.
        with loop.Log(None, stdout=io.StringIO()) as log,                 mock.patch.object(loop.time, "monotonic", side_effect=[100.0, 1000.0]):
            posted = loop.run_round(
                config=config, kernel=loop.Kernel(config.api, opener=opener),
                log=log, state=loop.RoundState(), snapshot=fresh)
        self.assertFalse(posted, "a quote that aged 15 minutes must not trade")
        self.assertFalse(any(call[1].endswith("/v2/live/round")
                             for call in opener.calls))

    def test_raw_llm_json_is_transported_verbatim_without_python_policy(self):
        model_json = {"confidence": 7, "bias": 999999,
                      "blocked_symbols": ["600000.SH"],
                      "reason": "raw answer"}
        opener = FakeOpener(
            get={"/account": {"slots": [
                {"slot": "steady", "cash_cents": 100, "equity_cents": 200,
                 "positions": 1}]},
                 "/trades": [{"round_id": "r", "symbol": "600000.SH"}]},
            post={"/round": round_response()}, llm=json.dumps(model_json))
        config = make_config(opener, ai_on=True)
        with loop.Log(None, stdout=io.StringIO()) as log:
            loop.run_round(config=config,
                           kernel=loop.Kernel(config.api, opener=opener),
                           log=log, state=loop.RoundState(),
                           snapshot=config.feed_builder())
        llm_call = next(call for call in opener.calls
                        if "/chat/completions" in call[1])
        payload = json.loads(llm_call[2]["messages"][1]["content"])
        self.assertEqual(list(payload)[0], "stable", "cached prefix must lead")
        digest = {**payload["stable"], **payload["round"]}
        self.assertEqual(digest["quote_time"], QUOTE_TIME)
        self.assertEqual(digest["sample_count"], 2)
        self.assertEqual(digest["change_distribution"],
                         {"up": 1, "down": 1, "flat": 0})
        self.assertEqual(digest["slots"][0]["slot"], "steady")
        self.assertEqual(len(digest["recent_trades"]), 1)
        round_call = next(call for call in opener.calls
                          if call[1].endswith("/v2/live/round"))
        self.assertEqual(round_call[2]["advice"], {
            "model": "deepseek-v4.1-flash", "raw_advice": model_json})

    def test_llm_failure_still_posts_round_with_no_advice(self):
        opener = FakeOpener(get={"/account": {}, "/trades": []},
                            post={"/round": round_response()},
                            llm=TimeoutError("model timeout"))
        config = make_config(opener, ai_on=True)
        output = io.StringIO()
        with loop.Log(None, stdout=output) as log:
            posted = loop.run_round(
                config=config, kernel=loop.Kernel(config.api, opener=opener),
                log=log, state=loop.RoundState(),
                snapshot=config.feed_builder())
        self.assertTrue(posted)
        self.assertIn("ai_unavailable", output.getvalue())
        round_call = next(call for call in opener.calls
                          if call[1].endswith("/v2/live/round"))
        self.assertIsNone(round_call[2]["advice"])

    def test_close_posts_evolve_once_with_inert_accept_false(self):
        # Exercise the close path directly: one round recorded, then the close
        # handler. Running the whole loop here would depend on how many times
        # the virtual clock advances, which is not what this test is about.
        opener = FakeOpener(post={
            "/round": round_response(),
            "/evolve": {"preview": {"reason": "proposal_not_validated",
                                    "proposal": {"change": 1}}},
        })
        config = make_config(opener, feed_result=[snapshot()] * 4,
                             clock=AdvancingClock(beijing("2026-11-13", 14, 57)))
        output = io.StringIO()
        with loop.Log(None, stdout=output) as log:
            kernel = loop.Kernel(config.api, opener=opener)
            state = loop.RoundState()
            self.assertTrue(loop.run_round(config=config, kernel=kernel, log=log,
                                           state=state,
                                           snapshot=config.feed_builder()))
            loop.close_evolve(config=config, kernel=kernel, log=log,
                              state=state, now=beijing("2026-11-13", 15, 0, 1))
            # A second call for the same day must not post again.
            loop.close_evolve(config=config, kernel=kernel, log=log,
                              state=state, now=beijing("2026-11-13", 15, 3))

        evolve_calls = [call for call in opener.calls
                        if call[1].endswith("/v2/live/evolve")]
        self.assertEqual(len(evolve_calls), 1,
                         "one close, one proposal -- never one per poll")
        payload = evolve_calls[0][2]
        self.assertEqual(payload["day"], "2026-11-13")
        self.assertFalse(payload["accept"])
        self.assertEqual(payload["summary"]["round_count"], 1)
        self.assertEqual(payload["summary"]["round_ids"], ["2026-11-13-100300"])
        self.assertEqual(payload["summary"]["source"], "posted_round_responses")
        self.assertIn("evolve", output.getvalue())

    def test_close_waits_for_next_open_instead_of_exiting(self):
        """After the close the process must sleep to the next open, not exit.

        Exiting would hand the job to systemd's Restart=always, turning every
        closed-market minute into a start-exit cycle.
        """
        after_close = beijing("2026-11-13", 15, 30)   # Friday afternoon
        nxt = loop.next_session_open(after_close, SHANGHAI)
        self.assertEqual(nxt, beijing("2026-11-16", 9, 30),
                         "Friday close waits for Monday 09:30")

        weekend = loop.next_session_open(beijing("2026-11-14", 10, 0), SHANGHAI)
        self.assertEqual(weekend, beijing("2026-11-16", 9, 30),
                         "a Saturday waits for Monday, not another Saturday")

        morning = loop.next_session_open(beijing("2026-11-13", 8, 0), SHANGHAI)
        self.assertEqual(morning, beijing("2026-11-13", 9, 30),
                         "before the open waits for today")

    def test_once_runs_one_round_and_never_calls_evolve(self):
        opener = FakeOpener()
        clock = Clock([beijing("2026-11-13", 10, 3),
                       beijing("2026-11-13", 15, 1)])
        config = make_config(opener, clock=clock, once=True)
        with loop.Log(None, stdout=io.StringIO()) as log:
            loop.run_loop(config, log=log)
        self.assertEqual(len([call for call in opener.calls
                              if call[1].endswith("/v2/live/round")]), 1)
        self.assertEqual([call for call in opener.calls
                          if call[1].endswith("/v2/live/evolve")], [])

    def test_resident_loop_recovers_from_feed_failure_at_next_boundary(self):
        opener = FakeOpener()
        clock = AdvancingClock(beijing("2026-11-13", 10, 3, 5))
        config = make_config(opener, clock=clock, sleep=clock.sleep, max_rounds=1,
                             feed_result=[loop.feed.FeedError("batch unavailable"), snapshot()])
        output = io.StringIO()
        with loop.Log(None, stdout=output) as log:
            self.assertEqual(loop.run_loop(config, log=log), 0)
        self.assertEqual(config.feed_builder.call_count, 2)
        self.assertEqual(clock.now, beijing("2026-11-13", 10, 6))
        self.assertEqual(sum(path.endswith("/v2/live/round") for _, path, _ in opener.calls), 1)
        events = [json.loads(line)["event"] for line in output.getvalue().splitlines()]
        self.assertEqual(events, ["feed_failed", "round"])

    def test_once_reports_feed_failure_without_posting_or_sleeping(self):
        opener = FakeOpener()
        sleep = mock.Mock()
        config = make_config(opener, once=True, sleep=sleep,
                             feed_result=loop.feed.FeedError("batch unavailable"))
        with loop.Log(None, stdout=io.StringIO()) as log:
            self.assertEqual(loop.run_loop(config, log=log), 1)
        self.assertEqual(opener.calls, [])
        sleep.assert_not_called()

    def test_outside_session_skips_feed_and_kernel(self):
        opener = FakeOpener()
        config = make_config(
            opener, clock=Clock([beijing("2026-11-14", 10, 0)]),  # Saturday
            max_rounds=1)
        output = io.StringIO()
        with loop.Log(None, stdout=output) as log:
            loop.run_loop(config, log=log)
        self.assertEqual(opener.calls, [])
        self.assertEqual(config.feed_builder.call_count, 0)
        self.assertIn("outside_session", output.getvalue())

    def test_lunch_break_and_early_morning_are_outside_session(self):
        before = beijing("2026-11-13", 9, 29)
        lunch = beijing("2026-11-13", 12, 0)
        morning = beijing("2026-11-13", 10, 0)
        self.assertFalse(loop.in_session(before, time_cls(15, 0)))
        self.assertFalse(loop.in_session(lunch, time_cls(15, 0)))
        self.assertTrue(loop.in_session(morning, time_cls(15, 0)))

    def test_sleeps_to_next_three_minute_boundary_not_fixed_interval(self):
        """Boundary alignment is a property of the sleep computation itself.

        Driving the whole loop across a session would mostly measure how many
        times a mock was called; the behaviour we care about is that the wait is
        "time to the next 3-minute mark", not a flat 180 s.
        """
        cases = [
            (beijing("2026-11-13", 10, 3, 5), 175.0),
            # Never 0: sitting exactly on a boundary must still advance time,
            # otherwise the loop re-polls the same instant forever.
            (beijing("2026-11-13", 10, 6, 0), 180.0),
            (beijing("2026-11-13", 10, 4, 30), 90.0),
        ]
        for now, expected in cases:
            self.assertAlmostEqual(
                loop.seconds_to_next_boundary(now, 180), expected, places=3,
                msg=f"at {now.isoformat()}")

    def test_a_round_crossing_a_boundary_does_not_immediately_scan_again(self):
        clock = AdvancingClock(beijing("2026-11-13", 10, 2, 59))
        scan_starts = []
        opener = FakeOpener()
        config = make_config(opener, clock=clock, sleep=clock.sleep, max_rounds=2)

        def slow_feed(*args, **kwargs):
            scan_starts.append(clock.now)
            clock.sleep(35)
            quote_time = clock.now.isoformat()
            return {**snapshot(), "quote_time": quote_time, "fetched_at": quote_time}

        config.feed_builder.side_effect = slow_feed
        with loop.Log(None, stdout=io.StringIO()) as log:
            self.assertEqual(loop.run_loop(config, log=log), 0)
        self.assertEqual(scan_starts, [beijing("2026-11-13", 10, 2, 59),
                                      beijing("2026-11-13", 10, 6)])

    def test_loop_stays_resident_after_the_close(self):
        """A closed market must not end the process.

        systemd runs this with Restart=always; returning at the close turns
        every closed-market minute into a start-exit cycle.
        """
        opener = FakeOpener(post={"/round": round_response(),
                                 "/evolve": {"preview": {"reason": "inert"}}})
        # Stop shortly after the close: the assertion is "slept across the
        # close", not "ran the next session".
        clock = AdvancingClock(beijing("2026-11-13", 14, 57),
                              stop_at=beijing("2026-11-13", 15, 5))
        sleeps = []
        config = make_config(
            opener, feed_result=[snapshot()] * 2000, clock=clock,
            sleep=lambda seconds: (sleeps.append(seconds), clock.sleep(seconds)))
        output = io.StringIO()
        with loop.Log(None, stdout=output) as log:
            with self.assertRaises(SimulationOver):
                loop.run_loop(config, log=log)
        waits = [value for value in sleeps if value > 3600.0]
        self.assertTrue(waits, "must sleep across the close, not exit")
        self.assertIn("waiting_for_open", output.getvalue())
        evolve_calls = [call for call in opener.calls
                        if call[1].endswith("/v2/live/evolve")]
        self.assertEqual(len(evolve_calls), 1,
                         "one proposal for the closed day")

    def test_prompt_names_the_fields_rust_consumes(self):
        # A model that answers with its own invented schema is silently
        # fail-closed. The prompt has to name the exact top-level keys the
        # kernel parses, or AI "involvement" is just an unused LLM call.
        prompt = loop.LIVE_SYSTEM_PROMPT
        for field in ("confidence", "bias", "entry_delta", "weight_tilt_bps",
                      "blocked_symbols", "suggestions", "reason"):
            self.assertIn('"' + field + '"', prompt)
        self.assertIn("不要输出 slot_recommendations", prompt)


class ModelJsonRepairTests(unittest.TestCase):
    """A fenced or truncated completion must not silently drop the whole round."""

    def test_plain_object_parses(self):
        self.assertEqual(loop._parse_model_json('{"bias": 5}'), {"bias": 5})

    def test_json_fence_is_stripped(self):
        fenced = '```json\n{"bias": -3, "confidence": 70}\n```'
        self.assertEqual(loop._parse_model_json(fenced),
                         {"bias": -3, "confidence": 70})

    def test_truncated_object_keeps_complete_fields(self):
        # The exact failure seen in production: cut mid-value at 1155 chars.
        truncated = '{"confidence": 78, "bias": -28, "entry_delta": 15, "rea'
        parsed = loop._parse_model_json(truncated)
        self.assertEqual(parsed["confidence"], 78)
        self.assertEqual(parsed["bias"], -28)
        self.assertNotIn("rea", parsed)

    def test_truncated_nested_arrays_are_dropped_not_fabricated(self):
        # The array is cut mid-element. Keeping the incomplete field would
        # invent a symbol; dropping it leaves Rust to treat the field as
        # absent, which is the fail-closed behaviour.
        truncated = '{"confidence": 60, "blocked_symbols": ["600000.SH", "000'
        parsed = loop._parse_model_json(truncated)
        self.assertEqual(parsed["confidence"], 60)
        self.assertNotIn("blocked_symbols", parsed)

    def test_unrecoverable_text_still_raises(self):
        with self.assertRaises(ValueError):
            loop._parse_model_json("not json at all")


class DigestVisibilityTests(unittest.TestCase):
    """The observer must see the market it is judging.

    Before this, the digest said only "positions: 3" and gave no candidate at
    all. The model had nothing to point at, so every round it produced a
    blanket risk-off verdict (bias -12..-45, entry_delta +12..+30 on 37 of 37
    rounds) and the account stopped trading. These tests pin the facts the
    observer now receives — and that absent data is never invented.
    """

    def _quote(self, symbol, price, prev, *, high=None, low=None, name=None,
               closes=None):
        q = {
            "symbol": symbol,
            "price_cents": price,
            "prev_close_cents": prev,
            "tradable": True,
        }
        if high is not None:
            q["high_cents"] = high
        if low is not None:
            q["low_cents"] = low
        if name is not None:
            q["name"] = name
        if closes is not None:
            q["trailing_closes"] = closes
        return q

    def test_ranked_candidates_are_ordered_by_strength(self):
        quotes = [
            self._quote("000001.SZ", 1_000, 1_000, high=1_010, low=990),
            self._quote("600000.SH", 1_050, 1_000, high=1_055, low=998),
            self._quote("600001.SH", 960, 1_000, high=1_005, low=955),
        ]
        ranked = loop.ranked_candidates(quotes)
        self.assertEqual(len(ranked), 3)
        symbols = [item["symbol"] for item in ranked]
        self.assertEqual(symbols[0], "600000.SH", "the +5% mover ranks first")
        self.assertEqual(symbols[-1], "600001.SH", "the -4% mover ranks last")
        self.assertEqual(ranked[0]["momentum_bps"], 500)
        self.assertLess(ranked[-1]["momentum_bps"], 0)

    def test_a_candidate_without_a_previous_close_is_skipped(self):
        quotes = [
            {"symbol": "600000.SH", "price_cents": 1_050, "tradable": True},
            self._quote("000001.SZ", 1_010, 1_000),
        ]
        ranked = loop.ranked_candidates(quotes)
        self.assertEqual([item["symbol"] for item in ranked], ["000001.SZ"])

    def test_absent_components_are_omitted_not_zeroed(self):
        # No high/low and no history: the entry must simply not carry those
        # keys, so a reader can tell "unknown" from "was zero".
        ranked = loop.ranked_candidates([self._quote("600000.SH", 1_010, 1_000)])
        entry = ranked[0]
        self.assertEqual(entry["momentum_bps"], 100)
        self.assertNotIn("position_in_range_bps", entry)
        self.assertNotIn("amplitude_bps", entry)
        self.assertNotIn("trend_5d_bps", entry)
        self.assertNotIn("name", entry)

    def test_digest_shows_only_observed_limit_distance_and_bps_amplitude(self):
        quotes = [
            {**self._quote("600000.SH", 1100, 1000, high=1100, low=1000),
             "limit_up_cents": 1100},
            self._quote("000001.SZ", 1040, 1000, high=1040, low=1000),
        ]
        digest = loop.market_digest({"quotes": quotes},
                                    account={"as_of_round_id": "2026-11-19-145709"},
                                    trades=[])
        first, second = digest["ranked_candidates"]
        self.assertEqual(first["distance_to_limit_up_bps"], 0)
        self.assertNotIn("distance_to_limit_up_bps", second)
        self.assertEqual(digest["average_amplitude_bps"], 700)
        self.assertEqual(digest["account_as_of_round_id"], "2026-11-19-145709")

    def test_candidates_are_capped(self):
        quotes = [
            self._quote("%06d.SZ" % i, 1_000 + i, 1_000) for i in range(60)
        ]
        self.assertEqual(len(loop.ranked_candidates(quotes)), loop.DIGEST_CANDIDATES)

    def test_held_positions_carry_symbol_and_live_pnl(self):
        account = {
            "positions": [
                {
                    "symbol": "600000.SH",
                    "name": "浦发银行",
                    "slot": "steady",
                    "quantity": 300,
                    "sellable_quantity": 0,
                    "avg_cost_cents": 1_000,
                    "last_price_cents": 1_080,
                },
                {
                    "symbol": "000001.SZ",
                    "slot": "aggressive",
                    "quantity": 100,
                    "sellable_quantity": 100,
                    "avg_cost_cents": 1_000,
                    # no live price yet -> no pnl key, never a zero
                },
            ]
        }
        account["positions"][1]["last_price_cents"] = 1050  # stale ledger mark
        held = loop.held_positions(account, [
            self._quote("600000.SH", 1_120, 1_000),
            {**self._quote("000001.SZ", 1_100, 1_000), "tradable": False},
        ])
        self.assertEqual([item["symbol"] for item in held], ["600000.SH", "000001.SZ"])
        self.assertEqual(held[0]["pnl_bps"], 1200)
        self.assertEqual(held[0]["mark_source"], "current_snapshot")
        self.assertEqual(held[0]["name"], "浦发银行")
        self.assertNotIn("pnl_bps", held[1])
        self.assertNotIn("last_price_cents", held[1])

    def test_nontradable_quote_does_not_enter_ai_breadth_or_candidates(self):
        quotes = [{"symbol": "600000.SH", "price_cents": 1100, "prev_close_cents": 1000,
                   "high_cents": 1100, "low_cents": 1000, "tradable": False},
                  {"symbol": "000001.SZ", "price_cents": 900, "prev_close_cents": 1000,
                   "high_cents": 1000, "low_cents": 900, "tradable": True}]
        digest = loop.market_digest({"quotes": quotes}, account={}, trades=[])
        self.assertEqual(digest["change_distribution"], {"up": 0, "down": 1, "flat": 0})
        self.assertEqual([row["symbol"] for row in digest["ranked_candidates"]], ["000001.SZ"])

    def test_digest_takes_newest_fills_from_descending_kernel_list(self):
        trades = [{"round_id": f"r-{n}", "symbol": "600000.SH"}
                  for n in range(12, 0, -1)]
        digest = loop.market_digest({"quotes": []}, account={}, trades=trades)
        self.assertEqual([fill["round_id"] for fill in digest["recent_trades"]],
                         [f"r-{n}" for n in range(12, 4, -1)])

    def test_the_digest_carries_names_and_positions_to_the_model(self):
        snapshot = {
            "quotes": [
                self._quote("600000.SH", 1_050, 1_000, high=1_055, low=998,
                            name="浦发银行"),
            ],
            "quote_time": "2026-11-16T10:00:00+08:00",
            "quote_time_estimated": True,
        }
        account = {
            "slots": [{"slot": "steady", "cash_cents": 1, "equity_cents": 2,
                       "positions": 1}],
            "positions": [
                {"symbol": "600000.SH", "slot": "steady", "quantity": 300,
                 "sellable_quantity": 0, "avg_cost_cents": 1_000,
                 "last_price_cents": 1_050},
            ],
        }
        digest = loop.market_digest(snapshot, account=account, trades=[])
        self.assertIn("ranked_candidates", digest)
        self.assertIn("held_positions", digest)
        self.assertEqual(digest["ranked_candidates"][0]["symbol"], "600000.SH")
        self.assertEqual(digest["ranked_candidates"][0]["name"], "浦发银行")
        self.assertEqual(digest["held_positions"][0]["pnl_bps"], 500)
        self.assertEqual(digest["held_positions"][0]["sellable_quantity"], 0)


class EmptyCompletionTests(unittest.TestCase):
    """A reasoning model can spend the whole budget thinking and emit nothing.

    Without this guard the round logged a bare JSONDecodeError and the operator
    could not tell an empty completion from a broken endpoint. The message must
    name finish_reason and the reasoning token count.
    """

    def _opener(self, payload):
        class FakeResponse:
            def __init__(self, body):
                self._body = body

            def read(self):
                return self._body

            def __enter__(self):
                return self

            def __exit__(self, *exc):
                return False

        def opener(request, timeout=None):
            return FakeResponse(json.dumps(payload).encode())

        return opener

    def test_an_empty_completion_names_the_real_cause(self):
        payload = {
            "choices": [{
                "message": {"content": ""},
                "finish_reason": "length",
            }],
            "usage": {"completion_tokens_details": {"reasoning_tokens": 700}},
        }
        events = []

        class CaptureLog:
            def emit(self, event, **fields):
                events.append((event, fields))

        result = loop.raw_advice(
            {"sample_count": 1},
            base_url="http://example.invalid/v1",
            api_key="k",
            model="m",
            opener=self._opener(payload),
            log=CaptureLog(),
        )
        self.assertIsNone(result)
        self.assertTrue(events)
        event, fields = events[-1]
        self.assertEqual(event, "ai_unavailable")
        reason = fields["reason"]
        self.assertIn("empty_completion", reason)
        self.assertIn("finish_reason=length", reason)
        self.assertIn("reasoning_tokens=700", reason)

    def test_a_whitespace_only_completion_is_treated_as_empty(self):
        payload = {
            "choices": [{"message": {"content": " "}, "finish_reason": "length"}],
        }
        events = []

        class CaptureLog:
            def emit(self, event, **fields):
                events.append((event, fields))

        result = loop.raw_advice(
            {"sample_count": 1},
            base_url="http://example.invalid/v1",
            api_key="k",
            model="m",
            opener=self._opener(payload),
            log=CaptureLog(),
        )
        self.assertIsNone(result)
        self.assertEqual(events[-1][0], "ai_unavailable")
        self.assertIn("empty_completion", events[-1][1]["reason"])

    def test_a_normal_answer_still_parses(self):
        payload = {
            "choices": [{
                "message": {"content": '{"bias": 5, "confidence": 70}'},
                "finish_reason": "stop",
            }],
        }
        result = loop.raw_advice(
            {"sample_count": 1},
            base_url="http://example.invalid/v1",
            api_key="k",
            model="m",
            opener=self._opener(payload),
        )
        self.assertIsNotNone(result)
        self.assertEqual(result["raw_advice"]["bias"], 5)


class SummaryCompletenessTests(unittest.TestCase):
    """Every anti-trap field the prompt names must reach the model."""

    def snapshot(self):
        return {"quote_time": "2026-11-23T10:33:00+08:00", "quotes": [
            {"symbol": "600000.SH", "name": "示例", "price_cents": 1_000,
             "prev_close_cents": 990, "open_cents": 995, "high_cents": 1_010,
             "low_cents": 990, "limit_up_cents": 1_089, "limit_down_cents": 891,
             "volume": 100_000, "amount_cents": 100_000 * 1_002, "tradable": True,
             "trailing_closes": [900, 920, 950, 970, 990]},
        ]}

    def test_candidates_carry_vwap_wick_and_trap_risk(self):
        digest = loop.market_digest(self.snapshot(), account=None, trades=None)
        candidate = digest["ranked_candidates"][0]
        self.assertEqual(candidate["vwap_distance_bps"], -20)
        self.assertIn("upper_wick_bps", candidate)
        self.assertIn("bull_trap_risk", candidate)
        self.assertEqual(digest["market_table"]["cols"][0], "symbol")
        row = digest["market_table"]["rows"][0]
        self.assertEqual(row[digest["market_table"]["cols"].index("vwap_distance_bps")], -20)

    def test_a_far_off_turnover_price_is_reported_unknown_not_a_huge_gap(self):
        snapshot = self.snapshot()
        snapshot["quotes"][0]["amount_cents"] *= 100
        candidate = loop.market_digest(snapshot, account=None, trades=None)["ranked_candidates"][0]
        self.assertNotIn("vwap_distance_bps", candidate)


from live_ai import digest_messages  # noqa: E402
import live_ai as ai  # noqa: E402


class SentimentLimitTests(unittest.TestCase):
    """A limit-up count must follow the published band, not a flat 9.5%."""

    def quote(self, symbol, price, prev, limit_up=None, limit_down=None):
        return {"symbol": symbol, "price_cents": price, "prev_close_cents": prev,
                "limit_up_cents": limit_up, "limit_down_cents": limit_down,
                "high_cents": price, "open_cents": prev, "tradable": True}

    def test_only_names_at_their_published_ceiling_are_counted(self):
        quotes = [self.quote("600001.SH", 1050, 1000, limit_up=1050),   # at ceiling
                  self.quote("600002.SH", 1040, 1000, limit_up=1050)]   # 4%, below
        self.assertEqual(ai.compute_market_sentiment(quotes)["limit_up_count"], 1)

    def test_the_flat_fallback_only_applies_without_a_published_limit(self):
        # With a published limit, the band decides; a 30% board name at +10% is
        # nowhere near its ceiling and must not be counted.
        quotes = [self.quote("830001.BJ", 1100, 1000, limit_up=1300)]
        result = ai.compute_market_sentiment(quotes)
        self.assertEqual(result["limit_up_count"], 0)
        # Without a published limit the 9.5% fallback still applies.
        quotes = [self.quote("600003.SH", 1100, 1000)]
        result = ai.compute_market_sentiment(quotes)
        self.assertEqual(result["limit_up_count"], 1)


class PromptCacheShapeTests(unittest.TestCase):
    """A cached prefix is the whole point of a 160k-token round."""

    def digest(self):
        return {"quote_time": "2026-11-23T13:00:09+08:00", "sample_count": 5219,
                "market_table": {"cols": ["symbol"], "rows": [["600000.SH", 100]]},
                "slots": [{"slot": "steady"}], "held_positions": [],
                "recent_bull_trap_lessons": [], "account_as_of_round_id": "r1",
                "change_distribution": {"up": 1, "down": 2, "flat": 0}}

    def test_stable_block_is_identical_across_rounds_and_leads(self):
        first = ai.digest_messages("SYS", self.digest())
        second = ai.digest_messages("SYS", {**self.digest(), "quote_time": "13:03"})
        self.assertEqual(first[0], second[0])
        self.assertNotEqual(first[1], second[1], "the volatile tail must change")
        payload = json.loads(first[1]["content"])
        self.assertEqual(list(payload)[0], "stable")
        self.assertIn("market_table", payload["stable"])
        self.assertNotIn("slots", payload["stable"], "cash changes every fill")
        self.assertNotIn("quote_time", payload["stable"])


class PromptContractTests(unittest.TestCase):
    """The kernel adds entry_delta to the threshold, so the sign must be spelled
    out: a defensive round that returns a negative delta silently loosens the
    entry gate and buys more."""

    def test_prompt_states_the_direction_of_entry_delta(self):
        prompt = loop.LIVE_SYSTEM_PROMPT
        self.assertIn("正值提高开仓门槛", prompt)
        self.assertIn("负值降低门槛", prompt)
        self.assertIn("reason 说防守", prompt)


class AdviceRetryTests(unittest.TestCase):
    empty = {"choices": [{"message": {"content": ""}, "finish_reason": "length"}],
             "usage": {"completion_tokens_details": {"reasoning_tokens": 2_000}}}
    good = {"choices": [{"message": {"content": '{"bias": 5, "confidence": 71}'},
                         "finish_reason": "stop"}]}

    def invoke(self, fixtures, *, model="deepseek-v4.1-flash", durations=()):
        events, sent = [], []
        responses = iter(fixtures)
        durations = iter(durations)
        now = 0.0

        class CaptureLog:
            def emit(self, event, **fields):
                events.append((event, fields))

        def opener(request, timeout=None):
            nonlocal now
            sent.append((json.loads(request.data), timeout))
            now += next(durations, 0)
            fixture = next(responses)
            if isinstance(fixture, Exception):
                raise fixture
            if isinstance(fixture, bytes):
                return io.BytesIO(fixture)
            return io.BytesIO(json.dumps(fixture).encode())

        result = loop.raw_advice({"sample_count": 3}, base_url="http://llm.invalid/v1",
                                 api_key="test", model=model, opener=opener,
                                 log=CaptureLog(), round_id="r1", monotonic=lambda: now)
        return result, sent, events

    def test_normal_response_uses_one_request_and_unchanged_first_payload(self):
        result, sent, events = self.invoke([self.good], durations=(7.06,))
        self.assertEqual(result, {"model": "deepseek-v4.1-flash",
                                  "raw_advice": {"bias": 5, "confidence": 71}})
        self.assertEqual([call[1] for call in sent], [30])
        self.assertEqual(sent[0][0]["thinking"], {"type": "disabled"})
        self.assertEqual(sent[0][0]["max_tokens"], 1_200)
        self.assertEqual(json.loads(sent[0][0]["messages"][1]["content"]),
                         {"stable": {}, "round": {"sample_count": 3}})
        self.assertEqual(events[0], ("ai_completed", {"round_id": "r1", "attempts": 1,
                                                     "elapsed_ms": 7_060}))
        event, fields = events[-1]
        self.assertEqual(event, "ai_usage")
        self.assertEqual(fields["elapsed_ms"], 7_060)
        self.assertIsNone(fields["total_tokens"])

    def test_empty_retry_drops_the_switch_and_widens_the_budget(self):
        result, sent, events = self.invoke([self.empty, self.good], durations=(7.06, 3.84))
        self.assertEqual(result["raw_advice"], {"bias": 5, "confidence": 71})
        self.assertEqual([call[1] for call in sent], [30, 15])
        self.assertEqual(sent[0][0]["thinking"], {"type": "disabled"})
        self.assertNotIn("thinking", sent[1][0])
        self.assertEqual(sent[1][0]["max_tokens"], 8_000)
        self.assertEqual(sent[1][0]["messages"], sent[0][0]["messages"])
        self.assertEqual([event for event, _ in events],
                         ["ai_usage", "ai_retry", "ai_completed", "ai_usage"])
        self.assertTrue(all(fields["round_id"] == "r1" for _, fields in events))
        self.assertEqual(events[0][1]["reasoning_tokens"], 2_000)
        self.assertIn("reasoning_tokens=2000", events[1][1]["reason"])
        self.assertEqual(events[-2][1]["attempts"], 2)
        self.assertEqual(events[-2][1]["elapsed_ms"], 10_900)

    def test_second_failure_deterministically_returns_no_advice(self):
        result, sent, events = self.invoke([self.empty, self.empty], durations=(7.06, 3.84))
        self.assertIsNone(result)
        self.assertEqual(len(sent), 2)
        self.assertEqual([event for event, _ in events],
                         ["ai_usage", "ai_retry", "ai_usage", "ai_unavailable"])
        self.assertEqual(events[-1][1]["attempts"], 2)
        self.assertIn("empty_completion", events[-1][1]["reason"])

    def test_network_parse_and_other_models_keep_the_single_request_path(self):
        for fixture, model in ((TimeoutError("gateway timeout"), "deepseek-v4.1-flash"),
                               (b"not json", "deepseek-v4.1-flash"),
                               (self.empty, "other-model")):
            with self.subTest(model=model, fixture=type(fixture).__name__):
                result, sent, events = self.invoke([fixture], model=model)
                self.assertIsNone(result)
                self.assertEqual([call[1] for call in sent], [30])
                self.assertEqual(events[-1][0], "ai_unavailable")

    def test_empty_or_malformed_usage_never_throws_or_retries(self):
        for usage in (None, [], {}, {"completion_tokens_details": None},
                      {"completion_tokens_details": []},
                      {"completion_tokens_details": {"reasoning_tokens": True}},
                      {"completion_tokens_details": {"reasoning_tokens": -1}},
                      {"completion_tokens_details": {"reasoning_tokens": "2000"}}):
            with self.subTest(usage=usage):
                result, sent, events = self.invoke([{**self.empty, "usage": usage}])
                self.assertIsNone(result)
                self.assertEqual(len(sent), 1)
                self.assertEqual(events[-1][0], "ai_unavailable")
                self.assertIn("reasoning_tokens=None", events[-1][1]["reason"])

    def test_fallback_shares_the_remaining_budget_or_is_skipped(self):
        _, sent, _ = self.invoke([self.empty, self.good], durations=(36.0, 1.0))
        self.assertEqual([call[1] for call in sent], [30, 9])
        result, sent, events = self.invoke([self.empty], durations=(45.0,))
        self.assertIsNone(result)
        self.assertEqual(len(sent), 1)
        self.assertEqual(events[-1][0], "ai_unavailable")
        self.assertEqual(events[-1][1]["attempts"], 1)
        self.assertEqual(events[-1][1]["reason"], "reasoning_retry_budget_exhausted")

    def test_usage_is_recorded_per_attempt_without_response_or_credentials(self):
        initial = {**self.empty, "usage": {
            "prompt_tokens": 1200, "completion_tokens": 2000, "total_tokens": 3200,
            "completion_thinking_tokens": 2000, "prompt_cache_hit_tokens": 1024,
            "other": "private provider data",
        }}
        recovered = {**self.good, "usage": {
            "input_tokens": 1200, "output_tokens": 70,
            "completion_tokens_details": {"reasoning_tokens": 0},
            "prompt_tokens_details": {"cached_tokens": 800},
        }}
        result, sent, events = self.invoke([initial, recovered], durations=(7, 4))
        self.assertIsNotNone(result)
        self.assertEqual(len(sent), 2)
        usage = [fields for event, fields in events if event == "ai_usage"]
        self.assertEqual(usage, [
            {"round_id": "r1", "attempt": 1, "model": "deepseek-v4.1-flash",
             "status": "empty_completion", "elapsed_ms": 7000,
             "prompt_tokens": 1200, "completion_tokens": 2000, "total_tokens": 3200,
             "reasoning_tokens": 2000, "cached_prompt_tokens": 1024},
            {"round_id": "r1", "attempt": 2, "model": "deepseek-v4.1-flash",
             "status": "completed", "elapsed_ms": 4000,
             "prompt_tokens": 1200, "completion_tokens": 70, "total_tokens": None,
             "reasoning_tokens": 0, "cached_prompt_tokens": 800},
        ])
        _, _, events = self.invoke([TimeoutError("secret detail")])
        fields = next(fields for event, fields in events if event == "ai_usage")
        self.assertEqual(fields["status"], "request_error")
        self.assertIsNone(fields["prompt_tokens"])
        self.assertNotIn("secret detail", str(events))

    def test_round_submits_recovered_advice_once(self):
        fixtures = iter((self.empty, self.good))
        provider_calls = []
        kernel_opener = FakeOpener(get={"/account": {}, "/trades": []},
                                   post={"/round": round_response()})

        def opener(request, timeout=None):
            if request.full_url.endswith("/chat/completions"):
                provider_calls.append(json.loads(request.data))
                return io.BytesIO(json.dumps(next(fixtures)).encode())
            return kernel_opener(request, timeout=timeout)

        config = make_config(opener, ai_on=True)
        with loop.Log(None, stdout=io.StringIO()) as log:
            self.assertTrue(loop.run_round(
                config=config, kernel=loop.Kernel(config.api, opener=opener),
                log=log, state=loop.RoundState(), snapshot=config.feed_builder()))
        self.assertEqual(len(provider_calls), 2)
        posts = [call[2] for call in kernel_opener.calls
                 if call[1].endswith("/v2/live/round")]
        self.assertEqual(len(posts), 1)
        self.assertEqual(posts[0]["advice"], {
            "model": config.llm_model, "raw_advice": {"bias": 5, "confidence": 71}})


class NewsWatchScopeTests(unittest.TestCase):
    """The comment said "holdings plus what the ranking surfaced", but only the
    holdings were passed. A candidate could therefore be bought on a round whose
    news block never looked at it."""

    def digest(self, held, ranked):
        return {
            "held_positions": [{"symbol": symbol} for symbol in held],
            "ranked_candidates": [{"symbol": symbol} for symbol in ranked],
        }

    def test_candidates_join_the_watch_list_alongside_holdings(self):
        held, candidates = loop.news_watch_scope(
            self.digest(["600000.SH"], ["002242.SZ", "300750.SZ"]))
        self.assertEqual(held, ["600000.SH"])
        self.assertEqual(candidates, ["002242.SZ", "300750.SZ"])
        self.assertEqual(held + candidates,
                         ["600000.SH", "002242.SZ", "300750.SZ"])

    def test_a_held_name_is_not_repeated_in_the_candidate_list(self):
        held, candidates = loop.news_watch_scope(
            self.digest(["600000.SH"], ["600000.SH", "002242.SZ"]))
        self.assertEqual(held, ["600000.SH"])
        self.assertEqual(candidates, ["002242.SZ"])

    def test_the_candidate_list_is_capped_without_dropping_a_holding(self):
        ranked = [f"{i:06d}.SZ" for i in range(80)]
        held, candidates = loop.news_watch_scope(self.digest(["600000.SH"], ranked))
        self.assertEqual(len(candidates), loop.NEWS_CANDIDATE_LIMIT)
        self.assertEqual(held, ["600000.SH"], "holdings are never capped away")

    def test_the_round_passes_holdings_and_candidates_with_per_symbol_on_holdings(self):
        # Behavioural: run a real round with the AI path on and read what the
        # news transport was actually given.
        opener = FakeOpener(get={
            "/account": {"positions": [{"symbol": "600000.SH", "quantity": 100,
                                        "sellable_quantity": 100,
                                        "avg_cost_cents": 1000}]},
            "/trades": [],
        }, post={"/settings/ai-advice": {"ok": False}, "/round": round_response()})
        config = make_config(opener, ai_on=True,
                             feed_result=snapshot(quotes=[
                                 {"symbol": "600000.SH", "price_cents": 1010,
                                  "prev_close_cents": 1000, "high_cents": 1020,
                                  "low_cents": 990, "tradable": True},
                                 {"symbol": "002242.SZ", "price_cents": 1050,
                                  "prev_close_cents": 1000, "high_cents": 1055,
                                  "low_cents": 998, "tradable": True},
                             ]))
        seen = {}

        def fake_news(snapshot_, watch, *, per_symbol_watch=None):
            seen["watch"] = list(watch)
            seen["per_symbol"] = list(per_symbol_watch or [])
            return None

        with mock.patch.object(loop, "fetch_news_block", side_effect=fake_news), \
             mock.patch.object(loop, "fetch_heat_block", return_value=None), \
             mock.patch.object(loop, "fetch_northbound_block", return_value=None), \
             loop.Log(None, stdout=io.StringIO()) as log:
            self.assertTrue(loop.run_round(
                config=config, kernel=loop.Kernel(config.api, opener=opener),
                log=log, state=loop.RoundState(), snapshot=config.feed_builder()))
        self.assertIn("002242.SZ", seen["watch"], "a ranked candidate must be covered")
        self.assertIn("600000.SH", seen["watch"])
        self.assertEqual(seen["per_symbol"], ["600000.SH"],
                         "the slow per-symbol request stays on the holdings")

    def test_an_unreadable_account_still_watches_the_ranked_names(self):
        opener = FakeOpener(get={"/trades": []},
                            post={"/settings/ai-advice": {"ok": False},
                                  "/round": round_response()},
                            fail_get=("/account",))
        config = make_config(opener, ai_on=True)
        seen = {}

        def fake_news(snapshot_, watch, *, per_symbol_watch=None):
            seen["watch"] = list(watch)
            seen["per_symbol"] = list(per_symbol_watch or [])
            return None

        with mock.patch.object(loop, "fetch_news_block", side_effect=fake_news), \
             mock.patch.object(loop, "fetch_heat_block", return_value=None), \
             mock.patch.object(loop, "fetch_northbound_block", return_value=None), \
             loop.Log(None, stdout=io.StringIO()) as log:
            loop.run_round(config=config, kernel=loop.Kernel(config.api, opener=opener),
                           log=log, state=loop.RoundState(),
                           snapshot=config.feed_builder())
        self.assertEqual(len(seen["watch"]), 2, "both ranked names are watched")
        self.assertEqual(seen["per_symbol"], [], "no holdings, no per-symbol request")


if __name__ == "__main__":
    unittest.main()

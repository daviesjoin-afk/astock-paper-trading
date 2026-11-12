"""Independent, read-only audit of regime_ab JSON using fills and source bars.

The audit does not import Rust calculations. Money/positions are rebuilt from
the exported ledger; Fraction supplies exact arithmetic for time-weighted
returns. Results distinguish accounting consistency from quote tradeability.
"""
from __future__ import annotations

import argparse
from collections import Counter, defaultdict
from contextlib import closing
from decimal import Decimal, localcontext
from fractions import Fraction
import hashlib
import json
from pathlib import Path
import sqlite3
import sys


SLOTS = ("steady", "aggressive")
SERIES = ("account", *SLOTS)
# Rust uses 18-decimal fixed-point unit values. Adjacent displayed integer
# bps are tolerated only at an almost-exact integer boundary, not for an
# arbitrary one-bp discrepancy. All such differences remain visible.
BOUNDARY_EPSILON_BPS = Fraction(1, 1_000_000_000)


class VerificationError(Exception):
    pass


def decimal_text(value: Fraction, places: int = 12) -> str:
    with localcontext() as context:
        context.prec = 60
        return format(Decimal(value.numerator) / Decimal(value.denominator), f".{places}f")


class Audit:
    def __init__(self):
        self.checks = Counter()
        self.errors = []
        self.error_count = 0
        self.precision_differences = []
        self.precision_difference_count = 0

    def equal(self, category, where, expected, actual):
        self.checks[category] += 1
        if expected != actual:
            self.error_count += 1
            if len(self.errors) < 50:
                self.errors.append(dict(category=category, where=where,
                                        expected=expected, reported=actual))

    def bps(self, where, exact, actual, allow_boundary):
        expected = int(exact)  # int(Fraction) truncates toward zero, as Rust does.
        self.checks["performance_bps"] += 1
        if actual == expected:
            return
        near_boundary = abs(exact - round(exact)) <= BOUNDARY_EPSILON_BPS
        if allow_boundary and near_boundary and abs(actual - expected) == 1:
            self.precision_difference_count += 1
            if len(self.precision_differences) < 30:
                self.precision_differences.append(dict(
                    where=where, exact_bps=decimal_text(exact, 18),
                    truncated_bps=expected, reported_bps=actual,
                    explanation="Adjacent integer at fixed-point rounding boundary"))
            return
        self.error_count += 1
        if len(self.errors) < 50:
            self.errors.append(dict(category="performance_bps", where=where,
                                    exact_bps=decimal_text(exact, 18),
                                    expected=expected, reported=actual))


class Performance:
    def __init__(self, initial):
        self.initial = initial
        self.previous = initial
        self.net_transferred = 0
        self.nav = Fraction(1)
        self.peak = Fraction(1)
        self.peak_day = None
        self.exact_worst_bps = Fraction(0)
        self.exact_peak_day = None
        self.exact_trough_day = None
        self.display_worst_bps = 0
        self.display_peak_day = None
        self.display_trough_day = None
        self.last = None

    def mark(self, day, equity, flow):
        # End-of-day transfer is removed before measuring today's performance.
        # A zero denominator is undefined and fails explicitly, never filled.
        self.nav *= Fraction(equity - flow, self.previous)
        self.net_transferred += flow
        if self.nav > self.peak:
            self.peak = self.nav
            self.peak_day = day
        drawdown = (self.peak - self.nav) * 10_000 / self.peak
        if drawdown > self.exact_worst_bps:
            self.exact_worst_bps = drawdown
            self.exact_peak_day = self.peak_day
            self.exact_trough_day = day
        # The API updates the event only when the displayed integer bps grow.
        # Keep the exact event separately so that sub-bp date ties are visible.
        if int(drawdown) > self.display_worst_bps:
            self.display_worst_bps = int(drawdown)
            self.display_peak_day = self.peak_day
            self.display_trough_day = day
        self.previous = equity
        self.last = dict(day=day, equity_cents=equity, net_flow_cents=flow,
                         profit_cents=equity - self.initial - self.net_transferred,
                         return_bps=(self.nav - 1) * 10_000,
                         max_drawdown_bps=self.exact_worst_bps,
                         peak_day=self.display_peak_day,
                         trough_day=self.display_trough_day)
        return self.last

    def summary(self):
        return dict(
            equity_cents=self.previous,
            profit_cents=self.previous - self.initial - self.net_transferred,
            net_transferred_cents=self.net_transferred,
            exact_return_bps=decimal_text((self.nav - 1) * 10_000),
            truncated_return_bps=int((self.nav - 1) * 10_000),
            exact_max_drawdown_bps=decimal_text(self.exact_worst_bps),
            displayed_max_drawdown_bps=self.display_worst_bps,
            exact_peak_day=self.exact_peak_day, exact_trough_day=self.exact_trough_day,
            displayed_peak_day=self.display_peak_day,
            displayed_trough_day=self.display_trough_day,
            exact_and_display_dates_differ=(self.exact_peak_day, self.exact_trough_day)
            != (self.display_peak_day, self.display_trough_day))


def compare_point(audit, where, independent, reported, allow_boundary):
    for field in ("day", "equity_cents", "net_flow_cents", "profit_cents",
                  "peak_day", "trough_day"):
        audit.equal("daily_accounting_and_dates", f"{where}.{field}",
                    independent[field], reported[field])
    audit.bps(f"{where}.return_bps", independent["return_bps"],
              reported["return_bps"], allow_boundary)
    audit.bps(f"{where}.max_drawdown_bps", independent["max_drawdown_bps"],
              reported["max_drawdown_bps"], allow_boundary)


def verify_variant(name, variant, report, connection):
    audit = Audit()
    days = [item["day"] for item in report["input"]["windows"]]
    initial = report["input"]["initial_cash_cents"]
    budgets = {"steady": initial // 2, "aggressive": initial - initial // 2}
    cash = budgets.copy()
    quantities = {slot: Counter() for slot in SLOTS}
    weights = {slot: 5_000 for slot in SLOTS}
    performance = {key: Performance(initial if key == "account" else budgets[key])
                   for key in SERIES}
    trades_by_day = defaultdict(list)
    for trade in variant["trades"]:
        if trade["trade_day"] not in days:
            raise VerificationError(f"{name}: trade outside selected days: {trade}")
        trades_by_day[trade["trade_day"]].append(trade)
    transfers_by_day = defaultdict(list)
    for transfer in variant["rebalances"]:
        if transfer["applied_day"] not in days:
            raise VerificationError(f"{name}: transfer outside selected days: {transfer}")
        transfers_by_day[transfer["applied_day"]].append(transfer)
    for series in SERIES:
        audit.equal("coverage", f"{name}.{series}.days", days,
                    [point["day"] for point in variant["curves"][series]])
    if any(len(variant["curves"][key]) != len(days) for key in SERIES):
        raise VerificationError(f"{name}: curve length differs from input dates")

    quote_cache = {}
    def quote(symbol, day):
        key = (symbol, day)
        if key not in quote_cache:
            row = connection.execute(
                "SELECT close_cents,open_cents,volume_shares,adjust,source,prev_close_cents,tradable FROM bar WHERE symbol=? AND day=?",
                key).fetchone()
            if row is None:
                raise VerificationError(f"required source quote missing: {symbol}:{day}")
            if row[3] != "none" or row[0] <= 0:
                raise VerificationError(f"required unadjusted positive close missing: {symbol}:{day}:{row}")
            quote_cache[key] = row
        return quote_cache[key]

    counts, fees, invested = Counter(), Counter(), Counter()
    untradeable = []
    untradeable_count = 0
    untradeable_sides = Counter()
    halted_fill_count = 0
    halted_fill_examples = []
    halted_holding_marks = 0
    halted_holding_slot_symbol_days = set()
    halted_holding_by_slot = Counter()
    halted_holding_examples = []
    pending_halts = {}
    halted_resumptions = []
    opening_reference_comparisons = 0
    reference_resets = []
    for index, day in enumerate(days):
        # Observe positions carried into the day, before any fill or transfer.
        # The selected period contains every source market date; its first day
        # starts with cash only, so there is no opening position to compare.
        if index > 0:
            prior_day = days[index - 1]
            for slot in SLOTS:
                for symbol, quantity in quantities[slot].items():
                    if quantity <= 0:
                        continue
                    prior_close = quote(symbol, prior_day)[0]
                    reference_prev_close = quote(symbol, day)[5]
                    opening_reference_comparisons += 1
                    if prior_close != reference_prev_close:
                        price_difference = reference_prev_close - prior_close
                        notional_difference = quantity * price_difference
                        reference_resets.append(dict(
                            slot=slot, symbol=symbol, day=day, prior_market_day=prior_day,
                            quantity=quantity, prior_close_cents=prior_close,
                            reference_prev_close_cents=reference_prev_close,
                            absolute_price_change_bps=decimal_text(
                                Fraction(abs(price_difference) * 10_000, prior_close)),
                            notional_reference_difference_cents=notional_difference,
                            absolute_notional_reference_difference_cents=abs(notional_difference),
                            opening_notional_at_prior_close_cents=quantity * prior_close))
        # A pending halt begins on the first held non-tradable row and ends on
        # the next row that is usable for a fill, whether or not the position
        # is sold that day. This is also a direct check that the ledger did not
        # leave a halted position permanently stuck after its quote resumed.
        for key, halted_day in list(pending_halts.items()):
            slot, symbol = key
            if quantities[slot][symbol] <= 0:
                raise VerificationError(
                    f"{name}: pending halt lost its position: {slot}:{symbol}")
            bar = quote(symbol, day)
            if bar[6] == 1:
                halted_resumptions.append(dict(
                    slot=slot, symbol=symbol, halted_from=halted_day,
                    resumed_day=day, quantity_held=quantities[slot][symbol],
                    resumed_close_cents=bar[0]))
                del pending_halts[key]

        sellable = {slot: quantities[slot].copy() for slot in SLOTS}
        for trade in trades_by_day[day]:
            slot, symbol, side = trade["slot"], trade["symbol"], trade["side"]
            quantity, price, fee = trade["quantity"], trade["price_cents"], trade["fees_cents"]
            if slot not in SLOTS or side not in ("buy", "sell"):
                raise VerificationError(f"invalid fill identity: {trade}")
            bar = quote(symbol, day)
            audit.equal("fill_price", f"{name}:{trade['id']}", bar[0], price)
            audit.equal("fill_tradability", f"{name}:{trade['id']}", True, bar[6] == 1)
            notional = quantity * price
            expected_fee = max(notional * 3 // 10_000, 500)
            if side == "sell":
                expected_fee += notional * 5 // 10_000
            audit.equal("fill_fee", f"{name}:{trade['id']}", expected_fee, fee)
            audit.equal("board_lots", f"{name}:{trade['id']}", True,
                        quantity > 0 and quantity % 100 == 0)
            if side == "buy":
                cash[slot] -= notional + fee
                quantities[slot][symbol] += quantity
            else:
                audit.equal("t_plus_one", f"{name}:{trade['id']}", True,
                            sellable[slot][symbol] >= quantity)
                cash[slot] += notional - fee
                quantities[slot][symbol] -= quantity
                sellable[slot][symbol] -= quantity
                audit.equal("no_short_position", f"{name}:{trade['id']}", True,
                            quantities[slot][symbol] >= 0)
            audit.equal("no_overdraft", f"{name}:{trade['id']}", True, cash[slot] >= 0)
            counts[slot] += 1
            fees[slot] += fee
            if bar[6] == 0:
                halted_fill_count += 1
                if len(halted_fill_examples) < 15:
                    halted_fill_examples.append(dict(
                        trade=trade, close_cents=bar[0],
                        prev_close_cents=bar[5], tradable=bar[6]))
            if bar[1] <= 0 or bar[2] <= 0:
                untradeable_count += 1
                untradeable_sides[side] += 1
                if len(untradeable) < 15:
                    untradeable.append(dict(trade=trade, close_cents=bar[0],
                                            open_cents=bar[1], volume_shares=bar[2],
                                            source=bar[4]))
        flows = Counter()
        for transfer in transfers_by_day[day]:
            for slot in SLOTS:
                audit.equal("transfer_weights", f"{name}:{transfer['id']}:old:{slot}",
                            weights[slot], transfer[f"old_{slot}_bps"])
            source, target = transfer["from"], transfer["to"]
            expected_source = "aggressive" if transfer["new_steady_bps"] > weights["steady"] else "steady"
            audit.equal("transfer_direction", f"{name}:{transfer['id']}",
                        expected_source, source)
            audit.equal("transfer_direction", f"{name}:{transfer['id']}:target",
                        next(slot for slot in SLOTS if slot != source), target)
            moved = transfer["moved_cents"]
            cash[source] -= moved
            cash[target] += moved
            flows[source] -= moved
            flows[target] += moved
            audit.equal("no_overdraft", f"{name}:transfer:{transfer['id']}", True, cash[source] >= 0)
            weights = {slot: transfer[f"new_{slot}_bps"] for slot in SLOTS}
        equities = {}
        for slot in SLOTS:
            market = 0
            for symbol, quantity in quantities[slot].items():
                if not quantity:
                    continue
                bar = quote(symbol, day)
                if bar[6] == 0:
                    # The carried close is the only permitted mark for that
                    # day. It must equal the package's official previous
                    # close; a different value would be an invented price.
                    audit.equal("halted_reference_price",
                                f"{name}:{slot}:{symbol}:{day}", bar[5], bar[0])
                    halted_holding_marks += 1
                    halted_holding_slot_symbol_days.add((slot, symbol, day))
                    halted_holding_by_slot[slot] += 1
                    pending_halts.setdefault((slot, symbol), day)
                    if len(halted_holding_examples) < 15:
                        halted_holding_examples.append(dict(
                            slot=slot, symbol=symbol, day=day,
                            quantity=quantity, close_cents=bar[0],
                            prev_close_cents=bar[5],
                            market_value_cents=quantity * bar[0]))
                market += quantity * bar[0]
            equities[slot] = cash[slot] + market
            invested[slot] += int(any(quantity > 0 for quantity in quantities[slot].values()))
        equities["account"] = sum(equities[slot] for slot in SLOTS)
        for series in SERIES:
            independent = performance[series].mark(day, equities[series], flows[series])
            compare_point(audit, f"{name}:{series}:{day}", independent,
                          variant["curves"][series][index], series != "account")

    books = {book["slot"]: book for book in variant["books"]}
    summaries = {item["slot"]: item for item in variant["slots"]}
    for slot in SLOTS:
        book, summary = books[slot], summaries[slot]
        audit.equal("final_book", f"{name}:{slot}:cash", cash[slot], book["cash_cents"])
        audit.equal("final_book", f"{name}:{slot}:quantities",
                    {symbol: quantity for symbol, quantity in quantities[slot].items() if quantity},
                    {position["symbol"]: position["quantity"] for position in book["positions"]})
        audit.equal("final_book", f"{name}:{slot}:sellable",
                    {symbol: quantity for symbol, quantity in sellable[slot].items() if quantity},
                    {position["symbol"]: position["quantity"] for position in book["sellable"]})
        for field, expected in (("fills", counts[slot]), ("fees_cents", fees[slot]),
                                ("invested_days", invested[slot]),
                                ("open_positions", sum(q > 0 for q in quantities[slot].values())),
                                ("net_transferred_cents", performance[slot].net_transferred)):
            audit.equal("final_summary", f"{name}:{slot}:{field}", expected, summary[field])
        compare_point(audit, f"{name}:{slot}:summary", performance[slot].last,
                      summary["performance"], True)
    compare_point(audit, f"{name}:account:summary", performance["account"].last,
                  variant["account"], False)
    audit.equal("final_summary", f"{name}:weights", [weights[slot] for slot in SLOTS], variant["weights_bps"])
    audit.equal("final_summary", f"{name}:fills", sum(counts.values()), variant["fills"])
    audit.equal("final_summary", f"{name}:fees", sum(fees.values()), variant["fees_cents"])
    accounting_passed = audit.error_count == 0
    return dict(
        passed=accounting_passed and untradeable_count == 0 and halted_fill_count == 0,
        accounting_and_performance_passed=accounting_passed,
        quote_tradeability_passed=untradeable_count == 0 and halted_fill_count == 0,
        checks=dict(audit.checks), mismatch_count=audit.error_count,
        mismatches=audit.errors, display_precision_difference_count=audit.precision_difference_count,
        display_precision_differences=audit.precision_differences,
        independently_marked_source_rows=len(quote_cache),
        final_cash_cents=cash,
        final_quantities={slot: {s: q for s, q in quantities[slot].items() if q} for slot in SLOTS},
        final_performance={series: performance[series].summary() for series in SERIES},
        fills=sum(counts.values()), fees_cents=sum(fees.values()),
        untradeable_fill_count=untradeable_count,
        untradeable_sides=dict(untradeable_sides), untradeable_fill_examples=untradeable,
        halted_fill_count=halted_fill_count,
        halted_fill_examples=halted_fill_examples,
        halted_holding_marks=dict(
            slot_symbol_days=halted_holding_marks,
            distinct_slot_symbol_days=len(halted_holding_slot_symbol_days),
            distinct_symbol_days=len({(row[1], row[2]) for row in halted_holding_slot_symbol_days}),
            distinct_symbols=len({row[1] for row in halted_holding_slot_symbol_days}),
            by_slot=dict(halted_holding_by_slot),
            first_examples=halted_holding_examples,
            valuation_rule="carried close equals the official previous close; no fill is allowed"),
        halted_resumptions=dict(
            resolved=len(halted_resumptions),
            unresolved_at_end=len(pending_halts),
            unresolved=[dict(slot=slot, symbol=symbol, halted_from=day)
                        for (slot, symbol), day in sorted(pending_halts.items())],
            first_examples=halted_resumptions[:15]),
        opening_holdings_reference_resets=dict(
            opening_slot_symbol_day_comparisons=opening_reference_comparisons,
            affected_slot_symbol_days=len(reference_resets),
            affected_symbol_days=len({(row["symbol"], row["day"]) for row in reference_resets}),
            affected_symbols=len({row["symbol"] for row in reference_resets}),
            affected_slot_symbol_days_over_10_percent=sum(
                abs(row["reference_prev_close_cents"] - row["prior_close_cents"]) * 10_000
                > row["prior_close_cents"] * 1_000 for row in reference_resets),
            by_slot={slot: sum(row["slot"] == slot for row in reference_resets) for slot in SLOTS},
            first_examples=reference_resets[:15],
            largest_notional_examples=sorted(
                reference_resets,
                key=lambda row: row["absolute_notional_reference_difference_cents"],
                reverse=True)[:15],
            largest_relative_examples=sorted(
                reference_resets,
                key=lambda row: Fraction(
                    abs(row["reference_prev_close_cents"] - row["prior_close_cents"]),
                    row["prior_close_cents"]), reverse=True)[:15],
            affects_accounting_passed=False,
            model_handles_corporate_actions=False,
            supports_executable_total_return=False,
            interpretation=("Observed reference reset only; not automatically a dividend, bonus share or split. "
                            "No adjustment is made to cash, quantities, profit or return. "
                            "The notional reference difference is an observation, not an additive total-return correction.")))


def verify(report_path, db_path):
    report = json.loads(report_path.read_text(encoding="utf-8"))
    if report["input"]["adjust"] != "none":
        raise VerificationError("only an unadjusted input report is in audit scope")
    windows = report["input"]["windows"]
    days = [window["day"] for window in windows]
    if not days or days != sorted(set(days)):
        raise VerificationError("report dates must be nonempty and strictly increasing")
    uri = db_path.resolve().as_uri() + "?mode=ro"
    with closing(sqlite3.connect(uri, uri=True)) as connection:
        connection.execute("BEGIN")
        db_days = [row[0] for row in connection.execute(
            "SELECT DISTINCT day FROM bar WHERE adjust='none' AND close_cents>0 AND day BETWEEN ? AND ? ORDER BY day",
            (report["input"]["from"], report["input"]["through"]))]
        if days != db_days or report["input"]["settled_days"] != len(days):
            raise VerificationError("report period is not the source's complete unadjusted date sequence")
        variants = {name: verify_variant(name, report[name], report, connection)
                    for name in ("unfiltered", "filtered")}
    return dict(
        passed=all(variant["passed"] for variant in variants.values()),
        completed=True,
        accounting_and_performance_passed=all(v["accounting_and_performance_passed"] for v in variants.values()),
        quote_tradeability_passed=all(v["quote_tradeability_passed"] for v in variants.values()),
        executable_total_return_validated=False,
        input_report=str(report_path.resolve()),
        input_report_sha256=hashlib.sha256(report_path.read_bytes()).hexdigest(),
        input_database=str(db_path.resolve()),
        selected_from=days[0], selected_through=days[-1], checked_days=len(days),
        calculation="Independent integer fill ledger and exact Fraction end-of-day-flow TWR",
        precision_policy={
            "money_and_positions": "exact equality", "account_bps": "exact integer truncation",
            "slot_bps": "exact integer truncation; adjacent 1bp accepted only within 1e-9 bps of integer boundary, differences retained",
            "drawdown_dates": "Compare API integer-bps event dates; also disclose exact unrounded maximum event dates"},
        variants=variants,
        limitations=[
            "A ledger arithmetic pass does not establish realistic execution or predictive profitability.",
            "Zero-volume/non-positive-open fills fail quote-tradeability checks even when ledger arithmetic is consistent.",
            "A halted holding is valued at its official carried previous close and may not trade; resumption is observed from the next tradable row.",
            "Daily bars do not prove intraday fill liquidity; price-limit execution is not validated here.",
            "Fee arithmetic is reconciled to the engine's fixed-rate model, not to date-specific historical exchange/tax schedules.",
            "Unadjusted bars and the exported ledger do not account for dividends, bonus shares or splits."])


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("report", type=Path)
    parser.add_argument("--db", type=Path, default=Path(__file__).resolve().parents[2] / ".data/research-market.sqlite3")
    parser.add_argument("--out", type=Path)
    args = parser.parse_args()
    out = args.out or args.report.with_suffix(".verification.json")
    try:
        result = verify(args.report, args.db)
    except (OSError, json.JSONDecodeError, sqlite3.Error, VerificationError, ZeroDivisionError) as error:
        result = dict(passed=False, completed=False, input_report=str(args.report),
                      error_type=type(error).__name__, error=str(error))
        out.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
        print(f"verification failed: {error}; {out}", file=sys.stderr)
        return 2
    out.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(dict(passed=result["passed"],
                          accounting_and_performance_passed=result["accounting_and_performance_passed"],
                          quote_tradeability_passed=result["quote_tradeability_passed"],
                          checked_days=result["checked_days"], output=str(out)), ensure_ascii=False))
    return 0 if result["passed"] else 1


if __name__ == "__main__":
    raise SystemExit(main())

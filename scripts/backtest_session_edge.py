#!/usr/bin/env python3
"""Compare baseline M5 entries with VWAP and Asian-edge context filters.

CSV columns: time (or timestamp/datetime), open, high, low, close, and
volume or tick_volume. Optional spread_price/spread columns are price units;
otherwise --spread-price supplies the per-trade cost. Asian-only requires a
matching active edge signal; both uses VWAP alignment or the matching edge
exception. Same historical bars and base Trend/PriceAction decisions are used
for every row.
"""
from __future__ import annotations

import argparse
import csv
import math
import sys
from collections import Counter
from pathlib import Path

from live_trading.signals.decision_engine import run_decision_engine
from live_trading.signals.gold_engine import OHLCV
from live_trading.signals.session_edge import evaluate_entry_context, utc_datetime
from scripts.backtest_core import (
    ALLOWED_DIRECTIONS,
    M5_TIMEFRAME,
    simulate_trade as _simulate_trade,
)


def _row_value(row: dict[str, str], *names: str) -> str | None:
    lowered = {str(key).strip().lower(): value for key, value in row.items() if key}
    for name in names:
        value = lowered.get(name.lower())
        if value is not None and str(value).strip() != "":
            return str(value).strip()
    return None


def load_m5_csv(path: Path, fallback_spread: float = 0.30) -> tuple[list[OHLCV], list[float]]:
    records: list[tuple[OHLCV, float]] = []
    with path.open("r", encoding="utf-8-sig", newline="") as handle:
        for row in csv.DictReader(handle):
            try:
                raw_time = _row_value(row, "time", "timestamp", "datetime", "date")
                if raw_time is None:
                    continue
                utc_time = utc_datetime(raw_time).isoformat().replace("+00:00", "Z")
                volume_raw = _row_value(row, "tick_volume", "volume", "real_volume") or "0"
                bar = OHLCV(
                    time=utc_time,
                    open=float(_row_value(row, "open", "o") or "nan"),
                    high=float(_row_value(row, "high", "h") or "nan"),
                    low=float(_row_value(row, "low", "l") or "nan"),
                    close=float(_row_value(row, "close", "c") or "nan"),
                    volume=float(volume_raw),
                )
                spread_raw = _row_value(row, "spread_price", "spread")
                spread = float(spread_raw) if spread_raw is not None else float(fallback_spread)
                if not all(math.isfinite(value) for value in (bar.open, bar.high, bar.low, bar.close, bar.volume, spread)):
                    continue
                if min(bar.open, bar.high, bar.low, bar.close) <= 0 or bar.volume < 0 or spread < 0:
                    continue
                if bar.high < max(bar.open, bar.close) or bar.low > min(bar.open, bar.close) or bar.high < bar.low:
                    continue
                records.append((bar, spread))
            except (TypeError, ValueError, OverflowError):
                continue
    records.sort(key=lambda item: utc_datetime(item[0].time))
    return [item[0] for item in records], [item[1] for item in records]


def _metrics(trades: list[dict[str, float | int | str]]) -> tuple[int, float, float, float, float, float]:
    pnls = [float(trade["net"]) for trade in trades]
    rs = [float(trade["r"]) for trade in trades]
    wins = sum(value > 0 for value in pnls)
    gross_profit = sum(value for value in pnls if value > 0)
    gross_loss = abs(sum(value for value in pnls if value < 0))
    profit_factor = gross_profit / gross_loss if gross_loss else (math.inf if gross_profit else 0.0)
    equity = peak = max_drawdown = 0.0
    for value in pnls:
        equity += value
        peak = max(peak, equity)
        max_drawdown = max(max_drawdown, peak - equity)
    count = len(trades)
    return (
        count,
        wins * 100.0 / count if count else 0.0,
        profit_factor,
        sum(rs) / count if count else 0.0,
        max_drawdown,
        sum(pnls),
    )


def run(
    csv_path: Path,
    *,
    spread_price: float = 0.30,
    account_balance: float = 10000.0,
    warmup: int = 200,
    allowed_directions: frozenset[str] = ALLOWED_DIRECTIONS,
) -> int:
    try:
        bars, spreads = load_m5_csv(csv_path, spread_price)
    except OSError as exc:
        print(f"Could not read CSV: {exc}", file=sys.stderr)
        return 2
    if len(bars) <= warmup + 1:
        print(f"Need more than {warmup + 1} valid M5 rows; found {len(bars)}.", file=sys.stderr)
        return 2

    names = ("baseline", "+VWAP", "+Asian sweep", "both")
    trades: dict[str, list[dict[str, float | int | str]]] = {name: [] for name in names}
    next_entry = {name: warmup for name in names}
    edge_used_days: dict[str, set[str]] = {name: set() for name in names}
    blocked_reasons: Counter[str] = Counter()
    allowed_signal_count = 0

    for index in range(warmup, len(bars) - 1):
        history = bars[max(0, index - 299):index + 1]
        decision = run_decision_engine(
            history,
            account_balance=account_balance,
            timeframe=M5_TIMEFRAME,
            symbol="XAUUSD",
        )
        if not decision.allowed:
            reasons = decision.blocked_reasons or ["Decision was blocked without a reason"]
            blocked_reasons.update(str(reason) for reason in reasons)
            continue
        if decision.trade_params is None:
            blocked_reasons["Decision allowed without trade parameters"] += 1
            continue
        direction = str(decision.direction).upper()
        if direction not in allowed_directions:
            blocked_reasons[f"{direction} filtered by ALLOWED_DIRECTIONS"] += 1
            continue
        allowed_signal_count += 1
        context = evaluate_entry_context(history, direction, mode="off", applicable=True)
        edge = context["session_edge"]
        vwap = context["vwap_filter"]
        day = str(edge.get("date_utc") or "")
        aligned = vwap.get("vwap_aligned") is True
        for name in names:
            if index < next_entry[name]:
                continue
            edge_match = bool(
                edge.get("active")
                and edge.get("signal") == direction
                and day
                and day not in edge_used_days[name]
            )
            eligible = (
                True if name == "baseline" else
                aligned if name == "+VWAP" else
                edge_match if name == "+Asian sweep" else
                aligned or edge_match
            )
            if not eligible:
                continue
            result = _simulate_trade(bars, spreads, index, decision)
            if result is None:
                blocked_reasons["Simulator rejected the allowed signal"] += 1
                continue
            trades[name].append(result)
            next_entry[name] = int(result["exit_index"]) + 1
            if edge_match and name in {"+Asian sweep", "both"}:
                edge_used_days[name].add(day)

    print("Variant       Trades  Win rate  Profit factor  Avg R   Max DD   Net PnL (price)")
    print("------------- ------  --------  -------------  ------  -------  ---------------")
    for name in names:
        count, win_rate, pf, avg_r, drawdown, net = _metrics(trades[name])
        pf_text = "inf" if math.isinf(pf) else f"{pf:.2f}"
        print(f"{name:<13} {count:>6}  {win_rate:>7.2f}%  {pf_text:>13}  {avg_r:>6.3f}  {drawdown:>7.3f}  {net:>15.3f}")
    if allowed_signal_count == 0 or not trades["baseline"]:
        print(
            f"\nERROR: no baseline trades were produced after "
            f"{allowed_signal_count} allowed signal(s) "
            f"(timeframe={M5_TIMEFRAME!r}). Top blocked reasons:",
            file=sys.stderr,
        )
        for reason, count in blocked_reasons.most_common(5):
            print(f"  {count:>5}x  {reason}", file=sys.stderr)
        if not blocked_reasons:
            print("  No decision attempts produced a blocked reason.", file=sys.stderr)
        return 2
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--csv", type=Path, help="Historical M5 CSV path")
    parser.add_argument("--spread-price", type=float, default=0.30, help="Fallback spread cost in price units")
    parser.add_argument("--account-balance", type=float, default=10000.0)
    parser.add_argument("--warmup", type=int, default=200)
    parser.add_argument(
        "--allowed-directions",
        choices=("both", "buy"),
        default="both",
        help="Backtest-only A/B filter; BUY stays enabled",
    )
    args = parser.parse_args()
    if args.csv is None:
        print("No historical CSV is checked into this repo; pass --csv PATH to run the M5 comparison.")
        return 2
    if args.spread_price < 0 or args.warmup < 50:
        parser.error("--spread-price must be non-negative and --warmup at least 50")
    allowed_directions = (
        ALLOWED_DIRECTIONS
        if args.allowed_directions == "both"
        else frozenset({"BUY"})
    )
    return run(
        args.csv,
        spread_price=args.spread_price,
        account_balance=args.account_balance,
        warmup=args.warmup,
        allowed_directions=allowed_directions,
    )


if __name__ == "__main__":
    raise SystemExit(main())

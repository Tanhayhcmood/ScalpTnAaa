#!/usr/bin/env python3
"""Compare legacy staircase and adaptive ATR trailing on recorded trades.

Input is JSON or JSONL. Each trade must contain:

    direction: BUY or SELL
    entry: number
    sl (or initial_sl): number
    candles: [{open, high, low, close, volume}, ...]

An optional ``exit_price`` is used when the candle path does not hit a stop.
The script refuses to invent candle paths from a summary-only trade log; this
is important because a trailing-stop comparison cannot be inferred from final
profit alone.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any

from live_trading.risk.adaptive_trailing_stop import (
    AdaptiveTrailingConfig,
    compute_adaptive_trail,
)
from live_trading.risk.trailing_stop import (
    TrailingConfig,
    compute_staircase_sl,
)


def _load_records(path: Path) -> list[dict[str, Any]]:
    text = path.read_text(encoding="utf-8").strip()
    if not text:
        return []
    try:
        decoded = json.loads(text)
        if isinstance(decoded, dict):
            decoded = decoded.get("trades", decoded.get("recent_trades", []))
        return [row for row in decoded if isinstance(row, dict)] if isinstance(decoded, list) else []
    except json.JSONDecodeError:
        records: list[dict[str, Any]] = []
        for line in text.splitlines():
            try:
                row = json.loads(line)
            except json.JSONDecodeError:
                continue
            if isinstance(row, dict):
                records.append(row)
        return records


def _price_result(direction: str, entry: float, exit_price: float) -> float:
    return exit_price - entry if direction == "BUY" else entry - exit_price


def _simulate_legacy(trade: dict[str, Any]) -> float:
    direction = str(trade["direction"]).upper()
    entry = float(trade["entry"])
    initial_sl = float(trade.get("initial_sl", trade.get("sl")))
    risk_distance = abs(entry - initial_sl)
    current_sl = initial_sl
    candles = trade["candles"]
    cfg = TrailingConfig(min_step_price=0.0)
    for index, candle in enumerate(candles):
        current_price = float(candle["close"])
        candidate = compute_staircase_sl(
            direction, entry, risk_distance, current_price, 0.0, cfg
        )
        if candidate is not None:
            current_sl = (
                max(current_sl, candidate)
                if direction == "BUY"
                else min(current_sl, candidate)
            )
        if index == 0:
            continue
        low = float(candle["low"])
        high = float(candle["high"])
        if direction == "BUY" and low <= current_sl:
            return _price_result(direction, entry, current_sl)
        if direction == "SELL" and high >= current_sl:
            return _price_result(direction, entry, current_sl)
    return _price_result(direction, entry, float(trade.get("exit_price", candles[-1]["close"])))


def _simulate_adaptive(trade: dict[str, Any], cfg: AdaptiveTrailingConfig) -> float:
    direction = str(trade["direction"]).upper()
    entry = float(trade["entry"])
    current_sl = float(trade.get("initial_sl", trade.get("sl")))
    for index, candle in enumerate(trade["candles"]):
        history = trade["candles"][: index + 1]
        decision = compute_adaptive_trail(direction, float(candle["close"]), history, cfg)
        if decision.candidate_sl is not None:
            current_sl = (
                max(current_sl, decision.candidate_sl)
                if direction == "BUY"
                else min(current_sl, decision.candidate_sl)
            )
        if index == 0:
            continue
        low = float(candle["low"])
        high = float(candle["high"])
        if direction == "BUY" and low <= current_sl:
            return _price_result(direction, entry, current_sl)
        if direction == "SELL" and high >= current_sl:
            return _price_result(direction, entry, current_sl)
    return _price_result(direction, entry, float(trade.get("exit_price", trade["candles"][-1]["close"])))


def run(path: Path) -> int:
    records = _load_records(path)
    valid = [
        row for row in records
        if row.get("candles")
        and row.get("entry") is not None
        and row.get("sl", row.get("initial_sl")) is not None
        and row.get("direction") in {"BUY", "SELL", "buy", "sell"}
    ]
    if not valid:
        print(
            f"No candle-level trade records found in {path}. "
            "A summary-only robot log cannot produce a valid trailing comparison."
        )
        return 2

    cfg = AdaptiveTrailingConfig()
    legacy_results = [_simulate_legacy(row) for row in valid]
    adaptive_results = [_simulate_adaptive(row, cfg) for row in valid]
    legacy_total = sum(legacy_results)
    adaptive_total = sum(adaptive_results)
    delta = adaptive_total - legacy_total
    print(f"Trades compared: {len(valid)}")
    print(f"Legacy trailing result (price units):   {legacy_total:.5f}")
    print(f"Adaptive trailing result (price units): {adaptive_total:.5f}")
    print(f"Difference (adaptive - legacy):         {delta:+.5f}")
    for index, (legacy, adaptive) in enumerate(zip(legacy_results, adaptive_results), 1):
        print(f"  trade {index}: legacy={legacy:+.5f} adaptive={adaptive:+.5f} delta={adaptive - legacy:+.5f}")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "path",
        nargs="?",
        help="JSON/JSONL trade file; defaults to robot_state.json or live_trading/robot.log",
    )
    args = parser.parse_args()
    path = Path(args.path) if args.path else next(
        (candidate for candidate in (Path("robot_state.json"), Path("live_trading/robot.log")) if candidate.exists()),
        None,
    )
    if path is None:
        print(
            "No robot_state.json or live_trading/robot.log found. "
            "Provide a path containing candle-level historical trades.",
            file=sys.stderr,
        )
        return 2
    return run(path)


if __name__ == "__main__":
    raise SystemExit(main())
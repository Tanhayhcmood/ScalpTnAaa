#!/usr/bin/env python3
"""Small-grid, rolling walk-forward analysis for XAUUSD M5 history.

Only test-window performance is printed. Training windows choose settings but
are never included in the reported out-of-sample metrics.
"""
from __future__ import annotations

import argparse
import math
import random
import sys
from collections import Counter
from copy import copy
from dataclasses import dataclass, replace
from itertools import product
from pathlib import Path
from typing import Any, Iterable

from live_trading.config import (
    ADAPTIVE_TRAIL_V2,
    SCALP_PROFILE,
    SL_ATR_BASE_MULTIPLIER,
    TRAIL_ATR_PERIOD,
    TRAIL_CHANDELIER_ATR_MULTIPLIER,
    TRAIL_ENABLED,
    TRAIL_MIN_STEP_PRICE,
)
from live_trading.risk.adaptive_trailing_stop import AdaptiveTrailingConfig
from live_trading.risk.capital_manager import FIXED_TP_RR
from live_trading.signals.decision_engine import run_decision_engine
from live_trading.signals.session_edge import utc_datetime
from scripts.backtest_core import (
    ALLOWED_DIRECTIONS,
    M5_TIMEFRAME,
    simulate_trade,
)
from scripts.backtest_session_edge import load_m5_csv


@dataclass(frozen=True)
class Fold:
    train_start: int
    train_end: int
    test_start: int
    test_end: int
    test_label: str


@dataclass(frozen=True)
class Variant:
    atr_multiplier: float
    tp_rr: float
    trail_name: str
    trailing_config: AdaptiveTrailingConfig
    allowed_directions: frozenset[str]
    session_filter: str

    @property
    def key(self) -> tuple[Any, ...]:
        return (
            round(self.atr_multiplier, 4),
            round(self.tp_rr, 4),
            self.trail_name,
            tuple(sorted(self.allowed_directions)),
            self.session_filter,
        )

    def label(self) -> str:
        directions = "both" if self.allowed_directions == ALLOWED_DIRECTIONS else "BUY only"
        return (
            f"ATR {self.atr_multiplier:g}, TP {self.tp_rr:g}R, "
            f"{self.trail_name}, {directions}, {self.session_filter}"
        )


@dataclass
class FoldResult:
    fold: Fold
    selected: Variant | None
    selected_trades: list[dict[str, float | int | str]]
    baseline_trades: list[dict[str, float | int | str]]


def _parse_float_list(value: str, label: str) -> list[float]:
    try:
        values = sorted({float(part.strip()) for part in value.split(",") if part.strip()})
    except ValueError as exc:
        raise ValueError(f"{label} must be a comma-separated list of numbers") from exc
    if not values or not all(math.isfinite(item) and item > 0 for item in values):
        raise ValueError(f"{label} values must be finite and positive")
    return values


def _month(timestamp: str) -> str:
    parsed = utc_datetime(timestamp)
    if parsed is None:
        raise ValueError(f"invalid UTC timestamp: {timestamp!r}")
    return parsed.strftime("%Y-%m")


def build_folds(
    bars: list[Any], train_months: int = 2, test_months: int = 1, step_months: int = 1
) -> list[Fold]:
    if train_months < 1 or test_months < 1 or step_months < 1:
        raise ValueError("train, test, and step month counts must be positive")
    starts: dict[str, int] = {}
    for index, bar in enumerate(bars):
        starts.setdefault(_month(bar.time), index)
    months = sorted(starts)
    folds: list[Fold] = []
    test_pos = train_months
    while test_pos + test_months <= len(months):
        train_start = starts[months[test_pos - train_months]]
        train_end = starts[months[test_pos]]
        test_start = train_end
        test_end = (
            starts[months[test_pos + test_months]]
            if test_pos + test_months < len(months)
            else len(bars)
        )
        folds.append(
            Fold(
                train_start=train_start,
                train_end=train_end,
                test_start=test_start,
                test_end=test_end,
                test_label=f"{months[test_pos]}..{months[test_pos + test_months - 1]}",
            )
        )
        test_pos += step_months
    return folds


def live_trailing_config() -> AdaptiveTrailingConfig:
    """Match the default adaptive settings constructed by live_loop."""
    return AdaptiveTrailingConfig(
        enabled=TRAIL_ENABLED,
        atr_period=TRAIL_ATR_PERIOD,
        chandelier_atr_multiplier=TRAIL_CHANDELIER_ATR_MULTIPLIER,
        min_step_price=TRAIL_MIN_STEP_PRICE,
        breakeven_arm_r=1.0 if ADAPTIVE_TRAIL_V2 else 0.5,
        adaptive_trail_v2=ADAPTIVE_TRAIL_V2,
    )


def build_grid(atr_values: list[float], rr_values: list[float]) -> list[Variant]:
    live = live_trailing_config()
    tighter = replace(
        live,
        breakeven_arm_r=1.0,
        lock_arm_r=1.0,
        lock_profit_r=0.5,
        chandelier_atr_multiplier=max(0.8, live.chandelier_atr_multiplier * 0.8),
        breakeven_buffer_price=0.0,
    )
    trails = [("live-default", live), ("1R-BE-tighter-trail", tighter)]
    # Never test a sell-only configuration: BUY remains enabled in every
    # candidate, consistent with the requested no-buy-disable constraint.
    direction_sets = [ALLOWED_DIRECTIONS, frozenset({"BUY"})]
    variants = [
        Variant(atr, rr, trail_name, trail_cfg, directions, session)
        for atr, rr, (trail_name, trail_cfg), directions, session in product(
            atr_values,
            rr_values,
            trails,
            direction_sets,
            ("all", "london_ny"),
        )
    ]
    return variants


def prepare_decisions(
    bars: list[Any],
    atr_values: list[float],
    *,
    warmup: int,
    account_balance: float,
) -> tuple[dict[float, list[tuple[int, Any]]], Counter[str]]:
    decisions: dict[float, list[tuple[int, Any]]] = {value: [] for value in atr_values}
    blocked: Counter[str] = Counter()
    for atr_multiplier in atr_values:
        for index in range(warmup, len(bars) - 1):
            history = bars[max(0, index - 299):index + 1]
            decision = run_decision_engine(
                history,
                account_balance=account_balance,
                timeframe=M5_TIMEFRAME,
                symbol="XAUUSD",
                sl_atr_multiplier=atr_multiplier,
            )
            if not decision.allowed or decision.trade_params is None:
                reasons = decision.blocked_reasons or ["No blocked reason supplied"]
                blocked.update(str(reason) for reason in reasons)
                continue
            decisions[atr_multiplier].append((index, decision))
    return decisions, blocked


def _in_session(timestamp: str, session_filter: str) -> bool:
    if session_filter == "all":
        return True
    parsed = utc_datetime(timestamp)
    if parsed is None:
        return False
    # Fixed UTC window used for the research grid: 07:00–17:00 UTC.
    return 7 <= parsed.hour < 17


def _decision_with_target(decision: Any, tp_rr: float) -> Any | None:
    minimum_rr = getattr(decision, "minimum_rr", None)
    try:
        if minimum_rr is not None and tp_rr + 1e-9 < float(minimum_rr):
            return None
        params = copy(decision.trade_params)
        entry = float(params.entry_price)
        risk = abs(entry - float(params.stop_loss))
        if risk <= 0 or not math.isfinite(risk):
            return None
        sign = 1.0 if str(decision.direction).upper() == "BUY" else -1.0
        params.take_profit = round(entry + sign * risk * tp_rr, 8)
        params.risk_reward_ratio = round(tp_rr, 4)
        return type("BacktestDecision", (), {
            "direction": decision.direction,
            "trade_params": params,
        })()
    except (AttributeError, TypeError, ValueError, OverflowError):
        return None


def evaluate_variant(
    bars: list[Any],
    spreads: list[float],
    decisions: dict[float, list[tuple[int, Any]]],
    variant: Variant,
    start: int,
    end: int,
) -> list[dict[str, float | int | str]]:
    trades: list[dict[str, float | int | str]] = []
    next_entry = start
    max_exit_index = end - 1
    for index, decision in decisions.get(variant.atr_multiplier, []):
        if index < start or index >= end or index < next_entry:
            continue
        direction = str(decision.direction).upper()
        if direction not in variant.allowed_directions:
            continue
        if not _in_session(bars[index].time, variant.session_filter):
            continue
        adjusted = _decision_with_target(decision, variant.tp_rr)
        if adjusted is None:
            continue
        result = simulate_trade(
            bars,
            spreads,
            index,
            adjusted,
            trailing_config=variant.trailing_config,
            max_exit_index=max_exit_index,
        )
        if result is None:
            continue
        trades.append(result)
        next_entry = int(result["exit_index"]) + 1
    return trades


def _metrics(trades: Iterable[dict[str, float | int | str]]) -> tuple[int, float, float]:
    rows = list(trades)
    net = [float(row["net"]) for row in rows]
    rs = [float(row["r"]) for row in rows]
    profit = sum(value for value in net if value > 0)
    loss = abs(sum(value for value in net if value < 0))
    pf = profit / loss if loss else (math.inf if profit > 0 else 0.0)
    return len(rows), pf, sum(rs) / len(rs) if rs else 0.0


def _percentile(values: list[float], p: float) -> float:
    ordered = sorted(values)
    if not ordered:
        return math.nan
    index = min(len(ordered) - 1, max(0, int(math.floor(p * (len(ordered) - 1)))))
    return ordered[index]


def bootstrap_ci(
    trades: list[dict[str, float | int | str]],
    *,
    samples: int,
    seed: int,
) -> tuple[tuple[float, float], tuple[float, float]]:
    """Seeded trade-level bootstrap intervals for PF and average net R."""
    if not trades:
        return ((math.nan, math.nan), (math.nan, math.nan))
    rng = random.Random(seed)
    n = len(trades)
    pfs: list[float] = []
    avg_rs: list[float] = []
    pnl_values = [float(row["net"]) for row in trades]
    r_values = [float(row["r"]) for row in trades]
    for _ in range(samples):
        indices = [rng.randrange(n) for _ in range(n)]
        sample_pnl = [pnl_values[i] for i in indices]
        gross_profit = sum(value for value in sample_pnl if value > 0)
        gross_loss = abs(sum(value for value in sample_pnl if value < 0))
        pfs.append(gross_profit / gross_loss if gross_loss else (math.inf if gross_profit else 0.0))
        avg_rs.append(sum(r_values[i] for i in indices) / n)
    return (
        (_percentile(pfs, 0.025), _percentile(pfs, 0.975)),
        (_percentile(avg_rs, 0.025), _percentile(avg_rs, 0.975)),
    )


def _fmt(value: float) -> str:
    if math.isnan(value):
        return "—"
    return "∞" if math.isinf(value) else f"{value:.2f}"


def _fmt_ci(bounds: tuple[float, float]) -> str:
    return f"[{_fmt(bounds[0])}, {_fmt(bounds[1])}]"


def _select_training_variant(
    bars: list[Any],
    spreads: list[float],
    decisions: dict[float, list[tuple[int, Any]]],
    variants: list[Variant],
    fold: Fold,
    minimum_trades: int,
) -> Variant | None:
    best: tuple[float, float, int, Variant] | None = None
    for variant in variants:
        trades = evaluate_variant(
            bars, spreads, decisions, variant, fold.train_start, fold.train_end
        )
        count, pf, avg_r = _metrics(trades)
        if count < minimum_trades:
            continue
        candidate = (pf, avg_r, count, variant)
        if best is None or candidate[:3] > best[:3]:
            best = candidate
    return best[3] if best else None


def run_report(
    csv_path: Path,
    *,
    spread_price: float = 0.30,
    account_balance: float = 10000.0,
    warmup: int = 200,
    train_months: int = 2,
    test_months: int = 1,
    step_months: int = 1,
    minimum_trades: int = 30,
    samples: int = 2000,
    seed: int = 20261114,
    atr_multipliers: list[float] | None = None,
    tp_rrs: list[float] | None = None,
) -> int:
    if SCALP_PROFILE == "on":
        print(
            "Walk-forward report currently models the default adaptive-trailing "
            "profile; SCALP_PROFILE=on uses an additional TP1/time-stop path.",
            file=sys.stderr,
        )
        return 2
    if spread_price < 0 or warmup < 50 or samples < 100 or minimum_trades < 30:
        print(
            "spread must be non-negative, warmup at least 50, samples at least 100, "
            "and minimum trades at least 30.",
            file=sys.stderr,
        )
        return 2
    try:
        bars, spreads = load_m5_csv(csv_path, spread_price)
        folds = build_folds(bars, train_months, test_months, step_months)
    except (OSError, ValueError) as exc:
        print(f"Could not prepare walk-forward input: {exc}", file=sys.stderr)
        return 2
    if len(bars) <= warmup + 1:
        print(f"Need more than {warmup + 1} valid M5 rows; found {len(bars)}.", file=sys.stderr)
        return 2
    if not folds:
        months = sorted({_month(bar.time) for bar in bars})
        print(
            f"Need at least {train_months + test_months} calendar months; "
            f"found {len(months)} ({months[0] if months else 'none'}..{months[-1] if months else 'none'}).",
            file=sys.stderr,
        )
        return 2

    requested_atr_values = atr_multipliers or [
        float(SL_ATR_BASE_MULTIPLIER),
        min(3.5, float(SL_ATR_BASE_MULTIPLIER) + 0.5),
    ]
    requested_rr_values = tp_rrs or [float(FIXED_TP_RR), 1.5]
    try:
        atr_values = _parse_float_list(
            ",".join(map(str, [*requested_atr_values, float(SL_ATR_BASE_MULTIPLIER)])),
            "ATR multipliers",
        )
        rr_values = _parse_float_list(
            ",".join(map(str, [*requested_rr_values, float(FIXED_TP_RR)])),
            "TP R:R values",
        )
    except ValueError as exc:
        print(str(exc), file=sys.stderr)
        return 2
    if any(value < 1.5 or value > 3.5 for value in atr_values):
        print("ATR multipliers must be within the live 1.5–3.5 range.", file=sys.stderr)
        return 2
    if any(value < 0.8 or value > 3.0 for value in rr_values):
        print("TP R:R values must be within the live 0.8–3.0 range.", file=sys.stderr)
        return 2
    variants = build_grid(atr_values, rr_values)
    if len(variants) > 64:
        print("Grid exceeds 64 combinations; reduce the requested parameter lists.", file=sys.stderr)
        return 2

    decisions, blocked = prepare_decisions(
        bars,
        atr_values,
        warmup=warmup,
        account_balance=account_balance,
    )
    if not any(decisions.values()):
        print("No decision-engine entries passed. Top blocked reasons:", file=sys.stderr)
        for reason, count in blocked.most_common(5):
            print(f"  {count:>5}x  {reason}", file=sys.stderr)
        return 2

    baseline_key = (
        round(float(SL_ATR_BASE_MULTIPLIER), 4),
        round(float(FIXED_TP_RR), 4),
        "live-default",
        tuple(sorted(ALLOWED_DIRECTIONS)),
        "all",
    )
    baseline = next((variant for variant in variants if variant.key == baseline_key), None)
    if baseline is None:
        print("The grid omitted its live-default baseline; add the default ATR and TP RR.", file=sys.stderr)
        return 2

    results: list[FoldResult] = []
    for fold in folds:
        selected = _select_training_variant(
            bars, spreads, decisions, variants, fold, minimum_trades
        )
        selected_trades = (
            evaluate_variant(
                bars, spreads, decisions, selected, fold.test_start, fold.test_end
            )
            if selected is not None
            else []
        )
        baseline_trades = evaluate_variant(
            bars, spreads, decisions, baseline, fold.test_start, fold.test_end
        )
        results.append(FoldResult(fold, selected, selected_trades, baseline_trades))

    print(
        f"Walk-forward OOS only | {train_months} train month(s) / "
        f"{test_months} test month(s) | spread={spread_price:.2f} | seed={seed}"
    )
    print("Training data is used only to select settings; no training performance is shown.")
    print("| Test window | Selected settings | Trades | PF (95% bootstrap CI) | Avg R (95% bootstrap CI) | Status |")
    print("|---|---|---:|---:|---:|---|")
    all_selected: list[dict[str, float | int | str]] = []
    all_baseline: list[dict[str, float | int | str]] = []
    for fold_index, row in enumerate(results):
        count, pf, avg_r = _metrics(row.selected_trades)
        pf_ci, r_ci = bootstrap_ci(
            row.selected_trades, samples=samples, seed=seed + fold_index
        )
        selected_label = row.selected.label() if row.selected else "none (training slice insufficient)"
        status = (
            "insufficient (<30)" if count < 30
            else "training selection insufficient" if row.selected is None
            else "meets minimum"
        )
        print(
            f"| {row.fold.test_label} | {selected_label} | {count} | "
            f"{_fmt(pf)} {_fmt_ci(pf_ci)} | {_fmt(avg_r)} {_fmt_ci(r_ci)} | {status} |"
        )
        all_selected.extend(row.selected_trades)
        all_baseline.extend(row.baseline_trades)
        base_count, base_pf, base_avg_r = _metrics(row.baseline_trades)
        base_pf_ci, base_r_ci = bootstrap_ci(
            row.baseline_trades, samples=samples, seed=seed + 1000 + fold_index
        )
        print(
            f"| {row.fold.test_label} baseline | {baseline.label()} | {base_count} | "
            f"{_fmt(base_pf)} {_fmt_ci(base_pf_ci)} | {_fmt(base_avg_r)} "
            f"{_fmt_ci(base_r_ci)} | "
            f"{'insufficient (<30)' if base_count < 30 else 'baseline'} |"
        )

    pooled_count, pooled_pf, pooled_avg_r = _metrics(all_selected)
    pooled_pf_ci, pooled_r_ci = bootstrap_ci(
        all_selected, samples=samples, seed=seed + 9999
    )
    same_selection = (
        bool(results)
        and all(row.selected is not None for row in results)
        and len({row.selected.key for row in results if row.selected is not None}) == 1
    )
    every_window_supported = all(
        len(row.selected_trades) >= minimum_trades
        and _metrics(row.selected_trades)[1] > _metrics(row.baseline_trades)[1]
        and _metrics(row.selected_trades)[1] > 1.0
        for row in results
    )
    reliable = (
        same_selection
        and every_window_supported
        and pooled_count >= minimum_trades
        and pooled_pf_ci[0] > 1.0
    )
    print(
        f"\nPooled selected OOS: n={pooled_count}, PF={_fmt(pooled_pf)} "
        f"{_fmt_ci(pooled_pf_ci)}, avg R={_fmt(pooled_avg_r)} {_fmt_ci(pooled_r_ci)}"
    )
    if reliable:
        chosen = results[0].selected
        print(
            f"Recommendation: controlled paper A/B test only — {chosen.label()}. "
            "Do not change live defaults automatically."
        )
    else:
        print(
            "Recommendation: NO RELIABLE EVIDENCE. Keep both directions and current "
            "live defaults; do not disable BUY."
        )
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--csv", type=Path, required=True, help="Historical M5 CSV path")
    parser.add_argument("--spread-price", type=float, default=0.30, help="Spread cost in price units")
    parser.add_argument("--account-balance", type=float, default=10000.0)
    parser.add_argument("--warmup", type=int, default=200)
    parser.add_argument("--train-months", type=int, default=2)
    parser.add_argument("--test-months", type=int, default=1)
    parser.add_argument("--step-months", type=int, default=1)
    parser.add_argument("--minimum-trades", type=int, default=30)
    parser.add_argument("--samples", type=int, default=2000)
    parser.add_argument("--seed", type=int, default=20261114)
    parser.add_argument(
        "--atr-multipliers",
        default=f"{SL_ATR_BASE_MULTIPLIER},{min(3.5, SL_ATR_BASE_MULTIPLIER + 0.5)}",
        help="Comma-separated small ATR stop grid (1.5–3.5)",
    )
    parser.add_argument(
        "--tp-rrs",
        default=f"{FIXED_TP_RR},1.5",
        help="Comma-separated target R:R grid",
    )
    args = parser.parse_args()
    try:
        atr_values = _parse_float_list(args.atr_multipliers, "ATR multipliers")
        rr_values = _parse_float_list(args.tp_rrs, "TP R:R values")
    except ValueError as exc:
        parser.error(str(exc))
    return run_report(
        args.csv,
        spread_price=args.spread_price,
        account_balance=args.account_balance,
        warmup=args.warmup,
        train_months=args.train_months,
        test_months=args.test_months,
        step_months=args.step_months,
        minimum_trades=args.minimum_trades,
        samples=args.samples,
        seed=args.seed,
        atr_multipliers=atr_values,
        tp_rrs=rr_values,
    )


if __name__ == "__main__":
    raise SystemExit(main())

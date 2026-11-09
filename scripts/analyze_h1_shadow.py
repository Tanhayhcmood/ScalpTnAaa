"""Compare closed-trade P&L for H1 shadow-blocked and allowed entries."""

import argparse
import json
import re
import sys
from pathlib import Path


_SHADOW_SIGNAL = re.compile(
    r"SHADOW_BLOCK h1_trend\b.*?\bsignal_id=([^\s]+)"
)


def _shadow_blocked_signal_ids(log_path: Path) -> set[str]:
    blocked: set[str] = set()
    with log_path.open(encoding="utf-8", errors="replace") as log_file:
        for line in log_file:
            match = _SHADOW_SIGNAL.search(line)
            if match:
                blocked.add(match.group(1))
    return blocked


def _closed_trade_totals(state_path: Path, blocked_ids: set[str]):
    with state_path.open(encoding="utf-8") as state_file:
        state = json.load(state_file)
    trades = state.get("recent_trades", [])
    blocked_count = allowed_count = unmatched_count = 0
    blocked_pnl = allowed_pnl = 0.0

    for trade in trades:
        if not isinstance(trade, dict):
            continue
        if not (trade.get("close_time") or trade.get("status") == "CLOSED"):
            continue
        try:
            pnl = float(trade["profit"])
        except (KeyError, TypeError, ValueError, OverflowError):
            continue
        signal_id = str(trade.get("signal_id") or "").strip()
        if not signal_id:
            unmatched_count += 1
        elif signal_id in blocked_ids:
            blocked_count += 1
            blocked_pnl += pnl
        else:
            allowed_count += 1
            allowed_pnl += pnl

    return (
        blocked_count,
        blocked_pnl,
        allowed_count,
        allowed_pnl,
        unmatched_count,
    )


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--log", type=Path, default=Path("live_trading/robot.log"))
    parser.add_argument("--state", type=Path, default=Path("robot_state.json"))
    args = parser.parse_args()
    try:
        blocked_ids = _shadow_blocked_signal_ids(args.log)
        totals = _closed_trade_totals(args.state, blocked_ids)
    except (OSError, json.JSONDecodeError) as exc:
        print(f"Could not read H1 shadow inputs: {exc}", file=sys.stderr)
        return 2

    blocked_count, blocked_pnl, allowed_count, allowed_pnl, unmatched_count = totals
    print(f"H1 would block: {blocked_count} closed trades | PnL {blocked_pnl:+.2f}")
    print(f"H1 would allow: {allowed_count} closed trades | PnL {allowed_pnl:+.2f}")
    print(f"Unmatched closed trades: {unmatched_count}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

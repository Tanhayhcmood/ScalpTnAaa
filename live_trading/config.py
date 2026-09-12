"""
GoldScalperPro v4 – Configuration

All settings are read from environment variables so they can be changed
on Render without touching code.

Required:
    MTAPI_URL     – official MTAPI REST API URL (normally https://mt5.mtapi.io)
    MT5_HOST      – broker server name (e.g. AMarkets-Demo)
    MT5_USER      – MT5 account login number
    MT5_PASSWORD  – MT5 account password
"""
import os
import sys

# ── Bounded env-var helpers ───────────────────────────────────────────────────

def _int(name: str, default: int, lo: int | None = None, hi: int | None = None) -> int:
    raw = os.getenv(name, str(default))
    try:
        val = int(raw)
    except ValueError:
        print(
            f"ERROR: {name}={raw!r} is not a valid integer. "
            f"Fix it in the Render dashboard and redeploy.",
            file=sys.stderr,
        )
        sys.exit(1)
    if lo is not None and val < lo:
        print(
            f"ERROR: {name}={val} is below the minimum allowed value ({lo}). "
            f"Fix it in the Render dashboard and redeploy.",
            file=sys.stderr,
        )
        sys.exit(1)
    if hi is not None and val > hi:
        print(
            f"ERROR: {name}={val} is above the maximum allowed value ({hi}). "
            f"Fix it in the Render dashboard and redeploy.",
            file=sys.stderr,
        )
        sys.exit(1)
    return val


def _float(name: str, default: float, lo: float | None = None, hi: float | None = None) -> float:
    raw = os.getenv(name, str(default))
    try:
        val = float(raw)
    except ValueError:
        print(
            f"ERROR: {name}={raw!r} is not a valid number. "
            f"Fix it in the Render dashboard and redeploy.",
            file=sys.stderr,
        )
        sys.exit(1)
    if lo is not None and val < lo:
        print(
            f"ERROR: {name}={val} is below the minimum allowed value ({lo}). "
            f"Fix it in the Render dashboard and redeploy.",
            file=sys.stderr,
        )
        sys.exit(1)
    if hi is not None and val > hi:
        print(
            f"ERROR: {name}={val} is above the maximum allowed value ({hi}). "
            f"Fix it in the Render dashboard and redeploy.",
            file=sys.stderr,
        )
        sys.exit(1)
    return val


# ── Valid timeframe labels ────────────────────────────────────────────────────
_VALID_TIMEFRAMES = {
    "1m", "5m", "10m", "15m", "20m", "30m", "1h", "4h", "1d",
    "M1", "M5", "M10", "M15", "M20", "M30", "H1", "H4", "D1",
}

# Minute-equivalent of every supported timeframe — used to sort TRADE_TIMEFRAMES
# highest-first so that longer TF signals always get evaluated before shorter ones.
_TF_MINUTES: dict[str, int] = {
    "1m": 1,   "M1":  1,
    "5m": 5,   "M5":  5,
    "10m": 10, "M10": 10,
    "15m": 15, "M15": 15,
    "20m": 20, "M20": 20,
    "30m": 30, "M30": 30,
    "1h":  60, "H1":  60,
    "4h":  240,"H4":  240,
    "1d":  1440,"D1": 1440,
}


def _trade_timeframes(name: str, default: str) -> list[str]:
    """Parse a comma-separated list of timeframe labels, validate each entry
    against _VALID_TIMEFRAMES, and return them sorted highest-first.

    Example:  TRADE_TIMEFRAMES=M20,M15,M10,5m  →  ["M20","M15","M10","5m"]
    """
    raw = os.getenv(name, default)
    tfs = [tf.strip() for tf in raw.split(",") if tf.strip()]
    if not tfs:
        print(
            f"ERROR: {name} is empty. Provide a comma-separated list "
            f"of timeframes, e.g. M20,M15,M10,5m",
            file=sys.stderr,
        )
        sys.exit(1)
    for tf in tfs:
        if tf not in _VALID_TIMEFRAMES:
            print(
                f"ERROR: {name} contains invalid timeframe {tf!r}. "
                f"Valid values: {', '.join(sorted(_VALID_TIMEFRAMES))}. "
                f"Fix it in the Render dashboard and redeploy.",
                file=sys.stderr,
            )
            sys.exit(1)
    # Sort highest-first so the loop processes longer-TF signals first.
    return sorted(tfs, key=lambda t: _TF_MINUTES.get(t, 0), reverse=True)


def _timeframe(name: str, default: str) -> str:
    val = os.getenv(name, default)
    if val not in _VALID_TIMEFRAMES:
        print(
            f"ERROR: {name}={val!r} is not a recognised timeframe. "
            f"Valid values: {', '.join(sorted(_VALID_TIMEFRAMES))}. "
            f"Fix it in the Render dashboard and redeploy.",
            file=sys.stderr,
        )
        sys.exit(1)
    return val


# ── Official MTAPI URL ────────────────────────────────────────────────────────
MTAPI_URL     = os.getenv("MTAPI_URL",     "https://mt5.mtapi.io").rstrip("/")

# ── MT5 Broker Credentials ───────────────────────────────────────────────────
MT5_HOST      = os.getenv("MT5_HOST",     "AMarkets-Demo")
MT5_PORT      = _int("MT5_PORT", 443, lo=1, hi=65535)
MT5_USER      = os.getenv("MT5_USER",     "")
MT5_PASSWORD  = os.getenv("MT5_PASSWORD", "")

# ── Symbol & Timeframe ───────────────────────────────────────────────────────
SYMBOL        = os.getenv("SYMBOL", "XAUUSD")
TIMEFRAME     = _timeframe("TIMEFRAME", "5m")
CANDLE_WINDOW = _int("CANDLE_WINDOW", 300, lo=50, hi=5000)

# ── Risk & Trade Rules ───────────────────────────────────────────────────────
# Production defaults — override via Render env vars if needed.
# MIN_CONFIRMATIONS: minimum engines that must agree (out of 4: SMC, Trend, PA, Wyckoff).
# CONF_HARD_MIN: trades below this confidence % are always rejected.
RISK_PERCENT      = _float("RISK_PERCENT",      1.0,  lo=0.01, hi=10.0)
#
# Confidence policy:
# - NORMAL_MIN_CONFIDENCE applies to every non-RANGE market regime.
# - RANGE_MIN_CONFIDENCE applies to the dedicated RANGE playbook.
# - CONF_HARD_MIN and OPTION_TWO_MIN_CONFIDENCE are the global and MTF gates.
# Keep these aligned when the operator wants one confidence threshold across
# both live entry modes.
NORMAL_MIN_CONFIDENCE = _float("NORMAL_MIN_CONFIDENCE", 47.0, lo=0.0, hi=100.0)
RANGE_MIN_CONFIDENCE  = _float("RANGE_MIN_CONFIDENCE",  47.0, lo=0.0, hi=100.0)
# MIN_CONFIRMATIONS=1: one aligned engine may qualify the ordinary entry gate.
# Confidence, quality, MTF, regime, R:R, and risk gates remain active.
# RANGE has its own stricter floor below so choppy conditions cannot enter on
# only one agreeing engine.
MIN_CONFIRMATIONS = _int("MIN_CONFIRMATIONS",   1,    lo=1,    hi=10)
# Dedicated RANGE playbook: the edge/sweep/reversal gate is mandatory and the
# four-engine vote must still meet this minimum. Keep it separate from the
# global floor so non-RANGE behavior remains unchanged.
RANGE_TRADING_ENABLED = os.getenv("RANGE_TRADING_ENABLED", "true").strip().lower() in {
    "1", "true", "yes", "on",
}
RANGE_MIN_CONFIRMATIONS = _int("RANGE_MIN_CONFIRMATIONS", 2, lo=2, hi=4)
RANGE_MIN_RR = _float("RANGE_MIN_RR", 1.5, lo=1.0, hi=10.0)
RANGE_EDGE_ATR_DISTANCE = _float("RANGE_EDGE_ATR_DISTANCE", 0.25, lo=0.05, hi=2.0)
RANGE_RISK_PERCENT = _float("RANGE_RISK_PERCENT", 0.5, lo=0.01, hi=10.0)
# When false, the RANGE regime remains eligible but its Option 2-specific
# confirmations (Wyckoff/PA/edge/sweep/reversal) are informational only.
# Core signal, confidence, sizing, stop, position, and risk gates remain active.
RANGE_ENTRY_FILTERS_ENABLED = os.getenv(
    "RANGE_ENTRY_FILTERS_ENABLED", "true"
).strip().lower() in {"1", "true", "yes", "on"}
MAX_RANGE_TRADES_PER_SESSION = _int("MAX_RANGE_TRADES_PER_SESSION", 2, lo=1, hi=20)
# When enabled, every new trade must also have a same-direction Price Action signal.
# Default false preserves existing behavior until explicitly enabled on Render.
REQUIRE_PRICE_ACTION = os.getenv("REQUIRE_PRICE_ACTION", "false").strip().lower() in {
    "1", "true", "yes", "on",
}
# Exact option 1 gate: SMC, Price Action, and Wyckoff must all agree with the
# candidate direction. EMA remains informational/confirmatory and is not
# required for entry.
REQUIRE_SMC_PRICE_ACTION_WYCKOFF = os.getenv(
    "REQUIRE_SMC_PRICE_ACTION_WYCKOFF", "false"
).strip().lower() in {"1", "true", "yes", "on"}
# CONF_HARD_MIN is the absolute confidence floor shared by normal and RANGE
# entries. 47% is the operator-selected production threshold.
CONF_HARD_MIN     = _float("CONF_HARD_MIN",      47.0, lo=0.0, hi=100.0)
# QUALITY_ADX_MIN: minimum ADX value required to confirm trend momentum.
# Below this threshold the quality filter rejects the signal as "low momentum".
# 15 is the recommended floor for M5 gold — catches genuine micro-trends
# without blocking valid moves that ADX=20 would silently filter out.
QUALITY_ADX_MIN   = _float("QUALITY_ADX_MIN",    15.0, lo=5.0,  hi=40.0)
# Maximum age of the structure event that can authorize a new entry.
# 300 bars on M5 is roughly 25 hours and is too permissive for scalping;
# the default 24 closed bars keeps BOS/CHoCH actionable for about two hours.
STRUCTURE_MAX_AGE_BARS = _int("STRUCTURE_MAX_AGE_BARS", 24, lo=3, hi=100)
MAX_OPEN_TRADES   = _int("MAX_OPEN_TRADES", 1, lo=1, hi=10)

USE_ATR_HIGH_VOL_FILTER = os.getenv("USE_ATR_HIGH_VOL_FILTER", "false").lower() == "true"
# ── Multi-Timeframe (HTF) Filter ─────────────────────────────────────────────
# MTF_ENABLED       : enable/disable the HTF alignment gate (default on).
#                     Set to "false" to revert to M5-only behaviour instantly.
# MTF_TIMEFRAME     : the Higher TimeFrame to use for bias detection.
#                     "H1" is the recommended default for M5 scalping of gold.
#                     Supported: M1 M5 M15 M30 H1 H4 D1 (same set as TIMEFRAME).
# MTF_CANDLE_WINDOW : number of HTF bars to fetch (needs ≥ 210 for EMA-200).
#                     300 gives a comfortable margin without excessive latency.
MTF_ENABLED       = os.getenv("MTF_ENABLED",   "true").lower() == "true"
MTF_TIMEFRAME     = _timeframe("MTF_TIMEFRAME",  "H1")
MTF_CANDLE_WINDOW = _int("MTF_CANDLE_WINDOW",    300, lo=50, hi=1000)
# Option 2: a trade needs a real HTF confirmation, a matching entry
# timeframe, and at least 47% confidence. Changing this requires an explicit
# Render env override.
OPTION_TWO_MIN_CONFIDENCE = _float("OPTION_TWO_MIN_CONFIDENCE", 47.0, lo=0.0, hi=100.0)
OPTION_TWO_MIN_TIMEFRAMES = _int("OPTION_TWO_MIN_TIMEFRAMES", 2, lo=2, hi=10)

# ── Trade Timeframes (Multi-Timeframe entry) ─────────────────────────────────
# Comma-separated list of timeframes the robot will watch for new bars and
# generate independent trade signals on.  Sorted automatically highest-first
# so M20 and M15 signals take priority over M10 and M5 when bars close
# simultaneously (e.g. at minute :20 all four TFs close at once).
#
# The H1 HTF bias filter (MTF_TIMEFRAME above) is separate — it is always
# computed on H1 regardless of which trade TFs are active, because H1
# represents the directional context for the whole session.
#
# Recommended:  "M20,M15,M10,5m"  (4 TFs = ~2-4 entries/day per TF)
# Conservative: "M15,5m"           (2 TFs = cleaner, fewer signals)
# Aggressive:   "M20,M15,M10,5m"   (same as recommended)
TRADE_TIMEFRAMES  = _trade_timeframes("TRADE_TIMEFRAMES", "M20,M15,M10,5m")



# ── Order Settings ───────────────────────────────────────────────────────────
COMMENT = "GSPv4"

# ── Loop Timing ──────────────────────────────────────────────────────────────
# Poll frequently enough to notice a newly closed M5 candle promptly.  This
# only checks bar timestamps; signal evaluation still uses closed candles.
BAR_CHECK_INTERVAL = _int("BAR_CHECK_INTERVAL", 5, lo=1, hi=60)
RECONNECT_DELAY    = 30       # seconds before reconnect attempt
SYNC_TIMEOUT       = 120      # seconds to wait for initial connect

# ── File Paths (for Telegram panel) ─────────────────────────────────────────
STATE_FILE          = os.getenv("STATE_FILE",           "robot_state.json")
MT5_SNAPSHOT        = os.getenv("MT5_SNAPSHOT",         "robot_mt5_snapshot.json")
COMMANDS_FILE       = os.getenv("COMMANDS_FILE",        "robot_commands.json")
GUARDIAN_STATE_FILE = os.getenv("GUARDIAN_STATE_FILE",  "guardian_state.json")
LOG_FILE            = os.getenv("LOG_FILE",             "live_trading/robot.log")

# ── Risk Guardian – Circuit Breakers ─────────────────────────────────────────
# lo=0.1 prevents accidentally disabling protection with 0 or negative values.
# The daily loss circuit breaker may be raised temporarily for controlled
# paper/live testing, but remains bounded at 100% to reject invalid values.
DAILY_LOSS_LIMIT_PCT = _float("DAILY_LOSS_LIMIT_PCT", 3.0,  lo=0.1, hi=100.0)
MAX_DRAWDOWN_PCT     = _float("MAX_DRAWDOWN_PCT",      8.0,  lo=0.1, hi=50.0)
SLIPPAGE_POINTS      = _int("SLIPPAGE_POINTS",         30,   lo=1,   hi=500)

# ── Staircase Trailing Stop ───────────────────────────────────────────────────
# The stop loss placed at trade entry never moved automatically before — a
# trade could run deep into profit and still be stopped out at its original,
# now far-too-generous level if price reversed. The staircase trailing engine
# (live_trading/risk/trailing_stop.py) now ratchets the stop forward in
# discrete steps, measured in multiples of the trade's own original risk (R),
# as the trade advances — never backwards. All of it is env-configurable so
# it can be tuned on Render without a code change.
TRAIL_ENABLED        = os.getenv("TRAIL_ENABLED", "true").lower() == "true"
# R-multiple of profit required before the stop first moves off its entry level.
TRAIL_ACTIVATION_R   = _float("TRAIL_ACTIVATION_R",   1.0,  lo=0.1, hi=10.0)
# Size, in R-multiples, of each staircase step beyond activation.
TRAIL_STEP_R         = _float("TRAIL_STEP_R",         0.5,  lo=0.05, hi=5.0)
# Extra R-multiple locked in at every step so the stop locks real profit
# (covers spread/slippage) instead of landing on exact break-even.
TRAIL_LOCK_BUFFER_R  = _float("TRAIL_LOCK_BUFFER_R",  0.1,  lo=0.0, hi=2.0)
# Safety floor: the stop is never placed closer to the live price than this
# multiple of the current ATR, so a fast move can't ratchet the stop into
# the middle of normal M5 noise.
TRAIL_ATR_GAP_MULT   = _float("TRAIL_ATR_GAP_MULT",   0.5,  lo=0.0, hi=5.0)
# Minimum price-unit improvement required before sending a modify request —
# avoids spamming OrderModifySafe with no-op / sub-cent adjustments.
TRAIL_MIN_STEP_PRICE = _float("TRAIL_MIN_STEP_PRICE", 0.05, lo=0.0, hi=100.0)

# ── Wyckoff Calibration ──────────────────────────────────────────────────────
WYCKOFF_MAX_RANGE_PCT = 0.01163
WYCKOFF_SPRING_MARGIN = 2.06

# ── Redis IPC ────────────────────────────────────────────────────────────────
REDIS_URL = os.getenv("REDIS_URL", "")

# ── Backward-compat stubs (no longer used) ───────────────────────────────────
METAAPI_TOKEN      = ""
METAAPI_ACCOUNT_ID = ""

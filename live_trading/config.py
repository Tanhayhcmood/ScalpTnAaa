"""
GoldScalperPro v4 – Configuration

All settings are read from environment variables so they can be changed
on Render without touching code.

Required:
    MTAPI_URL          – MTAPI REST base URL
    MT5_HOST           – broker server name accepted by MTAPI
    MT5_USER           – MT5 account login number
    MT5_PASSWORD       – MT5 account password
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


def _strategy_list(name: str, default: str) -> tuple[str, ...]:
    """Parse the signal engines that are allowed to authorize live entries."""
    valid = ("smc", "trend", "price_action", "wyckoff")
    raw = os.getenv(name, default)
    strategies = tuple(dict.fromkeys(
        item.strip().lower() for item in raw.split(",") if item.strip()
    ))
    invalid = tuple(item for item in strategies if item not in valid)
    if invalid or not strategies:
        print(
            f"ERROR: {name} contains invalid or empty strategy names: "
            f"{', '.join(invalid or ('<empty>',))}. "
            f"Valid values: {', '.join(valid)}. "
            "Fix it in the Render dashboard and redeploy.",
            file=sys.stderr,
        )
        sys.exit(1)
    return strategies


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


# ── MTAPI REST connection ─────────────────────────────────────────────────────
# /ConnectEx returns an in-memory session token using the broker server name.
# No MetaAPI account or self-hosted MT5 terminal is required by the robot.
MTAPI_URL      = os.getenv("MTAPI_URL", "https://mt5.mtapi.io").rstrip("/")
MT5_HOST       = os.getenv("MT5_HOST", "AMarkets-Demo").strip()
MT5_PORT      = _int("MT5_PORT", 443, lo=1, hi=65535)
MT5_USER      = os.getenv("MT5_USER", "").strip()
MT5_PASSWORD  = os.getenv("MT5_PASSWORD", "").strip()
# Deprecated compatibility aliases. They stay empty and are not used by the
# direct MTAPI connector.
METAAPI_TOKEN      = ""
METAAPI_ACCOUNT_ID = ""

# ── Symbol & Timeframe ───────────────────────────────────────────────────────
SYMBOL        = os.getenv("SYMBOL", "XAUUSD")
TIMEFRAME     = _timeframe("TIMEFRAME", "5m")
CANDLE_WINDOW = _int("CANDLE_WINDOW", 300, lo=50, hi=5000)

# ── Risk & Trade Rules ───────────────────────────────────────────────────────
# Production defaults — override via Render env vars if needed.
# ENABLED_STRATEGIES: only these engines can authorize a live entry. The
# default intentionally keeps SMC and Wyckoff in observation/telemetry only.
ENABLED_STRATEGIES = _strategy_list(
    "ENABLED_STRATEGIES", "trend,price_action"
)
# MIN_CONFIRMATIONS: minimum enabled engines that must agree for ordinary
# entries.
# CONF_HARD_MIN: trades below this confidence % are always rejected.
RISK_PERCENT      = _float("RISK_PERCENT",      1.0,  lo=0.01, hi=10.0)
#
# Confidence policy:
# - NORMAL_MIN_CONFIDENCE applies to every non-RANGE market regime.
# - RANGE_MIN_CONFIDENCE applies to the dedicated RANGE playbook.
# - CONF_HARD_MIN is the global confidence floor.
# - MTF_OPPOSITION_THRESHOLD is the independent HTF opposition gate.
# Keep these aligned when the operator wants one confidence threshold across
# both live entry modes.
NORMAL_MIN_CONFIDENCE = _float("NORMAL_MIN_CONFIDENCE", 40.0, lo=0.0, hi=100.0)
RANGE_MIN_CONFIDENCE  = _float("RANGE_MIN_CONFIDENCE",  40.0, lo=0.0, hi=100.0)
# Ordinary entries require two aligned engines. Price Action has a dedicated
# standalone path below; this keeps SMC, Trend, and Wyckoff from opening a
# trade alone.
# Trend-aligned entries have their own safer floor below, so a Trend vote
# cannot open a trade by itself after a transient candle signal.
# RANGE has its own confirmation floor below, independent of
# TREND_MIN_CONFIRMATIONS and the ordinary entry policy.
MIN_CONFIRMATIONS = _int("MIN_CONFIRMATIONS",   2,    lo=1,    hi=10)
# A Trend-aligned ordinary entry must have at least two independent votes by
# default. This is deliberately separate from MIN_CONFIRMATIONS so the
# operator can keep Trend entries stricter than Price Action entries.
TREND_MIN_CONFIRMATIONS = _int("TREND_MIN_CONFIRMATIONS", 2, lo=1, hi=4)
# Dedicated RANGE playbook. Its confirmation floor is intentionally separate
# from both MIN_CONFIRMATIONS and TREND_MIN_CONFIRMATIONS so RANGE can use a
# lighter vote requirement without changing ordinary or TREND entries.
RANGE_TRADING_ENABLED = os.getenv("RANGE_TRADING_ENABLED", "true").strip().lower() in {
    "1", "true", "yes", "on",
}
RANGE_MIN_CONFIRMATIONS = _int("RANGE_MIN_CONFIRMATIONS", 2, lo=1, hi=4)
# Weak RANGE conditions are noisier, so the entry policy can require a
# stronger consensus without changing the normal RANGE floor.
RANGE_WEAK_MIN_CONFIRMATIONS = _int(
    "RANGE_WEAK_MIN_CONFIRMATIONS", 3, lo=2, hi=4
)
RANGE_MIN_RR = _float("RANGE_MIN_RR", 1.5, lo=1.0, hi=10.0)
RANGE_EDGE_ATR_DISTANCE = _float("RANGE_EDGE_ATR_DISTANCE", 0.25, lo=0.05, hi=2.0)
RANGE_RISK_PERCENT = _float("RANGE_RISK_PERCENT", 0.5, lo=0.01, hi=10.0)
# RANGE edge proximity is an optional structural safeguard. It is disabled by
# default so valid directional RANGE signals are not blocked in the channel
# middle; sweep/reversal checks remain governed by RANGE_ENTRY_FILTERS_ENABLED.
RANGE_REQUIRE_EDGE_POSITION = os.getenv(
    "RANGE_REQUIRE_EDGE_POSITION", "false"
).strip().lower() in {"1", "true", "yes", "on"}
# When false, the optional structural RANGE checks are relaxed. The
# confirmation floor remains controlled independently by RANGE_MIN_CONFIRMATIONS.
RANGE_ENTRY_FILTERS_ENABLED = os.getenv(
    "RANGE_ENTRY_FILTERS_ENABLED", "true"
).strip().lower() in {"1", "true", "yes", "on"}
MAX_RANGE_TRADES_PER_SESSION = _int("MAX_RANGE_TRADES_PER_SESSION", 2, lo=1, hi=20)
# When enabled, a directional Price Action signal may authorize the strategy
# vote by itself. This is intentionally independent of the other strategy
# votes; confidence, quality, regime, R:R, position, and risk gates still
# decide whether that PA setup can become a live order.
PRICE_ACTION_STANDALONE = os.getenv(
    "PRICE_ACTION_STANDALONE", "true"
).strip().lower() in {"1", "true", "yes", "on"}
# Minimum diagnostic PA score for the independent entry path.  The regular
# PA detector can expose early/weak setups for telemetry; standalone trading
# requires a stronger local setup before it can bypass other strategy votes.
PA_STANDALONE_MIN_SCORE = _float(
    "PA_STANDALONE_MIN_SCORE", 0.24, lo=0.15, hi=1.0
)
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
# entries. 40% is the safe default; operators can explicitly set a lower floor
# for controlled testing while the MTF, R:R, quality, broker, and risk gates
# remain active.
CONF_HARD_MIN     = _float("CONF_HARD_MIN",      40.0, lo=0.0, hi=100.0)
# QUALITY_ADX_MIN: minimum ADX value required to confirm usable momentum.
# 12 blocks very weak/choppy entries without requiring a fully developed trend.
QUALITY_ADX_MIN   = _float("QUALITY_ADX_MIN",    12.0, lo=5.0,  hi=40.0)
# Maximum age of the structure event that can authorize a new entry.
# 300 bars on M5 is roughly 25 hours and is too permissive for scalping;
# the default 24 closed bars keeps BOS/CHoCH actionable for about two hours.
STRUCTURE_MAX_AGE_BARS = _int("STRUCTURE_MAX_AGE_BARS", 24, lo=3, hi=100)
# One concurrent position is allowed per strategy slot. The default active
# strategy set is Trend + Price Action, but legacy SMC/Wyckoff slots remain
# understood so existing positions fail closed during the transition.
MAX_OPEN_TRADES   = _int("MAX_OPEN_TRADES", 4, lo=1, hi=10)
# The account is directional by default. Strategy slots may still be used for
# scale-in decisions, but an opposite-side position is never opened while a
# position on this symbol is live unless the operator explicitly opts into
# hedging on Render.
ALLOW_HEDGED_POSITIONS = os.getenv(
    "ALLOW_HEDGED_POSITIONS", "false"
).strip().lower() in {"1", "true", "yes", "on"}

USE_ATR_HIGH_VOL_FILTER = os.getenv("USE_ATR_HIGH_VOL_FILTER", "false").lower() == "true"
# ── Multi-Timeframe (HTF) negative filter ────────────────────────────────────
# MTF_ENABLED       : enable the HTF opposition check (default on).
#                     It is a negative-only filter: ordinary entries remain
#                     allowed unless HTF opposition is clearly strong.
# MTF_TIMEFRAME     : the Higher TimeFrame to use for bias detection.
#                     "H1" is the recommended default for M5 scalping of gold.
# MTF_CANDLE_WINDOW : number of HTF bars to fetch (needs ≥ 210 for EMA-200).
MTF_ENABLED       = os.getenv("MTF_ENABLED", "true").strip().lower() in {
    "1", "true", "yes", "on",
}
MTF_TIMEFRAME     = _timeframe("MTF_TIMEFRAME", "H1")
MTF_CANDLE_WINDOW = _int("MTF_CANDLE_WINDOW", 300, lo=50, hi=1000)
# Only strong opposition at or above this Trend Engine score can be rejected.
# This is intentionally separate from all confidence/confirmation settings so
# operators can tune the HTF safety threshold without changing the code.
MTF_OPPOSITION_THRESHOLD = _float(
    "MTF_OPPOSITION_THRESHOLD", 55.0, lo=0.0, hi=100.0
)
# Start in observation mode. The filter computes and logs would_block but does
# not reject orders until the operator explicitly sets this to false on Render.
MTF_DRY_RUN = os.getenv("MTF_DRY_RUN", "true").strip().lower() in {
    "1", "true", "yes", "on",
}
# Deprecated compatibility settings retained for panel/config consumers. They
# no longer act as MTF approval gates.
OPTION_TWO_MIN_CONFIDENCE = _float("OPTION_TWO_MIN_CONFIDENCE", 40.0, lo=0.0, hi=100.0)
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
# Recommended:  "M20,M15,M10,1m"  (4 TFs = ~2-4 entries/day per TF)
# Conservative: "M15,1m"           (2 TFs = cleaner, fewer signals)
# Aggressive:   "M20,M15,M10,1m"   (same as recommended)
TRADE_TIMEFRAMES  = _trade_timeframes("TRADE_TIMEFRAMES", "1m")



# ── Order Settings ───────────────────────────────────────────────────────────
COMMENT = "GSPv4"

# ── Loop Timing ──────────────────────────────────────────────────────────────
# Poll frequently enough to notice a newly closed M5 candle promptly.  This
# only checks bar timestamps; signal evaluation still uses closed candles.
BAR_CHECK_INTERVAL = _int("BAR_CHECK_INTERVAL", 5, lo=1, hi=60)
RECONNECT_DELAY    = 30       # seconds before reconnect attempt
SYNC_TIMEOUT       = 120      # seconds to wait for initial connect
# Bound individual broker HTTP calls so one stalled request cannot freeze the
# trading loop and its heartbeat indefinitely.
RPC_CALL_TIMEOUT   = _int("RPC_CALL_TIMEOUT", 30, lo=5, hi=120)
# Historical candle requests are the noisiest MetaAPI RPC in this service:
# several timeframes can request data around the same bar close. Keep their
# concurrency bounded and retry a transient data-plane timeout without tearing
# down an otherwise healthy broker session.
HISTORICAL_RPC_CONCURRENCY = _int(
    "HISTORICAL_RPC_CONCURRENCY", 2, lo=1, hi=4
)
HISTORICAL_RETRY_ATTEMPTS = _int(
    "HISTORICAL_RETRY_ATTEMPTS", 2, lo=1, hi=3
)
HISTORICAL_RETRY_BACKOFF = _float(
    "HISTORICAL_RETRY_BACKOFF", 1.0, lo=0.1, hi=10.0
)

# ── File Paths (for Telegram panel) ─────────────────────────────────────────
STATE_FILE          = os.getenv("STATE_FILE",           "robot_state.json")
MT5_SNAPSHOT        = os.getenv("MT5_SNAPSHOT",         "robot_mt5_snapshot.json")
COMMANDS_FILE       = os.getenv("COMMANDS_FILE",        "robot_commands.json")
GUARDIAN_STATE_FILE = os.getenv("GUARDIAN_STATE_FILE",  "guardian_state.json")
LOG_FILE            = os.getenv("LOG_FILE",             "live_trading/robot.log")
# Broker history is the source of truth after a Render restart.  Keep the
# window bounded so a large account history cannot delay the first scan.
HISTORY_LOOKBACK_DAYS = _int("HISTORY_LOOKBACK_DAYS", 90, lo=1, hi=3650)
HISTORY_SYNC_INTERVAL  = _int("HISTORY_SYNC_INTERVAL", 300, lo=30, hi=86400)

# ── Risk Guardian – Circuit Breakers ─────────────────────────────────────────
# lo=0.1 prevents accidentally disabling protection with 0 or negative values.
# The daily loss circuit breaker may be raised temporarily for controlled
# paper/live testing, but remains bounded at 100% to reject invalid values.
DAILY_LOSS_LIMIT_PCT = _float("DAILY_LOSS_LIMIT_PCT", 3.0,  lo=0.1, hi=100.0)
MAX_DRAWDOWN_PCT     = _float("MAX_DRAWDOWN_PCT",      8.0,  lo=0.1, hi=50.0)
SLIPPAGE_POINTS      = _int("SLIPPAGE_POINTS",         30,   lo=1,   hi=500)

# ── Adaptive ATR Trailing Stop ─────────────────────────────────────────────────
# The live stop waits for 1R, moves to breakeven, then uses a Chandelier stop
# anchored to the best price seen since entry. Legacy normal/tight settings are
# retained for config compatibility and telemetry.
TRAIL_ENABLED        = os.getenv("TRAIL_ENABLED", "true").lower() == "true"
TRAIL_ATR_PERIOD = _int("TRAIL_ATR_PERIOD", 14, lo=1, hi=100)
TRAIL_CHANDELIER_ATR_MULTIPLIER = _float(
    "TRAIL_CHANDELIER_ATR_MULTIPLIER", 2.5, lo=2.0, hi=3.0
)
TRAIL_NORMAL_MULTIPLIER = _float("TRAIL_NORMAL_MULTIPLIER", 2.0, lo=0.1, hi=5.0)
TRAIL_TIGHT_MULTIPLIER = _float("TRAIL_TIGHT_MULTIPLIER", 0.9, lo=0.1, hi=3.0)
TRAIL_EXHAUSTION_CONFIRM_COUNT = _int(
    "TRAIL_EXHAUSTION_CONFIRM_COUNT", 2, lo=2, hi=3
)
TRAIL_MOMENTUM_LOOKBACK = _int("TRAIL_MOMENTUM_LOOKBACK", 3, lo=2, hi=20)
TRAIL_BODY_SHRINK_RATIO = _float("TRAIL_BODY_SHRINK_RATIO", 0.8, lo=0.1, hi=1.0)
TRAIL_VOLUME_SHRINK_RATIO = _float("TRAIL_VOLUME_SHRINK_RATIO", 0.8, lo=0.1, hi=1.0)
TRAIL_MIN_PROFIT_ATR = _float("TRAIL_MIN_PROFIT_ATR", 1.5, lo=0.0, hi=20.0)
TRAIL_MIN_DISTANCE_ATR = _float("TRAIL_MIN_DISTANCE_ATR", 1.0, lo=0.1, hi=10.0)
if TRAIL_TIGHT_MULTIPLIER >= TRAIL_NORMAL_MULTIPLIER:
    print(
        "ERROR: TRAIL_TIGHT_MULTIPLIER must be smaller than "
        "TRAIL_NORMAL_MULTIPLIER.",
        file=sys.stderr,
    )
    sys.exit(1)
TRAIL_MIN_STEP_PRICE = _float("TRAIL_MIN_STEP_PRICE", 0.05, lo=0.0, hi=100.0)

# ── Wyckoff Calibration ──────────────────────────────────────────────────────
WYCKOFF_MAX_RANGE_PCT = 0.01163
WYCKOFF_SPRING_MARGIN = 2.06

# ── Redis IPC ────────────────────────────────────────────────────────────────
REDIS_URL = os.getenv("REDIS_URL", "")


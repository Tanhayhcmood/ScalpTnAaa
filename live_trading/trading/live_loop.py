"""
Live Trading Loop — async candle-close event handler via official MTAPI REST.

Flow per tick:
  1. Wait for next M5 candle close
  2. Fetch 300 closed candles via official MTAPI REST
  3. Run decision engine (all 7 signal engines)
  4. Gate: RiskGuardian circuit breakers (daily loss / drawdown)
  5. Gate: max open positions + trade allowed + Telegram not paused
  6. Place order via MTAPI executor (with slippage control)
  7. Write robot_state.json for Telegram panel

Resilience improvements over baseline:
  • RiskGuardian — daily loss limit + peak drawdown stop, both configurable
    via env vars.  Guardian halts block trade entry without stopping the loop.
  • Exponential backoff — reconnect delay doubles on each consecutive failure
    (cap: 5 minutes) then resets to base on success.
  • Slippage control — SLIPPAGE_POINTS env var limits max fill deviation.
  • Trade-history persistence — trade log is restored from robot_state.json
    on startup, so the Telegram panel shows history after a restart.
  • Duplicate-entry safety — live MT5 position check already prevents
    double-entry (unchanged), but now also guarded by Guardian state.
"""
import asyncio
import json
import math
import os
import re
import sys
from datetime import datetime, timedelta, timezone
from typing import List, Optional

from live_trading.config import (
    SYMBOL, TIMEFRAME, CANDLE_WINDOW, RISK_PERCENT,
    SL_ATR_TIMEFRAME, SL_ATR_PERIOD, SL_ATR_BASE_MULTIPLIER,
    LOW_VOLATILITY_SL_ATR_ADD,
    MAX_OPEN_TRADES, COMMENT,
    BAR_CHECK_INTERVAL, RECONNECT_DELAY, SYNC_TIMEOUT, RPC_CALL_TIMEOUT,
    MIN_CONFIRMATIONS, TREND_MIN_CONFIRMATIONS,
    PRICE_ACTION_STANDALONE, PA_STANDALONE_MIN_SCORE, REQUIRE_PRICE_ACTION,
    REQUIRE_SMC_PRICE_ACTION_WYCKOFF, USE_ATR_HIGH_VOL_FILTER,
    CONF_HARD_MIN,
    RANGE_TRADING_ENABLED, RANGE_MIN_CONFIRMATIONS, RANGE_MIN_RR,
    RANGE_EDGE_ATR_DISTANCE, RANGE_RISK_PERCENT,
    RANGE_REQUIRE_EDGE_POSITION,
    RANGE_ENTRY_FILTERS_ENABLED,
    MAX_RANGE_TRADES_PER_SESSION,
    DAILY_LOSS_LIMIT_PCT, MAX_DRAWDOWN_PCT, SLIPPAGE_POINTS,
    STATE_FILE, GUARDIAN_STATE_FILE,
    HISTORY_LOOKBACK_DAYS, HISTORY_SYNC_INTERVAL,
    TRAIL_ENABLED, TRAIL_ATR_PERIOD, TRAIL_NORMAL_MULTIPLIER,
    TRAIL_TIGHT_MULTIPLIER, TRAIL_EXHAUSTION_CONFIRM_COUNT,
    TRAIL_MOMENTUM_LOOKBACK, TRAIL_BODY_SHRINK_RATIO,
    TRAIL_VOLUME_SHRINK_RATIO, TRAIL_MIN_PROFIT_ATR,
    TRAIL_MIN_DISTANCE_ATR, TRAIL_MIN_STEP_PRICE,
    TRAIL_CHANDELIER_ATR_MULTIPLIER,
    RANGE_WEAK_MIN_CONFIRMATIONS,
    MTF_ENABLED, MTF_TIMEFRAME, MTF_CANDLE_WINDOW,
    MTF_OPPOSITION_THRESHOLD, MTF_DRY_RUN,
    TRADE_TIMEFRAMES,
    ALLOW_HEDGED_POSITIONS,
)
from live_trading.logger import get_logger
from live_trading.risk.guardian import RiskGuardian, GuardianStatus
from live_trading.risk.adaptive_trailing_stop import (
    AdaptiveTrailingConfig, atr, compute_adaptive_trail, should_apply,
)
from live_trading.risk.capital_manager import (
    LOT_DOLLAR_PER_UNIT, MAX_FIXED_LOT_RISK_USD, CapitalOutput,
)
from live_trading.signals.decision_engine import run_decision_engine, DecisionResult, describe_strategy
from live_trading.signals.gold_engine import calc_atr
from live_trading.signals.mtf_filter import (
    compute_mtf_bias,
    evaluate_mtf_opposition,
    MtfBias,
)
from live_trading.signals.h1_validator import validate_h1_candles
from live_trading.signals.wyckoff_engine import calibrate_wyckoff, set_calibrated_config
from live_trading.mt5.connector import (
    connect, disconnect, ensure_connected,
    connect_with_retry, start_connection_watchdog,
    fetch_candles, get_account_balance, get_account_info,
    check_symbol_available,
    get_open_positions, get_last_completed_bar_time,
    get_current_quote, get_closed_position_history, get_deals_by_time_range,
    get_symbol_params,
    mt5_pos_to_dict,
)
from live_trading.mt5.executor import (
    place_market_order, close_position, modify_position, TradeResult
)
from live_trading.trading.strategy_slots import (
    strategy_order_comment,
    strategy_slots_for_decision,
    available_for_strategy_slots,
    one_way_entry_allowed,
)
from live_trading.utils.state_writer import (
    write_robot_state, write_mt5_snapshot,
    read_commands, clear_command, log_trade,
)

log = get_logger()

def _checkpoint(msg: str) -> None:
    """Write a bounded-size progress marker to an always-writable path so we
    can pinpoint exactly where the engine hangs, even when no exception is
    ever raised (e.g. an unbounded await). Never let this raise."""
    try:
        with open("/tmp/progress.txt", "a", encoding="utf-8") as _f:
            _f.write(f"{datetime.utcnow().isoformat()}  {msg}\n")
    except Exception:
        pass


# ── Exponential backoff constants ─────────────────────────────────────────────
_RECONNECT_MAX_DELAY = 300   # seconds — hard cap regardless of attempt count
_RECONNECT_BASE      = RECONNECT_DELAY  # first-failure delay (from config, default 30s)

# How often (seconds) to refresh account info between M5 candles.
# Without this, _last_acc_info is only updated inside _on_new_bar() — once
# every 5 minutes.  Refreshing every 30 s keeps balance/equity current even
# when no new candle has fired (deposits, withdrawals, positions closed elsewhere).
_ACC_REFRESH_INTERVAL = 30.0
_CLOSE_HISTORY_RETRY_INTERVAL = 60.0


def _normalize_bar_time(value: object) -> Optional[datetime]:
    """Return a comparable naive-UTC datetime for internal bar bookkeeping."""
    if isinstance(value, datetime):
        parsed = value
    elif value:
        try:
            parsed = datetime.fromisoformat(
                str(value).replace("Z", "+00:00")
            )
        except (TypeError, ValueError):
            return None
    else:
        return None

    if parsed.tzinfo is not None:
        parsed = parsed.astimezone(timezone.utc).replace(tzinfo=None)
    return parsed


def _numeric_field(mapping: object, *keys: str) -> Optional[float]:
    if not isinstance(mapping, dict):
        return None
    for key in keys:
        value = mapping.get(key)
        try:
            number = float(value)
        except (TypeError, ValueError):
            continue
        if math.isfinite(number) and number >= 0:
            return number
    return None


def _broker_stop_constraints(symbol_params: dict) -> Optional[dict]:
    """Normalize the broker's live symbol metadata for SL/TP validation.

    MTAPI exposes the point size in ``symbolInfo`` and, on its current MT5
    contract, the minimum SL/TP distances as ``symbolGroup.sl``/``tp``.
    Some deployments also return explicit stops/freeze-level fields, so those
    are preferred when present.  No broker distance is invented here.
    """
    info = symbol_params.get("symbolInfo") or {}
    group = symbol_params.get("symbolGroup") or {}
    mappings = (symbol_params, info, group)

    digits_value = next(
        (
            value
            for value in (
                _numeric_field(info, "digits", "precision"),
                _numeric_field(symbol_params, "digits", "precision"),
            )
            if value is not None
        ),
        None,
    )
    digits = int(digits_value) if digits_value is not None else 0
    point = next(
        (
            value
            for mapping in mappings
            for value in (
                _numeric_field(mapping, "points", "point"),
                _numeric_field(mapping, "tickSize", "tick_size"),
            )
            if value is not None and value > 0
        ),
        None,
    )
    if point is None and digits > 0:
        point = 10 ** (-digits)
    if point is None or point <= 0:
        return None

    stops_level_points = next(
        (
            value
            for mapping in mappings
            for value in (
                _numeric_field(
                    mapping,
                    "stopsLevel",
                    "stopLevel",
                    "stops_level",
                    "stop_level",
                ),
            )
            if value is not None
        ),
        None,
    )
    freeze_level_points = next(
        (
            value
            for mapping in mappings
            for value in (
                _numeric_field(
                    mapping,
                    "freezeLevel",
                    "freeze_level",
                    "tradeFreezeLevel",
                    "trade_freeze_level",
                ),
            )
            if value is not None
        ),
        None,
    )
    sl_level_points = next(
        (
            value
            for mapping in (group, info, symbol_params)
            for value in (
                _numeric_field(
                    mapping,
                    "sl",
                    "stopLoss",
                    "stopLossLevel",
                    "slLevel",
                ),
            )
            if value is not None
        ),
        None,
    )
    tp_level_points = next(
        (
            value
            for mapping in (group, info, symbol_params)
            for value in (
                _numeric_field(
                    mapping,
                    "tp",
                    "takeProfit",
                    "takeProfitLevel",
                    "tpLevel",
                ),
            )
            if value is not None
        ),
        None,
    )
    if all(
        value is None
        for value in (
            stops_level_points,
            freeze_level_points,
            sl_level_points,
            tp_level_points,
        )
    ):
        return None

    all_level_points = [
        value
        for value in (
            stops_level_points,
            freeze_level_points,
            sl_level_points,
            tp_level_points,
        )
        if value is not None
    ]
    sl_required_points = max(
        value
        for value in (
            stops_level_points,
            freeze_level_points,
            sl_level_points,
        )
        if value is not None
    ) if any(
        value is not None
        for value in (
            stops_level_points,
            freeze_level_points,
            sl_level_points,
        )
    ) else max(all_level_points)
    tp_required_points = max(
        value
        for value in (
            stops_level_points,
            freeze_level_points,
            tp_level_points,
        )
        if value is not None
    ) if any(
        value is not None
        for value in (
            stops_level_points,
            freeze_level_points,
            tp_level_points,
        )
    ) else max(all_level_points)
    pip_size = point * 10 if digits in {3, 5} else point
    return {
        "digits": digits,
        "point": point,
        "pip_size": pip_size,
        "stops_level_points": stops_level_points,
        "freeze_level_points": freeze_level_points,
        "sl_level_points": sl_level_points,
        "tp_level_points": tp_level_points,
        "sl_required_points": sl_required_points,
        "tp_required_points": tp_required_points,
        "sl_required_price": sl_required_points * point,
        "tp_required_price": tp_required_points * point,
    }


def _rebase_order_prices(
    trade_params: CapitalOutput,
    direction: str,
    quote: dict,
    constraints: dict,
) -> Optional[dict]:
    """Re-anchor SL/TP to the quote immediately before OrderSendSafe."""
    try:
        bid = float(quote["bid"])
        ask = float(quote["ask"])
        old_entry = float(trade_params.entry_price)
        old_sl = float(trade_params.stop_loss)
        old_tp = float(trade_params.take_profit)
    except (KeyError, TypeError, ValueError):
        return None
    if not all(math.isfinite(value) and value > 0 for value in (bid, ask)):
        return None

    normalized_direction = direction.upper().strip()
    if normalized_direction not in {"BUY", "SELL"}:
        return None
    old_sl_distance = abs(old_entry - old_sl)
    old_tp_distance = abs(old_tp - old_entry)
    point = float(constraints["point"])
    digits = int(constraints["digits"])
    if (
        not math.isfinite(old_sl_distance)
        or not math.isfinite(old_tp_distance)
        or old_sl_distance <= 0
        or old_tp_distance <= 0
        or point <= 0
    ):
        return None

    execution_price = ask if normalized_direction == "BUY" else bid
    sl_reference = bid if normalized_direction == "BUY" else ask
    tp_reference = ask if normalized_direction == "BUY" else bid
    # Add one broker point beyond the reported boundary.  This is derived
    # from the symbol metadata and avoids equality/rounding rejection.
    sl_distance = max(
        old_sl_distance,
        float(constraints["sl_required_price"]) + point,
    )
    tp_distance = max(
        old_tp_distance,
        float(constraints["tp_required_price"]) + point,
    )
    if normalized_direction == "BUY":
        stop_loss = round(sl_reference - sl_distance, digits)
        take_profit = round(tp_reference + tp_distance, digits)
    else:
        stop_loss = round(sl_reference + sl_distance, digits)
        take_profit = round(tp_reference - tp_distance, digits)

    actual_sl_distance = (
        sl_reference - stop_loss
        if normalized_direction == "BUY"
        else stop_loss - sl_reference
    )
    actual_tp_distance = (
        take_profit - tp_reference
        if normalized_direction == "BUY"
        else tp_reference - take_profit
    )
    if actual_sl_distance <= 0 or actual_tp_distance <= 0:
        return None
    pip_size = float(constraints["pip_size"])
    return {
        "entry_price": round(execution_price, digits),
        "stop_loss": stop_loss,
        "take_profit": take_profit,
        "sl_distance": actual_sl_distance,
        "tp_distance": actual_tp_distance,
        "sl_distance_pips": actual_sl_distance / pip_size,
        "tp_distance_pips": actual_tp_distance / pip_size,
    }


def _history_value(record: dict, *keys: str):
    for key in keys:
        value = record.get(key)
        if value is not None and value != "":
            return value
    return None


def _history_float(record: dict, *keys: str) -> Optional[float]:
    value = _history_value(record, *keys)
    try:
        return float(value) if value is not None else None
    except (TypeError, ValueError):
        return None


def _history_datetime(record: dict, *keys: str) -> Optional[str]:
    value = _history_value(record, *keys)
    if value is None:
        return None
    if isinstance(value, (int, float)):
        # mt5rest exposes UTC timestamps in milliseconds.
        return datetime.fromtimestamp(float(value) / 1000, tz=timezone.utc).isoformat()
    return str(value)


def classify_close_reason(
    history: dict,
    entry: dict,
    trailing_stop: Optional[float] = None,
) -> str:
    """Classify a completed position using broker history, never floating P&L."""
    comment = str(_history_value(history, "closeComment", "comment") or "").lower()
    comment_words = set(re.findall(r"[a-z]+", comment))
    if (
        "takeprofit" in comment_words
        or {"take", "profit"}.issubset(comment_words)
        or "tp" in comment_words
    ):
        return "TP"
    if (
        "stoploss" in comment_words
        or {"stop", "loss"}.issubset(comment_words)
        or "sl" in comment_words
    ):
        return "SL"
    if (
        "breakeven" in comment_words
        or {"break", "even"}.issubset(comment_words)
    ):
        return "BE"
    if "trailing" in comment_words or "trail" in comment_words:
        return "Trailing"

    close_price = _history_float(history, "closePrice", "price")
    original_sl = _history_float(entry, "sl", "stop_loss")
    original_tp = _history_float(entry, "tp", "take_profit")
    tolerance = max(abs(close_price or 0.0) * 0.00005, 0.01)
    if close_price is not None and original_tp is not None:
        if abs(close_price - original_tp) <= tolerance:
            return "TP"
    if close_price is not None and original_sl is not None:
        if abs(close_price - original_sl) <= tolerance:
            return "SL"
    if close_price is not None and trailing_stop is not None:
        if abs(close_price - trailing_stop) <= tolerance:
            entry_price = _history_float(entry, "entry", "open_price")
            if entry_price is not None and original_sl is not None:
                if abs(trailing_stop - entry_price) <= tolerance:
                    return "BE"
            return "Trailing"
    return "Manual"


class GoldScalperLive:
    def __init__(self):
        self.running: bool = True
        self.paused:  bool = False
        self.loop_count: int = 0
        # Multi-TF bar tracking: one last-seen bar-time per trade timeframe.
        # Initialised to None so the first bar on every TF is always processed.
        self._last_bar_times: dict[str, Optional[datetime]] = {
            tf: None for tf in TRADE_TIMEFRAMES
        }
        # Latest completed bar time observed for non-trade context timeframes
        # (currently H1).  It lets the MTF analysis cache refresh exactly once
        # per new HTF bar instead of once per M5/M10/M15/M20 entry event.
        self._latest_completed_bar_times: dict[str, Optional[datetime]] = {}
        self._sl_atr_cache_bar_time: Optional[datetime] = None
        self._sl_atr_cache_value: float = 0.0
        self._sl_atr_cache_reason: str = "initial"
        self._sl_atr_cache_failure_at: float = 0.0
        self._mtf_cache_bar_time: Optional[datetime] = None
        self._mtf_cache_bias: Optional[MtfBias] = None
        self._mtf_cache_reason: str = ""
        self._mtf_cache_failure_at: float = 0.0
        # Guard against opening multiple trades in the same tick when several
        # timeframes close simultaneously (e.g. M20+M10+M5 all fire at :20).
        # Without this, _on_new_bar() is called N times in one iteration of
        # _run_loop() and each call may see 0 open positions from mt5rest (the
        # bridge has not yet registered the trade placed by the previous call),
        # causing N trades to open instead of 1.
        self._trade_opened_this_tick: bool = False
        # Serializes the final position re-check and broker order placement.
        # This remains safe if entry evaluation is ever scheduled concurrently
        # (for example, after adding independent timeframe workers).
        self._entry_lock = asyncio.Lock()
        # tracks the last successfully placed trade (direction + bar_time)
        # so the post-SL cooldown gate can detect same-direction re-entry.
        self._last_entry_bar_time: Optional[datetime] = None
        self._last_entry_direction: str = ""
        self.trade_history: List[dict] = []
        self._last_open_positions: list[dict] = []
        self.last_decision: Optional[DecisionResult] = None
        # Structured telemetry for the most recently evaluated closed candle.
        # It is mirrored into robot_state.json and emitted as one JSON log
        # record per scan, so Render logs can be analysed without parsing
        # human-readable messages.
        self._last_candle_telemetry: dict = {}
        # This is deliberately independent from DecisionResult.allowed.
        # The decision engine reports signal eligibility; this state records
        # the result of every live entry gate before an order is sent.
        self._last_trade_permission: dict = {
            "allowed": False,
            "stage": "NOT_EVALUATED",
            "reasons": ["No live entry gate evaluation yet"],
        }
        self._close_history_checked_at: dict[str, float] = {}
        self._open_position_snapshot_initialized = False
        self._last_history_sync_at: float = 0.0
        self._history_sync_status: dict = {
            "status": "NOT_STARTED",
            "lookback_days": HISTORY_LOOKBACK_DAYS,
            "last_sync_at": None,
            "deals_read": 0,
            "positions_merged": 0,
            "closed_positions": 0,
        }

        # Risk Guardian — initialized after the MTAPI broker connection
        self.guardian = RiskGuardian(
            daily_loss_limit_pct=DAILY_LOSS_LIMIT_PCT,
            max_drawdown_pct=MAX_DRAWDOWN_PCT,
        )
        self._last_guardian_status: Optional[GuardianStatus] = None
        self._last_acc_info: Optional[dict] = None  # cache for WAITING state writes
        self._last_acc_refresh_ts: float = 0.0        # monotonic time of last between-bar refresh

        # Exponential backoff state
        self._reconnect_attempts: int = 0
        self._watchdog_task: asyncio.Task | None = None

        # Consecutive account-info failures before writing DISCONNECTED state.
        # This prevents a single transient network hiccup from triggering a
        # "Connection Lost" alert — we retry once silently first.
        self._consecutive_acc_failures: int = 0

        # ── Adaptive ATR Trailing Stop ────────────────────────────────────────
        # Toggleable at runtime via the Telegram panel's "Auto Trail" switch
        # (routed through the update_risk command — see _process_commands).
        self.trailing_enabled: bool = TRAIL_ENABLED
        self._trailing_cfg = AdaptiveTrailingConfig(
            enabled=TRAIL_ENABLED,
            atr_period=TRAIL_ATR_PERIOD,
            chandelier_atr_multiplier=TRAIL_CHANDELIER_ATR_MULTIPLIER,
            normal_multiplier=TRAIL_NORMAL_MULTIPLIER,
            tight_multiplier=TRAIL_TIGHT_MULTIPLIER,
            exhaustion_confirm_count=TRAIL_EXHAUSTION_CONFIRM_COUNT,
            momentum_lookback=TRAIL_MOMENTUM_LOOKBACK,
            body_shrink_ratio=TRAIL_BODY_SHRINK_RATIO,
            volume_shrink_ratio=TRAIL_VOLUME_SHRINK_RATIO,
            min_profit_atr_multiple=TRAIL_MIN_PROFIT_ATR,
            min_tight_distance_atr=TRAIL_MIN_DISTANCE_ATR,
            min_step_price=TRAIL_MIN_STEP_PRICE,
        )
        # Baseline for EACH currently open position, keyed by str(ticket id):
        # its ORIGINAL entry price and ORIGINAL risk distance (never mutated
        # once a trade opens), so the staircase always measures R from where
        # the trade actually started — not from wherever the stop happens to
        # be right now.
        #
        # FIX: this used to be a single dict, so with more than one position
        # open at once (MAX_OPEN_TRADES bypass, or manual multi-entry) only
        # the FIRST position returned by mt5rest ever got its stop trailed —
        # every other open position's SL sat frozen at its entry level
        # forever, no matter how far price ran in its favour. Keying by
        # ticket lets every open position get its own independent staircase.
        self._trail_baselines: dict = {}  # includes the entry timeframe
        self._last_trailing_statuses: dict = {}  # {str(ticket): status-dict}, for panel telemetry
        # Cached closed candles per trade timeframe. The trailing engine runs
        # between bars and must use the timeframe on which each position opened.
        self._last_candles_by_timeframe: dict[str, list] = {}
        self._last_atr_by_timeframe: dict[str, float] = {}
        # Kept as a compatibility telemetry value for the panel.
        self._last_atr: float = 0.0
        self._trailing_sync_reason: str = "initial"

    # ── Entry point ───────────────────────────────────────────────────────────

    async def start(self) -> bool:
        log.info("=" * 60)
        log.info("  GoldScalperPro v4 — LIVE TRADING ENGINE (MTAPI)")
        log.info(f"  Symbol: {SYMBOL}  |  Trade TFs: {chr(44).join(TRADE_TIMEFRAMES)} (highest first)")
        log.info(f"  Max positions: {MAX_OPEN_TRADES}")
        log.info(f"  Hedged positions: {'enabled' if ALLOW_HEDGED_POSITIONS else 'disabled'}")
        log.info(f"  Min confirmations: {MIN_CONFIRMATIONS}")
        log.info(f"  Trend min confirmations: {TREND_MIN_CONFIRMATIONS}")
        log.info(
            f"  Protective SL ATR: {SL_ATR_TIMEFRAME}/{SL_ATR_PERIOD} "
            f"base={SL_ATR_BASE_MULTIPLIER:.2f}x "
            f"low-vol-add={LOW_VOLATILITY_SL_ATR_ADD:.2f}x"
        )
        log.info(
            f"  MTF: {'ON' if MTF_ENABLED else 'OFF'} "
            f"(opposition threshold={MTF_OPPOSITION_THRESHOLD:.1f}, "
            f"dry-run={'on' if MTF_DRY_RUN else 'off'})"
        )
        log.info(f"  Risk per trade: {RISK_PERCENT:.2f}%")
        log.info(f"  Daily loss limit: {DAILY_LOSS_LIMIT_PCT}%  |  "
                 f"Max drawdown: {MAX_DRAWDOWN_PCT}%  |  "
                 f"Slippage: ≤{SLIPPAGE_POINTS}pts")
        log.info("=" * 60)

        self._write_state("STARTING")

        # Restore trade history from previous session (survives restarts)
        self.trade_history = self._load_trade_history()
        # Restore per-ticket entry/initial-SL baselines before the first broker
        # sync.  These are persisted separately from trade history because the
        # live SL changes while the original R baseline must never change.
        self._load_trailing_state()

        connected = await connect_with_retry(max_attempts=12, retry_delay=30.0)
        if not connected:
            log.error(
                "MTAPI connection failure after all retry attempts. "
                "Check MTAPI_URL, MT5_HOST, MT5_USER, and MT5_PASSWORD."
            )
            self._write_state("DISCONNECTED",
                              extra={"error": "MTAPI connection failed after retries"})
            return False  # non-False return signals failure to main.py for sys.exit(1)

        # ── Read-only broker checks — no order is placed here ──────────────────
        if not await check_symbol_available(SYMBOL):
            self._write_state(
                "DISCONNECTED",
                extra={"error": f"{SYMBOL} is not available through MTAPI"},
            )
            return False

        quote = await get_current_quote(SYMBOL)
        if quote:
            log.info(
                f"{SYMBOL} market data: AVAILABLE "
                f"(bid={quote['bid']}, ask={quote['ask']})"
            )
        else:
            # A closed market can have no current quote even when the symbol
            # and its historical data are available. Do not block a deployment
            # solely because the broker is outside trading hours.
            log.warning(
                f"{SYMBOL} market data: symbol is available but no live quote "
                "was returned; the market may be closed"
            )

        # One serialized watchdog owns all proactive MTAPI health checks and
        # reconnects.  Older versions started three independent keepalive
        # tasks, which sent overlapping account-info requests and could make a
        # transient packet loss look like a full broker disconnect.
        self._watchdog_task = asyncio.create_task(
            start_connection_watchdog(interval_seconds=30.0), name="mt5_watchdog"
        )
        try:
            # ── Guardian state restore (VB-02) ───────────────────────────────────
            # Redis is the cross-service persistent copy on Render; the local file
            # remains a fallback for local/single-process deployments. If Guardian
            # was halted before restart it must remain halted, and healthy baselines
            # must also be restored so a restart cannot reset the risk window.
            # State older than 26 hours is considered stale and is discarded.
            # ROOT-CAUSE FIX: Fetch live account info FIRST so we can:
            #   a) supply account identity to the Guardian before restore_state(),
            #      which lets it reject stale state from a different account; and
            #   b) always call initialize() when restore_state() is rejected.
            acc_info = await get_account_info()
            if acc_info:
                self._last_acc_info = acc_info
                log.info(
                    "MT5 account connection status: CONNECTED "
                    f"(account={str(acc_info.get('login') or 'unknown')[:3]}***, "
                    f"host={acc_info.get('server') or 'unknown'}, "
                    f"balance={float(acc_info.get('balance', 0.0)):.2f}, "
                    f"equity={float(acc_info.get('equity', 0.0)):.2f})"
                )
                _live_login  = str(acc_info.get("login",  ""))
                _live_server = str(acc_info.get("server", ""))
                self.guardian.set_account_identity(_live_login, _live_server)
            else:
                log.warning("MT5 account connection status: CONNECTED but account summary unavailable")

            _guardian_restored = False
            try:
                _gs_data = None
                try:
                    from live_trading.redis_ipc import (
                        redis_read_guardian_state,
                        redis_available,
                    )
                    if redis_available():
                        _gs_data = redis_read_guardian_state()
                except Exception as _redis_gs_exc:
                    log.warning(
                        f"Could not read Guardian state from Redis — "
                        f"falling back to disk: {_redis_gs_exc}"
                    )

                if _gs_data is None and os.path.exists(GUARDIAN_STATE_FILE):
                    with open(GUARDIAN_STATE_FILE, "r", encoding="utf-8") as _gsf:
                        _gs_data = json.load(_gsf)

                if _gs_data:
                    _written_at_str = _gs_data.get("written_at")
                    _state_fresh = False
                    if _written_at_str:
                        try:
                            _written_at = datetime.fromisoformat(_written_at_str)
                            if _written_at.tzinfo is None:
                                _written_at = _written_at.replace(tzinfo=timezone.utc)
                            _age_hours = (
                                (datetime.now(timezone.utc) - _written_at).total_seconds()
                                / 3600
                            )
                            if _age_hours <= 26:
                                _state_fresh = True
                            else:
                                log.warning(
                                    f"Guardian state on disk is stale "
                                    f"({_age_hours:.1f}h old, limit=26h) — cold start"
                                )
                        except Exception as _ts_exc:
                            log.warning(
                                f"Could not parse Guardian state timestamp — cold start: {_ts_exc}"
                            )
                    if _state_fresh:
                        _restored = self.guardian.restore_state(_gs_data)
                        if _restored:
                            if _gs_data.get("halted"):
                                self.paused = True
                                log.critical(
                                    "🛡️  Guardian HALT restored — trading PAUSED.  "
                                    "Use /reset_guardian in Telegram to resume."
                                )
                                # ROOT-CAUSE FIX: without this write, the paused loop
                                # below never calls _write_state() again (it takes the
                                # `if self.paused: sleep; continue` branch before ever
                                # reaching a state-writing code path), so the state
                                # file/Redis heartbeat freezes forever at the last
                                # RUNNING write and /status looks like a dead/crashed
                                # robot even though it is alive and correctly halted.
                                self._write_state(
                                    "PAUSED",
                                    self._last_acc_info,
                                    extra={"guardian_halt_reason": _gs_data.get("halt_reason", "restored from previous session")},
                                )
                            else:
                                log.info(
                                    "🛡️  Guardian baseline restored — "
                                    "risk window continues across restart."
                                )
                            _guardian_restored = True
                        # else: restore_state() rejected state → fall through to initialize()
            except Exception as _gs_exc:
                log.warning(
                    f"Could not read Guardian state file — cold start: {_gs_exc}"
                )

            # Initialise Guardian with live account data when no valid persisted
            # state was found.  This also covers the case where restore_state()
            # rejected the state due to account-identity or baseline mismatch
            # (the most common cause of false "violation" alerts on a demo account).
            if not _guardian_restored:
                balance = float((acc_info or {}).get("balance", 0))
                equity  = float((acc_info or {}).get("equity",  0))
                if balance > 0:
                    self.guardian.initialize(balance, equity)
                else:
                    # Re-fetch if the first attempt returned empty/no data
                    _retry_acc = await get_account_info()
                    if _retry_acc:
                        self._last_acc_info = _retry_acc
                        balance = float(_retry_acc.get("balance", 0))
                        equity  = float(_retry_acc.get("equity",  0))
                    if balance > 0:
                        self.guardian.initialize(balance, equity)
                    else:
                        log.warning(
                            "Could not fetch balance for Guardian initialization — "
                            "Guardian will block trades until account data is available"
                        )

            _checkpoint("before calibrate_wyckoff")
            await self._sync_broker_trade_history(force=True)
            _checkpoint("after broker history sync")
            await self._run_stage(
                "startup trailing sync",
                self._sync_trailing_positions("startup"),
            )
            await self._calibrate_wyckoff()
            _checkpoint("after calibrate_wyckoff")
            # Write RUNNING state immediately after connect with real account data
            # so the panel shows live balance before the first bar fires.
            self._write_state("RUNNING", self._last_acc_info)
            await self._run_loop()
        finally:
            # If startup fails before _run_loop() is entered, its finally block
            # cannot clean up the watchdog. Handle that early-exit path here.
            if self._watchdog_task and not self._watchdog_task.done():
                self._watchdog_task.cancel()
                try:
                    await self._watchdog_task
                except asyncio.CancelledError:
                    pass
                log.debug("MetaAPI watchdog cancelled in start() finally.")

    # ── Wyckoff calibration ───────────────────────────────────────────────────

    async def _calibrate_wyckoff(self) -> None:
        log.info("Calibrating Wyckoff config from live data …")
        candles = await fetch_candles(SYMBOL, TIMEFRAME, 500)
        if candles:
            cfg = calibrate_wyckoff(candles)
            set_calibrated_config(cfg)
            log.info(f"Wyckoff calibrated — "
                     f"maxRangePct={cfg.max_range_pct:.5f}  "
                     f"springMargin={cfg.spring_margin:.2f}")
        else:
            log.warning("Could not fetch candles for Wyckoff calibration; "
                        "using defaults")

    # ── Main async loop ───────────────────────────────────────────────────────

    async def _run_loop(self) -> None:
        log.info(
            f"Entering main loop — checking every {BAR_CHECK_INTERVAL}s "
            f"across {len(TRADE_TIMEFRAMES)} timeframes: "
            + ", ".join(TRADE_TIMEFRAMES) + " …"
        )
        try:
            while self.running:
                _checkpoint(f"loop#{self.loop_count} tick start")
                self._set_trade_permission(
                    False,
                    "NOT_EVALUATED",
                    ["No active entry evaluation"],
                )
                await self._process_commands()
                _checkpoint(f"loop#{self.loop_count} commands processed")

                if self.paused:
                    # Keep the heartbeat alive while paused so /status reflects
                    # reality (PAUSED with a fresh timestamp) instead of freezing
                    # at the last pre-pause state and looking like a dead process.
                    self._write_state("PAUSED", self._last_acc_info)
                    await asyncio.sleep(BAR_CHECK_INTERVAL)
                    continue

                # ── Reconnect with exponential backoff ────────────────────────
                ok = await ensure_connected(attempt=self._reconnect_attempts + 1)
                _checkpoint(f"loop#{self.loop_count} ensure_connected -> {ok}")
                if not ok:
                    self._reconnect_attempts += 1
                    backoff = min(
                        _RECONNECT_BASE * (2 ** (self._reconnect_attempts - 1)),
                        _RECONNECT_MAX_DELAY,
                    )
                    log.error(
                        f"Reconnect failed (attempt #{self._reconnect_attempts}) "
                        f"— backing off {backoff:.0f}s …"
                    )
                    self._write_state("DISCONNECTED")
                    await asyncio.sleep(backoff)
                    continue

                # Successful (re)connect — reset backoff counter
                if self._reconnect_attempts > 0:
                    log.info(
                        f"✅ Reconnected after {self._reconnect_attempts} attempt(s)"
                    )
                    await self._run_stage(
                        "reconnect trailing sync",
                        self._sync_trailing_positions("reconnect"),
                    )
                    self._reconnect_attempts = 0

                # Staircase trailing stop — checked every tick (not just on
                # candle close) so it reacts within seconds of price crossing
                # a step, not up to 5 minutes late.
                await self._run_stage(
                    "trailing stop management",
                    self._manage_trailing_stop(),
                )
                _checkpoint(f"loop#{self.loop_count} trailing stop managed")
                await self._run_stage(
                    "closed-trade reconciliation",
                    self._reconcile_closed_trades(),
                )
                _checkpoint(f"loop#{self.loop_count} closed trades reconciled")
                await self._run_stage(
                    "broker history sync",
                    self._sync_broker_trade_history(),
                )
                _checkpoint(f"loop#{self.loop_count} broker history synced")

                # Reset the within-tick trade guard before processing this
                # tick's bars.  All _on_new_bar() calls that share this tick
                # (e.g. M20+M10+M5 closing simultaneously) will see the same
                # flag and only the first successful placement will go through.
                self._trade_opened_this_tick = False
                new_bars = await self._run_stage(
                    "new-bar polling",
                    self._check_new_bars(),
                )
                _checkpoint(f"loop#{self.loop_count} new_bars={len(new_bars)}")
                if new_bars:
                    for _tf, _bar_time in new_bars:
                        self.loop_count += 1
                        log.info(
                            f"─── Bar #{self.loop_count} [{_tf}] "
                            f"at {_bar_time.isoformat()} ───"
                        )
                        await self._run_stage(
                            f"{_tf} bar processing",
                            self._on_new_bar(_bar_time, _tf),
                        )
                else:
                    # Refresh account info every _ACC_REFRESH_INTERVAL seconds
                    # so the panel shows current balance/equity between candles.
                    now_ts = asyncio.get_event_loop().time()
                    if now_ts - self._last_acc_refresh_ts >= _ACC_REFRESH_INTERVAL:
                        try:
                            fresh = await get_account_info()
                            if fresh and "balance" in fresh and "equity" in fresh:
                                self._last_acc_info = fresh
                                self._last_acc_refresh_ts = now_ts
                                log.debug(
                                    f"Between-bar acc refresh: "
                                    f"balance={fresh['balance']:.2f}  "
                                    f"equity={fresh['equity']:.2f}"
                                )
                        except Exception as _acc_err:
                            log.debug(f"Between-bar acc refresh skipped: {_acc_err}")
                    self._write_state("WAITING", self._last_acc_info)

                await asyncio.sleep(BAR_CHECK_INTERVAL)

        except asyncio.CancelledError:
            log.info("Loop cancelled")
        except Exception as exc:
            log.exception(f"Fatal error in main loop: {exc}")
            self._write_state("ERROR", extra={"error": str(exc)})
            raise  # Re-raise to supervisor — do NOT sys.exit() here.
            # sys.exit() kills the *entire* process and bypasses the supervisor's
            # exponential-backoff restart logic.  Raising lets the supervisor in
            # server.py catch the exception, apply the configured backoff, and
            # restart the engine without Render having to restart the container.
        finally:
            # Cancel the single MetaAPI watchdog so it does not remain orphaned
            # when the supervisor restarts the engine in the same event loop.
            if self._watchdog_task and not self._watchdog_task.done():
                self._watchdog_task.cancel()
                try:
                    await self._watchdog_task
                except asyncio.CancelledError:
                    pass
                log.debug("MetaAPI connection watchdog cancelled.")
            await disconnect()
            self._write_state("STOPPED")
            log.info("Engine stopped.")

    async def _run_stage(self, name: str, operation):
        """Prevent one stalled MetaAPI operation from freezing the engine."""
        timeout = max(RPC_CALL_TIMEOUT * 3, 90)
        try:
            return await asyncio.wait_for(operation, timeout=timeout)
        except asyncio.TimeoutError as exc:
            log.error(
                f"Trading stage '{name}' exceeded {timeout}s; "
                "forcing a clean MetaAPI reconnect."
            )
            self._write_state(
                "DISCONNECTED",
                self._last_acc_info,
                extra={"error": f"{name} timeout"},
            )
            raise RuntimeError(f"Trading stage timed out: {name}") from exc

    # ── Bar detection ─────────────────────────────────────────────────────────

    async def _check_new_bars(self) -> list[tuple[str, datetime]]:
        """Poll every configured trade timeframe and return a list of
        (timeframe, bar_time) pairs for every TF that has a new completed bar
        since the last tick.  Results preserve TRADE_TIMEFRAMES order, which
        is already sorted highest-first, so M20 signals are processed before
        M15, then M10, then M5.  If M20 opens a position, the M15/M10/M5
        handlers in the same tick will see it via get_open_positions() and
        skip entry, preventing duplicate positions.
        Never raises — individual TF errors are logged and skipped."""
        results: list[tuple[str, datetime]] = []
        poll_timeframes = list(TRADE_TIMEFRAMES)
        if MTF_ENABLED and MTF_TIMEFRAME not in poll_timeframes:
            # Poll the HTF timestamp alongside entry timeframes so the cache
            # knows when a new H1 context is available, without fetching the
            # full H1 candle window on every lower-timeframe bar.
            poll_timeframes.append(MTF_TIMEFRAME)
        if SL_ATR_TIMEFRAME not in poll_timeframes:
            # Keep the higher-timeframe ATR cache aligned with the completed
            # bar that produced it; do not fetch M5 ATR on every M1 signal.
            poll_timeframes.append(SL_ATR_TIMEFRAME)

        async def _poll(tf: str):
            try:
                return tf, await get_last_completed_bar_time(SYMBOL, tf), None
            except Exception as exc:
                return tf, None, exc

        polled = await asyncio.gather(*(_poll(tf) for tf in poll_timeframes))
        trade_timeframes = set(TRADE_TIMEFRAMES)
        for tf, bt, poll_error in polled:
            try:
                if poll_error is not None:
                    log.warning(f"[{tf}] Bar time check failed: {poll_error}")
                    continue
                if bt is None:
                    continue
                # ── Staleness guard ──────────────────────────────────────────
                # Right after MT5 connects, PriceHistoryV2 returns cached
                # historical data (sometimes years old) until the terminal
                # finishes syncing from the broker.  Processing a 2022 bar in
                # 2026 context would crash the signal pipeline or open a trade
                # with completely wrong ATR/SL/TP values.  Skip any bar that is
                # more than 2 hours old relative to UTC wall-clock time.
                # Normalize to naive UTC immediately.  get_last_completed_bar_time()
                # may return timezone-aware datetimes (when the mt5rest response
                # includes a "Z" suffix) on some timeframes and naive on others.
                # Storing a mix into _last_bar_times causes max() inside
                # _write_state() to raise:
                #   TypeError: can't compare offset-naive and offset-aware datetimes
                # which silently crashes every _write_state() call (WAITING, ERROR,
                # STOPPED) — leaving the state file permanently frozen at RUNNING.
                _bt_naive = _normalize_bar_time(bt)
                if _bt_naive is None:
                    log.warning(f"[{tf}] Ignoring bar with invalid timestamp: {bt!r}")
                    continue
                _stale_cutoff = datetime.now(timezone.utc).replace(
                    tzinfo=None
                ) - timedelta(hours=2)
                if _bt_naive < _stale_cutoff:
                    # Do not advance the processed-bar cursor with stale
                    # history.  After the broker finishes synchronizing, the
                    # first genuinely live closed candle must still be
                    # eligible for processing.
                    log.debug(
                        f"[{tf}] Bar {bt.isoformat()} is stale "
                        f"(>{int((datetime.now(timezone.utc).replace(tzinfo=None) - _bt_naive).total_seconds()/3600)}h old) "
                        f"— waiting for MT5 historical data sync"
                    )
                    continue
                if tf in {MTF_TIMEFRAME, SL_ATR_TIMEFRAME}:
                    self._latest_completed_bar_times[tf] = _bt_naive
                if tf not in trade_timeframes:
                    self._latest_completed_bar_times[tf] = _bt_naive
                    continue
                prev = self._last_bar_times.get(tf)
                if prev is None or _bt_naive > prev:
                    self._last_bar_times[tf] = _bt_naive
                    results.append((tf, _bt_naive))
            except Exception as _bar_err:
                log.warning(f"[{tf}] Bar time check failed: {_bar_err}")
        return results

    # ── Per-bar handler ───────────────────────────────────────────────────────

    def _mtf_cache_needs_refresh(self) -> bool:
        """Return whether the cached HTF bias is absent or belongs to an old bar."""
        if not MTF_ENABLED:
            return False
        current_bar = self._latest_completed_bar_times.get(MTF_TIMEFRAME)
        if self._mtf_cache_bias is not None and current_bar == self._mtf_cache_bar_time:
            return False
        # Retry a failed HTF fetch periodically, but do not hammer the bridge
        # once per lower-timeframe bar during a transient outage.
        if (
            self._mtf_cache_bias is None
            and current_bar == self._mtf_cache_bar_time
            and asyncio.get_event_loop().time() - self._mtf_cache_failure_at < 30.0
        ):
            return False
        return True

    async def _refresh_mtf_cache(self) -> tuple[Optional[MtfBias], str]:
        """Fetch and validate HTF candles once for the current HTF bar."""
        cache_bar = self._latest_completed_bar_times.get(MTF_TIMEFRAME)
        try:
            htf_candles = await fetch_candles(
                SYMBOL, MTF_TIMEFRAME, MTF_CANDLE_WINDOW
            )
            if len(htf_candles) < 50:
                reason = f"HTF candles insufficient ({len(htf_candles)})"
                self._mtf_cache_bias = None
                self._mtf_cache_reason = reason
                self._mtf_cache_bar_time = cache_bar
                self._mtf_cache_failure_at = asyncio.get_event_loop().time()
                return None, reason

            htf_bias: Optional[MtfBias] = None
            if MTF_TIMEFRAME in {"H1", "1h"}:
                h1_check = validate_h1_candles(htf_candles)
                if not h1_check.valid:
                    reason = h1_check.reason
                    self._mtf_cache_bias = None
                    self._mtf_cache_reason = reason
                    self._mtf_cache_bar_time = cache_bar
                    self._mtf_cache_failure_at = asyncio.get_event_loop().time()
                    return None, reason
                candidate_bias = compute_mtf_bias(htf_candles)
                if not h1_check.matches_ema(
                    candidate_bias.ema50,
                    candidate_bias.ema100,
                    candidate_bias.ema200,
                ):
                    reason = (
                        "H1 validation failed: EMA values changed between "
                        "validation and HTF analysis"
                    )
                    self._mtf_cache_bias = None
                    self._mtf_cache_reason = reason
                    self._mtf_cache_bar_time = cache_bar
                    self._mtf_cache_failure_at = asyncio.get_event_loop().time()
                    return None, reason
                candidate_bias.reasoning.insert(0, h1_check.summary)
                htf_bias = candidate_bias
            else:
                htf_bias = compute_mtf_bias(htf_candles)

            self._mtf_cache_bias = htf_bias
            self._mtf_cache_reason = ""
            self._mtf_cache_bar_time = cache_bar
            self._mtf_cache_failure_at = 0.0
            return htf_bias, ""
        except Exception as exc:
            reason = f"MTF fetch/analysis error (fail-safe): {exc}"
            self._mtf_cache_bias = None
            self._mtf_cache_reason = reason
            self._mtf_cache_bar_time = cache_bar
            self._mtf_cache_failure_at = asyncio.get_event_loop().time()
            return None, reason

    def _sl_atr_cache_needs_refresh(self) -> bool:
        current_bar = self._latest_completed_bar_times.get(SL_ATR_TIMEFRAME)
        if (
            self._sl_atr_cache_value > 0.0
            and current_bar == self._sl_atr_cache_bar_time
        ):
            return False
        if (
            self._sl_atr_cache_value <= 0.0
            and current_bar == self._sl_atr_cache_bar_time
            and asyncio.get_event_loop().time() - self._sl_atr_cache_failure_at < 30.0
        ):
            return False
        return True

    async def _refresh_sl_atr_cache(self) -> tuple[float, str]:
        """Fetch ATR from the configured higher timeframe for protective SLs."""
        cache_bar = self._latest_completed_bar_times.get(SL_ATR_TIMEFRAME)
        try:
            sl_candles = await fetch_candles(
                SYMBOL,
                SL_ATR_TIMEFRAME,
                max(100, SL_ATR_PERIOD + 1),
            )
            if len(sl_candles) < SL_ATR_PERIOD + 1:
                reason = (
                    f"SL ATR candles insufficient ({len(sl_candles)}; "
                    f"need {SL_ATR_PERIOD + 1})"
                )
                self._sl_atr_cache_value = 0.0
                self._sl_atr_cache_reason = reason
                self._sl_atr_cache_bar_time = cache_bar
                self._sl_atr_cache_failure_at = asyncio.get_event_loop().time()
                return 0.0, reason
            value = calc_atr(sl_candles, SL_ATR_PERIOD)
            if value <= 0.0:
                reason = "SL ATR calculation returned zero"
                self._sl_atr_cache_value = 0.0
                self._sl_atr_cache_reason = reason
                self._sl_atr_cache_bar_time = cache_bar
                self._sl_atr_cache_failure_at = asyncio.get_event_loop().time()
                return 0.0, reason
            self._sl_atr_cache_value = value
            self._sl_atr_cache_reason = ""
            self._sl_atr_cache_bar_time = cache_bar
            return value, ""
        except Exception as exc:
            reason = f"SL ATR fetch/calculation error: {exc}"
            self._sl_atr_cache_value = 0.0
            self._sl_atr_cache_reason = reason
            self._sl_atr_cache_bar_time = cache_bar
            self._sl_atr_cache_failure_at = asyncio.get_event_loop().time()
            return 0.0, reason

    async def _on_new_bar(self, bar_time: datetime, tf: str = TIMEFRAME) -> None:
        self._set_trade_permission(
            False,
            "EVALUATING",
            [f"Evaluating live entry gates for {tf}"],
        )
        # Fetch independent network inputs concurrently.  The account request
        # remains mandatory; this only removes avoidable wait time between
        # independent bridge calls.
        candles_task = asyncio.create_task(
            fetch_candles(SYMBOL, tf, CANDLE_WINDOW)
        )
        account_task = asyncio.create_task(get_account_info())
        mtf_task = (
            asyncio.create_task(self._refresh_mtf_cache())
            if self._mtf_cache_needs_refresh()
            else None
        )
        sl_atr_task = (
            asyncio.create_task(self._refresh_sl_atr_cache())
            if self._sl_atr_cache_needs_refresh()
            else None
        )
        tasks = [candles_task, account_task]
        if mtf_task is not None:
            tasks.append(mtf_task)
        if sl_atr_task is not None:
            tasks.append(sl_atr_task)
        results = await asyncio.gather(*tasks)
        candles, acc_info = results[0], results[1]
        result_index = 2
        if mtf_task is not None:
            htf_bias, htf_data_reason = results[result_index]
            result_index += 1
        else:
            htf_bias = self._mtf_cache_bias if MTF_ENABLED else None
            htf_data_reason = self._mtf_cache_reason if MTF_ENABLED else ""
        if sl_atr_task is not None:
            sl_atr, sl_atr_reason = results[result_index]
        else:
            sl_atr, sl_atr_reason = (
                self._sl_atr_cache_value,
                self._sl_atr_cache_reason,
            )

        # 1. Fetch candles for this timeframe (M5 / M10 / M15 / M20)
        if len(candles) < 50:
            log.warning(f"Only {len(candles)} candles returned — skipping bar")
            return
        if sl_atr <= 0.0:
            self._set_trade_permission(
                False,
                "SL_ATR_DATA_UNAVAILABLE",
                [sl_atr_reason or "Higher-timeframe SL ATR is unavailable"],
            )
            log.warning(
                f"[{tf}] Skipping entry — higher-timeframe "
                f"SL ATR ({SL_ATR_TIMEFRAME}) unavailable: "
                f"{sl_atr_reason or 'unknown reason'}"
            )
            return
        # Keep the latest closed window for adaptive trailing on this exact
        # trade timeframe. A single global ATR is incorrect when M1/M5/M15
        # positions are open together.
        self._last_candles_by_timeframe[tf] = list(candles)
        self._last_atr_by_timeframe[tf] = atr(candles, TRAIL_ATR_PERIOD)

        # The bar detector and the signal fetch are separate broker requests.
        # A reconnect can make the detector see a fresh timestamp while the
        # history endpoint still serves a cached window.  Never run the
        # decision engine on that cached window: it is exactly how the panel
        # can show an old candle_time while the scan itself is current.
        _trigger_time = _normalize_bar_time(bar_time)
        _signal_time = _normalize_bar_time(candles[-1].time)
        if _trigger_time is None or _signal_time is None:
            self._set_trade_permission(
                False,
                "INVALID_CANDLE_TIME",
                ["Could not normalize the live candle timestamp"],
            )
            log.warning(f"[{tf}] Skipping bar with invalid candle timestamp")
            return

        _now_naive = datetime.now(timezone.utc).replace(tzinfo=None)
        if _signal_time < _now_naive - timedelta(hours=2):
            reason = (
                f"Latest closed candle {_signal_time.isoformat()} is stale; "
                "waiting for broker history synchronization"
            )
            self._set_trade_permission(False, "STALE_CANDLE_DATA", [reason])
            log.warning(f"[{tf}] {reason}")
            self._write_state(
                "WAITING",
                acc_info,
                extra={"candle_sync": {
                    "status": "STALE",
                    "trigger_time": _trigger_time.isoformat(),
                    "latest_candle_time": _signal_time.isoformat(),
                }},
            )
            return

        if _signal_time < _trigger_time:
            reason = (
                f"Fetched candle {_signal_time.isoformat()} is older than "
                f"trigger {_trigger_time.isoformat()}"
            )
            self._set_trade_permission(False, "STALE_CANDLE_DATA", [reason])
            log.warning(f"[{tf}] {reason} — refusing to trade on stale history")
            self._write_state(
                "WAITING",
                acc_info,
                extra={"candle_sync": {
                    "status": "LAGGING",
                    "trigger_time": _trigger_time.isoformat(),
                    "latest_candle_time": _signal_time.isoformat(),
                }},
            )
            return

        # A candle may close between the polling request and this history
        # request.  Anchor all cooldown/session/order telemetry to the actual
        # candle that was analysed and advance the cursor so the same candle
        # is not evaluated twice on the next poll.
        if _signal_time > _trigger_time:
            log.info(
                f"[{tf}] History advanced while fetching: "
                f"{_trigger_time.isoformat()} → {_signal_time.isoformat()}"
            )
        bar_time = _signal_time
        self._last_bar_times[tf] = _signal_time

        if htf_bias is not None:
            log.info(
                f"[{tf}] HTF ({MTF_TIMEFRAME}) bias: {htf_bias.direction}  "
                f"trend={htf_bias.trend}  smc={htf_bias.smc_signal}  "
                f"regime={htf_bias.regime}  strength={htf_bias.strength}"
            )
        elif MTF_ENABLED and htf_data_reason:
            log.warning(f"[{tf}] {htf_data_reason} — blocking entry")

        # 2. Account info (live, required for Guardian)
        # Never continue with fabricated account values.  A failed account
        # request must block trading rather than make the risk guard appear
        # healthy and allow an order with stale/default data.
        if not acc_info or "balance" not in acc_info or "equity" not in acc_info:
            self._consecutive_acc_failures += 1
            if self._consecutive_acc_failures == 1:
                # First failure — try a silent reconnect + one immediate retry
                # before showing "Connection Lost" in the panel.  A single
                # transient network blip or broker timeout would otherwise
                # generate a spurious alert every time.
                log.warning(
                    "Account data unavailable (attempt 1) — "
                    "reconnecting and retrying before declaring DISCONNECTED …"
                )
                await ensure_connected()
                acc_info = await get_account_info()
            if not acc_info or "balance" not in acc_info or "equity" not in acc_info:
                self._set_trade_permission(
                    False,
                    "ACCOUNT_DATA_UNAVAILABLE",
                    ["Live account balance/equity unavailable"],
                )
                log.error(
                    f"Account data unavailable after {self._consecutive_acc_failures} "
                    "attempt(s) — declaring DISCONNECTED"
                )
                self._write_state(
                    "DISCONNECTED",
                    acc_info=acc_info,
                    extra={"error": "Live account data unavailable after retry"},
                )
                return
            # Retry succeeded
            log.info("✅ Account data recovered after retry — continuing bar")
            self._consecutive_acc_failures = 0

        else:
            self._consecutive_acc_failures = 0   # reset on clean success

        self._last_acc_info = acc_info  # update cache so WAITING writes show real balance
        balance  = float(acc_info["balance"])
        equity   = float(acc_info["equity"])

        # 3. ── RISK GUARDIAN CHECK ────────────────────────────────────────────
        #    Must run BEFORE any position check or order placement.
        #
        # Lazy initialization: if get_account_info() failed at start() time
        # (e.g. broker was slow to respond) the Guardian was left uninitialized
        # and would block every bar forever — even after /reset_guardian, because
        # reset_halt() clears _halted but not the _initialized=False flag, so
        # check() would return halted=True again on the next poll.
        # Now that we have fresh account data, initialize the Guardian on the
        # first bar where it is still uninitialized.
        if not self.guardian.is_initialized:
            log.info(
                "Guardian was not initialized at startup — performing lazy "
                f"initialization now (balance={balance:.2f}  equity={equity:.2f})"
            )
            self.guardian.initialize(balance, equity)

        gs = self.guardian.check(balance, equity)
        self._last_guardian_status = gs

        if gs.halted:
            self._set_trade_permission(
                False,
                "GUARDIAN_HALTED",
                [gs.reason or "RiskGuardian halted trading"],
            )
            log.warning(
                f"🛡️  GUARDIAN HALT — no trade this bar.  "
                f"Reason: {gs.reason}  "
                f"Daily PnL: {gs.daily_pnl:+.2f} ({gs.daily_pnl_pct:+.3f}%)  "
                f"Drawdown: {gs.drawdown_pct:.3f}%"
            )
            # Auto-pause the robot so Telegram panel shows PAUSED (not RUNNING)
            if not self.paused:
                self.paused = True
                log.critical(
                    "🛡️  Robot AUTO-PAUSED by RiskGuardian.  "
                    "Use /reset_guardian in Telegram to resume."
                )
            self._write_state(
                "PAUSED", acc_info,
                extra=self._guardian_extra(gs, "GUARDIAN_HALT"),
            )
            return

        # 4. Check open positions (live MT5 — prevents duplicate entry on restart)
        # get_open_positions() raises RuntimeError if mt5rest returns an error
        # response.  Treat that as a missing position check — skip trade entry
        # for this bar rather than risking duplicate-entry or crashing the loop.
        try:
            raw_positions, _dropped_unknown = await get_open_positions(
                SYMBOL, self._known_open_tickets(), return_diagnostics=True
            )
        except RuntimeError as _pos_err:
            self._set_trade_permission(
                False,
                "POSITION_CHECK_FAILED",
                [f"Could not verify open positions: {_pos_err}"],
            )
            log.error(
                f"Cannot verify open positions — skipping trade entry this bar: {_pos_err}"
            )
            self._write_state("WAITING", acc_info)
            return
        pos_dicts = [mt5_pos_to_dict(p) for p in raw_positions]
        self._last_open_positions = list(pos_dicts)
        pos       = pos_dicts[0] if pos_dicts else None

        # DEFENSE IN DEPTH: an unrecognised corrupted row was dropped this
        # poll. Most of the time that really is bridge garbage, but right
        # after a restart (known_positions not yet repopulated — see
        # _known_open_tickets) it can be a real position we simply don't
        # recognise yet, and treating "recognised 0 positions" as "flat" is
        # exactly the bug that let 5 duplicate entries stack in this
        # incident. Skip entry for this bar rather than risk stacking on top
        # of something we can't yet identify; positions we DO recognise are
        # unaffected and continue to be managed normally.
        if _dropped_unknown and pos is None:
            self._set_trade_permission(
                False,
                "UNKNOWN_POSITION_DATA",
                ["Unidentified live position data was reported"],
            )
            log.warning(
                f"Skipping trade entry — mt5rest reported unidentified ticket(s) "
                f"{_dropped_unknown} this poll that don't match any known "
                f"position; treating as possibly-open rather than assuming flat."
            )
            self._write_state("WAITING", acc_info)
            return

        if pos:
            log.info(f"Open position: id={pos['id']}  "
                     f"dir={pos['type']}  profit={pos.get('profit', 0):.2f}")

        # 5. Run decision engine (synchronous — all heavy math)
        decision = run_decision_engine(
            candles,
            balance,
            risk_percent=RISK_PERCENT,
            min_confirmations=MIN_CONFIRMATIONS,
            trend_min_confirmations=TREND_MIN_CONFIRMATIONS,
            use_atr_high_vol=USE_ATR_HIGH_VOL_FILTER,
            require_price_action=REQUIRE_PRICE_ACTION,
            require_smc_price_action_wyckoff=REQUIRE_SMC_PRICE_ACTION_WYCKOFF,
            range_trading_enabled=RANGE_TRADING_ENABLED,
            range_min_confirmations=RANGE_MIN_CONFIRMATIONS,
            range_min_rr=RANGE_MIN_RR,
            range_edge_atr_distance=RANGE_EDGE_ATR_DISTANCE,
            range_risk_percent=RANGE_RISK_PERCENT,
            range_entry_filters_enabled=RANGE_ENTRY_FILTERS_ENABLED,
            timeframe=tf,
            price_action_standalone=PRICE_ACTION_STANDALONE,
            range_weak_min_confirmations=RANGE_WEAK_MIN_CONFIRMATIONS,
            regime_strength=htf_bias.strength if htf_bias is not None else None,
            regime_context=htf_bias.regime if htf_bias is not None else None,
            sl_atr=sl_atr,
            sl_atr_multiplier=SL_ATR_BASE_MULTIPLIER,
            low_volatility_sl_atr_add=LOW_VOLATILITY_SL_ATR_ADD,
        )
        self.last_decision = decision

        # 6. Write MT5 snapshot for Telegram panel
        last_c = candles[-1]
        _strategy_telemetry = describe_strategy(decision)
        _policy_strength = str(
            decision.regime_strength
            or (htf_bias.strength if htf_bias is not None else "")
        ).upper()
        _policy_regime = str(
            decision.policy_regime
            or decision.regime
            or (htf_bias.regime if htf_bias is not None else "")
        ).upper()
        _policy_min_confirmations = decision.effective_min_confirmations
        if _policy_min_confirmations is None:
            if _policy_regime == "RANGE":
                _policy_min_confirmations = (
                    max(RANGE_MIN_CONFIRMATIONS, RANGE_WEAK_MIN_CONFIRMATIONS)
                    if _policy_strength == "WEAK"
                    else RANGE_MIN_CONFIRMATIONS
                )
            else:
                _policy_min_confirmations = MIN_CONFIRMATIONS
        _strategy_telemetry.update({
            "candle_time": last_c.time,
            "timeframe": tf,
            "entry_policy": {
                "min_confirmations": _policy_min_confirmations,
                "base_min_confirmations": MIN_CONFIRMATIONS,
                "range_min_confirmations": RANGE_MIN_CONFIRMATIONS,
                "range_weak_min_confirmations": RANGE_WEAK_MIN_CONFIRMATIONS,
                "regime": _policy_regime,
                "strength": _policy_strength or "UNKNOWN",
                "trend_min_confirmations": TREND_MIN_CONFIRMATIONS,
                "price_action_standalone": bool(PRICE_ACTION_STANDALONE),
                "pa_standalone_min_score": PA_STANDALONE_MIN_SCORE,
                "require_price_action": bool(REQUIRE_PRICE_ACTION),
                "confidence_hard_min": CONF_HARD_MIN,
                "risk_percent": RISK_PERCENT,
            },
            "protective_sl": {
                "atr_timeframe": SL_ATR_TIMEFRAME,
                "atr_period": SL_ATR_PERIOD,
                "atr": sl_atr,
                "base_multiplier": SL_ATR_BASE_MULTIPLIER,
                "low_volatility_add": LOW_VOLATILITY_SL_ATR_ADD,
                "effective_multiplier": min(
                    3.5,
                    SL_ATR_BASE_MULTIPLIER
                    + (
                        LOW_VOLATILITY_SL_ATR_ADD
                        if decision.regime == "LOW_VOLATILITY"
                        else 0.0
                    ),
                ),
                "cache_reason": sl_atr_reason or "fresh",
            },
            "mtf": {
                "enabled": bool(MTF_ENABLED),
                "timeframe": MTF_TIMEFRAME,
                "dry_run": bool(MTF_DRY_RUN),
                "opposition_threshold": MTF_OPPOSITION_THRESHOLD,
                "direction": htf_bias.direction if htf_bias else "NEUTRAL",
                "trend": htf_bias.trend if htf_bias else "NEUTRAL",
                "smc": htf_bias.smc_signal if htf_bias else "NEUTRAL",
                "regime": htf_bias.regime if htf_bias else "RANGE",
                "strength": htf_bias.strength if htf_bias else "WEAK",
                "trend_score": htf_bias.trend_score if htf_bias else 0.0,
                "data_available": htf_bias is not None,
                "data_reason": htf_data_reason or "",
                "gate": "PENDING",
            },
        })
        self._last_candle_telemetry = _strategy_telemetry
        log.info(
            "CANDLE_TELEMETRY %s",
            json.dumps(_strategy_telemetry, sort_keys=True, default=str),
        )
        # Compatibility telemetry for the panel; the adaptive trailing engine
        # reads the per-timeframe cache above.
        self._last_atr = self._last_atr_by_timeframe.get(tf, 0.0)
        # Compute ATR in price units using a 5-bar average True Range.
        # A single-candle TR makes the displayed ATR jump on every wick,
        # misleading the panel operator.  Keep this snapshot value separate
        # from the per-timeframe ATR cache used by adaptive trailing.
        _snap_trs = [
            max(
                candles[i].high - candles[i].low,
                abs(candles[i].high - candles[i - 1].close),
                abs(candles[i].low - candles[i - 1].close),
            )
            for i in range(1, len(candles))
        ]
        _snap_win = min(5, len(_snap_trs))
        _snap_atr = (
            round(sum(_snap_trs[-_snap_win:]) / _snap_win, 4)
            if _snap_trs
            else 0.0
        )
        # Build normalized account_info for the snapshot (snake_case keys to
        # match what telegram_panel's mt5_service expects).
        _snap_account_info = {
            "balance":          float(acc_info.get("balance",  0.0)),
            "equity":           float(acc_info.get("equity",   0.0)),
            "margin":           float(acc_info.get("margin",   0.0)),
            "free_margin":      float(acc_info.get("freeMargin",
                                      acc_info.get("free_margin", 0.0))),
            "floating_profit":  float(acc_info.get("equity", 0.0))
                                - float(acc_info.get("balance", 0.0)),
            "currency":         acc_info.get("currency", "USD"),
            "leverage":         acc_info.get("leverage", 0),
            "broker":           acc_info.get("broker", ""),
            "server":           acc_info.get("server", ""),
            "login":            acc_info.get("login",  ""),
            "connection_status": "connected",
        }
        # today_profit: sum of realised profits from trades closed today.
        _snap_today = __import__("datetime").date.today().isoformat()
        _snap_today_profit = round(sum(
            float(t.get("profit", 0.0))
            for t in self.trade_history
            if isinstance(t, dict) and t.get("profit") is not None
            and str(t.get("logged_at") or t.get("bar_time") or "").startswith(
                _snap_today
            )
        ), 2)
        write_mt5_snapshot(
            candle_time=last_c.time,
            price=last_c.close,
            regime=decision.regime,
            adx=decision.quality_filter.adx,
            atr=_snap_atr,
            smc_signal=decision.smc.smc_signal,
            trend=decision.trend.trend,
            # FIX: Full account data so the panel shows real balance, not USD 0.00
            account_info=_snap_account_info,
            open_positions=pos_dicts,
            recent_trades=self.trade_history[-20:],
            today_profit=_snap_today_profit,
            floating_profit=_snap_account_info["floating_profit"],
            drawdown={
                "current_percent": float(gs.drawdown_pct),
                "max_percent":     float(gs.max_drawdown_pct),
            },
        )

        # 7. Gate: aggregate strategy-slot capacity
        if len(raw_positions) >= MAX_OPEN_TRADES:
            self._set_trade_permission(
                False,
                "MAX_OPEN_TRADES",
                [f"Maximum open positions reached ({MAX_OPEN_TRADES})"],
            )
            log.info(f"Max positions ({MAX_OPEN_TRADES}) open — skipping entry")
            self._write_state(
                "HOLDING", acc_info, decision, pos,
                extra=self._guardian_extra(gs),
            )
            return

        # 7b. Gate: within-tick duplicate-entry guard
        # When TRADE_TIMEFRAMES has N entries and several TFs close at the
        # same bar boundary (e.g. M20+M15+M10+M5 all fire at minute :60),
        # _on_new_bar is called N times inside the same _run_loop iteration.
        # The mt5rest bridge may not yet reflect the position opened by the
        # first call when the second call runs its get_open_positions() check
        # above — so the max-positions gate can pass N times in a row and N
        # trades get placed.  This flag is reset once per tick (before the
        # for-loop in _run_loop) and set to True by the first successful
        # placement, blocking all subsequent calls in the same tick.
        if self._trade_opened_this_tick:
            self._set_trade_permission(
                False,
                "WITHIN_TICK_DUPLICATE_GUARD",
                ["A trade was already opened during this tick"],
            )
            log.info(
                f"[{tf}] Skipping entry — a trade was already opened "
                f"earlier this tick (multi-TF boundary guard)"
            )
            self._write_state(
                "HOLDING", acc_info, decision, pos,
                extra=self._guardian_extra(gs),
            )
            return

        # 8. Gate: decision engine
        if not decision.allowed:
            reasons = " | ".join(decision.blocked_reasons or ["No signal"])
            self._set_trade_permission(
                False,
                "SIGNAL_BLOCKED",
                decision.blocked_reasons or ["No signal"],
            )
            log.info(f"No trade → {reasons}")
            self._write_state(
                "SCANNING", acc_info, decision, pos,
                extra=self._guardian_extra(gs),
            )
            return

        # Final defense-in-depth check for RANGE.  The structural RANGE
        # filters may be disabled for telemetry-only operation.  An explicit
        # standalone Price Action policy may lower only the confirmation floor
        # to one aligned PA vote; all other entry gates remain mandatory.
        _range_confirmation_floor = (
            1
            if (
                PRICE_ACTION_STANDALONE
                and decision.entry_filter is not None
                and decision.entry_filter.price_action
            )
            else RANGE_MIN_CONFIRMATIONS
        )
        if (
            decision.regime == "RANGE"
            and decision.entry_filter is not None
            and decision.entry_filter.confirmation_count < _range_confirmation_floor
        ):
            _range_confirmation_reason = (
                f"RANGE entry blocked: "
                f"{decision.entry_filter.confirmation_count}/"
                f"{_range_confirmation_floor} confirmations"
            )
            self._set_trade_permission(
                False,
                "RANGE_CONFIRMATIONS_BLOCKED",
                [_range_confirmation_reason],
            )
            log.error(
                "Order blocked by final RANGE confirmation safety check: "
                f"{_range_confirmation_reason}"
            )
            self._write_state(
                "SCANNING", acc_info, decision, pos,
                extra=self._guardian_extra(gs),
            )
            return

        candidate_strategy_slots = strategy_slots_for_decision(decision)
        slots_available, slot_reason = available_for_strategy_slots(
            pos_dicts,
            candidate_strategy_slots,
            max_open_positions=MAX_OPEN_TRADES,
        )
        if not slots_available:
            self._set_trade_permission(
                False,
                "STRATEGY_POSITION_LIMIT",
                [slot_reason],
            )
            log.info(
                f"Strategy position limit — {slot_reason}; "
                f"candidate={candidate_strategy_slots}"
            )
            self._write_state(
                "HOLDING" if pos_dicts else "SCANNING",
                acc_info,
                decision,
                pos,
                extra=self._guardian_extra(gs),
            )
            return

        # RANGE trades are intentionally scarce.  Count successful RANGE
        # entries for the current UTC session, including entries restored from
        # the persisted trade history, so a Render restart cannot reset the
        # limit and over-trade a choppy market.
        if decision.regime == "RANGE":
            session_date = bar_time.date()
            range_entries = 0
            for trade in self.trade_history:
                if not isinstance(trade, dict) or trade.get("regime") != "RANGE":
                    continue
                raw_time = trade.get("bar_time") or trade.get("logged_at")
                try:
                    trade_date = datetime.fromisoformat(
                        str(raw_time).replace("Z", "+00:00")
                    ).date()
                except (TypeError, ValueError):
                    continue
                if trade_date == session_date:
                    range_entries += 1
            if range_entries >= MAX_RANGE_TRADES_PER_SESSION:
                self._set_trade_permission(
                    False,
                    "MAX_RANGE_TRADES_PER_SESSION",
                    [
                        f"RANGE session limit reached "
                        f"({MAX_RANGE_TRADES_PER_SESSION})"
                    ],
                )
                log.info(
                    f"RANGE session limit reached "
                    f"({MAX_RANGE_TRADES_PER_SESSION}) — skipping entry"
                )
                self._write_state(
                    "SCANNING", acc_info, decision, pos,
                    extra=self._guardian_extra(gs),
                )
                return

        # 8b. Gate: negative-only HTF opposition check.
        # MTF must not become a second positive-confirmation strategy.  The
        # existing decision, confidence, quality, and risk gates remain the
        # source of approval; MTF can only reject clear strong opposition.
        if MTF_ENABLED:
            _mtf_check = evaluate_mtf_opposition(
                htf_bias,
                decision.direction,
                opposition_threshold=MTF_OPPOSITION_THRESHOLD,
            )
            _would_block = _mtf_check.would_block
            _is_hard_block = _would_block and not MTF_DRY_RUN
            self._last_candle_telemetry.setdefault("mtf", {}).update({
                "htf_trend": _mtf_check.htf_trend,
                "candidate": _mtf_check.candidate,
                "opposition_strength": _mtf_check.opposition_strength,
                "would_block": _would_block,
                "gate": (
                    "BLOCKED"
                    if _is_hard_block
                    else "DRY_RUN_WOULD_BLOCK"
                    if _would_block
                    else "ALLOWED"
                ),
                "gate_reason": _mtf_check.reason,
            })
            log.info(
                "MTF check: candidate=%s htf_trend=%s "
                "opposition_strength=%.1f threshold=%.1f would_block=%s%s",
                _mtf_check.candidate,
                _mtf_check.htf_trend,
                _mtf_check.opposition_strength,
                _mtf_check.threshold,
                str(_would_block).lower(),
                " (dry_run)" if MTF_DRY_RUN else "",
            )
            if _is_hard_block:
                _mtf_reason = f"MTF BLOCK: {_mtf_check.reason}"
                self._set_trade_permission(False, "MTF_HARD_BLOCKED", [_mtf_reason])
                log.info(f"⛔  {_mtf_reason}")
                _mtf_extra = {
                    **self._guardian_extra(gs),
                    "htf_bias": {
                        "direction": htf_bias.direction if htf_bias else "NEUTRAL",
                        "trend":     htf_bias.trend if htf_bias else "NEUTRAL",
                        "smc":       htf_bias.smc_signal if htf_bias else "NEUTRAL",
                        "regime":    htf_bias.regime if htf_bias else "RANGE",
                        "strength":  htf_bias.strength if htf_bias else "WEAK",
                        "trend_score": htf_bias.trend_score if htf_bias else 0.0,
                        "reasoning": htf_bias.reasoning if htf_bias else [
                             htf_data_reason or "HTF bias unavailable — no opposition measured"
                        ],
                        "blocked":   _mtf_reason,
                        "opposition_strength": _mtf_check.opposition_strength,
                        "threshold": _mtf_check.threshold,
                    },
                }
                self._write_state("SCANNING", acc_info, decision, pos, extra=_mtf_extra)
                return
        else:
            self._last_candle_telemetry.setdefault("mtf", {})["gate"] = "DISABLED"

        # 7c. Gate: post-SL cooldown in choppy/range regimes
        # If the last trade was in the same direction and closed (or will close)
        # within 2 bars, the market setup has NOT changed — skip re-entry.
        # Uses only the existing _last_entry state; fails-open on any parse error.
        _RANGE_COOLDOWN_REGIMES = {"RANGE", "ACCUMULATION", "DISTRIBUTION", "HIGH_VOLATILITY"}
        if (self._last_entry_bar_time is not None
                and self._last_entry_direction == decision.direction
                and decision.regime in _RANGE_COOLDOWN_REGIMES):
            _TF_MIN_MAP = {
                "M1": 1, "1m": 1, "M5": 5, "5m": 5, "M10": 10, "10m": 10,
                "M15": 15, "15m": 15, "M20": 20, "M30": 30, "30m": 30,
                "H1": 60, "1h": 60, "H4": 240,
            }
            _tf_min = _TF_MIN_MAP.get(tf, 15)
            _elapsed_min = (bar_time - self._last_entry_bar_time).total_seconds() / 60.0
            if _elapsed_min < 2 * _tf_min:
                self._set_trade_permission(
                    False,
                    "RANGE_COOLDOWN",
                    [
                        f"Same-direction range cooldown active "
                        f"({2 * _tf_min:.0f} minutes)"
                    ],
                )
                log.info(
                    f"⏸ Post-SL cooldown [{tf}]: {decision.direction} last entered "
                    f"{_elapsed_min:.0f}min ago in {decision.regime} regime — "
                    f"cooldown {2*_tf_min}min, skipping bar"
                )
                self._write_state("WAITING", acc_info, decision, pos,
                                  extra=self._guardian_extra(gs))
                return

        # 8c. Safety re-check: confirm strategy-slot capacity immediately
        # before sending the order.
        #
        # ROOT CAUSE: mt5rest has occasionally corrupted a genuinely open
        # position's row into a "lone row with an insane volume" (see
        # _dedupe_positions in mt5/connector.py) — a bridge-side glitch, not a
        # real second position. _dedupe_positions correctly discards that
        # garbage row as unreliable, but the side effect is that
        # get_open_positions() briefly reports the account as flat (raw
        # positions = []) even though a real position is still open in MT5.
        # If that happens to coincide with step 4's position check above, the
        # strategy-slot gate (step 7) sees too few occupied slots and could
        # allow an unintended duplicate.
        #
        # Re-poll and place under one lock. This closes the race where two
        # timeframe evaluations both see a flat account and then submit
        # opposite orders before the bridge exposes the first fill.
        tp_params = decision.trade_params
        result, _confirm_pos_dicts, _entry_stage, _entry_reason = (
            await self._safe_entry_order(
                decision,
                candidate_strategy_slots,
                tf,
                bar_time,
            )
        )
        if result is None:
            self._set_trade_permission(False, _entry_stage, [_entry_reason])
            _confirm_pos = _confirm_pos_dicts[0] if _confirm_pos_dicts else pos
            if _entry_stage == "POSITION_CHECK_FAILED":
                state_status = "WAITING"
                log.error(f"Pre-order safety check blocked entry: {_entry_reason}")
            else:
                state_status = "HOLDING"
                log.warning(f"Pre-order entry guard blocked entry: {_entry_reason}")
            self._write_state(
                state_status,
                acc_info,
                decision,
                _confirm_pos,
                extra=self._guardian_extra(gs),
            )
            return

        if result.success:
            if (
                decision.regime == "RANGE"
                and not RANGE_REQUIRE_EDGE_POSITION
                and decision.entry_filter is not None
            ):
                log.info(
                    "RANGE mode trade allowed "
                    f"(relaxed: confirmations={decision.entry_filter.confirmation_count}, "
                    "edge_check=disabled)"
                )
            self._set_trade_permission(
                True,
                "ORDER_PLACED",
                ["Order accepted by MetaAPI"],
            )
            # Block all further _on_new_bar calls in this tick from opening
            # another position (covers the multi-TF same-bar-boundary race).
            self._trade_opened_this_tick = True
            self._last_entry_bar_time   = bar_time
            self._last_entry_direction  = decision.direction
            strategy = describe_strategy(decision)
            entry_log = {
                "position_id": result.position_id,
                "direction":   decision.direction,
                "entry":       tp_params.entry_price,
                "sl":          tp_params.stop_loss,
                "tp":          tp_params.take_profit,
                "lot":         tp_params.lot_size,
                "rr":          tp_params.risk_reward_ratio,
                "confidence":  decision.confidence,
                "grade":       decision.grade,
                "regime":      decision.regime,
                 "timeframe":   tf,
                "bar_time":    bar_time.isoformat(),
                "strategy":    strategy,
                "strategy_slots": list(candidate_strategy_slots),
            }
            log_trade(self.trade_history, entry_log)
            # Publish the "why" behind this trade, keyed by ticket, so the
            # Telegram panel's TRADE OPENED notification can explain the
            # strategy instead of showing only price/volume/SL/TP. Best
            # effort — never let a Redis hiccup affect trading itself.
            try:
                from live_trading.redis_ipc import redis_set_trade_strategy
                redis_set_trade_strategy(result.position_id, strategy)
            except Exception as exc:
                log.debug(f"Could not publish trade strategy for panel: {exc}")
            # Anchor the staircase trailing-stop baseline to this trade's
            # ORIGINAL entry price and ORIGINAL risk distance. This is set
            # exactly once, at open, and never touched again — the staircase
            # always measures its R-multiples from here, never from wherever
            # the stop has since been trailed to.
            self._trail_baselines[str(result.position_id)] = {
                "id":            result.position_id,
                "direction":     decision.direction,
                "entry":         tp_params.entry_price,
                "risk_distance": abs(tp_params.entry_price - tp_params.stop_loss),
                "initial_sl":    tp_params.stop_loss,
                "timeframe":     tf,
                "highest_price_since_entry": tp_params.entry_price,
                "lowest_price_since_entry": tp_params.entry_price,
                "breakeven_armed": False,
            }
            # Build a synthetic position so the Telegram panel reflects the
            # newly opened trade immediately rather than waiting up to 5 min
            # for the next bar to re-fetch live positions.
            # Keep the same strategy-tagged comment that was sent to MTAPI.
            # This telemetry path runs after the order succeeds; rebuilding it
            # here avoids carrying a local variable across _safe_entry_order().
            order_comment = strategy_order_comment(COMMENT, candidate_strategy_slots)
            pos = {
                "id":         result.position_id,
                "ticket":     result.position_id,
                "symbol":     SYMBOL,
                "type":       decision.direction,
                "volume":     tp_params.lot_size,
                "open_price": tp_params.entry_price,
                "sl":         tp_params.stop_loss,
                "tp":         tp_params.take_profit,
                "profit":     0.0,
                "comment":    order_comment,
            }
            # ROOT-CAUSE FIX: push the newly opened position into the live
            # Redis snapshot immediately. write_mt5_snapshot() above (step 6)
            # already ran BEFORE this order was placed, so without this the
            # panel's "open positions" view (which reads goldscalper:snapshot,
            # not goldscalper:state) would not show this trade until the next
            # M5 bar — up to 5 minutes later.
            try:
                from live_trading.redis_ipc import redis_update_snapshot_positions
                redis_update_snapshot_positions(pos_dicts + [pos])
            except Exception as _sync_exc:
                log.debug(f"Snapshot position sync skipped: {_sync_exc}")
        else:
            if result.message == "MARKET_CLOSED":
                self._set_trade_permission(
                    False,
                    "MARKET_CLOSED",
                    ["Broker session is closed; retrying on the next scan"],
                )
                log.info("⏸️ Market is closed; no order was opened. A fresh signal will be retried on the next scan.")
            else:
                self._set_trade_permission(
                    False,
                    "ORDER_FAILED",
                    [result.message or "Order was rejected"],
                )
                log.error(f"❌ Trade failed: {result.message}")

        self._write_state(
            "RUNNING", acc_info, decision, pos,
            extra=self._guardian_extra(gs),
        )

    # ── Staircase trailing stop ───────────────────────────────────────────────

    def _restore_trail_baseline(self, pos: dict) -> Optional[dict]:
        """Recover the trailing baseline for an already-open position.

        Runs when the engine (re)starts with a position already open (e.g.
        after a Render restart) and self._trail_baseline is empty. Finds the
        entry log written when this exact position was opened — that record
        was written once, before any SL modification, so it still holds the
        trade's true original risk distance.

        Falls back to the position's *current* entry/SL if no matching log
        entry survives (e.g. very old trade, log rotated away). This is an
        approximation only: if the stop had already been trailed before the
        log was lost, the recovered "risk distance" will be smaller than the
        true original — the staircase would then activate a little earlier
        than intended, but it can never move the stop backwards, so this is
        safe, just slightly more conservative.
        """
        pos_id = pos.get("id")
        for entry in reversed(self.trade_history):
            if not isinstance(entry, dict):
                continue
            if str(entry.get("position_id")) == str(pos_id) and "sl" in entry and "entry" in entry:
                risk = abs(float(entry["entry"]) - float(entry["sl"]))
                if risk > 0:
                    return {
                        "id":            pos_id,
                        "direction":     entry.get("direction", pos.get("type", "BUY")),
                        "entry":         float(entry["entry"]),
                        "risk_distance": risk,
                        "initial_sl":    float(entry["sl"]),
                    "timeframe":     entry.get("timeframe") or TIMEFRAME,
                    "highest_price_since_entry": float(entry["entry"]),
                    "lowest_price_since_entry": float(entry["entry"]),
                    "breakeven_armed": False,
                    }
                break
        # Fallback: derive from the position's live snapshot.
        risk = abs(float(pos.get("open_price", 0.0)) - float(pos.get("sl", 0.0)))
        if risk > 0:
            log.warning(
                f"Trailing baseline for position {pos_id} could not be found in "
                f"trade history — approximating from live position data."
            )
            return {
                "id":            pos_id,
                "direction":     pos.get("type", "BUY"),
                "entry":         float(pos.get("open_price", 0.0)),
                "risk_distance": risk,
                "initial_sl":    float(pos.get("sl", 0.0)),
                "timeframe":     pos.get("timeframe") or TIMEFRAME,
                "highest_price_since_entry": float(pos.get("open_price", 0.0)),
                "lowest_price_since_entry": float(pos.get("open_price", 0.0)),
                "breakeven_armed": False,
            }
        return None

    async def _sync_trailing_positions(self, reason: str) -> None:
        """Explicitly resync all open tickets after startup or reconnect."""
        self._trailing_sync_reason = reason
        log.info("Trailing sync: reason=%s", reason)
        await self._manage_trailing_stop()

    async def _manage_trailing_stop(self) -> None:
        """Manage every open ticket independently on every loop tick.

        Position modifications are gathered concurrently with
        ``return_exceptions=True``.  A broker error for one ticket therefore
        cannot prevent the other open positions from receiving their update.
        """
        if not self.trailing_enabled:
            return

        try:
            raw_positions = await get_open_positions(
                SYMBOL, self._known_open_tickets()
            )
        except RuntimeError as exc:
            log.debug(f"Trailing check skipped — could not fetch positions: {exc}")
            return

        if not raw_positions:
            self._trail_baselines = {}
            self._last_trailing_statuses = {}
            return

        live_ids = {str(mt5_pos_to_dict(raw)["id"]) for raw in raw_positions}
        self._trail_baselines = {
            pid: baseline
            for pid, baseline in self._trail_baselines.items()
            if pid in live_ids
        }
        self._last_trailing_statuses = {
            pid: status
            for pid, status in self._last_trailing_statuses.items()
            if pid in live_ids
        }

        quote = await get_current_quote(SYMBOL)
        if not quote:
            return

        async def update_ticket(raw_position: dict) -> None:
            pos = mt5_pos_to_dict(raw_position)
            pos_id = str(pos["id"])
            baseline = self._trail_baselines.get(pos_id)
            if not baseline:
                baseline = self._restore_trail_baseline(pos)
                if baseline:
                    self._trail_baselines[pos_id] = baseline
            if not baseline:
                self._last_trailing_statuses[pos_id] = {
                    "active": False,
                    "action": "SKIP",
                    "reason": "missing baseline",
                }
                log.warning(
                    "Trailing update: ticket=%s action=SKIP reason=missing baseline",
                    pos_id,
                )
                return

            direction = str(baseline["direction"]).upper()
            current_price = quote["bid"] if direction == "BUY" else quote["ask"]
            timeframe = str(baseline.get("timeframe") or TIMEFRAME)
            candles = self._last_candles_by_timeframe.get(timeframe, [])

            # Keep the best favourable quote observed for this ticket.  These
            # extremes, unlike the current quote, are the anchor for the
            # Chandelier calculation and are persisted with the baseline.
            try:
                current_price = float(current_price)
                entry_price = float(baseline["entry"])
                if direction == "BUY":
                    baseline["highest_price_since_entry"] = max(
                        float(baseline.get("highest_price_since_entry", entry_price)),
                        entry_price,
                        current_price,
                    )
                else:
                    baseline["lowest_price_since_entry"] = min(
                        float(baseline.get("lowest_price_since_entry", entry_price)),
                        entry_price,
                        current_price,
                    )
            except (TypeError, ValueError):
                self._last_trailing_statuses[pos_id] = {
                    "active": False,
                    "action": "SKIP",
                    "reason": "invalid quote or entry price",
                }
                return

            decision = compute_adaptive_trail(
                direction=direction,
                current_price=current_price,
                candles=candles,
                cfg=self._trailing_cfg,
                entry_price=baseline["entry"],
                initial_sl=baseline.get("initial_sl"),
                risk_distance=baseline.get("risk_distance"),
                current_sl=pos.get("sl"),
                highest_price_since_entry=baseline.get(
                    "highest_price_since_entry"
                ),
                lowest_price_since_entry=baseline.get(
                    "lowest_price_since_entry"
                ),
                breakeven_armed=baseline.get("breakeven_armed", False),
            )
            baseline.update(
                {
                    "highest_price_since_entry": (
                        decision.highest_price_since_entry
                    ),
                    "lowest_price_since_entry": (
                        decision.lowest_price_since_entry
                    ),
                    "breakeven_armed": decision.breakeven_armed,
                }
            )
            candidate_sl = decision.candidate_sl
            previous_status = self._last_trailing_statuses.get(pos_id, {})
            apply_step = (
                0.0
                if decision.stage == "BREAKEVEN"
                else self._trailing_cfg.min_step_price
            )
            applicable = should_apply(
                direction,
                pos["sl"],
                candidate_sl,
                apply_step,
            )
            action = "MODIFY" if applicable else "HOLD"
            if candidate_sl is None:
                reason_text = (
                    "waiting for 1R (current %.3fR, risk_distance=%.5f)"
                    % (
                        decision.floating_profit_r_multiple,
                        decision.risk_distance,
                    )
                )
                if decision.stage == "CHANDELIER":
                    reason_text = "insufficient ATR/candle data for chandelier"
            else:
                reason_text = (
                    "move SL to exact breakeven at 1R"
                    if decision.stage == "BREAKEVEN"
                    else (
                        "chandelier from favourable extreme "
                        f"(multiplier={self._trailing_cfg.chandelier_atr_multiplier:.2f})"
                    )
                )
            self._last_trailing_statuses[pos_id] = {
                "active": candidate_sl is not None,
                "mode": decision.mode,
                "stage": decision.stage,
                "timeframe": timeframe,
                "atr": decision.atr,
                "multiplier": decision.multiplier,
                "trail_distance": decision.distance,
                "exhaustion_confirmations": list(decision.exhaustion.active),
                "floating_profit": pos.get("profit", 0.0),
                "floating_profit_price": decision.floating_profit_price,
                "floating_profit_atr": decision.floating_profit_atr_multiple,
                "floating_profit_r": decision.floating_profit_r_multiple,
                "risk_distance": decision.risk_distance,
                "highest_price_since_entry": decision.highest_price_since_entry,
                "lowest_price_since_entry": decision.lowest_price_since_entry,
                "breakeven_armed": decision.breakeven_armed,
                "current_sl": pos["sl"],
                "candidate_sl": candidate_sl,
                "action": action,
                "reason": reason_text,
                "sync_reason": self._trailing_sync_reason,
            }
            if (
                previous_status.get("stage") != decision.stage
                and decision.stage == "BREAKEVEN"
            ):
                log.info(
                    "Adaptive trailing stage: ticket=%s stage=BREAKEVEN "
                    "timeframe=%s risk_distance=%.5f "
                    "floating_profit=%.2f floating_profit_price=%.5f "
                    "profit_r=%.3f",
                    pos_id,
                    timeframe,
                    decision.risk_distance,
                    pos.get("profit", 0.0),
                    decision.floating_profit_price,
                    decision.floating_profit_r_multiple,
                )
            elif (
                previous_status.get("stage") != decision.stage
                and decision.stage == "CHANDELIER"
            ):
                log.info(
                    "Adaptive trailing stage: ticket=%s stage=CHANDELIER "
                    "timeframe=%s highest=%.5f lowest=%.5f "
                    "atr=%.5f multiplier=%.3f",
                    pos_id,
                    timeframe,
                    decision.highest_price_since_entry or 0.0,
                    decision.lowest_price_since_entry or 0.0,
                    decision.atr,
                    decision.multiplier,
                )
            log.info(
                "Adaptive trailing update: ticket=%s stage=%s mode=%s timeframe=%s "
                "floating_profit=%.2f floating_profit_price=%.5f "
                "profit_r=%.3f profit_atr=%.3f risk_distance=%.5f "
                "atr=%.5f distance=%.5f "
                "extreme_high=%.5f extreme_low=%.5f indicators=%s "
                "reason=%s action=%s",
                pos_id,
                decision.stage,
                decision.mode,
                timeframe,
                pos.get("profit", 0.0),
                decision.floating_profit_price,
                decision.floating_profit_r_multiple,
                decision.floating_profit_atr_multiple,
                decision.risk_distance,
                decision.atr,
                decision.distance,
                decision.highest_price_since_entry or 0.0,
                decision.lowest_price_since_entry or 0.0,
                ",".join(decision.exhaustion.active) or "none",
                reason_text,
                action,
            )
            if not applicable:
                return

            try:
                result = await modify_position(pos["id"], candidate_sl, pos["tp"])
            except Exception as exc:
                self._last_trailing_statuses[pos_id].update(
                    {"action": "ERROR", "reason": f"modify exception: {exc}"}
                )
                log.exception("Trailing update: ticket=%s action=ERROR", pos_id)
                return
            if result.success:
                self._last_trailing_statuses[pos_id].update(
                    {"action": "MODIFIED", "reason": "adaptive stop advanced"}
                )
                log_trade(self.trade_history, {
                    "position_id": pos["id"],
                    "action": "TRAIL_SL",
                    "direction": direction,
                    "stage": decision.stage,
                    "mode": decision.mode,
                    "timeframe": timeframe,
                    "atr": decision.atr,
                    "multiplier": decision.multiplier,
                    "trail_distance": decision.distance,
                    "floating_profit_price": decision.floating_profit_price,
                    "floating_profit_r": decision.floating_profit_r_multiple,
                    "risk_distance": decision.risk_distance,
                    "highest_price_since_entry": decision.highest_price_since_entry,
                    "lowest_price_since_entry": decision.lowest_price_since_entry,
                    "exhaustion_confirmations": list(decision.exhaustion.active),
                    "old_sl": pos["sl"],
                    "new_sl": candidate_sl,
                })
                log.info(
                    "Adaptive trailing update: ticket=%s mode=%s "
                    "reason=stop advanced action=MODIFIED",
                    pos_id,
                    decision.mode,
                )
            else:
                self._last_trailing_statuses[pos_id].update(
                    {"action": "ERROR", "reason": result.message}
                )
                log.warning(
                    "Adaptive trailing update: ticket=%s mode=%s "
                    "reason=modify failed: %s action=ERROR",
                    pos_id,
                    decision.mode,
                    result.message,
                )

        results = await asyncio.gather(
            *(update_ticket(raw) for raw in raw_positions),
            return_exceptions=True,
        )
        for raw, result in zip(raw_positions, results):
            if isinstance(result, Exception):
                ticket = mt5_pos_to_dict(raw).get("id")
                log.exception(
                    "Trailing update: ticket=%s action=ERROR reason=worker failure",
                    ticket,
                    exc_info=result,
                )

    async def _reconcile_closed_trades(self) -> None:
        """Persist exact exit details for positions no longer open in MT5."""
        try:
            raw_positions, _ = await get_open_positions(
                SYMBOL, self._known_open_tickets(), return_diagnostics=True
            )
        except RuntimeError as exc:
            log.debug(f"Closed-trade reconciliation skipped: {exc}")
            return

        open_ids = {
            str(mt5_pos_to_dict(position).get("id"))
            for position in raw_positions
        }
        now = asyncio.get_event_loop().time()
        candidates: dict[str, dict] = {}
        for entry in self.trade_history:
            ticket = entry.get("position_id") or entry.get("ticket")
            if ticket is None or entry.get("close_time"):
                continue
            # TRAIL_SL and close-command audit rows are not entry records.
            if entry.get("entry") is None and entry.get("lot") is None:
                continue
            ticket_id = str(ticket)
            if ticket_id not in open_ids:
                candidates.setdefault(ticket_id, entry)

        changed = False
        for ticket_id, entry in candidates.items():
            last_checked = self._close_history_checked_at.get(ticket_id, 0.0)
            if now - last_checked < _CLOSE_HISTORY_RETRY_INTERVAL:
                continue
            self._close_history_checked_at[ticket_id] = now
            try:
                history = await get_closed_position_history(ticket_id)
            except RuntimeError as exc:
                log.warning(
                    f"Could not read MetaAPI close history for {ticket_id}: {exc}"
                )
                continue
            if not history:
                # Missing history is not evidence of a close. Retry later.
                continue
            if self._apply_close_history(entry, history):
                changed = True

        self._open_position_snapshot_initialized = True
        if changed:
            self._write_state("RUNNING", self._last_acc_info)

    @staticmethod
    def _deal_value(deal: dict, *keys: str, default=None):
        for key in keys:
            if key in deal and deal[key] is not None:
                return deal[key]
        return default

    @classmethod
    def _deal_position_id(cls, deal: dict) -> str:
        value = cls._deal_value(
            deal,
            "positionId",
            "positionID",
            "position_id",
            "position",
            default="",
        )
        return str(value) if value not in (None, "") else ""

    @staticmethod
    def _deal_direction(deal: dict) -> str:
        raw = deal.get("type", deal.get("direction", deal.get("orderType", "")))
        if isinstance(raw, (int, float)) and not isinstance(raw, bool):
            return {0: "BUY", 1: "SELL"}.get(int(raw), "UNKNOWN")
        text = str(raw).upper()
        if "BUY" in text or "LONG" in text:
            return "BUY"
        if "SELL" in text or "SHORT" in text:
            return "SELL"
        return "UNKNOWN"

    @staticmethod
    def _deal_entry_type(deal: dict) -> str:
        return str(
            deal.get("entryType", deal.get("entry", deal.get("entry_type", "")))
        ).upper()

    @classmethod
    def _history_records_from_deals(cls, deals: list[dict]) -> list[dict]:
        """Group MetaAPI entry/exit deals into one panel record per position."""
        grouped: dict[str, list[dict]] = {}
        for deal in deals:
            if not isinstance(deal, dict):
                continue
            symbol = str(
                cls._deal_value(deal, "symbol", "instrument", default=SYMBOL)
            ).upper()
            if symbol != SYMBOL.upper():
                continue
            position_id = cls._deal_position_id(deal)
            if position_id:
                grouped.setdefault(position_id, []).append(deal)

        records: list[dict] = []
        for position_id, position_deals in grouped.items():
            entries = [
                deal for deal in position_deals
                if cls._deal_entry_type(deal) in {"DEAL_ENTRY_IN", "IN"}
            ]
            closes = [
                deal for deal in position_deals
                if cls._deal_entry_type(deal) in {
                    "DEAL_ENTRY_OUT", "DEAL_ENTRY_OUT_BY", "OUT", "OUT_BY",
                }
            ]
            # Some broker adapters omit entryType. Keep such a row visible as
            # an open entry instead of silently dropping a real position.
            if not entries and not closes and len(position_deals) == 1:
                entries = position_deals
            entry = entries[0] if entries else position_deals[0]
            close = closes[-1] if closes else None

            def _number(source: dict, *keys: str, default: float = 0.0) -> float:
                raw = cls._deal_value(source, *keys, default=default)
                try:
                    return float(raw)
                except (TypeError, ValueError):
                    return default

            direction = cls._deal_direction(entry)
            if direction == "UNKNOWN" and close:
                direction = cls._deal_direction(close)
            volume = _number(entry, "volume", "lots", default=0.0)
            open_price = _number(entry, "price", "openPrice", "open_price")
            close_price = (
                _number(close, "price", "closePrice", "close_price")
                if close else None
            )
            profit_gross = sum(_number(deal, "profit") for deal in closes)
            commission = sum(_number(deal, "commission") for deal in closes)
            swap = sum(_number(deal, "swap") for deal in closes)
            fee = sum(_number(deal, "fee") for deal in closes)
            open_time = cls._deal_value(
                entry, "time", "brokerTime", "openTime", "open_time"
            )
            close_time = (
                cls._deal_value(
                    close, "time", "brokerTime", "closeTime", "close_time"
                )
                if close else None
            )
            close_comment = (
                cls._deal_value(
                    close, "comment", "brokerComment", "closeComment", default=""
                )
                if close else ""
            )
            record = {
                "position_id": position_id,
                "ticket": position_id,
                "direction": direction,
                "type": direction,
                "volume": volume,
                "lot": volume,
                "entry": open_price,
                "open_price": open_price,
                "close_price": close_price,
                "bar_time": open_time,
                "open_time": open_time,
                "close_time": close_time,
                "profit_gross": profit_gross,
                "commission": commission,
                "swap": swap,
                "fee": fee,
                "profit": profit_gross + commission + swap + fee,
                "close_reason": (
                    classify_close_reason(
                        {
                            "closePrice": close_price,
                            "closeComment": close_comment,
                        },
                        {
                            "direction": direction,
                            "entry": open_price,
                            "sl": 0.0,
                            "tp": 0.0,
                        },
                    )
                    if close else None
                ),
                "close_comment": close_comment,
                "close_source": "MT5_HISTORY_SYNC" if close else None,
                "status": "CLOSED" if close else "OPEN",
                "source": "BROKER_HISTORY",
            }
            records.append(record)
        return records

    async def _sync_broker_trade_history(self, force: bool = False) -> None:
        """Restore and refresh trade history directly from MetaAPI."""
        now_mono = asyncio.get_event_loop().time()
        if (
            not force
            and now_mono - self._last_history_sync_at < HISTORY_SYNC_INTERVAL
        ):
            return
        self._last_history_sync_at = now_mono
        end_time = datetime.now(timezone.utc)
        start_time = end_time - timedelta(days=HISTORY_LOOKBACK_DAYS)
        self._history_sync_status = {
            **self._history_sync_status,
            "status": "SYNCING",
            "lookback_days": HISTORY_LOOKBACK_DAYS,
            "last_sync_at": end_time.isoformat(),
        }
        try:
            deals = await get_deals_by_time_range(start_time, end_time)
            records = self._history_records_from_deals(deals)
            by_ticket = {
                str(entry.get("position_id")): entry
                for entry in self.trade_history
                if isinstance(entry, dict) and entry.get("position_id") is not None
            }
            merged = 0
            closed = 0
            for record in records:
                ticket = str(record["position_id"])
                existing = by_ticket.get(ticket)
                if existing is None:
                    record["logged_at"] = (
                        record.get("close_time")
                        or record.get("open_time")
                        or end_time.isoformat()
                    )
                    self.trade_history.append(record)
                    by_ticket[ticket] = record
                    merged += 1
                else:
                    # Preserve strategy telemetry captured at entry while
                    # replacing broker-derived lifecycle and P/L fields.
                    existing.update({
                        key: value for key, value in record.items()
                        if value is not None
                    })
                    merged += 1
                if record.get("status") == "CLOSED":
                    closed += 1

            self.trade_history.sort(
                key=lambda item: str(
                    item.get("close_time")
                    or item.get("open_time")
                    or item.get("logged_at")
                    or ""
                )
            )
            self.trade_history[:] = self.trade_history[-50:]
            self._history_sync_status = {
                **self._history_sync_status,
                "status": "SYNCED",
                "deals_read": len(deals),
                "positions_merged": merged,
                "closed_positions": closed,
                "records_total": len(self.trade_history),
            }
            log.info(
                "📚 Broker history sync: deals=%d positions=%d closed=%d "
                "merged_total=%d",
                len(deals),
                len(records),
                closed,
                len(self.trade_history),
            )
            if merged:
                self._write_state("RUNNING", self._last_acc_info)
        except Exception as exc:
            self._history_sync_status = {
                **self._history_sync_status,
                "status": "ERROR",
                "error": str(exc),
            }
            log.warning("Broker history sync failed: %s", exc)

    def _apply_close_history(self, entry: dict, history: dict) -> bool:
        close_price = _history_float(history, "closePrice", "price")
        close_time = _history_datetime(
            history,
            "closeTime",
            "closeTimestampUTC",
            "historyTime",
            "executionTime",
        )
        if close_price is None or not close_time:
            log.warning(
                "Ignoring incomplete MT5 close-history record for "
                f"{entry.get('position_id')}"
            )
            return False

        trailing_stop = None
        ticket_id = str(entry.get("position_id") or entry.get("ticket"))
        for event in self.trade_history:
            if str(event.get("position_id")) != ticket_id:
                continue
            if event.get("action") == "TRAIL_SL":
                trailing_stop = _history_float(event, "new_sl")

        gross_profit = _history_float(history, "profit") or 0.0
        commission = _history_float(history, "commission") or 0.0
        swap = _history_float(history, "swap") or 0.0
        fee = _history_float(history, "fee") or 0.0
        entry.update({
            "ticket": ticket_id,
            "type": entry.get("direction", entry.get("type", "UNKNOWN")),
            "volume": entry.get("lot", entry.get("volume", 0.0)),
            "open_price": entry.get("entry", entry.get("open_price", 0.0)),
            "close_price": close_price,
            "open_time": entry.get("bar_time", entry.get("open_time")),
            "close_time": close_time,
            "profit_gross": gross_profit,
            "commission": commission,
            "swap": swap,
            "fee": fee,
            "profit": gross_profit + commission + swap + fee,
            "close_reason": classify_close_reason(
                history, entry, trailing_stop=trailing_stop
            ),
            "close_comment": _history_value(history, "closeComment", "comment"),
            "close_source": "MT5_HISTORY",
            "status": "CLOSED",
        })
        log.info(
            f"Trade closed ticket={ticket_id} reason={entry['close_reason']} "
            f"price={close_price:.5f} profit={entry['profit']:+.2f} "
            f"time={close_time}"
        )
        return True

    # ── Telegram command processing ───────────────────────────────────────────

    async def _process_commands(self) -> None:
        cmds = read_commands()
        if not cmds:
            return

        # NOTE: "pause" takes priority over "resume" if both appear simultaneously
        # (e.g. two commands queued in the same JSON file between poll cycles).
        pause_applied = False
        if cmds.get("pause"):
            if not self.paused:
                self.paused = True
                log.info("⏸  Robot PAUSED by Telegram command")
                self._write_state("PAUSED")
            clear_command("pause")
            pause_applied = True

        if cmds.get("resume") and not pause_applied:
            if self.paused:
                # If Guardian is halted, don't allow resume without explicit reset
                if self.guardian.is_halted:
                    log.warning(
                        "⚠️  Cannot resume: RiskGuardian is still halted.  "
                        "Send /reset_guardian first."
                    )
                else:
                    self.paused = False
                    log.info("▶  Robot RESUMED by Telegram command")
                    self._write_state("RUNNING")
            clear_command("resume")

        if cmds.get("stop"):
            log.info("🛑 STOP command received from Telegram")
            self.running = False
            clear_command("stop")

        if cmds.get("close_all"):
            log.info("📤 Closing all positions (Telegram command)")
            await self._close_all_positions()
            clear_command("close_all")

        # New: manual Guardian reset from Telegram panel
        if cmds.get("reset_guardian"):
            log.warning("🛡️  Guardian reset requested from Telegram")
            reset_payload = cmds.get("reset_guardian")
            reset_daily_baseline = (
                isinstance(reset_payload, dict)
                and bool(reset_payload.get("reset_daily_baseline"))
            )
            current_balance = None
            if reset_daily_baseline:
                if self._last_acc_info:
                    current_balance = self._last_acc_info.get("balance")
                if current_balance is None:
                    try:
                        fresh_account = await get_account_info()
                        current_balance = fresh_account.get("balance") if fresh_account else None
                        if fresh_account:
                            self._last_acc_info = fresh_account
                    except Exception as exc:
                        log.warning(
                            f"Could not fetch live balance for daily reset: {exc}"
                        )

            reset_ok = self.guardian.reset_halt(
                reset_daily_baseline=reset_daily_baseline,
                current_balance=float(current_balance) if current_balance is not None else None,
            )
            if reset_ok and self.paused:
                self.paused = False
                log.info("▶  Robot RESUMED after Guardian reset")
                self._write_state("RUNNING")
            elif not reset_ok:
                log.warning(
                    "🛡️  Guardian reset did not resume robot because "
                    "the daily baseline could not be reset"
                )
            clear_command("reset_guardian")

        # "start" — sent by Telegram panel Start button.
        # If paused, treat as resume. If already running, log and ignore.
        if cmds.get("start") and not pause_applied:
            if self.paused:
                if self.guardian.is_halted:
                    log.warning(
                        "⚠️  Cannot start: RiskGuardian is still halted.  "
                        "Send /reset_guardian first."
                    )
                else:
                    self.paused = False
                    log.info("▶  Robot STARTED (resumed) by Telegram command")
                    self._write_state("RUNNING")
            else:
                log.info("ℹ️  START command received — robot is already running")
            clear_command("start")

        # "restart_engine" — sent by Telegram panel Restart Engine button.
        # Sets running=False so the engine loop exits cleanly; the supervisor
        # in server.py applies exponential-backoff and restarts it.
        if cmds.get("restart_engine"):
            log.info(
                "🔄 RESTART_ENGINE command received from Telegram — "
                "stopping engine for supervisor restart"
            )
            self.running = False
            clear_command("restart_engine")

        # "restart_mt5" — sent by Telegram panel Restart MT5 button.
        # Disconnects from MetaAPI so the main loop reconnects
        # immediately (reconnect_attempts reset so no exponential backoff delay).
        if cmds.get("restart_mt5"):
            log.info(
                "🔌 RESTART_MT5 command received from Telegram — "
                "disconnecting for immediate reconnect"
            )
            await disconnect()
            self._reconnect_attempts = 0  # bypass exponential backoff
            self._write_state(
                "DISCONNECTED",
                extra={"info": "MetaAPI reconnect requested via Telegram"},
            )
            clear_command("restart_mt5")

        # "restart_telegram" — sent by Telegram panel Restart Telegram Bot button.
        # The Telegram panel service handles its own restart; the robot only
        # needs to acknowledge (clear) the command so it does not persist in Redis.
        if cmds.get("restart_telegram"):
            log.info("ℹ️  RESTART_TELEGRAM received — handled by panel service")
            clear_command("restart_telegram")
        # "update_risk" — sent by Telegram panel risk settings.
        # Payload keys (all optional): risk_percent, daily_loss_limit_pct,
        # max_drawdown_pct, slippage_points.  Each value updates the live
        # config and the Guardian thresholds without a restart.
        if cmds.get("update_risk"):
            payload = cmds["update_risk"]
            if isinstance(payload, dict):
                import live_trading.config as _live_cfg
                _g = globals()
                try:
                    if "risk_percent" in payload:
                        v = float(payload["risk_percent"])
                        _live_cfg.RISK_PERCENT = v; _g["RISK_PERCENT"] = v
                    if "daily_loss_limit_pct" in payload:
                        v = float(payload["daily_loss_limit_pct"])
                        _live_cfg.DAILY_LOSS_LIMIT_PCT = v; _g["DAILY_LOSS_LIMIT_PCT"] = v
                        self.guardian._daily_loss_limit_pct = v
                    if "max_drawdown_pct" in payload:
                        v = float(payload["max_drawdown_pct"])
                        _live_cfg.MAX_DRAWDOWN_PCT = v; _g["MAX_DRAWDOWN_PCT"] = v
                        self.guardian._max_drawdown_pct = v
                    if "slippage_points" in payload:
                        v = int(float(payload["slippage_points"]))
                        _live_cfg.SLIPPAGE_POINTS = v; _g["SLIPPAGE_POINTS"] = v
                    # "Auto Trail" switch on the Telegram panel's Risk menu —
                    # previously accepted but never actually applied anywhere.
                    if "auto_trailing" in payload:
                        v = bool(payload["auto_trailing"])
                        self.trailing_enabled = v
                        self._trailing_cfg.enabled = v
                        log.info(
                            f"📐 Adaptive ATR trailing stop "
                            f"{'ENABLED' if v else 'DISABLED'} via Telegram"
                        )
                    log.info(f"🔧 Risk config updated via Telegram: {payload}")
                except Exception as _upd_err:
                    log.warning(f"update_risk payload error: {_upd_err}")
            clear_command("update_risk")

        # "update_strategy" — sent by Telegram panel strategy settings.
        # Payload keys (all optional): min_confirmations (int),
        # price_action_standalone (bool).
        if cmds.get("update_strategy"):
            payload = cmds["update_strategy"]
            if isinstance(payload, dict):
                import live_trading.config as _live_cfg
                _g = globals()
                try:
                    if "min_confirmations" in payload:
                        v = int(float(payload["min_confirmations"]))
                        _live_cfg.MIN_CONFIRMATIONS = v; _g["MIN_CONFIRMATIONS"] = v
                    if "price_action_standalone" in payload:
                        raw = payload["price_action_standalone"]
                        if isinstance(raw, str):
                            v = raw.strip().lower() in {"1", "true", "yes", "on"}
                        else:
                            v = bool(raw)
                        _live_cfg.PRICE_ACTION_STANDALONE = v
                        _g["PRICE_ACTION_STANDALONE"] = v
                    log.info(f"🔧 Strategy config updated via Telegram: {payload}")
                except Exception as _upd_err:
                    log.warning(f"update_strategy payload error: {_upd_err}")
            clear_command("update_strategy")

    # INCIDENT RECOVERY (2026-08-10): these 4 real XAUUSD BUY 0.01 tickets were
    # opened before the phantom-row repair fix existed, then their local
    # trade-log record (and its Redis mirror) was lost across the several
    # restarts made while deploying/debugging that fix — leaving mt5rest's
    # corrupted rows for them with no known-good data to repair from, so the
    # gate/trailing-stop/close_all stayed blind to all four. Values below
    # come directly from the account screenshots and the robot's own logs at
    # the time each was opened (ticket → BUY 0.01 lots). Safe to delete this
    # block (and the two lines wiring it into _known_open_tickets) once all
    # four tickets have closed — after that mt5rest will simply stop
    # reporting them and this map becomes a no-op.
    _LEGACY_RECOVERED_POSITIONS = {
        "274131033": {"volume": 0.01, "direction": "BUY"},
        "274131357": {"volume": 0.01, "direction": "BUY"},
        "274131902": {"volume": 0.01, "direction": "BUY"},
        "274132482": {"volume": 0.01, "direction": "BUY"},
        # Opened 22:44:30 UTC during this same incident window, before the
        # entry-gate hardening below existed: a restart briefly emptied
        # trade_history, the gate saw zero open positions, and a live signal
        # was allowed through, stacking a 5th real position. Same recovery
        # treatment as the other four.
        "274134983": {"volume": 0.01, "direction": "BUY"},
    }

    def _known_open_tickets(self) -> dict:
        """Build {str(position_id): {"volume", "direction"}} from this robot's
        own trade log, for every position it has ever opened.

        Used only to repair a corrupted lone row in get_open_positions() (see
        connector._dedupe_positions) — never to assert that a ticket is still
        open. mt5rest's OpenedOrders is the sole source of truth for whether a
        ticket is currently open; this map only fixes its volume/type fields
        when it reports one of our own tickets with obviously corrupted data.
        """
        known: dict = dict(self._LEGACY_RECOVERED_POSITIONS)
        for entry in self.trade_history:
            pid = entry.get("position_id")
            direction = entry.get("direction")
            lot = entry.get("lot")
            if pid is None or direction is None or lot is None:
                continue
            known[str(pid)] = {"volume": lot, "direction": direction}
        return known

    async def _close_all_positions(self) -> None:
        try:
            positions = await get_open_positions(SYMBOL, self._known_open_tickets())
        except RuntimeError as exc:
            log.error(f"CLOSE_ALL: could not fetch positions from mt5rest: {exc}")
            return
        for p in positions:
            d = mt5_pos_to_dict(p)
            result = await close_position(d["id"])
            if result.success:
                log_trade(self.trade_history, {
                    "position_id": d["id"],
                    "action":      "CLOSED_BY_TELEGRAM",
                    "profit":      d.get("profit"),
                })
                # ROOT-CAUSE FIX: persist trade history immediately after each
                # close so the Telegram panel shows it without waiting for the
                # next bar's write_mt5_snapshot() call.
                self._write_state("RUNNING", self._last_acc_info)

    async def _safe_entry_order(
        self,
        decision: DecisionResult,
        candidate_strategy_slots: tuple[str, ...],
        timeframe: str,
        bar_time: datetime,
    ) -> tuple[Optional[TradeResult], list[dict], str, str]:
        """Re-check capacity and submit one entry atomically.

        The initial position check is intentionally kept earlier in the bar
        pipeline for fast feedback. This final check is the authoritative
        safety boundary immediately before the broker request.
        """
        async with self._entry_lock:
            try:
                confirm_result = await get_open_positions(
                    SYMBOL,
                    self._known_open_tickets(),
                    return_diagnostics=True,
                )
            except RuntimeError as exc:
                return None, [], "POSITION_CHECK_FAILED", str(exc)

            confirm_positions, dropped_unknown = confirm_result
            confirm_dicts = [
                mt5_pos_to_dict(position) for position in confirm_positions
            ]
            if dropped_unknown:
                return (
                    None,
                    confirm_dicts,
                    "UNKNOWN_POSITION_DATA",
                    f"Unidentified live position data was reported: "
                    f"{dropped_unknown}",
                )
            one_way_ok, one_way_reason = one_way_entry_allowed(
                confirm_dicts,
                decision.direction,
                allow_hedged_positions=ALLOW_HEDGED_POSITIONS,
            )
            if not one_way_ok:
                return (
                    None,
                    confirm_dicts,
                    "OPPOSITE_POSITION_GUARD",
                    one_way_reason,
                )

            if (
                self._last_entry_bar_time == bar_time
                and self._last_entry_direction
                and self._last_entry_direction != decision.direction
            ):
                return (
                    None,
                    confirm_dicts,
                    "SAME_BAR_DIRECTION_GUARD",
                    f"Opposite signal on already-traded bar: "
                    f"{self._last_entry_direction} → {decision.direction}",
                )

            slots_ok, slots_reason = available_for_strategy_slots(
                confirm_dicts,
                candidate_strategy_slots,
                max_open_positions=MAX_OPEN_TRADES,
            )
            if not slots_ok:
                return None, confirm_dicts, "STRATEGY_POSITION_LIMIT", slots_reason

            self._set_trade_permission(
                True,
                "READY_TO_PLACE_ORDER",
                ["All live entry gates passed"],
            )
            tp_params = decision.trade_params
            if tp_params is None:
                return (
                    None,
                    confirm_dicts,
                    "MISSING_TRADE_PARAMETERS",
                    "Allowed decision did not include SL/TP parameters",
                )
            original_entry_price = tp_params.entry_price

            # The decision engine's entry price can be several network calls
            # old by the time the final position check completes.  Fetch
            # broker symbol constraints first, then fetch the quote last so
            # the SL/TP re-anchor is based on the freshest executable price.
            symbol_params = await get_symbol_params(SYMBOL)
            stop_constraints = _broker_stop_constraints(symbol_params)
            if stop_constraints is None:
                return (
                    None,
                    confirm_dicts,
                    "SYMBOL_CONSTRAINTS_UNAVAILABLE",
                    f"Could not read broker SL/TP constraints for {SYMBOL}",
                )
            quote = await get_current_quote(SYMBOL)
            if not quote:
                return (
                    None,
                    confirm_dicts,
                    "QUOTE_UNAVAILABLE",
                    f"Could not refresh {SYMBOL} quote before order",
                )
            rebased = _rebase_order_prices(
                tp_params,
                decision.direction,
                quote,
                stop_constraints,
            )
            if rebased is None:
                return (
                    None,
                    confirm_dicts,
                    "INVALID_REBASED_STOPS",
                    f"Could not produce valid broker-side SL/TP for {SYMBOL}",
                )

            # Keep all post-order telemetry/trailing state aligned with the
            # exact prices sent to MTAPI, not the stale decision snapshot.
            tp_params.entry_price = rebased["entry_price"]
            tp_params.stop_loss = rebased["stop_loss"]
            tp_params.take_profit = rebased["take_profit"]
            tp_params.sl_distance_usd = round(rebased["sl_distance"], 2)
            tp_params.sl_distance_pips = round(rebased["sl_distance_pips"], 2)
            tp_params.risk_reward_ratio = round(
                rebased["tp_distance"] / rebased["sl_distance"], 2
            )
            tp_params.risk_amount = round(
                tp_params.lot_size
                * rebased["sl_distance"]
                * LOT_DOLLAR_PER_UNIT,
                2,
            )
            tp_params.min_lot_risk_exceeded = (
                tp_params.risk_amount > MAX_FIXED_LOT_RISK_USD + 0.01
            )
            if tp_params.min_lot_risk_exceeded:
                return (
                    None,
                    confirm_dicts,
                    "BROKER_STOP_DISTANCE_RISK",
                    f"Broker minimum stop distance would risk "
                    f"${tp_params.risk_amount:.2f}, above the "
                    f"${MAX_FIXED_LOT_RISK_USD:.2f} cap",
                )
            direction_sign = 1 if decision.direction == "BUY" else -1
            tp_params.break_even_at = round(
                tp_params.entry_price
                + direction_sign * rebased["sl_distance"],
                stop_constraints["digits"],
            )
            tp_params.trailing_activation_at = tp_params.break_even_at
            tp_params.trailing_stop_distance = round(
                rebased["sl_distance"] * 0.5,
                stop_constraints["digits"],
            )
            tp_params.break_even_sl = tp_params.entry_price

            broker_stops = stop_constraints["stops_level_points"]
            broker_freeze = stop_constraints["freeze_level_points"]
            broker_sl = stop_constraints["sl_level_points"]
            broker_tp = stop_constraints["tp_level_points"]
            log.info(
                f"ORDER_STOPS_CHECK [{timeframe}] {decision.direction} {SYMBOL}  "
                f"bid={float(quote['bid']):.{stop_constraints['digits']}f}  "
                f"ask={float(quote['ask']):.{stop_constraints['digits']}f}  "
                f"entry={tp_params.entry_price:.{stop_constraints['digits']}f}  "
                f"SL={tp_params.stop_loss:.{stop_constraints['digits']}f}  "
                f"TP={tp_params.take_profit:.{stop_constraints['digits']}f}  "
                f"sl_distance={rebased['sl_distance_pips']:.2f}pip  "
                f"tp_distance={rebased['tp_distance_pips']:.2f}pip  "
                f"stopsLevel={broker_stops if broker_stops is not None else 'unknown'}pt  "
                f"freezeLevel={broker_freeze if broker_freeze is not None else 'unknown'}pt  "
                f"symbolGroup.sl={broker_sl if broker_sl is not None else 'unknown'}pt  "
                f"symbolGroup.tp={broker_tp if broker_tp is not None else 'unknown'}pt  "
                f"point={stop_constraints['point']}  "
                f"digits={stop_constraints['digits']}  "
                f"reanchored_from={original_entry_price}"
            )
            order_comment = strategy_order_comment(
                COMMENT,
                candidate_strategy_slots,
            )
            log.info(
                f"🔔 SIGNAL [{timeframe}] {decision.direction}  "
                f"conf={decision.confidence:.1f}%  "
                f"lot={tp_params.lot_size}  "
                f"SL={tp_params.stop_loss}  TP={tp_params.take_profit}  "
                f"R:R={tp_params.risk_reward_ratio:.2f}  "
                f"slippage≤{SLIPPAGE_POINTS}pts"
            )
            result = await place_market_order(
                symbol=SYMBOL,
                direction=decision.direction,
                lot_size=tp_params.lot_size,
                sl=tp_params.stop_loss,
                tp=tp_params.take_profit,
                comment=order_comment,
                deviation=SLIPPAGE_POINTS,
            )
            return result, confirm_dicts, "", ""

    # ── Trade history persistence (survives restarts) ─────────────────────────

    def _load_trailing_state(self) -> None:
        """Restore immutable per-ticket trailing baselines from file or Redis."""
        state = None
        try:
            from live_trading.redis_ipc import redis_read_state
            state = redis_read_state()
        except Exception as exc:
            log.debug("Trailing state Redis restore skipped: %s", exc)
        if state is None:
            try:
                if os.path.exists(STATE_FILE):
                    with open(STATE_FILE, "r", encoding="utf-8") as state_file:
                        state = json.load(state_file)
            except Exception as exc:
                log.warning("Trailing state file restore failed: %s", exc)

        trailing = state.get("trailing_stop", {}) if isinstance(state, dict) else {}
        persisted = trailing.get("baselines", {}) if isinstance(trailing, dict) else {}
        restored = {}
        for ticket, baseline in (persisted.items() if isinstance(persisted, dict) else []):
            if not isinstance(baseline, dict):
                continue
            try:
                entry = float(baseline["entry"])
                risk_distance = float(baseline["risk_distance"])
                if risk_distance <= 0:
                    continue
                restored[str(ticket)] = {
                    "id": str(baseline.get("id", ticket)),
                    "direction": str(baseline["direction"]).upper(),
                    "entry": entry,
                    "risk_distance": risk_distance,
                    "initial_sl": float(baseline.get("initial_sl", 0.0)),
                    "timeframe": baseline.get("timeframe") or TIMEFRAME,
                    "highest_price_since_entry": float(
                        baseline.get("highest_price_since_entry", entry)
                    ),
                    "lowest_price_since_entry": float(
                        baseline.get("lowest_price_since_entry", entry)
                    ),
                    "breakeven_armed": bool(
                        baseline.get("breakeven_armed", False)
                    ),
                }
            except (KeyError, TypeError, ValueError):
                continue
        self._trail_baselines.update(restored)
        if restored:
            log.info(
                "Trailing baseline restore: tickets=%s source=%s",
                ",".join(sorted(restored)),
                "redis" if state and not os.path.exists(STATE_FILE) else "state",
            )

    def _load_trade_history(self) -> List[dict]:
        """
        Restore trade history from the last written robot_state.json, falling
        back to (and merging with) the Redis-mirrored copy.

        ROOT-CAUSE FIX: On Render, STATE_FILE lives on the service's ephemeral
        filesystem — every deploy/restart starts from a clean container, so
        the local JSON file is empty right after any restart. Without this,
        _known_open_tickets() (used to repair mt5rest's corrupted phantom-row
        volume/type fields — see connector._dedupe_positions) would also come
        back empty right after a restart, silently reopening the exact
        blind-spot the phantom-row fix targets until this session logs a
        brand-new trade itself. Redis (REDIS_URL) is the durable cross-service
        copy already used for guardian-state restore; merging it in here
        closes that gap. Falls back to file-only, then empty, if Redis is
        unavailable — no behavior change from before in that case.
        """
        history: List[dict] = []
        _file_n = 0
        try:
            if os.path.exists(STATE_FILE):
                with open(STATE_FILE, "r", encoding="utf-8") as f:
                    state = json.load(f)
                history = list(state.get("recent_trades", []))
                _file_n = len(history)
        except Exception as exc:
            log.warning(f"Could not restore trade history from file: {exc}")

        _redis_n = 0
        _redis_status = "unavailable"
        try:
            from live_trading.redis_ipc import redis_read_state
            redis_state = redis_read_state()
            if redis_state is None:
                _redis_status = "no data / unreachable"
            else:
                redis_history = redis_state.get("recent_trades", [])
                _redis_n = len(redis_history)
                _redis_status = f"{_redis_n} records"
                if redis_history:
                    # Merge on position_id — Redis may hold trades opened by a
                    # session whose local file never got persisted (or vice
                    # versa). Keep whichever entry we see first per ticket.
                    seen = {
                        entry.get("position_id") for entry in history
                        if entry.get("position_id") is not None
                    }
                    for entry in redis_history:
                        pid = entry.get("position_id")
                        if pid is not None and pid not in seen:
                            history.append(entry)
                            seen.add(pid)
        except Exception as exc:
            _redis_status = f"error: {exc}"
            log.debug(f"Could not restore trade history from Redis: {exc}")

        log.info(
            f"📂 Trade history restore: file={_file_n} record(s), "
            f"redis={_redis_status} → merged total={len(history)}"
        )
        return history

    # ── Guardian state helper ─────────────────────────────────────────────────

    @staticmethod
    def _guardian_extra(
        gs: GuardianStatus,
        event: str = "",
    ) -> dict:
        """Build guardian sub-dict for injection into robot_state.json."""
        d = {
            "guardian": {
                "halted":               gs.halted,
                "reason":               gs.reason,
                "daily_pnl":            gs.daily_pnl,
                "daily_pnl_pct":        gs.daily_pnl_pct,
                "drawdown_pct":         gs.drawdown_pct,
                "equity_peak":          gs.equity_peak,
                "session_open_balance": gs.session_open_balance,
                "daily_loss_limit_pct": gs.daily_loss_limit_pct,
                "max_drawdown_pct":     gs.max_drawdown_pct,
                "triggered_at":         gs.triggered_at,
            }
        }
        if event:
            d["guardian"]["event"] = event
        return d

    # ── State writer ──────────────────────────────────────────────────────────

    def _set_trade_permission(
        self,
        allowed: bool,
        stage: str,
        reasons: list[str],
    ) -> None:
        """Record final entry permission separately from signal state."""
        self._last_trade_permission = {
            "allowed": bool(allowed),
            "stage": stage,
            "reasons": [str(reason) for reason in reasons if str(reason).strip()],
        }

    def _write_state(
        self,
        status: str,
        acc_info: Optional[dict] = None,
        decision: Optional[DecisionResult] = None,
        position: Optional[dict] = None,
        extra: Optional[dict] = None,
    ) -> None:
        # Merge guardian data into extra (non-destructive)
        merged_extra: dict = {}
        if self._last_guardian_status is not None:
            merged_extra.update(
                self._guardian_extra(self._last_guardian_status)
            )
        if self._trail_baselines or self._last_trailing_statuses:
            merged_extra["trailing_stop"] = {
                "enabled": self.trailing_enabled,
                # Baselines are immutable per-ticket entry/risk snapshots.
                # Persist them beside live telemetry so a restart cannot
                # mistake a trailed SL for the original risk.
                "baselines": dict(self._trail_baselines),
                "positions": dict(self._last_trailing_statuses),
            }
        if extra:
            merged_extra.update(extra)
        if self._last_candle_telemetry:
            merged_extra["candle_telemetry"] = dict(self._last_candle_telemetry)
        merged_extra["trade_history_sync"] = dict(self._history_sync_status)

        write_robot_state(
            status           = status,
            decision         = decision or self.last_decision,
            open_position    = position,
            account_info     = acc_info or {},
            trade_history    = self.trade_history,
            loop_count       = self.loop_count,
            last_signal_time = (
                max(
                    (bt for bt in self._last_bar_times.values() if bt is not None),
                    default=None,
                ).isoformat()
                if any(v is not None for v in self._last_bar_times.values())
                else None
            ),
            extra = merged_extra or None,
            trade_permission = self._last_trade_permission,
            open_positions   = self._last_open_positions,
        )


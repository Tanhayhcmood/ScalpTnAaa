"""
Live Trading Loop — async M5 candle-close event handler via mt5rest bridge.

Flow per tick:
  1. Wait for next M5 candle close
  2. Fetch 300 closed candles via mt5rest bridge
  3. Run decision engine (all 7 signal engines)
  4. Gate: RiskGuardian circuit breakers (daily loss / drawdown)
  5. Gate: max open positions + trade allowed + Telegram not paused
  6. Place order via mt5rest executor (with slippage control)
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
import os
import sys
from datetime import datetime, timedelta, timezone
from typing import List, Optional

from live_trading.config import (
    SYMBOL, TIMEFRAME, CANDLE_WINDOW, RISK_PERCENT,
    MAX_OPEN_TRADES, COMMENT,
    BAR_CHECK_INTERVAL, RECONNECT_DELAY, SYNC_TIMEOUT,
    MIN_CONFIRMATIONS, REQUIRE_PRICE_ACTION,
    REQUIRE_SMC_PRICE_ACTION_WYCKOFF, USE_ATR_HIGH_VOL_FILTER,
    DAILY_LOSS_LIMIT_PCT, MAX_DRAWDOWN_PCT, SLIPPAGE_POINTS,
    STATE_FILE, GUARDIAN_STATE_FILE,
    TRAIL_ENABLED, TRAIL_ACTIVATION_R, TRAIL_STEP_R,
    TRAIL_LOCK_BUFFER_R, TRAIL_ATR_GAP_MULT, TRAIL_MIN_STEP_PRICE,
    MTF_ENABLED, MTF_TIMEFRAME, MTF_CANDLE_WINDOW,
    TRADE_TIMEFRAMES,
)
from live_trading.logger import get_logger
from live_trading.risk.guardian import RiskGuardian, GuardianStatus
from live_trading.risk.trailing_stop import (
    TrailingConfig, compute_staircase_sl, should_apply, r_multiple_of,
)
from live_trading.signals.decision_engine import run_decision_engine, DecisionResult, describe_strategy
from live_trading.signals.mtf_filter import compute_mtf_bias, mtf_allows_trade, MtfBias
from live_trading.signals.wyckoff_engine import calibrate_wyckoff, set_calibrated_config
from live_trading.mt5.connector import (
    connect, disconnect, ensure_connected,
    connect_with_retry, keepalive_mtapi,
    start_connection_watchdog, start_mt5_session_keepalive,
    fetch_candles, get_account_balance, get_account_info,
    get_open_positions, get_last_completed_bar_time,
    get_current_quote, mt5_pos_to_dict,
)
from live_trading.mt5.executor import (
    place_market_order, close_position, modify_position, TradeResult
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
        # Guard against opening multiple trades in the same tick when several
        # timeframes close simultaneously (e.g. M20+M10+M5 all fire at :20).
        # Without this, _on_new_bar() is called N times in one iteration of
        # _run_loop() and each call may see 0 open positions from mt5rest (the
        # bridge has not yet registered the trade placed by the previous call),
        # causing N trades to open instead of 1.
        self._trade_opened_this_tick: bool = False
        # tracks the last successfully placed trade (direction + bar_time)
        # so the post-SL cooldown gate can detect same-direction re-entry.
        self._last_entry_bar_time: Optional[datetime] = None
        self._last_entry_direction: str = ""
        self.trade_history: List[dict] = []
        self.last_decision: Optional[DecisionResult] = None

        # Risk Guardian — initialized after mt5rest bridge connects
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
        self._mt5_keepalive_task: asyncio.Task | None = None

        # Handle for the MTAPI keepalive background task (cancelled on stop)
        self._keepalive_task: Optional[asyncio.Task] = None

        # ── Staircase Trailing Stop ──────────────────────────────────────────
        # Toggleable at runtime via the Telegram panel's "Auto Trail" switch
        # (routed through the update_risk command — see _process_commands).
        self.trailing_enabled: bool = TRAIL_ENABLED
        self._trailing_cfg = TrailingConfig(
            enabled=TRAIL_ENABLED,
            activation_r=TRAIL_ACTIVATION_R,
            step_r=TRAIL_STEP_R,
            lock_buffer_r=TRAIL_LOCK_BUFFER_R,
            atr_gap_mult=TRAIL_ATR_GAP_MULT,
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
        self._trail_baselines: dict = {}  # {str(ticket): {"id", "direction", "entry", "risk_distance"}}
        self._last_trailing_statuses: dict = {}  # {str(ticket): status-dict}, for panel telemetry
        # Cached ATR (price units) from the last completed bar — reused by the
        # trailing engine between bars so it doesn't need its own candle fetch.
        self._last_atr: float = 0.0

    # ── Entry point ───────────────────────────────────────────────────────────

    async def start(self) -> bool:
        log.info("=" * 60)
        log.info("  GoldScalperPro v4 — LIVE TRADING ENGINE (mt5rest)")
        log.info(f"  Symbol: {SYMBOL}  |  Trade TFs: {chr(44).join(TRADE_TIMEFRAMES)} (highest first)")
        log.info(f"  Risk: {RISK_PERCENT}%  |  Max positions: {MAX_OPEN_TRADES}")
        log.info(f"  Min confirmations: {MIN_CONFIRMATIONS}")
        log.info(f"  Daily loss limit: {DAILY_LOSS_LIMIT_PCT}%  |  "
                 f"Max drawdown: {MAX_DRAWDOWN_PCT}%  |  "
                 f"Slippage: ≤{SLIPPAGE_POINTS}pts")
        log.info("=" * 60)

        self._write_state("STARTING")

        # Restore trade history from previous session (survives restarts)
        self.trade_history = self._load_trade_history()

        connected = await connect_with_retry(max_attempts=12, retry_delay=30.0)
        if not connected:
            log.error("Could not connect to MT5 via mt5rest bridge after 5 attempts. "
                      "Check MTAPI_URL, MT5_USER, and MT5_PASSWORD.")
            self._write_state("DISCONNECTED",
                              extra={"error": "mt5rest connection failed after retries"})
            return False  # non-False return signals failure to main.py for sys.exit(1)

        # ── MTAPI keepalive task — prevents Render free-tier sleep ─────────────
        # Store the handle so we can cancel it when the engine stops; without
        # this, the task becomes orphaned on every supervisor-driven restart and
        # multiple background pings accumulate across restarts.
        self._keepalive_task = asyncio.create_task(
            self._keepalive_loop(), name="mtapi_keepalive"
        )
        # Proactive connection watchdog: checks MT5 health every 60 s and
        # reconnects before the trading loop hits a failure.  Faster than
        # waiting for a bar-tick request to fail (worst case: one full bar).
        self._watchdog_task = asyncio.create_task(
            start_connection_watchdog(interval_seconds=30.0), name="mt5_watchdog"
        )
        # MT5 broker-session keepalive: pings /ConnectionStatus every 3 min
        # so the broker socket stays open and conn_id never expires silently.
        self._mt5_keepalive_task = asyncio.create_task(
            start_mt5_session_keepalive(), name="mt5_session_keepalive"
        )
        # Belt-and-suspenders: if any step below raises before _run_loop() is
        # entered, _run_loop()'s own finally block never executes, leaving the
        # keepalive task orphaned.  This outer try/finally guarantees cleanup
        # on every exit path from start() after the task is created.
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
                _live_login  = str(acc_info.get("login",  ""))
                _live_server = str(acc_info.get("server", ""))
                self.guardian.set_account_identity(_live_login, _live_server)

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
            await self._calibrate_wyckoff()
            _checkpoint("after calibrate_wyckoff")
            # Write RUNNING state immediately after connect with real account data
            # so the panel shows live balance before the first bar fires.
            self._write_state("RUNNING", self._last_acc_info)
            await self._run_loop()
        finally:
            # Cancel the keepalive task if it is still running.  _run_loop()'s
            # own finally already handles the normal exit path; this block only
            # fires when start() exits before _run_loop() is called (e.g. an
            # unexpected exception in _calibrate_wyckoff or get_account_info).
            # Cancelling an already-done task is a safe no-op.
            if self._keepalive_task and not self._keepalive_task.done():
                self._keepalive_task.cancel()
                try:
                    await self._keepalive_task
                except asyncio.CancelledError:
                    pass
                log.debug("MTAPI keepalive task cancelled in start() finally.")

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

    async def _keepalive_loop(self) -> None:
        """Ping mt5rest bridge every 10 minutes to prevent Render free-tier sleep."""
        _INTERVAL = 480  # 8 minutes — more margin under Render's 15-min sleep threshold
        while True:
            await asyncio.sleep(_INTERVAL)
            try:
                await keepalive_mtapi()
            except Exception:
                pass

    async def _run_loop(self) -> None:
        log.info(
            f"Entering main loop — checking every {BAR_CHECK_INTERVAL}s "
            f"across {len(TRADE_TIMEFRAMES)} timeframes: "
            + ", ".join(TRADE_TIMEFRAMES) + " …"
        )
        try:
            while self.running:
                _checkpoint(f"loop#{self.loop_count} tick start")
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
                ok = await ensure_connected(
                    SYNC_TIMEOUT,
                    attempt=self._reconnect_attempts + 1,
                )
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
                    self._reconnect_attempts = 0

                # Staircase trailing stop — checked every tick (not just on
                # candle close) so it reacts within seconds of price crossing
                # a step, not up to 5 minutes late.
                await self._manage_trailing_stop()
                _checkpoint(f"loop#{self.loop_count} trailing stop managed")

                # Reset the within-tick trade guard before processing this
                # tick's bars.  All _on_new_bar() calls that share this tick
                # (e.g. M20+M10+M5 closing simultaneously) will see the same
                # flag and only the first successful placement will go through.
                self._trade_opened_this_tick = False
                new_bars = await self._check_new_bars()
                _checkpoint(f"loop#{self.loop_count} new_bars={len(new_bars)}")
                if new_bars:
                    for _tf, _bar_time in new_bars:
                        self.loop_count += 1
                        log.info(
                            f"─── Bar #{self.loop_count} [{_tf}] "
                            f"at {_bar_time.isoformat()} ───"
                        )
                        await self._on_new_bar(_bar_time, _tf)
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
            # Cancel the MTAPI keepalive background task so it does not remain
            # orphaned when the supervisor restarts the engine inside the same
            # asyncio event loop.  Multiple orphaned tasks would fire redundant
            # pings every 10 min and accumulate across restarts indefinitely.
            if self._keepalive_task and not self._keepalive_task.done():
                self._keepalive_task.cancel()
                try:
                    await self._keepalive_task
                except asyncio.CancelledError:
                    pass
                log.debug("MTAPI keepalive task cancelled.")
            if getattr(self, "_mt5_keepalive_task", None) and not self._mt5_keepalive_task.done():
                self._mt5_keepalive_task.cancel()
                try:
                    await self._mt5_keepalive_task
                except (asyncio.CancelledError, Exception):
                    pass
            if getattr(self, "_watchdog_task", None) and not self._watchdog_task.done():
                self._watchdog_task.cancel()
                try:
                    await self._watchdog_task
                except asyncio.CancelledError:
                    pass
                log.debug("MT5 connection watchdog cancelled.")
            await disconnect()
            self._write_state("STOPPED")
            log.info("Engine stopped.")

    # ── Bar detection ─────────────────────────────────────────────────────────

    async def _check_new_bars(self) -> list[tuple[str, datetime]]:
        """Poll every configured trade timeframe and return a list of
        (timeframe, bar_time) pairs for every TF that has a new completed bar
        since the last tick.  Results preserve TRADE_TIMEFRAMES order, which
        is already sorted highest-first, so M20 signals are processed before
        M15, then M10, then M5.  If M20 opens a position, the M15/M10/M5
        handlers in the same tick will see it via get_open_positions() and
        skip entry, preventing duplicate positions.
        Never raises — individual TF errors are logged and skipped.

        The four MTAPI reads are independent, so they run as one concurrent
        batch.  This keeps the poll interval the same for the provider while
        reducing detection latency from the sum of four network round trips to
        roughly the slowest single request."""
        results: list[tuple[str, datetime]] = []

        async def _read_bar_time(tf: str) -> tuple[str, Optional[datetime]]:
            try:
                return tf, await get_last_completed_bar_time(SYMBOL, tf)
            except Exception as _bar_err:
                log.warning(f"[{tf}] Bar time check failed: {_bar_err}")
                return tf, None

        # gather() preserves input order, so the existing highest-to-lowest
        # timeframe processing order remains unchanged after the concurrent IO.
        bar_reads = await asyncio.gather(
            *(_read_bar_time(tf) for tf in TRADE_TIMEFRAMES)
        )
        for tf, bt in bar_reads:
            if bt is None:
                continue
            # ── Staleness guard ──────────────────────────────────────────────
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
            _bt_naive = bt.replace(tzinfo=None) if bt.tzinfo else bt
            _stale_cutoff = datetime.utcnow() - timedelta(hours=2)
            if _bt_naive < _stale_cutoff:
                # Still update last_bar_times (as naive) so we don't re-log
                self._last_bar_times[tf] = _bt_naive
                log.debug(
                    f"[{tf}] Bar {bt.isoformat()} is stale "
                    f"(>{int((datetime.utcnow() - _bt_naive).total_seconds()/3600)}h old) "
                    f"— waiting for MT5 historical data sync"
                )
                continue
            prev = self._last_bar_times.get(tf)
            if prev is None or _bt_naive > prev:
                self._last_bar_times[tf] = _bt_naive
                results.append((tf, _bt_naive))
        return results

    # ── Per-bar handler ───────────────────────────────────────────────────────

    async def _on_new_bar(self, bar_time: datetime, tf: str = TIMEFRAME) -> None:
        # 1. Fetch the entry timeframe and HTF bias data concurrently.  These
        # requests are independent and this overlaps their network latency
        # without changing the amount of data requested from MTAPI.
        htf_candles = []
        if MTF_ENABLED:
            primary_result, htf_result = await asyncio.gather(
                fetch_candles(SYMBOL, tf, CANDLE_WINDOW),
                fetch_candles(SYMBOL, MTF_TIMEFRAME, MTF_CANDLE_WINDOW),
                return_exceptions=True,
            )
            if isinstance(primary_result, Exception):
                log.warning(f"[{tf}] Candle fetch failed — skipping bar: {primary_result}")
                return
            candles = primary_result
            if isinstance(htf_result, Exception):
                log.warning(f"MTF fetch/analysis error (fail-safe, skipping): {htf_result}")
            else:
                htf_candles = htf_result
        else:
            candles = await fetch_candles(SYMBOL, tf, CANDLE_WINDOW)

        if len(candles) < 50:
            log.warning(f"Only {len(candles)} candles returned — skipping bar")
            return

        # 1b. Compute HTF bias before account / guardian checks.  The fetch
        # latency is already overlapped with the entry-timeframe request.
        # compute_mtf_bias() never raises; a bad fetch simply yields htf_bias=None.
        htf_bias: Optional[MtfBias] = None
        if MTF_ENABLED:
            try:
                if len(htf_candles) >= 50:
                    htf_bias = compute_mtf_bias(htf_candles)
                    log.info(
                        f"[{tf}] HTF ({MTF_TIMEFRAME}) bias: {htf_bias.direction}  "
                        f"trend={htf_bias.trend}  smc={htf_bias.smc_signal}  "
                        f"regime={htf_bias.regime}  strength={htf_bias.strength}"
                    )
                else:
                    log.warning(
                        f"HTF candles insufficient ({len(htf_candles)}) "
                        f"— MTF filter skipped this bar"
                    )
            except Exception as _mtf_exc:
                log.warning(f"MTF fetch/analysis error (fail-safe, skipping): {_mtf_exc}")

        # 2. Account info (live, required for Guardian)
        acc_info = await get_account_info()
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
            log.error(
                f"Cannot verify open positions — skipping trade entry this bar: {_pos_err}"
            )
            self._write_state("WAITING", acc_info)
            return
        pos_dicts = [mt5_pos_to_dict(p) for p in raw_positions]
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
            use_atr_high_vol=USE_ATR_HIGH_VOL_FILTER,
            require_price_action=REQUIRE_PRICE_ACTION,
            require_smc_price_action_wyckoff=REQUIRE_SMC_PRICE_ACTION_WYCKOFF,
        )
        self.last_decision = decision

        # 6. Write MT5 snapshot for Telegram panel
        last_c = candles[-1]
        # Compute ATR in price units using a 5-bar average True Range.
        # A single-candle TR (trs[-1]) makes the displayed ATR jump on every
        # wick, misleading the panel operator.  Using the same 5-bar window
        # as market_regime._calc_atr_values (fixed in Fix 1) keeps both
        # values consistent.
        _snap_trs = [
            max(candles[i].high - candles[i].low,
                abs(candles[i].high - candles[i - 1].close),
                abs(candles[i].low  - candles[i - 1].close))
            for i in range(1, len(candles))
        ]
        _snap_win = min(5, len(_snap_trs))
        _snap_atr = round(sum(_snap_trs[-_snap_win:]) / _snap_win, 4) if _snap_trs else 0.0
        # Cached for the staircase trailing engine, which runs between bars
        # (every BAR_CHECK_INTERVAL) and has no candle fetch of its own.
        self._last_atr = _snap_atr
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

        # 7. Gate: max positions
        if len(raw_positions) >= MAX_OPEN_TRADES:
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
            log.info(f"No trade → {reasons}")
            self._write_state(
                "SCANNING", acc_info, decision, pos,
                extra=self._guardian_extra(gs),
            )
            return

        # 8b. Gate: Multi-Timeframe alignment
        # Only runs when decision.allowed=True (we never block an already-rejected
        # trade with extra noise).  mtf_allows_trade() is a pure function that
        # never raises and returns (True, "") when htf_bias is None or NEUTRAL.
        if MTF_ENABLED and htf_bias is not None:
            _mtf_ok, _mtf_reason = mtf_allows_trade(htf_bias, decision.direction)
            if not _mtf_ok:
                log.info(f"⛔  {_mtf_reason}")
                _mtf_extra = {
                    **self._guardian_extra(gs),
                    "htf_bias": {
                        "direction": htf_bias.direction,
                        "trend":     htf_bias.trend,
                        "smc":       htf_bias.smc_signal,
                        "regime":    htf_bias.regime,
                        "strength":  htf_bias.strength,
                        "reasoning": htf_bias.reasoning,
                        "blocked":   _mtf_reason,
                    },
                }
                self._write_state("SCANNING", acc_info, decision, pos, extra=_mtf_extra)
                return

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
                log.info(
                    f"⏸ Post-SL cooldown [{tf}]: {decision.direction} last entered "
                    f"{_elapsed_min:.0f}min ago in {decision.regime} regime — "
                    f"cooldown {2*_tf_min}min, skipping bar"
                )
                self._write_state("WAITING", acc_info, decision, pos,
                                  extra=self._guardian_extra(gs))
                return

        # 8c. Safety re-check: confirm we are still flat immediately before
        # sending the order.
        #
        # ROOT CAUSE: mt5rest has occasionally corrupted a genuinely open
        # position's row into a "lone row with an insane volume" (see
        # _dedupe_positions in mt5/connector.py) — a bridge-side glitch, not a
        # real second position. _dedupe_positions correctly discards that
        # garbage row as unreliable, but the side effect is that
        # get_open_positions() briefly reports the account as flat (raw
        # positions = []) even though a real position is still open in MT5.
        # If that happens to coincide with step 4's position check above, the
        # MAX_OPEN_TRADES gate (step 7) sees 0 open positions and lets a
        # second, unintended position stack on top of the first.
        #
        # Re-polling right here — seconds later, after the decision engine,
        # snapshot write, and all other gates have already run — is enough
        # time for a transient bridge glitch to clear. If a position now
        # shows up, we abort this entry rather than risk stacking a duplicate.
        # This costs one extra read-only mt5rest call only on the path that
        # is about to place an order; every other code path (trailing stop,
        # /close_all, panel snapshot) is untouched.
        try:
            _confirm_positions = await get_open_positions(SYMBOL, self._known_open_tickets())
        except RuntimeError as _confirm_err:
            log.error(
                f"Pre-order safety re-check could not verify positions — "
                f"skipping entry this bar: {_confirm_err}"
            )
            self._write_state("WAITING", acc_info, decision, pos,
                               extra=self._guardian_extra(gs))
            return
        if _confirm_positions:
            _confirm_pos = mt5_pos_to_dict(_confirm_positions[0])
            log.warning(
                f"Pre-order safety re-check found position "
                f"{_confirm_pos.get('id')} that was missing from the earlier "
                f"scan this bar (likely a transient mt5rest reporting glitch) "
                f"— aborting this entry to avoid stacking a duplicate position."
            )
            self._write_state("HOLDING", acc_info, decision, _confirm_pos,
                               extra=self._guardian_extra(gs))
            return

        # 9. ── PLACE ORDER ────────────────────────────────────────────────────
        tp_params = decision.trade_params
        log.info(
            f"🔔 SIGNAL [{tf}] {decision.direction}  "
            f"conf={decision.confidence:.1f}%  "
            f"lot={tp_params.lot_size}  "
            f"SL={tp_params.stop_loss}  TP={tp_params.take_profit}  "
            f"R:R={tp_params.risk_reward_ratio:.2f}  "
            f"slippage≤{SLIPPAGE_POINTS}pts"
        )

        result: TradeResult = await place_market_order(
            symbol    = SYMBOL,
            direction = decision.direction,
            lot_size  = tp_params.lot_size,
            sl        = tp_params.stop_loss,
            tp        = tp_params.take_profit,
            comment   = COMMENT,
            deviation = SLIPPAGE_POINTS,
        )

        if result.success:
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
                "bar_time":    bar_time.isoformat(),
                "strategy":    strategy,
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
            }
            # Build a synthetic position so the Telegram panel reflects the
            # newly opened trade immediately rather than waiting up to 5 min
            # for the next bar to re-fetch live positions.
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
                "comment":    COMMENT,
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
            }
        return None

    async def _manage_trailing_stop(self) -> None:
        """Check EVERY open position and ratchet each one's SL forward.

        Runs on every loop tick (every BAR_CHECK_INTERVAL seconds) — not just
        on M5 candle close — so the stop reacts within seconds of price
        moving through a staircase step, instead of waiting up to 5 minutes.

        FIX: this previously looked only at raw_positions[0] — correct while
        the robot truly ever had at most one open position, but silently
        wrong the moment more than one position exists at once (e.g. a
        MAX_OPEN_TRADES bypass, or positions carried over from before that
        was fixed): every position after the first got no SL management at
        all — its stop stayed exactly where it was placed at entry no
        matter how deep into profit price ran. Every currently open position
        now gets its own independent staircase, keyed by ticket.
        """
        if not self.trailing_enabled:
            return

        try:
            raw_positions = await get_open_positions(SYMBOL, self._known_open_tickets())
        except RuntimeError as exc:
            log.debug(f"Trailing check skipped — could not fetch positions: {exc}")
            return

        if not raw_positions:
            self._trail_baselines = {}
            self._last_trailing_statuses = {}
            return

        # Drop baselines/status for any position that is no longer open
        # (closed by SL/TP/manual close) so stale entries don't accumulate.
        live_ids = {str(mt5_pos_to_dict(raw)["id"]) for raw in raw_positions}
        self._trail_baselines = {
            pid: b for pid, b in self._trail_baselines.items() if pid in live_ids
        }
        self._last_trailing_statuses = {
            pid: s for pid, s in self._last_trailing_statuses.items() if pid in live_ids
        }

        quote = await get_current_quote(SYMBOL)
        if not quote:
            return  # no live price this tick — try again next tick

        for raw in raw_positions:
            pos = mt5_pos_to_dict(raw)
            pos_id = str(pos["id"])

            baseline = self._trail_baselines.get(pos_id)
            if not baseline:
                baseline = self._restore_trail_baseline(pos)
                if baseline:
                    self._trail_baselines[pos_id] = baseline
            if not baseline:
                continue  # no reliable risk baseline for this one — never trail blind

            direction = baseline["direction"]
            # Close-side price: a BUY exits (and is stopped out) at the bid,
            # a SELL exits at the ask — trailing off the wrong side would
            # trail too aggressively by the full spread.
            current_price = quote["bid"] if direction.upper() == "BUY" else quote["ask"]

            candidate_sl = compute_staircase_sl(
                direction=direction,
                entry=baseline["entry"],
                risk_distance=baseline["risk_distance"],
                current_price=current_price,
                atr=self._last_atr,
                cfg=self._trailing_cfg,
            )

            r_now = r_multiple_of(
                direction, baseline["entry"], baseline["risk_distance"], current_price,
            )
            self._last_trailing_statuses[pos_id] = {
                "active":       candidate_sl is not None,
                "r_multiple":   r_now,
                "current_sl":   pos["sl"],
                "candidate_sl": candidate_sl,
            }

            if not should_apply(direction, pos["sl"], candidate_sl, self._trailing_cfg.min_step_price):
                continue

            result = await modify_position(pos["id"], candidate_sl, pos["tp"])
            if result.success:
                log.info(
                    f"📐 Trailing stop advanced — position {pos['id']}  "
                    f"{direction}  +{r_now:.2f}R  SL {pos['sl']:.2f} → {candidate_sl:.2f}"
                )
                log_trade(self.trade_history, {
                    "position_id": pos["id"],
                    "action":      "TRAIL_SL",
                    "direction":   direction,
                    "r_multiple":  r_now,
                    "old_sl":      pos["sl"],
                    "new_sl":      candidate_sl,
                })
            else:
                log.warning(
                    f"Trailing stop modify failed for position {pos['id']}: {result.message}"
                )

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
            self.guardian.reset_halt()
            if self.paused:
                self.paused = False
                log.info("▶  Robot RESUMED after Guardian reset")
                self._write_state("RUNNING")
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
        # Disconnects from the mt5rest bridge so the main loop reconnects
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
                extra={"info": "MT5 reconnect requested via Telegram"},
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
                        log.info(f"📐 Staircase trailing stop {'ENABLED' if v else 'DISABLED'} via Telegram")
                    log.info(f"🔧 Risk config updated via Telegram: {payload}")
                except Exception as _upd_err:
                    log.warning(f"update_risk payload error: {_upd_err}")
            clear_command("update_risk")

        # "update_strategy" — sent by Telegram panel strategy settings.
        # Payload keys (all optional): min_confirmations (int).
        if cmds.get("update_strategy"):
            payload = cmds["update_strategy"]
            if isinstance(payload, dict):
                import live_trading.config as _live_cfg
                _g = globals()
                try:
                    if "min_confirmations" in payload:
                        v = int(float(payload["min_confirmations"]))
                        _live_cfg.MIN_CONFIRMATIONS = v; _g["MIN_CONFIRMATIONS"] = v
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

    # ── Trade history persistence (survives restarts) ─────────────────────────

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
        if self._last_trailing_statuses:
            merged_extra["trailing_stop"] = {
                "enabled": self.trailing_enabled,
                # Keyed by position ticket id so the panel can show every
                # open position's own staircase progress, not just one.
                "positions": dict(self._last_trailing_statuses),
            }
        if extra:
            merged_extra.update(extra)

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
        )


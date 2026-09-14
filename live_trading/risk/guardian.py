"""
Risk Guardian — Circuit Breaker for GoldScalperPro v4 Live Trading.

Implements three independent protection layers:
  1. Daily Loss Limit   — halt if intraday PnL drops below -DAILY_LOSS_LIMIT_PCT%
                          of the session-open balance.  Resets automatically at
                          UTC midnight so a new day gets a fresh window.
  2. Peak Drawdown Stop — halt if live equity falls more than MAX_DRAWDOWN_PCT%
                          below the highest equity seen this session.
  3. Not-Initialized    — block all trading until .initialize() is called with
                          live account data; prevents cold-start trades.

Design principles
  • Zero side-effects: Guardian is read-only about the account.  It NEVER places
    or cancels orders itself — it only returns GuardianStatus.
    The caller (live_loop.py) decides what to do (pause / close-all / log).
  • Idempotent halt: once triggered, subsequent .check() calls keep returning
    halted=True at WARNING level — no log flood.
  • Transparent: every metric is surfaced in GuardianStatus so the Telegram panel
    can display real-time risk data without any extra queries.
  • Configurable: all thresholds come from config.py env vars — no magic numbers.

Usage (in live_loop.py):
    guardian = RiskGuardian(DAILY_LOSS_LIMIT_PCT, MAX_DRAWDOWN_PCT)
    guardian.initialize(balance, equity)          # after MetaAPI connect
    ...
    gs = guardian.check(balance, equity)          # before each trade
    if gs.halted:
        self.paused = True
        ...
"""

import json
import os
from dataclasses import dataclass
from datetime import datetime, timezone, date
from typing import Optional

from live_trading.config import GUARDIAN_STATE_FILE as _GUARDIAN_STATE_FILE
from live_trading.logger import get_logger

log = get_logger()


# ── Data class returned by every .check() call ────────────────────────────────

@dataclass
class GuardianStatus:
    halted:               bool
    reason:               str          # empty string when not halted
    daily_pnl:            float        # running daily PnL in account currency
    daily_pnl_pct:        float        # daily_pnl as % of day-open balance
    drawdown_pct:         float        # current pullback from equity peak (%)
    equity_peak:          float        # highest equity seen this session
    session_open_balance: float        # balance at .initialize() time
    daily_loss_limit_pct: float        # configured limit
    max_drawdown_pct:     float        # configured limit
    triggered_at:         Optional[str]  # ISO timestamp of first trigger


# ── Main guardian class ───────────────────────────────────────────────────────

class RiskGuardian:
    """
    Stateful per-session risk circuit breaker.

    Lifecycle:
        guardian = RiskGuardian(daily_loss_limit_pct=3.0, max_drawdown_pct=8.0)
        guardian.initialize(balance, equity)   # call once after connecting
        ...
        gs = guardian.check(balance, equity)   # call every bar, before trade
        if gs.halted:
            # pause the robot
    """

    def __init__(self, daily_loss_limit_pct: float, max_drawdown_pct: float,
                 account_login: str = "", account_server: str = "") -> None:
        self._daily_loss_limit_pct = daily_loss_limit_pct
        self._max_drawdown_pct     = max_drawdown_pct

        # Account identity — used to reject stale state from a different account
        self._account_login:        str             = str(account_login)
        self._account_server:       str             = str(account_server)

        self._initialized:          bool            = False
        self._halted:               bool            = False
        self._halt_reason:          str             = ""
        self._triggered_at:         Optional[str]   = None

        self._equity_peak:          float           = 0.0
        self._session_open_balance: float           = 0.0
        self._day_open_balance:     float           = 0.0
        self._last_day:             Optional[date]  = None

        # Suppress repeated WARNING logs when already halted
        self._halt_log_count:       int             = 0
        self._HALT_LOG_EVERY:       int             = 4   # log every N checks

    # ── Public lifecycle ──────────────────────────────────────────────────────

    def initialize(self, balance: float, equity: float) -> None:
        """
        Capture baseline account values.  Must be called once after the MetaAPI
        connection is established and account info is available.
        """
        self._equity_peak          = equity
        self._session_open_balance = balance
        self._day_open_balance     = balance
        self._last_day             = datetime.now(timezone.utc).date()
        self._initialized          = True

        log.info(
            "🛡️  RiskGuardian ACTIVE — "
            f"session_balance={balance:.2f}  equity_peak={equity:.2f}  "
            f"daily_limit={self._daily_loss_limit_pct}%  "
            f"max_drawdown={self._max_drawdown_pct}%"
        )
        # Persist the healthy baseline too, so a restart cannot reset the
        # session risk window before the first circuit-breaker event.
        self._save_state()

    def check(self, balance: float, equity: float) -> GuardianStatus:
        """
        Evaluate circuit breakers against live account figures.
        Returns GuardianStatus; halted=True means DO NOT trade.
        """
        if not self._initialized:
            return self._status(
                halted=True,
                reason="Guardian not initialized — call .initialize() first",
                daily_pnl=0.0, daily_pnl_pct=0.0,
                drawdown_pct=0.0,
            )

        # ── Day rollover check ────────────────────────────────────────────────
        self._maybe_day_reset(balance)

        # ── Update equity high-water mark ─────────────────────────────────────
        if equity > self._equity_peak:
            prev = self._equity_peak
            self._equity_peak = equity
            if equity - prev > 0.01:  # avoid log spam on tiny ticks
                log.debug(f"🛡️  Equity peak updated: {prev:.2f} → {equity:.2f}")
            # Keep the high-water mark durable across restarts.
            self._save_state()

        # ── Compute risk metrics ──────────────────────────────────────────────
        day_open   = self._day_open_balance
        daily_pnl  = balance - day_open
        daily_pct  = (daily_pnl / day_open * 100) if day_open > 0 else 0.0
        drawdown   = ((self._equity_peak - equity) / self._equity_peak * 100
                      if self._equity_peak > 0 else 0.0)

        # ── Evaluate circuit breakers (only once — halt is sticky) ────────────
        if not self._halted:

            # Layer 1 — Daily loss limit
            if daily_pct <= -self._daily_loss_limit_pct:
                self._trigger(
                    f"🚨 DAILY LOSS LIMIT — "
                    f"day PnL={daily_pnl:+.2f} ({daily_pct:+.2f}%)  "
                    f"limit={self._daily_loss_limit_pct}%"
                )

            # Layer 2 — Peak drawdown
            elif drawdown >= self._max_drawdown_pct:
                self._trigger(
                    f"🚨 MAX DRAWDOWN STOP — "
                    f"drawdown={drawdown:.2f}%  "
                    f"equity={equity:.2f}  peak={self._equity_peak:.2f}  "
                    f"limit={self._max_drawdown_pct}%"
                )

        else:
            # Already halted — log at reduced frequency
            self._halt_log_count += 1
            if self._halt_log_count % self._HALT_LOG_EVERY == 1:
                log.warning(f"🛡️  Guardian HALTED ({self._halt_reason})")

        return self._status(
            halted       = self._halted,
            reason       = self._halt_reason,
            daily_pnl    = daily_pnl,
            daily_pnl_pct = daily_pct,
            drawdown_pct = drawdown,
        )

    def reset_halt(self, *, reset_daily_baseline: bool = False,
                   current_balance: float | None = None) -> bool:
        """
        Manually clear a halt.
        Exposed to Telegram panel via /reset_guardian command.
        Use with caution — understand WHY the halt triggered first.

        When ``reset_daily_baseline`` is requested, ``current_balance`` must
        be a positive live account balance.  This intentionally resets only
        the UTC-day loss window; the session equity high-water mark is kept so
        the max-drawdown circuit breaker remains effective.
        """
        if reset_daily_baseline:
            if current_balance is None or current_balance <= 0:
                log.warning(
                    "🛡️  Daily baseline reset rejected — "
                    "live balance is unavailable or invalid"
                )
                return False
            self._day_open_balance = float(current_balance)
            self._last_day = datetime.now(timezone.utc).date()
            log.warning(
                "🛡️  Daily loss baseline RESET to current balance "
                f"{self._day_open_balance:.2f}"
            )

        log.warning(
            "🛡️  Guardian halt MANUALLY CLEARED — "
            "previous reason: " + (self._halt_reason or "none")
        )
        self._halted       = False
        self._halt_reason  = ""
        self._triggered_at = None
        self._halt_log_count = 0
        self._save_state()
        return True

    def set_account_identity(self, login: str, server: str) -> None:
        """Set account identity used to validate persisted state on restore."""
        self._account_login  = str(login)
        self._account_server = str(server)

    def restore_state(self, data: dict) -> bool:
        """
        Restore Guardian state from a previously saved state dict.

        Returns True when state was accepted and restored, False when the state
        was rejected (mismatched account identity or invalid baselines).  The
        caller MUST call initialize() when this returns False.

        ROOT-CAUSE FIX: The global Redis guardian key is shared across restarts
        and could carry a halted state that belongs to a different account or a
        previous session with stale baselines.  We now validate:
          1. Account identity (login + server) — reject foreign-account state.
          2. Positive baselines — reject zero/negative equity_peak / balances,
             which would cause immediate false drawdown violations.
        """
        # ── 1. Account identity check ─────────────────────────────────────────
        if self._account_login or self._account_server:
            saved_login  = str(data.get("account_login",  ""))
            saved_server = str(data.get("account_server", ""))
            if saved_login and saved_server:
                if (saved_login != self._account_login or
                        saved_server != self._account_server):
                    log.warning(
                        "🛡️  Guardian state belongs to a different account "
                        f"({saved_server}/{saved_login} vs "
                        f"{self._account_server}/{self._account_login}) — "
                        "discarding stale state, cold start"
                    )
                    return False

        # ── 2. Baseline sanity check ──────────────────────────────────────────
        equity_peak          = float(data.get("equity_peak",          0.0))
        session_open_balance = float(data.get("session_open_balance", 0.0))
        day_open_balance     = float(data.get("day_open_balance",     0.0))

        if equity_peak <= 0 or day_open_balance <= 0 or session_open_balance <= 0:
            log.warning(
                "🛡️  Guardian state has zero/negative baselines "
                f"(equity_peak={equity_peak}, day_open={day_open_balance}, "
                f"session_open={session_open_balance}) — "
                "discarding invalid state, cold start"
            )
            return False

        # ── 3. Accept and restore ─────────────────────────────────────────────
        self._halted               = bool(data.get("halted", False))
        self._halt_reason          = str(data.get("halt_reason", ""))
        self._triggered_at         = data.get("triggered_at")          # str or None
        self._equity_peak          = equity_peak
        self._session_open_balance = session_open_balance
        self._day_open_balance     = day_open_balance

        last_day_str = data.get("last_day")
        if last_day_str:
            try:
                self._last_day = date.fromisoformat(last_day_str)
            except Exception:
                self._last_day = None

        # Restore risk limits if they were saved (UPDATE_RISK via panel persists
        # these). An explicitly configured environment value takes precedence,
        # so an operator can intentionally change a limit in Render without a
        # stale Redis snapshot silently putting the old limit back on restart.
        saved_daily_limit = data.get("daily_loss_limit_pct")
        if saved_daily_limit is not None and "DAILY_LOSS_LIMIT_PCT" not in os.environ:
            try:
                self._daily_loss_limit_pct = float(saved_daily_limit)
            except (TypeError, ValueError):
                pass
        saved_drawdown = data.get("max_drawdown_pct")
        if saved_drawdown is not None and "MAX_DRAWDOWN_PCT" not in os.environ:
            try:
                self._max_drawdown_pct = float(saved_drawdown)
            except (TypeError, ValueError):
                pass

        # Mark as initialized — baselines are restored from persisted state
        self._initialized = True

        log.info(
            "🛡️  Guardian state RESTORED — "
            f"halted={self._halted}  "
            f"equity_peak={self._equity_peak:.2f}  "
            f"day_open_balance={self._day_open_balance:.2f}"
        )
        if self._halted:
            log.critical(
                "🛡️  Guardian is HALTED (restored): "
                f"{self._halt_reason}  |  triggered_at={self._triggered_at}"
            )
        return True

    @property
    def is_halted(self) -> bool:
        return self._halted

    @property
    def is_initialized(self) -> bool:
        """True once initialize() or restore_state() has been called."""
        return self._initialized

    # ── Internal helpers ──────────────────────────────────────────────────────

    def _trigger(self, reason: str) -> None:
        self._halted        = True
        self._halt_reason   = reason
        self._triggered_at  = datetime.now(timezone.utc).isoformat()
        self._halt_log_count = 0
        log.critical(f"🛡️  CIRCUIT BREAKER TRIGGERED: {reason}")
        self._save_state()

    def _maybe_day_reset(self, balance: float) -> None:
        """Reset the daily PnL baseline at UTC midnight."""
        today = datetime.now(timezone.utc).date()
        if self._last_day is None or today > self._last_day:
            log.info(
                f"🌅 UTC day rollover — resetting daily loss counter.  "
                f"Previous day balance: {self._day_open_balance:.2f} → now: {balance:.2f}"
            )
            self._day_open_balance = balance
            self._last_day         = today

            # Auto-clear a daily-loss-only halt at day boundary
            if self._halted and "DAILY LOSS" in self._halt_reason:
                log.info("🛡️  Daily loss halt auto-cleared for new trading day")
                self._halted        = False
                self._halt_reason   = ""
                self._triggered_at  = None
                self._halt_log_count = 0

            self._save_state()

    def _save_state(self) -> None:
        """Persist current Guardian state to Redis and disk.

        Called on halt trigger, manual halt reset, and UTC day rollover.
        Redis is the durable cross-service copy on Render; the local file
        remains a fallback for single-process and local deployments. Failures
        are non-fatal — Guardian continues operating in memory.
        """
        state = {
            "halted":               self._halted,
            "halt_reason":          self._halt_reason,
            "triggered_at":         self._triggered_at,
            "equity_peak":          self._equity_peak,
            "session_open_balance": self._session_open_balance,
            "day_open_balance":     self._day_open_balance,
            "last_day":             self._last_day.isoformat() if self._last_day else None,
            "written_at":           datetime.now(timezone.utc).isoformat(),
            # Account identity — lets restore_state() reject foreign-account state
            "account_login":        self._account_login,
            "account_server":       self._account_server,
            # Risk limits — persisted so panel-side UPDATE_RISK changes
            # survive a service restart without re-sending the command.
            "daily_loss_limit_pct": self._daily_loss_limit_pct,
            "max_drawdown_pct":     self._max_drawdown_pct,
        }

        try:
            from live_trading.redis_ipc import redis_write_guardian_state
            redis_write_guardian_state(state)
        except Exception as exc:
            log.warning(f"Guardian Redis state save skipped: {exc}")

        tmp = _GUARDIAN_STATE_FILE + ".tmp"
        try:
            # Ensure parent directory exists (e.g. /data/ on a Render persistent disk).
            dir_path = os.path.dirname(_GUARDIAN_STATE_FILE)
            if dir_path:
                os.makedirs(dir_path, exist_ok=True)
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump(state, f, indent=2)
            os.replace(tmp, _GUARDIAN_STATE_FILE)
        except Exception as exc:
            log.error(f"Guardian state save failed ({_GUARDIAN_STATE_FILE}): {exc}")

    def _status(
        self,
        halted:        bool,
        reason:        str,
        daily_pnl:     float,
        daily_pnl_pct: float,
        drawdown_pct:  float,
    ) -> GuardianStatus:
        return GuardianStatus(
            halted               = halted,
            reason               = reason,
            daily_pnl            = round(daily_pnl, 2),
            daily_pnl_pct        = round(daily_pnl_pct, 4),
            drawdown_pct         = round(drawdown_pct, 4),
            equity_peak          = round(self._equity_peak, 2),
            session_open_balance = round(self._session_open_balance, 2),
            daily_loss_limit_pct = self._daily_loss_limit_pct,
            max_drawdown_pct     = self._max_drawdown_pct,
            triggered_at         = self._triggered_at,
        )


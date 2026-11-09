"""Opt-in, non-trading diagnostics for live M5 entry decisions."""

from collections import Counter, deque
from datetime import datetime, timedelta, timezone


_WINDOW = timedelta(hours=24)
_MAX_RECENT_EVENTS = 500


def classify_reason(reason: object) -> str:
    text = str(reason or "").strip().lower()
    if not text or "no unique strategy direction" in text:
        return "NO_CANDIDATE"
    if "target room" in text or "target-room" in text:
        return "TARGET_ROOM"
    if "marginal conf" in text or "r:r" in text or "risk/reward" in text:
        return "RR_MIN"
    if "confidence" in text or "conf_min" in text:
        return "CONF_MIN"
    if "adx" in text:
        return "QUALITY_ADX"
    if "regime" in text and ("allow" in text or "disabled" in text):
        return "REGIME_DISALLOW"
    if "active strategy" in text or "active entry" in text:
        return "ACTIVE_POLICY"
    if "strong trend" in text:
        return "STRONG_TREND_VALIDATION"
    if "news" in text:
        return "NEWS"
    if "spread" in text:
        return "SPREAD"
    if "session" in text or "trading window" in text or "friday" in text:
        return "SESSION_WINDOW"
    if "h1" in text or "higher-timeframe" in text or "htf" in text:
        return "H1_GATE"
    if "risk" in text or "minimum lot" in text or "stop-loss" in text:
        return "RISK_CAP"
    if "entry filter" in text or "confirmation" in text or "breakout" in text:
        return "ENTRY_FILTER"
    return "ENTRY_GUARD"


def classify_stage(stage: object, reasons: object = None) -> str:
    name = str(stage or "").strip().upper()
    reason_list = reasons if isinstance(reasons, (list, tuple)) else [reasons]
    reason = next((item for item in reason_list if item), "")
    fixed = {
        "MAX_OPEN_TRADES": "MAX_OPEN",
        "STRATEGY_POSITION_LIMIT": "MAX_OPEN",
        "WITHIN_TICK_DUPLICATE_GUARD": "ENTRY_GUARD",
        "ACTIVE_STRATEGY_BLOCKED": "ACTIVE_POLICY",
        "STRONG_TREND_STRUCTURE_BLOCKED": "STRONG_TREND_VALIDATION",
        "SPREAD_ATR_GATE_BLOCKED": "SPREAD",
        "LOSS_STREAK_PAUSED": "LOSS_STREAK",
        "NEWS_FILTER_BLOCKED": "NEWS",
        "H1_TREND_GATE_BLOCKED": "H1_GATE",
        "MTF_HARD_BLOCKED": "REGIME_DISALLOW",
        "MAX_RANGE_TRADES_PER_SESSION": "SESSION_WINDOW",
        "GUARDIAN_HALTED": "RISK_CAP",
        "RANGE_COOLDOWN": "ENTRY_GUARD",
        "RANGE_CONFIRMATIONS_BLOCKED": "ENTRY_FILTER",
        "POSITION_CHECK_FAILED": "ENTRY_GUARD",
        "UNKNOWN_POSITION_DATA": "ENTRY_GUARD",
        "ORDER_FAILED": "ENTRY_GUARD",
        "MARKET_CLOSED": "SESSION_WINDOW",
    }
    if name in fixed:
        return fixed[name]
    if name == "ENTRY_GATES_BLOCKED":
        return classify_reason(reason)
    if name in {"EVALUATING", "NOT_EVALUATED"}:
        return "NO_CANDIDATE"
    if name == "SIGNAL_BLOCKED":
        return classify_reason(reason)
    return classify_reason(reason or name)


def _ordered(counter: Counter) -> dict:
    return {
        key: value
        for key, value in sorted(counter.items(), key=lambda item: (-item[1], item[0]))
        if value
    }


def _parse_time(value: object) -> datetime | None:
    if not isinstance(value, str):
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


class DecisionFunnel:
    """Count first blockers and exclusive observed gate failures for M5 bars."""

    def __init__(self, enabled: bool = True, clock=None):
        self.enabled = bool(enabled)
        self._clock = clock or (lambda: datetime.now(timezone.utc))
        self._totals = Counter()
        self._blocked_by = Counter()
        self._blocked_only_by = Counter()
        self._recent_events = deque(maxlen=_MAX_RECENT_EVENTS)
        self._pending = None

    def begin(self, bar_time: object, timeframe: object) -> bool:
        if not self.enabled or str(timeframe or "").strip().upper() not in {
            "M5", "5M", "5MIN", "5MINUTE",
        }:
            return False
        self._pending = {
            "bar_time": str(bar_time),
            "candidate": None,
            "first_blocker": None,
            "failed_gates": set(),
            "passed": False,
        }
        return True

    def set_candidate(self, candidate: bool) -> None:
        if self._pending is None:
            return
        self._pending["candidate"] = bool(candidate)
        if not candidate:
            self.block("NO_CANDIDATE")

    def observe_decision(self, decision) -> None:
        if self._pending is None:
            return
        direction = str(getattr(decision, "direction", "") or "").upper()
        candidate = direction in {"BUY", "SELL"}
        self.set_candidate(candidate)
        if candidate and not bool(getattr(decision, "allowed", False)):
            reasons = getattr(decision, "blocked_reasons", None) or []
            codes = []
            for reason in reasons:
                code = classify_reason(reason)
                if code not in codes:
                    codes.append(code)
            for code in codes or ["ENTRY_FILTER"]:
                self.block(code)

    def block(self, code: str) -> None:
        if self._pending is None:
            return
        stable_code = str(code or "ENTRY_GUARD").strip().upper() or "ENTRY_GUARD"
        self._pending["failed_gates"].add(stable_code)
        if self._pending["first_blocker"] is None:
            self._pending["first_blocker"] = stable_code

    def would_block(self, code: str) -> None:
        """Record a shadow-mode gate failure without changing the first blocker."""
        if self._pending is None:
            return
        stable_code = str(code or "ENTRY_GUARD").strip().upper() or "ENTRY_GUARD"
        self._pending["failed_gates"].add(stable_code)

    def mark_passed(self) -> None:
        if self._pending is not None:
            self._pending["passed"] = True

    def _prune(self, now: datetime) -> None:
        cutoff = now - _WINDOW
        while self._recent_events:
            event_time = _parse_time(self._recent_events[0].get("observed_at"))
            if event_time is not None and event_time >= cutoff:
                break
            self._recent_events.popleft()

    def finish(self, fallback_code: str | None = None, passed: bool = False):
        if self._pending is None:
            return None
        pending = self._pending
        self._pending = None
        candidate = pending["candidate"]
        if candidate is None:
            candidate = False
        blocker = pending["first_blocker"]
        if blocker is None and fallback_code:
            blocker = str(fallback_code)
            pending["failed_gates"].add(blocker)
        if blocker is None and not (passed or pending["passed"]):
            blocker = "NO_CANDIDATE" if not candidate else "ENTRY_GUARD"
            pending["failed_gates"].add(blocker)

        now = self._clock().astimezone(timezone.utc)
        self._totals["bars"] += 1
        if candidate:
            self._totals["candidates"] += 1
        is_passed = bool(candidate and blocker is None and (passed or pending["passed"]))
        if is_passed:
            self._totals["passed"] += 1
        else:
            blocker = blocker or "NO_CANDIDATE"
            self._blocked_by[blocker] += 1
        if candidate and len(pending["failed_gates"]) == 1:
            self._blocked_only_by[next(iter(pending["failed_gates"]))] += 1

        self._recent_events.append({
            "observed_at": now.isoformat(),
            "bar_time": pending["bar_time"],
            "candidate": bool(candidate),
            "blocked_by": None if is_passed else blocker,
            "passed": is_passed,
            "failed_gates": sorted(pending["failed_gates"]),
        })
        self._prune(now)
        if self._totals["bars"] % 50 == 0:
            return (
                f"FUNNEL bars={self._totals['bars']} "
                f"candidates={self._totals['candidates']} "
                f"blocked_by={_ordered(self._blocked_by)} "
                f"passed={self._totals['passed']}"
            )
        return None

    def restore(self, snapshot: object) -> None:
        if not isinstance(snapshot, dict):
            return
        for key in ("bars", "candidates", "passed"):
            try:
                self._totals[key] = max(0, int(snapshot.get(key, 0)))
            except (TypeError, ValueError):
                self._totals[key] = 0
        for source, target in (
            ("blocked_by", self._blocked_by),
            ("blocked_only_by", self._blocked_only_by),
        ):
            values = snapshot.get(source, {})
            if isinstance(values, dict):
                for key, value in values.items():
                    try:
                        target[str(key)] = max(0, int(value))
                    except (TypeError, ValueError):
                        continue
        events = snapshot.get("recent_events", [])
        if isinstance(events, list):
            self._recent_events.clear()
            for event in events[-_MAX_RECENT_EVENTS:]:
                if isinstance(event, dict) and _parse_time(event.get("observed_at")):
                    self._recent_events.append(dict(event))
            self._prune(self._clock().astimezone(timezone.utc))

    def snapshot(self) -> dict:
        now = self._clock().astimezone(timezone.utc)
        self._prune(now)
        recent = list(self._recent_events)
        rolling_blocked = Counter(
            event["blocked_by"] for event in recent if event.get("blocked_by")
        )
        rolling_only = Counter(
            event["failed_gates"][0]
            for event in recent
            if event.get("candidate")
            and len(event.get("failed_gates", [])) == 1
        )
        rolling = {
            "bars": len(recent),
            "candidates": sum(bool(event.get("candidate")) for event in recent),
            "blocked_by": _ordered(rolling_blocked),
            "blocked_only_by": _ordered(rolling_only),
            "passed": sum(bool(event.get("passed")) for event in recent),
        }
        return {
            "enabled": self.enabled,
            "bars": self._totals["bars"],
            "candidates": self._totals["candidates"],
            "blocked_by": _ordered(self._blocked_by),
            "blocked_only_by": _ordered(self._blocked_only_by),
            "gate_only_basis": (
                "Only observed failures are counted; later short-circuited gates "
                "are not evaluated."
            ),
            "passed": self._totals["passed"],
            "rolling_24h": rolling,
            "recent_events": recent,
            "updated_at": recent[-1]["observed_at"] if recent else None,
        }

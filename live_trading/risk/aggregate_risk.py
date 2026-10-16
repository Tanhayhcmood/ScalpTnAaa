"""Account-currency risk sizing for all open positions on one symbol.

The broker's tick value is preferred because MT5 reports it in the account's
deposit currency.  A contract-size fallback is allowed only when the broker
also reports matching profit and account currencies; otherwise sizing fails
closed rather than guessing a conversion rate.
"""

from dataclasses import dataclass
import math
from typing import Iterable, Mapping, Optional


class RiskCalculationError(ValueError):
    """Broker, account, or position data is insufficient for safe sizing."""


class AggregateRiskLimitError(RiskCalculationError):
    """Existing and proposed stop risk cannot fit under the configured cap."""


class OpenPositionRiskError(RiskCalculationError):
    """At least one open ticket cannot be safely included in exposure."""


@dataclass(frozen=True)
class SymbolRiskSpec:
    tick_size: float
    tick_value_per_lot: float
    account_currency: str
    volume_min: float
    volume_step: float
    volume_max: float
    contract_size: Optional[float] = None
    tick_value_source: str = "broker"


@dataclass(frozen=True)
class AggregateRiskSizing:
    lot_size: float
    existing_risk: float
    new_risk: float
    aggregate_risk: float
    aggregate_limit: float
    trade_limit: float
    risk_budget: float
    position_count: int


def _positive_number(mappings: Iterable[object], *keys: str) -> Optional[float]:
    for mapping in mappings:
        if not isinstance(mapping, Mapping):
            continue
        for key in keys:
            value = mapping.get(key)
            if isinstance(value, bool):
                continue
            try:
                number = float(value)
            except (TypeError, ValueError):
                continue
            if math.isfinite(number) and number > 0:
                return number
    return None


def _currency(mappings: Iterable[object], *keys: str) -> str:
    for mapping in mappings:
        if not isinstance(mapping, Mapping):
            continue
        for key in keys:
            value = mapping.get(key)
            if value is not None and str(value).strip():
                return str(value).strip().upper()
    return ""


def _volume_constraint(
    mappings: Iterable[object],
    default: float,
    *keys: str,
) -> float:
    for mapping in mappings:
        if not isinstance(mapping, Mapping):
            continue
        for key in keys:
            if key not in mapping or mapping[key] is None or mapping[key] == "":
                continue
            value = mapping[key]
            if isinstance(value, bool):
                raise RiskCalculationError(f"Broker {key} is invalid")
            try:
                number = float(value)
            except (TypeError, ValueError) as exc:
                raise RiskCalculationError(f"Broker {key} is invalid") from exc
            if not math.isfinite(number) or number <= 0:
                raise RiskCalculationError(f"Broker {key} must be finite and positive")
            return number
    return default


def build_symbol_risk_spec(
    symbol_params: Mapping,
    account_currency: str,
) -> SymbolRiskSpec:
    """Normalize live broker symbol metadata without inventing FX rates."""
    if not isinstance(symbol_params, Mapping):
        raise RiskCalculationError("Broker symbol metadata is unavailable")
    account_ccy = str(account_currency or "").strip().upper()
    if not account_ccy:
        raise RiskCalculationError("Account currency is unavailable")

    info = symbol_params.get("symbolInfo")
    group = symbol_params.get("symbolGroup")
    mappings = (info, symbol_params, group)
    tick_size = _positive_number(
        mappings, "tickSize", "tick_size", "tradeTickSize", "trade_tick_size"
    )
    if tick_size is None:
        raise RiskCalculationError("Broker tick size is unavailable")

    tick_value = _positive_number(
        mappings,
        "tradeTickValueLoss",
        "trade_tick_value_loss",
        "tickValueLoss",
        "tick_value_loss",
        "tradeTickValue",
        "trade_tick_value",
        "tickValue",
        "tick_value",
    )
    tick_value_currency = _currency(
        mappings,
        "tickValueCurrency",
        "tick_value_currency",
        "tradeTickValueCurrency",
        "trade_tick_value_currency",
    )
    contract_size = _positive_number(
        mappings, "contractSize", "contract_size", "tradeContractSize"
    )
    source = "broker_tick_value"
    if tick_value is not None:
        if tick_value_currency and tick_value_currency != account_ccy:
            raise RiskCalculationError(
                "Broker tick value currency does not match account currency"
            )
    else:
        profit_currency = _currency(
            mappings,
            "currencyProfit",
            "currency_profit",
            "profitCurrency",
            "profit_currency",
        )
        if (
            contract_size is None
            or not profit_currency
            or profit_currency != account_ccy
        ):
            raise RiskCalculationError(
                "Broker tick value is unavailable and contract currency "
                "cannot be matched to the account currency"
            )
        # One tick moves one contract by tick_size in its profit currency.
        tick_value = tick_size * contract_size
        source = "contract_size_same_currency"

    volume_min = _volume_constraint(
        mappings, 0.01, "volumeMin", "volume_min", "minVolume", "min_volume"
    )
    volume_step = _volume_constraint(
        mappings,
        0.01,
        "volumeStep",
        "volume_step",
        "volumeIncrement",
        "volume_increment",
    )
    volume_max = _volume_constraint(
        mappings, 50.0, "volumeMax", "volume_max", "maxVolume", "max_volume"
    )
    if volume_step <= 0 or volume_max < volume_min:
        raise RiskCalculationError("Broker volume constraints are invalid")

    return SymbolRiskSpec(
        tick_size=tick_size,
        tick_value_per_lot=tick_value,
        account_currency=account_ccy,
        volume_min=volume_min,
        volume_step=volume_step,
        volume_max=volume_max,
        contract_size=contract_size,
        tick_value_source=source,
    )


def _number(value: object, field: str) -> float:
    if isinstance(value, bool):
        raise RiskCalculationError(f"{field} is invalid")
    try:
        number = float(value)
    except (TypeError, ValueError) as exc:
        raise RiskCalculationError(f"{field} is invalid") from exc
    if not math.isfinite(number) or number <= 0:
        raise RiskCalculationError(f"{field} must be finite and positive")
    return number


def risk_per_lot(
    direction: str,
    entry_price: object,
    stop_loss: object,
    spec: SymbolRiskSpec,
) -> float:
    """Calculate loss-to-stop per lot using the broker's live tick economics."""
    entry = _number(entry_price, "Entry price")
    stop = _number(stop_loss, "Stop loss")
    normalized = str(direction or "").upper().strip()
    if normalized == "BUY":
        distance = entry - stop
    elif normalized == "SELL":
        distance = stop - entry
    else:
        raise RiskCalculationError("Position direction is unknown")
    if not math.isfinite(distance) or distance <= 0:
        raise RiskCalculationError("New order SL is not on the loss-protection side")
    return (distance / spec.tick_size) * spec.tick_value_per_lot


def _position_id(position: Mapping) -> str:
    value = position.get("id", position.get("ticket"))
    if value is None or not str(value).strip():
        raise OpenPositionRiskError("Open position ticket is unavailable")
    return str(value).strip()


def position_risk_amount(position: Mapping, spec: SymbolRiskSpec) -> float:
    """Return one open ticket's current loss-to-SL risk in account currency."""
    ticket = _position_id(position)
    direction = str(position.get("type", position.get("direction", ""))).upper().strip()
    if direction not in {"BUY", "SELL"}:
        raise OpenPositionRiskError(
            f"Open position {ticket} direction is unknown"
        )
    try:
        entry = _number(
            position.get(
                "open_price",
                position.get("openPrice", position.get("price_open")),
            ),
            f"Open position {ticket} entry",
        )
        stop = _number(
            position.get("sl", position.get("stopLoss", position.get("stop_loss"))),
            f"Open position {ticket} SL",
        )
        volume = _number(
            position.get("volume", position.get("lots")),
            f"Open position {ticket} volume",
        )
    except RiskCalculationError as exc:
        raise OpenPositionRiskError(str(exc)) from exc
    signed_distance = entry - stop if direction == "BUY" else stop - entry
    # Break-even and profit-locked stops have zero loss-to-stop exposure.
    loss_distance = max(0.0, signed_distance)
    return (loss_distance / spec.tick_size) * spec.tick_value_per_lot * volume


def aggregate_open_risk(
    positions: Iterable[Mapping],
    spec: SymbolRiskSpec,
) -> tuple[float, int]:
    """Sum each ticket once; duplicate broker rows use the worst reported risk."""
    risk_by_ticket: dict[str, float] = {}
    for position in positions:
        if not isinstance(position, Mapping):
            raise RiskCalculationError("Open position data is malformed")
        ticket = _position_id(position)
        amount = position_risk_amount(position, spec)
        risk_by_ticket[ticket] = max(risk_by_ticket.get(ticket, 0.0), amount)
    return sum(risk_by_ticket.values()), len(risk_by_ticket)


def size_order_for_aggregate_risk(
    *,
    positions: Iterable[Mapping],
    direction: str,
    entry_price: object,
    stop_loss: object,
    account_balance: object,
    per_trade_risk_percent: object,
    aggregate_risk_percent: object,
    spec: SymbolRiskSpec,
) -> AggregateRiskSizing:
    """Size one new order to both per-trade and aggregate stop-risk budgets."""
    balance = _number(account_balance, "Account balance")
    trade_percent = _number(per_trade_risk_percent, "Per-trade risk percent")
    aggregate_percent = _number(
        aggregate_risk_percent, "Aggregate risk percent"
    )
    existing_risk, count = aggregate_open_risk(positions, spec)
    aggregate_limit = balance * aggregate_percent / 100.0
    trade_limit = balance * trade_percent / 100.0
    remaining_aggregate = aggregate_limit - existing_risk
    risk_budget = min(trade_limit, remaining_aggregate)
    if risk_budget <= 0:
        raise AggregateRiskLimitError(
            f"Open risk {existing_risk:.2f} {spec.account_currency} already "
            f"meets/exceeds the {aggregate_limit:.2f} {spec.account_currency} "
            "aggregate limit"
        )

    per_lot_risk = risk_per_lot(direction, entry_price, stop_loss, spec)
    raw_volume = min(risk_budget / per_lot_risk, spec.volume_max)
    if raw_volume + 1e-12 < spec.volume_min:
        raise AggregateRiskLimitError(
            f"Remaining risk budget {risk_budget:.2f} {spec.account_currency} "
            "is below the minimum-lot risk"
        )

    step_count = math.floor(
        (raw_volume - spec.volume_min) / spec.volume_step + 1e-9
    )
    volume = round(spec.volume_min + step_count * spec.volume_step, 8)
    if volume < spec.volume_min or volume > spec.volume_max:
        raise AggregateRiskLimitError("Risk-sized volume violates broker limits")
    new_risk = per_lot_risk * volume
    if existing_risk + new_risk > aggregate_limit + 1e-8:
        raise AggregateRiskLimitError(
            "Broker volume step would exceed the aggregate risk limit"
        )

    return AggregateRiskSizing(
        lot_size=volume,
        existing_risk=round(existing_risk, 8),
        new_risk=round(new_risk, 8),
        aggregate_risk=round(existing_risk + new_risk, 8),
        aggregate_limit=round(aggregate_limit, 8),
        trade_limit=round(trade_limit, 8),
        risk_budget=round(risk_budget, 8),
        position_count=count,
    )

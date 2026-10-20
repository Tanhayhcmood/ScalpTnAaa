"""Account-currency risk sizing for all open positions on one symbol.

The broker's tick value is preferred because MT5 reports it in the account's
deposit currency.  A contract-size fallback is allowed only when the broker
also reports matching profit and account currencies; otherwise sizing fails
closed rather than guessing a conversion rate.
"""

from dataclasses import dataclass
import json
import math
from typing import Iterable, Mapping, Optional

from live_trading.logger import get_logger


log = get_logger()


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
    tick_size_source: str = "broker"
    volume_min_source: str = "unknown"
    volume_step_source: str = "unknown"
    volume_max_source: str = "unknown"
    symbol: str = ""
    raw_symbol_fields: Optional[Mapping[str, Mapping[str, object]]] = None


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
) -> tuple[float, str]:
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
            return number, key
    return default, "default"


_RAW_SYMBOL_FIELD_NAMES = {
    "ticksize", "tradeticksize", "minpriceincrement", "pricestep",
    "tickvalue", "tradetickvalue",
    "tickvalueloss", "tradetickvalueloss", "tickvaluecurrency",
    "tradetickvaluecurrency", "contractsize", "tradecontractsize",
    "currencyprofit", "profitcurrency", "volumemin", "minvolume",
    "volumestep", "volumeincrement", "volumemax", "maxvolume",
    "digits", "precision", "pricedigits", "decimalplaces", "points",
    "point", "pointsize", "pricepoint",
}


def _raw_symbol_field_snapshot(
    symbol_params: Mapping,
) -> dict[str, dict[str, object]]:
    """Keep only sizing-relevant raw broker keys and their original values."""
    supplied = symbol_params.get("_raw_symbol_fields")
    if isinstance(supplied, Mapping):
        sources = supplied
    else:
        sources = {
            "root": symbol_params,
            "symbolInfo": symbol_params.get("symbolInfo"),
            "symbolGroup": symbol_params.get("symbolGroup"),
        }

    snapshot: dict[str, dict[str, object]] = {}
    for section, fields in sources.items():
        if not isinstance(fields, Mapping):
            continue
        selected = {
            str(key): value
            for key, value in fields.items()
            if str(key).replace("_", "").casefold() in _RAW_SYMBOL_FIELD_NAMES
        }
        if selected:
            snapshot[str(section)] = selected
    return snapshot


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
    symbol = str(symbol_params.get("symbol") or "").strip()
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
            or (profit_currency and profit_currency != account_ccy)
            or (
                not profit_currency
                and not (
                    account_ccy == "USD"
                    and symbol.upper().startswith("XAUUSD")
                )
            )
        ):
            raise RiskCalculationError(
                "Broker tick value is unavailable and contract currency "
                "cannot be matched to the account currency"
            )
        # One tick moves one contract by tick_size in its profit currency.
        tick_value = tick_size * contract_size
        source = (
            "contract_size_xauusd_usd_assumed"
            if not profit_currency
            else "contract_size_same_currency"
        )

    volume_min, volume_min_source = _volume_constraint(
        mappings, 0.01, "volumeMin", "volume_min", "minVolume", "min_volume"
    )
    volume_step, volume_step_source = _volume_constraint(
        mappings,
        0.01,
        "volumeStep",
        "volume_step",
        "volumeIncrement",
        "volume_increment",
    )
    volume_max, volume_max_source = _volume_constraint(
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
        tick_size_source=str(
            symbol_params.get("_tick_size_source")
            or ("broker" if tick_size is not None else "unknown")
        ),
        volume_min_source=volume_min_source,
        volume_step_source=volume_step_source,
        volume_max_source=volume_max_source,
        symbol=symbol,
        raw_symbol_fields=_raw_symbol_field_snapshot(symbol_params),
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
    account_equity: Optional[object] = None,
) -> AggregateRiskSizing:
    """Size one new order and emit one diagnostic for every guard evaluation."""
    balance: Optional[float] = None
    trade_percent: Optional[float] = None
    aggregate_percent: Optional[float] = None
    existing_risk: Optional[float] = None
    aggregate_limit: Optional[float] = None
    trade_limit: Optional[float] = None
    risk_budget: Optional[float] = None
    sl_distance: Optional[float] = None
    per_lot_risk: Optional[float] = None
    minimum_lot_risk: Optional[float] = None
    status = "ERROR"

    def emit_guard_log() -> None:
        equity = account_equity if account_equity is not None else balance
        try:
            equity_value = float(equity) if equity is not None else None
        except (TypeError, ValueError):
            equity_value = None
        price_value = (
            spec.tick_value_per_lot / spec.tick_size
            if spec.tick_size > 0
            else None
        )
        raw_fields = json.dumps(
            spec.raw_symbol_fields or {},
            separators=(",", ":"),
            sort_keys=True,
            default=str,
        )
        log.info(
            "PRE_ORDER_RISK_GUARD status=%s symbol=%s equity=%s "
            "budget_balance=%s risk_percent=%s total_budget=%s "
            "aggregate_budget=%s used_risk=%s remaining_budget=%s "
            "sl_distance_price=%s volume_min=%s volume_min_source=%s "
            "volume_step=%s volume_step_source=%s volume_max=%s "
            "volume_max_source=%s raw_mtapi_fields=%s contract_size=%s "
            "tick_size=%s tick_size_source=%s tick_value_per_lot=%s "
            "tick_value_source=%s value_per_price_unit=%s "
            "formula=abs(entry-stop)/tick_size*tick_value_per_lot*lots "
            "minimum_lot_risk=%s %s",
            status,
            spec.symbol or "unknown",
            f"{equity_value:.2f}" if equity_value is not None else "n/a",
            f"{balance:.2f}" if balance is not None else "n/a",
            f"{trade_percent:.4f}" if trade_percent is not None else "n/a",
            f"{trade_limit:.2f}" if trade_limit is not None else "n/a",
            f"{aggregate_limit:.2f}" if aggregate_limit is not None else "n/a",
            f"{existing_risk:.2f}" if existing_risk is not None else "n/a",
            f"{risk_budget:.2f}" if risk_budget is not None else "n/a",
            f"{sl_distance:.6f}" if sl_distance is not None else "n/a",
            f"{spec.volume_min:g}",
            spec.volume_min_source,
            f"{spec.volume_step:g}",
            spec.volume_step_source,
            f"{spec.volume_max:g}",
            spec.volume_max_source,
            raw_fields,
            f"{spec.contract_size:g}" if spec.contract_size is not None else "n/a",
            f"{spec.tick_size:g}",
            spec.tick_size_source,
            f"{spec.tick_value_per_lot:g}",
            spec.tick_value_source,
            f"{price_value:g}" if price_value is not None else "n/a",
            f"{minimum_lot_risk:.2f}" if minimum_lot_risk is not None else "n/a",
            spec.account_currency,
        )

    try:
        balance = _number(account_balance, "Account balance")
        trade_percent = _number(
            per_trade_risk_percent, "Per-trade risk percent"
        )
        aggregate_percent = _number(
            aggregate_risk_percent, "Aggregate risk percent"
        )
        existing_risk, count = aggregate_open_risk(positions, spec)
        aggregate_limit = balance * aggregate_percent / 100.0
        trade_limit = balance * trade_percent / 100.0
        remaining_aggregate = aggregate_limit - existing_risk
        risk_budget = min(trade_limit, remaining_aggregate)

        if risk_budget <= 0:
            status = "BLOCKED_NO_REMAINING_BUDGET"
            raise AggregateRiskLimitError(
                f"Open risk {existing_risk:.2f} {spec.account_currency} already "
                f"meets/exceeds the {aggregate_limit:.2f} {spec.account_currency} "
                "aggregate limit"
            )

        sl_distance = abs(float(entry_price) - float(stop_loss))
        per_lot_risk = risk_per_lot(direction, entry_price, stop_loss, spec)
        minimum_lot_risk = per_lot_risk * spec.volume_min
        raw_volume = min(risk_budget / per_lot_risk, spec.volume_max)
        if raw_volume + 1e-12 < spec.volume_min:
            status = "BLOCKED_MINIMUM_LOT"
            raise AggregateRiskLimitError(
                f"Remaining risk budget {risk_budget:.2f} {spec.account_currency} "
                "is below the minimum-lot risk"
            )

        step_count = math.floor(
            (raw_volume - spec.volume_min) / spec.volume_step + 1e-9
        )
        volume = round(spec.volume_min + step_count * spec.volume_step, 8)
        if volume < spec.volume_min or volume > spec.volume_max:
            status = "BLOCKED_VOLUME_CONSTRAINT"
            raise AggregateRiskLimitError(
                "Risk-sized volume violates broker limits"
            )
        new_risk = per_lot_risk * volume
        if existing_risk + new_risk > aggregate_limit + 1e-8:
            status = "BLOCKED_AGGREGATE_BUDGET"
            raise AggregateRiskLimitError(
                "Broker volume step would exceed the aggregate risk limit"
            )

        status = "ALLOWED"
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
    finally:
        emit_guard_log()

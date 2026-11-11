"""Account-currency risk sizing for all open positions on one symbol.

The broker's tick value is preferred because MT5 reports it in the account's
deposit currency.  A contract-size fallback is allowed only when the broker
also reports matching profit and account currencies; otherwise sizing fails
closed rather than guessing a conversion rate.
"""

from dataclasses import dataclass
from decimal import Decimal, ROUND_FLOOR
import json
import math
import os
from typing import Iterable, Mapping, Optional

from live_trading.logger import get_logger
from live_trading.mt5.volume_units import (
    configured_mtapi_volume_scale,
    extended_volume_to_lots,
    volume_constraint_sanity_error,
)
from live_trading.risk.capital_manager import LOT_DOLLAR_PER_UNIT
from live_trading.utils import canonical_symbol


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
    contract_size_confirmed: bool = False
    tick_value_source: str = "broker"
    tick_size_source: str = "broker"
    volume_min_source: str = "unknown"
    volume_step_source: str = "unknown"
    volume_max_source: str = "unknown"
    symbol: str = ""
    raw_symbol_fields: Optional[Mapping[str, Mapping[str, object]]] = None
    volume_scale: float = 1.0
    volume_min_raw: Optional[object] = None
    volume_step_raw: Optional[object] = None
    volume_max_raw: Optional[object] = None

    @property
    def value_per_point_per_lot(self) -> float:
        return self.tick_value_per_lot / self.tick_size


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
    *,
    field_name: str,
    aliases: tuple[str, ...],
    scale: float,
    values_are_lots: bool,
    raw_symbol_fields: Optional[Mapping[str, Mapping[str, object]]] = None,
) -> tuple[float, str, object]:
    def raw_value_for_field() -> Optional[object]:
        if not isinstance(raw_symbol_fields, Mapping):
            return None
        for section in ("symbolInfo", "root", "symbolGroup"):
            source = raw_symbol_fields.get(section)
            if not isinstance(source, Mapping):
                continue
            for alias in aliases:
                if alias in source and source[alias] is not None:
                    return source[alias]
        return None

    for mapping in mappings:
        if not isinstance(mapping, Mapping):
            continue
        for key in aliases:
            if key not in mapping or mapping[key] is None or mapping[key] == "":
                continue
            value = mapping[key]
            if isinstance(value, bool):
                log.error(
                    "MTAPI_VOLUME_SANITY_ERROR field=%s raw=%r scale=%g "
                    "lots=n/a reason=boolean volume value",
                    field_name,
                    value,
                    scale,
                )
                raise RiskCalculationError(f"Broker {field_name} is invalid")
            try:
                raw_number = float(value)
            except (TypeError, ValueError) as exc:
                log.error(
                    "MTAPI_VOLUME_SANITY_ERROR field=%s raw=%r scale=%g "
                    "lots=n/a reason=volume value is invalid",
                    field_name,
                    value,
                    scale,
                )
                raise RiskCalculationError(
                    f"Broker {field_name} is invalid"
                ) from exc
            if not math.isfinite(raw_number):
                log.error(
                    "MTAPI_VOLUME_SANITY_ERROR field=%s raw=%r scale=%g "
                    "lots=n/a reason=raw volume value is not finite",
                    field_name,
                    value,
                    scale,
                )
                raise RiskCalculationError(
                    f"Broker {field_name} must be finite"
                )

            if values_are_lots:
                lots = raw_number
                original_value = raw_value_for_field()
                if original_value is None:
                    original_value = value
                    log_scale = 1.0
                else:
                    log_scale = scale
            else:
                original_value = value
                log_scale = scale
                try:
                    lots = extended_volume_to_lots(value, scale)
                except ValueError as exc:
                    log.error(
                        "MTAPI_VOLUME_SANITY_ERROR field=%s raw=%r scale=%g "
                        "lots=n/a reason=%s",
                        field_name,
                        value,
                        scale,
                        exc,
                    )
                    raise RiskCalculationError(
                        f"Broker {field_name} is invalid: {exc}"
                    ) from exc

            log.info(
                "MTAPI_VOLUME_CONVERSION field=%s raw=%s scale=%s lots=%s",
                field_name,
                original_value,
                f"{log_scale:g}",
                f"{lots:g}",
            )
            if not math.isfinite(lots) or lots <= 0:
                reason = (
                    f"converted {field_name} must be finite and positive "
                    f"(raw={original_value!r}, scale={log_scale:g}, lots={lots:g})"
                )
                log.error("MTAPI_VOLUME_SANITY_ERROR %s", reason)
                raise RiskCalculationError(reason)
            return lots, key, original_value

    reason = (
        f"Broker {field_name} is unavailable; refusing to guess a volume constraint"
    )
    log.error("MTAPI_VOLUME_SANITY_ERROR field=%s reason=%s", field_name, reason)
    raise RiskCalculationError(reason)


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
    volume_units_error = symbol_params.get("_mtapi_volume_units_error")
    if volume_units_error:
        reason = str(volume_units_error)
        log.error(
            "MTAPI_VOLUME_SANITY_ERROR symbol=%s reason=%s",
            symbol_params.get("symbol", "unknown"),
            reason,
        )
        raise RiskCalculationError(
            f"MTAPI volume metadata is invalid: {reason}"
        )

    volume_units = symbol_params.get("_mtapi_volume_units")
    values_are_lots = volume_units == "lots"
    if values_are_lots:
        scale_value = symbol_params.get("_mtapi_volume_scale")
        try:
            volume_scale = float(scale_value)
        except (TypeError, ValueError) as exc:
            reason = "normalized MTAPI volume fields are missing their scale"
            log.error("MTAPI_VOLUME_SANITY_ERROR symbol=%s reason=%s", symbol_params.get("symbol", "unknown"), reason)
            raise RiskCalculationError(reason) from exc
        if not math.isfinite(volume_scale) or volume_scale <= 0:
            reason = "normalized MTAPI volume scale must be finite and positive"
            log.error(
                "MTAPI_VOLUME_SANITY_ERROR symbol=%s scale=%r reason=%s",
                symbol_params.get("symbol", "unknown"),
                scale_value,
                reason,
            )
            raise RiskCalculationError(reason)
    elif volume_units in (None, "extended"):
        try:
            volume_scale = configured_mtapi_volume_scale()
        except ValueError as exc:
            reason = str(exc)
            log.error(
                "MTAPI_VOLUME_SANITY_ERROR symbol=%s reason=%s",
                symbol_params.get("symbol", "unknown"),
                reason,
            )
            raise RiskCalculationError(reason) from exc
    else:
        reason = f"unknown MTAPI volume units marker: {volume_units!r}"
        log.error(
            "MTAPI_VOLUME_SANITY_ERROR symbol=%s reason=%s",
            symbol_params.get("symbol", "unknown"),
            reason,
        )
        raise RiskCalculationError(reason)

    info = symbol_params.get("symbolInfo")
    group = symbol_params.get("symbolGroup")
    mappings = (info, symbol_params, group)
    raw_symbol_fields = _raw_symbol_field_snapshot(symbol_params)
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
    profit_currency = _currency(
        mappings,
        "currencyProfit",
        "currency_profit",
        "profitCurrency",
        "profit_currency",
    )
    contract_size_confirmed = contract_size is not None and (
        profit_currency == account_ccy
        or (
            not profit_currency
            and account_ccy == "USD"
            and canonical_symbol(symbol) == "XAUUSD"
        )
    )
    if tick_value is not None and tick_value_currency and tick_value_currency != account_ccy:
        raise RiskCalculationError(
            "Broker tick value currency does not match account currency"
        )

    broker_value_per_point = (
        tick_value / tick_size if tick_value is not None else None
    )
    contract_value_per_point = (
        contract_size if contract_size_confirmed else None
    )
    if broker_value_per_point is not None and contract_value_per_point is not None:
        mismatch_ratio = abs(
            broker_value_per_point - contract_value_per_point
        ) / contract_value_per_point
        value_per_point = max(
            broker_value_per_point, contract_value_per_point
        )
        source = (
            "broker_tick_value"
            if broker_value_per_point >= contract_value_per_point
            else "contract_size"
        )
        if mismatch_ratio > 0.20:
            log.error(
                "TICK_VALUE_MISMATCH symbol=%s tick_value=%g tick_size=%g "
                "contract_size=%g broker_value_per_point_per_lot=%g "
                "contract_value_per_point_per_lot=%g "
                "selected_value_per_point_per_lot=%g",
                symbol or "unknown",
                tick_value,
                tick_size,
                contract_size,
                broker_value_per_point,
                contract_value_per_point,
                value_per_point,
            )
            source = f"{source}_after_mismatch"
    elif contract_value_per_point is not None:
        value_per_point = contract_value_per_point
        source = (
            "contract_size_xauusd_usd_assumed"
            if not profit_currency
            else "contract_size_same_currency"
        )
    elif broker_value_per_point is not None:
        value_per_point = broker_value_per_point
        source = "broker_tick_value"
    else:
        raise RiskCalculationError(
            "Broker tick value is unavailable and contract currency "
            "cannot be matched to the account currency"
        )

    if not contract_size_confirmed and value_per_point < LOT_DOLLAR_PER_UNIT:
        value_per_point = LOT_DOLLAR_PER_UNIT
        source = "lot_dollar_per_unit_floor"

    override_raw = os.getenv("TICK_VALUE_OVERRIDE_PER_POINT_PER_LOT")
    if override_raw is not None and override_raw.strip():
        try:
            override_value = float(override_raw)
        except (TypeError, ValueError) as exc:
            raise RiskCalculationError(
                "TICK_VALUE_OVERRIDE_PER_POINT_PER_LOT must be a positive number"
            ) from exc
        if not math.isfinite(override_value) or override_value <= 0:
            raise RiskCalculationError(
                "TICK_VALUE_OVERRIDE_PER_POINT_PER_LOT must be finite and positive"
            )
        if override_value >= value_per_point:
            value_per_point = override_value
            source = "env_override"
        else:
            log.warning(
                "TICK_VALUE_OVERRIDE_BELOW_SAFE_VALUE symbol=%s override=%g "
                "selected=%g; retaining the larger value",
                symbol or "unknown",
                override_value,
                value_per_point,
            )
    tick_value = value_per_point * tick_size

    volume_min, volume_min_source, volume_min_raw = _volume_constraint(
        mappings,
        field_name="volumeMin",
        aliases=("volumeMin", "volume_min", "minVolume", "min_volume"),
        scale=volume_scale,
        values_are_lots=values_are_lots,
        raw_symbol_fields=raw_symbol_fields,
    )
    volume_step, volume_step_source, volume_step_raw = _volume_constraint(
        mappings,
        field_name="volumeStep",
        aliases=(
            "volumeStep",
            "volume_step",
            "volumeIncrement",
            "volume_increment",
        ),
        scale=volume_scale,
        values_are_lots=values_are_lots,
        raw_symbol_fields=raw_symbol_fields,
    )
    volume_max, volume_max_source, volume_max_raw = _volume_constraint(
        mappings,
        field_name="volumeMax",
        aliases=("volumeMax", "volume_max", "maxVolume", "max_volume"),
        scale=volume_scale,
        values_are_lots=values_are_lots,
        raw_symbol_fields=raw_symbol_fields,
    )
    sanity_error = volume_constraint_sanity_error(
        volume_min, volume_step, volume_max
    )
    if sanity_error:
        log.error(
            "MTAPI_VOLUME_SANITY_ERROR symbol=%s min_lots=%g step_lots=%g "
            "max_lots=%g scale=%g reason=%s",
            symbol_params.get("symbol", "unknown"),
            volume_min,
            volume_step,
            volume_max,
            volume_scale,
            sanity_error,
        )
        raise RiskCalculationError(f"MTAPI volume sanity check failed: {sanity_error}")
    if volume_max < volume_min:
        reason = (
            f"converted maximum lot {volume_max:g} is below minimum lot "
            f"{volume_min:g}"
        )
        log.error(
            "MTAPI_VOLUME_SANITY_ERROR symbol=%s scale=%g reason=%s",
            symbol_params.get("symbol", "unknown"),
            volume_scale,
            reason,
        )
        raise RiskCalculationError(reason)

    spec = SymbolRiskSpec(
        tick_size=tick_size,
        tick_value_per_lot=tick_value,
        account_currency=account_ccy,
        volume_min=volume_min,
        volume_step=volume_step,
        volume_max=volume_max,
        contract_size=contract_size,
        contract_size_confirmed=contract_size_confirmed,
        tick_value_source=source,
        tick_size_source=str(
            symbol_params.get("_tick_size_source")
            or ("broker" if tick_size is not None else "unknown")
        ),
        volume_min_source=volume_min_source,
        volume_step_source=volume_step_source,
        volume_max_source=volume_max_source,
        symbol=symbol,
        raw_symbol_fields=raw_symbol_fields,
        volume_scale=volume_scale,
        volume_min_raw=volume_min_raw,
        volume_step_raw=volume_step_raw,
        volume_max_raw=volume_max_raw,
    )
    log.info(
        "SPEC symbol=%s contract_size=%s tick_size=%g tick_value=%g "
        "value_per_point_per_lot=%g source=%s",
        symbol or "unknown",
        f"{contract_size:g}" if contract_size is not None else "n/a",
        tick_size,
        tick_value,
        spec.value_per_point_per_lot,
        source,
    )
    return spec


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


def post_fill_risk_amount(position: Mapping, spec: SymbolRiskSpec) -> float:
    """Recalculate filled loss-to-SL risk from the broker's open position."""
    if not isinstance(position, Mapping):
        raise RiskCalculationError("Filled position data is malformed")
    entry = _positive_number(
        (position,), "openPrice", "open_price", "price_open", "priceOpen"
    )
    stop = _positive_number(
        (position,), "stopLoss", "stop_loss", "sl"
    )
    volume = _positive_number((position,), "lots", "volume")
    if entry is None or stop is None or volume is None:
        raise RiskCalculationError(
            "Filled position entry, stop loss, or volume is unavailable"
        )
    point_value = (
        spec.contract_size
        if spec.contract_size_confirmed and spec.contract_size is not None
        else spec.value_per_point_per_lot
    )
    risk = abs(entry - stop) * volume * point_value
    if not math.isfinite(risk):
        raise RiskCalculationError("Filled position risk is not finite")
    return risk


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

    sized_lots: Optional[float] = None
    sized_risk: Optional[float] = None

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
            "sl_distance_price=%s volume_min=%s volume_min_raw=%s "
            "volume_min_source=%s volume_step=%s volume_step_raw=%s "
            "volume_step_source=%s volume_max=%s volume_max_raw=%s "
            "volume_max_source=%s volume_scale=%s raw_mtapi_fields=%s contract_size=%s "
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
            spec.volume_min_raw,
            spec.volume_min_source,
            f"{spec.volume_step:g}",
            spec.volume_step_raw,
            spec.volume_step_source,
            f"{spec.volume_max:g}",
            spec.volume_max_raw,
            spec.volume_max_source,
            f"{spec.volume_scale:g}",
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
        if raw_volume < spec.volume_min:
            status = "BLOCKED_MINIMUM_LOT"
            raise AggregateRiskLimitError(
                f"Remaining risk budget {risk_budget:.2f} {spec.account_currency} "
                "is below the minimum-lot risk"
            )

        minimum_decimal = Decimal(str(spec.volume_min))
        step_decimal = Decimal(str(spec.volume_step))
        raw_decimal = Decimal(str(raw_volume))
        step_count = int(
            ((raw_decimal - minimum_decimal) / step_decimal).to_integral_value(
                rounding=ROUND_FLOOR
            )
        )
        volume_decimal = minimum_decimal + step_count * step_decimal
        volume = float(volume_decimal)
        if volume < spec.volume_min or volume > spec.volume_max:
            status = "BLOCKED_VOLUME_CONSTRAINT"
            raise AggregateRiskLimitError(
                "Risk-sized volume violates broker limits"
            )
        new_risk = per_lot_risk * volume
        while new_risk > risk_budget and volume_decimal > minimum_decimal:
            volume_decimal -= step_decimal
            volume = float(volume_decimal)
            new_risk = per_lot_risk * volume
        if new_risk > risk_budget:
            status = "BLOCKED_MINIMUM_LOT"
            raise AggregateRiskLimitError(
                f"Remaining risk budget {risk_budget:.2f} {spec.account_currency} "
                "is below the minimum-lot risk"
            )
        if existing_risk + new_risk > aggregate_limit:
            status = "BLOCKED_AGGREGATE_BUDGET"
            raise AggregateRiskLimitError(
                "Broker volume step would exceed the aggregate risk limit"
            )

        status = "ALLOWED"
        sized_lots = volume
        sized_risk = new_risk
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
        if status == "ALLOWED" and sized_lots is not None and sized_risk is not None:
            log.info(
                "PRE_ORDER_RISK_GUARD_RESULT status=ALLOWED sized_lots=%.8f "
                "risk_amount=%.2f risk_currency=%s",
                sized_lots,
                sized_risk,
                spec.account_currency,
            )

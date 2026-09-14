"""
Risk Service — read and update risk management parameters.
"""

import logging
from typing import Optional
from ..models.risk_config import RiskConfig
from ..config.constants import RiskParameter
from ..storage.repositories.settings_repo import SettingsRepository
from ..services.robot_service import RobotService

logger = logging.getLogger(__name__)


class RiskService:
    def __init__(self, settings_repo: SettingsRepository, robot: RobotService) -> None:
        self._settings = settings_repo
        self._robot = robot

    async def get_config(self, account_id: Optional[int] = None) -> RiskConfig:
        return await self._settings.get_risk_config(account_id)

    async def update_parameter(
        self,
        parameter: RiskParameter,
        value: float | bool,
        account_id: Optional[int] = None,
    ) -> tuple[bool, str]:
        """
        Update a single risk parameter.
        Returns (success, message).
        Validates before saving.
        """
        config = await self._settings.get_risk_config(account_id)

        # Map parameter to field
        field_map = {
            RiskParameter.RISK_PERCENT: ("risk_percent", float),
            RiskParameter.LOT_SIZE: ("lot_size_override", float),
            RiskParameter.DAILY_LOSS: ("daily_loss_limit", float),
            RiskParameter.MAX_TRADES: ("max_concurrent_trades", int),
            RiskParameter.MAX_SPREAD: ("max_spread_pips", float),
            RiskParameter.MAX_DRAWDOWN: ("max_drawdown_percent", float),
            RiskParameter.RR_RATIO: ("rr_ratio", float),
            RiskParameter.STOP_LOSS: ("default_sl_pips", float),
            RiskParameter.TAKE_PROFIT: ("default_tp_pips", float),
            RiskParameter.AUTO_BE: ("auto_breakeven", bool),
            RiskParameter.AUTO_TRAIL: ("auto_trailing", bool),
        }

        if parameter not in field_map:
            return False, f"Unknown parameter: {parameter}"

        attr, cast_type = field_map[parameter]
        try:
            typed_value = cast_type(value)
        except (ValueError, TypeError) as e:
            return False, f"Invalid value: {e}"

        setattr(config, attr, typed_value)

        # Validate
        errors = config.validate()
        if errors:
            return False, "; ".join(errors)

        # Save to DB
        await self._settings.save_risk_config(config)

        # Push to robot engine (non-blocking, best-effort)
        try:
            import dataclasses
            await self._robot.push_risk_config(dataclasses.asdict(config))
        except Exception as e:
            logger.warning(f"Failed to push risk config to robot: {e}")

        return True, f"✅ {parameter.value} updated to {value}"

    async def save_config(self, config: RiskConfig) -> tuple[bool, str]:
        errors = config.validate()
        if errors:
            return False, "; ".join(errors)
        await self._settings.save_risk_config(config)
        try:
            import dataclasses
            await self._robot.push_risk_config(dataclasses.asdict(config))
        except Exception as e:
            logger.warning(f"Failed to push risk config: {e}")
        return True, "Risk configuration saved successfully"

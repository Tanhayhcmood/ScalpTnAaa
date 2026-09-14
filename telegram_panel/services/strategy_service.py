"""
Strategy Service — enable/disable SMC strategy components.
"""

import logging
from typing import Optional
from ..models.strategy_config import StrategyConfig
from ..config.constants import StrategyComponent
from ..storage.repositories.settings_repo import SettingsRepository
from ..services.robot_service import RobotService

logger = logging.getLogger(__name__)


class StrategyService:
    def __init__(self, settings_repo: SettingsRepository, robot: RobotService) -> None:
        self._settings = settings_repo
        self._robot = robot

    async def get_config(self, account_id: Optional[int] = None) -> StrategyConfig:
        return await self._settings.get_strategy_config(account_id)

    async def toggle_component(
        self,
        component: StrategyComponent,
        enabled: bool,
        account_id: Optional[int] = None,
    ) -> tuple[bool, str]:
        """
        Enable or disable a single strategy component.
        Changes take effect on next robot tick (no restart required).
        """
        config = await self._settings.get_strategy_config(account_id)
        config.set_component(component, enabled)
        await self._settings.save_strategy_config(config)

        # Push to robot
        try:
            import dataclasses
            await self._robot.push_strategy_config(dataclasses.asdict(config))
        except Exception as e:
            logger.warning(f"Failed to push strategy config: {e}")

        action = "enabled" if enabled else "disabled"
        return True, f"✅ {component.display_name} {action}"

    async def toggle_all(
        self, enabled: bool, account_id: Optional[int] = None
    ) -> tuple[bool, str]:
        config = await self._settings.get_strategy_config(account_id)
        for component in StrategyComponent:
            config.set_component(component, enabled)
        await self._settings.save_strategy_config(config)
        try:
            import dataclasses
            await self._robot.push_strategy_config(dataclasses.asdict(config))
        except Exception as e:
            logger.warning(f"Failed to push strategy config: {e}")
        action = "enabled" if enabled else "disabled"
        return True, f"✅ All strategy components {action}"

    async def save_config(self, config: StrategyConfig) -> bool:
        await self._settings.save_strategy_config(config)
        try:
            import dataclasses
            await self._robot.push_strategy_config(dataclasses.asdict(config))
        except Exception as e:
            logger.warning(f"Failed to push strategy config: {e}")
        return True

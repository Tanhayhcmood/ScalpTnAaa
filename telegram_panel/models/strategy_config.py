"""
Strategy configuration model — which SMC components are active.
"""

from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Optional
from ..config.constants import StrategyComponent


@dataclass
class StrategyConfig:
    """Enable/disable each strategy component independently."""
    id: Optional[int] = None
    account_id: Optional[int] = None

    # SMC components
    smc_enabled: bool = True
    bos_enabled: bool = True
    choch_enabled: bool = True
    order_blocks_enabled: bool = True
    liquidity_enabled: bool = True
    fvg_enabled: bool = True
    mitigation_enabled: bool = True

    # Filters
    sessions_enabled: bool = True
    trend_filter_enabled: bool = True
    volume_filter_enabled: bool = True
    news_filter_enabled: bool = True
    time_filter_enabled: bool = True
    spread_filter_enabled: bool = True

    # Thresholds
    min_confidence_score: float = 49.0
    min_rr_ratio: float = 2.0

    created_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))
    updated_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))

    def is_component_enabled(self, component: StrategyComponent) -> bool:
        mapping = {
            StrategyComponent.SMC: self.smc_enabled,
            StrategyComponent.BOS: self.bos_enabled,
            StrategyComponent.CHOCH: self.choch_enabled,
            StrategyComponent.ORDER_BLOCKS: self.order_blocks_enabled,
            StrategyComponent.LIQUIDITY: self.liquidity_enabled,
            StrategyComponent.FVG: self.fvg_enabled,
            StrategyComponent.MITIGATION: self.mitigation_enabled,
            StrategyComponent.SESSIONS: self.sessions_enabled,
            StrategyComponent.TREND_FILTER: self.trend_filter_enabled,
            StrategyComponent.VOLUME_FILTER: self.volume_filter_enabled,
            StrategyComponent.NEWS_FILTER: self.news_filter_enabled,
            StrategyComponent.TIME_FILTER: self.time_filter_enabled,
            StrategyComponent.SPREAD_FILTER: self.spread_filter_enabled,
        }
        return mapping.get(component, False)

    def set_component(self, component: StrategyComponent, enabled: bool) -> None:
        attr_map = {
            StrategyComponent.SMC: "smc_enabled",
            StrategyComponent.BOS: "bos_enabled",
            StrategyComponent.CHOCH: "choch_enabled",
            StrategyComponent.ORDER_BLOCKS: "order_blocks_enabled",
            StrategyComponent.LIQUIDITY: "liquidity_enabled",
            StrategyComponent.FVG: "fvg_enabled",
            StrategyComponent.MITIGATION: "mitigation_enabled",
            StrategyComponent.SESSIONS: "sessions_enabled",
            StrategyComponent.TREND_FILTER: "trend_filter_enabled",
            StrategyComponent.VOLUME_FILTER: "volume_filter_enabled",
            StrategyComponent.NEWS_FILTER: "news_filter_enabled",
            StrategyComponent.TIME_FILTER: "time_filter_enabled",
            StrategyComponent.SPREAD_FILTER: "spread_filter_enabled",
        }
        attr = attr_map.get(component)
        if attr:
            setattr(self, attr, enabled)
            self.updated_at = datetime.now(timezone.utc)

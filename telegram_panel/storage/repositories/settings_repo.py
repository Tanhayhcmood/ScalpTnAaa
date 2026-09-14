"""
Settings Repository — risk configs and strategy configs.
"""

import logging
from typing import Optional
from ..database import Database
from ...models.risk_config import RiskConfig
from ...models.strategy_config import StrategyConfig
from ...utils.time import utc_now

logger = logging.getLogger(__name__)


class SettingsRepository:
    def __init__(self, db: Database) -> None:
        self._db = db

    # ─── Risk Config ───────────────────────────────────────────────────────────

    async def get_risk_config(self, account_id: Optional[int] = None) -> RiskConfig:
        async with self._db.connection() as db:
            if account_id is not None:
                cursor = await db.execute(
                    "SELECT * FROM risk_configs WHERE account_id=? LIMIT 1", (account_id,)
                )
            else:
                cursor = await db.execute(
                    "SELECT * FROM risk_configs WHERE account_id IS NULL LIMIT 1"
                )
            row = await cursor.fetchone()
        if row:
            return self._row_to_risk(row)
        # Return defaults if not configured yet
        return RiskConfig(account_id=account_id)

    async def save_risk_config(self, config: RiskConfig) -> RiskConfig:
        existing = await self.get_risk_config(config.account_id)
        config.updated_at = utc_now()

        async with self._db.connection() as db:
            if existing.id:
                await db.execute(
                    """UPDATE risk_configs SET
                       risk_percent=?, lot_size_override=?, daily_loss_limit=?,
                       max_concurrent_trades=?, max_spread_pips=?, max_drawdown_percent=?,
                       rr_ratio=?, default_sl_pips=?, default_tp_pips=?,
                       auto_breakeven=?, be_trigger_pips=?, auto_trailing=?,
                       trail_distance_pips=?, trail_activation_pips=?, updated_at=?
                       WHERE id=?""",
                    (
                        config.risk_percent, config.lot_size_override,
                        config.daily_loss_limit, config.max_concurrent_trades,
                        config.max_spread_pips, config.max_drawdown_percent,
                        config.rr_ratio, config.default_sl_pips, config.default_tp_pips,
                        1 if config.auto_breakeven else 0, config.be_trigger_pips,
                        1 if config.auto_trailing else 0,
                        config.trail_distance_pips, config.trail_activation_pips,
                        config.updated_at.isoformat(), existing.id,
                    ),
                )
            else:
                cursor = await db.execute(
                    """INSERT INTO risk_configs
                       (account_id, risk_percent, lot_size_override, daily_loss_limit,
                        max_concurrent_trades, max_spread_pips, max_drawdown_percent,
                        rr_ratio, default_sl_pips, default_tp_pips,
                        auto_breakeven, be_trigger_pips, auto_trailing,
                        trail_distance_pips, trail_activation_pips)
                       VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                    (
                        config.account_id, config.risk_percent, config.lot_size_override,
                        config.daily_loss_limit, config.max_concurrent_trades,
                        config.max_spread_pips, config.max_drawdown_percent,
                        config.rr_ratio, config.default_sl_pips, config.default_tp_pips,
                        1 if config.auto_breakeven else 0, config.be_trigger_pips,
                        1 if config.auto_trailing else 0,
                        config.trail_distance_pips, config.trail_activation_pips,
                    ),
                )
                config.id = cursor.lastrowid
            await db.commit()
        return config

    # ─── Strategy Config ────────────────────────────────────────────────────────

    async def get_strategy_config(self, account_id: Optional[int] = None) -> StrategyConfig:
        async with self._db.connection() as db:
            if account_id is not None:
                cursor = await db.execute(
                    "SELECT * FROM strategy_configs WHERE account_id=? LIMIT 1",
                    (account_id,),
                )
            else:
                cursor = await db.execute(
                    "SELECT * FROM strategy_configs WHERE account_id IS NULL LIMIT 1"
                )
            row = await cursor.fetchone()
        if row:
            return self._row_to_strategy(row)
        return StrategyConfig(account_id=account_id)

    async def save_strategy_config(self, config: StrategyConfig) -> StrategyConfig:
        existing = await self.get_strategy_config(config.account_id)
        config.updated_at = utc_now()

        async with self._db.connection() as db:
            if existing.id:
                await db.execute(
                    """UPDATE strategy_configs SET
                       smc_enabled=?, bos_enabled=?, choch_enabled=?,
                       order_blocks_enabled=?, liquidity_enabled=?, fvg_enabled=?,
                       mitigation_enabled=?, sessions_enabled=?, trend_filter_enabled=?,
                       volume_filter_enabled=?, news_filter_enabled=?, time_filter_enabled=?,
                       spread_filter_enabled=?, min_confidence_score=?, min_rr_ratio=?,
                       updated_at=?
                       WHERE id=?""",
                    (
                        1 if config.smc_enabled else 0,
                        1 if config.bos_enabled else 0,
                        1 if config.choch_enabled else 0,
                        1 if config.order_blocks_enabled else 0,
                        1 if config.liquidity_enabled else 0,
                        1 if config.fvg_enabled else 0,
                        1 if config.mitigation_enabled else 0,
                        1 if config.sessions_enabled else 0,
                        1 if config.trend_filter_enabled else 0,
                        1 if config.volume_filter_enabled else 0,
                        1 if config.news_filter_enabled else 0,
                        1 if config.time_filter_enabled else 0,
                        1 if config.spread_filter_enabled else 0,
                        config.min_confidence_score, config.min_rr_ratio,
                        config.updated_at.isoformat(), existing.id,
                    ),
                )
            else:
                cursor = await db.execute(
                    """INSERT INTO strategy_configs
                       (account_id, smc_enabled, bos_enabled, choch_enabled,
                        order_blocks_enabled, liquidity_enabled, fvg_enabled,
                        mitigation_enabled, sessions_enabled, trend_filter_enabled,
                        volume_filter_enabled, news_filter_enabled, time_filter_enabled,
                        spread_filter_enabled, min_confidence_score, min_rr_ratio)
                       VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                    (
                        config.account_id,
                        1 if config.smc_enabled else 0,
                        1 if config.bos_enabled else 0,
                        1 if config.choch_enabled else 0,
                        1 if config.order_blocks_enabled else 0,
                        1 if config.liquidity_enabled else 0,
                        1 if config.fvg_enabled else 0,
                        1 if config.mitigation_enabled else 0,
                        1 if config.sessions_enabled else 0,
                        1 if config.trend_filter_enabled else 0,
                        1 if config.volume_filter_enabled else 0,
                        1 if config.news_filter_enabled else 0,
                        1 if config.time_filter_enabled else 0,
                        1 if config.spread_filter_enabled else 0,
                        config.min_confidence_score, config.min_rr_ratio,
                    ),
                )
                config.id = cursor.lastrowid
            await db.commit()
        return config

    def _row_to_risk(self, row) -> RiskConfig:
        return RiskConfig(
            id=row["id"],
            account_id=row["account_id"],
            risk_percent=row["risk_percent"],
            lot_size_override=row["lot_size_override"],
            daily_loss_limit=row["daily_loss_limit"],
            max_concurrent_trades=row["max_concurrent_trades"],
            max_spread_pips=row["max_spread_pips"],
            max_drawdown_percent=row["max_drawdown_percent"],
            rr_ratio=row["rr_ratio"],
            default_sl_pips=row["default_sl_pips"],
            default_tp_pips=row["default_tp_pips"],
            auto_breakeven=bool(row["auto_breakeven"]),
            be_trigger_pips=row["be_trigger_pips"],
            auto_trailing=bool(row["auto_trailing"]),
            trail_distance_pips=row["trail_distance_pips"],
            trail_activation_pips=row["trail_activation_pips"],
        )

    def _row_to_strategy(self, row) -> StrategyConfig:
        return StrategyConfig(
            id=row["id"],
            account_id=row["account_id"],
            smc_enabled=bool(row["smc_enabled"]),
            bos_enabled=bool(row["bos_enabled"]),
            choch_enabled=bool(row["choch_enabled"]),
            order_blocks_enabled=bool(row["order_blocks_enabled"]),
            liquidity_enabled=bool(row["liquidity_enabled"]),
            fvg_enabled=bool(row["fvg_enabled"]),
            mitigation_enabled=bool(row["mitigation_enabled"]),
            sessions_enabled=bool(row["sessions_enabled"]),
            trend_filter_enabled=bool(row["trend_filter_enabled"]),
            volume_filter_enabled=bool(row["volume_filter_enabled"]),
            news_filter_enabled=bool(row["news_filter_enabled"]),
            time_filter_enabled=bool(row["time_filter_enabled"]),
            spread_filter_enabled=bool(row["spread_filter_enabled"]),
            min_confidence_score=row["min_confidence_score"],
            min_rr_ratio=row["min_rr_ratio"],
        )

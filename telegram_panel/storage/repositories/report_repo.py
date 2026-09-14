"""
Report Repository — trade records and daily reports.
"""

import logging
from datetime import datetime, date, timedelta
from typing import Optional
from ..database import Database
from ...models.report import TradeRecord, DailyReport
from ...utils.time import parse_utc, utc_now

logger = logging.getLogger(__name__)


class ReportRepository:
    def __init__(self, db: Database) -> None:
        self._db = db

    async def save_trade(self, trade: TradeRecord) -> TradeRecord:
        async with self._db.connection() as db:
            cursor = await db.execute(
                """INSERT OR REPLACE INTO trade_records
                   (ticket, account_id, symbol, direction, volume, open_price,
                    close_price, stop_loss, take_profit, open_time, close_time,
                    profit, commission, swap, pips, rr_ratio, duration_minutes, close_reason)
                   VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (
                    trade.ticket, trade.account_id, trade.symbol, trade.direction,
                    trade.volume, trade.open_price, trade.close_price,
                    trade.stop_loss, trade.take_profit,
                    trade.open_time.isoformat(), trade.close_time.isoformat(),
                    trade.profit, trade.commission, trade.swap,
                    trade.pips, trade.rr_ratio, trade.duration_minutes, trade.close_reason,
                ),
            )
            await db.commit()
            trade.id = cursor.lastrowid
        return trade

    async def get_trades_for_date(
        self, account_id: int, report_date: date
    ) -> list[TradeRecord]:
        start = datetime.combine(report_date, datetime.min.time()).isoformat()
        end = datetime.combine(report_date, datetime.max.time()).isoformat()
        async with self._db.connection() as db:
            cursor = await db.execute(
                """SELECT * FROM trade_records
                   WHERE account_id=? AND close_time BETWEEN ? AND ?
                   ORDER BY close_time""",
                (account_id, start, end),
            )
            rows = await cursor.fetchall()
        return [self._row_to_trade(r) for r in rows]

    async def get_trades_for_period(
        self, account_id: int, start_date: date, end_date: date
    ) -> list[TradeRecord]:
        start = datetime.combine(start_date, datetime.min.time()).isoformat()
        end = datetime.combine(end_date, datetime.max.time()).isoformat()
        async with self._db.connection() as db:
            cursor = await db.execute(
                """SELECT * FROM trade_records
                   WHERE account_id=? AND close_time BETWEEN ? AND ?
                   ORDER BY close_time""",
                (account_id, start, end),
            )
            rows = await cursor.fetchall()
        return [self._row_to_trade(r) for r in rows]

    async def upsert_daily_report(self, report: DailyReport) -> DailyReport:
        report.updated_at = utc_now()
        async with self._db.connection() as db:
            await db.execute(
                """INSERT INTO daily_reports
                   (account_id, report_date, total_trades, winning_trades, losing_trades,
                    break_even_trades, gross_profit, gross_loss, total_commission, total_swap,
                    net_profit, win_rate, average_rr, average_trade_profit,
                    average_winner, average_loser, max_drawdown, max_drawdown_percent,
                    best_trade_profit, worst_trade_profit, total_pips,
                    starting_balance, ending_balance, profit_factor, sharpe_ratio)
                   VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                   ON CONFLICT(account_id, report_date)
                   DO UPDATE SET
                   total_trades=excluded.total_trades,
                   winning_trades=excluded.winning_trades,
                   losing_trades=excluded.losing_trades,
                   break_even_trades=excluded.break_even_trades,
                   gross_profit=excluded.gross_profit,
                   gross_loss=excluded.gross_loss,
                   total_commission=excluded.total_commission,
                   total_swap=excluded.total_swap,
                   net_profit=excluded.net_profit,
                   win_rate=excluded.win_rate,
                   average_rr=excluded.average_rr,
                   average_trade_profit=excluded.average_trade_profit,
                   average_winner=excluded.average_winner,
                   average_loser=excluded.average_loser,
                   max_drawdown=excluded.max_drawdown,
                   max_drawdown_percent=excluded.max_drawdown_percent,
                   best_trade_profit=excluded.best_trade_profit,
                   worst_trade_profit=excluded.worst_trade_profit,
                   total_pips=excluded.total_pips,
                   starting_balance=excluded.starting_balance,
                   ending_balance=excluded.ending_balance,
                   profit_factor=excluded.profit_factor,
                   sharpe_ratio=excluded.sharpe_ratio,
                   updated_at=datetime('now')""",
                (
                    report.account_id, report.report_date.isoformat(),
                    report.total_trades, report.winning_trades, report.losing_trades,
                    report.break_even_trades, report.gross_profit, report.gross_loss,
                    report.total_commission, report.total_swap, report.net_profit,
                    report.win_rate, report.average_rr, report.average_trade_profit,
                    report.average_winner, report.average_loser,
                    report.max_drawdown, report.max_drawdown_percent,
                    report.best_trade_profit, report.worst_trade_profit,
                    report.total_pips, report.starting_balance,
                    report.ending_balance, report.profit_factor, report.sharpe_ratio,
                ),
            )
            await db.commit()
        return report

    async def get_daily_reports(
        self, account_id: int, days: int = 30
    ) -> list[DailyReport]:
        cutoff = (date.today() - timedelta(days=days)).isoformat()
        async with self._db.connection() as db:
            cursor = await db.execute(
                """SELECT * FROM daily_reports
                   WHERE account_id=? AND report_date >= ?
                   ORDER BY report_date DESC""",
                (account_id, cutoff),
            )
            rows = await cursor.fetchall()
        return [self._row_to_report(r) for r in rows]

    async def get_daily_report(
        self, account_id: int, report_date: date
    ) -> Optional[DailyReport]:
        async with self._db.connection() as db:
            cursor = await db.execute(
                "SELECT * FROM daily_reports WHERE account_id=? AND report_date=?",
                (account_id, report_date.isoformat()),
            )
            row = await cursor.fetchone()
        return self._row_to_report(row) if row else None

    def _row_to_trade(self, row) -> TradeRecord:
        return TradeRecord(
            id=row["id"], ticket=row["ticket"], account_id=row["account_id"],
            symbol=row["symbol"], direction=row["direction"], volume=row["volume"],
            open_price=row["open_price"], close_price=row["close_price"],
            stop_loss=row["stop_loss"], take_profit=row["take_profit"],
            open_time=parse_utc(row["open_time"]) or utc_now(),
            close_time=parse_utc(row["close_time"]) or utc_now(),
            profit=row["profit"], commission=row["commission"], swap=row["swap"],
            pips=row["pips"], rr_ratio=row["rr_ratio"],
            duration_minutes=row["duration_minutes"], close_reason=row["close_reason"],
        )

    def _row_to_report(self, row) -> DailyReport:
        return DailyReport(
            id=row["id"], account_id=row["account_id"],
            report_date=date.fromisoformat(row["report_date"]),
            total_trades=row["total_trades"], winning_trades=row["winning_trades"],
            losing_trades=row["losing_trades"], break_even_trades=row["break_even_trades"],
            gross_profit=row["gross_profit"], gross_loss=row["gross_loss"],
            total_commission=row["total_commission"], total_swap=row["total_swap"],
            net_profit=row["net_profit"], win_rate=row["win_rate"],
            average_rr=row["average_rr"], average_trade_profit=row["average_trade_profit"],
            average_winner=row["average_winner"], average_loser=row["average_loser"],
            max_drawdown=row["max_drawdown"], max_drawdown_percent=row["max_drawdown_percent"],
            best_trade_profit=row["best_trade_profit"], worst_trade_profit=row["worst_trade_profit"],
            total_pips=row["total_pips"], starting_balance=row["starting_balance"],
            ending_balance=row["ending_balance"], profit_factor=row["profit_factor"],
            sharpe_ratio=row["sharpe_ratio"],
        )

"""
Report Service — generate and export trading performance reports.
"""

import csv
import io
import logging
from datetime import date, datetime, timedelta
from typing import Optional
from ..models.report import DailyReport, TradeRecord
from ..storage.repositories.report_repo import ReportRepository

logger = logging.getLogger(__name__)


class ReportService:
    def __init__(self, report_repo: ReportRepository) -> None:
        self._repo = report_repo

    async def get_daily_report(
        self, account_id: int, report_date: Optional[date] = None
    ) -> Optional[DailyReport]:
        if report_date is None:
            report_date = date.today()
        return await self._repo.get_daily_report(account_id, report_date)

    async def get_weekly_report(self, account_id: int) -> DailyReport:
        today = date.today()
        start = today - timedelta(days=today.weekday())
        reports = await self._repo.get_daily_reports(account_id, days=7)
        return self._aggregate_reports(reports, account_id, start, today, "weekly")

    async def get_monthly_report(self, account_id: int) -> DailyReport:
        today = date.today()
        reports = await self._repo.get_daily_reports(account_id, days=30)
        return self._aggregate_reports(
            reports, account_id, today.replace(day=1), today, "monthly"
        )

    async def get_trade_history(
        self,
        account_id: int,
        start_date: Optional[date] = None,
        end_date: Optional[date] = None,
        limit: int = 50,
    ) -> list[TradeRecord]:
        if start_date is None:
            start_date = date.today() - timedelta(days=30)
        if end_date is None:
            end_date = date.today()
        trades = await self._repo.get_trades_for_period(account_id, start_date, end_date)
        return trades[:limit]

    async def compute_and_save_daily(
        self, account_id: int, trades: list[TradeRecord], starting_balance: float
    ) -> DailyReport:
        """Compute daily report from trade records and save it."""
        report = self._compute_stats(trades, account_id, starting_balance)
        await self._repo.upsert_daily_report(report)
        return report

    async def export_to_csv(
        self, account_id: int, period: str = "monthly"
    ) -> tuple[bytes, str]:
        """
        Export trade history to CSV.
        Returns (csv_bytes, filename).
        """
        today = date.today()
        if period == "daily":
            start = today
        elif period == "weekly":
            start = today - timedelta(days=today.weekday())
        else:
            start = today.replace(day=1)

        trades = await self._repo.get_trades_for_period(account_id, start, today)
        output = io.StringIO()
        writer = csv.writer(output)
        writer.writerow([
            "Ticket", "Symbol", "Direction", "Volume",
            "Open Price", "Close Price", "SL", "TP",
            "Open Time", "Close Time", "Profit", "Commission",
            "Swap", "Net Profit", "Pips", "R:R", "Duration (min)", "Reason",
        ])
        for t in trades:
            writer.writerow([
                t.ticket, t.symbol, t.direction, t.volume,
                t.open_price, t.close_price, t.stop_loss, t.take_profit,
                t.open_time.isoformat(), t.close_time.isoformat(),
                round(t.profit, 2), round(t.commission, 2),
                round(t.swap, 2), round(t.net_profit, 2),
                round(t.pips, 1),
                round(t.rr_ratio, 2) if t.rr_ratio else "",
                t.duration_minutes, t.close_reason or "",
            ])
        filename = f"trades_{period}_{today.isoformat()}.csv"
        return output.getvalue().encode("utf-8"), filename

    def _compute_stats(
        self,
        trades: list[TradeRecord],
        account_id: int,
        starting_balance: float,
    ) -> DailyReport:
        if not trades:
            return DailyReport(
                id=None, account_id=account_id,
                report_date=date.today(),
                starting_balance=starting_balance,
                ending_balance=starting_balance,
            )

        winners = [t for t in trades if t.net_profit > 0]
        losers = [t for t in trades if t.net_profit < 0]
        be = [t for t in trades if t.net_profit == 0]

        gross_profit = sum(t.net_profit for t in winners)
        gross_loss = abs(sum(t.net_profit for t in losers))
        net_profit = gross_profit - gross_loss
        commission = sum(t.commission for t in trades)
        swap = sum(t.swap for t in trades)
        pips = sum(t.pips for t in trades)

        win_rate = (len(winners) / len(trades) * 100) if trades else 0.0
        avg_rr = (
            sum(t.rr_ratio for t in trades if t.rr_ratio) /
            max(len([t for t in trades if t.rr_ratio]), 1)
        )
        avg_trade = net_profit / len(trades)
        avg_win = gross_profit / max(len(winners), 1)
        avg_loss = gross_loss / max(len(losers), 1)
        pf = gross_profit / gross_loss if gross_loss > 0 else float("inf")
        best = max(t.net_profit for t in trades)
        worst = min(t.net_profit for t in trades)

        # Simple drawdown calculation
        equity = starting_balance
        peak = starting_balance
        max_dd = 0.0
        for t in trades:
            equity += t.net_profit
            if equity > peak:
                peak = equity
            dd = peak - equity
            if dd > max_dd:
                max_dd = dd
        max_dd_pct = (max_dd / peak * 100) if peak > 0 else 0.0

        return DailyReport(
            id=None, account_id=account_id, report_date=date.today(),
            total_trades=len(trades),
            winning_trades=len(winners),
            losing_trades=len(losers),
            break_even_trades=len(be),
            gross_profit=round(gross_profit, 2),
            gross_loss=round(gross_loss, 2),
            total_commission=round(commission, 2),
            total_swap=round(swap, 2),
            net_profit=round(net_profit, 2),
            win_rate=round(win_rate, 1),
            average_rr=round(avg_rr, 2),
            average_trade_profit=round(avg_trade, 2),
            average_winner=round(avg_win, 2),
            average_loser=round(avg_loss, 2),
            max_drawdown=round(max_dd, 2),
            max_drawdown_percent=round(max_dd_pct, 1),
            best_trade_profit=round(best, 2),
            worst_trade_profit=round(worst, 2),
            total_pips=round(pips, 1),
            starting_balance=round(starting_balance, 2),
            ending_balance=round(starting_balance + net_profit, 2),
            profit_factor=round(pf, 2) if pf != float("inf") else 0.0,
        )

    def _aggregate_reports(
        self,
        reports: list[DailyReport],
        account_id: int,
        start: date,
        end: date,
        label: str,
    ) -> DailyReport:
        if not reports:
            return DailyReport(id=None, account_id=account_id, report_date=end)

        total_trades = sum(r.total_trades for r in reports)
        winners = sum(r.winning_trades for r in reports)
        losers = sum(r.losing_trades for r in reports)
        be = sum(r.break_even_trades for r in reports)
        gross_profit = sum(r.gross_profit for r in reports)
        gross_loss = sum(r.gross_loss for r in reports)
        commission = sum(r.total_commission for r in reports)
        swap = sum(r.total_swap for r in reports)
        net_profit = sum(r.net_profit for r in reports)
        pips = sum(r.total_pips for r in reports)
        win_rate = (winners / total_trades * 100) if total_trades else 0.0
        avg_rr = sum(r.average_rr for r in reports) / len(reports)
        avg_trade = net_profit / total_trades if total_trades else 0.0
        pf = gross_profit / gross_loss if gross_loss > 0 else 0.0
        best = max(r.best_trade_profit for r in reports)
        worst = min(r.worst_trade_profit for r in reports)
        max_dd = max(r.max_drawdown for r in reports)
        max_dd_pct = max(r.max_drawdown_percent for r in reports)
        starting = reports[-1].starting_balance if reports else 0.0
        ending = reports[0].ending_balance if reports else 0.0

        return DailyReport(
            id=None, account_id=account_id, report_date=end,
            total_trades=total_trades,
            winning_trades=winners, losing_trades=losers, break_even_trades=be,
            gross_profit=round(gross_profit, 2), gross_loss=round(gross_loss, 2),
            total_commission=round(commission, 2), total_swap=round(swap, 2),
            net_profit=round(net_profit, 2), win_rate=round(win_rate, 1),
            average_rr=round(avg_rr, 2), average_trade_profit=round(avg_trade, 2),
            average_winner=round(gross_profit / max(winners, 1), 2),
            average_loser=round(gross_loss / max(losers, 1), 2),
            max_drawdown=round(max_dd, 2), max_drawdown_percent=round(max_dd_pct, 1),
            best_trade_profit=round(best, 2), worst_trade_profit=round(worst, 2),
            total_pips=round(pips, 1), starting_balance=round(starting, 2),
            ending_balance=round(ending, 2), profit_factor=round(pf, 2),
        )

"""
Message Formatter — professional Unicode card-style message templates.
Produces beautiful, consistent messages for every panel screen.
"""

from datetime import datetime, timezone
from typing import Optional, Any
from ...config.constants import RobotStatus, ConnectionStatus, ICONS
from ...models.account import Account
from ...models.trade import Position, PendingOrder
from ...models.report import DailyReport, TradeRecord
from ...models.risk_config import RiskConfig
from ...models.strategy_config import StrategyConfig


_DIVIDER = "─" * 32
_THICK_DIVIDER = "═" * 32


class MessageFormatter:
    """Formats all bot messages with professional glass-card Unicode styling."""

    # ─── Home / Welcome ──────────────────────────────────────────────────────

    @staticmethod
    def welcome(user_name: str) -> str:
        return (
            f"🤖 <b>GoldScalperPro Control Panel</b>\n"
            f"<code>{_THICK_DIVIDER}</code>\n\n"
            f"Welcome, <b>{user_name}</b>.\n"
            f"Select an option from the menu below.\n\n"
            f"<i>Panel v1.0 · GoldScalperPro v4</i>"
        )

    # ─── Dashboard ───────────────────────────────────────────────────────────

    @staticmethod
    def dashboard(
        robot_state: dict[str, Any],
        account: Optional[Account],
        active_trades: int,
        pending_orders: int,
        today_profit: float,
        floating_profit: float,
        drawdown: dict[str, float],
        system_stats: dict[str, Any],
    ) -> str:
        status = robot_state.get("status", "stopped")
        try:
            rs = RobotStatus(status)
            status_icon = {
                RobotStatus.RUNNING: "🟢",
                RobotStatus.PAUSED: "🟡",
                RobotStatus.STOPPED: "🔴",
                RobotStatus.ERROR: "❌",
                RobotStatus.STARTING: "🔄",
                RobotStatus.STOPPING: "🔄",
                RobotStatus.RESTARTING: "🔄",
                RobotStatus.WAITING: "🟢",
                RobotStatus.SCANNING: "🟢",
                RobotStatus.HOLDING: "🟢",
                RobotStatus.DISCONNECTED: "🔴",
            }.get(rs, "⚪")
            status_label = rs.value.upper()
        except ValueError:
            status_icon = "⚪"
            status_label = status.upper()

        last_hb = robot_state.get("last_heartbeat", "—")
        data_age_seconds = -1
        stale_warning = ""
        if last_hb and last_hb != "—":
            try:
                hb_dt = datetime.fromisoformat(str(last_hb).replace("Z", "+00:00"))
                if hb_dt.tzinfo is None:
                    hb_dt = hb_dt.replace(tzinfo=timezone.utc)
                elapsed = (datetime.now(timezone.utc) - hb_dt).total_seconds()
                data_age_seconds = int(elapsed)
                if elapsed < 60:
                    last_hb = f"{int(elapsed)}s ago"
                elif elapsed < 3600:
                    last_hb = f"{int(elapsed // 60)}m {int(elapsed % 60)}s ago"
                else:
                    last_hb = f"{int(elapsed // 3600)}h ago"
                # FIX: Show staleness warning if data is older than 3 minutes
                if elapsed > 180:
                    stale_warning = (
                        f"\n⚠️ <b>STALE DATA</b> — last seen {last_hb}. "
                        f"Robot may be offline."
                    )
            except Exception:
                pass
        else:
            stale_warning = "\n⚠️ <b>No heartbeat received.</b> Robot may be offline."

        # Use the staleness flag injected by robot_service if available
        if not robot_state.get("_data_fresh", True) and not stale_warning:
            stale_warning = "\n⚠️ <b>STALE DATA</b> — Robot heartbeat not recent."

        conn_status = robot_state.get("connection_status", "disconnected")
        conn_icon = "🟢" if conn_status == "connected" else "🔴"
        mt5_status = robot_state.get("mt5_status", "disconnected")
        mt5_icon = "🟢" if mt5_status == "connected" else "🔴"

        profit_icon = "💰" if today_profit >= 0 else "🔻"
        float_icon = "💰" if floating_profit >= 0 else "🔻"
        dd_pct = drawdown.get("current_percent", 0.0)

        acc_lines = ""
        if account:
            acc_lines = (
                f"<code>{_DIVIDER}</code>\n"
                f"🏦 <b>Broker:</b>  {account.broker}\n"
                f"👤 <b>Account:</b> {account.login} ({account.account_type.value.upper()})\n"
                f"💳 <b>Balance:</b> {account.currency} {account.balance:,.2f}\n"
                f"📈 <b>Equity:</b>  {account.currency} {account.equity:,.2f}\n"
                f"📊 <b>Margin:</b>  {account.currency} {account.margin:,.2f}\n"
                f"{float_icon} <b>Float:</b>   {account.currency} {floating_profit:+,.2f}\n"
                f"{profit_icon} <b>Today:</b>  {account.currency} {today_profit:+,.2f}\n"
                f"📉 <b>Drawdown:</b> {dd_pct:.1f}%\n"
            )

        cpu = system_stats.get("cpu_percent", 0)
        ram = system_stats.get("ram_percent", 0)

        return (
            f"📊 <b>DASHBOARD</b>\n"
            f"<code>{_THICK_DIVIDER}</code>\n"
            f"{status_icon} <b>Robot:</b>    {status_label}\n"
            f"{conn_icon} <b>Broker:</b>   {conn_status.upper()}\n"
            f"{mt5_icon} <b>MT5:</b>      {mt5_status.upper()}\n"
            f"💓 <b>Heartbeat:</b> {last_hb}\n"
            f"{stale_warning}"
            f"{acc_lines}"
            f"<code>{_DIVIDER}</code>\n"
            f"📈 <b>Open Trades:</b>   {active_trades}\n"
            f"⏳ <b>Pending Orders:</b> {pending_orders}\n"
            f"<code>{_DIVIDER}</code>\n"
            f"🖥️ <b>CPU:</b>  {cpu:.1f}%\n"
            f"🧮 <b>RAM:</b>  {ram:.1f}%\n"
            f"<code>{_THICK_DIVIDER}</code>\n"
            f"<i>Updated: {datetime.now(timezone.utc).strftime('%H:%M:%S UTC')}</i>"
        )

    # ─── Accounts ────────────────────────────────────────────────────────────

    @staticmethod
    def account_list(accounts: list[Account]) -> str:
        if not accounts:
            return (
                f"👤 <b>ACCOUNTS</b>\n"
                f"<code>{_THICK_DIVIDER}</code>\n\n"
                f"No accounts configured.\n"
                f"Use <b>Add Account</b> to add your first broker account."
            )
        lines = [f"👤 <b>ACCOUNTS</b>\n<code>{_THICK_DIVIDER}</code>"]
        for acc in accounts:
            active_mark = "⭐ " if acc.is_active else ""
            lines.append(
                f"\n{active_mark}{acc.type_icon} <b>{acc.name}</b>\n"
                f"  {acc.connection_icon} {acc.broker} · {acc.account_type.value.upper()}\n"
                f"  💳 {acc.currency} {acc.balance:,.2f}"
            )
        return "\n".join(lines)

    @staticmethod
    def account_detail(account: Account) -> str:
        status_icon = "🟢" if account.is_enabled else "🔴"
        return (
            f"{account.type_icon} <b>{account.name}</b>\n"
            f"<code>{_THICK_DIVIDER}</code>\n"
            f"🏦 <b>Broker:</b>       {account.broker}\n"
            f"📡 <b>Server:</b>       {account.server}\n"
            f"👤 <b>Login:</b>        {account.login}\n"
            f"📋 <b>Type:</b>         {account.account_type.value.upper()}\n"
            f"{account.connection_icon} <b>Connection:</b>   {account.connection_status.value}\n"
            f"{status_icon} <b>Status:</b>       {'Enabled' if account.is_enabled else 'Disabled'}\n"
            f"⭐ <b>Active:</b>       {'Yes' if account.is_active else 'No'}\n"
            f"<code>{_DIVIDER}</code>\n"
            f"💳 <b>Balance:</b>  {account.currency} {account.balance:,.2f}\n"
            f"📈 <b>Equity:</b>   {account.currency} {account.equity:,.2f}\n"
            f"📊 <b>Margin:</b>   {account.currency} {account.margin:,.2f}\n"
            f"💰 <b>Float:</b>    {account.currency} {account.floating_profit:+,.2f}\n"
            f"🔢 <b>Leverage:</b> {('1:' + str(account.leverage)) if account.leverage else '—'}\n"
        )

    # ─── Trading ─────────────────────────────────────────────────────────────

    @staticmethod
    def no_positions() -> str:
        return (
            f"📈 <b>OPEN POSITIONS</b>\n"
            f"<code>{_THICK_DIVIDER}</code>\n\n"
            f"No open positions."
        )

    @staticmethod
    def positions_list(positions: list[Position]) -> str:
        if not positions:
            return MessageFormatter.no_positions()
        lines = [f"📈 <b>OPEN POSITIONS ({len(positions)})</b>\n<code>{_THICK_DIVIDER}</code>"]
        for pos in positions:
            _ot = pos.open_time if pos.open_time.tzinfo else pos.open_time.replace(tzinfo=timezone.utc)
            elapsed = int((datetime.now(timezone.utc) - _ot).total_seconds() / 60)
            profit_str = f"{pos.floating_profit:+.2f}"
            lines.append(
                f"\n{pos.direction_icon} <b>#{pos.ticket}</b> · {pos.symbol}\n"
                f"  📦 {pos.volume}L @ {pos.open_price:.2f}\n"
                f"  🛑 SL: {pos.stop_loss or '—'}  🎯 TP: {pos.take_profit or '—'}\n"
                f"  {pos.profit_icon} Profit: <b>{profit_str}</b>\n"
                f"  ⏱️ Open: {elapsed}m"
            )
        return "\n".join(lines)

    @staticmethod
    def position_detail(pos: Position) -> str:
        _ot2 = pos.open_time if pos.open_time.tzinfo else pos.open_time.replace(tzinfo=timezone.utc)
        elapsed_m = int((datetime.now(timezone.utc) - _ot2).total_seconds() / 60)
        return (
            f"📈 <b>POSITION #{pos.ticket}</b>\n"
            f"<code>{_THICK_DIVIDER}</code>\n"
            f"{pos.direction_icon} <b>Direction:</b>  {pos.direction.value}\n"
            f"📦 <b>Volume:</b>     {pos.volume} lots\n"
            f"💹 <b>Symbol:</b>    {pos.symbol}\n"
            f"📍 <b>Open:</b>      {pos.open_price:.5f}\n"
            f"📍 <b>Current:</b>   {pos.current_price:.5f}\n"
            f"🛑 <b>Stop Loss:</b> {pos.stop_loss or '—'}\n"
            f"🎯 <b>Take Profit:</b> {pos.take_profit or '—'}\n"
            f"<code>{_DIVIDER}</code>\n"
            f"{pos.profit_icon} <b>Floating P&L:</b> <b>{pos.floating_profit:+.2f}</b>\n"
            f"💸 <b>Commission:</b> {pos.commission:.2f}\n"
            f"🔄 <b>Swap:</b>      {pos.swap:.2f}\n"
            f"⚖️ <b>Break Even:</b> {'Yes' if pos.breakeven_activated else 'No'}\n"
            f"📐 <b>Trailing:</b>  {'Active' if pos.trailing_stop_active else 'Inactive'}\n"
            f"⏱️ <b>Open for:</b>  {elapsed_m}m\n"
            f"💬 <b>Comment:</b>   {pos.comment or '—'}"
        )

    # ─── Risk ─────────────────────────────────────────────────────────────────

    @staticmethod
    def risk_config(config: RiskConfig) -> str:
        lot_str = (
            f"{config.lot_size_override}" if config.lot_size_override
            else f"{config.risk_percent:.2f}% risk"
        )
        return (
            f"🛡️ <b>RISK CONFIGURATION</b>\n"
            f"<code>{_THICK_DIVIDER}</code>\n"
            f"💹 <b>Risk %:</b>         {config.risk_percent:.2f}%\n"
            f"📦 <b>Lot Size:</b>       {lot_str}\n"
            f"📉 <b>Daily Loss Limit:</b> {config.daily_loss_limit:.1f}%\n"
            f"🔢 <b>Max Trades:</b>     {config.max_concurrent_trades}\n"
            f"📡 <b>Max Spread:</b>     {config.max_spread_pips:.1f} pips\n"
            f"📉 <b>Max Drawdown:</b>   {config.max_drawdown_percent:.1f}%\n"
            f"⚖️ <b>R:R Ratio:</b>      {config.rr_ratio:.1f}\n"
            f"<code>{_DIVIDER}</code>\n"
            f"🛑 <b>SL Pips:</b>        {config.default_sl_pips:.0f}\n"
            f"🎯 <b>TP Pips:</b>        {config.default_tp_pips:.0f}\n"
            f"⚖️ <b>Auto BE:</b>        {'✅ ON' if config.auto_breakeven else '❌ OFF'}\n"
            f"   <b>BE Trigger:</b>     {config.be_trigger_pips:.0f} pips\n"
            f"📐 <b>Auto Trail:</b>     {'✅ ON' if config.auto_trailing else '❌ OFF'}\n"
            f"   <b>Trail Dist:</b>     {config.trail_distance_pips:.0f} pips\n"
            f"   <b>Trail Activation:</b> {config.trail_activation_pips:.0f} pips\n"
        )

    # ─── Strategy ────────────────────────────────────────────────────────────

    @staticmethod
    def strategy_config(config: StrategyConfig) -> str:
        from ...config.constants import StrategyComponent
        lines = [f"🧠 <b>STRATEGY CONFIGURATION</b>\n<code>{_THICK_DIVIDER}</code>"]
        for component in StrategyComponent:
            enabled = config.is_component_enabled(component)
            icon = "🟢" if enabled else "🔴"
            lines.append(f"{icon} {component.display_name}")
        lines.append(f"<code>{_DIVIDER}</code>")
        lines.append(f"🎯 <b>Min Confidence:</b> {config.min_confidence_score:.0f}%")
        lines.append(f"⚖️ <b>Min R:R:</b>        {config.min_rr_ratio:.1f}")
        return "\n".join(lines)

    # ─── Reports ─────────────────────────────────────────────────────────────

    @staticmethod
    def daily_report(report: Optional[DailyReport], period: str = "Daily") -> str:
        if not report or report.total_trades == 0:
            return (
                f"📋 <b>{period.upper()} REPORT</b>\n"
                f"<code>{_THICK_DIVIDER}</code>\n\n"
                f"No trades recorded for this period."
            )
        pf_str = f"{report.profit_factor:.2f}" if report.profit_factor else "∞"
        return (
            f"📋 <b>{period.upper()} REPORT</b> · {report.report_date}\n"
            f"<code>{_THICK_DIVIDER}</code>\n"
            f"📊 <b>Trades:</b>      {report.total_trades} "
            f"({report.winning_trades}W / {report.losing_trades}L)\n"
            f"🎯 <b>Win Rate:</b>    {report.win_rate:.1f}%\n"
            f"⚖️ <b>Avg R:R:</b>    {report.average_rr:.2f}\n"
            f"<code>{_DIVIDER}</code>\n"
            f"💰 <b>Net Profit:</b>  {report.net_profit:+,.2f}\n"
            f"📈 <b>Gross Profit:</b> {report.gross_profit:,.2f}\n"
            f"📉 <b>Gross Loss:</b>  {report.gross_loss:,.2f}\n"
            f"💸 <b>Commission:</b>  {report.total_commission:,.2f}\n"
            f"🔄 <b>Swap:</b>       {report.total_swap:,.2f}\n"
            f"<code>{_DIVIDER}</code>\n"
            f"📏 <b>Avg Trade:</b>   {report.average_trade_profit:+.2f}\n"
            f"⬆️ <b>Avg Winner:</b>  {report.average_winner:.2f}\n"
            f"⬇️ <b>Avg Loser:</b>   {report.average_loser:.2f}\n"
            f"🏆 <b>Best Trade:</b>  {report.best_trade_profit:+.2f}\n"
            f"💔 <b>Worst Trade:</b> {report.worst_trade_profit:+.2f}\n"
            f"<code>{_DIVIDER}</code>\n"
            f"📉 <b>Max Drawdown:</b> {report.max_drawdown_percent:.1f}%\n"
            f"📏 <b>Total Pips:</b>  {report.total_pips:.1f}\n"
            f"⚡ <b>Profit Factor:</b> {pf_str}\n"
            f"💳 <b>Start Balance:</b> {report.starting_balance:,.2f}\n"
            f"💳 <b>End Balance:</b>   {report.ending_balance:,.2f}\n"
        )

    # ─── System ──────────────────────────────────────────────────────────────

    @staticmethod
    def system_stats(stats: dict[str, Any]) -> str:
        uptime = stats.get("uptime", {})
        latency = stats.get("latency_ms")
        internet = stats.get("internet", False)
        net_icon = "🟢" if internet else "🔴"
        lat_str = f"{latency:.1f}ms" if latency else "—"

        def _bar(pct: float) -> str:
            filled = int(pct / 10)
            return "█" * filled + "░" * (10 - filled)

        cpu = stats.get("cpu_percent", 0)
        ram = stats.get("ram_percent", 0)
        disk = stats.get("disk_percent", 0)

        return (
            f"💻 <b>SYSTEM STATUS</b>\n"
            f"<code>{_THICK_DIVIDER}</code>\n"
            f"🤖 <b>Version:</b> {stats.get('version', 'v4.0.0')}\n"
            f"⏱️ <b>Uptime:</b>  {uptime.get('formatted', '—')}\n"
            f"<code>{_DIVIDER}</code>\n"
            f"🖥️ <b>CPU:</b>  {cpu:.1f}%  <code>[{_bar(cpu)}]</code>\n"
            f"🧮 <b>RAM:</b>  {ram:.1f}%  <code>[{_bar(ram)}]</code>\n"
            f"  {stats.get('ram_used_gb', 0):.1f}GB / {stats.get('ram_total_gb', 0):.1f}GB\n"
            f"💾 <b>Disk:</b> {disk:.1f}%  <code>[{_bar(disk)}]</code>\n"
            f"  {stats.get('disk_used_gb', 0):.0f}GB / {stats.get('disk_total_gb', 0):.0f}GB\n"
            f"<code>{_DIVIDER}</code>\n"
            f"{net_icon} <b>Internet:</b> {'Online' if internet else 'Offline'}\n"
            f"⚡ <b>Latency:</b>  {lat_str}\n"
            f"📡 <b>Sent:</b>     {stats.get('net_sent_mb', 0):.1f}MB\n"
            f"📥 <b>Recv:</b>     {stats.get('net_recv_mb', 0):.1f}MB\n"
            f"<code>{_THICK_DIVIDER}</code>\n"
            f"<i>{datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M:%S UTC')}</i>"
        )

    @staticmethod
    def logs(lines: list[str]) -> str:
        content = "\n".join(lines[-30:]) if lines else "No log entries."
        return f"📋 <b>ROBOT LOGS (last 30 lines)</b>\n<code>{_THICK_DIVIDER}</code>\n<code>{content}</code>"

    # ─── Notifications ───────────────────────────────────────────────────────

    @staticmethod
    def trade_opened(pos: Position) -> str:
        text = (
            f"📈 <b>TRADE OPENED</b>\n"
            f"<code>{_DIVIDER}</code>\n"
            f"{pos.direction_icon} {pos.direction.value} · {pos.symbol}\n"
            f"📦 Volume: {pos.volume}L @ {pos.open_price:.5f}\n"
            f"🛑 SL: {pos.stop_loss or '—'}  🎯 TP: {pos.take_profit or '—'}\n"
            f"🎫 Ticket: #{pos.ticket}"
        )
        strategy = pos.strategy
        if strategy:
            grade = strategy.get("grade", "—")
            confidence = strategy.get("confidence")
            regime_label = strategy.get("regime_label", "—")
            confirmations = strategy.get("confirmations") or []
            count = strategy.get("confirmation_count", len(confirmations))
            total = strategy.get("confirmation_total", 4)
            signals = strategy.get("signals") or []

            grade_icons = {"PRIME": "🥇", "HIGH": "🥈", "MARGINAL": "🥉"}
            grade_icon = grade_icons.get(grade, "🔹")

            lines = [
                f"\n<code>{_DIVIDER}</code>",
                f"🧠 <b>Strategy</b>",
                f"{grade_icon} Grade: <b>{grade}</b>"
                + (f"  ·  Confidence: <b>{confidence:.1f}%</b>" if confidence is not None else ""),
                f"📊 Regime: {regime_label}",
                f"✅ Confirmations ({count}/{total}): " + (", ".join(confirmations) if confirmations else "—"),
            ]
            if signals:
                lines.append("🔎 Signals:")
                lines.extend(f"  • {s}" for s in signals)
            text += "\n" + "\n".join(lines)
        return text

    @staticmethod
    def trade_closed(trade: TradeRecord) -> str:
        pnl_icon = "💰" if trade.net_profit >= 0 else "🔻"
        return (
            f"📉 <b>TRADE CLOSED</b>\n"
            f"<code>{_DIVIDER}</code>\n"
            f"{pnl_icon} {trade.direction} · {trade.symbol}\n"
            f"📦 {trade.volume}L @ {trade.close_price:.5f}\n"
            f"💹 Net P&L: <b>{trade.net_profit:+.2f}</b>\n"
            f"📏 Pips: {trade.pips:+.1f}\n"
            f"🏷️ Reason: {trade.close_reason or 'Manual'}\n"
            f"🎫 #{trade.ticket}"
        )

    @staticmethod
    def error_alert(error: str) -> str:
        return (
            f"❌ <b>ERROR ALERT</b>\n"
            f"<code>{_DIVIDER}</code>\n"
            f"<pre>{error[:1000]}</pre>\n"
            f"<i>{datetime.now(timezone.utc).strftime('%H:%M:%S UTC')}</i>"
        )

    @staticmethod
    def heartbeat(status: str, uptime_s: int) -> str:
        d = uptime_s // 86400
        h = (uptime_s % 86400) // 3600
        m = (uptime_s % 3600) // 60
        return (
            f"💓 <b>Heartbeat</b> · {status.upper()}\n"
            f"⏱️ Uptime: {d}d {h:02d}h {m:02d}m\n"
            f"<i>{datetime.now(timezone.utc).strftime('%H:%M:%S UTC')}</i>"
        )

    # ─── Trade History ──────────────────────────────────────────────────────

    @staticmethod
    def trade_history(trades: list) -> str:
        """Format recent closed trades for Telegram display."""
        _D = "─" * 32
        if not trades:
            return (
                "📋 <b>TRADE HISTORY</b>\n"
                f"<code>{_D}</code>\n\n"
                "No completed trades found.\n\n"
                "<i>Trades appear here once the robot closes a position.</i>"
            )
        lines = [f"📋 <b>TRADE HISTORY</b> (last {len(trades)})", f"<code>{_D}</code>"]
        total_pnl = 0.0
        wins = 0
        for t in reversed(trades):
            try:
                direction = getattr(t, "direction", None)
                dir_str   = direction.value if direction else "?"
                pnl       = float(getattr(t, "profit", 0) or 0)
                total_pnl += pnl
                if pnl >= 0:
                    wins += 1
                icon     = "🟢" if pnl >= 0 else "🔴"
                ticket   = getattr(t, "ticket", 0)
                symbol   = getattr(t, "symbol", "XAUUSD")
                volume   = getattr(t, "volume", 0)
                open_px  = float(getattr(t, "open_price",  0) or 0)
                close_px = float(getattr(t, "close_price", 0) or 0)
                open_t   = getattr(t, "open_time", None)
                close_t  = getattr(t, "close_time", None)
                open_s   = open_t.strftime("%m/%d %H:%M")  if open_t  else "—"
                close_s  = close_t.strftime("%m/%d %H:%M") if close_t else "—"
                lines.append(
                    f"\n{icon} <b>{dir_str} #{ticket}</b>  {symbol}\n"
                    f"  📦 {volume}L  |  {open_s} → {close_s}\n"
                    f"  📈 {open_px:.2f} → {close_px:.2f}\n"
                    f"  💵 P&L: <b>{pnl:+.2f}</b>\n"
                    f"  🏷️ Exit: {getattr(t, 'close_reason', None) or 'Unknown'}"
                )
            except Exception:
                continue
        n = len(trades)
        winrate = int(wins / n * 100) if n else 0
        lines.append(
            f"\n<code>{_D}</code>\n"
            f"📊 Total: <b>{total_pnl:+.2f}</b>  |  "
            f"Win-rate: <b>{winrate}%</b> ({wins}/{n})"
        )
        return "\n".join(lines)

    # ─── Helpers ─────────────────────────────────────────────────────────────

    @staticmethod
    def error(message: str) -> str:
        return f"❌ <b>Error</b>\n<code>{_DIVIDER}</code>\n{message}"

    @staticmethod
    def success(message: str) -> str:
        return f"✅ <b>Success</b>\n<code>{_DIVIDER}</code>\n{message}"

    @staticmethod
    def info(title: str, message: str) -> str:
        return f"ℹ️ <b>{title}</b>\n<code>{_DIVIDER}</code>\n{message}"

    @staticmethod
    def warning(message: str) -> str:
        return f"⚠️ <b>Warning</b>\n<code>{_DIVIDER}</code>\n{message}"

    @staticmethod
    def prompt(question: str) -> str:
        return f"✏️ {question}\n\nType your response or press <b>Cancel</b>."

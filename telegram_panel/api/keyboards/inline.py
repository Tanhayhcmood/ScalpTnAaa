"""
Inline Keyboard Layouts — professional keyboard UI for all menu pages.
Every screen has a consistent back button and breadcrumb navigation.
"""

from telegram import InlineKeyboardButton, InlineKeyboardMarkup
from ...config.constants import StrategyComponent, NotificationType, RiskParameter, ICONS
from ...models.account import Account
from ...models.trade import Position, PendingOrder


class Keyboards:
    """
    Factory for all InlineKeyboardMarkup layouts.
    Callback data format: <section>:<action>:<param>
    """

    # ─── Main Menu ──────────────────────────────────────────────────────────

    @staticmethod
    def main_menu() -> InlineKeyboardMarkup:
        return InlineKeyboardMarkup([
            [
                InlineKeyboardButton(f"{ICONS['dashboard']} Dashboard", callback_data="nav:dashboard"),
                InlineKeyboardButton(f"{ICONS['account']} Accounts", callback_data="nav:accounts"),
            ],
            [
                InlineKeyboardButton(f"{ICONS['trading']} Trading", callback_data="nav:trading"),
                InlineKeyboardButton(f"{ICONS['risk']} Risk", callback_data="nav:risk"),
            ],
            [
                InlineKeyboardButton(f"{ICONS['strategy']} Strategy", callback_data="nav:strategy"),
                InlineKeyboardButton(f"{ICONS['news']} News", callback_data="nav:news"),
            ],
            [
                InlineKeyboardButton(f"{ICONS['reports']} Reports", callback_data="nav:reports"),
                InlineKeyboardButton(f"{ICONS['notifications']} Notify", callback_data="nav:notifications"),
            ],
            [
                InlineKeyboardButton(f"{ICONS['settings']} Settings", callback_data="nav:settings"),
                InlineKeyboardButton(f"{ICONS['system']} System", callback_data="nav:system"),
            ],
            [
                InlineKeyboardButton(f"{ICONS['refresh']} Refresh", callback_data="nav:refresh_home"),
            ],
        ])

    # ─── Dashboard ──────────────────────────────────────────────────────────

    @staticmethod
    def dashboard() -> InlineKeyboardMarkup:
        return InlineKeyboardMarkup([
            [
                InlineKeyboardButton(f"{ICONS['refresh']} Refresh", callback_data="dashboard:refresh"),
                InlineKeyboardButton(f"{ICONS['play']} Start", callback_data="robot:start"),
                InlineKeyboardButton(f"{ICONS['pause']} Pause", callback_data="robot:pause"),
            ],
            [
                InlineKeyboardButton(f"{ICONS['stop']} Stop", callback_data="robot:stop_confirm"),
                InlineKeyboardButton(f"{ICONS['emergency']} Emergency", callback_data="robot:emergency_confirm"),
            ],
            [
                InlineKeyboardButton(f"{ICONS['arrow_back']} Back", callback_data="nav:home"),
            ],
        ])

    # ─── Robot Control ──────────────────────────────────────────────────────

    @staticmethod
    def robot_control() -> InlineKeyboardMarkup:
        return InlineKeyboardMarkup([
            [
                InlineKeyboardButton(f"▶️ Start Robot", callback_data="robot:start"),
                InlineKeyboardButton(f"⏸️ Pause Robot", callback_data="robot:pause"),
            ],
            [
                InlineKeyboardButton(f"▶️ Resume Robot", callback_data="robot:resume"),
                InlineKeyboardButton(f"⏹️ Safe Stop", callback_data="robot:stop_confirm"),
            ],
            [
                InlineKeyboardButton(f"🚨 Emergency Stop", callback_data="robot:emergency_confirm"),
            ],
            [
                InlineKeyboardButton(f"🔄 Restart Engine", callback_data="robot:restart_engine_confirm"),
                InlineKeyboardButton(f"📡 Restart MT5", callback_data="robot:restart_mt5_confirm"),
            ],
            [
                InlineKeyboardButton(f"🤖 Restart Telegram", callback_data="robot:restart_telegram_confirm"),
                InlineKeyboardButton(f"🛑 Safe Shutdown", callback_data="robot:shutdown_confirm"),
            ],
            [
                InlineKeyboardButton(f"{ICONS['arrow_back']} Back", callback_data="nav:home"),
            ],
        ])

    @staticmethod
    def confirm_action(action: str, label: str) -> InlineKeyboardMarkup:
        return InlineKeyboardMarkup([
            [
                InlineKeyboardButton(f"✅ Yes, {label}", callback_data=f"robot:{action}_confirmed"),
                InlineKeyboardButton(f"❌ Cancel", callback_data="nav:home"),
            ],
        ])

    # ─── Accounts ───────────────────────────────────────────────────────────

    @staticmethod
    def accounts_menu() -> InlineKeyboardMarkup:
        return InlineKeyboardMarkup([
            [
                InlineKeyboardButton(f"{ICONS['add']} Add Account", callback_data="accounts:add"),
                InlineKeyboardButton(f"📋 List Accounts", callback_data="accounts:list"),
            ],
            [
                InlineKeyboardButton(f"{ICONS['arrow_back']} Back", callback_data="nav:home"),
            ],
        ])

    @staticmethod
    def account_list(accounts: list[Account]) -> InlineKeyboardMarkup:
        rows = []
        for acc in accounts:
            rows.append([
                InlineKeyboardButton(
                    f"{acc.connection_icon} {acc.type_icon} {acc.name}",
                    callback_data=f"accounts:detail:{acc.id}",
                )
            ])
        rows.append([InlineKeyboardButton(f"{ICONS['add']} Add Account", callback_data="accounts:add")])
        rows.append([InlineKeyboardButton(f"{ICONS['arrow_back']} Back", callback_data="nav:home")])
        return InlineKeyboardMarkup(rows)

    @staticmethod
    def account_detail(account: Account) -> InlineKeyboardMarkup:
        status_label = "Disable" if account.is_enabled else "Enable"
        status_action = "disable" if account.is_enabled else "enable"
        return InlineKeyboardMarkup([
            [
                InlineKeyboardButton(
                    f"{'⏸️' if account.is_enabled else '▶️'} {status_label}",
                    callback_data=f"accounts:{status_action}:{account.id}",
                ),
                InlineKeyboardButton(
                    f"⭐ Switch Active",
                    callback_data=f"accounts:switch:{account.id}",
                ),
            ],
            [
                InlineKeyboardButton(
                    f"🔄 Reconnect",
                    callback_data=f"accounts:reconnect:{account.id}",
                ),
                InlineKeyboardButton(
                    f"📡 Test Connection",
                    callback_data=f"accounts:test:{account.id}",
                ),
            ],
            [
                InlineKeyboardButton(
                    f"{ICONS['trash']} Delete",
                    callback_data=f"accounts:delete_confirm:{account.id}",
                ),
            ],
            [
                InlineKeyboardButton(f"{ICONS['arrow_back']} Back", callback_data="accounts:list"),
            ],
        ])

    @staticmethod
    def account_type_select() -> InlineKeyboardMarkup:
        return InlineKeyboardMarkup([
            [InlineKeyboardButton("💰 Real", callback_data="accounts:type:real")],
            [InlineKeyboardButton("🎓 Demo", callback_data="accounts:type:demo")],
            [InlineKeyboardButton("🏆 Prop Firm", callback_data="accounts:type:prop_firm")],
            [InlineKeyboardButton(f"{ICONS['arrow_back']} Cancel", callback_data="accounts:cancel_add")],
        ])

    # ─── Trading ────────────────────────────────────────────────────────────

    @staticmethod
    def trading_menu() -> InlineKeyboardMarkup:
        return InlineKeyboardMarkup([
            [
                InlineKeyboardButton(f"📈 Open Positions", callback_data="trading:positions"),
                InlineKeyboardButton(f"⏳ Pending Orders", callback_data="trading:pending"),
            ],
            [
                InlineKeyboardButton(f"📋 Trade History", callback_data="trading:history"),
            ],
            [
                InlineKeyboardButton(f"🔴 Close All", callback_data="trading:close_all_confirm"),
                InlineKeyboardButton(f"🟢 Close Buy", callback_data="trading:close_buy_confirm"),
                InlineKeyboardButton(f"🔴 Close Sell", callback_data="trading:close_sell_confirm"),
            ],
            [
                InlineKeyboardButton(f"💰 Close Profits", callback_data="trading:close_profit_confirm"),
                InlineKeyboardButton(f"🔻 Close Losses", callback_data="trading:close_loss_confirm"),
            ],
            [
                InlineKeyboardButton(f"{ICONS['arrow_back']} Back", callback_data="nav:home"),
            ],
        ])

    @staticmethod
    def positions_list(positions: list[Position]) -> InlineKeyboardMarkup:
        rows = []
        for pos in positions:
            profit_str = f"+{pos.floating_profit:.2f}" if pos.floating_profit >= 0 else f"{pos.floating_profit:.2f}"
            rows.append([
                InlineKeyboardButton(
                    f"{pos.direction_icon} #{pos.ticket} {pos.symbol} {pos.volume}L | {profit_str}",
                    callback_data=f"trading:position_detail:{pos.ticket}",
                )
            ])
        rows.append([InlineKeyboardButton(f"{ICONS['arrow_back']} Back", callback_data="trading:menu")])
        return InlineKeyboardMarkup(rows)

    @staticmethod
    def position_detail(ticket: int) -> InlineKeyboardMarkup:
        return InlineKeyboardMarkup([
            [
                InlineKeyboardButton(f"❌ Close", callback_data=f"trading:close_confirm:{ticket}"),
                InlineKeyboardButton(f"🔄 Partial Close", callback_data=f"trading:partial_close:{ticket}"),
            ],
            [
                InlineKeyboardButton(f"🛡️ Move SL", callback_data=f"trading:move_sl:{ticket}"),
                InlineKeyboardButton(f"🎯 Move TP", callback_data=f"trading:move_tp:{ticket}"),
            ],
            [
                InlineKeyboardButton(f"⚖️ Break Even", callback_data=f"trading:breakeven:{ticket}"),
                InlineKeyboardButton(f"📐 Trail Stop", callback_data=f"trading:trail:{ticket}"),
            ],
            [
                InlineKeyboardButton(f"{ICONS['arrow_back']} Back", callback_data="trading:positions"),
            ],
        ])

    # ─── Risk ───────────────────────────────────────────────────────────────

    @staticmethod
    def risk_menu() -> InlineKeyboardMarkup:
        return InlineKeyboardMarkup([
            [InlineKeyboardButton(f"📊 View Configuration", callback_data="risk:view")],
            [
                InlineKeyboardButton(f"💹 Risk %", callback_data="risk:edit:risk_percent"),
                InlineKeyboardButton(f"📦 Lot Size", callback_data="risk:edit:lot_size"),
            ],
            [
                InlineKeyboardButton(f"📉 Daily Loss", callback_data="risk:edit:daily_loss_limit"),
                InlineKeyboardButton(f"🔢 Max Trades", callback_data="risk:edit:max_concurrent_trades"),
            ],
            [
                InlineKeyboardButton(f"📡 Max Spread", callback_data="risk:edit:max_spread_pips"),
                InlineKeyboardButton(f"📉 Max DD", callback_data="risk:edit:max_drawdown_percent"),
            ],
            [
                InlineKeyboardButton(f"⚖️ R:R Ratio", callback_data="risk:edit:rr_ratio"),
                InlineKeyboardButton(f"🛑 SL Pips", callback_data="risk:edit:default_sl_pips"),
                InlineKeyboardButton(f"🎯 TP Pips", callback_data="risk:edit:default_tp_pips"),
            ],
            [
                InlineKeyboardButton(f"⚖️ Auto BE", callback_data="risk:toggle:auto_breakeven"),
                InlineKeyboardButton(f"📐 Auto Trail", callback_data="risk:toggle:auto_trailing"),
            ],
            [
                InlineKeyboardButton(f"{ICONS['arrow_back']} Back", callback_data="nav:home"),
            ],
        ])

    # ─── Strategy ───────────────────────────────────────────────────────────

    @staticmethod
    def strategy_menu(config=None) -> InlineKeyboardMarkup:
        def _btn(component: StrategyComponent, config) -> InlineKeyboardButton:
            enabled = config.is_component_enabled(component) if config else True
            icon = "🟢" if enabled else "🔴"
            return InlineKeyboardButton(
                f"{icon} {component.display_name}",
                callback_data=f"strategy:toggle:{component.value}",
            )

        components = list(StrategyComponent)
        rows = []
        # Pair buttons
        for i in range(0, len(components) - 1, 2):
            rows.append([
                _btn(components[i], config),
                _btn(components[i + 1], config),
            ])
        if len(components) % 2 == 1:
            rows.append([_btn(components[-1], config)])

        rows.extend([
            [
                InlineKeyboardButton(f"✅ Enable All", callback_data="strategy:all_on"),
                InlineKeyboardButton(f"❌ Disable All", callback_data="strategy:all_off"),
            ],
            [InlineKeyboardButton(f"{ICONS['arrow_back']} Back", callback_data="nav:home")],
        ])
        return InlineKeyboardMarkup(rows)

    # ─── Reports ────────────────────────────────────────────────────────────

    @staticmethod
    def reports_menu() -> InlineKeyboardMarkup:
        return InlineKeyboardMarkup([
            [
                InlineKeyboardButton(f"📅 Daily", callback_data="reports:daily"),
                InlineKeyboardButton(f"📆 Weekly", callback_data="reports:weekly"),
                InlineKeyboardButton(f"📊 Monthly", callback_data="reports:monthly"),
            ],
            [
                InlineKeyboardButton(f"📋 Trade History", callback_data="reports:history"),
            ],
            [
                InlineKeyboardButton(f"⬇️ Export Daily CSV", callback_data="reports:export:daily"),
                InlineKeyboardButton(f"⬇️ Export Monthly CSV", callback_data="reports:export:monthly"),
            ],
            [
                InlineKeyboardButton(f"{ICONS['arrow_back']} Back", callback_data="nav:home"),
            ],
        ])

    # ─── Notifications ──────────────────────────────────────────────────────

    @staticmethod
    def notifications_menu(settings: list = None) -> InlineKeyboardMarkup:
        rows = []
        settings_map = {s.notification_type: s for s in (settings or [])}

        for ntype in NotificationType:
            setting = settings_map.get(ntype)
            enabled = setting.enabled if setting else True
            icon = "🔔" if enabled else "🔕"
            rows.append([
                InlineKeyboardButton(
                    f"{icon} {ntype.icon} {ntype.display_name}",
                    callback_data=f"notif:toggle:{ntype.value}",
                )
            ])

        rows.extend([
            [
                InlineKeyboardButton(f"🔔 Enable All", callback_data="notif:all_on"),
                InlineKeyboardButton(f"🔕 Disable All", callback_data="notif:all_off"),
            ],
            [InlineKeyboardButton(f"{ICONS['arrow_back']} Back", callback_data="nav:home")],
        ])
        return InlineKeyboardMarkup(rows)

    # ─── System ─────────────────────────────────────────────────────────────

    @staticmethod
    def system_menu() -> InlineKeyboardMarkup:
        return InlineKeyboardMarkup([
            [
                InlineKeyboardButton(f"📊 System Stats", callback_data="system:stats"),
                InlineKeyboardButton(f"📋 View Logs", callback_data="system:logs"),
            ],
            [
                InlineKeyboardButton(f"⏱️ Uptime", callback_data="system:uptime"),
                InlineKeyboardButton(f"🌐 Network", callback_data="system:network"),
            ],
            [
                InlineKeyboardButton(f"👥 Users", callback_data="system:users"),
                InlineKeyboardButton(f"📋 Audit Log", callback_data="system:audit"),
            ],
            [
                InlineKeyboardButton(f"{ICONS['refresh']} Refresh", callback_data="system:refresh"),
            ],
            [
                InlineKeyboardButton(f"{ICONS['arrow_back']} Back", callback_data="nav:home"),
            ],
        ])

    @staticmethod
    def users_menu(users: list = None) -> InlineKeyboardMarkup:
        rows = []
        for user in (users or []):
            rows.append([
                InlineKeyboardButton(
                    f"{user.role_icon} {user.display_name}",
                    callback_data=f"system:user_detail:{user.telegram_id}",
                )
            ])
        rows.append([InlineKeyboardButton(f"{ICONS['arrow_back']} Back", callback_data="system:menu")])
        return InlineKeyboardMarkup(rows)

    @staticmethod
    def user_role_select(telegram_id: int) -> InlineKeyboardMarkup:
        return InlineKeyboardMarkup([
            [InlineKeyboardButton(f"👑 Owner", callback_data=f"system:set_role:{telegram_id}:owner")],
            [InlineKeyboardButton(f"🛡️ Admin", callback_data=f"system:set_role:{telegram_id}:admin")],
            [InlineKeyboardButton(f"👁️ Viewer", callback_data=f"system:set_role:{telegram_id}:viewer")],
            [InlineKeyboardButton(f"🚫 Block", callback_data=f"system:set_role:{telegram_id}:blocked")],
            [InlineKeyboardButton(f"{ICONS['arrow_back']} Back", callback_data="system:users")],
        ])

    # ─── Settings ───────────────────────────────────────────────────────────

    @staticmethod
    def settings_menu() -> InlineKeyboardMarkup:
        return InlineKeyboardMarkup([
            [InlineKeyboardButton(f"🔑 Change Bot Token", callback_data="settings:token")],
            [InlineKeyboardButton(f"👥 Manage Admins", callback_data="settings:admins")],
            [InlineKeyboardButton(f"⏱️ Session Timeout", callback_data="settings:session_timeout")],
            [InlineKeyboardButton(f"🔒 Generate Encryption Key", callback_data="settings:gen_key")],
            [InlineKeyboardButton(f"{ICONS['arrow_back']} Back", callback_data="nav:home")],
        ])

    # ─── Generic ────────────────────────────────────────────────────────────

    @staticmethod
    def back_only(destination: str = "nav:home") -> InlineKeyboardMarkup:
        return InlineKeyboardMarkup([
            [InlineKeyboardButton(f"{ICONS['arrow_back']} Back", callback_data=destination)],
        ])

    @staticmethod
    def confirm_cancel(confirm_data: str, cancel_data: str = "nav:home") -> InlineKeyboardMarkup:
        return InlineKeyboardMarkup([
            [
                InlineKeyboardButton("✅ Confirm", callback_data=confirm_data),
                InlineKeyboardButton("❌ Cancel", callback_data=cancel_data),
            ],
        ])

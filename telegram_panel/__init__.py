"""
GoldScalperPro Telegram Control Panel
======================================
A fully modular, enterprise-grade Telegram administration panel
for the GoldScalperPro trading robot.

This module is designed as a plugin:
  - If Telegram is disabled, the robot runs exactly as before
  - All communication with the trading engine uses service interfaces
  - No circular dependencies with the core trading logic
  - Zero modification to existing robot code

Usage:
    from telegram_panel.main import TelegramPanel
    panel = TelegramPanel()
    await panel.start()
"""

__version__ = "1.0.0"
__author__ = "GoldScalperPro"

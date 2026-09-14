"""
Import sanity check — verifies all modules load without errors.
Run from project root: python telegram_panel/test_imports.py
"""

import sys
import os

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

errors = []

modules = [
    "telegram_panel",
    "telegram_panel.config",
    "telegram_panel.config.settings",
    "telegram_panel.config.constants",
    "telegram_panel.models",
    "telegram_panel.models.account",
    "telegram_panel.models.user",
    "telegram_panel.models.trade",
    "telegram_panel.models.notification",
    "telegram_panel.models.report",
    "telegram_panel.models.session",
    "telegram_panel.models.audit",
    "telegram_panel.models.risk_config",
    "telegram_panel.models.strategy_config",
    "telegram_panel.storage",
    "telegram_panel.storage.database",
    "telegram_panel.storage.encryption",
    "telegram_panel.storage.repositories",
    "telegram_panel.storage.repositories.account_repo",
    "telegram_panel.storage.repositories.user_repo",
    "telegram_panel.storage.repositories.settings_repo",
    "telegram_panel.storage.repositories.notification_repo",
    "telegram_panel.storage.repositories.audit_repo",
    "telegram_panel.storage.repositories.report_repo",
    "telegram_panel.storage.repositories.session_repo",
    "telegram_panel.services",
    "telegram_panel.services.robot_service",
    "telegram_panel.services.mt5_service",
    "telegram_panel.services.account_service",
    "telegram_panel.services.trade_service",
    "telegram_panel.services.risk_service",
    "telegram_panel.services.strategy_service",
    "telegram_panel.services.report_service",
    "telegram_panel.services.system_service",
    "telegram_panel.services.notification_service",
    "telegram_panel.core.event_bus",
    "telegram_panel.core.heartbeat",
    "telegram_panel.main",
]

print("Testing imports...")
for module in modules:
    try:
        __import__(module)
        print(f"  ✅ {module}")
    except ImportError as e:
        # Expected for telegram-specific modules without the package installed
        if "telegram" in str(e) or "aiosqlite" in str(e) or "cryptography" in str(e):
            print(f"  ⚠️  {module} — missing dependency: {e}")
        else:
            print(f"  ❌ {module} — IMPORT ERROR: {e}")
            errors.append((module, str(e)))
    except Exception as e:
        print(f"  ❌ {module} — ERROR: {e}")
        errors.append((module, str(e)))

print()
if errors:
    print(f"❌ {len(errors)} import error(s):")
    for mod, err in errors:
        print(f"   {mod}: {err}")
    sys.exit(1)
else:
    print("✅ All imports passed (dependency errors are expected before pip install)")
    sys.exit(0)

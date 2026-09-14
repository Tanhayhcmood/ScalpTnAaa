"""
conftest.py — pytest session configuration for GoldScalperPro v4 engineering tests.

Adds the project root to sys.path so `live_trading` and `telegram_panel` are
importable as packages during test runs without requiring `pip install -e .`.

Run tests from the project root:
    pytest tests/ -v
or with coverage:
    pytest tests/ -v --cov=live_trading --cov=telegram_panel --cov-report=term-missing
"""
import sys
import os

# Ensure project root is on the path so package imports work
PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if PROJECT_ROOT not in sys.path:
    sys.path.insert(0, PROJECT_ROOT)

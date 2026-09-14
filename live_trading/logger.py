"""Structured logging for GoldScalperPro v4 Live Trading."""
import logging
import sys
from logging.handlers import RotatingFileHandler
from pathlib import Path
from live_trading.config import LOG_FILE

def get_logger(name: str = "GSPv4") -> logging.Logger:
    logger = logging.getLogger(name)
    if logger.handlers:
        return logger

    logger.setLevel(logging.DEBUG)

    fmt = logging.Formatter(
        "%(asctime)s  %(levelname)-8s  %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S"
    )

    # Console handler — always succeeds
    ch = logging.StreamHandler(sys.stdout)
    ch.setLevel(logging.INFO)
    ch.setFormatter(fmt)
    logger.addHandler(ch)

    # Rotating file handler — 10 MB per file, 5 backups (~50 MB total)
    # Prevents disk exhaustion on long-running deployments.
    # Falls back to console-only on permission/disk errors; prints a warning
    # so the failure is visible in cloud provider logs.
    try:
        Path(LOG_FILE).parent.mkdir(parents=True, exist_ok=True)
        fh = RotatingFileHandler(
            LOG_FILE,
            maxBytes=10_000_000,   # 10 MB
            backupCount=5,
            encoding="utf-8",
        )
        fh.setLevel(logging.DEBUG)
        fh.setFormatter(fmt)
        logger.addHandler(fh)
    except Exception as exc:
        sys.stderr.write(
            f"WARNING: GoldScalperPro — could not set up file logging "
            f"to '{LOG_FILE}': {exc}. Console logging only.\n"
        )

    return logger

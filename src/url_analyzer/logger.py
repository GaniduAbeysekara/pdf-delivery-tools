"""Logging setup."""
from __future__ import annotations

import logging
import sys
from pathlib import Path


def setup_logger(log_dir: str = "logs", level: int = logging.INFO) -> logging.Logger:
    """Configure and return the application logger."""
    Path(log_dir).mkdir(parents=True, exist_ok=True)
    log_file = Path(log_dir) / "analyzer.log"

    fmt = "%(asctime)s [%(levelname)s] %(message)s"
    datefmt = "%Y-%m-%d %H:%M:%S"

    logger = logging.getLogger("url_analyzer")
    logger.setLevel(level)

    if logger.handlers:
        return logger

    # File handler
    fh = logging.FileHandler(log_file, encoding="utf-8")
    fh.setFormatter(logging.Formatter(fmt, datefmt))
    fh.setLevel(level)

    # Console handler — INFO and above only
    ch = logging.StreamHandler(sys.stdout)
    ch.setFormatter(logging.Formatter(fmt, datefmt))
    ch.setLevel(logging.WARNING)

    logger.addHandler(fh)
    logger.addHandler(ch)

    return logger


log = setup_logger()

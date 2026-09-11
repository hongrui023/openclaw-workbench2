"""日志与时间工具。

日志的两条纪律：
  1. 写进容器日志的内容一律先过 scrub()，Token 永不落盘。
  2. 用户可见的文案不来自这里——它来自 errors.py 的预定义表。
"""

from __future__ import annotations

import logging
import sys
from datetime import datetime, tzinfo
from zoneinfo import ZoneInfo

from app.config import settings

_LOGGER_NAME = "owb"

_configured = False


def setup_logging() -> logging.Logger:
    global _configured
    logger = logging.getLogger(_LOGGER_NAME)
    if _configured:
        return logger

    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(
        logging.Formatter(
            fmt="%(asctime)s %(levelname)-7s [%(name)s] %(message)s",
            datefmt="%Y-%m-%d %H:%M:%S",
        )
    )
    logger.addHandler(handler)
    logger.setLevel(getattr(logging, settings.log_level, logging.INFO))
    logger.propagate = False
    # uvicorn 的访问日志降噪，避免把极空间的日志刷满
    logging.getLogger("uvicorn.access").setLevel(logging.WARNING)
    _configured = True
    return logger


def get_logger() -> logging.Logger:
    if not _configured:
        return setup_logging()
    return logging.getLogger(_LOGGER_NAME)


def tz() -> tzinfo:
    try:
        return ZoneInfo(settings.timezone)
    except Exception:
        return ZoneInfo("UTC")


def now() -> datetime:
    return datetime.now(tz())


def stamp(fmt: str = "%Y-%m-%d %H:%M:%S") -> str:
    return now().strftime(fmt)


def today_str() -> str:
    return now().strftime("%Y-%m-%d")


def clock_str() -> str:
    return now().strftime("%H:%M")


def human_duration(seconds: float) -> str:
    seconds = max(0.0, float(seconds))
    if seconds < 60:
        return f"{seconds:.0f}s"
    minutes, sec = divmod(int(seconds), 60)
    if minutes < 60:
        return f"{minutes}m{sec:02d}s"
    hours, minutes = divmod(minutes, 60)
    return f"{hours}h{minutes:02d}m"

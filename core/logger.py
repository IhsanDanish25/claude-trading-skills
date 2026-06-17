"""
Logging setup — structured output for Railway logs.
"""
import logging
import sys
import datetime
import pytz

ET = pytz.timezone("America/New_York")


def setup(name: str, level=logging.INFO) -> logging.Logger:
    log = logging.getLogger(name)
    log.setLevel(level)

    if not log.handlers:
        handler = logging.StreamHandler(sys.stdout)
        handler.setLevel(level)
        fmt = logging.Formatter(
            fmt="%(asctime)s | %(levelname)-8s | %(name)s | %(message)s",
            datefmt="%Y-%m-%d %H:%M:%S ET",
        )
        handler.setFormatter(fmt)
        log.addHandler(handler)

    return log


def banner(log: logging.Logger, title: str):
    """Print section banner."""
    log.info("=" * 60)
    log.info(f"  {title.upper()}")
    now = datetime.datetime.now(ET)
    log.info(f"  {now.strftime('%A %Y-%m-%d %H:%M %Z')}")
    log.info("=" * 60)

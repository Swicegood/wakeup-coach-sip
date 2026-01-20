"""Logging configuration for Wake-Up Coach service."""

import logging
import sys


def setup_logging(log_level: str = "INFO") -> logging.Logger:
    """
    Setup logging configuration.

    Args:
        log_level: Log level (DEBUG, INFO, WARNING, ERROR)

    Returns:
        Configured logger instance
    """
    level = getattr(logging, log_level.upper(), logging.INFO)

    logging.basicConfig(
        level=level,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
        stream=sys.stdout
    )

    logger = logging.getLogger("wakeup-coach")
    logger.setLevel(level)

    return logger

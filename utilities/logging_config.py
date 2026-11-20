"""Backward-compatible import for the centralized logger."""

from utils.logger import LOGGER_NAME, configure_logger

configure_logging = configure_logger

__all__ = ["LOGGER_NAME", "configure_logging"]

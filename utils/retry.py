"""Helpers for identifying safe-to-retry transient browser network errors."""

from __future__ import annotations

from playwright.sync_api import Error as PlaywrightError


TRANSIENT_ERROR_MARKERS = (
    "net::err_",
    "err_connection",
    "connection reset",
    "connection refused",
    "temporarily unavailable",
    "timed out",
)


def is_transient_network_error(error: PlaywrightError) -> bool:
    """Return true only for errors commonly caused by temporary network faults."""
    message = str(error).lower()
    return any(marker in message for marker in TRANSIENT_ERROR_MARKERS)
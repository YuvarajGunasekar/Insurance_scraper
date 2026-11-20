"""Bounded HTTP file fetching with atomic writes and completion verification."""

from __future__ import annotations

import logging
import time
from datetime import datetime
from pathlib import Path
from uuid import uuid4

from playwright.sync_api import Error as PlaywrightError
from playwright.sync_api import Page, TimeoutError as PlaywrightTimeoutError

from config.settings import PROJECT_ROOT
from utils.retry import is_transient_network_error


class DownloadError(RuntimeError):
    """Raised when a file cannot be fetched and verified."""


def download_file(
    page: Page,
    url: str,
    destination: Path,
    *,
    timeout_ms: int,
    max_attempts: int,
    backoff_seconds: float,
    logger: logging.Logger,
) -> Path:
    """Fetch a URL using the browser context's cookies and save a verified file.

    Response bodies are fully buffered before an atomic rename. Only temporary
    network errors and timeouts are retried; HTTP errors are not.
    """
    if timeout_ms <= 0:
        raise ValueError("timeout_ms must be positive")
    if not 1 <= max_attempts <= 3:
        raise ValueError("max_attempts must be between 1 and 3")
    if not 0 <= backoff_seconds <= 30:
        raise ValueError("backoff_seconds must be between 0 and 30 seconds")

    destination = Path(destination)
    destination.parent.mkdir(parents=True, exist_ok=True)

    for attempt in range(1, max_attempts + 1):
        temporary_path = destination.with_name(
            f".{destination.name}.{uuid4().hex}.part"
        )
        response = None
        logger.info(
            "File fetching started | operation=download | url=%s | file=%s | "
            "attempt=%d/%d",
            url,
            destination.name,
            attempt,
            max_attempts,
        )
        try:
            response = page.context.request.get(url, timeout=timeout_ms)
            if not response.ok:
                raise DownloadError(
                    f"HTTP {response.status} returned while fetching {url}."
                )

            content = response.body()
            if not content:
                raise DownloadError(f"The response from {url} was empty.")

            temporary_path.write_bytes(content)
            if not temporary_path.is_file() or temporary_path.stat().st_size == 0:
                raise DownloadError(
                    f"Downloaded file {destination.name} is missing or empty."
                )
            temporary_path.replace(destination)
            size_bytes = destination.stat().st_size
            logger.info(
                "File download completed | operation=download | url=%s | "
                "file=%s | bytes=%d",
                url,
                destination.name,
                size_bytes,
            )
            return destination
        except PlaywrightTimeoutError as exc:
            logger.error(
                "File fetching timeout exceeded | operation=download | url=%s | "
                "file=%s | timeout_ms=%d | error=%s",
                url,
                destination.name,
                timeout_ms,
                exc,
                exc_info=True,
            )
            _capture_timeout_screenshot(page, logger, url, destination.name)
            if attempt == max_attempts:
                raise DownloadError(
                    f"Fetching {url} exceeded {timeout_ms} ms after "
                    f"{attempt} attempt(s): {exc}"
                ) from exc
            _wait_before_retry(
                logger, url, destination.name, attempt, max_attempts,
                backoff_seconds, exc,
            )
        except PlaywrightError as exc:
            if not is_transient_network_error(exc) or attempt == max_attempts:
                logger.error(
                    "Download failed | operation=download | url=%s | file=%s | "
                    "error=%s",
                    url,
                    destination.name,
                    exc,
                    exc_info=True,
                )
                _capture_timeout_screenshot(page, logger, url, destination.name)
                raise DownloadError(f"Could not fetch {url}: {exc}") from exc
            logger.warning(
                "Temporary file-fetch network failure | operation=download | "
                "url=%s | file=%s | error=%s",
                url,
                destination.name,
                exc,
            )
            _wait_before_retry(
                logger, url, destination.name, attempt, max_attempts,
                backoff_seconds, exc,
            )
        except DownloadError as exc:
            logger.error(
                "Download failed | operation=download | url=%s | file=%s | "
                "error=%s",
                url,
                destination.name,
                exc,
            )
            raise
        except OSError as exc:
            logger.exception(
                "Download failed while writing file | operation=download | "
                "url=%s | file=%s | error=%s",
                url,
                destination.name,
                exc,
            )
            raise DownloadError(
                f"Could not write downloaded file {destination}: {exc}"
            ) from exc
        except Exception as exc:
            logger.exception(
                "Unexpected browser exception | operation=download | url=%s | "
                "file=%s | error=%s",
                url,
                destination.name,
                exc,
            )
            _capture_timeout_screenshot(page, logger, url, destination.name)
            raise DownloadError(f"Unexpected error while fetching {url}: {exc}") from exc
        finally:
            if response is not None:
                try:
                    response.dispose()
                except Exception:
                    logger.exception(
                        "Response cleanup failed | operation=dispose_download | "
                        "url=%s | file=%s",
                        url,
                        destination.name,
                    )
            try:
                temporary_path.unlink(missing_ok=True)
            except OSError:
                logger.exception(
                    "Temporary file cleanup failed | operation=cleanup_download | "
                    "url=%s | file=%s",
                    url,
                    temporary_path.name,
                )

    raise DownloadError(f"Fetching {url} failed after {max_attempts} attempts.")


def _wait_before_retry(
    logger: logging.Logger,
    url: str,
    file_name: str,
    attempt: int,
    max_attempts: int,
    backoff_seconds: float,
    error: Exception,
) -> None:
    delay = min(backoff_seconds * (2 ** (attempt - 1)), 30)
    logger.warning(
        "Retrying file fetch | operation=download | url=%s | file=%s | "
        "next_attempt=%d/%d | delay_seconds=%.2f | error=%s",
        url,
        file_name,
        attempt + 1,
        max_attempts,
        delay,
        error,
    )
    if delay:
        time.sleep(delay)


def _capture_timeout_screenshot(
    page: Page, logger: logging.Logger, url: str, file_name: str
) -> None:
    screenshot_directory = PROJECT_ROOT / "screenshots"
    timestamp = datetime.now().astimezone().strftime("%Y%m%d-%H%M%S-%f")
    screenshot_path = screenshot_directory / f"download-error-{timestamp}.png"
    try:
        screenshot_directory.mkdir(parents=True, exist_ok=True)
        page.screenshot(path=str(screenshot_path), full_page=True)
    except Exception:
        logger.exception(
            "Screenshot capture failed | operation=capture_download_error | "
            "url=%s | file=%s",
            url,
            file_name,
        )
        return
    logger.info(
        "Screenshot captured | operation=capture_download_error | "
        "url=%s | file=%s | screenshot=%s",
        url,
        file_name,
        screenshot_path.name,
    )
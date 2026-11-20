"""Shared browser navigation, waiting, retry, and screenshot behavior."""

from __future__ import annotations

import logging
import time
from datetime import datetime
from pathlib import Path

from playwright.sync_api import Page, TimeoutError as PlaywrightTimeoutError
from playwright.sync_api import Error as PlaywrightError

from config.settings import PROJECT_ROOT, Settings
from utils.retry import is_transient_network_error


class PageLoadError(RuntimeError):
    """Raised when a browser operation cannot complete within configured limits."""


class BasePage:
    """Common bounded browser operations for page objects."""

    def __init__(self, page: Page, settings: Settings, logger: logging.Logger) -> None:
        self.page = page
        self.settings = settings
        self.logger = logger
        self.screenshots_directory = PROJECT_ROOT / "screenshots"

    def navigate(self, url: str) -> None:
        """Load a URL through the `load` event, retrying only safe transient errors."""
        for attempt in range(1, self.settings.max_attempts + 1):
            self.logger.info(
                "Website loading started | operation=navigate | url=%s | attempt=%d/%d",
                url,
                attempt,
                self.settings.max_attempts,
            )
            try:
                self.page.goto(
                    url,
                    wait_until="load",
                    timeout=self.settings.navigation_timeout_ms,
                )
                self.logger.info(
                    "Website loading completed | operation=navigate | url=%s",
                    self.page.url,
                )
                return
            except PlaywrightTimeoutError as exc:
                self.logger.warning(
                    "Website loading is taking longer than expected | "
                    "operation=navigate | url=%s | attempt=%d/%d | error=%s",
                    url,
                    attempt,
                    self.settings.max_attempts,
                    exc,
                )
                self.logger.error(
                    "Browser loading timeout exceeded | operation=navigate | "
                    "url=%s | error=%s",
                    url,
                    exc,
                    exc_info=True,
                )
                self.capture_screenshot("browser-loading-timeout")
                if attempt == self.settings.max_attempts:
                    raise PageLoadError(
                        f"Navigation to {url} exceeded "
                        f"{self.settings.navigation_timeout_ms} ms after "
                        f"{attempt} attempt(s): {exc}"
                    ) from exc
                self._wait_before_retry(attempt, url, exc)
            except PlaywrightError as exc:
                if not is_transient_network_error(exc):
                    self.logger.exception(
                        "Unexpected browser exception | operation=navigate | url=%s",
                        url,
                    )
                    self.capture_screenshot("browser-navigation-error")
                    raise PageLoadError(
                        f"Could not navigate to {url}: {exc}"
                    ) from exc
                self.logger.error(
                    "Temporary browser network failure | operation=navigate | "
                    "url=%s | error=%s",
                    url,
                    exc,
                    exc_info=True,
                )
                self.capture_screenshot("browser-network-error")
                if attempt == self.settings.max_attempts:
                    raise PageLoadError(
                        f"Navigation to {url} failed after {attempt} attempt(s): {exc}"
                    ) from exc
                self._wait_before_retry(attempt, url, exc)

    def wait_for_selector(
        self, selector: str, *, operation: str, timeout_ms: int
    ) -> None:
        """Wait for a visible element and report a useful bounded timeout error."""
        try:
            self.page.locator(selector).wait_for(state="visible", timeout=timeout_ms)
        except PlaywrightTimeoutError as exc:
            url = self.page.url
            self.logger.error(
                "Browser loading timeout exceeded | operation=%s | url=%s | "
                "selector=%s | error=%s",
                operation,
                url,
                selector,
                exc,
                exc_info=True,
            )
            self.capture_screenshot(f"{operation}-timeout")
            raise PageLoadError(
                f"Timed out after {timeout_ms} ms waiting for {operation} "
                f"at {url} (selector: {selector}): {exc}"
            ) from exc

    def capture_screenshot(self, label: str) -> Path | None:
        """Capture a page screenshot when possible; log failures without hiding them."""
        timestamp = datetime.now().astimezone().strftime("%Y%m%d-%H%M%S-%f")
        screenshot_path = self.screenshots_directory / f"{label}-{timestamp}.png"
        try:
            self.screenshots_directory.mkdir(parents=True, exist_ok=True)
            self.page.screenshot(path=str(screenshot_path), full_page=True)
        except Exception:
            self.logger.exception(
                "Screenshot capture failed | operation=capture_screenshot | "
                "url=%s | file=%s",
                self.page.url,
                screenshot_path.name,
            )
            return None
        self.logger.info(
            "Screenshot captured | operation=capture_screenshot | url=%s | file=%s",
            self.page.url,
            screenshot_path.name,
        )
        return screenshot_path

    def _wait_before_retry(self, attempt: int, url: str, error: Exception) -> None:
        delay = min(
            self.settings.retry_backoff_seconds * (2 ** (attempt - 1)), 30
        )
        self.logger.warning(
            "Retrying temporary navigation failure | operation=navigate | "
            "url=%s | next_attempt=%d/%d | delay_seconds=%.2f | error=%s",
            url,
            attempt + 1,
            self.settings.max_attempts,
            delay,
            error,
        )
        if delay:
            time.sleep(delay)
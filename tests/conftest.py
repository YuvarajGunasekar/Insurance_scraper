"""Shared pytest setup, browser lifecycle, and failure screenshots."""

from __future__ import annotations

import logging
import re
from datetime import datetime
from typing import Iterator

import pytest
from playwright.sync_api import Browser, Page, sync_playwright

from config.settings import ConfigurationError, PROJECT_ROOT, Settings
from utils.logger import configure_logger

ARTIFACT_DIRECTORIES = ("screenshots", "reports", "logs")


def enter_test_code(page: Page, input_selector: str, code: str) -> None:
    """Fill a test-only, known five-letter code into an input field."""
    if not re.fullmatch(r"[A-Z]{5}", code):
        raise ValueError("Test code must be exactly five uppercase letters")
    page.locator(input_selector).fill(code)


def pytest_configure() -> None:
    """Prepare artifact directories and configure application logging."""
    for directory_name in ARTIFACT_DIRECTORIES:
        (PROJECT_ROOT / directory_name).mkdir(parents=True, exist_ok=True)
    configure_logger(PROJECT_ROOT / "logs")


@pytest.fixture(scope="session")
def logger() -> logging.Logger:
    """Provide the shared application logger."""
    return configure_logger(PROJECT_ROOT / "logs")


@pytest.fixture(scope="session")
def settings() -> Settings:
    """Load settings once so missing configuration fails with a clear message."""
    try:
        return Settings.from_env()
    except ConfigurationError as exc:
        pytest.fail(str(exc), pytrace=False)


@pytest.fixture
def page(settings: Settings) -> Iterator[Page]:
    """Launch a fresh browser context and page for each test."""
    with sync_playwright() as playwright:
        browser_type = getattr(playwright, settings.browser)
        try:
            browser: Browser = browser_type.launch(headless=settings.headless)
        except Exception:
            configure_logger(PROJECT_ROOT / "logs").exception(
                "Unexpected browser exception | operation=launch_browser | "
                "url=%s | browser=%s",
                settings.base_url,
                settings.browser,
            )
            raise
        logger = configure_logger(PROJECT_ROOT / "logs")
        logger.info(
            "Browser opened successfully | operation=launch_browser | "
            "url=%s | browser=%s",
            settings.base_url,
            settings.browser,
        )
        try:
            context = browser.new_context(accept_downloads=True)
            browser_page = context.new_page()
            browser_page.set_default_timeout(settings.timeout_ms)
            browser_page.set_default_navigation_timeout(settings.navigation_timeout_ms)
            try:
                yield browser_page
            finally:
                context.close()
        except Exception:
            logger.exception(
                "Unexpected browser exception | operation=create_browser_page | "
                "url=%s | browser=%s",
                settings.base_url,
                settings.browser,
            )
            raise
        finally:
            browser.close()


@pytest.hookimpl(hookwrapper=True)
def pytest_runtest_makereport(item: pytest.Item, call: pytest.CallInfo[object]):
    """Capture a screenshot when a test fails and a browser page is available."""
    outcome = yield
    report = outcome.get_result()
    if not report.failed:
        return

    browser_page = getattr(item, "funcargs", {}).get("page")
    if not isinstance(browser_page, Page):
        return

    timestamp = datetime.now().astimezone().strftime("%Y%m%d-%H%M%S-%f")
    screenshot_path = PROJECT_ROOT / "screenshots" / f"pytest-failure-{timestamp}.png"
    try:
        browser_page.screenshot(path=str(screenshot_path), full_page=True)
        configure_logger(PROJECT_ROOT / "logs").error(
            "Pytest failure screenshot saved: %s", screenshot_path.name
        )
    except Exception:
        configure_logger(PROJECT_ROOT / "logs").exception(
            "Could not capture screenshot for failed test %s.", item.nodeid
        )

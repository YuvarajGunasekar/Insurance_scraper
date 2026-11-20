"""End-to-end login test for an explicitly configured authorized account."""

import logging

from playwright.sync_api import Page

from config.settings import Settings
from pages.login_page import LoginPage


def test_login_result_matches_expectation(
    page: Page, settings: Settings, logger: logging.Logger
) -> None:
    """Check the observed login result against EXPECTED_LOGIN_SUCCESS."""
    login_page = LoginPage(page, settings, logger)
    login_succeeded = login_page.login()

    assert login_succeeded is settings.expected_login_success, (
        "Observed login result did not match EXPECTED_LOGIN_SUCCESS. "
        "Check the configured outcome selectors and authorized test-account setup. "
        f"Current page URL: {page.url}"
    )

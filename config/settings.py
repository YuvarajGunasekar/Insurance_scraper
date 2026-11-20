"""Load and validate runtime settings from the local environment."""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from math import isfinite
from pathlib import Path
from urllib.parse import urlparse

from dotenv import dotenv_values, load_dotenv

PROJECT_ROOT = Path(__file__).resolve().parents[1]
ENV_FILE = PROJECT_ROOT / ".env"
ENV_FILE_VALUES = dotenv_values(ENV_FILE)
load_dotenv(ENV_FILE)


class ConfigurationError(ValueError):
    """Raised when required environment configuration is missing or invalid."""


def _get_bool(name: str, default: bool) -> bool:
    value = os.getenv(name)
    if value is None:
        return default
    normalized = value.strip().lower()
    if normalized in {"1", "true", "yes", "on"}:
        return True
    if normalized in {"0", "false", "no", "off"}:
        return False
    raise ConfigurationError(f"{name} must be true or false, not {value!r}.")


def _required(name: str) -> str:
    # Windows defines USERNAME for the current OS account. Prefer the project
    # .env value so it cannot silently replace the website login username.
    if name == "USERNAME" and name in ENV_FILE_VALUES:
        value = (ENV_FILE_VALUES[name] or "").strip()
    else:
        value = os.getenv(name, "").strip()
    if not value:
        raise ConfigurationError(
            f"{name} is required. Copy .env.example to .env and configure it."
        )
    return value


@dataclass(frozen=True, slots=True)
class Settings:
    """Immutable configuration for one login test run."""

    base_url: str
    username: str
    password: str = field(repr=False)
    username_selector: str
    password_selector: str
    login_button_selector: str
    success_selector: str
    failure_selector: str | None
    captcha_selector: str | None
    browser: str
    headless: bool
    timeout_ms: int
    navigation_timeout_ms: int
    download_timeout_ms: int
    max_attempts: int
    retry_backoff_seconds: float
    expected_login_success: bool
    # New CAPTCHA settings
    max_captcha_attempts: int = 10
    allow_manual_captcha_fallback: bool = True

    @classmethod
    def from_env(cls) -> Settings:
        """Read configuration from environment variables and the project .env."""
        base_url = _required("BASE_URL")
        parsed_url = urlparse(base_url)
        if parsed_url.scheme not in {"http", "https"} or not parsed_url.netloc:
            raise ConfigurationError("BASE_URL must be an absolute http:// or https:// URL.")

        username = _required("USERNAME")
        password = _required("PASSWORD")
        if username.lower().startswith("your-") or password.lower().startswith("replace-"):
            raise ConfigurationError(
                "Replace the sample USERNAME and PASSWORD values in .env with an authorized test account."
            )

        browser = os.getenv("BROWSER", "chromium").strip().lower()
        if browser not in {"chromium", "firefox", "webkit"}:
            raise ConfigurationError("BROWSER must be chromium, firefox, or webkit.")

        timeout_value = os.getenv("TIMEOUT_MS", "10000").strip()
        try:
            timeout_ms = int(timeout_value)
        except ValueError as exc:
            raise ConfigurationError("TIMEOUT_MS must be a positive integer.") from exc
        if timeout_ms <= 0:
            raise ConfigurationError("TIMEOUT_MS must be a positive integer.")

        navigation_timeout_value = os.getenv("NAVIGATION_TIMEOUT_MS", "30000").strip()
        try:
            navigation_timeout_ms = int(navigation_timeout_value)
        except ValueError as exc:
            raise ConfigurationError(
                "NAVIGATION_TIMEOUT_MS must be a positive integer."
            ) from exc
        if navigation_timeout_ms <= 0:
            raise ConfigurationError(
                "NAVIGATION_TIMEOUT_MS must be a positive integer."
            )

        download_timeout_value = os.getenv("DOWNLOAD_TIMEOUT_MS", "60000").strip()
        try:
            download_timeout_ms = int(download_timeout_value)
        except ValueError as exc:
            raise ConfigurationError(
                "DOWNLOAD_TIMEOUT_MS must be a positive integer."
            ) from exc
        if download_timeout_ms <= 0:
            raise ConfigurationError(
                "DOWNLOAD_TIMEOUT_MS must be a positive integer."
            )

        max_attempts_value = os.getenv("MAX_ATTEMPTS", "1").strip()
        try:
            max_attempts = int(max_attempts_value)
        except ValueError as exc:
            raise ConfigurationError("MAX_ATTEMPTS must be an integer from 1 to 3.") from exc
        if not 1 <= max_attempts <= 3:
            raise ConfigurationError("MAX_ATTEMPTS must be an integer from 1 to 3.")

        backoff_value = os.getenv("RETRY_BACKOFF_SECONDS", "1").strip()
        try:
            retry_backoff_seconds = float(backoff_value)
        except ValueError as exc:
            raise ConfigurationError(
                "RETRY_BACKOFF_SECONDS must be a non-negative number."
            ) from exc
        if (
            not isfinite(retry_backoff_seconds)
            or retry_backoff_seconds < 0
            or retry_backoff_seconds > 30
        ):
            raise ConfigurationError(
                "RETRY_BACKOFF_SECONDS must be between 0 and 30 seconds."
            )

        headless = _get_bool("HEADLESS", False)
        captcha_selector = os.getenv("CAPTCHA_SELECTOR", "").strip() or None
        if captcha_selector and headless:
            raise ConfigurationError(
                "Set HEADLESS=false when CAPTCHA_SELECTOR is configured so you can enter the CAPTCHA manually."
            )

        return cls(
            base_url=base_url,
            username=username,
            password=password,
            username_selector=os.getenv(
                "USERNAME_SELECTOR", 'input[name="username"]'
            ).strip(),
            password_selector=os.getenv(
                "PASSWORD_SELECTOR", 'input[name="password"]'
            ).strip(),
            login_button_selector=os.getenv(
                "LOGIN_BUTTON_SELECTOR", 'button[type="submit"]'
            ).strip(),
            success_selector=os.getenv(
                "SUCCESS_SELECTOR", '[data-testid="login-success"]'
            ).strip(),
            failure_selector=os.getenv(
                "FAILURE_SELECTOR", '[data-testid="login-error"]'
            ).strip()
            or None,
            captcha_selector=captcha_selector,
            browser=browser,
            headless=headless,
            timeout_ms=timeout_ms,
            navigation_timeout_ms=navigation_timeout_ms,
            download_timeout_ms=download_timeout_ms,
            max_attempts=max_attempts,
            retry_backoff_seconds=retry_backoff_seconds,
            expected_login_success=_get_bool("EXPECTED_LOGIN_SUCCESS", True),
            max_captcha_attempts=int(os.getenv("MAX_CAPTCHA_ATTEMPTS", "10")),
            allow_manual_captcha_fallback=_get_bool("ALLOW_MANUAL_CAPTCHA_FALLBACK", True),
        )

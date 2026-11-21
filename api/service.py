"""Scraper adapter, URL allowlist validation, and API-specific logging."""

from __future__ import annotations

import asyncio
import hashlib
import hmac
import importlib
import inspect
import ipaddress
import logging
import os
from pathlib import Path
from logging.handlers import RotatingFileHandler
from secrets import token_bytes
from urllib.parse import urlsplit

from dotenv import dotenv_values


PROJECT_ROOT = Path(__file__).resolve().parents[1]
ENV_FILE = PROJECT_ROOT / ".env"
LOGGER_NAME = "scrape_api"
_DEDUPE_SALT = token_bytes(32)


class ScraperNotConfiguredError(RuntimeError):
    """Raised when no existing scraping callable has been configured."""


class ScraperConfigurationError(RuntimeError):
    """Raised when the scraper import path is invalid or cannot be loaded."""


class TargetNotAllowedError(ValueError):
    """Raised when a requested URL is outside the configured host allowlist."""


def _config_value(name: str) -> str:
    """Read a setting from the process environment or project .env file."""
    value = os.getenv(name)
    if value is not None:
        return value.strip()
    return (dotenv_values(ENV_FILE).get(name) or "").strip()


def get_api_logger() -> logging.Logger:
    """Configure a rotating API-only log without changing automation logging."""
    log_directory = PROJECT_ROOT / "logs"
    log_directory.mkdir(parents=True, exist_ok=True)
    logger = logging.getLogger(LOGGER_NAME)
    logger.setLevel(logging.INFO)
    logger.propagate = False
    if getattr(logger, "_api_configured", False):
        return logger

    formatter = logging.Formatter(
        "%(asctime)s | %(levelname)-8s | %(name)s | %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )
    handler = RotatingFileHandler(
        log_directory / "api.log",
        maxBytes=2_000_000,
        backupCount=3,
        encoding="utf-8",
    )
    handler.setFormatter(formatter)
    logger.addHandler(handler)
    logger._api_configured = True  # type: ignore[attr-defined]
    return logger


def scraper_is_configured() -> bool:
    """Return whether the existing scraper callable has an import path set."""
    return bool(_config_value("SCRAPER_CALLABLE"))


def allowed_hosts_are_configured() -> bool:
    """Return whether API URL restrictions have been explicitly configured."""
    return bool(_allowed_hosts())


def get_scrape_website_url() -> str:
    """Read the scrape target URL from BASE_URL in the project configuration."""
    website_url = _config_value("BASE_URL")
    if not website_url:
        raise ScraperNotConfiguredError(
            "BASE_URL is required. Set it in the project .env file."
        )
    return website_url


def validate_target_url(website_url: str) -> str:
    """Require HTTPS, a configured exact host allowlist, and no private IP host."""
    parsed = urlsplit(website_url)
    if parsed.scheme != "https" or not parsed.hostname:
        raise TargetNotAllowedError("website_url must use HTTPS and include a hostname.")
    if parsed.username or parsed.password:
        raise TargetNotAllowedError("website_url must not contain embedded credentials.")

    host = parsed.hostname.rstrip(".").lower()
    try:
        ip_address = ipaddress.ip_address(host)
    except ValueError:
        ip_address = None
    if ip_address is not None and not ip_address.is_global:
        raise TargetNotAllowedError("Private or non-public IP addresses are not allowed.")

    allowed_hosts = _allowed_hosts()
    if not allowed_hosts:
        raise ScraperNotConfiguredError(
            "Set API_ALLOWED_HOSTS to a comma-separated list of authorized hostnames."
        )
    if host not in allowed_hosts:
        raise TargetNotAllowedError(
            f"Hostname '{host}' is not in the API_ALLOWED_HOSTS allowlist."
        )
    return website_url


def load_scraper_callable():
    """Load an existing callable configured as `python.module:function_name`."""
    import_path = _config_value("SCRAPER_CALLABLE")
    if not import_path:
        raise ScraperNotConfiguredError(
            "No scraper entry point exists in this project. Configure "
            "SCRAPER_CALLABLE=package.module:function_name to an existing authorized scraper."
        )
    module_name, separator, function_name = import_path.partition(":")
    if not separator or not module_name or not function_name:
        raise ScraperConfigurationError(
            "SCRAPER_CALLABLE must use the format package.module:function_name."
        )
    try:
        module = importlib.import_module(module_name)
        scraper = getattr(module, function_name)
    except (ImportError, AttributeError) as exc:
        raise ScraperConfigurationError(
            f"Could not load configured scraper callable '{import_path}': {exc}"
        ) from exc
    if not callable(scraper):
        raise ScraperConfigurationError(
            f"Configured scraper '{import_path}' is not callable."
        )
    return scraper


def run_scraper(
    scraper,
    website_url: str,
    username: str,
    password: str,
    scrape_type: str,
    **kwargs,
) -> str:
    """Invoke the configured scraper and return a bounded result summary."""
    logger = get_api_logger()
    logger.info(
        "Existing scraper invocation started | operation=invoke_scraper | "
        "url=%s | scrape_type=%s",
        website_url,
        scrape_type,
    )
    try:
        # Check what arguments the scraper callable accepts
        sig = inspect.signature(scraper)
        accepted_kwargs = {}
        has_var_keyword = any(p.kind == inspect.Parameter.VAR_KEYWORD for p in sig.parameters.values())
        for k, v in kwargs.items():
            if has_var_keyword or k in sig.parameters:
                accepted_kwargs[k] = v

        result = scraper(
            website_url=website_url,
            username=username,
            password=password,
            scrape_type=scrape_type,
            **accepted_kwargs,
        )
        if inspect.isawaitable(result):
            result = asyncio.run(result)
    except Exception as exc:
        safe_error = str(exc)
        for secret in (username, password):
            if secret:
                safe_error = safe_error.replace(secret, "[REDACTED]")
        logger.error(
            "Existing scraper failed | operation=invoke_scraper | "
            "url=%s | error=%s",
            website_url,
            safe_error,
        )
        raise RuntimeError(safe_error) from None
    return f"Scraper returned {type(result).__name__}."


def credential_fingerprint(username: str, password: str) -> str:
    """Return an ephemeral keyed digest for safe active-task deduplication."""
    credentials = f"{username}\0{password}".encode("utf-8")
    return hmac.new(_DEDUPE_SALT, credentials, hashlib.sha256).hexdigest()


def _allowed_hosts() -> set[str]:
    return {
        host.strip().rstrip(".").lower()
        for host in _config_value("API_ALLOWED_HOSTS").split(",")
        if host.strip()
    }

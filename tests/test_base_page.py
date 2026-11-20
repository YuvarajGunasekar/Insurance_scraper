"""Offline tests for bounded page navigation and timeout handling."""

from __future__ import annotations

import logging
from pathlib import Path
from types import SimpleNamespace

import pytest
from playwright.sync_api import TimeoutError as PlaywrightTimeoutError

import pages.base_page as base_page_module
from pages.base_page import BasePage, PageLoadError


class FakePage:
    def __init__(self, outcomes: list[object]) -> None:
        self.outcomes = outcomes
        self.calls = 0
        self.url = "about:blank"

    def goto(self, url: str, *, wait_until: str, timeout: int) -> None:
        self.calls += 1
        outcome = self.outcomes.pop(0)
        if isinstance(outcome, Exception):
            raise outcome
        self.url = url

    def screenshot(self, *, path: str, full_page: bool) -> None:
        Path(path).write_bytes(b"screenshot")


def make_settings() -> SimpleNamespace:
    return SimpleNamespace(
        max_attempts=3,
        navigation_timeout_ms=100,
        retry_backoff_seconds=0,
    )


def test_navigation_retries_timeout_then_loads(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(base_page_module, "PROJECT_ROOT", tmp_path)
    page = FakePage([PlaywrightTimeoutError("slow response"), None])
    base = BasePage(
        page,  # type: ignore[arg-type]
        make_settings(),  # type: ignore[arg-type]
        logging.getLogger("test_base_page"),
    )

    base.navigate("https://example.test/login")

    assert page.calls == 2
    assert page.url == "https://example.test/login"
    assert list(tmp_path.joinpath("screenshots").glob("browser-loading-timeout-*.png"))


def test_navigation_stops_after_configured_attempts(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(base_page_module, "PROJECT_ROOT", tmp_path)
    page = FakePage([PlaywrightTimeoutError("slow response") for _ in range(3)])
    base = BasePage(
        page,  # type: ignore[arg-type]
        make_settings(),  # type: ignore[arg-type]
        logging.getLogger("test_base_page"),
    )

    with pytest.raises(PageLoadError, match="after 3 attempt"):
        base.navigate("https://example.test/login")

    assert page.calls == 3
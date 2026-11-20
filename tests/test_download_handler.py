"""Focused tests for bounded file fetching and download verification."""

from __future__ import annotations

import logging
from pathlib import Path

import pytest
from playwright.sync_api import TimeoutError as PlaywrightTimeoutError

import utils.download_handler as download_handler
from utils.download_handler import DownloadError, download_file


class FakeResponse:
    def __init__(self, content: bytes, *, status: int = 200) -> None:
        self._content = content
        self.status = status
        self.ok = 200 <= status < 300

    def body(self) -> bytes:
        return self._content

    def dispose(self) -> None:
        pass


class FakeRequest:
    def __init__(self, outcomes: list[object]) -> None:
        self.outcomes = outcomes
        self.calls = 0

    def get(self, url: str, *, timeout: int) -> FakeResponse:
        self.calls += 1
        outcome = self.outcomes.pop(0)
        if isinstance(outcome, Exception):
            raise outcome
        return outcome  # type: ignore[return-value]


class FakePage:
    def __init__(self, outcomes: list[object]) -> None:
        self.context = type(
            "FakeContext", (), {"request": FakeRequest(outcomes)}
        )()

    def screenshot(self, *, path: str, full_page: bool) -> None:
        Path(path).write_bytes(b"screenshot")


def test_download_retries_timeout_and_verifies_nonempty_file(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(download_handler, "PROJECT_ROOT", tmp_path)
    request_timeout = PlaywrightTimeoutError("request timed out")
    page = FakePage([request_timeout, FakeResponse(b"completed file")])
    destination = tmp_path / "download" / "result.bin"

    result = download_file(
        page,  # type: ignore[arg-type]
        "https://example.test/export",
        destination,
        timeout_ms=100,
        max_attempts=3,
        backoff_seconds=0,
        logger=logging.getLogger("test_download_handler"),
    )

    assert result == destination
    assert destination.read_bytes() == b"completed file"
    assert destination.stat().st_size > 0
    assert page.context.request.calls == 2
    assert list((tmp_path / "screenshots").glob("download-error-*.png"))


def test_http_error_is_not_retried(tmp_path: Path) -> None:
    page = FakePage([FakeResponse(b"unauthorized", status=401), FakeResponse(b"unused")])

    with pytest.raises(DownloadError, match="HTTP 401"):
        download_file(
            page,  # type: ignore[arg-type]
            "https://example.test/private-export",
            tmp_path / "private.bin",
            timeout_ms=100,
            max_attempts=3,
            backoff_seconds=0,
            logger=logging.getLogger("test_download_handler"),
        )

    assert page.context.request.calls == 1
    assert not (tmp_path / "private.bin").exists()
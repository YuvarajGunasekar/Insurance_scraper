"""Tests for scrape API credentials and .env-backed configuration."""

from __future__ import annotations

from datetime import datetime, timezone
from uuid import uuid4

from fastapi.testclient import TestClient

import api.main as api_main
import api.routes as api_routes
import api.service as api_service
from api.task_manager import ScrapeTask, TaskManager, TaskStatus, TaskSubmission
from config.settings import Settings


def test_scrape_uses_only_request_credentials_and_dotenv_defaults(
    monkeypatch,
) -> None:
    config = {
        "BASE_URL": "https://example.com/quote",
        "API_ALLOWED_HOSTS": "example.com",
        "SCRAPER_CALLABLE": "scrapers.vehicle_scraper:scrape_vehicle_coverage",
    }
    for key in config:
        monkeypatch.delenv(key, raising=False)
    monkeypatch.setattr(api_service, "dotenv_values", lambda _: config)
    monkeypatch.setattr(api_routes, "load_scraper_callable", lambda: object())

    invoked = {}

    def fake_run_scraper(
        scraper, website_url, username, password, scrape_type, **kwargs
    ):
        invoked.update(
            website_url=website_url,
            username=username,
            password=password,
            scrape_type=scrape_type,
            kwargs=kwargs,
        )
        return "Scraper returned None."

    def fake_submit(
        manager: TaskManager,
        website_url: str,
        scrape_type: str,
        work,
        *,
        dedupe_token: str,
    ) -> TaskSubmission:
        work()
        now = datetime.now(timezone.utc)
        task = ScrapeTask(
            task_id=uuid4(),
            website_url=website_url,
            scrape_type=scrape_type,
            status=TaskStatus.PENDING,
            created_at=now,
            updated_at=now,
        )
        return TaskSubmission(task=task, deduplicated=False)

    monkeypatch.setattr(api_routes, "run_scraper", fake_run_scraper)
    monkeypatch.setattr(TaskManager, "submit", fake_submit)

    with TestClient(api_main.app) as client:
        response = client.post(
            "/api/scrape",
            json={"username": "api-user", "password": "api-password"},
        )
        extra_field_response = client.post(
            "/api/scrape",
            json={
                "username": "api-user",
                "password": "api-password",
                "website_url": "https://other.example/",
            },
        )

    assert response.status_code == 202
    assert invoked == {
        "website_url": config["BASE_URL"],
        "username": "api-user",
        "password": "api-password",
        "scrape_type": "full",
        "kwargs": {},
    }
    assert extra_field_response.status_code == 422


def test_scrape_reports_missing_dotenv_target_url(monkeypatch) -> None:
    monkeypatch.delenv("BASE_URL", raising=False)
    monkeypatch.setattr(api_service, "dotenv_values", lambda _: {})

    with TestClient(api_main.app) as client:
        response = client.post(
            "/api/scrape",
            json={"username": "api-user", "password": "api-password"},
        )

    assert response.status_code == 503
    assert response.json()["detail"] == (
        "BASE_URL is required. Set it in the project .env file."
    )


def test_scraper_settings_can_use_request_credentials(monkeypatch) -> None:
    monkeypatch.setenv("BASE_URL", "https://example.com/login")
    monkeypatch.delenv("USERNAME", raising=False)
    monkeypatch.delenv("PASSWORD", raising=False)

    settings = Settings.from_env(username="api-user", password="api-password")

    assert settings.username == "api-user"
    assert settings.password == "api-password"

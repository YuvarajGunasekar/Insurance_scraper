"""Offline tests for the API's manual-CAPTCHA login task endpoints."""

from __future__ import annotations

import threading
from uuid import UUID

from fastapi.testclient import TestClient

import api.main as api_main
from api.login_tasks import LoginTaskManager, LoginTaskStatus
from api.service import PROJECT_ROOT


def test_login_api_pauses_for_manual_captcha_and_redacts_credentials(
    monkeypatch,
) -> None:
    monkeypatch.setenv("API_ALLOWED_HOSTS", "example.com")
    monkeypatch.setenv("HEADLESS", "false")
    worker_started = threading.Event()
    permit_completion = threading.Event()
    credentials = ("api-test-user-9382", "api-test-password-7812")

    def fake_browser_login(
        manager: LoginTaskManager,
        task_id,
        website_url: str,
        username: str,
        password: str,
    ) -> bool:
        assert (username, password) == credentials
        manager._set_state(
            task_id,
            LoginTaskStatus.AWAITING_CAPTCHA,
            message="Enter CAPTCHA manually in browser.",
        )
        worker_started.set()
        assert permit_completion.wait(3)
        return True

    monkeypatch.setattr(LoginTaskManager, "_run_browser_login", fake_browser_login)
    with TestClient(api_main.app) as client:
        body = {
            "website_url": "https://example.com/login",
            "username": credentials[0],
            "password": credentials[1],
        }
        accepted = client.post("/api/login", json=body)
        assert accepted.status_code == 202
        task_id = accepted.json()["task_id"]
        assert worker_started.wait(2)

        waiting = client.get(f"/api/login/status/{task_id}")
        assert waiting.status_code == 200
        assert waiting.json()["status"] == "awaiting_captcha"
        assert credentials[0] not in waiting.text
        assert credentials[1] not in waiting.text

        duplicate = client.post("/api/login", json=body)
        assert duplicate.status_code == 202
        assert duplicate.json()["task_id"] == task_id
        assert duplicate.json()["deduplicated"] is True

        continued = client.post(f"/api/login/continue/{task_id}")
        assert continued.status_code == 200
        permit_completion.set()

        manager = client.app.state.login_task_manager
        thread = manager._threads[UUID(task_id)]
        thread.join(timeout=3)
        completed = client.get(f"/api/login/status/{task_id}")
        assert completed.status_code == 200
        assert completed.json()["status"] == "completed"
        assert completed.json()["login_succeeded"] is True

    log_content = (PROJECT_ROOT / "logs" / "api.log").read_text(encoding="utf-8")
    assert credentials[0] not in log_content
    assert credentials[1] not in log_content

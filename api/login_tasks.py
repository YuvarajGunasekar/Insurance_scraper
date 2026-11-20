"""Manual-CAPTCHA login tasks driven by the LoginPage Page Object."""

from __future__ import annotations

import logging
import os
from dataclasses import dataclass, replace
from datetime import datetime, timezone
from enum import StrEnum
from pathlib import Path
from threading import Event, Lock, Thread
import time
from uuid import UUID, uuid4

from dotenv import dotenv_values
from playwright.sync_api import Error as PlaywrightError
from playwright.sync_api import TimeoutError as PlaywrightTimeoutError
from playwright.sync_api import sync_playwright

from api.service import credential_fingerprint
from config.settings import Settings
from pages.login_page import LoginPage


PROJECT_ROOT = Path(__file__).resolve().parents[1]
ENV_FILE = PROJECT_ROOT / ".env"


class LoginTaskStatus(StrEnum):
    PENDING = "pending"
    AWAITING_CAPTCHA = "awaiting_captcha"
    RUNNING = "running"
    COMPLETED = "completed"
    FAILED = "failed"


class LoginTaskError(RuntimeError):
    """Raised when a manual login task cannot safely continue."""


@dataclass(slots=True)
class LoginTask:
    task_id: UUID
    website_url: str
    status: LoginTaskStatus
    created_at: datetime
    updated_at: datetime
    message: str | None = None
    login_succeeded: bool | None = None
    error: str | None = None
    continue_event: Event | None = None
    cancel_event: Event | None = None


@dataclass(frozen=True, slots=True)
class LoginSubmission:
    task: LoginTask
    deduplicated: bool


class LoginTaskManager:
    """Launch visible login browsers and wait for the user to enter the CAPTCHA."""

    def __init__(self, logger: logging.Logger) -> None:
        self._logger = logger
        self._lock = Lock()
        self._tasks: dict[UUID, LoginTask] = {}
        self._active_keys: dict[tuple[str, str], UUID] = {}
        self._threads: dict[UUID, Thread] = {}
        self._config = self._read_nonsecret_config()

    def start_login(
        self, website_url: str, username: str, password: str
    ) -> LoginSubmission:
        """Create a task and open the configured login form; the user enters the CAPTCHA."""
        config = self._config
        if not config["username_selector"] or not config["password_selector"]:
            raise LoginTaskError(
                "Set USERNAME_SELECTOR and PASSWORD_SELECTOR in the project .env file."
            )
        if not config["login_button_selector"]:
            raise LoginTaskError("Set LOGIN_BUTTON_SELECTOR in the project .env file.")

        dedupe_key = (
            website_url,
            credential_fingerprint(username, password),
        )
        with self._lock:
            existing_id = self._active_keys.get(dedupe_key)
            existing = self._tasks.get(existing_id) if existing_id else None
            if existing and existing.status in {
                LoginTaskStatus.PENDING,
                LoginTaskStatus.AWAITING_CAPTCHA,
                LoginTaskStatus.RUNNING,
            }:
                return LoginSubmission(replace(existing), deduplicated=True)

            now = datetime.now(timezone.utc)
            task = LoginTask(
                task_id=uuid4(),
                website_url=website_url,
                status=LoginTaskStatus.PENDING,
                created_at=now,
                updated_at=now,
                message="Opening the login page.",
                continue_event=Event(),
                cancel_event=Event(),
            )
            self._tasks[task.task_id] = task
            self._active_keys[dedupe_key] = task.task_id
            thread = Thread(
                target=self._run_task,
                args=(task.task_id, dedupe_key, website_url, username, password),
                name=f"manual-login-{task.task_id}",
                daemon=True,
            )
            self._threads[task.task_id] = thread
            thread.start()
            return LoginSubmission(replace(task), deduplicated=False)

    def continue_login(self, task_id: UUID) -> LoginTask | None:
        """Signal the browser worker to continue after manual CAPTCHA entry."""
        with self._lock:
            task = self._tasks.get(task_id)
            if task is None:
                return None
            if task.status is not LoginTaskStatus.AWAITING_CAPTCHA:
                raise LoginTaskError(
                    f"Task is {task.status.value}; it is not waiting for CAPTCHA entry."
                )
            assert task.continue_event is not None
            task.continue_event.set()
            return replace(task)

    def get(self, task_id: UUID) -> LoginTask | None:
        """Return a task snapshot without its events or credentials."""
        with self._lock:
            task = self._tasks.get(task_id)
            return replace(task) if task else None

    def wait_for_completion(self, task_id: UUID, timeout_seconds: float = 300.0) -> LoginTask | None:
        """Block until the task finishes (completed or failed) and return a snapshot.

        Returns None if the task does not exist. Raises TimeoutError if the task
        does not finish within *timeout_seconds*.
        """
        deadline = time.monotonic() + timeout_seconds
        poll_interval = 0.5  # seconds
        while True:
            with self._lock:
                task = self._tasks.get(task_id)
            if task is None:
                return None
            if task.status in {LoginTaskStatus.COMPLETED, LoginTaskStatus.FAILED}:
                return replace(task)
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise TimeoutError(
                    f"Login task {task_id} did not finish within {timeout_seconds:.0f}s."
                )
            time.sleep(min(poll_interval, remaining))

    def shutdown(self) -> None:
        """Release workers waiting for user input when the API is stopping."""
        with self._lock:
            tasks = list(self._tasks.values())
        for task in tasks:
            if task.cancel_event is not None:
                task.cancel_event.set()
            if task.continue_event is not None:
                task.continue_event.set()

    def _run_task(
        self,
        task_id: UUID,
        dedupe_key: tuple[str, str],
        website_url: str,
        username: str,
        password: str,
    ) -> None:
        try:
            succeeded = self._run_browser_login(
                task_id, website_url, username, password
            )
        except Exception as exc:
            safe_error = self._redact(str(exc), username, password)
            self._logger.error(
                "Manual login task failed | operation=login | task_id=%s | "
                "url=%s | error=%s",
                task_id,
                website_url,
                safe_error,
            )
            self._set_state(
                task_id,
                LoginTaskStatus.FAILED,
                error=safe_error[:1000],
                message="Login task failed. See the API log for details.",
            )
        else:
            state = LoginTaskStatus.COMPLETED if succeeded else LoginTaskStatus.FAILED
            message = "Login succeeded." if succeeded else "Login was not successful."
            self._set_state(
                task_id,
                state,
                login_succeeded=succeeded,
                message=message,
                error=None if succeeded else "The site did not show a successful login outcome.",
            )
            self._logger.info(
                "Manual login task finished | operation=login | task_id=%s | "
                "url=%s | login_succeeded=%s",
                task_id,
                website_url,
                succeeded,
            )
        finally:
            with self._lock:
                if self._active_keys.get(dedupe_key) == task_id:
                    del self._active_keys[dedupe_key]
                self._threads.pop(task_id, None)

    def _run_browser_login(
        self, task_id: UUID, website_url: str, username: str, password: str
    ) -> bool:
        """Run the login flow via LoginPage, pausing for manual CAPTCHA entry."""
        config = self._config
        browser = None
        page = None
        self._logger.info(
            "Manual login started | operation=open_login_page | task_id=%s | url=%s",
            task_id,
            website_url,
        )
        try:
            with sync_playwright() as playwright:
                browser_type = getattr(playwright, config["browser"])
                browser = browser_type.launch(headless=config["headless"])
                context = browser.new_context()
                page = context.new_page()
                page.set_default_timeout(config["timeout_ms"])
                page.set_default_navigation_timeout(config["navigation_timeout_ms"])

                # Build a Settings object so LoginPage can drive the form.
                settings = Settings(
                    base_url=website_url,
                    username=username,
                    password=password,
                    username_selector=config["username_selector"],
                    password_selector=config["password_selector"],
                    captcha_selector=config["captcha_selector"] or None,
                    login_button_selector=config["login_button_selector"],
                    success_selector=config["success_selector"] or "",
                    failure_selector=config["failure_selector"] or None,
                    browser=config["browser"],
                    headless=config["headless"],
                    timeout_ms=config["timeout_ms"],
                    navigation_timeout_ms=config["navigation_timeout_ms"],
                    download_timeout_ms=60000,
                    max_attempts=5,
                    retry_backoff_seconds=1.0,
                    expected_login_success=True,
                )

                self._set_state(
                    task_id,
                    LoginTaskStatus.RUNNING,
                    message="Opening the login page and filling credentials.",
                )

                login_page = LoginPage(
                    page,
                    settings,
                    self._logger,
                    captcha_wait=lambda: self._wait_for_manual_captcha(
                        task_id, config["captcha_timeout_ms"] / 1000
                    ),
                )
                succeeded = login_page.login(max_attempts=5)
                label = "login-success" if succeeded else "login-failure"
                self._capture_screenshot(page, task_id, label)

                # Keep an explicitly visible browser open until the user closes it.
                if not config["headless"]:
                    self._set_state(
                        task_id,
                        LoginTaskStatus.RUNNING,
                        message="Browser is open. Close the browser window when finished.",
                        login_succeeded=succeeded,
                    )
                    try:
                        page.wait_for_timeout(config["captcha_timeout_ms"])
                    except PlaywrightError:
                        pass

                return succeeded
        except PlaywrightTimeoutError as exc:
            if page is not None:
                self._capture_screenshot(page, task_id, "login-timeout")
            raise LoginTaskError(
                f"Browser operation timed out for {website_url}: {exc}"
            ) from exc
        except PlaywrightError as exc:
            if page is not None:
                self._capture_screenshot(page, task_id, "login-browser-error")
            raise LoginTaskError(
                f"Browser operation failed for {website_url}: {exc}"
            ) from exc
        finally:
            if browser is not None:
                try:
                    browser.close()
                except Exception as exc:
                    self._logger.error(
                        "Browser close failed | operation=close_browser | "
                        "task_id=%s | url=%s | error=%s",
                        task_id,
                        website_url,
                        exc,
                    )

    def _wait_for_manual_captcha(self, task_id: UUID, timeout_seconds: float) -> None:
        """Mark the task as awaiting the CAPTCHA and block until /continue is called."""
        with self._lock:
            task = self._tasks[task_id]
            continue_event, cancel_event = task.continue_event, task.cancel_event
        assert continue_event is not None and cancel_event is not None

        # Clear before publishing the status so a /continue call can't be lost.
        continue_event.clear()
        self._set_state(
            task_id,
            LoginTaskStatus.AWAITING_CAPTCHA,
            message="Enter the CAPTCHA in the browser, then call the continue endpoint.",
        )
        if not continue_event.wait(timeout_seconds):
            raise LoginTaskError("Timed out waiting for the CAPTCHA to be entered.")
        if cancel_event.is_set():
            raise LoginTaskError("Login task was cancelled.")
        self._set_state(
            task_id, LoginTaskStatus.RUNNING, message="Submitting the login form."
        )

    @staticmethod
    def _wait_for_outcome(page, starting_url: str, config: dict[str, object]) -> None:
        page.wait_for_function(
            """args => {
                const visible = selector => {
                    if (!selector) return false;
                    const element = document.querySelector(selector);
                    if (!element) return false;
                    const style = window.getComputedStyle(element);
                    return element.getClientRects().length > 0 &&
                        style.visibility !== 'hidden' && style.display !== 'none';
                };
                return location.href !== args.startingUrl ||
                    visible(args.successSelector) || visible(args.failureSelector);
            }""",
            arg={
                "startingUrl": starting_url,
                "successSelector": config["success_selector"],
                "failureSelector": config["failure_selector"],
            },
            timeout=config["timeout_ms"],
        )

    def _set_state(self, task_id: UUID, status: LoginTaskStatus, **updates) -> None:
        with self._lock:
            task = self._tasks[task_id]
            task.status = status
            task.updated_at = datetime.now(timezone.utc)
            for name, value in updates.items():
                setattr(task, name, value)

    @staticmethod
    def _capture_screenshot(page, task_id: UUID, label: str) -> None:
        screenshot_directory = PROJECT_ROOT / "screenshots"
        timestamp = datetime.now().astimezone().strftime("%Y%m%d-%H%M%S-%f")
        screenshot_path = screenshot_directory / f"api-{label}-{timestamp}.png"
        try:
            screenshot_directory.mkdir(parents=True, exist_ok=True)
            page.screenshot(path=str(screenshot_path), full_page=True)
        except Exception:
            logging.getLogger("scrape_api").exception(
                "Login screenshot failed | operation=capture_screenshot | "
                "task_id=%s | file=%s",
                task_id,
                screenshot_path.name,
            )

    @staticmethod
    def _redact(message: str, username: str, password: str) -> str:
        for secret in (username, password):
            if secret:
                message = message.replace(secret, "[REDACTED]")
        return message

    @staticmethod
    def _read_nonsecret_config() -> dict[str, object]:
        file_values = dotenv_values(ENV_FILE)

        def value(name: str, default: str) -> str:
            return os.getenv(name, file_values.get(name) or default).strip()

        def positive_int(name: str, default: int) -> int:
            raw_value = value(name, str(default))
            try:
                parsed_value = int(raw_value)
            except ValueError as exc:
                raise LoginTaskError(f"{name} must be a positive integer.") from exc
            if parsed_value <= 0:
                raise LoginTaskError(f"{name} must be a positive integer.")
            return parsed_value

        headless_value = value("HEADLESS", "false").lower()
        if headless_value not in {"true", "false", "1", "0", "yes", "no", "on", "off"}:
            raise LoginTaskError("HEADLESS must be true or false.")

        return {
            "username_selector": value("USERNAME_SELECTOR", 'input[name="username"]'),
            "password_selector": value("PASSWORD_SELECTOR", 'input[name="password"]'),
            "captcha_selector": value("CAPTCHA_SELECTOR", ""),
            "login_button_selector": value("LOGIN_BUTTON_SELECTOR", 'button[type="submit"]'),
            "success_selector": value("SUCCESS_SELECTOR", ""),
            "failure_selector": value("FAILURE_SELECTOR", ""),
            "browser": value("BROWSER", "chromium").lower(),
            "headless": headless_value in {"true", "1", "yes", "on"},
            "timeout_ms": positive_int("TIMEOUT_MS", 60000),
            "navigation_timeout_ms": positive_int("NAVIGATION_TIMEOUT_MS", 30000),
            "captcha_timeout_ms": positive_int("LOGIN_CAPTCHA_TIMEOUT_MS", 300000),
        }
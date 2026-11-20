"""Page Object for a configurable, authorized website login form."""

from __future__ import annotations

import hashlib
import json
import logging
import os
from collections.abc import Callable
from datetime import datetime
from pathlib import Path

from playwright.sync_api import Page, TimeoutError as PlaywrightTimeoutError

from config.settings import PROJECT_ROOT, Settings

# Saved login cookies, so the CAPTCHA is only needed when the session has expired.
# This file grants access to the logged-in account: keep it local and out of Git.
SESSION_FILE = PROJECT_ROOT / ".auth" / "session.json"


class LoginPage:
    """Encapsulate login-page selectors and user-facing login interactions.

    The CAPTCHA is always entered by a person in the visible browser. By default
    the page object prompts in the terminal and waits for Enter; callers such as
    the API can pass ``captcha_wait`` to pause some other way.
    """

    def __init__(
        self,
        page: Page,
        settings: Settings,
        logger: logging.Logger,
        captcha_wait: Callable[[], None] | None = None,
    ) -> None:
        self.page = page
        self.settings = settings
        self.logger = logger
        self._captcha_wait = captcha_wait
        self.screenshots_directory = PROJECT_ROOT / "screenshots"

    def open(self) -> None:
        """Navigate to the configured login page."""
        self.logger.info("Opening configured login page.")
        self.page.goto(self.settings.base_url, wait_until="domcontentloaded")
        self.page.locator(self.settings.username_selector).wait_for(state="visible")

    def enter_credentials(self) -> None:
        """Fill the username and password without logging either value."""
        self.logger.info("Entering configured username.")
        self.page.locator(self.settings.username_selector).fill(self.settings.username)
        self.logger.info("Entering configured password from the local environment.")
        self.page.locator(self.settings.password_selector).fill(self.settings.password)

    def ensure_credentials(self) -> None:
        """Verify username and password fields are filled; re-fill if cleared by postbacks."""
        try:
            u_el = self.page.locator(self.settings.username_selector).first
            p_el = self.page.locator(self.settings.password_selector).first
            if u_el.count() and not u_el.input_value().strip():
                u_el.fill(self.settings.username)
            if p_el.count() and not p_el.input_value():
                p_el.fill(self.settings.password)
        except Exception:
            pass

    def handle_manual_captcha(self) -> None:
        """Pause so a person can type the CAPTCHA into the visible browser."""
        selector = self.settings.captcha_selector
        if not selector:
            self.logger.info("No CAPTCHA selector configured; continuing without CAPTCHA interaction.")
            return

        self.page.locator(selector).wait_for(state="visible")
        self.logger.info("Waiting for the CAPTCHA to be entered manually in the browser.")
        (self._captcha_wait or self._wait_for_terminal_confirmation)()

    @staticmethod
    def _wait_for_terminal_confirmation() -> None:
        """Default pause: ask the person at the terminal to press Enter when done."""
        try:
            input("Enter the CAPTCHA in the browser window, then press Enter here to continue... ")
        except EOFError as exc:
            raise RuntimeError(
                "Manual CAPTCHA entry needs an interactive terminal (for pytest, use -s)."
            ) from exc

    def submit_and_check(self) -> bool:
        """Submit the form and determine its result using configured page markers or URL redirect."""
        # Ensure credentials were not cleared by CAPTCHA refresh or postbacks
        self.ensure_credentials()

        # The person typing the CAPTCHA may also press Enter or click Log In
        # themselves, in which case the form is already gone.
        login_button = self.page.locator(self.settings.login_button_selector).first
        if login_button.is_visible():
            self.logger.info("Submitting login form.")
            try:
                login_button.click()
            except PlaywrightTimeoutError:
                if login_button.is_visible():
                    raise
                self.logger.info("Login form was submitted in the browser while clicking.")
        else:
            self.logger.info("Login form was already submitted in the browser; checking the result.")

        # Wait up to 6 seconds for either redirect away from IMDLogin OR error banner
        try:
            self.page.wait_for_function(
                """() => {
                    const url = window.location.href;
                    if (!url.includes('IMDLogin')) return true;
                    const err = document.querySelector('.validation-summary-errors, #lblError, [id*=\"Error\"], [id*=\"error\"], .alert-danger');
                    if (err && err.innerText && err.innerText.trim()) return true;
                    const body = document.body ? document.body.innerText : '';
                    if (body.includes('does not match') || body.includes('Invalid Captcha') || body.includes('invalid') || body.includes('check your user name')) {
                        return true;
                    }
                    return false;
                }""",
                timeout=6000,
            )
        except PlaywrightTimeoutError:
            pass

        if "IMDLogin" not in self.page.url:
            login_succeeded = True
        else:
            # Check if any error text is visible
            err_text = self.page.evaluate("""() => {
                const el = document.querySelector('.validation-summary-errors, #lblError, [id*=\"Error\"], [id*=\"error\"], .alert-danger');
                if (el && el.innerText && el.innerText.trim()) return el.innerText.trim();
                const body = document.body ? document.body.innerText : '';
                const match = body.match(/(?:User Name and password does not match|Invalid Captcha|Please check your user name|login failed)[^\\n.]*/i);
                return match ? match[0] : null;
            }""")
            if err_text:
                self.logger.warning("Login unsuccessful: %s", err_text)
            else:
                self.logger.warning("Still on login page after submit.")
            login_succeeded = False

        outcome = "success" if login_succeeded else "failure"
        screenshot_path = self.capture_screenshot(outcome)
        self.logger.info(
            "Login outcome: %s. Screenshot: %s",
            outcome,
            screenshot_path.name if screenshot_path else "unavailable",
        )
        return login_succeeded

    def login(self, max_attempts: int | None = None) -> bool:
        """Run the login flow for the requested number of attempts.

        When max_attempts is omitted, use Settings.max_attempts.
        """
        if max_attempts is None:
            max_attempts = getattr(self.settings, "max_attempts", 1)
        try:
            if self._restore_session():
                return True
            for attempt in range(1, max_attempts + 1):
                self.logger.info("Login attempt %d of %d", attempt, max_attempts)
                # Fresh page navigation for every attempt to reset session/tokens/CAPTCHA
                self.open()
                self.enter_credentials()
                self.handle_manual_captcha()
                self.ensure_credentials()
                if self.submit_and_check():
                    self._save_session()
                    return True
                # If still on login page and we have attempts left, retry.
                if "IMDLogin" in self.page.url and attempt < max_attempts:
                    self.logger.warning("Retrying with a fresh login page and CAPTCHA.")
                    self.page.wait_for_timeout(1000)
                else:
                    break
            return False
        except Exception:
            self.logger.exception("Login workflow raised an exception.")
            self.capture_screenshot("failure")
            raise

    def _session_owner(self) -> str:
        """Identify the site and account a saved session belongs to, without storing either."""
        raw = f"{self.settings.base_url}\n{self.settings.username}".encode("utf-8")
        return hashlib.sha256(raw).hexdigest()

    def _save_session(self) -> None:
        """Save the logged-in cookies so later runs can skip the login form."""
        try:
            SESSION_FILE.parent.mkdir(parents=True, exist_ok=True)
            payload = {
                "owner": self._session_owner(),
                "cookies": self.page.context.cookies(),
            }
            SESSION_FILE.write_text(json.dumps(payload), encoding="utf-8")
            self.logger.info("Saved login session for reuse.")
        except Exception:
            self.logger.exception("Could not save the login session; next run will log in again.")

    def _restore_session(self) -> bool:
        """Return True if a saved session is still logged in, so no CAPTCHA is needed."""
        if not SESSION_FILE.exists():
            return False
        try:
            payload = json.loads(SESSION_FILE.read_text(encoding="utf-8"))
            if payload.get("owner") != self._session_owner() or not payload.get("cookies"):
                return False
            self.page.context.add_cookies(payload["cookies"])

            # A protected page shows whether the session is alive: an expired one
            # is sent back to the login form.
            check_url = (
                os.getenv("SESSION_CHECK_URL")
                or os.getenv("VEHICLE_DETAILS_URL")
                or self.settings.base_url
            )
            self.page.goto(check_url, wait_until="domcontentloaded")
            self.page.wait_for_timeout(1000)
            on_login_form = (
                "IMDLogin" in self.page.url
                or self.page.locator(self.settings.username_selector).first.is_visible()
            )
            if not on_login_form:
                self.logger.info("Reused saved login session; no CAPTCHA needed.")
                return True
            self.logger.info("Saved login session has expired; logging in again.")
        except Exception:
            self.logger.exception("Could not reuse the saved login session; logging in again.")

        # Stale cookies must not leak into the fresh login attempt.
        try:
            self.page.context.clear_cookies()
            SESSION_FILE.unlink(missing_ok=True)
        except Exception:
            pass
        return False

    def capture_screenshot(self, outcome: str) -> Path | None:
        """Save a full-page screenshot for this login outcome, if possible."""
        self.screenshots_directory.mkdir(parents=True, exist_ok=True)
        timestamp = datetime.now().astimezone().strftime("%Y%m%d-%H%M%S-%f")
        screenshot_path = self.screenshots_directory / f"login-{outcome}-{timestamp}.png"
        try:
            self.page.screenshot(path=str(screenshot_path), full_page=True)
        except Exception:
            self.logger.exception("Could not capture login screenshot.")
            return None
        return screenshot_path

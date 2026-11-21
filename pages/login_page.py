"""Page Object for a configurable, authorized website login form."""

from __future__ import annotations

import hashlib
import io
import json
import logging
import os
import re
import tempfile
from collections.abc import Callable
from datetime import datetime
from pathlib import Path

from playwright.sync_api import Page, TimeoutError as PlaywrightTimeoutError

from config.settings import PROJECT_ROOT, Settings

# Saved login cookies, so the CAPTCHA is only needed when the session has expired.
# This file grants access to the logged-in account: keep it local and out of Git.
SESSION_FILE = PROJECT_ROOT / ".auth" / "session.json"

# ---------------------------------------------------------------------------
# CAPTCHA OCR helpers  (Tesseract + Pillow)
# ---------------------------------------------------------------------------
_TESSERACT_CMD: str | None = None


def _find_tesseract() -> str:
    """Return the path to the Tesseract binary, cached after first lookup."""
    global _TESSERACT_CMD
    if _TESSERACT_CMD:
        return _TESSERACT_CMD

    # Explicit override via env var
    env_path = os.getenv("TESSERACT_CMD", "").strip()
    if env_path and Path(env_path).is_file():
        _TESSERACT_CMD = env_path
        return _TESSERACT_CMD

    # Common Windows install location
    win_default = Path(r"C:\Program Files\Tesseract-OCR\tesseract.exe")
    if win_default.is_file():
        _TESSERACT_CMD = str(win_default)
        return _TESSERACT_CMD

    # Assume it's on PATH (Linux / macOS / manually configured Windows)
    _TESSERACT_CMD = "tesseract"
    return _TESSERACT_CMD


def _ocr_captcha_image(image_bytes: bytes, logger: logging.Logger) -> str:
    """Run optimized Tesseract OCR on raw PNG bytes for 1st-attempt CAPTCHA capture.

    IndusInd SmartZone CAPTCHAs are always exactly 4 uppercase letters (A-Z).
    Pre-processing pipeline:
      1. Extract Red channel and Grayscale at 3× resolution (Red channel removes background noise).
      2. Invert if background is dark; add white border padding (15px) to prevent edge clipping.
      3. Generate multi-variant set (clean, contrast-enhanced, and adaptive thresholded).
      4. Run OCR with PSM 6 (single block), PSM 7 (single line), and PSM 8 (single word)
         using a strict uppercase-only whitelist (A-Z, no digits, no lowercase).
      5. Majority-vote consensus: returns the most frequent 4-character uppercase code.
    """
    try:
        from collections import Counter
        from PIL import Image, ImageEnhance, ImageOps
        import pytesseract
    except ImportError as exc:
        logger.error(
            "Pillow or pytesseract is not installed. "
            "Install them with: pip install Pillow pytesseract"
        )
        raise RuntimeError("CAPTCHA OCR dependencies missing") from exc

    pytesseract.pytesseract.tesseract_cmd = _find_tesseract()

    raw_img = Image.open(io.BytesIO(image_bytes))
    if raw_img.mode != "RGB":
        raw_img = raw_img.convert("RGB")

    w, h = raw_img.size
    # 1. Scale up 3× with bicubic interpolation
    scaled = raw_img.resize((w * 3, h * 3), Image.BICUBIC)

    # 2. Extract Red channel (best glyph isolation on IndusInd) and Grayscale
    r, _, _ = scaled.split()
    gray = scaled.convert("L")

    # Invert if the background is dark
    import numpy as np
    if np.mean(np.array(r)) < 127:
        r = ImageOps.invert(r)
    if np.mean(np.array(gray)) < 127:
        gray = ImageOps.invert(gray)

    # Save debug image for troubleshooting
    debug_path = PROJECT_ROOT / "screenshots" / "_captcha_debug.png"
    try:
        debug_path.parent.mkdir(parents=True, exist_ok=True)
        r.save(str(debug_path))
    except Exception:
        pass

    # 3. Create high-confidence padded variants
    pad_px = 15
    padded_r = ImageOps.expand(r, border=pad_px, fill="white")
    padded_gray = ImageOps.expand(gray, border=pad_px, fill="white")

    enhanced_r = ImageOps.expand(ImageEnhance.Contrast(r).enhance(2.0), border=pad_px, fill="white")
    enhanced_gray = ImageOps.expand(ImageEnhance.Contrast(gray).enhance(2.0), border=pad_px, fill="white")

    thresh_r_140 = ImageOps.expand(r.point(lambda p: 255 if p > 140 else 0, "1").convert("L"), border=pad_px, fill="white")
    thresh_g_140 = ImageOps.expand(gray.point(lambda p: 255 if p > 140 else 0, "1").convert("L"), border=pad_px, fill="white")
    thresh_r_155 = ImageOps.expand(r.point(lambda p: 255 if p > 155 else 0, "1").convert("L"), border=pad_px, fill="white")

    variants = [
        ("red", padded_r),
        ("red_enhanced", enhanced_r),
        ("thresh_r_140", thresh_r_140),
        ("gray_enhanced", enhanced_gray),
        ("thresh_g_140", thresh_g_140),
        ("thresh_r_155", thresh_r_155),
        ("gray", padded_gray),
    ]

    # Strict uppercase whitelist — IndusInd CAPTCHAs are exclusively letters A-Z
    _WHITELIST = "ABCDEFGHIJKLMNOPQRSTUVWXYZ"
    configs = [
        f"--oem 3 --psm 6 -c tessedit_char_whitelist={_WHITELIST}",   # single uniform block
        f"--oem 3 --psm 7 -c tessedit_char_whitelist={_WHITELIST}",   # single text line
        f"--oem 3 --psm 8 -c tessedit_char_whitelist={_WHITELIST}",   # single word
    ]

    candidates_4char: list[str] = []
    all_candidates: list[str] = []

    for v_name, v_img in variants:
        for config in configs:
            psm = config.split("--psm ")[1][:2].strip()
            raw_text = pytesseract.image_to_string(v_img, config=config)
            cleaned = re.sub(r"[^A-Z]", "", raw_text).strip()
            if not cleaned:
                continue

            all_candidates.append(cleaned)
            if len(cleaned) == 4:
                candidates_4char.append(cleaned)
                counts = Counter(candidates_4char)
                top, freq = counts.most_common(1)[0]
                # High-confidence early consensus: 2 matching 4-character readings
                if freq >= 2:
                    logger.info(
                        "CAPTCHA OCR 1st-attempt consensus result: '%s' (freq=%d/%d, variant=%s, psm=%s)",
                        top, freq, len(candidates_4char), v_name, psm,
                    )
                    return top

    # Fallback 1: Most common 4-character result if any was seen
    if candidates_4char:
        counts = Counter(candidates_4char)
        top, freq = counts.most_common(1)[0]
        logger.info(
            "CAPTCHA OCR result (4-char majority): '%s' (freq=%d/%d)",
            top, freq, len(candidates_4char),
        )
        return top

    # Fallback 2: Most common non-empty result
    if all_candidates:
        counts = Counter(all_candidates)
        top, freq = counts.most_common(1)[0]
        logger.info(
            "CAPTCHA OCR fallback result: '%s' (freq=%d/%d)",
            top, freq, len(all_candidates),
        )
        return top

    logger.warning("CAPTCHA OCR produced no usable text across all variants.")
    return ""


# ---------------------------------------------------------------------------
# CAPTCHA image selectors — ordered by likelihood on the IndusInd SmartZone
# login page.  The script tries each selector until it finds the CAPTCHA
# image element.
# ---------------------------------------------------------------------------
_CAPTCHA_IMAGE_SELECTORS = [
    "#imgCaptcha",                              # most common id
    "#CaptchaImage",                            # alternate id
    "img[src*='Captcha']",                      # src-based
    "img[src*='captcha']",                      # src-based (lowercase)
    "img[src*='CaptchaImage']",                 # src-based (full handler)
    "#CaptchaInputText ~ img",                  # img sibling of the input
    "#CaptchaInputText + img",                  # img immediately after input
    "img[alt*='captcha' i]",                    # alt attribute
    ".captcha-image img",                       # generic wrapper class
    "#divCaptcha img",                          # div wrapper id
    "img[id*='captcha' i]",                     # any id containing "captcha"
]


class LoginPage:
    """Encapsulate login-page selectors and user-facing login interactions.

    The CAPTCHA is solved automatically using OCR (Tesseract).  When OCR fails
    after ``max_captcha_attempts``, the page falls back to asking a human in
    the terminal (if ``allow_manual_captcha_fallback`` is enabled in settings).
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

    # ------------------------------------------------------------------
    # Navigation
    # ------------------------------------------------------------------

    def open(self) -> None:
        """Navigate to the configured login page."""
        self.logger.info("Opening configured login page.")
        self.page.goto(self.settings.base_url, wait_until="domcontentloaded")
        self.page.locator(self.settings.username_selector).wait_for(state="visible")

    # ------------------------------------------------------------------
    # Credentials
    # ------------------------------------------------------------------

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

    # ------------------------------------------------------------------
    # Automatic CAPTCHA solving
    # ------------------------------------------------------------------

    def _find_captcha_image_element(self):
        """Locate the CAPTCHA image element on the page.

        Tries a list of common selectors and also does a JS-based fallback
        search for any ``<img>`` whose src or id contains 'captcha'.
        """
        for selector in _CAPTCHA_IMAGE_SELECTORS:
            try:
                loc = self.page.locator(selector).first
                if loc.count() and loc.is_visible():
                    self.logger.info("Found CAPTCHA image via selector: %s", selector)
                    return loc
            except Exception:
                continue

        # JS fallback: find any visible <img> with 'captcha' in src/id/alt
        handle = self.page.evaluate_handle("""() => {
            const imgs = Array.from(document.querySelectorAll('img'));
            for (const img of imgs) {
                const attrs = [img.src, img.id, img.alt, img.className].join(' ').toLowerCase();
                if (attrs.includes('captcha') && img.getClientRects().length > 0) {
                    return img;
                }
            }
            return null;
        }""")
        element = handle.as_element()
        if element:
            self.logger.info("Found CAPTCHA image via JS fallback search.")
            return element

        self.logger.warning("Could not locate the CAPTCHA image element on the page.")
        return None

    def _screenshot_captcha_image(self) -> bytes | None:
        """Take a screenshot of just the CAPTCHA image element and return PNG bytes."""
        el = self._find_captcha_image_element()
        if el is None:
            return None
        try:
            return el.screenshot(type="png")
        except Exception as exc:
            self.logger.warning("Could not screenshot the CAPTCHA image: %s", exc)
            return None

    def _solve_captcha_ocr(self) -> str | None:
        """Attempt to read the CAPTCHA text using OCR.  Returns None on failure."""
        image_bytes = self._screenshot_captcha_image()
        if not image_bytes:
            return None
        try:
            text = _ocr_captcha_image(image_bytes, self.logger)
            if text:
                return text
            self.logger.warning("OCR returned empty text for the CAPTCHA image.")
            return None
        except Exception as exc:
            self.logger.warning("CAPTCHA OCR failed: %s", exc)
            return None

    def _refresh_captcha(self) -> None:
        """Click the CAPTCHA refresh button if one exists, otherwise do nothing.

        Many sites show a small refresh/reload icon next to the CAPTCHA image.
        """
        refresh_selectors = [
            "#btnRefreshCaptcha",
            "#imgRefreshCaptcha",
            "#refreshCaptcha",
            "a[onclick*='Captcha']",
            "img[onclick*='Captcha']",
            "[id*='refresh' i][id*='captcha' i]",
            ".captcha-refresh",
            "a.captcha-reload",
            "#imgCaptcha",  # clicking the image itself often refreshes it
        ]
        for selector in refresh_selectors:
            try:
                loc = self.page.locator(selector).first
                if loc.count() and loc.is_visible():
                    loc.click()
                    self.page.wait_for_timeout(1000)
                    self.logger.info("Refreshed CAPTCHA image via: %s", selector)
                    return
            except Exception:
                continue
        self.logger.debug("No CAPTCHA refresh button found; will rely on page reload.")

    def handle_captcha(self) -> None:
        """Automatically solve the CAPTCHA using OCR and fill the input field.

        Retries up to ``settings.max_captcha_attempts`` times.  If all OCR
        attempts fail and ``allow_manual_captcha_fallback`` is True, falls
        back to the original manual entry flow.
        """
        selector = self.settings.captcha_selector
        if not selector:
            self.logger.info("No CAPTCHA selector configured; continuing without CAPTCHA interaction.")
            return

        self.page.locator(selector).wait_for(state="visible")

        max_attempts = getattr(self.settings, "max_captcha_attempts", 10)
        allow_fallback = getattr(self.settings, "allow_manual_captcha_fallback", True)

        for attempt in range(1, max_attempts + 1):
            self.logger.info(
                "CAPTCHA OCR attempt %d of %d", attempt, max_attempts
            )
            captcha_text = self._solve_captcha_ocr()
            if captcha_text:
                # Clear any previous value and type the OCR result
                captcha_input = self.page.locator(selector)
                captcha_input.fill("")
                captcha_input.fill(captcha_text)
                self.logger.info(
                    "Filled CAPTCHA input with OCR result: '%s'", captcha_text
                )
                return  # Credentials + CAPTCHA filled → ready to submit

            # OCR failed — refresh the CAPTCHA and retry
            self.logger.warning(
                "CAPTCHA OCR attempt %d produced no usable text; refreshing.", attempt
            )
            self._refresh_captcha()
            self.page.wait_for_timeout(500)

        # All OCR attempts exhausted
        if allow_fallback:
            self.logger.warning(
                "All %d CAPTCHA OCR attempts failed. Falling back to manual entry.",
                max_attempts,
            )
            self._manual_captcha_fallback()
        else:
            raise RuntimeError(
                f"CAPTCHA could not be solved after {max_attempts} OCR attempts "
                "and manual fallback is disabled."
            )

    def _manual_captcha_fallback(self) -> None:
        """Fall back to the original manual CAPTCHA entry when OCR fails."""
        self.logger.info("Waiting for the CAPTCHA to be entered manually in the browser.")
        (self._captcha_wait or self._wait_for_terminal_confirmation)()

    # Keep the old method name so callers that referenced it directly still work.
    def handle_manual_captcha(self) -> None:
        """Backward-compatible alias — now attempts OCR first."""
        self.handle_captcha()

    @staticmethod
    def _wait_for_terminal_confirmation() -> None:
        """Default pause: ask the person at the terminal to press Enter when done."""
        try:
            input("Enter the CAPTCHA in the browser window, then press Enter here to continue... ")
        except EOFError as exc:
            raise RuntimeError(
                "Manual CAPTCHA entry needs an interactive terminal (for pytest, use -s)."
            ) from exc

    # ------------------------------------------------------------------
    # Form submission
    # ------------------------------------------------------------------

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
                    const err = document.querySelector('.validation-summary-errors, #lblError, [id*="Error"], [id*="error"], .alert-danger');
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
                const el = document.querySelector('.validation-summary-errors, #lblError, [id*="Error"], [id*="error"], .alert-danger');
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

    # ------------------------------------------------------------------
    # Login flow
    # ------------------------------------------------------------------

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
                self.handle_captcha()         # ← OCR-first, manual fallback
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

    # ------------------------------------------------------------------
    # Session persistence
    # ------------------------------------------------------------------

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

    # ------------------------------------------------------------------
    # Screenshots
    # ------------------------------------------------------------------

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

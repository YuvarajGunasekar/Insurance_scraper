# Website Login Automation Framework

A configurable, Python 3.12+ website login test framework using Playwright and
pytest. It is intended only for websites and test accounts you are authorized
to use. CAPTCHA challenges are never solved or bypassed by this project: when
a CAPTCHA selector is configured, the browser stays visible and a person enters
the CAPTCHA manually. Use an official test CAPTCHA configuration where the
site provides one.

## Project layout

```text
.
├── config/
│   ├── __init__.py          Configuration package marker
│   └── settings.py          .env loading, validation, selectors, and settings
├── pages/
│   ├── __init__.py          Page Object package marker
│   ├── base_page.py         Shared waits, navigation retries, and screenshots
│   └── login_page.py        Login Page Object and manual CAPTCHA flow
├── tests/
│   ├── __init__.py          Test package marker
│   ├── conftest.py          Pytest fixtures, browser lifecycle, failure screenshots
│   └── test_login.py        Configurable expected-success/expected-failure test
├── test_data/
│   ├── __init__.py          Non-secret test data package marker
│   └── README.md            Test-data and secret-handling guidance
├── utilities/
│   ├── __init__.py          Utility package marker
│   └── logging_config.py    Backward-compatible logging import
├── utils/
│   ├── logger.py            Console and rotating-file logging setup
│   ├── retry.py             Temporary network error classification
│   └── download_handler.py  Bounded file fetching and completion checks
├── screenshots/             Login-outcome and pytest-failure screenshots
├── reports/                 Self-contained pytest HTML report
├── logs/                    Rotating automation log
├── .env.example             Safe template for local settings
├── .gitignore               Excludes credentials and generated artifacts
├── pytest.ini               Test discovery and HTML report options
├── requirements.txt         Python dependencies
└── README.md                Setup and execution guide
```

## Setup in VS Code (Windows)

1. Open this project folder in VS Code and open **Terminal → New Terminal**.
2. Create and activate a virtual environment:

   ```powershell
   py -3.12 -m venv .venv
   .\.venv\Scripts\Activate.ps1
   ```

   If PowerShell blocks activation, use the VS Code Python interpreter picker to
   select `.venv\Scripts\python.exe`, then open a new terminal.
3. Install Python dependencies and the Chromium browser:

   ```powershell
   .\.venv\Scripts\python.exe -m pip install --upgrade pip
   .\.venv\Scripts\python.exe -m pip install -r requirements.txt
   .\.venv\Scripts\python.exe -m playwright install chromium
   ```
4. Copy `.env.example` to `.env` in the project root. For example:

   ```powershell
   Copy-Item .env.example .env
   ```
5. Edit `.env` with the authorized login URL, test credentials, and selectors
   matching that site's login page. Never commit `.env`; `.gitignore` excludes it.
   Configure `SUCCESS_SELECTOR` to match an element visible only after a
   successful login. Configure `FAILURE_SELECTOR` to match the site's visible
   authentication error when available. Selectors in this project use
   Playwright locator syntax.
6. In VS Code, select the interpreter at `.venv\Scripts\python.exe` if it is not
   already selected. Run the test from the integrated terminal:

   ```powershell
   .\.venv\Scripts\python.exe -m pytest -s
   ```

   The browser is headed by default. If `CAPTCHA_SELECTOR` is set, the test
   waits for that field, prompts in the terminal, and waits while you enter the
   CAPTCHA directly into the browser. Press Enter in the terminal to continue.
   Use an interactive terminal; do not enable headless mode for manual CAPTCHA
   entry.

## Configuration

| Variable | Required | Purpose |
| --- | --- | --- |
| `BASE_URL` | Yes | Absolute `http://` or `https://` login URL |
| `USERNAME` / `PASSWORD` | Yes | Authorized test-account credentials, kept in `.env` |
| `USERNAME_SELECTOR` | No | Username field locator; default `input[name="username"]` |
| `PASSWORD_SELECTOR` | No | Password field locator; default `input[name="password"]` |
| `LOGIN_BUTTON_SELECTOR` | No | Submit button locator; default `button[type="submit"]` |
| `SUCCESS_SELECTOR` | Yes in practice | Locator visible after successful authentication |
| `FAILURE_SELECTOR` | No | Locator visible when authentication is rejected |
| `CAPTCHA_SELECTOR` | No | CAPTCHA input locator; blank disables the manual step |
| `HEADLESS` | No | `false` by default; must remain false for manual CAPTCHA |
| `BROWSER` | No | `chromium`, `firefox`, or `webkit` (default `chromium`) |
| `TIMEOUT_MS` | No | Playwright explicit-wait timeout, default `10000` |
| `NAVIGATION_TIMEOUT_MS` | No | Page load timeout, default `30000` |
| `DOWNLOAD_TIMEOUT_MS` | No | File-fetch timeout, default `60000` |
| `MAX_ATTEMPTS` | No | Total safe navigation/download attempts, from 1 to 3; default `3` |
| `RETRY_BACKOFF_SECONDS` | No | Initial exponential retry delay, from 0 to 30 seconds; default `1` |
| `EXPECTED_LOGIN_SUCCESS` | No | `true` by default; set `false` for an authorized negative test |

For an unsuccessful-login test, use a test account or an intentionally invalid
credential scenario permitted by the site owner, set `EXPECTED_LOGIN_SUCCESS=false`,
and ensure the failure marker is configured. Do not use this framework to probe
accounts or test against sites without authorization.

The CAPTCHA field is site-specific. Set `CAPTCHA_SELECTOR` to its actual input
locator to activate the manual step. If an official test environment disables
CAPTCHA, leave the selector blank. No CAPTCHA solver, bypass, or anti-bot
evasion is included.

## Browser waits and file fetching

Navigation waits for the browser `load` event, with a bounded timeout. Only
navigation and file-fetch network timeouts or recognized temporary network
errors are retried; login submission, invalid credentials, and CAPTCHA outcomes
are never retried. Backoff doubles after each retry, and total attempts are
limited to three.

The reusable `download_file` helper uses the Playwright browser context's
authenticated request client. It buffers the complete response, writes to a
temporary file, verifies a non-empty result, then atomically moves it to the
destination. Example inside a test with an available `page`, `settings`, and
`logger` fixture:

```python
from config.settings import PROJECT_ROOT
from utils.download_handler import download_file

download_file(
   page,
   file_url,
   PROJECT_ROOT / "reports" / "export.csv",
   timeout_ms=settings.download_timeout_ms,
   max_attempts=settings.max_attempts,
   backoff_seconds=settings.retry_backoff_seconds,
   logger=logger,
)
```

Timeouts and failed fetches are recorded with an operation and URL in
`logs/automation.log`; a page screenshot is attempted when available.

## Outputs

- `logs/automation.log`: timestamped application log with credential values
  omitted; the log rotates at a size limit.
- `screenshots/login-success-*.png` and `screenshots/login-failure-*.png`:
  full-page screenshots after the observed login result.
- `screenshots/pytest-failure-*.png`: extra screenshot captured if a test fails
  while a browser page is available.
- `reports/report.html`: self-contained pytest HTML report.

Artifacts may contain sensitive page information. Review and protect them as
carefully as test data; do not commit them. The generated artifact contents are
ignored by Git.

## Useful commands

Run all tests and generate the HTML report (configured by `pytest.ini`):

```powershell
.\.venv\Scripts\python.exe -m pytest -s
```

Run only the login test:

```powershell
.\.venv\Scripts\python.exe -m pytest tests/test_login.py -s
```

Run without capturing terminal output, which is useful for interactive prompts:

```powershell
.\.venv\Scripts\python.exe -m pytest -s
```

`-s` is recommended when entering CAPTCHA manually so the terminal prompt is
shown immediately.

Run the local download/retry unit tests without opening a browser:

```powershell
.\.venv\Scripts\python.exe -m pytest tests/test_base_page.py tests/test_download_handler.py
```

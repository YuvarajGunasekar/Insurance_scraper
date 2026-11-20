# Scraping Trigger API

This API is a separate integration layer. The current workspace contains a login Page Object but no reusable website-scraping entry-point function, so the API does not invoke the login flow. Configure `SCRAPER_CALLABLE` to point to an existing authorized scraper before submitting jobs. Until then, health checks work and `POST /api/scrape` returns HTTP 503 with a configuration message.

For interactive sign-in, the API also provides a separate manual-CAPTCHA login
workflow. It opens a visible browser, fills URL and credentials from the request,
waits for the user to enter CAPTCHA in the browser, and continues only after the
explicit continue endpoint is called. It does not invoke OCR or solve CAPTCHA.

## Existing scraper contract

The configured callable must be importable from the project root and accept the request fields as keyword arguments:

```python
scrape(
  website_url="https://allowed.example/path",
  username="provided-in-request",
  password="provided-in-request",
  scrape_type="full",
)
```

Set `SCRAPER_CALLABLE` to `python.module:function_name`. For example, for an existing `collect.py` module with a `scrape` function, use `collect:scrape`. The API does not create, change, or wrap the implementation. If the existing callable has a different interface, do not point this API at the login workflow; provide a dedicated adapter as a new module or request approval to change an existing file.

## Install and run (PowerShell)

From the project directory:

```powershell
.\.venv\Scripts\python.exe -m pip install -r requirements-api.txt
$env:SCRAPER_CALLABLE = "python.module:function_name"
$env:API_ALLOWED_HOSTS = "allowed.example,another-allowed.example"
.\.venv\Scripts\python.exe -m uvicorn api.main:app --host 127.0.0.1 --port 8000 --reload
```

Use exact hostnames in `API_ALLOWED_HOSTS`. Only HTTPS URLs on those hosts are accepted. Private and non-public IP addresses, embedded URL credentials, and unlisted hosts are rejected. Keep the API bound to `127.0.0.1`; this local API has no caller authentication and must not be exposed to an untrusted network.

## Endpoints

### Health

```http
GET /api/health
```

Example response:

```json
{
  "status": "ok",
  "scraper_configured": true,
  "allowed_hosts_configured": true
}
```

### Start a scrape

```http
POST /api/scrape
Content-Type: application/json
```

Request:

```json
{
  "website_url": "https://allowed.example/path",
  "username": "authorized-test-user",
  "password": "your-secret-password",
  "scrape_type": "full"
}
```

Credentials are passed only to the configured callable. They are not written
to task status, application logs, or API response models. Keep requests local;
validation errors also omit submitted values. Do not expose this unauthenticated
API to a network.

Accepted response (`202`):

```json
{
  "task_id": "4e5e3b2b-6377-4419-9bce-e9ac130d188a",
  "status": "pending",
  "deduplicated": false
}
```

If the same URL, scrape type, and credentials are already pending or running,
the active task ID is returned with `deduplicated: true`. Credentials are
compared using a process-local keyed digest; plaintext values are not stored in
the task record. Missing scraper or allowlist configuration returns `503`; a
host outside the allowlist returns `403`.

### Check status

```http
GET /api/scrape/status/4e5e3b2b-6377-4419-9bce-e9ac130d188a
```

Status is `pending`, `running`, `completed`, or `failed`. Task state is held in memory and is lost when the process restarts; use one Uvicorn worker for this in-memory implementation.

### Manual login with CAPTCHA

Start the browser login:

```http
POST /api/login
Content-Type: application/json
```

```json
{
  "website_url": "https://allowed.example/login",
  "username": "authorized-test-user",
  "password": "your-secret-password"
}
```

The API returns a `task_id`. A visible browser opens with the URL and credentials
filled. Enter the CAPTCHA yourself in that browser. Wait until
`GET /api/login/status/{task_id}` reports `awaiting_captcha`, then call:

```http
POST /api/login/continue/{task_id}
```

The browser submits the form and the status endpoint reports `completed` or
`failed`. CAPTCHA values are never sent through the API. Credentials are not
stored in task status or API logs. Keep the API bound to loopback.

## Interactive documentation

Open <http://127.0.0.1:8000/docs> for Swagger UI. For manual login, submit the
body to `POST /api/login`, enter CAPTCHA in the opened browser, call the continue
endpoint, and poll login status. The Postman collection in
`api/postman_collection.json` includes the same sequence.

API logs are written separately to `logs/api.log`; the existing automation log and project files are not changed by the API layer.

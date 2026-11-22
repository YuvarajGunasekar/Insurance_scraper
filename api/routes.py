"""API routes for health, scrape submission, and task status."""

from __future__ import annotations

from functools import partial
from uuid import UUID

from fastapi import APIRouter, HTTPException, Request, status
import psycopg2
import psycopg2.extras

from api.schemas import (
    HealthResponse,
    LoginAccepted,
    LoginContinueResponse,
    LoginRequest,
    LoginResponse,
    LoginStatusResponse,
    ScrapeAccepted,
    ScrapeInputCreate,
    ScrapeInputResponse,
    ScrapeRequest,
    ScrapeStatusResponse,
)
from db.scrape_input_client import (
    get_latest_scrape_input,
    get_scrape_input_by_id,
    list_scrape_inputs,
    save_scrape_input,
)
from api.service import (
    ScraperConfigurationError,
    ScraperNotConfiguredError,
    TargetNotAllowedError,
    allowed_hosts_are_configured,
    credential_fingerprint,
    get_api_logger,
    get_scrape_website_url,
    load_scraper_callable,
    run_scraper,
    scraper_is_configured,
    validate_target_url,
)
from api.task_manager import TaskManager
from api.login_tasks import LoginTaskError, LoginTaskManager, LoginTaskStatus


router = APIRouter(prefix="/api")


@router.get("/health", response_model=HealthResponse)
def health_check() -> HealthResponse:
    """Report API availability and whether integration settings exist."""
    return HealthResponse(
        scraper_configured=scraper_is_configured(),
        allowed_hosts_configured=allowed_hosts_are_configured(),
    )


@router.post(
    "/scrape",
    response_model=ScrapeAccepted,
    status_code=status.HTTP_202_ACCEPTED,
)
def start_scrape(
    payload: ScrapeRequest, request: Request
) -> ScrapeAccepted:
    """Queue an existing scraper function in a background worker."""
    logger = get_api_logger()
    website_url = ""
    try:
        website_url = get_scrape_website_url()
        validate_target_url(website_url)
        scraper = load_scraper_callable()
    except ScraperNotConfiguredError as exc:
        logger.error(
            "Scrape request rejected | operation=submit_scrape | url=%s | error=%s",
            website_url,
            exc,
        )
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail=str(exc),
        ) from exc
    except ScraperConfigurationError as exc:
        logger.exception(
            "Scraper configuration error | operation=submit_scrape | url=%s",
            website_url,
        )
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail=str(exc),
        ) from exc
    except TargetNotAllowedError as exc:
        logger.warning(
            "Scrape target rejected | operation=submit_scrape | url=%s | error=%s",
            website_url,
            exc,
        )
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail=str(exc),
        ) from exc

    manager: TaskManager = request.app.state.task_manager
    password = payload.password.get_secret_value()
    work = partial(
        run_scraper,
        scraper,
        website_url,
        payload.username,
        password,
        "full",
    )
    submission = manager.submit(
        website_url,
        "full",
        work,
        dedupe_token=credential_fingerprint(payload.username, password),
    )
    if submission.deduplicated:
        # A scrape is already running or just finished. Return 409 so the
        # caller knows to check the existing task status instead of retrying.
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail={
                "message": "A scrape task is already running or in cooldown. Please wait.",
                "task_id": str(submission.task.task_id),
                "current_status": submission.task.status.value,
            },
        )
    return ScrapeAccepted(
        task_id=submission.task.task_id,
        status=submission.task.status.value,
        deduplicated=False,
    )


@router.get(
    "/scrape/status/{task_id}", response_model=ScrapeStatusResponse
)
def scrape_status(task_id: UUID, request: Request) -> ScrapeStatusResponse:
    """Return the in-memory state for a previously accepted task."""
    manager: TaskManager = request.app.state.task_manager
    task = manager.get(task_id)
    if task is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Scrape task was not found. Tasks are kept in memory per API process.",
        )
    return ScrapeStatusResponse(
        task_id=task.task_id,
        website_url=task.website_url,
        scrape_type=task.scrape_type,
        status=task.status.value,
        created_at=task.created_at,
        updated_at=task.updated_at,
        result_summary=task.result_summary,
        error=task.error,
    )


@router.post(
    "/login",
    response_model=LoginAccepted,
    status_code=status.HTTP_202_ACCEPTED,
)
def start_login(payload: LoginRequest, request: Request) -> LoginAccepted:
    """Start an automated login session and return its task immediately.

    The browser runs in a background thread. Poll the task status endpoint for
    the final login outcome instead of blocking this request.
    """
    logger = get_api_logger()
    website_url = str(payload.website_url)
    try:
        validate_target_url(website_url)
        manager: LoginTaskManager = request.app.state.login_task_manager
        submission = manager.start_login(
            website_url,
            payload.username,
            payload.password.get_secret_value(),
        )
    except TargetNotAllowedError as exc:
        logger.warning(
            "Login target rejected | operation=start_login | url=%s | error=%s",
            website_url,
            exc,
        )
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail=str(exc),
        ) from exc
    except (ScraperNotConfiguredError, LoginTaskError) as exc:
        logger.error(
            "Login request rejected | operation=start_login | url=%s | error=%s",
            website_url,
            exc,
        )
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail=str(exc),
        ) from exc

    if submission.deduplicated:
        # A login for the same credentials is already running; wait for that one.
        logger.info(
            "Deduplication hit for task_id=%s; waiting for existing task.",
            submission.task.task_id,
        )

    return LoginAccepted(
        task_id=submission.task.task_id,
        status=submission.task.status.value,
        deduplicated=submission.deduplicated,
    )


@router.post(
    "/login/continue/{task_id}", response_model=LoginContinueResponse
)
def continue_login(task_id: UUID, request: Request) -> LoginContinueResponse:
    """Continue the browser login after manual CAPTCHA entry in the browser."""
    manager: LoginTaskManager = request.app.state.login_task_manager
    try:
        task = manager.continue_login(task_id)
    except LoginTaskError as exc:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=str(exc),
        ) from exc
    if task is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Login task was not found.",
        )
    return LoginContinueResponse(
        task_id=task.task_id,
        status=LoginTaskStatus.AWAITING_CAPTCHA.value,
        message="Continue signal sent. Check the login task status for the result.",
    )


@router.get("/login/status/{task_id}", response_model=LoginStatusResponse)
def login_status(task_id: UUID, request: Request) -> LoginStatusResponse:
    """Return login-task state without returning username, password, or CAPTCHA."""
    manager: LoginTaskManager = request.app.state.login_task_manager
    task = manager.get(task_id)
    if task is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Login task was not found.",
        )
    return LoginStatusResponse(
        task_id=task.task_id,
        website_url=task.website_url,
        status=task.status.value,
        created_at=task.created_at,
        updated_at=task.updated_at,
        message=task.message,
        login_succeeded=task.login_succeeded,
        error=task.error,
    )



# ---------------------------------------------------------------------------
# scrape-input CRUD — manage the DB rows that drive the scraper
# ---------------------------------------------------------------------------

@router.post(
    "/scrape-input",
    response_model=ScrapeInputResponse,
    status_code=status.HTTP_201_CREATED,
)
def create_scrape_input(payload: ScrapeInputCreate) -> ScrapeInputResponse:
    """Create a new scrape_input row (future scrapes read from the latest active row)."""
    data = payload.model_dump()
    new_id = save_scrape_input(data)
    row = get_scrape_input_by_id(new_id)
    if row is None:
        raise HTTPException(status_code=500, detail="Failed to retrieve created row.")
    return ScrapeInputResponse(**row)


@router.get("/scrape-input/latest", response_model=ScrapeInputResponse)
def get_latest_scrape_input_route() -> ScrapeInputResponse:
    """Return the most-recently-created active scrape_input row."""
    row = get_latest_scrape_input()
    if row is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="No active scrape_input row found. POST to /api/scrape-input to create one.",
        )
    return ScrapeInputResponse(**row)


@router.get("/scrape-input/{input_id}", response_model=ScrapeInputResponse)
def get_scrape_input_by_id_route(input_id: int) -> ScrapeInputResponse:
    """Return a specific scrape_input row by id."""
    row = get_scrape_input_by_id(input_id)
    if row is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"scrape_input id={input_id} not found.",
        )
    return ScrapeInputResponse(**row)


@router.get("/scrape-input", response_model=list[ScrapeInputResponse])
def list_scrape_inputs_route(limit: int = 20) -> list[ScrapeInputResponse]:
    """List recent scrape_input rows (newest first)."""
    rows = list_scrape_inputs(limit=limit)
    return [ScrapeInputResponse(**r) for r in rows]


@router.patch("/scrape-input/{input_id}/activate", response_model=ScrapeInputResponse)
def activate_scrape_input(input_id: int) -> ScrapeInputResponse:
    """Set is_active=TRUE for a quote so the scraper will use it next run."""
    import psycopg2
    from db.scrape_input_client import _pg_config
    try:
        conn = psycopg2.connect(**_pg_config())
        cur = conn.cursor()
        cur.execute(
            "UPDATE insurance_quote SET is_active=TRUE, updated_at=NOW() WHERE id=%s RETURNING id;",
            (input_id,),
        )
        if cur.fetchone() is None:
            conn.rollback()
            conn.close()
            raise HTTPException(status_code=404, detail=f"Quote id={input_id} not found.")
        conn.commit()
        conn.close()
    except HTTPException:
        raise
    except Exception as exc:
        raise HTTPException(status_code=500, detail=str(exc)) from exc
    row = get_scrape_input_by_id(input_id)
    return ScrapeInputResponse(**row)


# ---------------------------------------------------------------------------
# /api/scrape-session — structured (nested) endpoint for normalized schema
# ---------------------------------------------------------------------------
from api.schemas import ScrapeSessionCreate


@router.post(
    "/scrape-session",
    response_model=ScrapeInputResponse,
    status_code=status.HTTP_201_CREATED,
)
def create_scrape_session(payload: ScrapeSessionCreate) -> ScrapeInputResponse:
    """
    Create a new scrape session using the structured (normalized) format.

    Accepts a nested JSON body with sub-objects for vehicle, covers, customer,
    address, and flow. Internally stored across 6 normalized DB tables linked
    by foreign keys. Returns the same response as POST /api/scrape-input.
    """
    flat_data = payload.to_flat()
    new_id = save_scrape_input(flat_data)
    row = get_scrape_input_by_id(new_id)
    if row is None:
        raise HTTPException(status_code=500, detail="Failed to retrieve created session.")
    return ScrapeInputResponse(**row)


# ---------------------------------------------------------------------------
# Relational Quotes API — inspect the 7 normalized tables connected by FK
# ---------------------------------------------------------------------------
@router.get("/quotes")
def list_insurance_quotes(limit: int = 20) -> list[dict]:
    """List scraped insurance quotes from the relational master table."""
    from db.postgres_client import _get_conn
    with _get_conn() as conn:
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            cur.execute("""
                SELECT q.id, q.registration_number, q.policy_start_date, q.proposal_number,
                       q.trace_id, q.business_type, q.scraped_at,
                       v.make_model_variant, v.rto_location,
                       p.total_premium, p.net_premium, p.idv
                FROM insurance_quote q
                LEFT JOIN vehicle_details v ON v.quote_id = q.id
                LEFT JOIN premium_details p ON p.quote_id = q.id
                ORDER BY q.scraped_at DESC
                LIMIT %s;
            """, (limit,))
            return [dict(r) for r in cur.fetchall()]


@router.get("/quotes/{quote_id}")
def get_insurance_quote_complete(quote_id: int) -> dict:
    """Get the full policy quote joined across all 7 relational tables."""
    from db.postgres_client import _get_conn
    with _get_conn() as conn:
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            cur.execute("SELECT * FROM v_complete_insurance_quote WHERE quote_id = %s;", (quote_id,))
            row = cur.fetchone()
            if not row:
                raise HTTPException(status_code=404, detail=f"Insurance quote id={quote_id} not found.")
            return dict(row)


@router.get("/quotes/{quote_id}/sections")
def get_insurance_quote_sections(quote_id: int) -> dict:
    """Get the policy quote broken down into its individual form sections."""
    from db.postgres_client import _get_conn
    with _get_conn() as conn:
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            cur.execute("SELECT * FROM insurance_quote WHERE id = %s;", (quote_id,))
            quote = cur.fetchone()
            if not quote:
                raise HTTPException(status_code=404, detail=f"Quote id={quote_id} not found.")

            cur.execute("SELECT * FROM vehicle_details WHERE quote_id = %s;", (quote_id,))
            vehicle = cur.fetchone()

            cur.execute("SELECT * FROM cover_details WHERE quote_id = %s;", (quote_id,))
            covers = cur.fetchone()

            cur.execute("SELECT * FROM customer_details WHERE quote_id = %s;", (quote_id,))
            customer = cur.fetchone()

            cur.execute("SELECT * FROM nominee_details WHERE quote_id = %s;", (quote_id,))
            nominee = cur.fetchone()

            cur.execute("SELECT * FROM address_details WHERE quote_id = %s;", (quote_id,))
            address = cur.fetchone()

            cur.execute("SELECT * FROM premium_details WHERE quote_id = %s;", (quote_id,))
            premium = cur.fetchone()

            return {
                "quote": dict(quote) if quote else None,
                "vehicle_details": dict(vehicle) if vehicle else None,
                "cover_details": dict(covers) if covers else None,
                "customer_details": dict(customer) if customer else None,
                "nominee_details": dict(nominee) if nominee else None,
                "address_details": dict(address) if address else None,
                "premium_details": dict(premium) if premium else None,
            }

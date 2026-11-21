"""API routes for health, scrape submission, and task status."""

from __future__ import annotations

from functools import partial
from uuid import UUID

from fastapi import APIRouter, HTTPException, Request, status

from api.schemas import (
    HealthResponse,
    LoginAccepted,
    LoginContinueResponse,
    LoginRequest,
    LoginResponse,
    LoginStatusResponse,
    ScrapeAccepted,
    ScrapeRequest,
    ScrapeStatusResponse,
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
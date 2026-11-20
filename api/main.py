"""FastAPI application for triggering an existing authorized scraper."""

from __future__ import annotations

from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse

from api.routes import router
from api.service import get_api_logger
from api.login_tasks import LoginTaskManager
from api.task_manager import TaskManager


@asynccontextmanager
async def lifespan(app: FastAPI):
    logger = get_api_logger()
    app.state.task_manager = TaskManager(logger)
    app.state.login_task_manager = LoginTaskManager(logger)
    logger.info("API server started | operation=start_api")
    try:
        yield
    finally:
        app.state.task_manager.shutdown()
        app.state.login_task_manager.shutdown()
        logger.info("API server stopped | operation=stop_api")


app = FastAPI(
    title="Authorized Scraping Trigger API",
    version="1.0.0",
    description=(
        "Triggers an existing scraper callable configured by the operator. "
        "Only explicitly allowlisted HTTPS hostnames are accepted."
    ),
    lifespan=lifespan,
)
app.include_router(router)


@app.exception_handler(RequestValidationError)
async def redact_validation_input(request, exc: RequestValidationError) -> JSONResponse:
    """Return validation details without echoing submitted credential values."""
    safe_errors = []
    has_invalid_json = any(error.get("type") == "json_invalid" for error in exc.errors())
    for error in exc.errors():
        safe_errors.append(
            {
                "type": error.get("type"),
                "loc": error.get("loc"),
                "msg": "Send a valid JSON object." if has_invalid_json else error.get("msg"),
            }
        )
    get_api_logger().warning(
        "Request validation failed | operation=validate_request | path=%s | errors=%s",
        request.url.path,
        safe_errors,
    )
    return JSONResponse(status_code=422, content={"detail": safe_errors})
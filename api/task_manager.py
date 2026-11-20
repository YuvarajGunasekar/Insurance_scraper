"""Thread-safe, in-memory task registry for background scraper jobs."""

from __future__ import annotations

import logging
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, replace
from datetime import datetime, timezone
from enum import StrEnum
from threading import Lock
from typing import Callable
from uuid import UUID, uuid4


class TaskStatus(StrEnum):
    PENDING = "pending"
    RUNNING = "running"
    COMPLETED = "completed"
    FAILED = "failed"


@dataclass(slots=True)
class ScrapeTask:
    task_id: UUID
    website_url: str
    scrape_type: str
    status: TaskStatus
    created_at: datetime
    updated_at: datetime
    result_summary: str | None = None
    error: str | None = None


@dataclass(frozen=True, slots=True)
class TaskSubmission:
    task: ScrapeTask
    deduplicated: bool


class TaskManager:
    """Queue background scraper jobs through a single worker thread."""

    def __init__(self, logger: logging.Logger) -> None:
        self._logger = logger
        # max_workers=1 ensures submissions execute sequentially.
        self._executor = ThreadPoolExecutor(
            max_workers=1, thread_name_prefix="scrape-task"
        )
        self._lock = Lock()
        self._tasks: dict[UUID, ScrapeTask] = {}
        self._running_task_id: UUID | None = None
        self._last_completed_at: datetime | None = None
        self._last_completed_task_id: UUID | None = None

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def submit(
        self,
        website_url: str,
        scrape_type: str,
        work: Callable[[], str],
        *,
        dedupe_token: str,  # kept for API compatibility; not used for dedup
    ) -> TaskSubmission:
        """Accept at most one scrape job at a time.

        Returns the existing active/recently-completed task when busy.
        """
        with self._lock:
            now = datetime.now(timezone.utc)

            if self._running_task_id is not None:
                active = self._tasks.get(self._running_task_id)
                if active and active.status in {TaskStatus.PENDING, TaskStatus.RUNNING}:
                    self._logger.info(
                        "Scrape already active | task_id=%s | status=%s",
                        active.task_id,
                        active.status,
                    )
                    return TaskSubmission(replace(active), deduplicated=True)

            if (
                self._last_completed_at is not None
                and self._last_completed_task_id is not None
                and (now - self._last_completed_at).total_seconds() < 5
            ):
                completed = self._tasks.get(self._last_completed_task_id)
                if completed:
                    return TaskSubmission(replace(completed), deduplicated=True)

            # --- Accept the new task ---
            task = ScrapeTask(
                task_id=uuid4(),
                website_url=website_url,
                scrape_type=scrape_type,
                status=TaskStatus.PENDING,
                created_at=now,
                updated_at=now,
            )
            self._tasks[task.task_id] = task
            self._running_task_id = task.task_id
            self._executor.submit(self._execute, task.task_id, work)
            self._logger.info(
                "Scrape task accepted | task_id=%s | url=%s | type=%s",
                task.task_id,
                website_url,
                scrape_type,
            )
            return TaskSubmission(replace(task), deduplicated=False)

    def get(self, task_id: UUID) -> ScrapeTask | None:
        """Return a snapshot of a task, if it exists."""
        with self._lock:
            task = self._tasks.get(task_id)
            return replace(task) if task else None

    def shutdown(self) -> None:
        """Stop accepting jobs and cancel work that has not started."""
        self._executor.shutdown(wait=False, cancel_futures=True)

    # ------------------------------------------------------------------
    # Internal
    # ------------------------------------------------------------------

    def _execute(self, task_id: UUID, work: Callable[[], str]) -> None:
        """Run a task in the background thread and update its status."""
        with self._lock:
            task = self._tasks[task_id]
            task.status = TaskStatus.RUNNING
            task.updated_at = datetime.now(timezone.utc)
            self._running_task_id = task_id

        self._logger.info(
            "Scrape task started | operation=scrape | task_id=%s | url=%s",
            task_id,
            self._tasks[task_id].website_url,
        )

        try:
            result_summary = work()
        except Exception as exc:
            self._logger.exception(
                "Scrape task failed | operation=scrape | task_id=%s | error=%s",
                task_id,
                exc,
            )
            with self._lock:
                task = self._tasks[task_id]
                task.status = TaskStatus.FAILED
                task.error = str(exc)[:1000]
                task.updated_at = datetime.now(timezone.utc)
                self._last_completed_task_id = task_id
                self._last_completed_at = task.updated_at
                self._running_task_id = None
        else:
            with self._lock:
                task = self._tasks[task_id]
                task.status = TaskStatus.COMPLETED
                task.result_summary = result_summary[:1000]
                task.updated_at = datetime.now(timezone.utc)
                self._last_completed_task_id = task_id
                self._last_completed_at = task.updated_at
                self._running_task_id = None
            self._logger.info(
                "Scrape task completed | operation=scrape | task_id=%s | url=%s",
                task_id,
                self._tasks[task_id].website_url,
            )
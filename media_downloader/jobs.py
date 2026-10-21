"""Background download jobs: state machine, progress and a worker pool.

Status transitions are explicit and validated::

    queued ──► running ──► completed
       │          ├──────► failed
       │          └──────► cancelled
       ├──────────────────► cancelled   (cancelled before it started)
       └──────────────────► failed      (e.g. rejected before it started)

``completed`` is only set by the pipeline after the final file exists and has
been verified, never earlier.
"""

from __future__ import annotations

import threading
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from enum import Enum
from pathlib import Path
from typing import Any, Callable

from .errors import AppError, JobCancelled, classify_error
from .logging_setup import get_logger
from .storage import new_job_id

log = get_logger("jobs")


class JobStatus(str, Enum):
    QUEUED = "queued"
    RUNNING = "running"
    COMPLETED = "completed"
    FAILED = "failed"
    CANCELLED = "cancelled"


TERMINAL_STATUSES = frozenset({JobStatus.COMPLETED, JobStatus.FAILED, JobStatus.CANCELLED})
ALLOWED_TRANSITIONS: dict[JobStatus, frozenset[JobStatus]] = {
    JobStatus.QUEUED: frozenset({JobStatus.RUNNING, JobStatus.CANCELLED, JobStatus.FAILED}),
    JobStatus.RUNNING: frozenset({JobStatus.COMPLETED, JobStatus.FAILED, JobStatus.CANCELLED}),
    JobStatus.COMPLETED: frozenset(),
    JobStatus.FAILED: frozenset(),
    JobStatus.CANCELLED: frozenset(),
}

STAGE_LABELS = {
    "queued": "Waiting in queue",
    "retrieving": "Retrieving information",
    "preparing": "Preparing download",
    "downloading_video": "Downloading video",
    "downloading_audio": "Downloading audio",
    "merging": "Merging audio and video",
    "converting_video": "Converting to H.264",
    "converting_mp3": "Converting to MP3",
    "finalizing": "Finalizing file",
    "completed": "Completed",
    "failed": "Failed",
    "cancelled": "Cancelled",
}

DEFAULT_STEPS = {
    "mp4": ["retrieving", "preparing", "downloading_video", "downloading_audio", "merging", "finalizing"],
    "mp3": ["retrieving", "preparing", "downloading_audio", "converting_mp3", "finalizing"],
}


class InvalidTransition(RuntimeError):
    pass


@dataclass(frozen=True)
class JobRequest:
    video_id: str
    url: str
    kind: str  # "mp4" | "mp3"
    quality: str
    convert_h264: bool = False

    @property
    def key(self) -> tuple[str, str, str, bool]:
        return self.video_id, self.kind, self.quality, self.convert_h264 and self.kind == "mp4"


class Job:
    def __init__(self, job_id: str, request: JobRequest, *, title: str | None = None,
                 thumbnail: str | None = None) -> None:
        self.id = job_id
        self.request = request
        self.title = title
        self.thumbnail = thumbnail
        self.label: str | None = None
        self.status = JobStatus.QUEUED
        self.stage = "queued"
        self.last_stage = "queued"  # stage that was active when the job ended
        self.stage_detail: str | None = None
        self.steps: list[str] = list(DEFAULT_STEPS.get(request.kind, []))
        self.percent: float | None = None
        self.downloaded_bytes: int | None = None
        self.total_bytes: int | None = None
        self.total_is_estimate = False
        self.speed: float | None = None
        self.eta: float | None = None
        self.result_path: Path | None = None
        self.result_size: int | None = None
        self.error: dict[str, Any] | None = None
        self.plan_summary: dict[str, Any] | None = None
        self.cancel_event = threading.Event()
        self.created_at = time.time()
        self.started_at: float | None = None
        self.finished_at: float | None = None
        self.updated_at = self.created_at
        self.timed_out = False
        self._lock = threading.RLock()

    # -- state ------------------------------------------------------------ #
    @property
    def is_terminal(self) -> bool:
        return self.status in TERMINAL_STATUSES

    def transition(self, new_status: JobStatus) -> None:
        with self._lock:
            if new_status not in ALLOWED_TRANSITIONS[self.status]:
                raise InvalidTransition(f"{self.status.value} -> {new_status.value}")
            self.status = new_status
            now = time.time()
            self.updated_at = now
            if new_status == JobStatus.RUNNING:
                self.started_at = now
            if new_status in TERMINAL_STATUSES:
                self.finished_at = now
                self.last_stage = self.stage
                self.stage = new_status.value
                self.speed = None
                self.eta = None

    def start(self) -> bool:
        """Move queued -> running. False if the job was cancelled meanwhile."""
        with self._lock:
            if self.status != JobStatus.QUEUED:
                return False
            self.transition(JobStatus.RUNNING)
            return True

    def complete(self, path: Path, size: int) -> None:
        with self._lock:
            self.result_path = path
            self.result_size = size
            self.percent = 100.0
            self.stage_detail = None
            self.transition(JobStatus.COMPLETED)

    def fail(self, error: dict[str, Any]) -> None:
        with self._lock:
            if self.is_terminal:
                return
            self.error = error
            self.stage_detail = None
            self.transition(JobStatus.FAILED)

    def mark_cancelled(self) -> None:
        with self._lock:
            if self.is_terminal:
                return
            self.stage_detail = None
            self.transition(JobStatus.CANCELLED)

    def request_cancel(self) -> bool:
        """Ask the job to stop. Returns False if it had already finished."""
        with self._lock:
            if self.is_terminal:
                return False
            self.cancel_event.set()
            if self.status == JobStatus.QUEUED:
                self.transition(JobStatus.CANCELLED)
            else:
                self.stage_detail = "Cancelling…"
                self.updated_at = time.time()
            return True

    def expire(self) -> None:
        """Stop a job that ran too long. It ends as failed, not cancelled."""
        with self._lock:
            if self.is_terminal:
                return
            self.timed_out = True
            self.cancel_event.set()
            self.stage_detail = "Stopping: time limit reached…"
            self.updated_at = time.time()

    def check_cancelled(self) -> None:
        if self.cancel_event.is_set():
            raise JobCancelled()

    # -- progress ----------------------------------------------------------- #
    def set_stage(self, stage: str, *, detail: str | None = None, percent: float | None = None) -> None:
        with self._lock:
            if self.is_terminal:
                return
            self.stage = stage
            self.stage_detail = "Cancelling…" if self.cancel_event.is_set() else detail
            self.percent = percent
            self.speed = None
            self.eta = None
            if stage not in self.steps and stage in STAGE_LABELS:
                self.steps.append(stage)
            self.updated_at = time.time()

    def update(self, **fields: Any) -> None:
        with self._lock:
            if self.is_terminal:
                return
            for key, value in fields.items():
                if not hasattr(self, key) or key.startswith("_"):
                    raise AttributeError(key)
                setattr(self, key, value)
            if self.cancel_event.is_set():
                self.stage_detail = "Cancelling…"
            self.updated_at = time.time()

    def _steps_view(self) -> list[dict[str, str]]:
        steps = list(self.steps)
        current = self.last_stage if self.status in (JobStatus.FAILED, JobStatus.CANCELLED) else self.stage
        try:
            current_index = steps.index(current)
        except ValueError:
            current_index = len(steps) if self.status == JobStatus.COMPLETED else -1
        view = []
        for index, key in enumerate(steps):
            if self.status == JobStatus.COMPLETED:
                state = "done"
            elif current_index == -1:
                state = "pending"
            elif index < current_index:
                state = "done"
            elif index == current_index:
                state = "active"
            else:
                state = "pending"
            view.append({"key": key, "label": STAGE_LABELS.get(key, key), "state": state})
        if self.status in (JobStatus.FAILED, JobStatus.CANCELLED):
            # Mark the step that was running when the job stopped.
            for item in view:
                if item["state"] == "active":
                    item["state"] = self.status.value
        return view

    def snapshot(self) -> dict[str, Any]:
        with self._lock:
            data: dict[str, Any] = {
                "id": self.id,
                "status": self.status.value,
                "stage": self.stage,
                "stage_label": STAGE_LABELS.get(self.stage, self.stage),
                "stage_detail": self.stage_detail,
                "steps": self._steps_view(),
                "percent": None if self.percent is None else round(self.percent, 1),
                "downloaded_bytes": self.downloaded_bytes,
                "total_bytes": self.total_bytes,
                "total_is_estimate": self.total_is_estimate,
                "speed": self.speed,
                "eta": None if self.eta is None else round(self.eta),
                "video_id": self.request.video_id,
                "kind": self.request.kind,
                "quality": self.request.quality,
                "convert_h264": self.request.convert_h264,
                "label": self.label,
                "title": self.title,
                "thumbnail": self.thumbnail,
                "created_at": self.created_at,
                "started_at": self.started_at,
                "finished_at": self.finished_at,
                "cancellable": not self.is_terminal and not self.cancel_event.is_set(),
                "file": None,
                "error": self.error,
            }
            if self.status == JobStatus.COMPLETED and self.result_path is not None:
                data["file"] = {
                    "name": self.result_path.name,
                    "size": self.result_size,
                    "url": f"/api/file/{self.id}",
                }
            return data


class JobManager:
    """Runs jobs on a bounded thread pool and keeps their state in memory."""

    def __init__(self, runner: Callable[[Job], None], max_workers: int = 2, *, max_pending: int | None = None,
                 job_timeout: float | None = None) -> None:
        self._runner = runner
        self._executor = ThreadPoolExecutor(max_workers=max_workers, thread_name_prefix="download")
        self._jobs: dict[str, Job] = {}
        self._lock = threading.Lock()
        self._closed = False
        self._max_pending = max_pending  # queued + running jobs accepted at once
        self._job_timeout = job_timeout  # seconds a job may run (queue time excluded)

    def submit(self, request: JobRequest, *, title: str | None = None, thumbnail: str | None = None) -> Job:
        job = Job(new_job_id(), request, title=title, thumbnail=thumbnail)
        with self._lock:
            if self._closed:
                raise AppError("shutting_down", "Server is shutting down", "Please try again in a moment.", status=503)
            pending = sum(1 for j in self._jobs.values() if not j.is_terminal)
            if self._max_pending and pending >= self._max_pending:
                raise AppError("queue_full", "Too many downloads", "Too many downloads are queued or running right "
                               "now. Wait for some to finish and try again.", status=503, retryable=True)
            self._jobs[job.id] = job
        self._executor.submit(self._run, job)
        log.info("Job %s queued: %s %s %s%s", job.id[:8], request.video_id, request.kind, request.quality,
                 " (H.264)" if request.convert_h264 else "")
        return job

    def _run(self, job: Job) -> None:
        if not job.start():
            log.info("Job %s was cancelled before it started", job.id[:8])
            return
        timer = None
        if self._job_timeout:
            timer = threading.Timer(self._job_timeout, job.expire)
            timer.daemon = True
            timer.start()
        try:
            self._run_started(job)
        finally:
            if timer is not None:
                timer.cancel()

    def _run_started(self, job: Job) -> None:
        try:
            self._runner(job)
        except JobCancelled:
            if job.timed_out:
                minutes = max(1, round((self._job_timeout or 0) / 60))
                job.fail(AppError("job_timeout", "Took too long",
                                  f"The download didn't finish within {minutes} minutes and was stopped. Try a "
                                  "lower quality or a shorter video.", status=504, retryable=True).to_dict())
                log.warning("Job %s stopped after the %d-minute time limit", job.id[:8], minutes)
            else:
                job.mark_cancelled()
                log.info("Job %s cancelled", job.id[:8])
        except AppError as exc:
            job.fail(exc.to_dict())
            log.warning("Job %s failed: %s - %s (%s)", job.id[:8], exc.code, exc.message, exc.detail or "")
        except Exception as exc:  # noqa: BLE001 - last line of defence; the UI must never hang
            error = classify_error(exc, fallback_stage=STAGE_LABELS.get(job.stage, job.stage).lower())
            job.fail(error.to_dict())
            log.error("Job %s failed during %s: %s: %s", job.id[:8], job.stage, type(exc).__name__, exc)
            log.debug("Traceback for job %s", job.id[:8], exc_info=True)
        else:
            if job.status == JobStatus.RUNNING:
                job.fail(AppError("internal_error", "Something went wrong",
                                  "The download finished without producing a file.", status=500).to_dict())
                log.error("Job %s runner returned without completing", job.id[:8])
            else:
                log.info("Job %s %s", job.id[:8], job.status.value)

    def get(self, job_id: str) -> Job | None:
        with self._lock:
            return self._jobs.get(job_id)

    def cancel(self, job_id: str) -> Job | None:
        job = self.get(job_id)
        if job is not None and job.request_cancel():
            log.info("Job %s: cancellation requested", job.id[:8])
        return job

    def jobs(self) -> list[Job]:
        with self._lock:
            return list(self._jobs.values())

    def find_active(self, request: JobRequest) -> Job | None:
        for job in self.jobs():
            if not job.is_terminal and not job.cancel_event.is_set() and job.request.key == request.key:
                return job
        return None

    def queue_position(self, job: Job) -> int | None:
        if job.status != JobStatus.QUEUED:
            return None
        queued = sorted((j for j in self.jobs() if j.status == JobStatus.QUEUED), key=lambda j: j.created_at)
        return next((i + 1 for i, j in enumerate(queued) if j.id == job.id), None)

    def snapshot(self, job: Job) -> dict[str, Any]:
        data = job.snapshot()
        position = self.queue_position(job)
        if position is not None:
            data["queue_position"] = position
            data["stage_detail"] = data["stage_detail"] or f"Position {position} in queue"
        return data

    def active_ids(self) -> set[str]:
        return {j.id for j in self.jobs() if not j.is_terminal}

    def prune(self, max_age_seconds: float) -> int:
        """Forget finished jobs older than ``max_age_seconds`` or whose file is gone."""
        now = time.time()
        removed = 0
        with self._lock:
            for job_id, job in list(self._jobs.items()):
                if not job.is_terminal or job.finished_at is None:
                    continue
                expired = now - job.finished_at > max_age_seconds
                file_gone = job.result_path is not None and not job.result_path.exists()
                if expired or file_gone:
                    del self._jobs[job_id]
                    removed += 1
        return removed

    def shutdown(self, timeout: float = 10.0) -> None:
        with self._lock:
            self._closed = True
            jobs = list(self._jobs.values())
        for job in jobs:
            job.request_cancel()
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline and any(j.status == JobStatus.RUNNING for j in jobs):
            time.sleep(0.1)
        self._executor.shutdown(wait=False, cancel_futures=True)

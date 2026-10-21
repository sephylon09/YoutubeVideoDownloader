"""Job state machine and the background job manager."""

import threading
import time
from pathlib import Path

import pytest

from media_downloader.errors import AppError, JobCancelled
from media_downloader.jobs import (
    ALLOWED_TRANSITIONS,
    InvalidTransition,
    Job,
    JobManager,
    JobRequest,
    JobStatus,
)

REQUEST = JobRequest("dQw4w9WgXcQ", "https://www.youtube.com/watch?v=dQw4w9WgXcQ", "mp4", "best")


def wait_terminal(job: Job, timeout: float = 5.0) -> Job:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if job.is_terminal:
            return job
        time.sleep(0.02)
    raise AssertionError(f"job stuck in {job.status}")


# ------------------------------------------------------------ transitions

def test_valid_lifecycle():
    job = Job("a" * 32, REQUEST)
    assert job.status == JobStatus.QUEUED
    assert job.start()
    job.set_stage("downloading_video", percent=10)
    job.complete(Path("x.mp4"), 123)
    assert job.status == JobStatus.COMPLETED and job.percent == 100
    assert job.finished_at is not None


@pytest.mark.parametrize("terminal", [JobStatus.COMPLETED, JobStatus.FAILED, JobStatus.CANCELLED])
def test_terminal_states_are_final(terminal):
    assert ALLOWED_TRANSITIONS[terminal] == frozenset()
    job = Job("a" * 32, REQUEST)
    job.start()
    job.transition(terminal)
    with pytest.raises(InvalidTransition):
        job.transition(JobStatus.RUNNING)


def test_queued_cannot_complete_directly():
    job = Job("a" * 32, REQUEST)
    with pytest.raises(InvalidTransition):
        job.transition(JobStatus.COMPLETED)


def test_cancel_queued_job_is_immediate():
    job = Job("a" * 32, REQUEST)
    assert job.request_cancel()
    assert job.status == JobStatus.CANCELLED
    assert not job.start()  # a worker picking it up later must not run it


def test_cancel_running_job_signals_worker():
    job = Job("a" * 32, REQUEST)
    job.start()
    assert job.request_cancel()
    assert job.status == JobStatus.RUNNING and job.cancel_event.is_set()
    assert job.snapshot()["stage_detail"] == "Cancelling…"
    assert job.snapshot()["cancellable"] is False
    with pytest.raises(JobCancelled):
        job.check_cancelled()


def test_cancel_finished_job_is_noop():
    job = Job("a" * 32, REQUEST)
    job.start()
    job.complete(Path("x.mp4"), 1)
    assert job.request_cancel() is False
    assert job.status == JobStatus.COMPLETED


def test_progress_updates_ignored_after_finish():
    job = Job("a" * 32, REQUEST)
    job.start()
    job.fail({"code": "x", "title": "t", "message": "m"})
    job.set_stage("merging", percent=50)
    job.update(percent=10)
    assert job.stage == "failed" and job.percent is None


def test_steps_view():
    job = Job("a" * 32, REQUEST)
    job.start()
    job.steps = ["preparing", "downloading_video", "merging", "finalizing"]
    job.set_stage("merging")
    states = [s["state"] for s in job.snapshot()["steps"]]
    assert states == ["done", "done", "active", "pending"]
    job.fail({"code": "merge_failed", "title": "t", "message": "m"})
    states = [s["state"] for s in job.snapshot()["steps"]]
    assert states == ["done", "done", "failed", "pending"]


def test_snapshot_exposes_file_only_when_completed(tmp_path):
    job = Job("a" * 32, REQUEST)
    job.start()
    assert job.snapshot()["file"] is None
    job.complete(tmp_path / "video.mp4", 42)
    file_info = job.snapshot()["file"]
    assert file_info == {"name": "video.mp4", "size": 42, "url": f"/api/file/{'a' * 32}"}
    assert str(tmp_path) not in str(job.snapshot())  # server paths are never exposed


# ---------------------------------------------------------------- manager

def test_manager_runs_job_to_completion(tmp_path):
    def runner(job):
        job.set_stage("downloading_video", percent=50)
        job.complete(tmp_path / "out.mp4", 10)

    manager = JobManager(runner, max_workers=1)
    job = wait_terminal(manager.submit(REQUEST))
    assert job.status == JobStatus.COMPLETED
    assert manager.get(job.id) is job
    manager.shutdown()


def test_manager_reports_app_errors():
    def runner(job):
        raise AppError("quality_unavailable", "Quality unavailable", "Not offered.", status=409)

    manager = JobManager(runner, max_workers=1)
    job = wait_terminal(manager.submit(REQUEST))
    assert job.status == JobStatus.FAILED
    assert job.error["code"] == "quality_unavailable"
    manager.shutdown()


def test_manager_never_leaves_jobs_stuck_on_unexpected_errors():
    def runner(job):
        raise RuntimeError("boom")

    manager = JobManager(runner, max_workers=1)
    job = wait_terminal(manager.submit(REQUEST))
    assert job.status == JobStatus.FAILED
    assert job.error["code"] == "unexpected_error"
    assert "Traceback" not in job.error["message"]
    manager.shutdown()


def test_runner_that_forgets_to_complete_fails_cleanly():
    manager = JobManager(lambda job: None, max_workers=1)
    job = wait_terminal(manager.submit(REQUEST))
    assert job.status == JobStatus.FAILED
    manager.shutdown()


def test_cancel_running_job():
    started = threading.Event()

    def runner(job):
        started.set()
        while not job.cancel_event.wait(0.01):
            pass
        raise JobCancelled()

    manager = JobManager(runner, max_workers=1)
    job = manager.submit(REQUEST)
    assert started.wait(2)
    manager.cancel(job.id)
    assert wait_terminal(job).status == JobStatus.CANCELLED
    manager.shutdown()


def test_queued_job_cancelled_before_it_runs():
    release = threading.Event()
    ran = []

    def runner(job):
        ran.append(job.id)
        release.wait(2)
        job.complete(Path("x.mp4"), 1)

    manager = JobManager(runner, max_workers=1)
    first = manager.submit(REQUEST)
    second = manager.submit(JobRequest("dQw4w9WgXcQ", REQUEST.url, "mp3", "best"))
    deadline = time.monotonic() + 2
    while first.status != JobStatus.RUNNING and time.monotonic() < deadline:
        time.sleep(0.01)
    assert manager.snapshot(second)["queue_position"] == 1
    manager.cancel(second.id)
    release.set()
    wait_terminal(first)
    time.sleep(0.1)
    assert second.status == JobStatus.CANCELLED
    assert ran == [first.id]
    manager.shutdown()


def test_find_active_detects_duplicates():
    release = threading.Event()

    def runner(job):
        release.wait(2)
        job.complete(Path("x"), 1)

    manager = JobManager(runner, max_workers=1)
    job = manager.submit(REQUEST)
    assert manager.find_active(REQUEST) is job
    assert manager.find_active(JobRequest(REQUEST.video_id, REQUEST.url, "mp3", "best")) is None
    release.set()
    wait_terminal(job)
    assert manager.find_active(REQUEST) is None
    manager.shutdown()


def test_concurrent_jobs_run_in_parallel():
    barrier = threading.Barrier(2, timeout=3)

    def runner(job):
        barrier.wait()  # only passes if both jobs run at the same time
        job.complete(Path("x"), 1)

    manager = JobManager(runner, max_workers=2)
    jobs = [manager.submit(REQUEST), manager.submit(JobRequest(REQUEST.video_id, REQUEST.url, "mp3", "128"))]
    assert all(wait_terminal(j).status == JobStatus.COMPLETED for j in jobs)
    manager.shutdown()


def test_prune_forgets_old_finished_jobs(tmp_path):
    manager = JobManager(lambda job: job.complete(tmp_path / "gone.mp4", 1), max_workers=1)
    job = wait_terminal(manager.submit(REQUEST))
    assert manager.prune(3600) == 1  # file doesn't exist any more
    assert manager.get(job.id) is None
    manager.shutdown()

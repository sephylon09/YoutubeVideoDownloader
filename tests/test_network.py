"""End-to-end tests against real YouTube (opt-in: set MD_NETWORK_TESTS=1).

Uses "Me at the zoo" (19 s, the first YouTube video) so downloads are tiny.
"""

from __future__ import annotations

import dataclasses
import os
import time

import pytest

from media_downloader.config import settings
from media_downloader.downloader import InfoCache, extract_info
from media_downloader.environment import ToolRegistry
from media_downloader.errors import AppError
from media_downloader.ffmpeg import probe
from media_downloader.formats import build_video_options
from media_downloader.jobs import JobManager, JobRequest, JobStatus
from media_downloader.pipeline import PipelineContext, run_job
from media_downloader.validation import parse_youtube_url

pytestmark = [
    pytest.mark.network,
    pytest.mark.skipif(os.environ.get("MD_NETWORK_TESTS") != "1", reason="set MD_NETWORK_TESTS=1 to run"),
]

VIDEO = "https://www.youtube.com/watch?v=jNQXAC9IVRw"


@pytest.fixture(scope="module")
def tools():
    report = ToolRegistry(settings.tools_dir).get()
    if not report.ffmpeg_ok:
        pytest.skip("FFmpeg not installed")
    return report


@pytest.fixture
def context(tmp_path, tools):
    test_settings = dataclasses.replace(settings, download_dir=tmp_path / "downloads")
    (tmp_path / "downloads" / ".work").mkdir(parents=True)
    return PipelineContext(test_settings, ToolRegistry(settings.tools_dir), InfoCache(600))


def _run(context, kind, quality):
    manager = JobManager(lambda job: run_job(job, context), max_workers=1)
    ref = parse_youtube_url(VIDEO)
    job = manager.submit(JobRequest(ref.video_id, ref.canonical_url, kind, quality))
    deadline = time.monotonic() + 300
    while not job.is_terminal and time.monotonic() < deadline:
        time.sleep(0.2)
    manager.shutdown()
    return job


def test_analyze_real_video(tools):
    info, _ = extract_info(parse_youtube_url(VIDEO), tools, settings)
    assert info["id"] == "jNQXAC9IVRw"
    assert build_video_options(info), "expected at least one video option"


def test_unavailable_video_is_reported(tools):
    with pytest.raises(AppError) as excinfo:
        extract_info(parse_youtube_url("https://www.youtube.com/watch?v=aaaaaaaaaaa"), tools, settings)
    assert excinfo.value.code in ("video_unavailable", "private_video", "extraction_failed")


def test_download_mp4_with_audio(context, tools):
    job = _run(context, "mp4", "best")
    assert job.status == JobStatus.COMPLETED, job.error
    result = probe(tools.ffprobe.path, job.result_path)
    assert result.video_codecs and result.audio_codecs
    assert job.result_path.suffix == ".mp4"
    assert not any((context.settings.work_dir).iterdir())  # temporary files cleaned up


def test_download_mp3(context, tools):
    job = _run(context, "mp3", "128")
    assert job.status == JobStatus.COMPLETED, job.error
    result = probe(tools.ffprobe.path, job.result_path)
    assert result.audio_codecs == ["mp3"]
    assert job.result_path.suffix == ".mp3"

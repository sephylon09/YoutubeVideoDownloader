"""HTTP API behaviour (YouTube access is mocked; no network needed)."""

from __future__ import annotations

import dataclasses
import time
from pathlib import Path
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

import app as app_module
from media_downloader.jobs import Job, JobManager, JobRequest, JobStatus

BASE = "http://127.0.0.1:8000"
LOCAL_CLIENT = ("127.0.0.1", 50000)
VIDEO_URL = "https://youtu.be/aqz-KE-bpKQ?si=tracking"


@pytest.fixture
def client():
    return TestClient(app_module.app, base_url=BASE, client=LOCAL_CLIENT)


@pytest.fixture
def fake_tools(monkeypatch):
    report = SimpleNamespace(ffmpeg_ok=True, can_encode_mp3=True, h264_encoder="libx264")
    monkeypatch.setattr(app_module.tool_registry, "get", lambda refresh=False: report)
    return report


@pytest.fixture
def fake_youtube(monkeypatch, bbb_info):
    calls = []

    def fake_extract(ref, tools, settings, logger=None):
        calls.append(ref.canonical_url)
        return bbb_info, []

    monkeypatch.setattr(app_module, "extract_info", fake_extract)
    app_module.info_cache.evict("aqz-KE-bpKQ")
    yield calls
    app_module.info_cache.evict("aqz-KE-bpKQ")


@pytest.fixture
def fake_manager(monkeypatch, tmp_path):
    def runner(job):
        job.set_stage("downloading_video", percent=40)
        path = tmp_path / "result.mp4"
        path.write_bytes(b"fake")
        job.complete(path, 4)

    manager = JobManager(runner, max_workers=1)
    monkeypatch.setattr(app_module, "job_manager", manager)
    yield manager
    manager.shutdown()


# ------------------------------------------------------------------ pages

def test_homepage_and_assets_load(client):
    page = client.get("/")
    assert page.status_code == 200
    assert "Media Downloader" in page.text
    assert "Download your videos and audio in your preferred quality" in page.text
    for asset, content_type in [
        ("/static/css/style.css", "text/css"),
        ("/static/js/app.js", "javascript"),
        ("/static/js/theme-init.js", "javascript"),
        ("/static/fonts/inter-latin.woff2", "font/woff2"),
        ("/static/img/favicon.svg", "image/svg+xml"),
    ]:
        assert asset in page.text or asset.endswith(("woff2", "svg"))
        response = client.get(asset)
        assert response.status_code == 200, asset
        assert content_type in response.headers["content-type"]


def test_security_headers(client):
    headers = client.get("/").headers
    assert "default-src 'self'" in headers["content-security-policy"]
    assert headers["x-content-type-options"] == "nosniff"
    assert headers["x-frame-options"] == "DENY"


def test_unknown_host_header_rejected():
    response = TestClient(app_module.app, base_url="http://evil.example").get("/")
    assert response.status_code == 400


def test_health(client):
    data = client.get("/api/health").json()
    assert data["app"] == "media-downloader"
    assert "ffmpeg" in data["tools"] and "js_runtime" in data["tools"]


# ---------------------------------------------------------------- analyze

def test_analyze_returns_real_options(client, fake_youtube, fake_tools):
    response = client.post("/api/analyze", json={"url": VIDEO_URL})
    assert response.status_code == 200, response.text
    data = response.json()
    assert fake_youtube == ["https://www.youtube.com/watch?v=aqz-KE-bpKQ"]  # canonical URL only
    assert data["video"]["title"].startswith("Big Buck Bunny")
    assert data["video"]["max_quality"] == "2160p60"
    assert [o["id"] for o in data["mp4"]["options"]][0] == "2160p60"
    assert data["mp4"]["best"]["resolves_to"] == "2160p60"
    assert [o["id"] for o in data["mp3"]["options"]] == ["best", "320", "256", "192", "128"]


@pytest.mark.parametrize("url,code", [
    ("https://vimeo.com/1", "unsupported_url"),
    ("hello", "invalid_url"),
    ("https://www.youtube.com/playlist?list=PL0123456789", "unsupported_url"),
])
def test_analyze_rejects_bad_urls(client, url, code):
    response = client.post("/api/analyze", json={"url": url})
    assert response.status_code == 400
    assert response.json()["error"]["code"] == code


def test_analyze_validates_body(client):
    assert client.post("/api/analyze", json={}).status_code == 422
    assert client.post("/api/analyze", json={"url": "x", "extra": 1}).status_code == 422
    assert client.post("/api/analyze", json={"url": "a" * 5000}).status_code == 422


def test_non_json_post_rejected(client):
    response = client.post("/api/analyze", content='{"url": "https://youtu.be/dQw4w9WgXcQ"}',
                           headers={"Content-Type": "text/plain"})
    assert response.status_code == 415


def test_cross_site_post_rejected(client):
    response = client.post("/api/analyze", json={"url": VIDEO_URL}, headers={"Origin": "https://evil.example"})
    assert response.status_code == 403
    response = client.post("/api/analyze", json={"url": VIDEO_URL}, headers={"Sec-Fetch-Site": "cross-site"})
    assert response.status_code == 403


# --------------------------------------------------------------- download

def test_download_validates_quality_against_formats(client, fake_youtube, fake_tools, fake_manager):
    client.post("/api/analyze", json={"url": VIDEO_URL})
    response = client.post("/api/download", json={"url": VIDEO_URL, "format": "mp4", "quality": "4320p60"})
    assert response.status_code == 409
    assert response.json()["error"]["code"] == "quality_unavailable"


@pytest.mark.parametrize("body", [
    {"url": VIDEO_URL, "format": "mkv", "quality": "best"},
    {"url": VIDEO_URL, "format": "mp4", "quality": "1080p60; del *"},
    {"url": VIDEO_URL, "format": "mp3", "quality": "999"},
    {"url": VIDEO_URL, "format": "mp4"},
])
def test_download_rejects_invalid_requests(client, fake_tools, body):
    response = client.post("/api/download", json=body)
    assert response.status_code in (400, 422)
    assert "error" in response.json()


def test_download_requires_ffmpeg(client, monkeypatch):
    report = SimpleNamespace(ffmpeg_ok=False, can_encode_mp3=False, h264_encoder=None)
    monkeypatch.setattr(app_module.tool_registry, "get", lambda refresh=False: report)
    response = client.post("/api/download", json={"url": VIDEO_URL, "format": "mp4", "quality": "best"})
    assert response.status_code == 503
    assert response.json()["error"]["code"] == "ffmpeg_missing"


def test_download_job_lifecycle(client, fake_youtube, fake_tools, fake_manager):
    client.post("/api/analyze", json={"url": VIDEO_URL})
    response = client.post("/api/download", json={"url": VIDEO_URL, "format": "mp4", "quality": "1080p60"})
    assert response.status_code == 202, response.text
    job_id = response.json()["job_id"]
    for _ in range(100):
        progress = client.get(f"/api/progress/{job_id}").json()
        if progress["status"] == "completed":
            break
        time.sleep(0.02)
    assert progress["status"] == "completed"
    assert progress["file"]["url"] == f"/api/file/{job_id}"
    assert progress["title"].startswith("Big Buck Bunny")


@pytest.mark.parametrize("path", [
    "/api/progress/not-a-job", f"/api/progress/{'0' * 32}", f"/api/file/{'0' * 32}", "/api/file/..%2F..%2Fapp.py",
])
def test_unknown_jobs_404(client, path):
    response = client.get(path)
    assert response.status_code == 404


def test_cancel_unknown_job_404(client):
    assert client.post(f"/api/cancel/{'0' * 32}").status_code == 404


# ------------------------------------------------------------ file serving

def _completed_job(path: Path, kind: str = "mp3") -> Job:
    job = Job("f" * 32, JobRequest("dQw4w9WgXcQ", "https://www.youtube.com/watch?v=dQw4w9WgXcQ", kind, "best"))
    job.start()
    job.complete(path, path.stat().st_size)
    return job


def test_file_download_uses_unicode_filename(client, monkeypatch, tmp_path):
    downloads = tmp_path / "downloads"
    downloads.mkdir()
    monkeypatch.setattr(app_module, "settings", dataclasses.replace(app_module.settings, download_dir=downloads))
    path = downloads / "日本語 タイトル 🎵 - test.mp3"
    path.write_bytes(b"ID3" + b"\x00" * 1000)
    job = _completed_job(path)
    manager = JobManager(lambda j: None, max_workers=1)
    manager._jobs[job.id] = job
    monkeypatch.setattr(app_module, "job_manager", manager)

    response = client.get(f"/api/file/{job.id}")
    assert response.status_code == 200
    assert response.content == path.read_bytes()
    assert response.headers["content-type"] == "audio/mpeg"
    disposition = response.headers["content-disposition"]
    assert disposition.startswith("attachment;")
    assert 'filename="' in disposition and disposition.split('filename="')[1].split('"')[0].endswith(".mp3")
    assert "filename*=UTF-8''%E6%97%A5%E6%9C%AC%E8%AA%9E" in disposition
    head = client.head(f"/api/file/{job.id}")
    assert head.status_code == 200 and head.content == b""
    assert head.headers["content-length"] == str(path.stat().st_size)
    manager.shutdown()


def test_file_outside_download_dir_is_never_served(client, monkeypatch, tmp_path):
    downloads = tmp_path / "downloads"
    downloads.mkdir()
    monkeypatch.setattr(app_module, "settings", dataclasses.replace(app_module.settings, download_dir=downloads))
    outside = tmp_path / "secret.mp4"
    outside.write_bytes(b"secret")
    manager = JobManager(lambda j: None, max_workers=1)
    job = _completed_job(outside, "mp4")
    manager._jobs[job.id] = job
    monkeypatch.setattr(app_module, "job_manager", manager)
    response = client.get(f"/api/file/{job.id}")
    assert response.status_code == 410
    assert b"secret" not in response.content
    manager.shutdown()


def test_file_not_ready(client, monkeypatch):
    manager = JobManager(lambda j: None, max_workers=1)
    job = Job("e" * 32, JobRequest("dQw4w9WgXcQ", "u", "mp4", "best"))
    manager._jobs[job.id] = job
    monkeypatch.setattr(app_module, "job_manager", manager)
    response = client.get(f"/api/file/{job.id}")
    assert response.status_code == 409
    assert job.status == JobStatus.QUEUED
    manager.shutdown()


@pytest.mark.parametrize("name,fallback", [
    ("Video.mp4", 'filename="Video.mp4"'),
    ("日本語.mp3", 'filename="download.mp3"'),
    ("Café – live.mp4", 'filename="Cafe live.mp4"'),
    ('Quote"and\\slash.mp4', 'filename="Quote_and_slash.mp4"'),
])
def test_content_disposition(name, fallback):
    header = app_module.content_disposition(name)
    assert fallback in header
    assert "filename*=UTF-8''" in header

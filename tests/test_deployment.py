"""Hosted (public) mode: configuration, sign-in, health check, limits and hosted wording."""

from __future__ import annotations

import dataclasses
import threading
import time
from pathlib import Path
from types import SimpleNamespace

import pytest
import yt_dlp
from fastapi.testclient import TestClient

import app as app_module
from media_downloader import config as config_module
from media_downloader.config import load_settings
from media_downloader.downloader import check_allowed, download_streams
from media_downloader.errors import AppError, JobCancelled
from media_downloader.formats import resolve_plan
from media_downloader.jobs import JobManager, JobRequest, JobStatus
from media_downloader.security import RateLimiter, SessionSigner, password_matches
from media_downloader.storage import directory_size, fit_name_bytes, reserve_unique_path

BASE = "http://127.0.0.1:8000"
REMOTE_CLIENT = ("203.0.113.7", 50000)
PASSWORD = "correct horse battery staple"
VIDEO_URL = "https://youtu.be/aqz-KE-bpKQ"
REQUEST = JobRequest("dQw4w9WgXcQ", "https://www.youtube.com/watch?v=dQw4w9WgXcQ", "mp4", "best")


@pytest.fixture
def public(monkeypatch):
    """Switch the app into hosted mode with a password and fresh rate limits."""
    hosted = dataclasses.replace(app_module.settings, host="0.0.0.0", password=PASSWORD, secret_key="s" * 32,
                                 rate_limits=True)
    monkeypatch.setattr(app_module, "settings", hosted)
    monkeypatch.setattr(app_module, "rate_limiter", RateLimiter())
    return hosted


@pytest.fixture
def remote():
    return TestClient(app_module.app, base_url=BASE, client=REMOTE_CLIENT, follow_redirects=False)


@pytest.fixture
def signed_in(public, remote):
    response = remote.post("/login", data={"password": PASSWORD})
    assert response.status_code == 303, response.text
    return remote


@pytest.fixture
def fake_tools(monkeypatch):
    report = SimpleNamespace(ffmpeg_ok=True, can_encode_mp3=True, h264_encoder="libx264")
    monkeypatch.setattr(app_module.tool_registry, "get", lambda refresh=False: report)
    return report


@pytest.fixture
def fake_youtube(monkeypatch, bbb_info):
    monkeypatch.setattr(app_module, "extract_info", lambda ref, tools, settings, logger=None: (bbb_info, []))
    app_module.info_cache.evict("aqz-KE-bpKQ")
    yield bbb_info
    app_module.info_cache.evict("aqz-KE-bpKQ")


# ---------------------------------------------------------------- settings

def _clear_env(monkeypatch):
    for name in ("MD_HOST", "MD_PORT", "PORT", "RENDER", "RENDER_EXTERNAL_HOSTNAME", "MD_ALLOWED_HOSTS",
                 "MD_TRUST_PROXY", "MD_RATE_LIMITS", "MD_PASSWORD"):
        monkeypatch.delenv(name, raising=False)


def test_local_mode_ignores_platform_port(monkeypatch):
    _clear_env(monkeypatch)
    monkeypatch.setenv("PORT", "3000")  # e.g. left over from another dev tool
    settings = load_settings()
    assert settings.port == 8000
    assert not settings.is_public and not settings.rate_limits and not settings.trust_proxy
    assert settings.startup_problem() is None


def test_public_mode_uses_render_port_and_hostname(monkeypatch):
    _clear_env(monkeypatch)
    monkeypatch.setenv("MD_HOST", "0.0.0.0")
    monkeypatch.setenv("PORT", "10000")
    monkeypatch.setenv("RENDER", "true")
    monkeypatch.setenv("RENDER_EXTERNAL_HOSTNAME", "my-app.onrender.com")
    monkeypatch.setenv("MD_ALLOWED_HOSTS", "downloads.example.com, *.example.org")
    settings = load_settings()
    assert settings.is_public and settings.port == 10000
    assert settings.trust_proxy and settings.rate_limits
    assert {"my-app.onrender.com", "downloads.example.com", "*.example.org"} <= set(settings.allowed_hosts)
    monkeypatch.setenv("MD_PORT", "9000")
    assert load_settings().port == 9000


def test_public_mode_requires_a_real_password(monkeypatch):
    _clear_env(monkeypatch)
    monkeypatch.setenv("MD_HOST", "0.0.0.0")
    assert "MD_PASSWORD" in load_settings().startup_problem()
    monkeypatch.setenv("MD_PASSWORD", "short")
    assert "too short" in load_settings().startup_problem()
    monkeypatch.setenv("MD_PASSWORD", PASSWORD)
    assert load_settings().startup_problem() is None
    assert PASSWORD not in repr(load_settings())


def test_main_refuses_public_bind_without_password(monkeypatch):
    monkeypatch.setattr(app_module, "settings", dataclasses.replace(app_module.settings, password=None))
    assert app_module.main(["--host", "0.0.0.0", "--port", "8123"]) == 2


# ---------------------------------------------------------- access control

def test_remote_clients_blocked_without_password():
    client = TestClient(app_module.app, base_url=BASE, client=REMOTE_CLIENT)
    response = client.get("/")
    assert response.status_code == 403
    assert response.json()["error"]["code"] == "remote_access_disabled"
    assert client.get("/api/health").status_code == 403


def test_forwarded_loopback_address_is_not_trusted(monkeypatch):
    monkeypatch.setattr(app_module, "settings", dataclasses.replace(app_module.settings, trust_proxy=True))
    client = TestClient(app_module.app, base_url=BASE, client=("127.0.0.1", 50000))
    assert client.get("/").status_code == 403


def test_pages_and_api_require_sign_in(public, remote):
    page = remote.get("/")
    assert page.status_code == 303 and page.headers["location"] == "/login"
    assert "default-src 'self'" in page.headers["content-security-policy"]  # rejected responses too
    api = remote.get("/api/health")
    assert api.status_code == 401 and api.json()["error"]["code"] == "auth_required"
    assert remote.post("/api/analyze", json={"url": VIDEO_URL}).status_code == 401
    assert remote.get(f"/api/file/{'0' * 32}").status_code == 401
    # The sign-in page and its assets stay reachable.
    login = remote.get("/login")
    assert login.status_code == 200 and 'name="password"' in login.text
    assert remote.get("/static/css/style.css").status_code == 200


def test_sign_in_and_out(public, remote):
    wrong = remote.post("/login", data={"password": "nope"})
    assert wrong.status_code == 401 and "Wrong password" in wrong.text
    assert app_module.SESSION_COOKIE not in remote.cookies

    response = remote.post("/login", data={"password": PASSWORD})
    assert response.status_code == 303 and response.headers["location"] == "/"
    cookie = response.headers["set-cookie"]
    assert "HttpOnly" in cookie and "SameSite=lax" in cookie and "Max-Age=" in cookie
    assert remote.get("/").status_code == 200
    page = remote.get("/").text
    assert 'data-mode="public"' in page and 'action="/logout"' in page
    assert remote.get("/api/health").json()["settings"]["auth_enabled"] is True
    assert remote.get("/login").status_code == 303  # already signed in

    token = remote.cookies[app_module.SESSION_COOKIE]
    assert remote.post("/logout").status_code == 303
    assert app_module.SESSION_COOKIE not in remote.cookies
    remote.cookies.set(app_module.SESSION_COOKIE, token)  # a copied cookie stops working after sign-out
    assert remote.get("/api/health").status_code == 401


def test_secure_cookie_and_hsts_over_https(public):
    client = TestClient(app_module.app, base_url="https://127.0.0.1", client=REMOTE_CLIENT, follow_redirects=False)
    response = client.post("/login", data={"password": PASSWORD})
    assert "Secure" in response.headers["set-cookie"]
    assert "max-age" in client.get("/").headers["strict-transport-security"]


def test_tampered_session_cookie_rejected(public, remote):
    remote.post("/login", data={"password": PASSWORD})
    token = remote.cookies[app_module.SESSION_COOKIE]
    session_id, expiry, signature = token.split(".")
    remote.cookies.clear()
    remote.cookies.set(app_module.SESSION_COOKIE, token)
    assert remote.get("/api/health").status_code == 200  # the cookie set this way is sent
    remote.cookies.clear()
    remote.cookies.set(app_module.SESSION_COOKIE, f"{session_id}.{int(expiry) + 99999}.{signature}")
    assert remote.get("/api/health").status_code == 401


def test_cross_site_login_rejected(public, remote):
    response = remote.post("/login", data={"password": PASSWORD}, headers={"Origin": "https://evil.example"})
    assert response.status_code == 403
    response = remote.post("/login", data={"password": PASSWORD}, headers={"Origin": "null"})
    assert response.status_code == 403


def test_same_origin_form_posts_allowed(public, remote):
    # Browsers only send the real Origin with form posts if the referrer policy allows it.
    assert remote.get("/login").headers["referrer-policy"] == "same-origin"
    response = remote.post("/login", data={"password": PASSWORD}, headers={"Origin": BASE})
    assert response.status_code == 303
    assert remote.post("/logout", headers={"Origin": BASE}).status_code == 303


def test_login_attempts_are_rate_limited(public, remote, monkeypatch):
    async def no_sleep(_seconds):
        return None

    monkeypatch.setattr(app_module.asyncio, "sleep", no_sleep)
    limit, _window = app_module.LOGIN_FAILURES_PER_CLIENT
    for _ in range(limit):
        assert remote.post("/login", data={"password": "guess"}).status_code == 401
    blocked = remote.post("/login", data={"password": PASSWORD})  # even the right password waits
    assert blocked.status_code == 429 and "Too many failed attempts" in blocked.text


def test_login_body_size_is_limited(public, remote):
    response = remote.post("/login", content=b"password=" + b"x" * 10_000,
                           headers={"Content-Type": "application/x-www-form-urlencoded"})
    assert response.status_code == 413


def test_analyses_are_rate_limited_per_session(signed_in, public, fake_youtube, fake_tools, monkeypatch):
    monkeypatch.setattr(app_module, "settings", dataclasses.replace(public, analyses_per_hour=2))
    assert signed_in.post("/api/analyze", json={"url": VIDEO_URL}).status_code == 200
    assert signed_in.post("/api/analyze", json={"url": VIDEO_URL}).status_code == 200
    response = signed_in.post("/api/analyze", json={"url": VIDEO_URL})
    assert response.status_code == 429
    assert response.json()["error"]["code"] == "too_many_requests"


# ------------------------------------------------------------ health check

def test_healthz_is_public_and_minimal(public):
    client = TestClient(app_module.app, base_url="http://10.0.0.5:10000", client=("10.0.0.1", 1))
    response = client.get("/healthz")  # unknown Host header and no session: still answered
    assert response.status_code in (200, 503)
    assert set(response.json()) == {"status", "version"}
    assert client.head("/healthz").status_code == response.status_code


def test_healthz_reports_missing_ffmpeg(monkeypatch, tmp_path):
    monkeypatch.setattr(app_module, "settings", dataclasses.replace(app_module.settings, download_dir=tmp_path))
    client = TestClient(app_module.app, base_url=BASE)
    monkeypatch.setattr(app_module.tool_registry, "get", lambda refresh=False: SimpleNamespace(ffmpeg_ok=True))
    assert client.get("/healthz").json() == {"status": "ok", "version": app_module.__version__}
    monkeypatch.setattr(app_module.tool_registry, "get", lambda refresh=False: SimpleNamespace(ffmpeg_ok=False))
    response = client.get("/healthz")
    assert response.status_code == 503 and response.json()["status"] == "unhealthy"


# ------------------------------------------------------ hosted wording/limits

def test_hosted_error_messages_avoid_local_instructions(public):
    error = AppError("bot_check", "YouTube blocked the request", "… keep yt-dlp up to date with update.bat.")
    presented = app_module._present_error(error.to_dict())
    assert "update.bat" not in presented["message"] and "cloud" in presented["message"]
    generic = app_module._present_error({"code": "unexpected_error", "message": "Details were written to logs/app.log."})
    assert generic["message"] == "Details were written to the server log."


def test_index_mentions_temporary_storage(signed_in, public):
    page = signed_in.get("/").text
    assert "deleted earlier if the server restarts" in page
    assert "Runs entirely on your computer" not in page


def test_h264_conversion_can_be_turned_off(signed_in, public, fake_youtube, fake_tools, monkeypatch):
    monkeypatch.setattr(app_module, "settings", dataclasses.replace(public, allow_h264_conversion=False))
    data = signed_in.post("/api/analyze", json={"url": VIDEO_URL}).json()
    assert data["mp4"]["h264_conversion_available"] is False
    response = signed_in.post("/api/download", json={"url": VIDEO_URL, "format": "mp4", "quality": "1440p60",
                                                    "convert_h264": True})
    assert response.status_code == 403 and response.json()["error"]["code"] == "h264_disabled"


def test_download_over_size_limit_rejected_up_front(signed_in, public, fake_youtube, fake_tools, monkeypatch):
    monkeypatch.setattr(app_module, "settings", dataclasses.replace(public, max_file_mb=1))
    data = signed_in.post("/api/analyze", json={"url": VIDEO_URL}).json()
    assert data["limits"]["max_file_bytes"] == 1024 * 1024
    response = signed_in.post("/api/download", json={"url": VIDEO_URL, "format": "mp4", "quality": "2160p60"})
    assert response.status_code == 413
    assert response.json()["error"]["code"] == "file_too_large"


# ------------------------------------------------------------ video policy

def test_duration_limit(monkeypatch):
    settings = dataclasses.replace(config_module.settings, max_duration_minutes=10)
    check_allowed({"duration": 599}, settings)
    with pytest.raises(AppError) as excinfo:
        check_allowed({"duration": 3600}, settings)
    assert excinfo.value.code == "too_long"


@pytest.mark.parametrize("entry", ["UCSMOQeBJ2RAnuFungnQOxLg", "@BlenderStudio", "blenderstudio",
                                   "https://www.youtube.com/@BlenderStudio"])
def test_channel_allow_list(entry):
    info = {"channel_id": "UCSMOQeBJ2RAnuFungnQOxLg", "uploader_id": "@BlenderStudio",
            "uploader_url": "https://www.youtube.com/@BlenderStudio"}
    check_allowed(info, dataclasses.replace(config_module.settings, allowed_channels=(entry,)))
    with pytest.raises(AppError) as excinfo:
        check_allowed({"channel_id": "UCother", "uploader_id": "@someone"},
                      dataclasses.replace(config_module.settings, allowed_channels=(entry,)))
    assert excinfo.value.code == "channel_not_allowed"


def test_download_aborted_when_streams_exceed_size_limit(monkeypatch, tmp_path, bbb_info):
    plan = resolve_plan(bbb_info, "mp4", "1080p60")

    class FakeYoutubeDL:
        sanitize_info = staticmethod(yt_dlp.YoutubeDL.sanitize_info)

        def __init__(self, options):
            self.hook = options["progress_hooks"][0]

        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

        def process_ie_result(self, info, download):
            for done in range(0, 10_000_000, 1_000_000):
                self.hook({"status": "downloading", "downloaded_bytes": done,
                           "info_dict": {"format_id": plan.format_ids[0]}})
            raise AssertionError("size limit was not enforced")

    monkeypatch.setattr(yt_dlp, "YoutubeDL", FakeYoutubeDL)
    tools = SimpleNamespace(js_runtimes_param=lambda: {}, ffmpeg_dir=None)
    with pytest.raises(AppError) as excinfo:
        download_streams(bbb_info, plan, tmp_path, tools=tools, settings=config_module.settings,
                         cancel_event=threading.Event(), on_progress=lambda status: None, max_bytes=3_000_000)
    assert excinfo.value.code == "file_too_large"


# ------------------------------------------------------------- job limits

def test_job_timeout_fails_the_job():
    def slow_runner(job):
        while not job.cancel_event.wait(0.01):
            pass
        raise JobCancelled()

    manager = JobManager(slow_runner, max_workers=1, job_timeout=0.1)
    job = manager.submit(REQUEST)
    deadline = time.monotonic() + 5
    while not job.is_terminal and time.monotonic() < deadline:
        time.sleep(0.02)
    assert job.status == JobStatus.FAILED
    assert job.error["code"] == "job_timeout"
    manager.shutdown()


def test_user_cancel_is_not_reported_as_timeout():
    started = threading.Event()

    def runner(job):
        started.set()
        job.cancel_event.wait(5)
        raise JobCancelled()

    manager = JobManager(runner, max_workers=1, job_timeout=60)
    job = manager.submit(REQUEST)
    assert started.wait(5)
    manager.cancel(job.id)
    deadline = time.monotonic() + 5
    while not job.is_terminal and time.monotonic() < deadline:
        time.sleep(0.02)
    assert job.status == JobStatus.CANCELLED
    manager.shutdown()


def test_queue_is_bounded():
    release = threading.Event()
    manager = JobManager(lambda job: release.wait(5), max_workers=1, max_pending=2)
    manager.submit(REQUEST)
    manager.submit(dataclasses.replace(REQUEST, quality="720p"))
    with pytest.raises(AppError) as excinfo:
        manager.submit(dataclasses.replace(REQUEST, quality="480p"))
    assert excinfo.value.code == "queue_full" and excinfo.value.status == 503
    release.set()
    manager.shutdown()


# ------------------------------------------------------------ storage/security

def test_filenames_fit_linux_byte_limit(clean_dir: Path, monkeypatch):
    title = "一口气看完进击的巨人全系列" * 20  # 260 CJK characters = 780 UTF-8 bytes
    stem = fit_name_bytes(title, reserved=len(" (999).mp4"), max_bytes=255)
    assert len(stem.encode("utf-8")) + len(" (999).mp4") <= 255
    assert title.startswith(stem)
    from media_downloader import storage
    monkeypatch.setattr(storage, "MAX_NAME_BYTES", 255)
    path = reserve_unique_path(clean_dir, title, "mp4")
    assert len(path.name.encode("utf-8")) <= 255
    assert fit_name_bytes(title, reserved=10, max_bytes=None) == title  # Windows: unchanged


def test_directory_size(tmp_path: Path):
    (tmp_path / "a.mp4").write_bytes(b"x" * 100)
    (tmp_path / ".work" / "job").mkdir(parents=True)
    (tmp_path / ".work" / "job" / "stream.webm").write_bytes(b"x" * 50)
    assert directory_size(tmp_path) == 150


def test_storage_quota_blocks_new_jobs(tmp_path: Path, bbb_info):
    from media_downloader.pipeline import _check_disk_space

    (tmp_path / "old.mp4").write_bytes(b"x" * (2 * 1024 * 1024))
    settings = dataclasses.replace(config_module.settings, download_dir=tmp_path, max_storage_mb=1)
    with pytest.raises(AppError) as excinfo:
        _check_disk_space(resolve_plan(bbb_info, "mp3", "best"), tmp_path, settings)
    assert excinfo.value.code == "storage_full"


def test_session_signer():
    signer = SessionSigner("secret", PASSWORD, lifetime_seconds=60)
    token = signer.issue()
    assert signer.verify(token)
    assert signer.verify(token + "0") is None
    assert signer.verify("not.a.token") is None and signer.verify(None) is None
    assert SessionSigner("secret", "a different password", 60).verify(token) is None
    assert SessionSigner("other secret", PASSWORD, 60).verify(token) is None
    assert SessionSigner("secret", PASSWORD, lifetime_seconds=-1).verify(
        SessionSigner("secret", PASSWORD, lifetime_seconds=-1).issue()) is None  # expired
    signer.revoke(token)
    assert signer.verify(token) is None


def test_password_matches():
    assert password_matches(PASSWORD, PASSWORD)
    assert not password_matches(PASSWORD[:-1], PASSWORD)
    assert not password_matches("", PASSWORD)


def test_rate_limiter_window(monkeypatch):
    now = [1000.0]
    monkeypatch.setattr(time, "monotonic", lambda: now[0])
    limiter = RateLimiter()
    assert limiter.hit("k", 2, 60) == 0 and limiter.hit("k", 2, 60) == 0
    assert limiter.hit("k", 2, 60) > 0
    now[0] += 61
    assert limiter.hit("k", 2, 60) == 0

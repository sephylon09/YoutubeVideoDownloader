"""Media Downloader - FastAPI application and server entry point.

Locally: start it with ``start.bat`` (recommended) or ``python app.py --open``.
The server only listens on 127.0.0.1 unless MD_HOST is changed.

Hosted (Docker / Render): ``python app.py`` with ``MD_HOST=0.0.0.0``. The
platform's PORT is used, and a password (MD_PASSWORD) is required.
"""

from __future__ import annotations

import argparse
import asyncio
import functools
import hashlib
import ipaddress
import json
import mimetypes
import os
import re
import socket
import sys
import threading
import time
import unicodedata
import urllib.parse
import urllib.request
import webbrowser
from contextlib import asynccontextmanager
from typing import Any, Literal

from fastapi import FastAPI, Request
from fastapi.concurrency import run_in_threadpool
from fastapi.exceptions import RequestValidationError
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, RedirectResponse, Response
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from pydantic import BaseModel, ConfigDict, Field
from starlette.exceptions import HTTPException as StarletteHTTPException
from starlette.middleware.trustedhost import TrustedHostMiddleware

from media_downloader import APP_ID, APP_NAME, __version__
from media_downloader.config import settings
from media_downloader.downloader import InfoCache, extract_info
from media_downloader.environment import ToolRegistry, python_info
from media_downloader.errors import AppError
from media_downloader.formats import (
    build_mp3_options,
    resolve_plan,
    summarize_video,
    thumbnail_candidates,
    validate_quality_id,
    video_formats_summary,
)
from media_downloader.jobs import Job, JobManager, JobRequest, JobStatus
from media_downloader.logging_setup import get_logger, setup_logging
from media_downloader.pipeline import PipelineContext, check_file_size, run_job
from media_downloader.security import SESSION_COOKIE, RateLimiter, SessionSigner, password_matches
from media_downloader.storage import clear_work_dir, cleanup_old_files, is_valid_job_id, is_within
from media_downloader.validation import parse_youtube_url

log = get_logger("app")

# Windows derives MIME types from the registry, which some software changes (e.g. ".js" -> text/plain).
# With "X-Content-Type-Options: nosniff" a wrong type would stop the page from working, so pin them.
for _ext, _mime in ((".js", "text/javascript"), (".css", "text/css"), (".svg", "image/svg+xml"),
                    (".woff2", "font/woff2"), (".json", "application/json"), (".html", "text/html")):
    mimetypes.add_type(_mime, _ext)

tool_registry = ToolRegistry(settings.tools_dir)
info_cache = InfoCache(settings.info_cache_ttl_seconds)
pipeline_context = PipelineContext(settings, tool_registry, info_cache)
job_manager = JobManager(lambda job: run_job(job, pipeline_context), settings.max_concurrent_downloads,
                         max_pending=settings.max_pending_jobs,
                         job_timeout=settings.job_timeout_minutes * 60 or None)
analysis_slots = threading.BoundedSemaphore(settings.max_concurrent_analyses)
templates = Jinja2Templates(directory=str(settings.templates_dir))
rate_limiter = RateLimiter()

# Check often enough that short retention times (public mode) are honoured.
CLEANUP_INTERVAL_SECONDS = min(10 * 60, max(60, settings.file_retention_hours * 3600 / 4))
_cleanup_stop = threading.Event()


# --------------------------------------------------------------------------- #
# Lifecycle
# --------------------------------------------------------------------------- #

def _cleanup_once() -> None:
    max_age = settings.file_retention_hours * 3600
    protected = {settings.work_dir / job_id for job_id in job_manager.active_ids()}
    removed = cleanup_old_files(settings.download_dir, max_age, protected)
    pruned = job_manager.prune(max_age)
    if removed or pruned:
        log.info("Cleanup removed %d old file(s) and forgot %d finished job(s)", removed, pruned)


def _cleanup_loop() -> None:
    while not _cleanup_stop.wait(CLEANUP_INTERVAL_SECONDS):
        try:
            _cleanup_once()
        except Exception:  # noqa: BLE001 - never let the janitor die
            log.exception("Cleanup failed")


def _describe_limits() -> str:
    parts = [f"files kept {settings.file_retention_hours:g} h", f"{settings.max_concurrent_downloads} parallel"]
    if settings.max_file_mb:
        parts.append(f"max file {settings.max_file_mb} MB")
    if settings.max_storage_mb:
        parts.append(f"storage quota {settings.max_storage_mb} MB")
    if settings.max_duration_minutes:
        parts.append(f"max length {settings.max_duration_minutes} min")
    if settings.job_timeout_minutes:
        parts.append(f"job timeout {settings.job_timeout_minutes} min")
    if not settings.allow_h264_conversion:
        parts.append("H.264 conversion off")
    if settings.allowed_channels:
        parts.append(f"{len(settings.allowed_channels)} approved channel(s) only")
    return ", ".join(parts)


@asynccontextmanager
async def lifespan(_app: FastAPI):
    setup_logging(settings.log_dir)
    problem = settings.startup_problem()
    if problem:
        log.error(problem)
        raise RuntimeError(problem)
    for directory in (settings.download_dir, settings.work_dir, settings.cache_dir):
        directory.mkdir(parents=True, exist_ok=True)
    clear_work_dir(settings.work_dir)  # partial files left by a crash or forced shutdown
    await run_in_threadpool(_cleanup_once)
    report = await run_in_threadpool(tool_registry.get, True)
    log.info("%s %s starting - %s", APP_NAME, __version__, python_info())
    if settings.is_public:
        log.info("Public mode: password %s, hosts %s, rate limits %s; %s",
                 "required" if settings.auth_enabled else "MISSING", ", ".join(settings.allowed_hosts),
                 "on" if settings.rate_limits else "off", _describe_limits())
    for problem in report.problems():
        level = log.warning if problem["severity"] != "info" else log.info
        level("%s: %s", problem["title"], problem["message"])
    tool_registry.check_for_updates_async()
    _cleanup_stop.clear()
    janitor = threading.Thread(target=_cleanup_loop, name="cleanup", daemon=True)
    janitor.start()
    try:
        yield
    finally:
        _cleanup_stop.set()
        log.info("Shutting down: cancelling active downloads")
        await run_in_threadpool(job_manager.shutdown, 8.0)
        clear_work_dir(settings.work_dir)


app = FastAPI(title=APP_NAME, version=__version__, lifespan=lifespan, docs_url=None, redoc_url=None,
              openapi_url=None)


# --------------------------------------------------------------------------- #
# Security
# --------------------------------------------------------------------------- #

CONTENT_SECURITY_POLICY = "; ".join([
    "default-src 'self'",
    "img-src 'self' data: https://i.ytimg.com https://*.ytimg.com https://*.ggpht.com https://*.googleusercontent.com",
    "style-src 'self'",
    "script-src 'self'",
    "font-src 'self'",
    "connect-src 'self'",
    "object-src 'none'",
    "base-uri 'none'",
    "form-action 'self'",
    "frame-ancestors 'none'",
])
SECURITY_HEADERS = {
    "Content-Security-Policy": CONTENT_SECURITY_POLICY,
    "X-Content-Type-Options": "nosniff",
    "X-Frame-Options": "DENY",
    # "same-origin" still sends nothing to other sites, but unlike "no-referrer" it lets browsers send the
    # real Origin header with the sign-in/sign-out form posts (they send "Origin: null" otherwise).
    "Referrer-Policy": "same-origin",
    "Cross-Origin-Opener-Policy": "same-origin",
    "Cross-Origin-Resource-Policy": "same-origin",
    "Permissions-Policy": "camera=(), microphone=(), geolocation=(), clipboard-read=(self)",
}
UNSAFE_METHODS = {"POST", "PUT", "PATCH", "DELETE"}
HEALTHZ_PATH = "/healthz"
# Reachable without signing in: the login page, its assets and the platform health check.
PUBLIC_PATHS = {"/login", "/logout", "/favicon.ico", HEALTHZ_PATH}
LOGIN_FAILURES_PER_CLIENT = (10, 15 * 60)  # failed attempts per client IP per window (seconds)
LOGIN_FAILURES_GLOBAL = (30, 5 * 60)  # all clients together: caps guessing even with spoofed IPs
REQUESTS_PER_CLIENT = (600, 60)  # flood guard for everything except static files


def _error_response(code: str, title: str, message: str, status: int) -> JSONResponse:
    return JSONResponse({"error": {"code": code, "title": title, "message": message, "retryable": False}},
                        status_code=status)


def _host_allowed(host: str) -> bool:
    """Same matching rules as Starlette's TrustedHostMiddleware ("*" and "*.example.com")."""
    host = host.strip("[]").lower()
    for pattern in settings.allowed_hosts:
        pattern = pattern.strip("[]").lower()
        if pattern == "*" or host == pattern or (pattern.startswith("*.") and host.endswith(pattern[1:])):
            return True
    return False


class HostCheckMiddleware(TrustedHostMiddleware):
    """Rejects unknown Host headers, except for the health check (platform probes use internal hosts)."""

    async def __call__(self, scope, receive, send) -> None:
        if scope["type"] == "http" and scope["path"] == HEALTHZ_PATH:
            await self.app(scope, receive, send)
            return
        await super().__call__(scope, receive, send)


def _client_ip(request: Request) -> str:
    # Behind a trusted proxy (MD_TRUST_PROXY), uvicorn has already replaced this with X-Forwarded-For.
    return request.client.host if request.client else "unknown"


def _is_loopback_client(request: Request) -> bool:
    if settings.trust_proxy:
        return False  # forwarded addresses can be spoofed, so they never grant password-free access
    host = _client_ip(request)
    try:
        address = ipaddress.ip_address(host)
    except ValueError:
        return host == "localhost"
    if isinstance(address, ipaddress.IPv6Address) and address.ipv4_mapped:
        address = address.ipv4_mapped
    return address.is_loopback


@functools.lru_cache(maxsize=4)
def _make_signer(secret_key: str | None, password: str, lifetime_seconds: float) -> SessionSigner:
    return SessionSigner(secret_key, password, lifetime_seconds)


def _signer() -> SessionSigner | None:
    if not settings.auth_enabled:
        return None
    return _make_signer(settings.secret_key, settings.password, settings.session_hours * 3600)


def _session_id(request: Request) -> str | None:
    signer = _signer()
    return signer.verify(request.cookies.get(SESSION_COOKIE)) if signer else None


def _is_public_path(path: str) -> bool:
    return path in PUBLIC_PATHS or path.startswith("/static/")


def _auth_required(request: Request) -> Response:
    if request.url.path.startswith("/api/"):
        return _error_response("auth_required", "Signed out", "Your session has ended. Sign in again.", 401)
    return RedirectResponse("/login", status_code=303)


def _reject_request(request: Request) -> Response | None:
    """The response for a request that must not reach the app, or None to let it through."""
    path = request.url.path
    if path != HEALTHZ_PATH:
        if not settings.auth_enabled:
            # Without a password the app is a local tool: only this computer may use it, whatever
            # interface the server was bound to.
            if not _is_loopback_client(request):
                return _error_response("remote_access_disabled", "Access denied",
                                       "This server only accepts connections from its own computer. Set "
                                       "MD_PASSWORD to allow access from other devices.", 403)
        elif not _is_public_path(path) and _session_id(request) is None:
            return _auth_required(request)
        if settings.rate_limits and not path.startswith("/static/"):
            limit, window = REQUESTS_PER_CLIENT
            if rate_limiter.hit(f"req:{_client_ip(request)}", limit, window):
                return _error_response("too_many_requests", "Too many requests",
                                       "You're sending requests too quickly. Wait a minute and try again.", 429)
    if request.method in UNSAFE_METHODS:
        # Block cross-site requests from other web pages (CSRF / DNS-rebinding style attacks).
        origin = request.headers.get("origin")
        if origin:
            origin_host = (urllib.parse.urlsplit(origin).hostname or "").lower()
            if not _host_allowed(origin_host):
                return _error_response("forbidden_origin", "Request blocked",
                                       "Requests from other websites are not allowed.", 403)
        if request.headers.get("sec-fetch-site") == "cross-site":
            return _error_response("forbidden_origin", "Request blocked",
                                   "Requests from other websites are not allowed.", 403)
        content_type = request.headers.get("content-type", "")
        if path.startswith("/api/") and "content-length" in request.headers \
                and request.headers.get("content-length") != "0" and "application/json" not in content_type:
            return _error_response("unsupported_media_type", "Unsupported request",
                                   "Requests must be sent as JSON.", 415)
    return None


@app.middleware("http")
async def security_middleware(request: Request, call_next):
    response = _reject_request(request) or await call_next(request)
    for header, value in SECURITY_HEADERS.items():
        response.headers.setdefault(header, value)
    if request.url.scheme == "https":
        response.headers.setdefault("Strict-Transport-Security", "max-age=31536000")
    path = request.url.path
    if path.startswith("/api/") or path in ("/", "/login"):
        response.headers.setdefault("Cache-Control", "no-store")
    return response


app.add_middleware(HostCheckMiddleware, allowed_hosts=settings.allowed_hosts)
app.mount("/static", StaticFiles(directory=str(settings.static_dir)), name="static")


# --------------------------------------------------------------------------- #
# Error handling
# --------------------------------------------------------------------------- #

# In public mode the people using the app aren't on the server's computer, so messages that point to
# start.bat/update.bat or "your connection" are replaced with ones that make sense for a hosted server.
HOSTED_MESSAGES = {
    "ffmpeg_missing": "FFmpeg isn't available on the server, so downloads are disabled. The server needs to be "
                      "redeployed.",
    "ffprobe_missing": "ffprobe isn't available on the server. The server needs to be redeployed.",
    "mp3_encoder_missing": "The server's FFmpeg has no MP3 encoder, so MP3 downloads are unavailable.",
    "js_runtime_missing": "YouTube's JavaScript challenge couldn't be solved, so some or all formats are "
                          "unavailable. The server may need to be redeployed with the latest yt-dlp.",
    "bot_check": "YouTube asked this server to confirm it isn't a bot. YouTube often blocks requests from cloud "
                 "hosting providers, so this can keep happening. Try again later.",
    "rate_limited": "YouTube is temporarily limiting requests from this server. Wait a few minutes and try again.",
    "forbidden": "YouTube refused the download (HTTP 403). Analyze the video again and retry. YouTube sometimes "
                 "blocks downloads from cloud servers.",
    "geo_restricted": "The uploader hasn't made this video available in the country where this server runs.",
    "network_timeout": "The server's connection to YouTube timed out. Try again.",
    "network_error": "The server couldn't reach YouTube. Try again in a moment.",
    "extraction_failed": "yt-dlp couldn't extract this video. YouTube may have changed something; the server "
                         "needs to be redeployed with the latest yt-dlp.",
    "disk_full": "The server ran out of temporary storage. Try again later or choose a lower quality.",
}


def _present_error(error: dict[str, Any] | None) -> dict[str, Any] | None:
    if not error or not settings.is_public:
        return error
    error = dict(error)
    error["message"] = HOSTED_MESSAGES.get(error.get("code", ""), error.get("message", "")).replace(
        "Details were written to logs/app.log.", "Details were written to the server log.")
    return error


def _present_problems(problems: list[dict[str, str]], latest: str | None, installed: str | None):
    if not settings.is_public:
        return problems
    presented = []
    for problem in problems:
        problem = dict(problem)
        if problem["code"] == "yt_dlp_update":
            problem["message"] = (f"yt-dlp {latest} is available (installed: {installed}). Redeploy the server "
                                  "to keep YouTube downloads working reliably.")
        elif problem["code"] == "js_runtime_fallback":
            problem["message"] = "Deno was not found on the server, so Node.js is used to solve YouTube challenges."
        else:
            problem["message"] = HOSTED_MESSAGES.get(problem["code"], problem["message"])
        presented.append(problem)
    return presented


@app.exception_handler(AppError)
async def handle_app_error(_request: Request, exc: AppError) -> JSONResponse:
    return JSONResponse({"error": _present_error(exc.to_dict())}, status_code=exc.status)


@app.exception_handler(RequestValidationError)
async def handle_validation_error(_request: Request, exc: RequestValidationError) -> JSONResponse:
    errors = exc.errors()
    field = ".".join(str(p) for p in errors[0].get("loc", [])[1:]) if errors else ""
    message = f"Invalid value for '{field}'." if field else "The request was not valid."
    if field == "url":
        message = "Please paste a valid YouTube link."
    log.debug("Validation error: %s", errors)
    return _error_response("invalid_request", "Invalid request", message, 422)


@app.exception_handler(StarletteHTTPException)
async def handle_http_error(_request: Request, exc: StarletteHTTPException) -> JSONResponse:
    titles = {404: "Not found", 405: "Method not allowed"}
    return _error_response(f"http_{exc.status_code}", titles.get(exc.status_code, "Error"),
                           str(exc.detail), exc.status_code)


@app.exception_handler(Exception)
async def handle_unexpected_error(request: Request, exc: Exception) -> JSONResponse:
    log.error("Unhandled error on %s %s: %s", request.method, request.url.path, exc)
    log.debug("Traceback", exc_info=exc)
    where = "the server log" if settings.is_public else "logs/app.log"
    return _error_response("internal_error", "Something went wrong",
                           f"An unexpected server error occurred. Details were written to {where}.", 500)


# --------------------------------------------------------------------------- #
# Request models
# --------------------------------------------------------------------------- #

class AnalyzeRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    url: str = Field(..., min_length=1, max_length=4096)


class DownloadRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    url: str = Field(..., min_length=1, max_length=4096)
    format: Literal["mp4", "mp3"]
    quality: str = Field(..., min_length=1, max_length=16, pattern=r"^[a-z0-9-]+$")
    convert_h264: bool = False


# --------------------------------------------------------------------------- #
# Pages
# --------------------------------------------------------------------------- #

def _asset_version() -> str:
    digest = hashlib.sha1()
    for relative in ("css/style.css", "js/app.js", "js/theme-init.js"):
        path = settings.static_dir / relative
        try:
            stat = path.stat()
            digest.update(f"{relative}:{stat.st_mtime_ns}:{stat.st_size}".encode())
        except OSError:
            continue
    return digest.hexdigest()[:10]


def _retention_text() -> str:
    minutes = round(settings.file_retention_hours * 60)
    if minutes < 120:
        return f"{minutes} minutes"
    hours = settings.file_retention_hours
    return f"{hours:g} hours" if hours < 48 else f"{hours / 24:g} days"


def _page_context(**extra: Any) -> dict[str, Any]:
    return {
        "app_name": APP_NAME,
        "version": __version__,
        "asset_version": _asset_version(),
        "public": settings.is_public,
        "auth_enabled": settings.auth_enabled,
        "retention": _retention_text(),
        **extra,
    }


@app.get("/", response_class=HTMLResponse, include_in_schema=False)
def index(request: Request):
    return templates.TemplateResponse(request, "index.html", _page_context())


@app.get("/favicon.ico", include_in_schema=False)
def favicon():
    return FileResponse(settings.static_dir / "img" / "favicon.svg", media_type="image/svg+xml")


# --------------------------------------------------------------------------- #
# Sign-in (public mode)
# --------------------------------------------------------------------------- #

def _login_page(request: Request, error: str | None = None, status: int = 200) -> HTMLResponse:
    return templates.TemplateResponse(request, "login.html", _page_context(error=error), status_code=status)


async def _read_form(request: Request, limit: int = 4096) -> dict[str, str]:
    body = bytearray()
    async for chunk in request.stream():
        body += chunk
        if len(body) > limit:
            raise AppError("request_too_large", "Request too large", "The request was too large.", status=413)
    try:
        fields = urllib.parse.parse_qs(body.decode("utf-8", "replace"), max_num_fields=5)
    except ValueError:
        fields = {}
    return {key: values[0] for key, values in fields.items() if values}


@app.get("/login", response_class=HTMLResponse, include_in_schema=False)
def login_form(request: Request):
    if not settings.auth_enabled or _session_id(request):
        return RedirectResponse("/", status_code=303)
    return _login_page(request)


@app.post("/login", include_in_schema=False)
async def login(request: Request):
    if not settings.auth_enabled:
        return RedirectResponse("/", status_code=303)
    client_key = f"login:{_client_ip(request)}"
    for key, (limit, window) in ((client_key, LOGIN_FAILURES_PER_CLIENT), ("login:*", LOGIN_FAILURES_GLOBAL)):
        wait = rate_limiter.retry_after(key, limit, window)
        if wait:
            log.warning("Login blocked for %s: too many failed attempts", _client_ip(request))
            return _login_page(request, f"Too many failed attempts. Try again in {max(1, round(wait / 60))} "
                                        "minute(s).", status=429)
    form = await _read_form(request)
    if not password_matches(form.get("password", ""), settings.password or ""):
        for key, (_limit, window) in ((client_key, LOGIN_FAILURES_PER_CLIENT), ("login:*", LOGIN_FAILURES_GLOBAL)):
            rate_limiter.record(key, window)
        log.warning("Failed sign-in from %s", _client_ip(request))
        await asyncio.sleep(1.0)  # slows down guessing
        return _login_page(request, "Wrong password.", status=401)

    rate_limiter.reset(client_key)
    signer = _signer()
    assert signer is not None
    response = RedirectResponse("/", status_code=303)
    response.set_cookie(SESSION_COOKIE, signer.issue(), max_age=int(signer.lifetime_seconds), httponly=True,
                        secure=request.url.scheme == "https", samesite="lax", path="/")
    log.info("Signed in from %s", _client_ip(request))
    return response


@app.post("/logout", include_in_schema=False)
def logout(request: Request):
    signer = _signer()
    if signer is None:
        return RedirectResponse("/", status_code=303)
    signer.revoke(request.cookies.get(SESSION_COOKIE))
    response = RedirectResponse("/login", status_code=303)
    response.delete_cookie(SESSION_COOKIE, path="/", httponly=True, secure=request.url.scheme == "https",
                           samesite="lax")
    return response


# --------------------------------------------------------------------------- #
# Health check for the hosting platform (no sign-in, no details)
# --------------------------------------------------------------------------- #

@app.api_route(HEALTHZ_PATH, methods=["GET", "HEAD"], include_in_schema=False)
def healthz():
    report = tool_registry.get()
    storage_ok = settings.download_dir.is_dir() and os.access(settings.download_dir, os.W_OK)
    ok = report.ffmpeg_ok and storage_ok
    return JSONResponse({"status": "ok" if ok else "unhealthy", "version": __version__},
                        status_code=200 if ok else 503, headers={"Cache-Control": "no-store"})


# --------------------------------------------------------------------------- #
# API
# --------------------------------------------------------------------------- #

def _notices_from_warnings(warnings: list[str]) -> list[dict[str, str]]:
    text = " ".join(warnings).lower()
    notices = []
    if "challenge solving failed" in text or "signature solving failed" in text or "javascript runtime" in text:
        fix = ("The server may need to be redeployed with the latest yt-dlp." if settings.is_public
               else "Make sure Deno is installed and run update.bat to update yt-dlp.")
        notices.append({
            "severity": "warning",
            "message": f"YouTube's JavaScript challenge couldn't be fully solved, so some qualities may be missing. "
                       f"{fix}",
        })
    return notices


def _rate_limit(request: Request, action: str, per_hour: int) -> None:
    """Per-user hourly quota (per session when signed in, else per client IP)."""
    if not settings.rate_limits:
        return
    subject = _session_id(request) or _client_ip(request)
    wait = rate_limiter.hit(f"{action}:{subject}", per_hour, 3600)
    if wait:
        raise AppError("too_many_requests", "Limit reached",
                       f"This server allows {per_hour} {action} per hour. Try again in about "
                       f"{max(1, round(wait / 60))} minute(s).", status=429, retryable=True)


def _limits() -> dict[str, Any]:
    return {
        "max_file_bytes": settings.max_file_bytes,
        "max_duration_seconds": settings.max_duration_seconds,
        "file_retention_hours": settings.file_retention_hours,
    }


def _snapshot(job: Job) -> dict[str, Any]:
    data = job_manager.snapshot(job)
    data["error"] = _present_error(data.get("error"))
    return data


@app.get("/api/health")
def health(refresh: bool = False):
    report = tool_registry.get(refresh=refresh)
    tools = report.to_dict()
    tools["problems"] = _present_problems(tools["problems"], report.latest_yt_dlp_version, report.yt_dlp_version)
    return {
        "app": APP_ID,
        "name": APP_NAME,
        "version": __version__,
        "status": "ok" if report.ffmpeg_ok else "degraded",
        "tools": tools,
        "settings": {
            "max_concurrent_downloads": settings.max_concurrent_downloads,
            "file_retention_hours": settings.file_retention_hours,
            "public": settings.is_public,
            "auth_enabled": settings.auth_enabled,
            "limits": _limits(),
        },
        "active_jobs": len(job_manager.active_ids()),
    }


@app.post("/api/analyze")
def analyze(payload: AnalyzeRequest, request: Request):
    ref = parse_youtube_url(payload.url)
    _rate_limit(request, "analyses", settings.analyses_per_hour)
    tools = tool_registry.get()
    if not analysis_slots.acquire(timeout=60):
        raise AppError("busy", "Server busy", "Too many videos are being analyzed at once. Try again shortly.",
                       status=503, retryable=True)
    try:
        info, warnings = extract_info(ref, tools, settings)
    finally:
        analysis_slots.release()
    info_cache.put(ref.video_id, info)
    mp3 = build_mp3_options(info)
    mp3["encoder_available"] = tools.can_encode_mp3
    return {
        "video": summarize_video(info, ref.canonical_url, ref.is_short),
        "mp4": video_formats_summary(info, tools.h264_encoder is not None and settings.allow_h264_conversion),
        "mp3": mp3,
        "notices": _notices_from_warnings(warnings),
        "ffmpeg_available": tools.ffmpeg_ok,
        "limits": _limits(),
    }


@app.post("/api/download", status_code=202)
def start_download(payload: DownloadRequest, request: Request):
    ref = parse_youtube_url(payload.url)
    validate_quality_id(payload.format, payload.quality)
    convert_h264 = payload.convert_h264 and payload.format == "mp4"
    if convert_h264 and not settings.allow_h264_conversion:
        raise AppError("h264_disabled", "Conversion turned off",
                       "Converting video to H.264 is turned off on this server because it needs a lot of CPU time.",
                       status=403)
    tools = tool_registry.get()
    if not tools.ffmpeg_ok:
        raise AppError("ffmpeg_missing", "FFmpeg is not installed",
                       "FFmpeg and ffprobe are required to create MP4 and MP3 files. Run start.bat to install "
                       "FFmpeg or follow the steps in README.md, then try again.", status=503)
    if payload.format == "mp3" and not tools.can_encode_mp3:
        raise AppError("mp3_encoder_missing", "MP3 encoder missing",
                       "The installed FFmpeg has no MP3 encoder (libmp3lame).", status=503)
    if convert_h264 and not tools.h264_encoder:
        raise AppError("h264_encoder_missing", "H.264 encoder missing",
                       "The installed FFmpeg has no H.264 encoder (libx264).", status=503)

    job_request = JobRequest(ref.video_id, ref.canonical_url, payload.format, payload.quality, convert_h264)
    title = thumbnail = None
    info = info_cache.get(ref.video_id)
    if info is not None:
        # Fail fast if the selection doesn't match the analysed formats or is over the size limit.
        check_file_size(resolve_plan(info, payload.format, payload.quality, convert_h264), settings)
        title = info.get("title")
        thumbs = thumbnail_candidates(info)
        thumbnail = thumbs[0] if thumbs else None

    existing = job_manager.find_active(job_request)
    if existing is not None:
        return {"job_id": existing.id, "existing": True, "job": _snapshot(existing)}
    _rate_limit(request, "downloads", settings.downloads_per_hour)
    job = job_manager.submit(job_request, title=title, thumbnail=thumbnail)
    return {"job_id": job.id, "existing": False, "job": _snapshot(job)}


def _job_or_404(job_id: str):
    job = job_manager.get(job_id) if is_valid_job_id(job_id) else None
    if job is None:
        raise AppError("job_not_found", "Download not found",
                       "This download doesn't exist anymore. The app may have been restarted.", status=404)
    return job


@app.get("/api/progress/{job_id}")
def progress(job_id: str):
    return _snapshot(_job_or_404(job_id))


@app.post("/api/cancel/{job_id}")
def cancel(job_id: str):
    job = _job_or_404(job_id)
    job_manager.cancel(job.id)
    return _snapshot(job)


def content_disposition(filename: str) -> str:
    """RFC 6266 header with an ASCII fallback plus the exact UTF-8 name."""
    stem, dot, ext = filename.rpartition(".")
    if not dot:
        stem, ext = filename, ""
    ascii_stem = unicodedata.normalize("NFKD", stem).encode("ascii", "ignore").decode("ascii")
    ascii_stem = re.sub(r"[^A-Za-z0-9 ._()\[\]-]+", "_", ascii_stem)
    ascii_stem = re.sub(r"_{2,}", "_", re.sub(r"\s{2,}", " ", ascii_stem)).strip(" ._") or "download"
    fallback = f"{ascii_stem}.{ext}" if ext else ascii_stem
    encoded = urllib.parse.quote(filename, safe="")
    return f"attachment; filename=\"{fallback}\"; filename*=UTF-8''{encoded}"


@app.api_route("/api/file/{job_id}", methods=["GET", "HEAD"])
def download_file(job_id: str):
    job = _job_or_404(job_id)
    if job.status != JobStatus.COMPLETED or job.result_path is None:
        raise AppError("not_ready", "File not ready", "This download hasn't finished yet.", status=409)
    path = job.result_path
    if not is_within(path, settings.download_dir) or not path.is_file():
        raise AppError("file_gone", "File no longer available",
                       "The file was removed by the automatic cleanup. Download it again.", status=410)
    media_type = "audio/mpeg" if path.suffix.lower() == ".mp3" else "video/mp4"
    log.info("Serving %s", path.name)
    return FileResponse(path, media_type=media_type, headers={
        "Content-Disposition": content_disposition(path.name),
        "Cache-Control": "no-store",
    })


# --------------------------------------------------------------------------- #
# Server entry point
# --------------------------------------------------------------------------- #

def _port_is_free(host: str, port: int) -> bool:
    family = socket.AF_INET6 if ":" in host else socket.AF_INET
    with socket.socket(family, socket.SOCK_STREAM) as sock:
        if hasattr(socket, "SO_EXCLUSIVEADDRUSE"):
            sock.setsockopt(socket.SOL_SOCKET, socket.SO_EXCLUSIVEADDRUSE, 1)
        try:
            sock.bind((host, port))
        except OSError:
            return False
    return True


def _running_instance(url: str) -> bool:
    try:
        with urllib.request.urlopen(f"{url}/api/health", timeout=2) as response:  # noqa: S310 - local URL
            return json.load(response).get("app") == APP_ID
    except Exception:  # noqa: BLE001
        return False


def _open_when_ready(url: str, timeout: float = 30.0) -> None:
    def worker() -> None:
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if _running_instance(url):
                webbrowser.open(url)
                return
            time.sleep(0.3)
        log.warning("Server did not become ready in time; open %s manually", url)

    threading.Thread(target=worker, name="open-browser", daemon=True).start()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=f"Run {APP_NAME}.")
    parser.add_argument("--host", default=settings.host, help="interface to bind (default: MD_HOST or 127.0.0.1)")
    parser.add_argument("--port", type=int, default=settings.port,
                        help="port (default: MD_PORT, or PORT when hosted, or 8000)")
    parser.add_argument("--open", action="store_true", help="open the app in the default browser")
    args = parser.parse_args(argv)

    setup_logging(settings.log_dir)
    host = args.host
    problem = settings.startup_problem(host)
    if problem:
        log.error(problem)
        return 2
    local = host in ("127.0.0.1", "localhost", "::1")
    browse_host = "127.0.0.1" if host in ("0.0.0.0", "::") else host
    port = args.port
    if not _port_is_free(host, port):
        existing = f"http://{browse_host}:{port}"
        if _running_instance(existing):
            print(f"{APP_NAME} is already running at {existing}")
            if args.open and not os.environ.get("MD_NO_BROWSER"):
                webbrowser.open(existing)
            return 0
        if not local:
            # A hosting platform expects exactly this port; moving elsewhere would only hide the problem.
            log.error("Port %d is already in use.", port)
            return 1
        for candidate in range(port + 1, port + 21):
            if _port_is_free(host, candidate):
                log.warning("Port %d is in use by another program; using port %d instead", port, candidate)
                port = candidate
                break
        else:
            log.error("Ports %d-%d are all in use. Set MD_PORT to a free port.", port, port + 20)
            return 1
    if not local:
        log.warning("Listening on %s - other devices can reach this app; sign-in with MD_PASSWORD is required.",
                    host)

    url = f"http://{browse_host}:{port}"
    print(f"\n  {APP_NAME} {__version__} is running at {url}\n  Press Ctrl+C to stop.\n", flush=True)
    if args.open and not os.environ.get("MD_NO_BROWSER"):
        _open_when_ready(url)

    import uvicorn

    # Behind a hosting platform's HTTPS proxy, take the client address and scheme (https) from its
    # X-Forwarded-* headers. Otherwise ignore those headers entirely.
    uvicorn.run(app, host=host, port=port, log_config=None, access_log=False, timeout_graceful_shutdown=10,
                proxy_headers=settings.trust_proxy, forwarded_allow_ips="*" if settings.trust_proxy else None)
    return 0


if __name__ == "__main__":
    sys.exit(main())

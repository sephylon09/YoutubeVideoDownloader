"""yt-dlp integration through its maintained Python API.

* ``extract_info`` retrieves metadata and the full format list.
* ``download_streams`` downloads the exact raw streams of a plan into a
  private work folder. Merging and conversion are done afterwards by
  ``ffmpeg.py`` so every step reports progress and can be cancelled.
"""

from __future__ import annotations

import threading
import time
import urllib.request
from collections import OrderedDict
from pathlib import Path
from typing import Any, Callable

import yt_dlp
from yt_dlp.utils import DownloadCancelled

from .config import Settings
from .environment import ToolReport
from .errors import AppError, JobCancelled, classify_error
from .formats import DownloadPlan, is_safe_thumbnail_url, thumbnail_candidates
from .logging_setup import get_logger
from .storage import is_within
from .validation import VideoRef

log = get_logger("yt-dlp")

# Top-level fields that describe the format selected during analysis. They are
# removed before a cached info dict is reused so yt-dlp starts from a clean
# slate (the same thing ``--load-info-json`` does with private keys).
_SELECTION_KEYS = (
    "format_id", "format", "format_note", "url", "manifest_url", "ext", "protocol", "width", "height",
    "resolution", "fps", "vcodec", "acodec", "vbr", "abr", "tbr", "asr", "filesize", "filesize_approx",
    "dynamic_range", "aspect_ratio", "audio_channels", "quality", "has_drm", "container", "http_headers",
    "downloader_options", "fragments", "fragment_base_url", "source_preference", "preference", "video_ext",
    "audio_ext", "stretched_ratio", "available_at", "language", "language_preference",
)


class YtdlpLogger:
    """Adapter that sends yt-dlp output to our logger and records warnings."""

    def __init__(self, logger=log, prefix: str = "") -> None:
        self._log = logger
        self._prefix = prefix
        self.warnings: list[str] = []

    def debug(self, message: str) -> None:
        self._log.debug("%s%s", self._prefix, message.removeprefix("[debug] "))

    def info(self, message: str) -> None:
        self._log.debug("%s%s", self._prefix, message)

    def warning(self, message: str) -> None:
        self.warnings.append(message)
        self._log.warning("%s%s", self._prefix, message)

    def error(self, message: str) -> None:
        self._log.warning("%s%s", self._prefix, message)


def build_options(tools: ToolReport, settings: Settings, logger: YtdlpLogger, **extra: Any) -> dict[str, Any]:
    options: dict[str, Any] = {
        "logger": logger,
        "quiet": True,
        "no_warnings": False,
        "noprogress": True,
        "color": "no_color",
        "noplaylist": True,
        "socket_timeout": settings.socket_timeout_seconds,
        "retries": 10,
        "fragment_retries": 10,
        "extractor_retries": 2,
        "file_access_retries": 5,
        "concurrent_fragment_downloads": 4,
        "js_runtimes": tools.js_runtimes_param(),
        "cachedir": str(settings.cache_dir / "yt-dlp"),
        "windowsfilenames": True,
        "updatetime": False,  # keep real mtimes so old-file cleanup works
    }
    if tools.ffmpeg_dir:
        options["ffmpeg_location"] = tools.ffmpeg_dir
    if settings.ytdlp_proxy:
        options["proxy"] = settings.ytdlp_proxy
    options.update(extra)
    return options


def _js_hint(warnings: list[str]) -> bool:
    text = " ".join(warnings).lower()
    return "javascript runtime" in text or "challenge solving failed" in text or "signature solving failed" in text


def check_supported(info: dict[str, Any], warnings: list[str] | None = None) -> None:
    """Reject content this app intentionally doesn't handle."""
    live_status = info.get("live_status")
    if info.get("_type") in ("playlist", "multi_video"):
        raise AppError("unsupported_url", "Unsupported link", "Paste a link to a single video, not a playlist.")
    if live_status == "is_live" or (info.get("is_live") and live_status != "was_live"):
        raise AppError("live_not_supported", "Live streams aren't supported",
                       "This video is a live stream that is currently broadcasting. Try again after it has ended.",
                       status=422)
    if live_status == "is_upcoming":
        raise AppError("upcoming", "Video hasn't started yet",
                       "This is a scheduled premiere or live stream that hasn't started yet.", status=422)
    if live_status == "post_live":
        raise AppError("post_live", "Stream still processing",
                       "This live stream just ended and YouTube is still processing it. Try again later.",
                       status=422, retryable=True)
    formats = info.get("formats") or []
    if formats and all(f.get("has_drm") for f in formats if f.get("format_note") != "storyboard"):
        raise AppError("drm_protected", "Protected content",
                       "This video is DRM-protected and can't be downloaded.", status=422)
    if not formats:
        if warnings and _js_hint(warnings):
            raise AppError("js_runtime_missing", "JavaScript runtime required",
                           "YouTube's JavaScript challenge couldn't be solved, so no formats are available. "
                           "Make sure Deno is installed (run start.bat) and yt-dlp is up to date (update.bat).",
                           status=422, retryable=True)
        raise AppError("no_formats", "No downloadable formats",
                       "YouTube didn't return any downloadable formats for this video.", status=422)


def _channel_keys(info: dict[str, Any]) -> set[str]:
    keys = set()
    for field in ("channel_id", "uploader_id", "channel_url", "uploader_url"):
        value = str(info.get(field) or "").strip().rstrip("/")
        if value:
            keys.add(value.rsplit("/", 1)[-1].lower().lstrip("@"))
    return keys


def check_allowed(info: dict[str, Any], settings: Settings) -> None:
    """Apply this server's own limits (video length, approved channels)."""
    limit = settings.max_duration_seconds
    duration = info.get("duration")
    if limit and isinstance(duration, (int, float)) and duration > limit:
        raise AppError("too_long", "Video too long",
                       f"This server only processes videos up to {limit // 60} minutes long. This one is "
                       f"{int(duration // 60)} minutes.", status=422)
    if settings.allowed_channels:
        allowed = {c.strip().rstrip("/").rsplit("/", 1)[-1].lower().lstrip("@") for c in settings.allowed_channels}
        if not _channel_keys(info) & allowed:
            raise AppError("channel_not_allowed", "Channel not allowed",
                           "This server only downloads videos from channels its owner has approved.", status=403)


def extract_info(ref: VideoRef, tools: ToolReport, settings: Settings, logger=log) -> tuple[dict[str, Any], list[str]]:
    """Fetch metadata and formats for one video (no download)."""
    ylog = YtdlpLogger(logger, prefix=f"[{ref.video_id}] ")
    options = build_options(tools, settings, ylog, skip_download=True)
    started = time.monotonic()
    try:
        with yt_dlp.YoutubeDL(options) as ydl:
            raw = ydl.extract_info(ref.canonical_url, download=False)
            info = ydl.sanitize_info(raw)
    except Exception as exc:  # noqa: BLE001 - yt-dlp raises many types; all are classified
        error = classify_error(exc, fallback_stage="reading the video information")
        if error.code in ("unexpected_error", "extraction_failed", "format_unavailable") and _js_hint(ylog.warnings):
            error = classify_error(Exception("challenge solving failed " + " ".join(ylog.warnings)))
        logger.warning("Extraction failed for %s: %s (%s)", ref.video_id, error.code, exc)
        raise error from exc
    if not isinstance(info, dict):
        raise AppError("extraction_failed", "Couldn't read video information",
                       "yt-dlp returned no information for this video.", status=502, retryable=True)
    check_supported(info, ylog.warnings)
    check_allowed(info, settings)
    logger.info("Analyzed %s in %.1fs: %r (%d formats)", ref.video_id, time.monotonic() - started,
                info.get("title"), len(info.get("formats") or []))
    return info, ylog.warnings


class InfoCache:
    """Short-lived cache of analysed videos so a download can start immediately.

    Format URLs expire after several hours; the TTL is far below that.
    """

    def __init__(self, ttl_seconds: float, max_entries: int = 32) -> None:
        self._ttl = ttl_seconds
        self._max = max_entries
        self._items: OrderedDict[str, tuple[float, dict[str, Any]]] = OrderedDict()
        self._lock = threading.Lock()

    def get(self, video_id: str) -> dict[str, Any] | None:
        with self._lock:
            entry = self._items.get(video_id)
            if not entry:
                return None
            stored_at, info = entry
            if time.monotonic() - stored_at > self._ttl:
                del self._items[video_id]
                return None
            self._items.move_to_end(video_id)
            return info

    def put(self, video_id: str, info: dict[str, Any]) -> None:
        if self._ttl <= 0:
            return
        with self._lock:
            self._items[video_id] = (time.monotonic(), info)
            self._items.move_to_end(video_id)
            while len(self._items) > self._max:
                self._items.popitem(last=False)

    def evict(self, video_id: str) -> None:
        with self._lock:
            self._items.pop(video_id, None)


def prepare_for_download(info: dict[str, Any]) -> dict[str, Any]:
    """Copy of a sanitised info dict that yt-dlp can process again."""
    clean = yt_dlp.YoutubeDL.sanitize_info(dict(info), remove_private_keys=True)
    for key in _SELECTION_KEYS:
        clean.pop(key, None)
    return clean


ProgressHook = Callable[[dict[str, Any]], None]


def download_streams(
    info: dict[str, Any],
    plan: DownloadPlan,
    work_dir: Path,
    *,
    tools: ToolReport,
    settings: Settings,
    cancel_event: threading.Event,
    on_progress: ProgressHook,
    logger=log,
    max_bytes: int | None = None,
) -> dict[str, Path]:
    """Download each stream of ``plan`` as its own file. Returns {format_id: path}.

    With ``max_bytes`` the download is aborted as soon as the streams together
    grow beyond that size (sizes aren't always known in advance).
    """
    ylog = YtdlpLogger(logger)
    received: dict[str, int] = {}
    too_large = threading.Event()

    def hook(status: dict[str, Any]) -> None:
        if cancel_event.is_set():
            raise DownloadCancelled("Cancelled by user")
        if max_bytes:
            format_id = str((status.get("info_dict") or {}).get("format_id") or "")
            size = int(status.get("downloaded_bytes") or status.get("total_bytes") or 0)
            received[format_id] = max(received.get(format_id, 0), size)
            if sum(received.values()) > max_bytes:
                too_large.set()
                raise DownloadCancelled("File size limit reached")
        on_progress(status)

    def size_error() -> AppError:
        limit_mb = (max_bytes or 0) // (1024 * 1024)
        return AppError("file_too_large", "File too large",
                        f"This download grew beyond this server's {limit_mb} MB limit and was stopped. "
                        "Choose a lower quality or MP3.", status=413)

    options = build_options(
        tools, settings, ylog,
        format=",".join(plan.format_ids),
        paths={"home": str(work_dir), "temp": str(work_dir)},
        outtmpl={"default": "stream-%(format_id)s.%(ext)s"},
        progress_hooks=[hook],
        overwrites=True,
        continuedl=False,
        writethumbnail=False,
    )
    try:
        with yt_dlp.YoutubeDL(options) as ydl:
            result = ydl.process_ie_result(prepare_for_download(info), download=True)
    except DownloadCancelled as exc:
        if too_large.is_set() and not cancel_event.is_set():
            raise size_error() from exc
        raise JobCancelled() from exc
    except Exception as exc:  # noqa: BLE001
        inner = getattr(exc, "exc_info", None)
        inner_exc = inner[1] if isinstance(inner, tuple) and len(inner) > 1 else None
        if cancel_event.is_set():
            raise JobCancelled() from exc
        if too_large.is_set():
            raise size_error() from exc
        if isinstance(inner_exc, DownloadCancelled):
            raise JobCancelled() from exc
        raise
    if cancel_event.is_set():
        raise JobCancelled()

    paths: dict[str, Path] = {}
    for entry in (result or {}).get("requested_downloads") or []:
        filepath = entry.get("filepath") or entry.get("_filename")
        if filepath:
            path = Path(filepath)
            if is_within(path, work_dir) and path.is_file():
                paths[path.stem.removeprefix("stream-")] = path
    # Fall back to the predictable file names if yt-dlp didn't report them.
    for format_id in plan.format_ids:
        if format_id not in paths:
            matches = [p for p in work_dir.glob(f"stream-{glob_escape(format_id)}.*")
                       if p.is_file() and not p.name.endswith((".part", ".ytdl", ".temp"))]
            if matches:
                paths[format_id] = matches[0]
    missing = [fid for fid in plan.format_ids if fid not in paths]
    if missing:
        raise AppError("download_interrupted", "Download incomplete",
                       "Some parts of the video couldn't be downloaded. Try again.", status=502, retryable=True,
                       detail=f"missing streams: {', '.join(missing)}")
    return paths


def glob_escape(text: str) -> str:
    return "".join(f"[{c}]" if c in "*?[]" else c for c in text)


def fetch_thumbnail(info: dict[str, Any], destination: Path, timeout: float = 15,
                    max_bytes: int = 8 * 1024 * 1024) -> Path | None:
    """Download a JPEG thumbnail for MP3 cover art (best effort)."""
    for url in thumbnail_candidates(info, jpeg_only=True)[:4]:
        if not is_safe_thumbnail_url(url):
            continue
        try:
            request = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
            with urllib.request.urlopen(request, timeout=timeout) as response:  # noqa: S310 - host allow-listed
                data = response.read(max_bytes + 1)
        except Exception as exc:  # noqa: BLE001 - optional feature
            log.debug("Thumbnail %s failed: %s", url, exc)
            continue
        if len(data) > max_bytes or not data.startswith(b"\xff\xd8\xff"):
            continue
        destination.write_bytes(data)
        return destination
    return None

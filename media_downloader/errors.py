"""User-facing errors and translation of low-level failures into them.

yt-dlp, FFmpeg and the OS report problems as free-form text. ``classify_error``
maps the common cases to stable error codes with friendly explanations. The
original message is still written to the log for diagnosis.
"""

from __future__ import annotations

import errno
import re
from typing import Any


class AppError(Exception):
    """An error that is safe and meaningful to show to the user."""

    def __init__(
        self,
        code: str,
        title: str,
        message: str,
        *,
        status: int = 400,
        detail: str | None = None,
        retryable: bool = False,
    ) -> None:
        super().__init__(message)
        self.code = code
        self.title = title
        self.message = message
        self.status = status
        self.detail = detail
        self.retryable = retryable

    def to_dict(self) -> dict[str, Any]:
        data: dict[str, Any] = {
            "code": self.code,
            "title": self.title,
            "message": self.message,
            "retryable": self.retryable,
        }
        if self.detail:
            data["detail"] = self.detail
        return data

    def __repr__(self) -> str:  # pragma: no cover - debugging aid
        return f"AppError({self.code!r}, {self.message!r})"


class JobCancelled(Exception):
    """Raised inside a job when the user cancelled it."""


# (code, title, message, retryable, patterns). Order matters: first match wins.
_RULES: list[tuple[str, str, str, bool, tuple[str, ...]]] = [
    (
        "disk_full", "Not enough disk space",
        "The disk ran out of space while saving the file. Free up some space and try again.",
        True, (r"no space left on device", r"not enough space on the disk", r"disk (?:is )?full",
               r"\[errno 28\]", r"\[winerror 112\]"),
    ),
    (
        "ffmpeg_missing", "FFmpeg is not installed",
        "FFmpeg and ffprobe are required to merge and convert media. See README.md for installation steps.",
        False, (r"ffmpeg (?:is )?not (?:found|installed)", r"ffprobe and ffmpeg not found", r"ffmpeg not found"),
    ),
    (
        "js_runtime_missing", "JavaScript runtime required",
        "YouTube now requires a JavaScript runtime (Deno) to unlock video formats. "
        "Run start.bat again to reinstall dependencies, or see README.md.",
        False, (r"no supported javascript runtime", r"javascript runtime could not be found",
                r"challenge solv\w* failed", r"signature solving failed", r"requires? a javascript runtime"),
    ),
    (
        "upcoming", "Video hasn't started yet",
        "This is a scheduled premiere or live stream that hasn't started yet.",
        False, (r"premieres in", r"will begin in", r"upcoming live", r"live event will begin"),
    ),
    (
        "live_not_supported", "Live streams aren't supported",
        "This video is a live stream that is currently broadcasting. Try again after the stream has ended.",
        False, (r"\bis live\b", r"currently live", r"live stream is not supported"),
    ),
    (
        "private_video", "Private video",
        "This video is private. Only videos that are publicly accessible can be downloaded.",
        False, (r"private video", r"video is private"),
    ),
    (
        "members_only", "Members-only video",
        "This video is only available to channel members. Signing in isn't supported by this app.",
        False, (r"members[- ]only", r"join this channel", r"available to this channel's members"),
    ),
    (
        "age_restricted", "Age-restricted video",
        "This video is age-restricted and can only be watched when signed in. Signing in isn't supported by this app.",
        False, (r"confirm your age", r"age[- ]restricted", r"inappropriate for some users"),
    ),
    (
        "bot_check", "YouTube blocked the request",
        "YouTube asked to confirm that this isn't a bot. This is usually temporary. Wait a while and try again, "
        "and keep yt-dlp up to date with update.bat.",
        True, (r"not a bot", r"confirm you.re not a bot"),
    ),
    (
        "rate_limited", "Too many requests",
        "YouTube is temporarily limiting requests from your connection. Wait a few minutes and try again.",
        True, (r"http error 429", r"too many requests"),
    ),
    (
        "geo_restricted", "Not available in your region",
        "The uploader has not made this video available in your country.",
        False, (r"available in your country", r"geo.?restrict", r"blocked it in your country"),
    ),
    (
        "copyright_removed", "Video removed",
        "This video has been removed (for example due to a copyright claim) and is no longer available.",
        False, (r"copyright", r"removed for violating", r"has been removed"),
    ),
    (
        "account_terminated", "Video unavailable",
        "This video is no longer available because the uploader's account has been terminated.",
        False, (r"account associated with this video has been terminated",),
    ),
    (
        "video_unavailable", "Video unavailable",
        "This video is unavailable. It may have been deleted, made private, or blocked.",
        False, (r"video unavailable", r"this video is (?:no longer )?(?:un)?available", r"video does not exist",
                r"incomplete youtube id", r"404: not found"),
    ),
    (
        "drm_protected", "Protected content",
        "This video is DRM-protected and can't be downloaded.",
        False, (r"\bdrm\b",),
    ),
    (
        "format_unavailable", "Quality unavailable",
        "The selected quality is no longer offered for this video. Analyze the video again and pick another quality.",
        True, (r"requested format is not available", r"no video formats found", r"format is not available"),
    ),
    (
        "forbidden", "Download blocked",
        "YouTube refused the download (HTTP 403). Analyze the video again and retry; if it keeps happening, "
        "update yt-dlp with update.bat.",
        True, (r"http error 403", r"\b403\b.*forbidden"),
    ),
    (
        "network_timeout", "Network timeout",
        "The connection to YouTube timed out. Check your internet connection and try again.",
        True, (r"timed? ?out", r"timeout"),
    ),
    (
        "network_error", "Network problem",
        "Couldn't reach YouTube. Check your internet connection and try again.",
        True, (r"getaddrinfo failed", r"name or service not known", r"nodename nor servname",
               r"failed to resolve", r"connection (?:reset|refused|aborted)", r"network is unreachable",
               r"unable to download (?:webpage|api page|json)", r"urlopen error", r"\[winerror 100\d\d\]",
               r"remote end closed connection", r"\bssl\b", r"certificate verify failed"),
    ),
    (
        "download_interrupted", "Download interrupted",
        "The download was interrupted before it finished. Try again.",
        True, (r"incomplete data", r"did not get any data blocks", r"content too short", r"fragment.*not found",
               r"giving up after \d+ (?:fragment )?retries", r"unable to download video data"),
    ),
    (
        "unsupported_url", "Unsupported link",
        "This link isn't supported. Paste a link to a single YouTube video.",
        False, (r"unsupported url",),
    ),
    (
        "extraction_failed", "Couldn't read video information",
        "yt-dlp couldn't extract this video. YouTube may have changed something; run update.bat to get the "
        "latest yt-dlp and try again.",
        True, (r"unable to extract", r"failed to extract", r"signature extraction failed",
               r"nsig extraction failed", r"please report this issue"),
    ),
]

_COMPILED_RULES = [
    (code, title, message, retryable, re.compile("|".join(patterns), re.IGNORECASE))
    for code, title, message, retryable, patterns in _RULES
]

_ANSI_RE = re.compile(r"\x1b\[[0-9;]*m")
_PREFIX_RE = re.compile(r"^(?:ERROR:\s*)?(?:\[[\w:]+\]\s*)?(?:[A-Za-z0-9_-]{11}:\s*)?")


def clean_message(text: str, limit: int = 400) -> str:
    """Strip ANSI codes and yt-dlp prefixes; collapse whitespace; truncate."""
    text = _ANSI_RE.sub("", text or "")
    text = _PREFIX_RE.sub("", text.strip())
    text = re.sub(r"\s+", " ", text).strip()
    return text if len(text) <= limit else text[: limit - 1] + "…"


def _is_disk_full(exc: BaseException) -> bool:
    while exc is not None:
        if isinstance(exc, OSError) and (
            exc.errno == errno.ENOSPC or getattr(exc, "winerror", None) in (39, 112)
        ):
            return True
        exc = exc.__cause__ or exc.__context__  # type: ignore[assignment]
    return False


def match_message(text: str) -> AppError | None:
    """Return a friendly AppError for a known failure message, else ``None``."""
    for code, title, message, retryable, pattern in _COMPILED_RULES:
        if pattern.search(text or ""):
            return AppError(code, title, message, status=422, retryable=retryable, detail=clean_message(text))
    return None


def classify_error(exc: BaseException, *, fallback_stage: str | None = None) -> AppError:
    """Convert any exception into a user-friendly :class:`AppError`."""
    if isinstance(exc, AppError):
        return exc
    if _is_disk_full(exc):
        return AppError(
            "disk_full", "Not enough disk space",
            "The disk ran out of space while saving the file. Free up some space and try again.",
            status=507, retryable=True,
        )

    text = str(exc) or exc.__class__.__name__
    # yt-dlp wraps the original exception; include its text in the search.
    inner = getattr(exc, "exc_info", None)
    if inner and len(inner) > 1 and inner[1] is not None and str(inner[1]) not in text:
        text = f"{text} {inner[1]}"

    matched = match_message(text)
    if matched:
        return matched

    if isinstance(exc, (TimeoutError,)):
        return AppError("network_timeout", "Network timeout",
                        "The connection timed out. Check your internet connection and try again.",
                        status=504, retryable=True, detail=clean_message(text))
    if isinstance(exc, (ConnectionError,)):
        return AppError("network_error", "Network problem",
                        "Couldn't reach YouTube. Check your internet connection and try again.",
                        status=502, retryable=True, detail=clean_message(text))

    what = f" while {fallback_stage}" if fallback_stage else ""
    return AppError(
        "unexpected_error", "Something went wrong",
        f"An unexpected problem occurred{what}. Details were written to logs/app.log.",
        status=500, retryable=True, detail=clean_message(text),
    )

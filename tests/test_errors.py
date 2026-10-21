"""Low-level failures become friendly, stable error codes."""

import pytest

from media_downloader.errors import AppError, classify_error, clean_message


@pytest.mark.parametrize(("message", "code"), [
    ("ERROR: [youtube] dQw4w9WgXcQ: Video unavailable", "video_unavailable"),
    ("ERROR: [youtube] dQw4w9WgXcQ: Private video. Sign in if you've been granted access", "private_video"),
    ("ERROR: [youtube] x: Sign in to confirm your age. This video may be inappropriate for some users.",
     "age_restricted"),
    ("ERROR: [youtube] x: Sign in to confirm you’re not a bot.", "bot_check"),
    ("ERROR: [youtube] x: This live event will begin in a few moments.", "upcoming"),
    ("ERROR: [youtube] x: Join this channel to get access to members-only content", "members_only"),
    ("ERROR: [youtube] x: The uploader has not made this video available in your country", "geo_restricted"),
    ("ERROR: Requested format is not available. Use --list-formats", "format_unavailable"),
    ("ERROR: unable to download video data: HTTP Error 403: Forbidden", "forbidden"),
    ("ERROR: HTTP Error 429: Too Many Requests", "rate_limited"),
    ("<urlopen error [Errno 11001] getaddrinfo failed>", "network_error"),
    ("The read operation timed out", "network_timeout"),
    ("[Errno 28] No space left on device", "disk_full"),
    ("[WinError 112] There is not enough space on the disk", "disk_full"),
    ("ERROR: Postprocessing: ffprobe and ffmpeg not found. Please install or provide the path", "ffmpeg_missing"),
    ("WARNING: [youtube] x: n challenge solving failed: Some formats may be missing.", "js_runtime_missing"),
    ("ERROR: [youtube] x: Unable to extract initial player response; please report this issue", "extraction_failed"),
])
def test_known_messages(message, code):
    error = classify_error(Exception(message))
    assert error.code == code
    assert error.message and "Traceback" not in error.message


def test_disk_full_from_oserror():
    assert classify_error(OSError(28, "No space left on device")).code == "disk_full"


def test_unknown_errors_get_generic_message():
    error = classify_error(ValueError("something odd"))
    assert error.code == "unexpected_error"
    assert error.detail == "something odd"


def test_app_errors_pass_through():
    original = AppError("x", "t", "m")
    assert classify_error(original) is original


def test_clean_message():
    assert clean_message("\x1b[0;31mERROR:\x1b[0m [youtube] dQw4w9WgXcQ: Video unavailable") == "Video unavailable"
    assert len(clean_message("x" * 1000)) <= 400

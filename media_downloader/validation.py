"""Strict validation and normalisation of user-supplied YouTube URLs.

Only an explicit allow-list of hosts and URL shapes is accepted. Whatever the
user pastes, yt-dlp only ever receives a canonical URL rebuilt from the
validated 11-character video ID, so stray query parameters (playlists, tracking
parameters, timestamps) can never change what gets downloaded.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from urllib.parse import parse_qs, urlsplit

from .errors import AppError

MAX_URL_LENGTH = 2048
VIDEO_ID_RE = re.compile(r"^[A-Za-z0-9_-]{11}$")

YOUTUBE_HOSTS = frozenset({"youtube.com", "www.youtube.com", "m.youtube.com", "music.youtube.com"})
SHORT_LINK_HOSTS = frozenset({"youtu.be", "www.youtu.be"})
NOCOOKIE_HOSTS = frozenset({"youtube-nocookie.com", "www.youtube-nocookie.com"})

# youtube.com/<prefix>/<VIDEO_ID>
_PATH_ID_PREFIXES = {"shorts": "short", "live": "live", "embed": "embed", "v": "embed"}
_CHANNEL_PREFIXES = ("channel", "c", "user")

# Something that looks like "host.tld/..." typed without a scheme.
_SCHEMELESS_RE = re.compile(r"^(?:[a-z0-9-]+\.)+[a-z]{2,}(?:[:/?#]|$)", re.IGNORECASE)
_WHITESPACE_OR_CONTROL_RE = re.compile(r"[\s\x00-\x1f\x7f]")


@dataclass(frozen=True)
class VideoRef:
    video_id: str
    kind: str  # "watch" | "short" | "live" | "embed" | "short_link"

    @property
    def canonical_url(self) -> str:
        return f"https://www.youtube.com/watch?v={self.video_id}"

    @property
    def is_short(self) -> bool:
        return self.kind == "short"


def _invalid(message: str, code: str = "invalid_url") -> AppError:
    return AppError(code, "Invalid link", message, status=400)


def _unsupported(message: str) -> AppError:
    return AppError("unsupported_url", "Unsupported link", message, status=400)


def _checked_id(candidate: str | None) -> str:
    if candidate and VIDEO_ID_RE.fullmatch(candidate):
        return candidate
    raise _invalid("The link doesn't contain a valid YouTube video ID.")


def parse_youtube_url(raw: object) -> VideoRef:
    """Validate ``raw`` and return the video it refers to.

    Raises :class:`AppError` (HTTP 400) with a user-friendly message otherwise.
    """
    if not isinstance(raw, str):
        raise _invalid("Please paste a YouTube link.")
    url = raw.strip()
    if not url:
        raise _invalid("Please paste a YouTube link.", code="empty_url")
    if len(url) > MAX_URL_LENGTH:
        raise _invalid("That link is too long to be a YouTube video link.")
    if _WHITESPACE_OR_CONTROL_RE.search(url):
        raise _invalid("Links can't contain spaces or line breaks. Paste a single YouTube link.")

    if url.startswith("//"):
        url = "https:" + url
    elif "://" not in url and _SCHEMELESS_RE.match(url):
        url = "https://" + url

    try:
        parts = urlsplit(url)
        port = parts.port  # raises ValueError for malformed ports
    except ValueError:
        raise _invalid("That doesn't look like a valid web link.") from None

    if parts.scheme.lower() not in ("http", "https"):
        raise _invalid("Only http(s) YouTube links are supported.")
    if parts.username is not None or parts.password is not None or "@" in parts.netloc:
        raise _invalid("Links containing login details are not accepted.")
    if port not in (None, 80, 443):
        raise _invalid("That doesn't look like a YouTube link.")

    host = (parts.hostname or "").rstrip(".").lower()
    segments = [s for s in parts.path.split("/") if s]
    query = parse_qs(parts.query, keep_blank_values=False)

    if host in SHORT_LINK_HOSTS:
        if len(segments) != 1:
            raise _invalid("That youtu.be link doesn't point to a video.")
        return VideoRef(_checked_id(segments[0]), "short_link")

    if host in NOCOOKIE_HOSTS:
        if len(segments) == 2 and segments[0] == "embed":
            return VideoRef(_checked_id(segments[1]), "embed")
        raise _unsupported("Only embedded video links are supported from youtube-nocookie.com.")

    if host not in YOUTUBE_HOSTS:
        raise _unsupported("Only YouTube links (youtube.com, youtu.be) are supported.")

    if not segments:
        raise _invalid("That's the YouTube homepage. Paste a link to a specific video.")

    head = segments[0].lower()
    if head == "watch" and len(segments) == 1:
        video_ids = query.get("v")
        if video_ids:
            return VideoRef(_checked_id(video_ids[0]), "watch")
        if "list" in query:
            raise _unsupported("Playlists aren't supported. Paste a link to a single video.")
        raise _invalid("That link doesn't include a video ID.")
    if head in _PATH_ID_PREFIXES and len(segments) >= 2:
        return VideoRef(_checked_id(segments[1]), _PATH_ID_PREFIXES[head])
    if head == "playlist":
        raise _unsupported("Playlists aren't supported. Paste a link to a single video.")
    if head.startswith("@") or head in _CHANNEL_PREFIXES:
        raise _unsupported("Channel pages aren't supported. Paste a link to a single video.")
    raise _unsupported("This kind of YouTube link isn't supported. Paste a link to a single video.")

"""Quality options built from the formats yt-dlp actually returned.

Nothing here is hard-coded per video: every option is derived from the format
list of the analysed video, and a user's choice is resolved back into concrete
yt-dlp format IDs. Requested qualities are never silently downgraded. If an
option no longer exists the request fails with a clear error.

Notes on YouTube's formats (see README for details):
* Above 360p, video and audio are separate streams; the chosen video stream is
  paired with the best audio stream and merged with FFmpeg.
* The same resolution is usually offered as H.264, VP9 and AV1. For an MP4
  file H.264 is the most compatible, so it is preferred when available. VP9 and
  AV1 (the only codecs above 1080p) are valid inside MP4 and are stream-copied
  unless the user asks for an H.264 conversion.
* ``info["formats"]`` is sorted by yt-dlp from worst to best; that order is
  used as the tie-breaker between otherwise equivalent streams.
"""

from __future__ import annotations

import math
import re
from dataclasses import dataclass, field
from typing import Any
from urllib.parse import urlsplit

from .errors import AppError

Format = dict[str, Any]

STANDARD_TIERS = (144, 240, 360, 480, 720, 1080, 1440, 2160, 4320)
TIER_NAMES = {720: "HD", 1080: "Full HD", 1440: "Quad HD", 2160: "4K Ultra HD", 4320: "8K"}
TIER_BADGES = {2160: "4K", 4320: "8K"}

MP3_BITRATES = (128, 192, 256, 320)
MP3_VBR_ESTIMATE_KBPS = 245  # typical average of LAME VBR V0
AAC_CONVERSION_KBPS = 192
H264_MAX_TIER = 2160

VIDEO_CODEC_NAMES = {"h264": "H.264", "hevc": "HEVC", "vp9": "VP9", "av1": "AV1", "vp8": "VP8"}
AUDIO_CODEC_NAMES = {
    "aac": "AAC", "opus": "Opus", "vorbis": "Vorbis", "mp3": "MP3", "ac3": "AC-3", "eac3": "E-AC-3",
    "flac": "FLAC", "unknown": "Unknown",
}
# Codecs that can be stream-copied into an MP4 container.
MP4_COPY_VIDEO = frozenset({"h264", "hevc", "vp9", "av1"})
MP4_COPY_AUDIO = frozenset({"aac", "mp3", "ac3", "eac3"})
# Preference when several codecs exist at one resolution: compatibility first.
MP4_VIDEO_PREFERENCE = {"h264": 4, "hevc": 3, "vp9": 2, "av1": 1}

OPTION_ID_RE = re.compile(r"^\d{2,4}p\d{0,3}(?:-hdr)?$")
MP3_QUALITY_IDS = ("best", *(str(b) for b in MP3_BITRATES))


# --------------------------------------------------------------------------- #
# Format inspection helpers
# --------------------------------------------------------------------------- #

def video_codec_family(vcodec: str | None) -> str | None:
    if not vcodec or vcodec == "none":
        return None
    v = vcodec.lower()
    if v.startswith(("avc1", "avc3", "h264")):
        return "h264"
    if v.startswith(("vp09", "vp9")):
        return "vp9"
    if v.startswith("av01"):
        return "av1"
    if v.startswith(("hev1", "hvc1", "h265", "hevc")):
        return "hevc"
    if v.startswith("vp8"):
        return "vp8"
    return v.split(".")[0]


def audio_codec_family(acodec: str | None) -> str | None:
    if acodec == "none":
        return None
    if not acodec:
        return "unknown"
    a = acodec.lower()
    if a.startswith(("mp4a", "aac")):
        return "aac"
    if a.startswith("opus"):
        return "opus"
    if a.startswith("vorbis"):
        return "vorbis"
    if a.startswith(("mp3", "mp4a.40.34", "mp4a.6b")):
        return "mp3"
    if a.startswith(("ac-3", "ac3")):
        return "ac3"
    if a.startswith(("ec-3", "eac3")):
        return "eac3"
    if a.startswith("flac"):
        return "flac"
    return a.split(".")[0]


def codec_display(family: str | None, names: dict[str, str]) -> str | None:
    if family is None:
        return None
    return names.get(family, family.upper())


def is_storyboard(f: Format) -> bool:
    return f.get("ext") == "mhtml" or f.get("protocol") == "mhtml" or f.get("format_note") == "storyboard"


def is_usable(f: Format) -> bool:
    return bool(f.get("format_id")) and not f.get("has_drm") and not is_storyboard(f)


def has_video(f: Format) -> bool:
    vcodec = f.get("vcodec")
    if vcodec == "none" or is_storyboard(f):
        return False
    return vcodec is not None or bool(f.get("height") or f.get("width"))


def has_audio(f: Format) -> bool:
    acodec = f.get("acodec")
    if acodec == "none" or is_storyboard(f):
        return False
    if acodec is not None:
        return True
    # Unknown audio codec: only treat audio-only entries (vcodec "none") as audio.
    return f.get("vcodec") == "none"


def is_drc(f: Format) -> bool:
    return "-drc" in str(f.get("format_id", "")) or "drc" in str(f.get("format_note") or "").lower()


def is_hdr(f: Format) -> bool:
    return str(f.get("dynamic_range") or "SDR").upper() != "SDR"


def round_fps(fps: Any) -> int | None:
    try:
        value = float(fps)
    except (TypeError, ValueError):
        return None
    return int(round(value)) if value > 0 else None


def quality_tier(width: Any, height: Any) -> int | None:
    """Map pixel dimensions to YouTube's quality label (the "1080" in 1080p).

    YouTube labels a stream by the 16:9 box it fits in, so 1920x800 (cinema
    widescreen) is 1080p and a 1080x1920 Short is also 1080p.
    """
    w = int(width) if isinstance(width, (int, float)) and width > 0 else None
    h = int(height) if isinstance(height, (int, float)) and height > 0 else None
    if not (w or h):
        return None
    if w and h:
        short_side, long_side = sorted((w, h))
    else:
        short_side, long_side = (h or w), None  # type: ignore[assignment]
    for tier in STANDARD_TIERS:
        fits_short = short_side <= tier * 1.05
        fits_long = long_side is None or long_side <= tier * 16 / 9 * 1.05
        if fits_short and fits_long:
            return tier
    return int(short_side)


def _number(value: Any) -> float | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)) and math.isfinite(value) and value > 0:
        return float(value)
    return None


def stream_size(f: Format | None, duration: float | None) -> tuple[int | None, bool]:
    """(bytes, is_estimate) for one stream."""
    if not f:
        return 0, False
    exact = _number(f.get("filesize"))
    if exact:
        return int(exact), False
    approx = _number(f.get("filesize_approx"))
    if approx:
        return int(approx), True
    tbr = _number(f.get("tbr")) or _number(f.get("vbr") or 0) or _number(f.get("abr") or 0)
    if tbr and duration:
        return int(tbr * 1000 / 8 * duration), True
    return None, True


def _language_preference(f: Format) -> float:
    pref = f.get("language_preference")
    return float(pref) if isinstance(pref, (int, float)) and not isinstance(pref, bool) else -1.0


def audio_candidates(formats: list[Format]) -> list[Format]:
    """Audio-only streams of the original language, best last.

    Dubbed tracks, dynamic-range-compressed (DRC) variants and streams with an
    unknown codec are only used when nothing better exists.
    """
    audio = [f for f in formats if is_usable(f) and not has_video(f) and has_audio(f)]
    if not audio:
        return []
    best_pref = max(_language_preference(f) for f in audio)
    audio = [f for f in audio if _language_preference(f) == best_pref]
    for predicate in (lambda f: not is_drc(f), lambda f: audio_codec_family(f.get("acodec")) != "unknown"):
        preferred = [f for f in audio if predicate(f)]
        if preferred:
            audio = preferred
    return audio


def best_audio(formats: list[Format]) -> Format | None:
    candidates = audio_candidates(formats)
    return candidates[-1] if candidates else None


def best_mp4_audio(formats: list[Format]) -> Format | None:
    """Best AAC stream (copied as-is into MP4), else the best audio overall."""
    candidates = audio_candidates(formats)
    aac = [f for f in candidates if audio_codec_family(f.get("acodec")) == "aac"]
    if aac:
        return aac[-1]
    return candidates[-1] if candidates else None


# --------------------------------------------------------------------------- #
# MP4 options
# --------------------------------------------------------------------------- #

@dataclass
class VideoOption:
    tier: int
    fps: int | None
    hdr: bool
    video: Format
    audio: Format | None
    progressive: bool
    alternatives: list[str] = field(default_factory=list)

    @property
    def id(self) -> str:
        return f"{self.tier}p{self.fps or ''}{'-hdr' if self.hdr else ''}"

    @property
    def label(self) -> str:
        fps = str(self.fps) if self.fps and self.fps > 30 else ""
        return f"{self.tier}p{fps}{' HDR' if self.hdr else ''}"

    @property
    def video_codec(self) -> str:
        return video_codec_family(self.video.get("vcodec")) or "unknown"

    @property
    def audio_codec(self) -> str | None:
        source = self.video if self.progressive else self.audio
        return audio_codec_family(source.get("acodec")) if source else None

    @property
    def can_convert_h264(self) -> bool:
        return not self.hdr and self.tier <= H264_MAX_TIER

    def needs_video_reencode(self, convert_h264: bool) -> bool:
        if self.video_codec not in MP4_COPY_VIDEO:
            return True
        return convert_h264 and self.video_codec != "h264"

    @property
    def needs_audio_reencode(self) -> bool:
        return self.audio_codec is not None and self.audio_codec not in MP4_COPY_AUDIO

    def processing(self, convert_h264: bool) -> str:
        if self.needs_video_reencode(convert_h264):
            return "video"
        if self.needs_audio_reencode:
            return "audio"
        return "copy"

    def estimated_size(self, duration: float | None) -> tuple[int | None, bool]:
        video_size, video_est = stream_size(self.video, duration)
        audio_size, audio_est = stream_size(None if self.progressive else self.audio, duration)
        if video_size is None or audio_size is None:
            return None, True
        return video_size + audio_size, video_est or audio_est

    def describe(self, duration: float | None) -> dict[str, Any]:
        size, approx = self.estimated_size(duration)
        return {
            "id": self.id,
            "label": self.label,
            "tier": self.tier,
            "fps": self.fps,
            "hdr": self.hdr,
            "dynamic_range": self.video.get("dynamic_range") or ("HDR" if self.hdr else "SDR"),
            "width": self.video.get("width"),
            "height": self.video.get("height"),
            "quality_name": TIER_NAMES.get(self.tier),
            "badge": TIER_BADGES.get(self.tier),
            "video_codec": codec_display(self.video_codec, VIDEO_CODEC_NAMES),
            "video_codec_key": self.video_codec,
            "audio_codec": codec_display(self.audio_codec, AUDIO_CODEC_NAMES),
            "alternatives": self.alternatives,
            "filesize": size,
            "filesize_approx": approx,
            "progressive": self.progressive,
            "processing": self.processing(False),
            "processing_h264": self.processing(True) if self.can_convert_h264 else None,
            "can_convert_h264": self.can_convert_h264,
            "has_audio": self.audio_codec is not None,
            "format_ids": [self.video["format_id"]] + ([] if self.progressive or not self.audio else [self.audio["format_id"]]),
        }


def build_video_options(info: Format) -> list[VideoOption]:
    """One option per distinct (resolution, HDR) found in the format list.

    Sorted best first. When a resolution exists at several frame rates (e.g. a
    low-fps variant), only the highest frame rate is kept.
    """
    formats: list[Format] = list(info.get("formats") or [])
    order = {id(f): index for index, f in enumerate(formats)}
    audio = best_mp4_audio(formats)

    groups: dict[tuple[int, bool], list[Format]] = {}
    for f in formats:
        if not (is_usable(f) and has_video(f)):
            continue
        tier = quality_tier(f.get("width"), f.get("height"))
        if tier is None:
            note = re.match(r"^(\d{3,4})p", str(f.get("format_note") or ""))
            tier = int(note.group(1)) if note else None
        if tier is None:
            continue
        groups.setdefault((tier, is_hdr(f)), []).append(f)

    options: list[VideoOption] = []
    for (tier, hdr), members in groups.items():
        top_fps = max(round_fps(f.get("fps")) or 0 for f in members)
        members = [f for f in members if (round_fps(f.get("fps")) or 0) == top_fps]
        video_only = [f for f in members if not has_audio(f)]
        muxed = [f for f in members if has_audio(f)]
        pool = video_only if (video_only and (audio or not muxed)) else muxed

        def rank(f: Format) -> tuple[int, int]:
            return MP4_VIDEO_PREFERENCE.get(video_codec_family(f.get("vcodec")) or "", 0), order[id(f)]

        chosen = max(pool, key=rank)
        progressive = has_audio(chosen)
        codecs = {codec_display(video_codec_family(f.get("vcodec")), VIDEO_CODEC_NAMES) for f in members}
        options.append(VideoOption(
            tier=tier,
            fps=top_fps or None,
            hdr=hdr,
            video=chosen,
            audio=None if progressive else audio,
            progressive=progressive,
            alternatives=sorted(c for c in codecs if c),
        ))

    options.sort(key=lambda o: (o.tier, o.fps or 0, not o.hdr), reverse=True)
    return options


def pick_best_option(options: list[VideoOption], convert_h264: bool = False) -> VideoOption | None:
    """Highest resolution and frame rate; SDR preferred at equal resolution."""
    eligible = [o for o in options if not convert_h264 or o.video_codec == "h264" or o.can_convert_h264]
    return eligible[0] if eligible else None


# --------------------------------------------------------------------------- #
# MP3 options
# --------------------------------------------------------------------------- #

def describe_audio_source(f: Format | None) -> dict[str, Any] | None:
    if not f:
        return None
    family = audio_codec_family(f.get("acodec"))
    return {
        "format_id": f.get("format_id"),
        "codec": codec_display(family, AUDIO_CODEC_NAMES),
        "codec_key": family,
        "bitrate": round(f["abr"]) if _number(f.get("abr")) else None,
        "sample_rate": f.get("asr"),
        "channels": f.get("audio_channels"),
        "language": f.get("language"),
        "ext": f.get("ext"),
    }


def build_mp3_options(info: Format) -> dict[str, Any]:
    formats: list[Format] = list(info.get("formats") or [])
    source = best_audio(formats)
    duration = _number(info.get("duration"))
    if source is None:
        return {"available": False, "source": None, "options": [], "default": None}
    source_kbps = _number(source.get("abr"))

    def size_for(kbps: float) -> int | None:
        return int(kbps * 1000 / 8 * duration) if duration else None

    options = [{
        "id": "best",
        "label": "Best Source Audio",
        "mode": "VBR",
        "bitrate": None,
        "approx_bitrate": MP3_VBR_ESTIMATE_KBPS,
        "detail": "Highest-quality MP3 encoding (LAME VBR V0, ~220–260 kbps)",
        "filesize": size_for(MP3_VBR_ESTIMATE_KBPS),
        "filesize_approx": True,
        "above_source": False,
        "recommended": True,
    }]
    for kbps in sorted(MP3_BITRATES, reverse=True):
        options.append({
            "id": str(kbps),
            "label": f"{kbps} kbps",
            "mode": "CBR",
            "bitrate": kbps,
            "approx_bitrate": kbps,
            "detail": f"Constant bitrate {kbps} kbps",
            "filesize": size_for(kbps),
            "filesize_approx": True,
            "above_source": bool(source_kbps and kbps > source_kbps * 1.1),
            "recommended": False,
        })
    return {"available": True, "source": describe_audio_source(source), "options": options, "default": "best"}


# --------------------------------------------------------------------------- #
# Plans
# --------------------------------------------------------------------------- #

@dataclass
class DownloadPlan:
    kind: str                     # "mp4" | "mp3"
    option_id: str                # resolved option (never "best")
    requested_id: str             # what the user asked for
    label: str                    # e.g. "1080p60 · H.264" / "MP3 · 192 kbps"
    video: Format | None
    audio: Format | None
    progressive: bool = False
    reencode_video: bool = False
    reencode_audio: bool = False
    mp3_bitrate: int | None = None  # None with kind == "mp3" means VBR V0
    estimated_size: int | None = None
    duration: float | None = None

    @property
    def format_ids(self) -> list[str]:
        ids = []
        if self.video:
            ids.append(str(self.video["format_id"]))
        if self.audio and not self.progressive:
            ids.append(str(self.audio["format_id"]))
        return ids

    def role_of(self, format_id: str | None) -> str:
        if self.video and format_id == str(self.video.get("format_id")):
            return "av" if self.progressive else "video"
        return "audio"

    @property
    def stages(self) -> list[str]:
        if self.kind == "mp3":
            return ["preparing", "downloading_audio", "converting_mp3", "finalizing"]
        steps = ["preparing", "downloading_video"]
        if self.audio and not self.progressive:
            steps.append("downloading_audio")
        if self.reencode_video:
            steps.append("converting_video")
        elif self.audio and not self.progressive:
            steps.append("merging")
        steps.append("finalizing")
        return steps

    def summary(self) -> dict[str, Any]:
        return {
            "kind": self.kind,
            "option_id": self.option_id,
            "label": self.label,
            "format_ids": self.format_ids,
            "reencode_video": self.reencode_video,
            "reencode_audio": self.reencode_audio,
            "estimated_size": self.estimated_size,
        }


def quality_unavailable(label: str) -> AppError:
    return AppError(
        "quality_unavailable", "Quality unavailable",
        f"{label} isn't available for this video. Analyze the video again and choose one of the listed qualities.",
        status=409,
    )


def validate_quality_id(kind: str, quality: str) -> None:
    ok = quality in MP3_QUALITY_IDS if kind == "mp3" else (quality == "best" or bool(OPTION_ID_RE.fullmatch(quality)))
    if not ok:
        raise AppError("invalid_quality", "Invalid quality", "The selected quality isn't valid.", status=422)


def resolve_plan(info: Format, kind: str, quality: str, convert_h264: bool = False) -> DownloadPlan:
    """Turn a user's selection into exact formats, validated against ``info``."""
    if kind not in ("mp4", "mp3"):
        raise AppError("invalid_format", "Invalid format", "Choose MP4 or MP3.", status=422)
    validate_quality_id(kind, quality)
    duration = _number(info.get("duration"))
    formats: list[Format] = list(info.get("formats") or [])

    if kind == "mp3":
        source = best_audio(formats)
        if source is None:
            raise AppError("no_audio", "No audio available",
                           "This video has no downloadable audio stream, so an MP3 can't be created.", status=422)
        bitrate = None if quality == "best" else int(quality)
        label = "Best source audio" if bitrate is None else f"{bitrate} kbps"
        kbps = bitrate or MP3_VBR_ESTIMATE_KBPS
        return DownloadPlan(
            kind="mp3", option_id=quality, requested_id=quality, label=f"MP3 · {label}",
            video=None, audio=source, mp3_bitrate=bitrate, reencode_audio=True,
            estimated_size=int(kbps * 1000 / 8 * duration) if duration else None, duration=duration,
        )

    options = build_video_options(info)
    if not options:
        raise AppError("no_formats", "No video formats",
                       "No downloadable video formats were found for this video.", status=422)
    if quality == "best":
        option = pick_best_option(options, convert_h264)
        if option is None:
            raise AppError("conversion_unavailable", "Conversion unavailable",
                           "None of this video's qualities can be converted to H.264.", status=409)
    else:
        option = next((o for o in options if o.id == quality), None)
        if option is None:
            raise quality_unavailable(_label_from_id(quality))

    if convert_h264 and option.video_codec != "h264" and not option.can_convert_h264:
        reason = "HDR video" if option.hdr else f"resolutions above {H264_MAX_TIER}p"
        raise AppError("conversion_unavailable", "Conversion unavailable",
                       f"H.264 conversion isn't offered for {reason}. Turn off the conversion option to keep the "
                       f"original {codec_display(option.video_codec, VIDEO_CODEC_NAMES)} video.", status=409)

    reencode_video = option.needs_video_reencode(convert_h264)
    codec = "H.264" if reencode_video else codec_display(option.video_codec, VIDEO_CODEC_NAMES)
    size, _ = option.estimated_size(duration)
    return DownloadPlan(
        kind="mp4",
        option_id=option.id,
        requested_id=quality,
        label=f"MP4 · {option.label} · {codec}",
        video=option.video,
        audio=option.audio,
        progressive=option.progressive,
        reencode_video=reencode_video,
        reencode_audio=option.needs_audio_reencode,
        estimated_size=None if reencode_video else size,
        duration=duration,
    )


def _label_from_id(option_id: str) -> str:
    match = re.fullmatch(r"(\d+)p(\d*)(-hdr)?", option_id)
    if not match:
        return option_id
    tier, fps, hdr = match.groups()
    fps_label = fps if fps and int(fps) > 30 else ""
    return f"{tier}p{fps_label}{' HDR' if hdr else ''}"


# --------------------------------------------------------------------------- #
# Video metadata for the preview card
# --------------------------------------------------------------------------- #

_THUMB_HOSTS = ("ytimg.com", "ggpht.com", "googleusercontent.com")


def is_safe_thumbnail_url(url: Any) -> bool:
    if not isinstance(url, str) or len(url) > 2048:
        return False
    try:
        parts = urlsplit(url)
    except ValueError:
        return False
    host = (parts.hostname or "").lower()
    return parts.scheme == "https" and any(host == h or host.endswith("." + h) for h in _THUMB_HOSTS)


def thumbnail_candidates(info: Format, jpeg_only: bool = False) -> list[str]:
    """Safe thumbnail URLs, best first."""
    urls: list[str] = []
    for thumb in reversed(info.get("thumbnails") or []):
        url = thumb.get("url")
        if not is_safe_thumbnail_url(url):
            continue
        if jpeg_only and not urlsplit(url).path.lower().endswith((".jpg", ".jpeg")):
            continue
        if url not in urls:
            urls.append(url)
    if is_safe_thumbnail_url(info.get("thumbnail")) and info["thumbnail"] not in urls and not jpeg_only:
        urls.insert(0, info["thumbnail"])
    video_id = info.get("id")
    if isinstance(video_id, str) and re.fullmatch(r"[A-Za-z0-9_-]{11}", video_id):
        for name in ("hqdefault.jpg", "mqdefault.jpg"):
            fallback = f"https://i.ytimg.com/vi/{video_id}/{name}"
            if fallback not in urls:
                urls.append(fallback)
    return urls


def _format_upload_date(value: Any) -> str | None:
    if isinstance(value, str) and re.fullmatch(r"\d{8}", value):
        return f"{value[:4]}-{value[4:6]}-{value[6:]}"
    return None


def _safe_youtube_url(value: Any) -> str | None:
    if not isinstance(value, str):
        return None
    try:
        parts = urlsplit(value)
    except ValueError:
        return None
    host = (parts.hostname or "").lower()
    if parts.scheme == "https" and host in ("www.youtube.com", "youtube.com", "m.youtube.com"):
        return value
    return None


def summarize_video(info: Format, canonical_url: str, is_short_link: bool = False) -> dict[str, Any]:
    options = build_video_options(info)
    top = options[0] if options else None
    width, height = info.get("width"), info.get("height")
    vertical = bool(width and height and height > width)
    thumbs = thumbnail_candidates(info)
    duration = _number(info.get("duration"))
    return {
        "id": info.get("id"),
        "url": canonical_url,
        "title": info.get("title") or "Untitled video",
        "uploader": info.get("uploader") or info.get("channel") or "Unknown channel",
        "channel_url": _safe_youtube_url(info.get("channel_url") or info.get("uploader_url")),
        "channel_verified": bool(info.get("channel_is_verified")),
        "duration": int(duration) if duration else None,
        "duration_string": info.get("duration_string"),
        "thumbnail": thumbs[0] if thumbs else None,
        "thumbnail_fallbacks": thumbs[1:4],
        "view_count": info.get("view_count"),
        "like_count": info.get("like_count"),
        "upload_date": _format_upload_date(info.get("upload_date")),
        "categories": list(info.get("categories") or [])[:3],
        "is_short": is_short_link or (vertical and bool(duration and duration <= 180)),
        "is_vertical": vertical,
        "width": width,
        "height": height,
        "max_quality": top.label if top else None,
        "max_tier": top.tier if top else None,
        "has_hdr": any(o.hdr for o in options),
        "chapters": len(info.get("chapters") or []),
        "live_status": info.get("live_status"),
        "resolutions": [o.label for o in options],
    }


def video_formats_summary(info: Format, convert_h264_available: bool) -> dict[str, Any]:
    options = build_video_options(info)
    duration = _number(info.get("duration"))
    best = pick_best_option(options)
    described = [o.describe(duration) for o in options]
    best_entry = None
    if best:
        size, approx = best.estimated_size(duration)
        best_entry = {
            "id": "best",
            "resolves_to": best.id,
            "label": "Best quality",
            "detail": best.label,
            "video_codec": codec_display(best.video_codec, VIDEO_CODEC_NAMES),
            "filesize": size,
            "filesize_approx": approx,
            "processing": best.processing(False),
            "processing_h264": best.processing(True) if best.can_convert_h264 else None,
            "recommended": True,
        }
    audio = best_mp4_audio(list(info.get("formats") or []))
    return {
        "available": bool(options),
        "best": best_entry,
        "options": described,
        "audio": describe_audio_source(audio),
        "h264_conversion_available": convert_h264_available and any(
            o.can_convert_h264 and o.video_codec != "h264" for o in options),
        "default": "best" if best_entry else None,
    }

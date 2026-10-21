"""FFmpeg/ffprobe invocation.

All commands are built as argument lists and run without a shell. Paths are
prefixed with ``file:`` so that a file name can never be interpreted as an
FFmpeg protocol or option. FFmpeg reports machine-readable progress on stdout
(``-progress pipe:1``), which drives the progress bar and lets a running
conversion be cancelled at any time.
"""

from __future__ import annotations

import json
import re
import subprocess
import threading
import time
from collections import deque
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable

from .environment import NO_WINDOW
from .errors import JobCancelled
from .logging_setup import get_logger

log = get_logger("ffmpeg")

ProgressCallback = Callable[[float | None, float | None, float | None], None]  # percent, speed (x), eta (s)

H264_CRF = 20
H264_PRESET = "veryfast"


class FFmpegError(Exception):
    def __init__(self, action: str, returncode: int | None, stderr_tail: str) -> None:
        super().__init__(f"FFmpeg failed while {action} (exit code {returncode}): {stderr_tail}")
        self.action = action
        self.returncode = returncode
        self.stderr_tail = stderr_tail


def ffpath(path: Path) -> str:
    """Path argument for FFmpeg that can't be mistaken for a protocol/option."""
    return f"file:{Path(path).resolve()}"


def _escape_ffmetadata(value: str) -> str:
    value = str(value).replace("\r\n", "\n").replace("\r", "\n")
    for ch in ("\\", "=", ";", "#", "\n"):
        value = value.replace(ch, "\\" + ch)
    return value


def write_ffmetadata(path: Path, tags: dict[str, Any], chapters: list[dict[str, Any]] | None = None) -> Path:
    """Write an FFmpeg metadata file with global tags and optional chapters."""
    lines = [";FFMETADATA1"]
    for key, value in tags.items():
        if value is None or value == "":
            continue
        lines.append(f"{key}={_escape_ffmetadata(str(value)[:1000])}")
    for chapter in chapters or []:
        try:
            start = int(float(chapter["start_time"]) * 1000)
            end = int(float(chapter["end_time"]) * 1000)
        except (KeyError, TypeError, ValueError):
            continue
        if end <= start:
            continue
        lines += ["[CHAPTER]", "TIMEBASE=1/1000", f"START={start}", f"END={end}"]
        if chapter.get("title"):
            lines.append(f"title={_escape_ffmetadata(str(chapter['title'])[:300])}")
    # newline="\n": on Windows "\r\n" would break backslash-escaped line breaks.
    path.write_text("\n".join(lines) + "\n", encoding="utf-8", newline="\n")
    return path


def build_mp4_command(
    ffmpeg: str,
    video: Path,
    audio: Path | None,
    output: Path,
    *,
    metadata: Path | None = None,
    reencode_video: bool = False,
    reencode_audio: bool = False,
    video_codec_family: str | None = None,
    audio_bitrate_kbps: int = 192,
) -> list[str]:
    """Merge (or remux) streams into an MP4, optionally converting codecs.

    * ``audio`` is None for a single file that already contains audio
      (or a video without any audio).
    * With no re-encoding the streams are copied bit-for-bit (fast, lossless).
    """
    cmd = [ffmpeg, "-hide_banner", "-nostdin", "-y", "-loglevel", "error", "-i", ffpath(video)]
    inputs = 1
    if audio is not None:
        cmd += ["-i", ffpath(audio)]
        inputs += 1
    meta_index = None
    if metadata is not None:
        cmd += ["-f", "ffmetadata", "-i", ffpath(metadata)]
        meta_index = inputs
        inputs += 1

    cmd += ["-map", "0:v:0"]
    cmd += ["-map", "1:a:0"] if audio is not None else ["-map", "0:a:0?"]
    if meta_index is not None:
        cmd += ["-map_metadata", str(meta_index), "-map_chapters", str(meta_index)]

    if reencode_video:
        cmd += ["-c:v", "libx264", "-preset", H264_PRESET, "-crf", str(H264_CRF), "-profile:v", "high",
                "-pix_fmt", "yuv420p"]
    else:
        cmd += ["-c:v", "copy"]
        if video_codec_family == "hevc":
            cmd += ["-tag:v", "hvc1"]  # required by Apple players

    if reencode_audio:
        cmd += ["-c:a", "aac", "-b:a", f"{audio_bitrate_kbps}k"]
    else:
        cmd += ["-c:a", "copy"]

    cmd += ["-movflags", "+faststart", "-progress", "pipe:1", "-nostats", "-stats_period", "0.5",
            "-f", "mp4", ffpath(output)]
    return cmd


def build_mp3_command(
    ffmpeg: str,
    audio: Path,
    output: Path,
    *,
    bitrate_kbps: int | None,
    cover: Path | None = None,
    metadata: Path | None = None,
    channels: int | None = None,
) -> list[str]:
    """Encode audio to MP3 with LAME: CBR ``bitrate_kbps`` or VBR V0 when None."""
    cmd = [ffmpeg, "-hide_banner", "-nostdin", "-y", "-loglevel", "error", "-i", ffpath(audio)]
    inputs = 1
    cover_index = meta_index = None
    if cover is not None:
        cmd += ["-i", ffpath(cover)]
        cover_index = inputs
        inputs += 1
    if metadata is not None:
        cmd += ["-f", "ffmetadata", "-i", ffpath(metadata)]
        meta_index = inputs
        inputs += 1

    cmd += ["-map", "0:a:0"]
    if cover_index is not None:
        cmd += [
            "-map", f"{cover_index}:v:0", "-c:v", "mjpeg", "-q:v", "2", "-disposition:v:0", "attached_pic",
            "-metadata:s:v", "title=Album cover", "-metadata:s:v", "comment=Cover (front)",
        ]
    if meta_index is not None:
        cmd += ["-map_metadata", str(meta_index)]

    cmd += ["-c:a", "libmp3lame"]
    cmd += ["-b:a", f"{int(bitrate_kbps)}k"] if bitrate_kbps else ["-q:a", "0"]
    if channels and channels > 2:
        cmd += ["-ac", "2"]
    cmd += ["-id3v2_version", "3", "-write_id3v1", "1",
            "-progress", "pipe:1", "-nostats", "-stats_period", "0.5", "-f", "mp3", ffpath(output)]
    return cmd


def _parse_speed(value: str) -> float | None:
    match = re.match(r"\s*([\d.]+)x", value or "")
    try:
        return float(match.group(1)) if match else None
    except ValueError:
        return None


def run_ffmpeg(
    cmd: list[str],
    *,
    action: str,
    duration: float | None,
    cancel_event: threading.Event,
    on_progress: ProgressCallback | None = None,
    on_stall: Callable[[], None] | None = None,
    log_path: Path | None = None,
) -> None:
    """Run FFmpeg, report progress and honour cancellation.

    ``on_stall`` is called when FFmpeg stops reporting progress for a while
    after it has started, typically while ``+faststart`` rewrites the file.
    Raises :class:`JobCancelled` if cancelled, :class:`FFmpegError` on failure.
    """
    log.debug("Running: %s", subprocess.list2cmdline(cmd))
    stderr_target = open(log_path, "w", encoding="utf-8", errors="replace") if log_path else subprocess.DEVNULL
    try:
        process = subprocess.Popen(
            cmd, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=stderr_target,
            text=True, encoding="utf-8", errors="replace", bufsize=1, creationflags=NO_WINDOW,
        )
    except OSError as exc:
        if log_path:
            stderr_target.close()  # type: ignore[union-attr]
        raise FFmpegError(action, None, f"could not start ffmpeg: {exc}") from exc

    last_progress = [0.0]  # monotonic time of the last progress report (0 = not started)
    stall_reported = [False]

    def watchdog() -> None:
        while process.poll() is None:
            if cancel_event.wait(0.25):
                if process.poll() is None:
                    process.terminate()
                return
            if (on_stall and last_progress[0] and not stall_reported[0]
                    and time.monotonic() - last_progress[0] > 2.5):
                stall_reported[0] = True
                on_stall()

    watcher = threading.Thread(target=watchdog, name="ffmpeg-cancel-watch", daemon=True)
    watcher.start()

    state: dict[str, str] = {}
    try:
        assert process.stdout is not None
        for line in process.stdout:
            key, _, value = line.strip().partition("=")
            if not key:
                continue
            state[key] = value
            if key != "progress":
                continue
            last_progress[0] = time.monotonic()
            stall_reported[0] = False
            if on_progress:
                out_us = state.get("out_time_us") or state.get("out_time_ms")
                try:
                    seconds = int(out_us) / 1_000_000 if out_us and out_us != "N/A" else None
                except ValueError:
                    seconds = None
                speed = _parse_speed(state.get("speed", ""))
                percent = eta = None
                if seconds is not None and duration:
                    percent = max(0.0, min(99.9, seconds / duration * 100))
                    if speed:
                        eta = max(0.0, (duration - seconds) / speed)
                if value == "end":
                    percent = 100.0
                on_progress(percent, speed, eta)
        returncode = process.wait()
    finally:
        if process.poll() is None:
            process.kill()
            process.wait()
        if log_path:
            stderr_target.close()  # type: ignore[union-attr]
        watcher.join(timeout=2)

    if cancel_event.is_set():
        raise JobCancelled()
    if returncode != 0:
        tail = ""
        if log_path and log_path.exists():
            tail = " | ".join(deque(log_path.read_text(encoding="utf-8", errors="replace").splitlines(), maxlen=8))
        raise FFmpegError(action, returncode, tail or "no error output")


@dataclass
class ProbeResult:
    format_name: str
    duration: float | None
    size: int | None
    streams: list[dict[str, Any]]

    def codecs(self, kind: str) -> list[str]:
        return [s.get("codec_name", "") for s in self.streams
                if s.get("codec_type") == kind and not (s.get("disposition") or {}).get("attached_pic")]

    @property
    def video_codecs(self) -> list[str]:
        return self.codecs("video")

    @property
    def audio_codecs(self) -> list[str]:
        return self.codecs("audio")


def probe(ffprobe: str, path: Path, timeout: float = 60) -> ProbeResult:
    cmd = [ffprobe, "-v", "error", "-show_entries",
           "format=format_name,duration,size:stream=codec_type,codec_name,width,height,bit_rate:stream_disposition=attached_pic",
           "-of", "json", ffpath(path)]
    try:
        result = subprocess.run(cmd, stdin=subprocess.DEVNULL, capture_output=True, text=True, encoding="utf-8",
                                errors="replace", timeout=timeout, creationflags=NO_WINDOW, check=False)
    except (OSError, subprocess.SubprocessError) as exc:
        raise FFmpegError("checking the output file", None, str(exc)) from exc
    if result.returncode != 0:
        raise FFmpegError("checking the output file", result.returncode, result.stderr.strip()[-500:])
    data = json.loads(result.stdout or "{}")
    fmt = data.get("format") or {}

    def num(value: Any) -> float | None:
        try:
            return float(value)
        except (TypeError, ValueError):
            return None

    size = num(fmt.get("size"))
    return ProbeResult(
        format_name=str(fmt.get("format_name") or ""),
        duration=num(fmt.get("duration")),
        size=int(size) if size else None,
        streams=list(data.get("streams") or []),
    )


def wait_for_file_release(path: Path, timeout: float = 5.0) -> None:
    """On Windows a just-finished process can briefly keep a file locked."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            with open(path, "rb+"):
                return
        except PermissionError:
            time.sleep(0.2)
        except FileNotFoundError:
            return

"""Real FFmpeg invocations on small generated media (no network needed)."""

from __future__ import annotations

import subprocess
import threading
import time
from pathlib import Path

import pytest

from media_downloader.errors import JobCancelled
from media_downloader.ffmpeg import (
    FFmpegError,
    build_mp3_command,
    build_mp4_command,
    probe,
    run_ffmpeg,
    write_ffmetadata,
)

pytestmark = pytest.mark.ffmpeg


def _make(ffmpeg: str, *args: str) -> None:
    result = subprocess.run([ffmpeg, "-hide_banner", "-loglevel", "error", "-y", *args],
                            capture_output=True, text=True, check=False)
    if result.returncode != 0:
        pytest.skip(f"FFmpeg build can't generate test media: {result.stderr.strip()[:200]}")


@pytest.fixture(scope="module")
def media(tmp_path_factory, ffmpeg_exe) -> dict[str, Path]:
    root = tmp_path_factory.mktemp("media")
    files = {
        "vp9": root / "video.webm",
        "h264": root / "video.mp4",
        "opus": root / "audio.webm",
        "aac": root / "audio.m4a",
        "cover": root / "cover.jpg",
    }
    _make(ffmpeg_exe, "-f", "lavfi", "-i", "testsrc2=size=320x240:rate=30", "-t", "3",
          "-c:v", "libvpx-vp9", "-b:v", "300k", "-deadline", "realtime", str(files["vp9"]))
    _make(ffmpeg_exe, "-f", "lavfi", "-i", "testsrc2=size=320x240:rate=30", "-t", "3",
          "-c:v", "libx264", "-preset", "ultrafast", str(files["h264"]))
    _make(ffmpeg_exe, "-f", "lavfi", "-i", "sine=frequency=440:duration=3", "-c:a", "libopus", "-b:a", "96k",
          str(files["opus"]))
    _make(ffmpeg_exe, "-f", "lavfi", "-i", "sine=frequency=440:duration=3", "-c:a", "aac", "-b:a", "128k",
          str(files["aac"]))
    _make(ffmpeg_exe, "-f", "lavfi", "-i", "color=c=purple:size=320x180", "-frames:v", "1", str(files["cover"]))
    return files


def _run(cmd, duration=3.0, cancel=None, progress=None):
    run_ffmpeg(cmd, action="testing", duration=duration, cancel_event=cancel or threading.Event(),
               on_progress=progress)


def test_merge_h264_and_aac_by_stream_copy(ffmpeg_exe, ffprobe_exe, media, tmp_path):
    out = tmp_path / "out.mp4"
    meta = write_ffmetadata(tmp_path / "meta.txt", {"title": "Merged", "artist": "Tester"})
    updates = []
    _run(build_mp4_command(ffmpeg_exe, media["h264"], media["aac"], out, metadata=meta),
         progress=lambda p, s, e: updates.append(p))
    result = probe(ffprobe_exe, out)
    assert "mp4" in result.format_name
    assert result.video_codecs == ["h264"] and result.audio_codecs == ["aac"]
    assert result.duration == pytest.approx(3.0, abs=0.3)
    assert updates and updates[-1] == 100.0


def test_vp9_is_remuxed_and_opus_converted_to_aac(ffmpeg_exe, ffprobe_exe, media, tmp_path):
    out = tmp_path / "out.mp4"
    _run(build_mp4_command(ffmpeg_exe, media["vp9"], media["opus"], out, reencode_audio=True))
    result = probe(ffprobe_exe, out)
    assert result.video_codecs == ["vp9"]  # copied, not re-encoded
    assert result.audio_codecs == ["aac"]


def test_vp9_converted_to_h264(ffmpeg_exe, ffprobe_exe, media, tmp_path):
    out = tmp_path / "out.mp4"
    _run(build_mp4_command(ffmpeg_exe, media["vp9"], media["opus"], out, reencode_video=True, reencode_audio=True))
    result = probe(ffprobe_exe, out)
    assert result.video_codecs == ["h264"] and result.audio_codecs == ["aac"]


def test_single_file_without_separate_audio(ffmpeg_exe, ffprobe_exe, media, tmp_path):
    out = tmp_path / "out.mp4"
    _run(build_mp4_command(ffmpeg_exe, media["h264"], None, out))
    result = probe(ffprobe_exe, out)
    assert result.video_codecs == ["h264"] and result.audio_codecs == []


@pytest.mark.parametrize("bitrate", [128, 192, 320, None])
def test_mp3_conversion(ffmpeg_exe, ffprobe_exe, media, tmp_path, bitrate):
    out = tmp_path / "out.mp3"
    _run(build_mp3_command(ffmpeg_exe, media["opus"], out, bitrate_kbps=bitrate, cover=media["cover"]))
    result = probe(ffprobe_exe, out)
    assert result.format_name == "mp3"
    assert result.audio_codecs == ["mp3"]
    attached = [s for s in result.streams if (s.get("disposition") or {}).get("attached_pic")]
    assert len(attached) == 1  # cover art embedded
    audio_stream = next(s for s in result.streams if s.get("codec_type") == "audio")
    if bitrate:
        assert int(audio_stream["bit_rate"]) == bitrate * 1000


def test_metadata_special_characters_survive(ffmpeg_exe, ffprobe_exe, media, tmp_path):
    title = 'Rock = Roll; #1 \\ "Live" 日本語 🎵\nsecond line'
    meta = write_ffmetadata(tmp_path / "meta.txt", {"title": title, "artist": "Ärtist"},
                            [{"start_time": 0, "end_time": 1.5, "title": "Intro"},
                             {"start_time": 1.5, "end_time": 3, "title": "Part 2"}])
    out = tmp_path / "out.mp4"
    _run(build_mp4_command(ffmpeg_exe, media["h264"], media["aac"], out, metadata=meta))
    tags = subprocess.run([ffprobe_exe, "-v", "error", "-show_entries", "format_tags=title,artist", "-show_chapters",
                           "-of", "json", str(out)], capture_output=True, text=True, encoding="utf-8").stdout
    import json

    data = json.loads(tags)
    assert data["format"]["tags"]["title"] == title
    assert data["format"]["tags"]["artist"] == "Ärtist"
    assert [c["tags"]["title"] for c in data["chapters"]] == ["Intro", "Part 2"]


def test_cancellation_stops_ffmpeg_quickly(ffmpeg_exe, tmp_path):
    cancel = threading.Event()
    cmd = [ffmpeg_exe, "-hide_banner", "-nostdin", "-y", "-loglevel", "error", "-f", "lavfi", "-re",
           "-i", "testsrc2=size=640x360:rate=30", "-t", "60", "-c:v", "libx264", "-preset", "ultrafast",
           "-progress", "pipe:1", "-nostats", "-f", "mp4", str(tmp_path / "long.mp4")]
    threading.Timer(0.8, cancel.set).start()
    started = time.monotonic()
    with pytest.raises(JobCancelled):
        run_ffmpeg(cmd, action="testing", duration=60, cancel_event=cancel)
    assert time.monotonic() - started < 10


def test_failure_raises_ffmpeg_error(ffmpeg_exe, tmp_path):
    missing = tmp_path / "does-not-exist.webm"
    with pytest.raises(FFmpegError) as excinfo:
        run_ffmpeg(build_mp4_command(ffmpeg_exe, missing, None, tmp_path / "x.mp4"), action="merging",
                   duration=1, cancel_event=threading.Event(), log_path=tmp_path / "log.txt")
    assert excinfo.value.returncode not in (0, None)
    assert "does-not-exist" in excinfo.value.stderr_tail or excinfo.value.stderr_tail

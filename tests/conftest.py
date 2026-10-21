"""Shared fixtures and helpers for the test-suite."""

from __future__ import annotations

import copy
import json
import os
import shutil
from pathlib import Path

import pytest

from media_downloader.config import settings
from media_downloader.environment import find_ffmpeg_dir

FIXTURES = Path(__file__).parent / "fixtures"


@pytest.fixture(scope="session")
def bbb_info_raw() -> dict:
    """Real format list of 'Big Buck Bunny 60fps 4K' (CC-BY, Blender Foundation)."""
    with open(FIXTURES / "bbb_info.json", encoding="utf-8") as fh:
        return json.load(fh)


@pytest.fixture
def bbb_info(bbb_info_raw) -> dict:
    return copy.deepcopy(bbb_info_raw)


def make_format(format_id: str, **fields) -> dict:
    """Minimal yt-dlp-style format dict."""
    base = {"format_id": format_id, "protocol": "https", "has_drm": False}
    base.update(fields)
    return base


def video(format_id: str, width: int, height: int, vcodec: str, fps: int = 30, size: int | None = 1_000_000,
          ext: str = "mp4", **extra) -> dict:
    return make_format(format_id, width=width, height=height, vcodec=vcodec, acodec="none", fps=fps,
                       filesize=size, ext=ext, dynamic_range=extra.pop("dynamic_range", "SDR"), **extra)


def audio(format_id: str, acodec: str, abr: float, size: int | None = 100_000, ext: str = "m4a", **extra) -> dict:
    return make_format(format_id, vcodec="none", acodec=acodec, abr=abr, tbr=abr, filesize=size, ext=ext,
                       asr=48000, audio_channels=2, **extra)


def info_with(*formats: dict, duration: float = 60.0, **extra) -> dict:
    data = {"id": "dQw4w9WgXcQ", "title": "Test video", "duration": duration, "formats": list(formats),
            "live_status": "not_live"}
    data.update(extra)
    return data


@pytest.fixture(scope="session")
def ffmpeg_dir() -> Path:
    directory = find_ffmpeg_dir(settings.tools_dir)
    if directory is None:
        pytest.skip("FFmpeg/ffprobe not installed")
    return directory


@pytest.fixture(scope="session")
def ffmpeg_exe(ffmpeg_dir: Path) -> str:
    return str(ffmpeg_dir / ("ffmpeg.exe" if os.name == "nt" else "ffmpeg"))


@pytest.fixture(scope="session")
def ffprobe_exe(ffmpeg_dir: Path) -> str:
    return str(ffmpeg_dir / ("ffprobe.exe" if os.name == "nt" else "ffprobe"))


@pytest.fixture
def clean_dir(tmp_path: Path) -> Path:
    path = tmp_path / "downloads"
    path.mkdir()
    yield path
    shutil.rmtree(path, ignore_errors=True)

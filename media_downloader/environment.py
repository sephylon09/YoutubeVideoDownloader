"""Discovery and verification of external tools.

* FFmpeg + ffprobe: needed to merge separate video/audio streams and to
  convert audio to MP3.
* A JavaScript runtime (Deno preferred): needed by yt-dlp-ejs to solve
  YouTube's JavaScript challenges.

Tools are searched in several places, not only the current ``PATH``. A
terminal opened before FFmpeg/Deno was installed (for example VS Code's) still
has the old ``PATH``, so the Windows registry and common install folders are
checked too.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import sys
import sysconfig
import threading
import time
import urllib.request
from dataclasses import asdict, dataclass, field
from importlib import metadata
from pathlib import Path
from typing import Iterable, Iterator

from .logging_setup import get_logger

log = get_logger("environment")

IS_WINDOWS = os.name == "nt"
EXE = ".exe" if IS_WINDOWS else ""
NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)

DENO_MIN_VERSION = (2, 3, 0)
NODE_MIN_VERSION = (22, 0, 0)


@dataclass
class ToolInfo:
    name: str
    available: bool = False
    path: str | None = None
    version: str | None = None
    note: str | None = None


@dataclass
class ToolReport:
    ffmpeg: ToolInfo
    ffprobe: ToolInfo
    js_runtime: ToolInfo
    encoders: dict[str, bool] = field(default_factory=dict)
    yt_dlp_version: str | None = None
    yt_dlp_ejs_version: str | None = None
    latest_yt_dlp_version: str | None = None
    checked_at: float = field(default_factory=time.time)

    @property
    def ffmpeg_ok(self) -> bool:
        return self.ffmpeg.available and self.ffprobe.available

    @property
    def ffmpeg_dir(self) -> str | None:
        return str(Path(self.ffmpeg.path).parent) if self.ffmpeg.path else None

    @property
    def can_encode_mp3(self) -> bool:
        return self.ffmpeg_ok and self.encoders.get("libmp3lame", False)

    @property
    def h264_encoder(self) -> str | None:
        if self.ffmpeg_ok and self.encoders.get("libx264"):
            return "libx264"
        return None

    @property
    def update_available(self) -> bool:
        if not (self.latest_yt_dlp_version and self.yt_dlp_version):
            return False
        return version_tuple(self.latest_yt_dlp_version) > version_tuple(self.yt_dlp_version)

    def js_runtimes_param(self) -> dict:
        """Value for yt-dlp's ``js_runtimes`` option."""
        if self.js_runtime.available and self.js_runtime.path:
            return {self.js_runtime.name: {"path": self.js_runtime.path}}
        return {"deno": {}}  # yt-dlp default; it will warn if nothing is found

    def problems(self) -> list[dict[str, str]]:
        items: list[dict[str, str]] = []
        if not self.ffmpeg.available:
            items.append({
                "code": "ffmpeg_missing", "severity": "error", "title": "FFmpeg not found",
                "message": "FFmpeg is required to merge video with audio and to create MP3 files. "
                           "Run start.bat to install it, or follow the steps in README.md, then restart the app.",
            })
        elif not self.ffprobe.available:
            items.append({
                "code": "ffprobe_missing", "severity": "error", "title": "ffprobe not found",
                "message": "ffprobe must be in the same folder as ffmpeg. Reinstall FFmpeg (see README.md).",
            })
        elif not self.encoders.get("libmp3lame"):
            items.append({
                "code": "mp3_encoder_missing", "severity": "warning", "title": "MP3 encoder missing",
                "message": "This FFmpeg build has no MP3 encoder (libmp3lame). Install a full FFmpeg build to enable MP3.",
            })
        if not self.js_runtime.available:
            items.append({
                "code": "js_runtime_missing", "severity": "warning", "title": "JavaScript runtime not found",
                "message": "yt-dlp needs Deno to unlock all YouTube formats. Without it some or all formats may be "
                           "missing. Run start.bat again or see README.md.",
            })
        elif self.js_runtime.name != "deno":
            items.append({
                "code": "js_runtime_fallback", "severity": "info", "title": "Using Node.js",
                "message": "Deno was not found, so Node.js is used to solve YouTube challenges. Deno is recommended.",
            })
        if self.update_available:
            items.append({
                "code": "yt_dlp_update", "severity": "info", "title": "yt-dlp update available",
                "message": f"yt-dlp {self.latest_yt_dlp_version} is available (installed: {self.yt_dlp_version}). "
                           "Run update.bat to keep YouTube downloads working reliably.",
            })
        return items

    def to_dict(self) -> dict:
        data = {
            "ffmpeg": asdict(self.ffmpeg),
            "ffprobe": asdict(self.ffprobe),
            "js_runtime": asdict(self.js_runtime),
            "encoders": dict(self.encoders),
            "yt_dlp_version": self.yt_dlp_version,
            "yt_dlp_ejs_version": self.yt_dlp_ejs_version,
            "latest_yt_dlp_version": self.latest_yt_dlp_version,
            "update_available": self.update_available,
            "can_encode_mp3": self.can_encode_mp3,
            "h264_encoder": self.h264_encoder,
            "problems": self.problems(),
        }
        return data


def version_tuple(version: str | None) -> tuple[int, ...]:
    return tuple(int(part) for part in re.findall(r"\d+", version or "")[:4])


def run_quiet(args: list[str], timeout: float = 15) -> subprocess.CompletedProcess | None:
    """Run a command without a shell; return None if it can't be executed."""
    try:
        return subprocess.run(
            args, stdin=subprocess.DEVNULL, capture_output=True, text=True, encoding="utf-8",
            errors="replace", timeout=timeout, creationflags=NO_WINDOW, check=False,
        )
    except (OSError, subprocess.SubprocessError, ValueError):
        return None


def _registry_path_dirs() -> list[Path]:
    """PATH entries stored in the registry (fresher than this process's PATH)."""
    if not IS_WINDOWS:
        return []
    try:
        import winreg
    except ImportError:  # pragma: no cover
        return []
    locations = (
        (winreg.HKEY_CURRENT_USER, r"Environment"),
        (winreg.HKEY_LOCAL_MACHINE, r"SYSTEM\CurrentControlSet\Control\Session Manager\Environment"),
    )
    dirs: list[Path] = []
    for hive, subkey in locations:
        try:
            with winreg.OpenKey(hive, subkey) as key:
                value, _ = winreg.QueryValueEx(key, "Path")
        except OSError:
            continue
        for part in str(value).split(os.pathsep):
            part = os.path.expandvars(part.strip().strip('"'))
            if part:
                dirs.append(Path(part))
    return dirs


def _env_dir(name: str) -> Path | None:
    value = os.environ.get(name)
    return Path(value) if value else None


def _winget_dirs(package_glob: str) -> list[Path]:
    local = _env_dir("LOCALAPPDATA")
    if not (IS_WINDOWS and local):
        return []
    dirs = [local / "Microsoft" / "WinGet" / "Links"]
    packages = local / "Microsoft" / "WinGet" / "Packages"
    if packages.is_dir():
        for pattern in (f"{package_glob}/*/bin", f"{package_glob}/bin", f"{package_glob}/*", package_glob):
            dirs.extend(sorted(packages.glob(pattern), reverse=True))
    return dirs


def _unique(paths: Iterable[Path | None]) -> Iterator[Path]:
    seen: set[str] = set()
    for path in paths:
        if path is None:
            continue
        key = os.path.normcase(str(path))
        if key not in seen:
            seen.add(key)
            yield path


def _which_dir(executable: str) -> Path | None:
    found = shutil.which(executable)
    return Path(found).parent if found else None


def find_ffmpeg_dir(tools_dir: Path) -> Path | None:
    """Return a directory containing both ffmpeg and ffprobe, if any."""
    env_location = os.environ.get("MD_FFMPEG_LOCATION") or os.environ.get("FFMPEG_LOCATION")
    env_dir = None
    if env_location:
        env_path = Path(env_location)
        env_dir = env_path if env_path.is_dir() else env_path.parent
    program_files = _env_dir("ProgramFiles")
    user_profile = _env_dir("USERPROFILE")
    program_data = _env_dir("ProgramData")
    candidates = _unique([
        env_dir,
        tools_dir / "ffmpeg" / "bin",
        tools_dir / "ffmpeg",
        tools_dir,
        _which_dir("ffmpeg"),
        *_registry_path_dirs(),
        *_winget_dirs("*FFmpeg*"),
        Path("C:/ffmpeg/bin") if IS_WINDOWS else None,
        program_files / "ffmpeg" / "bin" if program_files else None,
        user_profile / "scoop" / "shims" if user_profile else None,
        program_data / "chocolatey" / "bin" if program_data else None,
    ])
    for directory in candidates:
        if (directory / f"ffmpeg{EXE}").is_file() and (directory / f"ffprobe{EXE}").is_file():
            return directory
    return None


def _ffmpeg_tools(tools_dir: Path) -> tuple[ToolInfo, ToolInfo, dict[str, bool]]:
    ffmpeg = ToolInfo("ffmpeg")
    ffprobe = ToolInfo("ffprobe")
    encoders: dict[str, bool] = {}
    directory = find_ffmpeg_dir(tools_dir)
    if directory is None:
        lone = shutil.which("ffmpeg")
        if lone:
            ffmpeg.note = f"ffmpeg found at {lone} but ffprobe is missing from the same folder"
        return ffmpeg, ffprobe, encoders

    for info in (ffmpeg, ffprobe):
        exe = directory / f"{info.name}{EXE}"
        result = run_quiet([str(exe), "-hide_banner", "-version"])
        if result and result.returncode == 0:
            match = re.search(r"version\s+(\S+)", result.stdout)
            info.available = True
            info.path = str(exe)
            info.version = match.group(1) if match else "unknown"
        else:
            info.path = str(exe)
            info.note = "found but failed to run"

    if ffmpeg.available:
        result = run_quiet([ffmpeg.path, "-hide_banner", "-encoders"])  # type: ignore[list-item]
        text = result.stdout if result else ""
        for name in ("libmp3lame", "libx264", "aac"):
            encoders[name] = bool(re.search(rf"^\s*\S+\s+{re.escape(name)}\s", text, re.MULTILINE))
    return ffmpeg, ffprobe, encoders


def _deno_candidates() -> Iterator[Path]:
    env = os.environ.get("MD_DENO_PATH")
    user_profile = _env_dir("USERPROFILE") or Path.home()
    pip_deno = None
    try:  # the "deno" PyPI package ships the official binary
        import deno  # type: ignore[import-not-found]

        pip_deno = Path(deno.find_deno_bin())
    except Exception:  # noqa: BLE001 - optional package, any failure means "not available"
        pip_deno = None
    yield from _unique([
        Path(env) if env else None,
        Path(sysconfig.get_path("scripts")) / f"deno{EXE}",
        pip_deno,
        Path(shutil.which("deno")) if shutil.which("deno") else None,
        user_profile / ".deno" / "bin" / f"deno{EXE}",
        *(d / f"deno{EXE}" for d in _registry_path_dirs()),
        *(d / f"deno{EXE}" for d in _winget_dirs("DenoLand.Deno*")),
    ])


def _node_candidates() -> Iterator[Path]:
    program_files = _env_dir("ProgramFiles")
    yield from _unique([
        Path(shutil.which("node")) if shutil.which("node") else None,
        *(d / f"node{EXE}" for d in _registry_path_dirs()),
        program_files / "nodejs" / f"node{EXE}" if program_files else None,
    ])


def _probe_runtime(name: str, candidates: Iterable[Path], pattern: str, minimum: tuple[int, ...]) -> ToolInfo | None:
    too_old: ToolInfo | None = None
    for path in candidates:
        if not path.is_file():
            continue
        result = run_quiet([str(path), "--version"])
        if not result or result.returncode != 0:
            continue
        match = re.search(pattern, result.stdout, re.MULTILINE)
        version = match.group(1) if match else "unknown"
        info = ToolInfo(name, available=True, path=str(path), version=version)
        if version_tuple(version) >= minimum:
            return info
        info.available = False
        info.note = f"version {version} is too old (need {'.'.join(map(str, minimum))}+)"
        too_old = too_old or info
    return too_old


def find_js_runtime() -> ToolInfo:
    deno = _probe_runtime("deno", _deno_candidates(), r"^deno\s+(\S+)", DENO_MIN_VERSION)
    if deno and deno.available:
        return deno
    node = _probe_runtime("node", _node_candidates(), r"^v(\S+)", NODE_MIN_VERSION)
    if node and node.available:
        return node
    return deno or node or ToolInfo("deno", note="no supported JavaScript runtime found")


def _package_version(name: str) -> str | None:
    try:
        return metadata.version(name)
    except metadata.PackageNotFoundError:
        return None


def installed_yt_dlp_version() -> str | None:
    try:
        from yt_dlp.version import __version__

        return __version__
    except Exception:  # noqa: BLE001
        return _package_version("yt-dlp")


def fetch_latest_yt_dlp_version(timeout: float = 6) -> str | None:
    request = urllib.request.Request(
        "https://pypi.org/pypi/yt-dlp/json", headers={"Accept": "application/json", "User-Agent": "media-downloader"}
    )
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:  # noqa: S310 - fixed https URL
            return str(json.load(response)["info"]["version"])
    except Exception as exc:  # noqa: BLE001 - best effort only
        log.debug("Could not check PyPI for yt-dlp updates: %s", exc)
        return None


def detect_tools(tools_dir: Path) -> ToolReport:
    ffmpeg, ffprobe, encoders = _ffmpeg_tools(tools_dir)
    return ToolReport(
        ffmpeg=ffmpeg,
        ffprobe=ffprobe,
        js_runtime=find_js_runtime(),
        encoders=encoders,
        yt_dlp_version=installed_yt_dlp_version(),
        yt_dlp_ejs_version=_package_version("yt-dlp-ejs"),
    )


class ToolRegistry:
    """Caches the tool report; re-checks periodically while something is missing."""

    RECHECK_MISSING_AFTER = 30.0

    def __init__(self, tools_dir: Path) -> None:
        self._tools_dir = tools_dir
        self._lock = threading.Lock()
        self._report: ToolReport | None = None
        self._latest_version: str | None = None

    def get(self, refresh: bool = False) -> ToolReport:
        with self._lock:
            report = self._report
            stale = report is None or refresh or (
                (not report.ffmpeg_ok or not report.js_runtime.available)
                and time.time() - report.checked_at > self.RECHECK_MISSING_AFTER
            )
            if stale:
                report = detect_tools(self._tools_dir)
                report.latest_yt_dlp_version = self._latest_version
                self._report = report
                log.info(
                    "Tools: ffmpeg=%s ffprobe=%s js=%s %s yt-dlp=%s ejs=%s",
                    report.ffmpeg.version or "missing", report.ffprobe.version or "missing",
                    report.js_runtime.name, report.js_runtime.version or "missing",
                    report.yt_dlp_version, report.yt_dlp_ejs_version,
                )
            return report  # type: ignore[return-value]

    def check_for_updates_async(self) -> None:
        def worker() -> None:
            latest = fetch_latest_yt_dlp_version()
            if not latest:
                return
            with self._lock:
                self._latest_version = latest
                if self._report:
                    self._report.latest_yt_dlp_version = latest
            current = installed_yt_dlp_version()
            if version_tuple(latest) > version_tuple(current):
                log.warning("yt-dlp %s is available (installed %s). Run update.bat to update.", latest, current)

        threading.Thread(target=worker, name="yt-dlp-update-check", daemon=True).start()


def python_info() -> str:
    return f"Python {sys.version.split()[0]} ({sys.executable})"

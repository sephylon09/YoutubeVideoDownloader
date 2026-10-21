"""Environment check used by start.bat (``python -m media_downloader.doctor``).

Exit code is a bit mask so the batch file can react to each problem:
  1 = FFmpeg/ffprobe missing   2 = JavaScript runtime missing
  4 = Python packages missing  8 = MP3 encoder (libmp3lame) missing
"""

from __future__ import annotations

import argparse
import sys

EXIT_FFMPEG = 1
EXIT_JS_RUNTIME = 2
EXIT_PACKAGES = 4
EXIT_MP3 = 8


def _line(label: str, status: str, detail: str = "") -> None:
    print(f"  {label:<12} {status:<10} {detail}")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Check Media Downloader's dependencies.")
    parser.add_argument("--quiet", action="store_true", help="only set the exit code")
    args = parser.parse_args(argv)
    say = (lambda *a, **k: None) if args.quiet else _line

    missing_packages = []
    for module in ("fastapi", "uvicorn", "jinja2", "yt_dlp", "yt_dlp_ejs"):
        try:
            __import__(module)
        except ImportError:
            missing_packages.append(module)
    if missing_packages:
        if not args.quiet:
            print(f"  Missing Python packages: {', '.join(missing_packages)}")
        return EXIT_PACKAGES

    from .config import settings
    from .environment import detect_tools

    report = detect_tools(settings.tools_dir)
    code = 0
    say("Python", "OK", sys.version.split()[0])
    say("yt-dlp", "OK", f"{report.yt_dlp_version} (yt-dlp-ejs {report.yt_dlp_ejs_version or 'MISSING'})")
    if report.ffmpeg_ok:
        say("FFmpeg", "OK", f"{report.ffmpeg.version}  {report.ffmpeg.path}")
        say("ffprobe", "OK", f"{report.ffprobe.version}  {report.ffprobe.path}")
        if not report.encoders.get("libmp3lame"):
            say("MP3 encoder", "MISSING", "this FFmpeg build lacks libmp3lame")
            code |= EXIT_MP3
    else:
        say("FFmpeg", "MISSING", report.ffmpeg.note or "")
        code |= EXIT_FFMPEG
    js = report.js_runtime
    if js.available:
        say("JS runtime", "OK", f"{js.name} {js.version}  {js.path}")
    else:
        say("JS runtime", "MISSING", js.note or "Deno not found")
        code |= EXIT_JS_RUNTIME
    return code


if __name__ == "__main__":
    sys.exit(main())

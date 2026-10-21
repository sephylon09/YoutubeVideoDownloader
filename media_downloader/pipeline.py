"""The work performed for one download job (runs in a worker thread).

1. Get video information (from the short-lived analysis cache, or fresh).
2. Resolve the requested quality into exact formats. Never downgrade.
3. Check free disk space.
4. Download the raw stream(s) with yt-dlp.
5. Merge/convert with FFmpeg into a temporary file.
6. Verify the result with ffprobe.
7. Move it into ``downloads/`` under a safe, unique name, then mark the job
   completed.

Any failure or cancellation removes all partial files.
"""

from __future__ import annotations

import logging
import math
import os
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .config import Settings
from .downloader import InfoCache, check_allowed, download_streams, extract_info, fetch_thumbnail
from .environment import ToolRegistry, ToolReport
from .errors import AppError, JobCancelled, classify_error
from .ffmpeg import (
    FFmpegError,
    build_mp3_command,
    build_mp4_command,
    probe,
    run_ffmpeg,
    wait_for_file_release,
    write_ffmetadata,
)
from .formats import (
    AAC_CONVERSION_KBPS,
    DownloadPlan,
    resolve_plan,
    stream_size,
    thumbnail_candidates,
    video_codec_family,
)
from .jobs import Job
from .logging_setup import get_logger
from .storage import (
    directory_size,
    free_disk_space,
    make_work_dir,
    remove_tree,
    reserve_unique_path,
    sanitize_filename,
)
from .validation import VideoRef

log = get_logger("pipeline")
MB = 1024 * 1024


@dataclass
class PipelineContext:
    settings: Settings
    tools: ToolRegistry
    info_cache: InfoCache


class JobLog(logging.LoggerAdapter):
    def process(self, msg: str, kwargs: Any) -> tuple[str, Any]:
        return f"[job {self.extra['job']}] {msg}", kwargs


def human_size(num: float | None) -> str:
    if not num:
        return "0 B"
    units = ["B", "KB", "MB", "GB", "TB"]
    power = min(int(math.log(num, 1024)), len(units) - 1)
    return f"{num / 1024 ** power:.1f} {units[power]}"


# --------------------------------------------------------------------------- #
# Progress tracking for yt-dlp downloads
# --------------------------------------------------------------------------- #

class TransferTracker:
    """Combines per-stream yt-dlp progress into one overall figure."""

    def __init__(self, job: Job, plan: DownloadPlan) -> None:
        self.job = job
        self.plan = plan
        self.order = plan.format_ids
        self.downloaded: dict[str, int] = {fid: 0 for fid in self.order}
        self.totals: dict[str, int | None] = {}
        self.estimated: dict[str, bool] = {}
        for f in (plan.video, plan.audio):
            if f and str(f.get("format_id")) in self.downloaded:
                size, is_estimate = stream_size(f, plan.duration)
                self.totals[str(f["format_id"])] = size
                self.estimated[str(f["format_id"])] = is_estimate
        self._last_update = 0.0
        self._current: str | None = None
        self._speed: float | None = None  # smoothed, so speed/ETA don't jump around

    def __call__(self, status: dict[str, Any]) -> None:
        info = status.get("info_dict") or {}
        format_id = str(info.get("format_id") or "")
        if format_id not in self.downloaded:
            return
        if format_id != self._current:
            self._current = format_id
            role = self.plan.role_of(format_id)
            self.job.set_stage("downloading_audio" if role == "audio" else "downloading_video")

        state = status.get("status")
        raw_speed = status.get("speed")
        if state == "downloading" and raw_speed:
            self._speed = raw_speed if self._speed is None else 0.25 * raw_speed + 0.75 * self._speed
        downloaded = status.get("downloaded_bytes") or 0
        total = status.get("total_bytes")
        estimate = status.get("total_bytes_estimate")
        if total:
            self.totals[format_id] = int(total)
            self.estimated[format_id] = False
        elif estimate and self.estimated.get(format_id, True):
            # yt-dlp's running estimate (e.g. for fragmented HLS) beats a bitrate guess.
            self.totals[format_id] = int(estimate)
            self.estimated[format_id] = True
        if state == "finished":
            downloaded = downloaded or self.totals.get(format_id) or 0
            self.totals[format_id] = max(self.totals.get(format_id) or 0, downloaded)
        self.downloaded[format_id] = int(downloaded)

        now = time.monotonic()
        if state == "downloading" and now - self._last_update < 0.25:
            return
        self._last_update = now

        done = sum(self.downloaded.values())
        totals = [self.totals.get(fid) for fid in self.order]
        total_all = sum(t for t in totals if t) if all(totals) else None
        speed = self._speed if state == "downloading" else None
        percent = None
        eta = None
        if total_all:
            total_all = max(total_all, done)
            percent = min(99.9, done / total_all * 100) if state != "finished" or done < total_all else 100.0
            if speed:
                eta = max(0.0, (total_all - done) / speed)
        elif status.get("fragment_count"):
            percent = min(99.9, (status.get("fragment_index") or 0) / status["fragment_count"] * 100)
            eta = status.get("eta")
        self.job.update(
            downloaded_bytes=done,
            total_bytes=total_all,
            total_is_estimate=any(self.estimated.get(fid) for fid in self.order),
            speed=float(speed) if speed else None,
            eta=eta,
            percent=percent,
        )


# --------------------------------------------------------------------------- #
# Pipeline steps
# --------------------------------------------------------------------------- #

def _require_tools(tools: ToolReport, plan: DownloadPlan | None = None) -> None:
    if not tools.ffmpeg_ok:
        raise AppError("ffmpeg_missing", "FFmpeg is not installed",
                       "FFmpeg and ffprobe are required to create MP4 and MP3 files. Run start.bat to install "
                       "FFmpeg or follow the steps in README.md, then try again.", status=503)
    if plan is None:
        return
    if plan.kind == "mp3" and not tools.can_encode_mp3:
        raise AppError("mp3_encoder_missing", "MP3 encoder missing",
                       "The installed FFmpeg has no MP3 encoder (libmp3lame). Install a full FFmpeg build.",
                       status=503)
    if plan.reencode_video and not tools.h264_encoder:
        raise AppError("h264_encoder_missing", "H.264 encoder missing",
                       "The installed FFmpeg has no H.264 encoder (libx264). Turn off H.264 conversion or install "
                       "a full FFmpeg build.", status=503)


def _raw_size(plan: DownloadPlan) -> int | None:
    """Estimated size of the streams to download, or None if any size is unknown."""
    raw = 0
    for f in (plan.video, None if plan.progressive else plan.audio):
        if f:
            size, _ = stream_size(f, plan.duration)
            if size is None:
                return None
            raw += size
    return raw


def _too_large(size: int, limit: int) -> AppError:
    return AppError("file_too_large", "File too large",
                    f"This download would be about {human_size(size)}, more than this server's "
                    f"{human_size(limit)} limit. Choose a lower quality or MP3.", status=413)


def check_file_size(plan: DownloadPlan, settings: Settings) -> None:
    limit = settings.max_file_bytes
    raw = _raw_size(plan)
    if limit and raw is not None and raw > limit:
        raise _too_large(raw, limit)


def _check_disk_space(plan: DownloadPlan, directory: Path, settings: Settings | None = None) -> None:
    free = free_disk_space(directory)
    quota = settings.max_storage_bytes if settings else None
    if quota:
        # Everything in the downloads folder counts: other users' finished files and running jobs.
        quota_free = max(0, quota - directory_size(directory))
        if quota_free < 50 * MB:
            raise AppError("storage_full", "Server storage is full",
                           "The server's temporary storage is full. Files are deleted automatically after a while, "
                           "so try again later.", status=507, retryable=True)
        free = quota_free if free is None else min(free, quota_free)
    if free is None:
        return
    raw = _raw_size(plan)
    if raw is None:
        return  # unknown size: can't predict, rely on runtime errors
    factor = 3.5 if plan.reencode_video else 2.1  # raw streams + final file (+ larger H.264 output)
    needed = int(raw * factor) + 50 * MB
    if free < needed:
        raise AppError(
            "disk_full", "Not enough disk space",
            f"This download needs about {human_size(needed)} of free space, but only {human_size(free)} is "
            f"available on the drive containing the downloads folder.", status=507)


def _wait_until_available(plan: DownloadPlan, job: Job) -> None:
    """Honour YouTube's mandatory wait before a stream may be fetched.

    yt-dlp would sleep for this too, but in a way that can't be cancelled.
    """
    streams = [f for f in (plan.video, plan.audio) if f]
    available_at = max((float(f.get("available_at") or 0) for f in streams), default=0.0)
    deadline = available_at + 1
    while (remaining := deadline - time.time()) > 0:
        job.update(stage_detail=f"Waiting {math.ceil(remaining)}s as required by YouTube")
        if job.cancel_event.wait(min(1.0, remaining)):
            raise JobCancelled()
    job.update(stage_detail=None)


def _get_info(job: Job, ctx: PipelineContext, tools: ToolReport, jlog: JobLog, *, fresh: bool) -> tuple[dict, bool]:
    info = None if fresh else ctx.info_cache.get(job.request.video_id)
    if info is not None:
        return info, True
    job.set_stage("retrieving")
    ref = VideoRef(job.request.video_id, "watch")
    info, _ = extract_info(ref, tools, ctx.settings, logger=jlog)
    ctx.info_cache.put(job.request.video_id, info)
    return info, False


def _plan(job: Job, info: dict, tools: ToolReport, used_cache: bool) -> DownloadPlan:
    request = job.request
    plan = resolve_plan(info, request.kind, request.quality, request.convert_h264 and request.kind == "mp4")
    _require_tools(tools, plan)
    thumbnails = thumbnail_candidates(info)
    job.update(
        title=info.get("title") or job.title,
        thumbnail=job.thumbnail or (thumbnails[0] if thumbnails else None),
        label=plan.label,
        plan_summary=plan.summary(),
        steps=([] if used_cache else ["retrieving"]) + plan.stages,
    )
    return plan


def _metadata_tags(info: dict) -> dict[str, Any]:
    upload_date = str(info.get("upload_date") or "")
    return {
        "title": info.get("title"),
        "artist": info.get("uploader") or info.get("channel"),
        "date": upload_date[:4] if len(upload_date) == 8 else None,
        "comment": info.get("webpage_url"),
    }


def _ffmpeg_progress(job: Job):
    def callback(percent: float | None, speed: float | None, eta: float | None) -> None:
        detail = f"{speed:.1f}× realtime" if speed else None
        job.update(percent=percent, eta=eta, stage_detail=detail)
    return callback


def _ffmpeg_stall(job: Job):
    def callback() -> None:
        job.update(eta=None, stage_detail="Finishing the file…")
    return callback


def _process_mp4(job: Job, plan: DownloadPlan, info: dict, streams: dict[str, Path], work_dir: Path,
                 tools: ToolReport) -> Path:
    video_path = streams[str(plan.video["format_id"])]  # type: ignore[index]
    audio_path = None if plan.progressive or not plan.audio else streams[str(plan.audio["format_id"])]
    metadata = write_ffmetadata(work_dir / "metadata.txt", _metadata_tags(info), info.get("chapters"))
    output = work_dir / "output.mp4"
    cmd = build_mp4_command(
        tools.ffmpeg.path or "ffmpeg", video_path, audio_path, output,
        metadata=metadata,
        reencode_video=plan.reencode_video,
        reencode_audio=plan.reencode_audio,
        video_codec_family=video_codec_family(plan.video.get("vcodec")),  # type: ignore[union-attr]
        audio_bitrate_kbps=AAC_CONVERSION_KBPS,
    )
    if plan.reencode_video:
        stage, action = "converting_video", "converting the video to H.264"
    elif audio_path is not None:
        stage, action = "merging", "merging audio and video"
    else:
        stage, action = "finalizing", "preparing the MP4 file"
    job.set_stage(stage, percent=0.0)
    run_ffmpeg(cmd, action=action, duration=plan.duration, cancel_event=job.cancel_event,
               on_progress=_ffmpeg_progress(job), on_stall=_ffmpeg_stall(job),
               log_path=work_dir / "ffmpeg.log")
    return output


def _process_mp3(job: Job, plan: DownloadPlan, info: dict, streams: dict[str, Path], work_dir: Path,
                 tools: ToolReport, jlog: JobLog) -> Path:
    audio_path = streams[str(plan.audio["format_id"])]  # type: ignore[index]
    job.set_stage("converting_mp3", percent=0.0)
    cover = fetch_thumbnail(info, work_dir / "cover.jpg")
    if cover is None:
        jlog.info("No cover art available; creating MP3 without it")
    metadata = write_ffmetadata(work_dir / "metadata.txt", _metadata_tags(info))
    output = work_dir / "output.mp3"
    channels = plan.audio.get("audio_channels") if plan.audio else None
    cmd = build_mp3_command(tools.ffmpeg.path or "ffmpeg", audio_path, output, bitrate_kbps=plan.mp3_bitrate,
                            cover=cover, metadata=metadata, channels=channels)
    job.check_cancelled()
    run_ffmpeg(cmd, action="converting to MP3", duration=plan.duration, cancel_event=job.cancel_event,
               on_progress=_ffmpeg_progress(job), on_stall=_ffmpeg_stall(job),
               log_path=work_dir / "ffmpeg.log")
    return output


def _verify(output: Path, plan: DownloadPlan, tools: ToolReport, jlog: JobLog) -> int:
    if not output.is_file() or output.stat().st_size == 0:
        raise AppError("output_missing", "Processing failed", "FFmpeg didn't produce an output file.", status=500)
    result = probe(tools.ffprobe.path or "ffprobe", output)
    jlog.info("Output: %s, video=%s audio=%s duration=%s size=%s", result.format_name, result.video_codecs,
              result.audio_codecs, result.duration, human_size(output.stat().st_size))
    if plan.kind == "mp3":
        if "mp3" not in result.format_name or result.audio_codecs != ["mp3"]:
            raise AppError("conversion_failed", "MP3 conversion failed",
                           "The converted file isn't a valid MP3. Try again.", status=500)
    else:
        if "mp4" not in result.format_name or not result.video_codecs:
            raise AppError("merge_failed", "Processing failed",
                           "The final file isn't a valid MP4 video. Try again.", status=500)
        expects_audio = bool(plan.audio) or (plan.progressive and plan.video and plan.video.get("acodec") != "none")
        if expects_audio and not result.audio_codecs:
            raise AppError("merge_failed", "Merging failed",
                           "Audio and video couldn't be merged: the final file has no audio track. Try again.",
                           status=500)
        if plan.reencode_video and result.video_codecs[0] != "h264":
            raise AppError("conversion_failed", "Conversion failed",
                           "The video couldn't be converted to H.264.", status=500)
    if plan.duration and result.duration:
        ratio = result.duration / plan.duration
        if ratio < 0.5:
            raise AppError("download_interrupted", "Download incomplete",
                           "The finished file is much shorter than the video. The download was probably "
                           "interrupted. Try again.", status=502, retryable=True)
        if ratio < 0.95:
            jlog.warning("Output duration %.1fs differs from expected %.1fs", result.duration, plan.duration)
    return output.stat().st_size


def _publish(output: Path, info: dict, plan: DownloadPlan, settings: Settings) -> Path:
    """Move the verified file into the downloads folder under a unique name."""
    video_id = info.get("id") or "video"
    stem = sanitize_filename(info.get("title"), fallback=f"youtube-{video_id}")
    destination = reserve_unique_path(settings.download_dir, stem, plan.kind)
    wait_for_file_release(output)
    try:
        os.replace(output, destination)
    except OSError:
        remove_tree(destination, settings.download_dir)
        raise
    return destination


def run_job(job: Job, ctx: PipelineContext) -> None:
    """Entry point executed by the job manager."""
    jlog = JobLog(log, {"job": job.id[:8]})
    settings = ctx.settings
    tools = ctx.tools.get()
    _require_tools(tools)
    work_dir = make_work_dir(settings.work_dir, job.id)
    try:
        job.set_stage("preparing")
        info, used_cache = _get_info(job, ctx, tools, jlog, fresh=False)
        check_allowed(info, settings)
        job.check_cancelled()
        plan = _plan(job, info, tools, used_cache)
        job.set_stage("preparing")
        jlog.info("Plan: %s formats=%s reencode_video=%s reencode_audio=%s", plan.label, plan.format_ids,
                  plan.reencode_video, plan.reencode_audio)
        check_file_size(plan, settings)
        _check_disk_space(plan, settings.download_dir, settings)

        try:
            _wait_until_available(plan, job)
            streams = download_streams(info, plan, work_dir, tools=tools, settings=settings,
                                       cancel_event=job.cancel_event, on_progress=TransferTracker(job, plan),
                                       logger=jlog, max_bytes=settings.max_file_bytes)
        except JobCancelled:
            raise
        except Exception as exc:  # noqa: BLE001
            error = classify_error(exc)
            if not used_cache or error.code in ("disk_full", "ffmpeg_missing", "file_too_large"):
                raise error from exc
            # Cached format URLs can be rejected (e.g. HTTP 403). Retry once with fresh information.
            jlog.warning("Download with cached info failed (%s: %s); retrying with fresh info", error.code, exc)
            ctx.info_cache.evict(job.request.video_id)
            for leftover in work_dir.iterdir():
                remove_tree(leftover, work_dir)
            info, _ = _get_info(job, ctx, tools, jlog, fresh=True)
            plan = _plan(job, info, tools, used_cache=False)
            check_file_size(plan, settings)
            _wait_until_available(plan, job)
            streams = download_streams(info, plan, work_dir, tools=tools, settings=settings,
                                       cancel_event=job.cancel_event, on_progress=TransferTracker(job, plan),
                                       logger=jlog, max_bytes=settings.max_file_bytes)

        job.check_cancelled()
        jlog.info("Downloaded: %s", {k: human_size(v.stat().st_size) for k, v in streams.items()})
        try:
            if plan.kind == "mp3":
                output = _process_mp3(job, plan, info, streams, work_dir, tools, jlog)
            else:
                output = _process_mp4(job, plan, info, streams, work_dir, tools)
        except FFmpegError as exc:
            jlog.error("%s", exc)
            if "space" in exc.stderr_tail.lower():
                raise classify_error(OSError(28, exc.stderr_tail)) from exc
            raise _ffmpeg_failure(plan) from exc

        job.set_stage("finalizing")
        job.check_cancelled()
        size = _verify(output, plan, tools, jlog)
        if settings.max_file_bytes and size > settings.max_file_bytes:
            raise _too_large(size, settings.max_file_bytes)  # e.g. a long MP3 at 320 kbps
        destination = _publish(output, info, plan, settings)
        job.complete(destination, size)
        jlog.info("Completed: %s (%s)", destination.name, human_size(size))
    except JobCancelled:
        raise
    except AppError:
        raise
    except Exception as exc:  # noqa: BLE001
        jlog.debug("Unexpected failure", exc_info=True)
        raise classify_error(exc, fallback_stage=job.stage.replace("_", " ")) from exc
    finally:
        remove_tree(work_dir, settings.work_dir)


def _ffmpeg_failure(plan: DownloadPlan) -> AppError:
    if plan.kind == "mp3":
        return AppError("conversion_failed", "MP3 conversion failed",
                        "FFmpeg couldn't convert the audio to MP3. Details were written to logs/app.log.", status=500)
    if plan.reencode_video:
        return AppError("conversion_failed", "Conversion failed",
                        "FFmpeg couldn't convert the video to H.264. Details were written to logs/app.log.",
                        status=500)
    return AppError("merge_failed", "Merging failed",
                    "FFmpeg couldn't merge the video and audio into an MP4. Details were written to logs/app.log.",
                    status=500)

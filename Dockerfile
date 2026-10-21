# Media Downloader - production image (used by Render; runs anywhere Docker runs).
#
#   docker build -t media-downloader .
#   docker run --rm -p 8000:8000 -e MD_PASSWORD="choose-a-long-password" media-downloader
#
# The app starts in public mode: it listens on 0.0.0.0:$PORT and requires MD_PASSWORD.
FROM python:3.13-slim-trixie

# FFmpeg (includes ffprobe, libmp3lame and libx264) and tini, a tiny init that forwards stop signals
# and reaps child processes (ffmpeg, deno) because Python runs as PID 1 otherwise.
RUN apt-get update \
    && apt-get install -y --no-install-recommends ffmpeg tini ca-certificates \
    && rm -rf /var/lib/apt/lists/*

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

WORKDIR /app

# yt-dlp[default] (includes the matching yt-dlp-ejs), FastAPI, uvicorn and the official Deno binary
# (PyPI package "deno", installed as /usr/local/bin/deno). Rebuilding the image picks up new yt-dlp releases.
COPY requirements.txt .
RUN pip install -r requirements.txt

COPY app.py ./
COPY media_downloader ./media_downloader
COPY templates ./templates
COPY static ./static

# Run as an unprivileged user. /data holds temporary files only: Render's filesystem is wiped on
# every deploy and restart, so nothing in it is permanent.
RUN useradd --system --uid 10001 --create-home --home-dir /home/app app \
    && mkdir -p /data \
    && chown app:app /data

# Hosted defaults, sized for a small instance (0.5 CPU / 512 MB). Override any of them with
# environment variables in the Render dashboard; see README.md for the full list.
ENV MD_HOST=0.0.0.0 \
    PORT=8000 \
    MD_DOWNLOAD_DIR=/data/downloads \
    MD_LOG_DIR=/data/logs \
    MD_CACHE_DIR=/data/cache \
    DENO_DIR=/data/cache/deno \
    MD_RETENTION_HOURS=1 \
    MD_MAX_CONCURRENT_DOWNLOADS=1 \
    MD_MAX_CONCURRENT_ANALYSES=2 \
    MD_MAX_PENDING_JOBS=6 \
    MD_MAX_FILE_MB=1024 \
    MD_MAX_STORAGE_MB=3072 \
    MD_MAX_DURATION_MINUTES=180 \
    MD_JOB_TIMEOUT_MINUTES=60 \
    MD_ALLOW_H264=0

USER app

# Fail the build if FFmpeg/ffprobe, the MP3 encoder, Deno, yt-dlp or yt-dlp-ejs are missing.
RUN python -m media_downloader.doctor

EXPOSE 8000

HEALTHCHECK --interval=30s --timeout=5s --start-period=30s --retries=3 \
    CMD python -c "import os, urllib.request; urllib.request.urlopen('http://127.0.0.1:%s/healthz' % (os.environ.get('MD_PORT') or os.environ.get('PORT') or '8000'), timeout=4)" || exit 1

ENTRYPOINT ["/usr/bin/tini", "--"]
CMD ["python", "app.py"]

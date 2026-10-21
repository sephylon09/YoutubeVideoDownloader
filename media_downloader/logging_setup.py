"""Logging: concise console output plus a detailed rotating log file."""

from __future__ import annotations

import logging
import logging.handlers
import sys
from pathlib import Path

LOGGER_NAME = "media_downloader"
_configured = False


def setup_logging(log_dir: Path, *, console_level: int = logging.INFO) -> Path:
    """Configure application logging once. Returns the log file path."""
    global _configured
    log_dir.mkdir(parents=True, exist_ok=True)
    log_file = log_dir / "app.log"
    if _configured:
        return log_file

    # Video titles can contain emoji; never let a console encoding crash logging.
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(errors="replace")  # type: ignore[union-attr]
        except (AttributeError, ValueError):
            pass

    root = logging.getLogger(LOGGER_NAME)
    root.setLevel(logging.DEBUG)
    root.propagate = False

    file_handler = logging.handlers.RotatingFileHandler(
        log_file, maxBytes=5 * 1024 * 1024, backupCount=5, encoding="utf-8", delay=True
    )
    file_handler.setLevel(logging.DEBUG)
    file_handler.setFormatter(
        logging.Formatter("%(asctime)s %(levelname)-7s %(name)s: %(message)s")
    )

    console_handler = logging.StreamHandler(sys.stdout)
    console_handler.setLevel(console_level)
    console_handler.setFormatter(logging.Formatter("%(asctime)s  %(levelname)-7s %(message)s", "%H:%M:%S"))

    root.addHandler(file_handler)
    root.addHandler(console_handler)

    # Route uvicorn's error log (startup messages, crashes) into the same handlers.
    for name in ("uvicorn", "uvicorn.error"):
        uv_logger = logging.getLogger(name)
        uv_logger.handlers = [file_handler, console_handler]
        uv_logger.setLevel(logging.INFO)
        uv_logger.propagate = False
    logging.getLogger("uvicorn.access").disabled = True

    _configured = True
    return log_file


def get_logger(name: str | None = None) -> logging.Logger:
    return logging.getLogger(f"{LOGGER_NAME}.{name}" if name else LOGGER_NAME)

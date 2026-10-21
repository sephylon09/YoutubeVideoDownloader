"""Application settings.

Every value has a sensible default and can be overridden with an environment
variable (prefixed ``MD_``) without touching the code.

Two modes follow from ``MD_HOST``:

* **local** (the default, 127.0.0.1): the Windows workflow with ``start.bat``.
  No password, no limits beyond the concurrency settings.
* **public** (any other interface, e.g. ``0.0.0.0`` in the Docker image): the
  hosting platform's ``PORT`` is honoured and a password (``MD_PASSWORD``) is
  mandatory. The resource limits below are meant for this mode; the
  Dockerfile sets conservative values for them.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent.parent

LOOPBACK_HOSTS = ("127.0.0.1", "localhost", "::1")
MIN_PASSWORD_LENGTH = 10


def _env_str(name: str, default: str) -> str:
    value = os.environ.get(name, "").strip()
    return value or default


def _env_bool(name: str, default: bool) -> bool:
    value = os.environ.get(name, "").strip().lower()
    if value in ("1", "true", "yes", "on"):
        return True
    if value in ("0", "false", "no", "off"):
        return False
    return default


def _env_list(name: str) -> tuple[str, ...]:
    return tuple(item.strip() for item in os.environ.get(name, "").split(",") if item.strip())


def _env_int(name: str, default: int, minimum: int, maximum: int) -> int:
    try:
        value = int(os.environ.get(name, default))
    except (TypeError, ValueError):
        return default
    return max(minimum, min(maximum, value))


def _env_float(name: str, default: float, minimum: float, maximum: float) -> float:
    try:
        value = float(os.environ.get(name, default))
    except (TypeError, ValueError):
        return default
    return max(minimum, min(maximum, value))


def _env_path(name: str, default: Path) -> Path:
    value = os.environ.get(name, "").strip()
    return Path(value).expanduser().resolve() if value else default


@dataclass(frozen=True)
class Settings:
    host: str
    port: int
    download_dir: Path
    log_dir: Path
    cache_dir: Path
    tools_dir: Path
    max_concurrent_downloads: int
    max_concurrent_analyses: int
    file_retention_hours: float
    info_cache_ttl_seconds: int
    socket_timeout_seconds: int
    templates_dir: Path = field(default=BASE_DIR / "templates")
    static_dir: Path = field(default=BASE_DIR / "static")
    # -- access control (public mode) --
    password: str | None = field(default=None, repr=False)
    secret_key: str | None = field(default=None, repr=False)
    session_hours: float = 12.0
    extra_allowed_hosts: tuple[str, ...] = ()
    trust_proxy: bool = False
    rate_limits: bool = False
    analyses_per_hour: int = 60
    downloads_per_hour: int = 20
    # -- resource limits (0 = unlimited) --
    max_file_mb: int = 0
    max_storage_mb: int = 0
    max_duration_minutes: int = 0
    max_pending_jobs: int = 20
    job_timeout_minutes: int = 0
    allow_h264_conversion: bool = True
    allowed_channels: tuple[str, ...] = ()
    ytdlp_proxy: str | None = field(default=None, repr=False)

    @property
    def work_dir(self) -> Path:
        """Scratch space for in-progress jobs (raw streams, partial files)."""
        return self.download_dir / ".work"

    @property
    def allowed_hosts(self) -> list[str]:
        hosts = set(LOOPBACK_HOSTS) | {"[::1]"} | set(self.extra_allowed_hosts)
        if self.host not in ("0.0.0.0", "::"):
            hosts.add(self.host)
        return sorted(hosts)

    @property
    def is_loopback_only(self) -> bool:
        return self.host in LOOPBACK_HOSTS

    @property
    def is_public(self) -> bool:
        """Reachable from other machines: requires a password and shows hosted wording."""
        return not self.is_loopback_only

    @property
    def auth_enabled(self) -> bool:
        return bool(self.password)

    @property
    def max_file_bytes(self) -> int | None:
        return self.max_file_mb * 1024 * 1024 if self.max_file_mb else None

    @property
    def max_storage_bytes(self) -> int | None:
        return self.max_storage_mb * 1024 * 1024 if self.max_storage_mb else None

    @property
    def max_duration_seconds(self) -> int | None:
        return self.max_duration_minutes * 60 if self.max_duration_minutes else None

    def startup_problem(self, host: str | None = None) -> str | None:
        """Why the server must not start with this configuration, if anything."""
        host = host or self.host
        if host in LOOPBACK_HOSTS:
            return None
        if not self.password:
            return (f"Refusing to listen on {host} without a password. Set MD_PASSWORD (at least "
                    f"{MIN_PASSWORD_LENGTH} characters) so the app isn't an open public download service.")
        if len(self.password) < MIN_PASSWORD_LENGTH:
            return f"MD_PASSWORD is too short. Use at least {MIN_PASSWORD_LENGTH} characters."
        return None


def load_settings() -> Settings:
    host = _env_str("MD_HOST", "127.0.0.1")
    public = host not in LOOPBACK_HOSTS
    # Hosting platforms such as Render tell the app which port to use via PORT. It is ignored in
    # local mode so an unrelated global PORT variable can't move the local app away from 8000.
    default_port = _env_int("PORT", 8000, 1, 65535) if public else 8000
    on_render = bool(os.environ.get("RENDER"))
    render_host = os.environ.get("RENDER_EXTERNAL_HOSTNAME", "").strip()
    return Settings(
        host=host,
        port=_env_int("MD_PORT", default_port, 1, 65535),
        download_dir=_env_path("MD_DOWNLOAD_DIR", BASE_DIR / "downloads"),
        log_dir=_env_path("MD_LOG_DIR", BASE_DIR / "logs"),
        cache_dir=_env_path("MD_CACHE_DIR", BASE_DIR / ".cache"),
        tools_dir=_env_path("MD_TOOLS_DIR", BASE_DIR / "tools"),
        max_concurrent_downloads=_env_int("MD_MAX_CONCURRENT_DOWNLOADS", 2, 1, 8),
        max_concurrent_analyses=_env_int("MD_MAX_CONCURRENT_ANALYSES", 4, 1, 16),
        file_retention_hours=_env_float("MD_RETENTION_HOURS", 24.0, 0.25, 24.0 * 30),
        info_cache_ttl_seconds=_env_int("MD_INFO_CACHE_TTL", 15 * 60, 0, 6 * 3600),
        socket_timeout_seconds=_env_int("MD_SOCKET_TIMEOUT", 30, 5, 300),
        password=os.environ.get("MD_PASSWORD") or None,
        secret_key=os.environ.get("MD_SECRET_KEY") or None,
        session_hours=_env_float("MD_SESSION_HOURS", 12.0, 0.25, 24.0 * 30),
        extra_allowed_hosts=_env_list("MD_ALLOWED_HOSTS") + ((render_host,) if render_host else ()),
        trust_proxy=_env_bool("MD_TRUST_PROXY", on_render),
        rate_limits=_env_bool("MD_RATE_LIMITS", public),
        analyses_per_hour=_env_int("MD_ANALYSES_PER_HOUR", 60, 1, 100_000),
        downloads_per_hour=_env_int("MD_DOWNLOADS_PER_HOUR", 20, 1, 100_000),
        max_file_mb=_env_int("MD_MAX_FILE_MB", 0, 0, 1024 * 1024),
        max_storage_mb=_env_int("MD_MAX_STORAGE_MB", 0, 0, 1024 * 1024 * 10),
        max_duration_minutes=_env_int("MD_MAX_DURATION_MINUTES", 0, 0, 60 * 24 * 7),
        max_pending_jobs=_env_int("MD_MAX_PENDING_JOBS", 20, 1, 1000),
        job_timeout_minutes=_env_int("MD_JOB_TIMEOUT_MINUTES", 0, 0, 60 * 24),
        allow_h264_conversion=_env_bool("MD_ALLOW_H264", True),
        allowed_channels=_env_list("MD_ALLOWED_CHANNELS"),
        ytdlp_proxy=os.environ.get("MD_PROXY") or None,
    )


settings = load_settings()

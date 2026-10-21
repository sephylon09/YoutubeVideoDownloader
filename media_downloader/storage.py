"""Safe filenames, output paths and cleanup of old files.

Security rules enforced here:
* Final filenames are derived from the video title but sanitised so they can
  never contain path separators, reserved Windows names or control characters.
* Every path that is created, served or deleted is checked to be inside the
  application's own download directory.
* Job IDs are opaque random hex strings validated by a strict regex.
"""

from __future__ import annotations

import os
import re
import shutil
import threading
import time
import unicodedata
import uuid
from pathlib import Path

from .logging_setup import get_logger

log = get_logger("storage")

JOB_ID_RE = re.compile(r"^[0-9a-f]{32}$")

# Characters Windows forbids in filenames, mapped to readable substitutes.
_REPLACEMENTS = {
    '"': "'",
    "/": "-",
    "\\": "-",
    "|": "-",
    "<": "",
    ">": "",
    "?": "",
    "*": "",
}
# Bidirectional overrides can disguise a file's real extension ("evil‮mp4.exe").
_BIDI_CONTROLS = {chr(c) for c in (0x200E, 0x200F, *range(0x202A, 0x202F), *range(0x2066, 0x206A))}
_KEEP_FORMAT_CHARS = {"‍"}  # zero-width joiner, used inside emoji sequences
_RESERVED_NAMES = {
    "CON", "PRN", "AUX", "NUL", "CONIN$", "CONOUT$",
    *(f"COM{i}" for i in range(1, 10)), *(f"LPT{i}" for i in range(1, 10)),
    "COM¹", "COM²", "COM³", "LPT¹", "LPT²", "LPT³",
}
MAX_STEM_LENGTH = 150
# Linux/macOS limit a file name to 255 *bytes*; 150 CJK characters are 450 bytes in UTF-8.
# Windows counts UTF-16 characters instead, so the byte limit only applies elsewhere.
MAX_NAME_BYTES = None if os.name == "nt" else 255
_name_lock = threading.Lock()


def new_job_id() -> str:
    return uuid.uuid4().hex


def is_valid_job_id(job_id: object) -> bool:
    return isinstance(job_id, str) and bool(JOB_ID_RE.fullmatch(job_id))


def sanitize_filename(title: str | None, fallback: str = "download", max_length: int = MAX_STEM_LENGTH) -> str:
    """Turn a video title into a safe filename stem (no extension).

    Unicode letters, digits and emoji are preserved; only characters that are
    illegal or dangerous in Windows filenames are replaced or removed.
    """
    text = unicodedata.normalize("NFC", title or "")
    text = text.replace(": ", " - ").replace(":", "-")
    out: list[str] = []
    for ch in text:
        if ch in _REPLACEMENTS:
            out.append(_REPLACEMENTS[ch])
        elif ch in _BIDI_CONTROLS:
            continue
        elif ch in ("\t", "\n", "\r"):
            out.append(" ")
        else:
            category = unicodedata.category(ch)
            if category == "Cc" or (category == "Cf" and ch not in _KEEP_FORMAT_CHARS):
                continue
            if category in ("Zl", "Zp", "Zs"):
                out.append(" ")
            else:
                out.append(ch)
    text = re.sub(r"\s+", " ", "".join(out))
    text = re.sub(r"-{3,}", "--", text)
    text = text.strip(" .-_")  # Windows silently drops trailing dots/spaces

    if len(text) > max_length:
        text = text[:max_length].rstrip(" .-_")
        # Avoid leaving a dangling combining mark or half of a ZWJ sequence.
        while text and (unicodedata.combining(text[-1]) or text[-1] in _KEEP_FORMAT_CHARS):
            text = text[:-1]
        text = text.rstrip(" .-_")

    if not text:
        text = "download" if fallback == title else sanitize_filename(fallback, "download", max_length)
    if text.split(".")[0].strip().upper() in _RESERVED_NAMES:
        text = f"_{text}"
    return text


def fit_name_bytes(stem: str, reserved: int, max_bytes: int | None) -> str:
    """Shorten ``stem`` so ``stem`` plus ``reserved`` bytes fits in ``max_bytes`` of UTF-8."""
    if max_bytes is None or len(stem.encode("utf-8")) + reserved <= max_bytes:
        return stem
    budget = max(1, max_bytes - reserved)
    text = stem.encode("utf-8")[:budget].decode("utf-8", "ignore")  # never split a character
    while text and (unicodedata.combining(text[-1]) or text[-1] in _KEEP_FORMAT_CHARS):
        text = text[:-1]
    return text.rstrip(" .-_") or "download"


def is_within(path: Path, directory: Path) -> bool:
    """True if ``path`` resolves to a location inside ``directory``."""
    try:
        resolved = Path(path).resolve()
        root = Path(directory).resolve()
    except OSError:
        return False
    return root in resolved.parents


def reserve_unique_path(directory: Path, stem: str, extension: str) -> Path:
    """Atomically create an empty placeholder file with a unique name.

    ``Title.mp4``, then ``Title (2).mp4``, ``Title (3).mp4``... The caller
    replaces the placeholder with the finished file via ``os.replace``.
    """
    extension = extension.lstrip(".").lower()
    if not re.fullmatch(r"[a-z0-9]{1,5}", extension):
        raise ValueError(f"Unexpected extension {extension!r}")
    directory.mkdir(parents=True, exist_ok=True)
    # Keep the full path comfortably under the classic 260-character MAX_PATH.
    budget = max(40, 245 - len(str(directory.resolve())) - len(extension) - 6)
    stem = sanitize_filename(stem, max_length=min(MAX_STEM_LENGTH, budget))
    stem = fit_name_bytes(stem, reserved=len(f" (999).{extension}"), max_bytes=MAX_NAME_BYTES)

    with _name_lock:
        for index in range(1, 1000):
            name = f"{stem}.{extension}" if index == 1 else f"{stem} ({index}).{extension}"
            candidate = directory / name
            if not is_within(candidate, directory):  # defence in depth
                raise ValueError("Refusing to write outside the download directory")
            try:
                fd = os.open(candidate, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
            except FileExistsError:
                continue
            os.close(fd)
            return candidate
    raise RuntimeError("Could not find a free filename")


def make_work_dir(work_root: Path, job_id: str) -> Path:
    if not is_valid_job_id(job_id):
        raise ValueError("Invalid job id")
    path = work_root / job_id
    if not is_within(path, work_root):
        raise ValueError("Invalid work directory")
    path.mkdir(parents=True, exist_ok=True)
    return path


def remove_tree(path: Path, root: Path, attempts: int = 5) -> bool:
    """Delete ``path`` (a directory or file) if it lives inside ``root``.

    Retries briefly because Windows may hold a lock for a moment after a
    subprocess exits.
    """
    if not path.exists():
        return True
    if not is_within(path, root):
        log.error("Refusing to delete %s: outside %s", path, root)
        return False
    for attempt in range(attempts):
        try:
            if path.is_dir():
                shutil.rmtree(path)
            else:
                path.unlink()
            return True
        except FileNotFoundError:
            return True
        except OSError as exc:
            if attempt == attempts - 1:
                log.warning("Could not delete %s: %s", path, exc)
                return False
            time.sleep(0.4 * (attempt + 1))
    return False


MANAGED_EXTENSIONS = {".mp4", ".mp3"}


def cleanup_old_files(download_dir: Path, max_age_seconds: float, protected: set[Path] | None = None) -> int:
    """Delete finished files older than ``max_age_seconds`` and stale work dirs.

    Only files the app itself creates (``.mp4``/``.mp3`` in the download
    directory) and job folders in ``downloads/.work`` are touched.
    """
    protected = {p.resolve() for p in (protected or set())}
    now = time.time()
    removed = 0
    if not download_dir.is_dir():
        return 0

    for entry in download_dir.iterdir():
        try:
            if not entry.is_file() or entry.suffix.lower() not in MANAGED_EXTENSIONS:
                continue
            if entry.resolve() in protected:
                continue
            if now - entry.stat().st_mtime > max_age_seconds and remove_tree(entry, download_dir):
                removed += 1
                log.info("Cleanup: removed old file %s", entry.name)
        except OSError as exc:
            log.warning("Cleanup: could not inspect %s: %s", entry, exc)

    work_root = download_dir / ".work"
    if work_root.is_dir():
        for entry in work_root.iterdir():
            try:
                if entry.resolve() in protected:
                    continue
                if now - entry.stat().st_mtime > max_age_seconds and remove_tree(entry, work_root):
                    removed += 1
            except OSError:
                continue
    return removed


def clear_work_dir(work_root: Path, keep: set[str] | None = None) -> None:
    """Remove leftover job folders (e.g. after a crash or restart)."""
    keep = keep or set()
    if not work_root.is_dir():
        return
    for entry in work_root.iterdir():
        if entry.name not in keep:
            remove_tree(entry, work_root)


def free_disk_space(path: Path) -> int | None:
    try:
        return shutil.disk_usage(path).free
    except OSError:
        return None


def directory_size(path: Path) -> int:
    """Total size of the files below ``path`` (finished files and work folders)."""
    total = 0
    for root, _dirs, files in os.walk(path):
        for name in files:
            try:
                total += os.stat(os.path.join(root, name)).st_size
            except OSError:
                continue
    return total

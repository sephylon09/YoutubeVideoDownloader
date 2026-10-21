"""Filename sanitisation and output-path security."""

import os
import time
from pathlib import Path

import pytest

from media_downloader.storage import (
    cleanup_old_files,
    is_valid_job_id,
    is_within,
    make_work_dir,
    new_job_id,
    remove_tree,
    reserve_unique_path,
    sanitize_filename,
)


@pytest.mark.parametrize(
    ("title", "expected"),
    [
        ("Hello: World?", "Hello - World"),
        ("AC/DC | Live", "AC-DC - Live"),
        ('He said "hi" <loud>*', "He said 'hi' loud"),
        ("12:30 news", "12-30 news"),
        ("trailing dots...", "trailing dots"),
        ("  spaces   inside  ", "spaces inside"),
        ("日本語のタイトル 🎵", "日本語のタイトル 🎵"),
        ("Ünïcödé café", "Ünïcödé café"),
        ("👨‍👩‍👧 family", "👨‍👩‍👧 family"),
        ("line\nbreak\ttab", "line break tab"),
        ("CON", "_CON"),
        ("nul.txt", "_nul.txt"),
        ("lpt1", "_lpt1"),
        ("", "download"),
        ("???", "download"),
    ],
)
def test_sanitize_filename(title, expected):
    assert sanitize_filename(title) == expected


def test_sanitize_removes_bidi_overrides_and_controls():
    name = sanitize_filename("evil\u202egnp.exe\x00\x07 video")
    assert "\u202e" not in name and "\x00" not in name and "\x07" not in name


@pytest.mark.parametrize("title", ["../../etc/passwd", "..\\..\\Windows\\System32", "/absolute/path", "C:\\x\\y"])
def test_sanitize_never_produces_paths(title):
    name = sanitize_filename(title)
    assert "/" not in name and "\\" not in name and ":" not in name
    assert not name.startswith(".")


def test_sanitize_truncates_long_titles():
    name = sanitize_filename("a" * 500 + " b")
    assert len(name) <= 150


def test_sanitize_uses_fallback():
    assert sanitize_filename("", fallback="youtube-dQw4w9WgXcQ") == "youtube-dQw4w9WgXcQ"


def test_reserve_unique_path_avoids_collisions(clean_dir: Path):
    first = reserve_unique_path(clean_dir, "My Video", "mp4")
    second = reserve_unique_path(clean_dir, "My Video", "mp4")
    third = reserve_unique_path(clean_dir, "My Video", "mp3")
    assert first.name == "My Video.mp4"
    assert second.name == "My Video (2).mp4"
    assert third.name == "My Video.mp3"
    assert all(p.exists() and is_within(p, clean_dir) for p in (first, second, third))


def test_reserve_unique_path_with_unicode(clean_dir: Path):
    path = reserve_unique_path(clean_dir, "日本語のタイトル 🎵: part 2", "mp3")
    assert path.name == "日本語のタイトル 🎵 - part 2.mp3"
    path.write_bytes(b"data")
    assert path.read_bytes() == b"data"


def test_reserve_unique_path_sanitises_and_stays_inside(clean_dir: Path):
    path = reserve_unique_path(clean_dir, "../../escape", "mp4")
    assert path.parent == clean_dir
    with pytest.raises(ValueError):
        reserve_unique_path(clean_dir, "x", "exe/../mp4")


def test_is_within(tmp_path: Path):
    root = tmp_path / "root"
    (root / "sub").mkdir(parents=True)
    assert is_within(root / "sub" / "file.mp4", root)
    assert not is_within(root / ".." / "outside.mp4", root)
    assert not is_within(root, root)
    assert not is_within(tmp_path / "rootless" / "x", root)


@pytest.mark.parametrize("job_id,valid", [
    (new_job_id(), True),
    ("0" * 32, True),
    ("A" * 32, False),
    ("../" + "0" * 29, False),
    ("0" * 31, False),
    (None, False),
])
def test_job_id_validation(job_id, valid):
    assert is_valid_job_id(job_id) is valid


def test_make_work_dir_rejects_bad_ids(tmp_path: Path):
    good = make_work_dir(tmp_path, "a" * 32)
    assert good.is_dir() and good.parent == tmp_path
    with pytest.raises(ValueError):
        make_work_dir(tmp_path, "../evil")


def test_remove_tree_refuses_outside_root(tmp_path: Path):
    root = tmp_path / "root"
    root.mkdir()
    outside = tmp_path / "keep.txt"
    outside.write_text("keep")
    assert remove_tree(outside, root) is False
    assert outside.exists()


def test_cleanup_old_files(clean_dir: Path):
    old_time = time.time() - 48 * 3600
    old_video = clean_dir / "old.mp4"
    new_video = clean_dir / "new.mp4"
    unrelated = clean_dir / "notes.txt"
    protected_dir = clean_dir / ".work" / ("b" * 32)
    stale_dir = clean_dir / ".work" / ("c" * 32)
    for path in (old_video, new_video, unrelated):
        path.write_bytes(b"x")
    protected_dir.mkdir(parents=True)
    stale_dir.mkdir(parents=True)
    for path in (old_video, unrelated, protected_dir, stale_dir):
        os.utime(path, (old_time, old_time))

    removed = cleanup_old_files(clean_dir, 24 * 3600, protected={protected_dir})
    assert removed == 2
    assert not old_video.exists() and not stale_dir.exists()
    assert new_video.exists() and unrelated.exists() and protected_dir.exists()

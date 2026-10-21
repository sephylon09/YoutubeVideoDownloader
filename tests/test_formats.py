"""Quality options are derived from the real format list and resolved exactly."""

import pytest

from conftest import audio, info_with, make_format, video
from media_downloader.errors import AppError
from media_downloader.formats import (
    best_audio,
    best_mp4_audio,
    build_mp3_options,
    build_video_options,
    is_safe_thumbnail_url,
    quality_tier,
    resolve_plan,
    summarize_video,
    thumbnail_candidates,
    video_formats_summary,
)


# --------------------------------------------------------------- tiers

@pytest.mark.parametrize(
    ("width", "height", "tier"),
    [
        (256, 144, 144), (426, 240, 240), (640, 360, 360), (854, 480, 480), (640, 480, 480),
        (1280, 720, 720), (1920, 1080, 1080), (1080, 1920, 1080), (1920, 800, 1080), (1440, 1080, 1080),
        (2560, 1440, 1440), (2560, 1080, 1440), (3840, 2160, 2160), (3840, 1608, 2160), (3840, 1920, 2160),
        (7680, 4320, 4320), (None, 720, 720), (None, None, None), (0, 0, None),
    ],
)
def test_quality_tier(width, height, tier):
    assert quality_tier(width, height) == tier


# ------------------------------------------------- real-world format list

def test_real_format_list_produces_every_resolution(bbb_info):
    options = build_video_options(bbb_info)
    assert [o.id for o in options] == [
        "2160p60", "1440p60", "1080p60", "720p60", "480p30", "360p30", "240p30", "144p30"]
    assert [o.label for o in options][:4] == ["2160p60", "1440p60", "1080p60", "720p60"]


def test_prefers_h264_and_https_when_available(bbb_info):
    options = {o.id: o for o in build_video_options(bbb_info)}
    # H.264 exists up to 1080p: chosen for maximum MP4 compatibility, HTTPS over HLS.
    assert options["1080p60"].video["format_id"] == "299"
    assert options["1080p60"].video_codec == "h264"
    # Above 1080p only VP9/AV1 exist; VP9 is preferred, not silently downgraded.
    assert options["2160p60"].video["format_id"] == "315"
    assert options["2160p60"].video_codec == "vp9"
    assert set(options["2160p60"].alternatives) == {"VP9", "AV1"}


def test_separate_streams_are_paired_with_aac_audio(bbb_info):
    plan = resolve_plan(bbb_info, "mp4", "1080p60")
    assert plan.format_ids == ["299", "140"]  # video-only + original-language AAC
    assert not plan.reencode_video and not plan.reencode_audio
    assert plan.stages == ["preparing", "downloading_video", "downloading_audio", "merging", "finalizing"]
    assert plan.estimated_size == 257619653 + 10271496


def test_best_resolves_to_highest_quality(bbb_info):
    plan = resolve_plan(bbb_info, "mp4", "best")
    assert plan.option_id == "2160p60"
    assert plan.format_ids == ["315", "140"]
    summary = video_formats_summary(bbb_info, convert_h264_available=True)
    assert summary["best"]["resolves_to"] == "2160p60"
    assert summary["h264_conversion_available"] is True


def test_requested_resolution_is_never_downgraded(bbb_info):
    with pytest.raises(AppError) as excinfo:
        resolve_plan(bbb_info, "mp4", "4320p60")
    assert excinfo.value.code == "quality_unavailable"
    assert "4320p60" in excinfo.value.message


@pytest.mark.parametrize("quality", ["", "1080", "1080p60; rm -rf", "../etc", "BEST", "1080p60-sdr", "p"])
def test_invalid_quality_ids_are_rejected(bbb_info, quality):
    with pytest.raises(AppError) as excinfo:
        resolve_plan(bbb_info, "mp4", quality)
    assert excinfo.value.code == "invalid_quality"


def test_invalid_kind_is_rejected(bbb_info):
    with pytest.raises(AppError):
        resolve_plan(bbb_info, "mkv", "best")


def test_h264_conversion_only_reencodes_non_h264(bbb_info):
    vp9 = resolve_plan(bbb_info, "mp4", "1440p60", convert_h264=True)
    assert vp9.reencode_video and "H.264" in vp9.label
    assert vp9.stages[-2] == "converting_video"
    h264 = resolve_plan(bbb_info, "mp4", "1080p60", convert_h264=True)
    assert not h264.reencode_video


def test_mp3_uses_best_original_audio(bbb_info):
    plan = resolve_plan(bbb_info, "mp3", "best")
    assert plan.format_ids == ["251"]  # Opus ~129 kbps, the best source stream
    assert plan.mp3_bitrate is None  # VBR V0
    plan_192 = resolve_plan(bbb_info, "mp3", "192")
    assert plan_192.mp3_bitrate == 192
    assert plan_192.stages == ["preparing", "downloading_audio", "converting_mp3", "finalizing"]


def test_mp3_options_flag_bitrates_above_the_source(bbb_info):
    mp3 = build_mp3_options(bbb_info)
    assert mp3["source"]["codec"] == "Opus" and mp3["source"]["bitrate"] == 129
    by_id = {o["id"]: o for o in mp3["options"]}
    assert list(by_id) == ["best", "320", "256", "192", "128"]
    assert by_id["320"]["above_source"] and by_id["256"]["above_source"] and by_id["192"]["above_source"]
    assert not by_id["128"]["above_source"]
    assert by_id["192"]["filesize"] == int(192 * 1000 / 8 * bbb_info["duration"])


def test_summary_metadata(bbb_info):
    summary = summarize_video(bbb_info, "https://www.youtube.com/watch?v=aqz-KE-bpKQ")
    assert summary["max_quality"] == "2160p60"
    assert summary["resolutions"][0] == "2160p60"
    assert summary["upload_date"] == "2014-11-10"
    assert summary["thumbnail"].startswith("https://i.ytimg.com/")


# ------------------------------------------------------ synthetic cases

def test_options_reflect_only_available_formats():
    info = info_with(video("136", 1280, 720, "avc1.4d401f"), video("134", 640, 360, "avc1.4d401e"),
                     audio("140", "mp4a.40.2", 128))
    assert [o.id for o in build_video_options(info)] == ["720p30", "360p30"]


def test_progressive_only_format_needs_no_merge():
    info = info_with(make_format("18", width=640, height=360, vcodec="avc1.42001E", acodec="mp4a.40.2",
                                 fps=30, filesize=5_000_000, ext="mp4"))
    (option,) = build_video_options(info)
    assert option.progressive and option.audio is None
    plan = resolve_plan(info, "mp4", option.id)
    assert plan.format_ids == ["18"]
    assert "merging" not in plan.stages and "downloading_audio" not in plan.stages


def test_video_only_preferred_over_progressive_when_audio_exists():
    info = info_with(
        make_format("18", width=640, height=360, vcodec="avc1.42001E", acodec="mp4a.40.2", fps=30, filesize=5_000_000),
        video("134", 640, 360, "avc1.4d401e"),
        audio("140", "mp4a.40.2", 128),
    )
    (option,) = build_video_options(info)
    assert option.video["format_id"] == "134" and option.audio["format_id"] == "140"


def test_opus_only_audio_is_converted_to_aac_for_mp4():
    info = info_with(video("248", 1920, 1080, "vp9", ext="webm"), audio("251", "opus", 140, ext="webm"))
    plan = resolve_plan(info, "mp4", "1080p30")
    assert plan.reencode_audio and not plan.reencode_video
    assert build_video_options(info)[0].processing(False) == "audio"


def test_hdr_is_a_separate_option_and_best_prefers_sdr():
    info = info_with(
        video("337", 3840, 2160, "vp09.02.51.10", fps=60, dynamic_range="HDR10", ext="webm"),
        video("315", 3840, 2160, "vp9", fps=60, ext="webm"),
        audio("140", "mp4a.40.2", 128),
    )
    options = build_video_options(info)
    assert [o.id for o in options] == ["2160p60", "2160p60-hdr"]
    assert resolve_plan(info, "mp4", "best").video["format_id"] == "315"
    assert resolve_plan(info, "mp4", "2160p60-hdr").video["format_id"] == "337"
    with pytest.raises(AppError) as excinfo:
        resolve_plan(info, "mp4", "2160p60-hdr", convert_h264=True)
    assert excinfo.value.code == "conversion_unavailable"


def test_h264_conversion_not_offered_above_4k():
    info = info_with(video("571", 7680, 4320, "av01.0.17M.08", fps=60), audio("140", "mp4a.40.2", 128))
    with pytest.raises(AppError):
        resolve_plan(info, "mp4", "4320p60", convert_h264=True)
    assert resolve_plan(info, "mp4", "4320p60").video["format_id"] == "571"


def test_low_fps_duplicates_are_collapsed():
    info = info_with(video("602", 256, 144, "vp09.00.10.08", fps=12), video("160", 256, 144, "avc1.4d400c", fps=24),
                     audio("140", "mp4a.40.2", 128))
    (option,) = build_video_options(info)
    assert option.fps == 24 and option.video["format_id"] == "160"


def test_original_language_and_non_drc_audio_preferred():
    info = info_with(
        video("137", 1920, 1080, "avc1.640028"),
        audio("140-0", "mp4a.40.2", 130, language="es", language_preference=-1),
        audio("251-0", "opus", 135, language="es", language_preference=-1, ext="webm"),
        audio("140-drc", "mp4a.40.2", 129, language="en", language_preference=10, format_note="medium, DRC"),
        audio("140-1", "mp4a.40.2", 129, language="en", language_preference=10),
        audio("251-1", "opus", 125, language="en", language_preference=10, ext="webm"),
    )
    assert best_mp4_audio(info["formats"])["format_id"] == "140-1"
    assert best_audio(info["formats"])["format_id"] == "251-1"


def test_storyboards_drm_and_unknown_codecs_are_ignored():
    info = info_with(
        make_format("sb0", ext="mhtml", protocol="mhtml", vcodec="none", acodec="none", width=320, height=180,
                    format_note="storyboard"),
        video("999", 1920, 1080, "avc1.640028", has_drm=True),
        video("137", 1280, 720, "avc1.640028"),
        make_format("233", vcodec="none", acodec=None, protocol="m3u8_native"),
        audio("140", "mp4a.40.2", 128),
    )
    assert [o.id for o in build_video_options(info)] == ["720p30"]
    assert best_audio(info["formats"])["format_id"] == "140"


def test_errors_when_nothing_is_downloadable():
    with pytest.raises(AppError) as excinfo:
        resolve_plan(info_with(), "mp4", "best")
    assert excinfo.value.code == "no_formats"
    with pytest.raises(AppError) as excinfo:
        resolve_plan(info_with(video("137", 1920, 1080, "avc1.640028")), "mp3", "best")
    assert excinfo.value.code == "no_audio"


def test_estimated_size_falls_back_to_bitrate():
    info = info_with(video("137", 1920, 1080, "avc1.640028", size=None, tbr=4000),
                     audio("140", "mp4a.40.2", 128, size=None), duration=100)
    size, approximate = build_video_options(info)[0].estimated_size(100)
    assert approximate
    assert size == int(4000 * 1000 / 8 * 100) + int(128 * 1000 / 8 * 100)


@pytest.mark.parametrize(
    ("url", "safe"),
    [
        ("https://i.ytimg.com/vi/x/hqdefault.jpg", True),
        ("https://i9.ytimg.com/vi/x/hqdefault.jpg", True),
        ("http://i.ytimg.com/vi/x/hqdefault.jpg", False),
        ("https://ytimg.com.evil.net/x.jpg", False),
        ("https://evil.net/i.ytimg.com/x.jpg", False),
        ("javascript:alert(1)", False),
        (None, False),
    ],
)
def test_thumbnail_url_allow_list(url, safe):
    assert is_safe_thumbnail_url(url) is safe


def test_thumbnail_candidates_skip_unsafe_entries():
    info = {"id": "dQw4w9WgXcQ", "thumbnails": [
        {"url": "https://evil.example/x.jpg"},
        {"url": "https://i.ytimg.com/vi/dQw4w9WgXcQ/maxresdefault.jpg"},
    ]}
    urls = thumbnail_candidates(info, jpeg_only=True)
    assert urls[0] == "https://i.ytimg.com/vi/dQw4w9WgXcQ/maxresdefault.jpg"
    assert all("evil" not in u for u in urls)

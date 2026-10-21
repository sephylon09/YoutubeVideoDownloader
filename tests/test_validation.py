"""URL validation: only explicit YouTube hosts/shapes are accepted."""

import pytest

from media_downloader.errors import AppError
from media_downloader.validation import parse_youtube_url

ID = "dQw4w9WgXcQ"
SHORT_ID = "aBcDeF-_123"


@pytest.mark.parametrize(
    ("url", "video_id", "kind"),
    [
        (f"https://www.youtube.com/watch?v={ID}", ID, "watch"),
        (f"http://youtube.com/watch?v={ID}&t=42s", ID, "watch"),
        (f"www.youtube.com/watch?v={ID}", ID, "watch"),
        (f"youtube.com/watch?feature=share&v={ID}", ID, "watch"),
        (f"https://m.youtube.com/watch?v={ID}&list=PL0123456789&index=2", ID, "watch"),
        (f"https://music.youtube.com/watch?v={ID}&si=tracking", ID, "watch"),
        (f"https://WWW.YouTube.COM/watch?v={ID}", ID, "watch"),
        (f"https://www.youtube.com:443/watch?v={ID}", ID, "watch"),
        (f"https://www.youtube.com./watch?v={ID}", ID, "watch"),
        (f"  https://www.youtube.com/watch?v={ID}  ", ID, "watch"),
        (f"https://youtu.be/{ID}", ID, "short_link"),
        (f"https://youtu.be/{ID}?si=abc&t=10", ID, "short_link"),
        (f"youtu.be/{ID}", ID, "short_link"),
        (f"https://www.youtube.com/shorts/{SHORT_ID}?feature=share", SHORT_ID, "short"),
        (f"https://youtube.com/shorts/{SHORT_ID}/", SHORT_ID, "short"),
        (f"https://www.youtube.com/live/{SHORT_ID}?si=x", SHORT_ID, "live"),
        (f"https://www.youtube.com/embed/{SHORT_ID}?start=5", SHORT_ID, "embed"),
        (f"https://www.youtube-nocookie.com/embed/{SHORT_ID}", SHORT_ID, "embed"),
        (f"//www.youtube.com/watch?v={ID}", ID, "watch"),
    ],
)
def test_accepts_supported_urls(url, video_id, kind):
    ref = parse_youtube_url(url)
    assert ref.video_id == video_id
    assert ref.kind == kind
    # Whatever was pasted, yt-dlp only ever sees the canonical single-video URL.
    assert ref.canonical_url == f"https://www.youtube.com/watch?v={video_id}"


def test_shorts_are_flagged():
    assert parse_youtube_url(f"https://www.youtube.com/shorts/{SHORT_ID}").is_short
    assert not parse_youtube_url(f"https://youtu.be/{ID}").is_short


@pytest.mark.parametrize(
    ("url", "code"),
    [
        ("", "empty_url"),
        ("    ", "empty_url"),
        ("not a url", "invalid_url"),
        ("https://vimeo.com/123456", "unsupported_url"),
        (f"https://youtube.com.evil.com/watch?v={ID}", "unsupported_url"),
        (f"https://evilyoutube.com/watch?v={ID}", "unsupported_url"),
        (f"https://notyoutu.be/{ID}", "unsupported_url"),
        (f"https://youtu.be.evil.net/{ID}", "unsupported_url"),
        ("javascript:alert(1)", "invalid_url"),
        (f"ftp://youtube.com/watch?v={ID}", "invalid_url"),
        ("file:///C:/Windows/win.ini", "invalid_url"),
        (f"https://user:pass@youtube.com/watch?v={ID}", "invalid_url"),
        (f"https://youtube.com@evil.com/watch?v={ID}", "invalid_url"),
        (f"https://www.youtube.com:8080/watch?v={ID}", "invalid_url"),
        (f"https://www.youtube.com:99999/watch?v={ID}", "invalid_url"),
        ("https://www.youtube.com/watch?v=short", "invalid_url"),
        (f"https://www.youtube.com/watch?v={ID}1", "invalid_url"),
        ("https://www.youtube.com/watch?v=dQw4w9WgX<Q", "invalid_url"),
        ("https://www.youtube.com/watch?v=", "invalid_url"),
        ("https://www.youtube.com/playlist?list=PL0123456789", "unsupported_url"),
        ("https://www.youtube.com/watch?list=PL0123456789", "unsupported_url"),
        ("https://www.youtube.com/@SomeChannel", "unsupported_url"),
        ("https://www.youtube.com/channel/UC0123456789", "unsupported_url"),
        ("https://www.youtube.com/results?search_query=test", "unsupported_url"),
        ("https://www.youtube.com/", "invalid_url"),
        ("https://youtu.be/", "invalid_url"),
        ("https://youtu.be/../../etc/passwd", "invalid_url"),
        (f"https://www.youtube.com/watch?v={ID}\nhttps://evil.example", "invalid_url"),
        (f"https://www.youtube.com/watch?v={ID} https://evil.example", "invalid_url"),
        (f"https://www.youtube-nocookie.com/watch?v={ID}", "unsupported_url"),
        ("https://www.youtube.com/watch?v=" + "a" * 3000, "invalid_url"),
    ],
)
def test_rejects_invalid_or_unsupported_urls(url, code):
    with pytest.raises(AppError) as excinfo:
        parse_youtube_url(url)
    assert excinfo.value.code == code
    assert excinfo.value.status == 400
    assert excinfo.value.message  # always a human-readable explanation


@pytest.mark.parametrize("value", [None, 123, b"https://youtu.be/dQw4w9WgXcQ", ["x"]])
def test_rejects_non_strings(value):
    with pytest.raises(AppError):
        parse_youtube_url(value)

import pytest

from app.core.urls import VIDEO_HOSTS, safe_url


@pytest.mark.parametrize(
    "value",
    [
        "javascript:alert(1)",
        "JavaScript:alert(1)",
        "data:text/html;base64,PHNjcmlwdD4=",
        "http://cdn.jwplayer.com/players/x.html",
        "https://evil.com/players/x.html",
        "https://cdn.jwplayer.com.evil.com/x.html",
        "//evil.com/x.html",
        "https://user:pass@cdn.jwplayer.com/x.html",
        "https://cdn.jwplayer.com:8443/x.html",
        r"https://cdn.jwplayer.com\@evil.com/x.html",
        "https://cdn.jwplayer.com/x.html\njavascript:alert(1)",
        "",
        None,
        42,
    ],
)
def test_safe_url_rejects(value) -> None:
    assert safe_url(value, VIDEO_HOSTS) is None


def test_safe_url_accepts_allowed_https_host() -> None:
    url = "https://cdn.jwplayer.com/players/OXExoEAD.html"
    assert safe_url(url, VIDEO_HOSTS) == url
    assert safe_url("https://CDN.jwplayer.com/x.html", VIDEO_HOSTS) is not None

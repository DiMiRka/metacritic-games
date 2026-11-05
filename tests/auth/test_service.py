import pytest

from app.auth.service import safe_next


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        ("/monitor?x=1", "/monitor?x=1"),
        ("//evil.com", "/"),
        ("https://evil.com", "/"),
        ("/\\evil.com", "/"),
        ("/ok\r\nLocation: x", "/"),
        ("monitor", "/"),
        ("", "/"),
        (None, "/"),
    ],
)
def test_safe_next(value, expected) -> None:
    assert safe_next(value) == expected

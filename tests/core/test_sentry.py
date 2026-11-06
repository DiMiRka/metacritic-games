from app.core import sentry
from app.core.config import settings


def test_sentry_disabled_without_dsn(monkeypatch) -> None:
    calls = []
    monkeypatch.setattr(settings, "sentry_dsn", "")
    monkeypatch.setattr(sentry.sentry_sdk, "init", lambda **kw: calls.append(kw))
    assert sentry.init_sentry("web") is False
    assert calls == []


def test_sentry_init_without_pii_and_with_loguru(monkeypatch) -> None:
    calls, tags = [], []
    monkeypatch.setattr(settings, "sentry_dsn", "https://key@sentry.example/1")
    monkeypatch.setattr(sentry.sentry_sdk, "init", lambda **kw: calls.append(kw))
    monkeypatch.setattr(sentry.sentry_sdk, "set_tag", lambda *a: tags.append(a))

    assert sentry.init_sentry("worker") is True

    (options,) = calls
    assert options["send_default_pii"] is False
    assert options["environment"] == settings.env
    assert [type(i).__name__ for i in options["integrations"]] == ["LoguruIntegration"]
    assert tags == [("component", "worker")]

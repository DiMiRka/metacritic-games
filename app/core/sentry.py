"""Отчёты об исключениях в Sentry или совместимый сервис (GlitchTip, self-hosted Sentry)"""

import sentry_sdk
from sentry_sdk.integrations.logging import LoggingIntegration
from sentry_sdk.integrations.loguru import LoggingLevels, LoguruIntegration

from app.core.config import Settings, settings


def init_sentry(component: str, config: Settings | None = None) -> bool:
    """Без SENTRY_DSN ничего не делает. Событие создаёт каждая строка лога уровня ERROR
    и выше, включая logger.exception, и любое необработанное исключение в web
    """
    config = config or settings
    if not config.sentry_dsn:
        return False
    sentry_sdk.init(
        dsn=config.sentry_dsn,
        environment=config.env,
        traces_sample_rate=config.sentry_traces_sample_rate,
        send_default_pii=False,
        integrations=[
            LoguruIntegration(level=LoggingLevels.INFO.value, event_level=LoggingLevels.ERROR.value)
        ],
        # Стандартный logging и так перенаправлен в loguru, иначе события задвоятся
        disabled_integrations=[LoggingIntegration()],
    )
    sentry_sdk.set_tag("component", component)
    return True

from datetime import datetime
from enum import StrEnum

from sqlalchemy import (
    BigInteger,
    Boolean,
    DateTime,
    ForeignKey,
    Index,
    String,
    Text,
    func,
    text,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.core.models import Base, created_column, enum_check


class UserRole(StrEnum):
    VIEWER = "viewer"
    ADMIN = "admin"


class User(Base):
    __tablename__ = "users"

    id: Mapped[int] = mapped_column(primary_key=True)
    login: Mapped[str] = mapped_column(String(100), unique=True)
    password_hash: Mapped[str] = mapped_column(Text)
    role: Mapped[str] = mapped_column(String(10), default=UserRole.VIEWER)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True, server_default=text("true"))
    created_at: Mapped[datetime] = created_column()
    last_login_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    # Секрет TOTP, зашифрованный ключом из SECRET_KEY (app/auth/crypto.py), и последний
    # принятый шаг: один код дважды не проходит
    totp_secret: Mapped[str | None] = mapped_column(Text)
    totp_last_step: Mapped[int | None] = mapped_column(BigInteger)

    __table_args__ = (enum_check("role", UserRole),)


class UserSession(Base):
    """Серверная сессия: в базе только sha256 от токена из cookie"""

    __tablename__ = "sessions"

    id: Mapped[int] = mapped_column(primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"))
    token_hash: Mapped[str] = mapped_column(String(64), unique=True)
    created_at: Mapped[datetime] = created_column()
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    last_seen_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )
    ip: Mapped[str | None] = mapped_column(String(64))
    user_agent: Mapped[str | None] = mapped_column(String(300))

    user: Mapped[User] = relationship()

    __table_args__ = (
        Index("ix_sessions_user_id", "user_id"),
        Index("ix_sessions_expires_at", "expires_at"),
    )


class LoginAttempt(Base):
    """Неудачные попытки входа: счётчик в базе общий для всех процессов web"""

    __tablename__ = "login_attempts"

    id: Mapped[int] = mapped_column(primary_key=True)
    ip: Mapped[str] = mapped_column(String(64))
    login: Mapped[str] = mapped_column(String(100))
    attempted_at: Mapped[datetime] = created_column()

    __table_args__ = (
        Index("ix_login_attempts_ip_login_at", "ip", "login", "attempted_at"),
        Index("ix_login_attempts_login_at", "login", "attempted_at"),
    )


class TotpRecoveryCode(Base):
    """Одноразовый резервный код второго фактора. Код вида abcd-efgh-ijkl: первая группа
    ищет запись, остальное хранится как хеш argon2
    """

    __tablename__ = "totp_recovery_codes"

    id: Mapped[int] = mapped_column(primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"))
    lookup: Mapped[str] = mapped_column(String(8))
    code_hash: Mapped[str] = mapped_column(Text)
    created_at: Mapped[datetime] = created_column()
    used_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    __table_args__ = (
        Index("uq_totp_recovery_codes_user_id_lookup", "user_id", "lookup", unique=True),
    )

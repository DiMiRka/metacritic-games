from datetime import datetime
from enum import StrEnum

from sqlalchemy import BigInteger, Boolean, DateTime, String, Text, text
from sqlalchemy.orm import Mapped, mapped_column

from app.core.models import Base, created_column, enum_check


class BackupStatus(StrEnum):
    RUNNING = "running"
    SUCCESS = "success"
    FAILED = "failed"


class BackupRun(Base):
    """Журнал бэкапов: пишет контейнер backup (scripts/backup.sh), читает монитор и метрики"""

    __tablename__ = "backup_runs"

    id: Mapped[int] = mapped_column(primary_key=True)
    started_at: Mapped[datetime] = created_column()
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    status: Mapped[str] = mapped_column(String(10), default=BackupStatus.RUNNING)
    file: Mapped[str | None] = mapped_column(String(200))
    size_bytes: Mapped[int | None] = mapped_column(BigInteger)
    # Копия восстановлена в отдельную базу и версия схемы совпала
    restore_checked: Mapped[bool] = mapped_column(
        Boolean, default=False, server_default=text("false")
    )
    error: Mapped[str | None] = mapped_column(Text)

    __table_args__ = (enum_check("status", BackupStatus),)

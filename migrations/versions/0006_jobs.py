"""jobs: task queue in Postgres

Revision ID: 0006
Revises: 0005
Create Date: 2026-11-06
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0006"
down_revision: str | None = "0005"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "jobs",
        sa.Column("id", sa.BigInteger(), nullable=False),
        sa.Column("kind", sa.String(20), nullable=False),
        sa.Column("payload", postgresql.JSONB(), server_default=sa.text("'{}'"), nullable=False),
        sa.Column("status", sa.String(10), nullable=False),
        sa.Column("priority", sa.Integer(), nullable=False),
        sa.Column("attempts", sa.Integer(), nullable=False),
        sa.Column("max_attempts", sa.Integer(), nullable=False),
        sa.Column(
            "run_after", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False
        ),
        sa.Column("locked_by", sa.String(200)),
        sa.Column("locked_until", sa.DateTime(timezone=True)),
        sa.Column("dedupe_key", sa.Text()),
        sa.Column("run_id", sa.Integer()),
        sa.Column("requested_by", sa.Integer()),
        sa.Column("last_error", sa.Text()),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.Column("started_at", sa.DateTime(timezone=True)),
        sa.Column("finished_at", sa.DateTime(timezone=True)),
        sa.PrimaryKeyConstraint("id", name="pk_jobs"),
        sa.ForeignKeyConstraint(
            ["run_id"], ["runs.id"], name="fk_jobs_run_id_runs", ondelete="SET NULL"
        ),
        sa.ForeignKeyConstraint(
            ["requested_by"], ["users.id"], name="fk_jobs_requested_by_users", ondelete="SET NULL"
        ),
        sa.CheckConstraint(
            "kind IN ('collect', 'scrape_game', 'summarize', 'embed', 'letsplay')",
            name=op.f("ck_jobs_kind"),
        ),
        sa.CheckConstraint(
            "status IN ('queued', 'running', 'done', 'failed', 'dead', 'cancelled')",
            name=op.f("ck_jobs_status"),
        ),
    )
    op.create_index(
        "ix_jobs_pick",
        "jobs",
        [sa.text("priority DESC"), "run_after", "id"],
        postgresql_where=sa.text("status = 'queued'"),
    )
    op.create_index(
        "uq_jobs_dedupe_key_active",
        "jobs",
        ["dedupe_key"],
        unique=True,
        postgresql_where=sa.text("status IN ('queued', 'running')"),
    )
    op.create_index("ix_jobs_run_id", "jobs", ["run_id"])
    op.create_index("ix_jobs_kind_status", "jobs", ["kind", "status"])
    op.create_foreign_key(
        "fk_run_items_job_id_jobs", "run_items", "jobs", ["job_id"], ["id"], ondelete="SET NULL"
    )


def downgrade() -> None:
    op.drop_constraint("fk_run_items_job_id_jobs", "run_items", type_="foreignkey")
    op.drop_table("jobs")

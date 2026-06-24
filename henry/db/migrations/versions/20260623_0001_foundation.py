"""foundation schema

Revision ID: 20260623_0001
Revises:
Create Date: 2026-06-23 00:00:00.000000
"""

from __future__ import annotations

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision = "20260623_0001"
down_revision = None
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "channel_config",
        sa.Column("channel_id", sa.String(length=128), nullable=False),
        sa.Column("system_prompt", sa.Text(), nullable=False),
        sa.Column("model", sa.String(length=256), nullable=False),
        sa.Column("enabled_integrations", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("ambient_on", sa.Boolean(), nullable=False),
        sa.Column("budget_caps", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.PrimaryKeyConstraint("channel_id", name=op.f("pk_channel_config")),
    )
    op.create_table(
        "channel_memory",
        sa.Column("channel_id", sa.String(length=128), nullable=False),
        sa.Column("path", sa.String(length=512), nullable=False),
        sa.Column("content", sa.Text(), nullable=False),
        sa.Column("kind", sa.String(length=64), nullable=False),
        sa.Column("metadata", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.PrimaryKeyConstraint("channel_id", "path", name=op.f("pk_channel_memory")),
    )
    op.create_table(
        "channel_state",
        sa.Column("channel_id", sa.String(length=128), nullable=False),
        sa.Column("rolling_summary", sa.Text(), nullable=False),
        sa.Column("open_tasks", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("key_facts", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.PrimaryKeyConstraint("channel_id", name=op.f("pk_channel_state")),
    )
    op.create_table(
        "task",
        sa.Column("id", sa.BigInteger(), autoincrement=True, nullable=False),
        sa.Column("channel_id", sa.String(length=128), nullable=False),
        sa.Column("thread_ts", sa.String(length=64), nullable=False),
        sa.Column("kind", sa.String(length=64), nullable=False),
        sa.Column("status", sa.String(length=32), nullable=False),
        sa.Column("run_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("payload", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("dedup_key", sa.String(length=256), nullable=False),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_task")),
        sa.UniqueConstraint("dedup_key", name=op.f("uq_task_dedup_key")),
    )
    op.create_index("ix_task_status_run_at", "task", ["status", "run_at"])
    op.create_table(
        "audit_log",
        sa.Column("id", sa.BigInteger(), autoincrement=True, nullable=False),
        sa.Column("run_id", sa.String(length=128), nullable=False),
        sa.Column("channel_id", sa.String(length=128), nullable=False),
        sa.Column("thread_ts", sa.String(length=64), nullable=False),
        sa.Column("actor", sa.String(length=128), nullable=False),
        sa.Column("action", sa.String(length=128), nullable=False),
        sa.Column("integration", sa.String(length=128), nullable=False),
        sa.Column("model", sa.String(length=256), nullable=False),
        sa.Column("input_tokens", sa.Integer(), nullable=False),
        sa.Column("output_tokens", sa.Integer(), nullable=False),
        sa.Column("cost_usd", sa.Numeric(precision=12, scale=6), nullable=False),
        sa.Column("latency_ms", sa.Integer(), nullable=False),
        sa.Column("status", sa.String(length=32), nullable=False),
        sa.Column("error", sa.Text(), nullable=True),
        sa.Column("ts", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_audit_log")),
    )
    op.create_index("ix_audit_log_channel_id_ts", "audit_log", ["channel_id", "ts"])


def downgrade() -> None:
    op.drop_index("ix_audit_log_channel_id_ts", table_name="audit_log")
    op.drop_table("audit_log")
    op.drop_index("ix_task_status_run_at", table_name="task")
    op.drop_table("task")
    op.drop_table("channel_state")
    op.drop_table("channel_memory")
    op.drop_table("channel_config")

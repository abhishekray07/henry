"""channel_config.enabled_integrations: NULL means "no override"

Revision ID: 20260713_0002
Revises: 20260623_0001
Create Date: 2026-07-13 00:00:00.000000
"""

from __future__ import annotations

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision = "20260713_0002"
down_revision = "20260623_0001"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.alter_column(
        "channel_config",
        "enabled_integrations",
        existing_type=postgresql.JSONB(astext_type=sa.Text()),
        nullable=True,
    )
    # Existing [] values are the old column default, written back when [] also matched
    # the global default and therefore meant nothing. Under the "*" default they would
    # become surprise deny-all overrides, so convert them to "no override".
    op.execute("UPDATE channel_config SET enabled_integrations = NULL WHERE enabled_integrations = '[]'::jsonb")


def downgrade() -> None:
    op.execute("UPDATE channel_config SET enabled_integrations = '[]'::jsonb WHERE enabled_integrations IS NULL")
    op.alter_column(
        "channel_config",
        "enabled_integrations",
        existing_type=postgresql.JSONB(astext_type=sa.Text()),
        nullable=False,
    )

"""prompt registry: prompt_versions

Revision ID: 0003_prompt_versions
Revises: 0002_chunk_fts
Create Date: 2026-10-12

Pure "expand" change (new table), safe to deploy before the code.
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0003_prompt_versions"
down_revision: str | None = "0002_chunk_fts"
branch_labels: Sequence[str] | str | None = None
depends_on: Sequence[str] | str | None = None


def upgrade() -> None:
    op.create_table(
        "prompt_versions",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("name", sa.String(100), nullable=False),
        sa.Column("version", sa.String(50), nullable=False),
        sa.Column("template_hash", sa.String(64), nullable=False),
        sa.Column("author", sa.String(255), nullable=False),
        sa.Column("status", sa.String(20), nullable=False),
        sa.Column("retired_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint("name", "version", name="uq_prompt_versions_name_version"),
    )


def downgrade() -> None:
    op.drop_table("prompt_versions")

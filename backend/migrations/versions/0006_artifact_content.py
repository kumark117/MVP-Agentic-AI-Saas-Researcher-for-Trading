"""Artifact bytes in PostgreSQL (DV-15).

Hosts such as Render give the api and worker no shared disk, so the Postgres
ArtifactStore keeps the bytes in `artifacts.content`. The local store leaves it NULL.
Existing table grants and RLS policies cover the new column.

Revision ID: 0006
Revises: 0005
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0006"
down_revision: str | None = "0005"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

OLD_COMMENT = "retention=activity_90d; object references (local ArtifactStore in MVP)"
NEW_COMMENT = (
    "retention=activity_90d; object references; bytes on a local volume or in content (DV-15)"
)


def upgrade() -> None:
    op.add_column("artifacts", sa.Column("content", sa.LargeBinary(), nullable=True))
    op.create_table_comment("artifacts", NEW_COMMENT, existing_comment=OLD_COMMENT)


def downgrade() -> None:
    op.create_table_comment("artifacts", OLD_COMMENT, existing_comment=NEW_COMMENT)
    op.drop_column("artifacts", "content")

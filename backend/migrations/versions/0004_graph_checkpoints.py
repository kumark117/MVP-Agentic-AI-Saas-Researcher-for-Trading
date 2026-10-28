"""LangGraph checkpoint schema (P6; build prompt section 7: "in a separate schema graph").

The checkpointer (langgraph-checkpoint-postgres) creates and migrates its own tables, so
this migration only creates the schema and lets the worker role, the sole user of
checkpoints, create objects in it. The API role has no access: checkpoints hold run
state and are never served directly.

Revision ID: 0004
Revises: 0003
"""

from collections.abc import Sequence

from alembic import op

revision: str = "0004"
down_revision: str | None = "0003"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.execute("CREATE SCHEMA IF NOT EXISTS graph")
    op.execute("REVOKE ALL ON SCHEMA graph FROM PUBLIC")
    op.execute("GRANT USAGE, CREATE ON SCHEMA graph TO alloyiq_worker")


def downgrade() -> None:
    op.execute("DROP SCHEMA IF EXISTS graph CASCADE")

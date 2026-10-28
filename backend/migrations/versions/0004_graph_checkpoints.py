"""LangGraph checkpoint schema (P6; build prompt section 7: "in a separate schema graph").

The checkpoint tables are created here, as the migrator, from a frozen copy of
langgraph-checkpoint-postgres 3.2.0's migrations 0-9 (recorded in checkpoint_migrations,
so the library sees an up-to-date schema). The worker role, the only user of checkpoints,
gets data access but cannot create or alter objects, and the API role has no access:
checkpoints hold run state and are never served directly. A library upgrade that adds
checkpoint migrations needs a new Alembic revision.

Revision ID: 0004
Revises: 0003
"""

from collections.abc import Sequence

from alembic import op

revision: str = "0004"
down_revision: str | None = "0003"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

# langgraph-checkpoint-postgres MIGRATIONS[0..9]; CONCURRENTLY dropped (empty tables,
# inside the migration transaction).
CHECKPOINT_SQL = [
    "CREATE TABLE graph.checkpoint_migrations (v INTEGER PRIMARY KEY)",
    """CREATE TABLE graph.checkpoints (
        thread_id TEXT NOT NULL,
        checkpoint_ns TEXT NOT NULL DEFAULT '',
        checkpoint_id TEXT NOT NULL,
        parent_checkpoint_id TEXT,
        type TEXT,
        checkpoint JSONB NOT NULL,
        metadata JSONB NOT NULL DEFAULT '{}',
        PRIMARY KEY (thread_id, checkpoint_ns, checkpoint_id))""",
    """CREATE TABLE graph.checkpoint_blobs (
        thread_id TEXT NOT NULL,
        checkpoint_ns TEXT NOT NULL DEFAULT '',
        channel TEXT NOT NULL,
        version TEXT NOT NULL,
        type TEXT NOT NULL,
        blob BYTEA,
        PRIMARY KEY (thread_id, checkpoint_ns, channel, version))""",
    """CREATE TABLE graph.checkpoint_writes (
        thread_id TEXT NOT NULL,
        checkpoint_ns TEXT NOT NULL DEFAULT '',
        checkpoint_id TEXT NOT NULL,
        task_id TEXT NOT NULL,
        idx INTEGER NOT NULL,
        channel TEXT NOT NULL,
        type TEXT,
        blob BYTEA NOT NULL,
        PRIMARY KEY (thread_id, checkpoint_ns, checkpoint_id, task_id, idx))""",
    "ALTER TABLE graph.checkpoint_blobs ALTER COLUMN blob DROP NOT NULL",
    "SELECT 1",
    "CREATE INDEX checkpoints_thread_id_idx ON graph.checkpoints (thread_id)",
    "CREATE INDEX checkpoint_blobs_thread_id_idx ON graph.checkpoint_blobs (thread_id)",
    "CREATE INDEX checkpoint_writes_thread_id_idx ON graph.checkpoint_writes (thread_id)",
    "ALTER TABLE graph.checkpoint_writes ADD COLUMN IF NOT EXISTS task_path TEXT NOT NULL DEFAULT ''",
]


def upgrade() -> None:
    op.execute("CREATE SCHEMA graph")
    op.execute("REVOKE ALL ON SCHEMA graph FROM PUBLIC")
    for sql in CHECKPOINT_SQL:
        op.execute(sql)
    op.execute(
        "INSERT INTO graph.checkpoint_migrations (v)"
        f" SELECT generate_series(0, {len(CHECKPOINT_SQL) - 1})"
    )
    op.execute("GRANT USAGE ON SCHEMA graph TO alloyiq_worker")
    op.execute(
        "GRANT SELECT, INSERT, UPDATE, DELETE ON ALL TABLES IN SCHEMA graph TO alloyiq_worker"
    )


def downgrade() -> None:
    op.execute("DROP SCHEMA IF EXISTS graph CASCADE")

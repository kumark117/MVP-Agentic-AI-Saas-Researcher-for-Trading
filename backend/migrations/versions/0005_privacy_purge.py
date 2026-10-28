"""Privacy purge (P9; R054, R055, D079).

Messages stay immutable for everyone, but the worker's *system-scope* purge may delete
them when an account deletion or the 12-month retention window requires it. UPDATE
remains rejected; DELETE is allowed only in system scope (app_system()).

Revision ID: 0005
Revises: 0004
"""

from collections.abc import Sequence

from alembic import op

revision: str = "0005"
down_revision: str | None = "0004"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

PURGE_FN = """
CREATE FUNCTION reject_update_allow_system_delete() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
  IF TG_OP = 'DELETE' AND app_system() THEN
    RETURN OLD;  -- privacy purge / retention expiry, system scope only
  END IF;
  RAISE EXCEPTION '% rows are immutable (% rejected)', TG_TABLE_NAME, TG_OP
    USING ERRCODE = 'insufficient_privilege';
END $$;
"""


def upgrade() -> None:
    op.execute(PURGE_FN)
    op.execute("DROP TRIGGER messages_immutable ON messages")
    op.execute(
        "CREATE TRIGGER messages_immutable BEFORE UPDATE OR DELETE ON messages"
        " FOR EACH ROW EXECUTE FUNCTION reject_update_allow_system_delete()"
    )
    op.execute("GRANT DELETE ON messages TO alloyiq_worker")


def downgrade() -> None:
    op.execute("REVOKE DELETE ON messages FROM alloyiq_worker")
    op.execute("DROP TRIGGER messages_immutable ON messages")
    op.execute(
        "CREATE TRIGGER messages_immutable BEFORE UPDATE OR DELETE ON messages"
        " FOR EACH ROW EXECUTE FUNCTION reject_mutation()"
    )
    op.execute("DROP FUNCTION reject_update_allow_system_delete()")

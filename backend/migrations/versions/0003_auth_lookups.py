"""SECURITY DEFINER lookups for pre-authentication flows (P2).

Before a session exists there is no app.user_id, so RLS hides every user row. These
functions, owned by alloyiq_migrator, are the only way the API can:
  - find a user by normalised email for login/registration/recovery,
  - resolve a session from its token hash,
  - find a verification/reset token by its hash.
Each returns the minimum columns needed and nothing for inactive rows. search_path is
pinned to prevent object-shadowing attacks.

Revision ID: 0003
Revises: 0002
"""

from collections.abc import Sequence

from alembic import op

revision: str = "0003"
down_revision: str | None = "0002"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

FUNCTIONS = """
CREATE FUNCTION auth_lookup_user(p_email text)
RETURNS TABLE (user_id uuid, account_id uuid, password_hash text, status text,
               account_status text, email_verified_at timestamptz, display_name text)
LANGUAGE sql STABLE SECURITY DEFINER SET search_path = public, pg_temp AS $$
  SELECT u.id, a.id, u.password_hash, u.status, a.status, u.email_verified_at, u.display_name
  FROM users u JOIN accounts a ON a.owner_user_id = u.id
  WHERE u.email = lower(btrim(p_email)) AND u.status <> 'deleted'
$$;

CREATE FUNCTION auth_resolve_session(p_token_hash bytea)
RETURNS TABLE (session_id uuid, user_id uuid, account_id uuid, role text,
               created_at timestamptz, last_seen_at timestamptz, idle_expires_at timestamptz,
               absolute_expires_at timestamptz, fresh_until timestamptz,
               user_status text, account_status text)
LANGUAGE sql STABLE SECURITY DEFINER SET search_path = public, pg_temp AS $$
  SELECT s.id, s.user_id, s.account_id, m.role, s.created_at, s.last_seen_at,
         s.idle_expires_at, s.absolute_expires_at, s.fresh_until, u.status, a.status
  FROM sessions s
  JOIN users u ON u.id = s.user_id
  JOIN accounts a ON a.id = s.account_id
  JOIN account_memberships m ON m.account_id = s.account_id AND m.user_id = s.user_id
  WHERE s.token_hash = p_token_hash
    AND s.revoked_at IS NULL
    AND s.idle_expires_at > now()
    AND s.absolute_expires_at > now()
$$;

CREATE FUNCTION auth_find_token(p_token_hash bytea, p_kind text)
RETURNS TABLE (token_id uuid, user_id uuid, account_id uuid, expires_at timestamptz,
               consumed_at timestamptz)
LANGUAGE sql STABLE SECURITY DEFINER SET search_path = public, pg_temp AS $$
  SELECT t.id, t.user_id, a.id, t.expires_at, t.consumed_at
  FROM auth_tokens t JOIN accounts a ON a.owner_user_id = t.user_id
  WHERE t.token_hash = p_token_hash AND t.kind = p_kind
$$;
"""

NAMES = (
    "auth_lookup_user(text)",
    "auth_resolve_session(bytea)",
    "auth_find_token(bytea, text)",
)


def upgrade() -> None:
    op.execute(FUNCTIONS)
    for fn in NAMES:
        op.execute(f"REVOKE ALL ON FUNCTION {fn} FROM PUBLIC")
        op.execute(f"GRANT EXECUTE ON FUNCTION {fn} TO alloyiq_api, alloyiq_worker")


def downgrade() -> None:
    for fn in NAMES:
        op.execute(f"DROP FUNCTION IF EXISTS {fn}")

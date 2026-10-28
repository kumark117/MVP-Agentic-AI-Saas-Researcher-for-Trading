"""Row-level security, role grants and integrity triggers.

- FORCE ROW LEVEL SECURITY on every account-owned table. Policies key on the
  transaction-local settings app.account_id / app.user_id (SET LOCAL by the unit of work).
- "System scope" bypasses account policies only for:
    * alloyiq_migrator (migrations, alloyiq-admin CLI, SECURITY DEFINER auth lookups), and
    * alloyiq_worker inside a transaction that explicitly sets app.system = 'on'
      (schedulers and sweepers). The API role can never enter system scope.
- Ledger: postings must balance per transaction and currency (deferred constraint
  trigger); journals and postings are immutable. Messages and event rows are immutable.
- Rate cards are immutable once active; items can change only while the card is a draft.

Revision ID: 0002
Revises: 0001
"""

from collections.abc import Sequence

from alembic import op

revision: str = "0002"
down_revision: str | None = "0001"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

API, WORKER, MIGRATOR = "alloyiq_api", "alloyiq_worker", "alloyiq_migrator"

CONFIG_TABLES = (
    "instruments",
    "provider_mappings",
    "model_profiles",
    "rate_cards",
    "rate_card_items",
    "plans",
    "evaluation_runs",
    "evaluation_scores",
    "provider_health",
)
# Tables keyed on user_id rather than account_id (no account exists yet at login time).
USER_TABLES = ("users", "auth_identities", "sessions", "auth_tokens")
# account_id may be NULL (pre-account or platform rows); NULL rows are system-only to read.
NULLABLE_ACCOUNT_TABLES = (
    "audit_events",
    "security_events",
    "email_outbox",
    "outbox",
    "incidents",
    "trace_outbox",
    "expense_reconciliation_items",
)
IMMUTABLE_TABLES = (
    "ledger_transactions",
    "ledger_postings",
    "messages",
    "reservation_events",
    "audit_events",
    "security_events",
)
APPEND_ONLY_EVENT_TABLES = ("run_events", "account_events")  # no UPDATE; DELETE for expiry purge

# Frozen copy of schema.ACCOUNT_OWNED_TABLES (minus sessions) at this revision. Tables added
# later get their policies in their own migration; tests/integration/test_schema.py checks
# that every table with account_id has FORCE RLS.
OWNED = (
    "account_event_cursors",
    "account_events",
    "account_memberships",
    "artifacts",
    "audit_events",
    "checkouts",
    "conversations",
    "deletion_requests",
    "email_outbox",
    "estimates",
    "evidence_items",
    "expense_reconciliation_items",
    "export_jobs",
    "feedback",
    "idempotency_records",
    "incidents",
    "job_attempts",
    "jobs",
    "ledger_postings",
    "ledger_transactions",
    "llm_calls",
    "messages",
    "outbox",
    "payment_events",
    "real_spend_reservations",
    "receipts",
    "refunds",
    "report_citations",
    "reports",
    "research_runs",
    "reservation_events",
    "reservations",
    "run_events",
    "run_nodes",
    "security_events",
    "tombstones",
    "tool_calls",
    "trace_outbox",
    "usage_records",
    "validation_results",
    "wallets",
)

HELPERS = f"""
CREATE FUNCTION app_account_id() RETURNS uuid LANGUAGE sql STABLE AS
$$ SELECT nullif(current_setting('app.account_id', true), '')::uuid $$;

CREATE FUNCTION app_user_id() RETURNS uuid LANGUAGE sql STABLE AS
$$ SELECT nullif(current_setting('app.user_id', true), '')::uuid $$;

CREATE FUNCTION app_system() RETURNS boolean LANGUAGE sql STABLE AS
$$ SELECT current_user = '{MIGRATOR}'
       OR (current_user = '{WORKER}' AND coalesce(current_setting('app.system', true), '') = 'on') $$;
"""

LEDGER_SQL = """
CREATE FUNCTION ledger_check_balanced() RETURNS trigger LANGUAGE plpgsql AS $$
DECLARE
  txn uuid;
  n integer;
  bad integer;
  wrong_currency integer;
BEGIN
  -- IF, not CASE: PL/pgSQL resolves every field referenced in one expression.
  IF TG_TABLE_NAME = 'ledger_postings' THEN
    txn := NEW.transaction_id;
  ELSE
    txn := NEW.id;
  END IF;
  SELECT count(*) INTO n FROM ledger_postings WHERE transaction_id = txn;
  IF n < 2 THEN
    RAISE EXCEPTION 'ledger transaction % has % postings; at least 2 required', txn, n
      USING ERRCODE = 'check_violation';
  END IF;
  SELECT count(*) INTO bad FROM (
    SELECT currency FROM ledger_postings WHERE transaction_id = txn
    GROUP BY currency HAVING sum(amount) <> 0) unbalanced;
  IF bad > 0 THEN
    RAISE EXCEPTION 'ledger transaction % does not balance', txn USING ERRCODE = 'check_violation';
  END IF;
  SELECT count(*) INTO wrong_currency FROM ledger_postings p JOIN ledger_transactions t
    ON t.id = p.transaction_id WHERE p.transaction_id = txn AND p.currency <> t.currency;
  IF wrong_currency > 0 THEN
    RAISE EXCEPTION 'ledger transaction % has postings in a different currency', txn
      USING ERRCODE = 'check_violation';
  END IF;
  RETURN NULL;
END $$;

CREATE CONSTRAINT TRIGGER ledger_postings_balanced
  AFTER INSERT ON ledger_postings DEFERRABLE INITIALLY DEFERRED
  FOR EACH ROW EXECUTE FUNCTION ledger_check_balanced();
CREATE CONSTRAINT TRIGGER ledger_transactions_have_postings
  AFTER INSERT ON ledger_transactions DEFERRABLE INITIALLY DEFERRED
  FOR EACH ROW EXECUTE FUNCTION ledger_check_balanced();
"""

IMMUTABLE_FN = """
CREATE FUNCTION reject_mutation() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
  RAISE EXCEPTION '% rows are immutable (% rejected)', TG_TABLE_NAME, TG_OP
    USING ERRCODE = 'insufficient_privilege';
END $$;
"""

RATE_CARD_SQL = """
CREATE FUNCTION rate_card_guard() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
  IF TG_OP = 'DELETE' THEN
    IF OLD.status <> 'draft' THEN RAISE EXCEPTION 'only draft rate cards can be deleted'; END IF;
    RETURN OLD;
  END IF;
  IF OLD.status = 'retired' THEN RAISE EXCEPTION 'retired rate cards are immutable'; END IF;
  IF OLD.status = 'active' AND (
       NEW.status NOT IN ('active', 'retired')
       OR NEW.version IS DISTINCT FROM OLD.version
       OR NEW.effective_from IS DISTINCT FROM OLD.effective_from
       OR NEW.demo_fx_usd_inr IS DISTINCT FROM OLD.demo_fx_usd_inr
       OR NEW.demo_markup IS DISTINCT FROM OLD.demo_markup
       OR NEW.fx_rule IS DISTINCT FROM OLD.fx_rule) THEN
    RAISE EXCEPTION 'active rate card % is immutable except retirement', OLD.version;
  END IF;
  RETURN NEW;
END $$;

CREATE TRIGGER rate_cards_guard BEFORE UPDATE OR DELETE ON rate_cards
  FOR EACH ROW EXECUTE FUNCTION rate_card_guard();

CREATE FUNCTION rate_card_item_guard() RETURNS trigger LANGUAGE plpgsql AS $$
DECLARE card_status text;
BEGIN
  SELECT status INTO card_status FROM rate_cards
    WHERE id = CASE TG_OP WHEN 'DELETE' THEN OLD.rate_card_id ELSE NEW.rate_card_id END;
  IF card_status <> 'draft' THEN
    RAISE EXCEPTION 'rate card items can change only while the card is a draft';
  END IF;
  RETURN CASE TG_OP WHEN 'DELETE' THEN OLD ELSE NEW END;
END $$;

CREATE TRIGGER rate_card_items_guard BEFORE INSERT OR UPDATE OR DELETE ON rate_card_items
  FOR EACH ROW EXECUTE FUNCTION rate_card_item_guard();
"""


def _policy(table: str, using: str, insert_check: str | None = None) -> str:
    sql = [
        f"ALTER TABLE {table} ENABLE ROW LEVEL SECURITY;",
        f"ALTER TABLE {table} FORCE ROW LEVEL SECURITY;",
    ]
    if insert_check is None:
        sql.append(f"CREATE POLICY isolation ON {table} USING ({using}) WITH CHECK ({using});")
    else:
        sql += [
            f"CREATE POLICY isolation_read ON {table} FOR SELECT USING ({using});",
            f"CREATE POLICY isolation_update ON {table} FOR UPDATE USING ({using}) "
            f"WITH CHECK ({using});",
            f"CREATE POLICY isolation_delete ON {table} FOR DELETE USING ({using});",
            f"CREATE POLICY isolation_insert ON {table} FOR INSERT WITH CHECK ({insert_check});",
        ]
    return "\n".join(sql)


def upgrade() -> None:
    op.execute(HELPERS)

    # ---- RLS policies
    by_account = "account_id = app_account_id() OR app_system()"
    for t in OWNED:
        if t in NULLABLE_ACCOUNT_TABLES:
            op.execute(_policy(t, by_account, insert_check=f"account_id IS NULL OR {by_account}"))
        else:
            op.execute(_policy(t, by_account))
    op.execute(_policy("accounts", "id = app_account_id() OR app_system()"))
    op.execute(_policy("users", "id = app_user_id() OR app_system()"))
    for t in ("auth_identities", "sessions", "auth_tokens"):
        op.execute(_policy(t, "user_id = app_user_id() OR app_system()"))
    op.execute(
        _policy(
            "real_budget_accounts",
            "scope = 'platform_day' OR scope_key = app_account_id()::text OR app_system()",
        )
    )

    # ---- grants (the API and worker roles own nothing)
    op.execute(f"GRANT USAGE ON SCHEMA public TO {API}, {WORKER}")
    op.execute(f"GRANT SELECT ON {', '.join(CONFIG_TABLES)} TO {API}, {WORKER}")
    data_tables = (*OWNED, "accounts", *USER_TABLES, "real_budget_accounts")
    op.execute(f"GRANT SELECT, INSERT, UPDATE ON {', '.join(data_tables)} TO {API}")
    op.execute(f"GRANT SELECT, INSERT, UPDATE, DELETE ON {', '.join(data_tables)} TO {WORKER}")
    frozen = (*IMMUTABLE_TABLES, *APPEND_ONLY_EVENT_TABLES)
    op.execute(f"REVOKE UPDATE ON {', '.join(frozen)} FROM {API}, {WORKER}")
    op.execute(f"REVOKE DELETE ON {', '.join(IMMUTABLE_TABLES)} FROM {WORKER}")
    op.execute(
        f"GRANT EXECUTE ON FUNCTION app_account_id(), app_user_id(), app_system() "
        f"TO {API}, {WORKER}"
    )

    # ---- integrity triggers
    op.execute(LEDGER_SQL)
    op.execute(IMMUTABLE_FN)
    for t in IMMUTABLE_TABLES:
        op.execute(
            f"CREATE TRIGGER {t}_immutable BEFORE UPDATE OR DELETE ON {t} "
            f"FOR EACH ROW EXECUTE FUNCTION reject_mutation()"
        )
    for t in APPEND_ONLY_EVENT_TABLES:
        op.execute(
            f"CREATE TRIGGER {t}_append_only BEFORE UPDATE ON {t} "
            f"FOR EACH ROW EXECUTE FUNCTION reject_mutation()"
        )
    op.execute(RATE_CARD_SQL)


def downgrade() -> None:
    op.execute("DROP TRIGGER IF EXISTS rate_card_items_guard ON rate_card_items")
    op.execute("DROP TRIGGER IF EXISTS rate_cards_guard ON rate_cards")
    op.execute("DROP FUNCTION IF EXISTS rate_card_item_guard(), rate_card_guard()")
    for t in APPEND_ONLY_EVENT_TABLES:
        op.execute(f"DROP TRIGGER IF EXISTS {t}_append_only ON {t}")
    for t in IMMUTABLE_TABLES:
        op.execute(f"DROP TRIGGER IF EXISTS {t}_immutable ON {t}")
    op.execute("DROP FUNCTION IF EXISTS reject_mutation()")
    op.execute("DROP TRIGGER IF EXISTS ledger_transactions_have_postings ON ledger_transactions")
    op.execute("DROP TRIGGER IF EXISTS ledger_postings_balanced ON ledger_postings")
    op.execute("DROP FUNCTION IF EXISTS ledger_check_balanced()")

    data_tables = (*OWNED, "accounts", *USER_TABLES, "real_budget_accounts")
    op.execute(f"REVOKE ALL ON {', '.join((*data_tables, *CONFIG_TABLES))} FROM {API}, {WORKER}")
    op.execute(f"REVOKE USAGE ON SCHEMA public FROM {API}, {WORKER}")
    for t in (*data_tables,):
        op.execute(f"DROP POLICY IF EXISTS isolation ON {t}")
        for suffix in ("read", "update", "delete", "insert"):
            op.execute(f"DROP POLICY IF EXISTS isolation_{suffix} ON {t}")
        op.execute(f"ALTER TABLE {t} NO FORCE ROW LEVEL SECURITY")
        op.execute(f"ALTER TABLE {t} DISABLE ROW LEVEL SECURITY")
    op.execute("DROP FUNCTION IF EXISTS app_system(), app_user_id(), app_account_id()")

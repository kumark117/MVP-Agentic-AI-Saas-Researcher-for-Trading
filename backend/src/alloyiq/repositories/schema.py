"""PostgreSQL schema as SQLAlchemy Core metadata (build prompt section 7; D075-D079, D111).

Conventions:
- UUID primary keys (gen_random_uuid()), TIMESTAMPTZ instants, NUMERIC(20,6) money,
  BIGINT token counts, typed status columns as CHECK constraints.
- JSONB only for validated payloads that may change shape; never money, identity,
  status or ownership.
- Every owned row carries account_id; RLS policies (migration 0002) key on it.
- Every table comment records its retention class: "retention=<class>; <description>".

Migrations are frozen files in migrations/versions; tests/integration/test_schema.py fails
if the migrated database drifts from this metadata.
"""

from __future__ import annotations

from typing import Any

import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import ARRAY, BYTEA, JSONB, UUID

NAMING = {
    "ix": "ix_%(table_name)s_%(column_0_N_name)s",
    "uq": "uq_%(table_name)s_%(column_0_N_name)s",
    "ck": "ck_%(table_name)s_%(constraint_name)s",
    "fk": "fk_%(table_name)s_%(column_0_name)s_%(referred_table_name)s",
    "pk": "pk_%(table_name)s",
}
metadata = sa.MetaData(naming_convention=NAMING)

# Retention classes (D078). Values are documented in docs/decisions.md ADR-011.
RETENTION_CLASSES = frozenset(
    {
        "identity",  # account lifetime; deleted/pseudonymised on account deletion
        "security_12m",  # security and audit records, 12 months online
        "auth_short",  # tokens and sessions; purged after expiry
        "config",  # catalogue, model, price and plan configuration
        "product_12m",  # reports, history, demo transactions: 12 months
        "activity_90d",  # detailed activity, tool calls, validation, evidence
        "sse_30d",  # replayable SSE events
        "ephemeral_7d",  # idempotency records, outbox
        "financial_policy",  # actual provider expense: platform accounting policy (OD-6)
        "platform",  # evaluations, provider health, incidents
    }
)

MONEY = sa.Numeric(20, 6)
TOKENS = sa.BigInteger


def _id() -> sa.Column[Any]:
    return sa.Column(
        "id", UUID(as_uuid=True), primary_key=True, server_default=sa.text("gen_random_uuid()")
    )


def _account(nullable: bool = False) -> sa.Column[Any]:
    return sa.Column(
        "account_id",
        UUID(as_uuid=True),
        sa.ForeignKey("accounts.id", ondelete="RESTRICT"),
        nullable=nullable,
    )


def _ts(name: str, *, nullable: bool = True, default_now: bool = False) -> sa.Column[Any]:
    return sa.Column(
        name,
        sa.DateTime(timezone=True),
        nullable=nullable,
        server_default=sa.text("now()") if default_now else None,
    )


def _created() -> sa.Column[Any]:
    return _ts("created_at", nullable=False, default_now=True)


def _fk(name: str, target: str, *, nullable: bool = False) -> sa.Column[Any]:
    return sa.Column(
        name, UUID(as_uuid=True), sa.ForeignKey(target, ondelete="RESTRICT"), nullable=nullable
    )


def _enum(name: str, values: tuple[str, ...]) -> sa.CheckConstraint:
    quoted = ", ".join(f"'{v}'" for v in values)
    return sa.CheckConstraint(f"{name} IN ({quoted})", name=f"{name}_enum")


def _table(name: str, retention: str, description: str, *items: Any) -> sa.Table:
    if retention not in RETENTION_CLASSES:
        raise ValueError(f"unknown retention class {retention!r} for {name}")
    return sa.Table(name, metadata, *items, comment=f"retention={retention}; {description}")


# ---------------------------------------------------------------- enums used in Python too
USER_STATUS = ("active", "suspended", "deleted")
ROLES = ("member", "platform_admin", "support", "finance")
AUTH_TOKEN_KINDS = ("verify", "reset")
DATA_MODES = ("exchange_live", "exchange_delayed", "reference")
INSTRUMENT_TYPES = ("spot", "futures_contract", "futures_continuous", "reference_series")
HORIZONS = ("intraday", "swing", "long_term")
RATE_CARD_STATUS = ("draft", "active", "retired")
TOKEN_CATEGORIES = ("input", "output", "cache_read", "cache_write")
RESERVATION_STATUS = ("active", "settled", "released", "pending")
LEDGER_KINDS = ("topup", "run_debit", "refund", "adjustment", "reversal", "opening_grant")
CHECKOUT_STATUS = ("created", "pending", "paid", "failed", "expired")
PAYMENT_EVENT_TYPES = (
    "checkout.pending",
    "checkout.paid",
    "checkout.failed",
    "checkout.expired",
    "refund.succeeded",
    "refund.failed",
)
RUN_STATUS = (
    "queued",
    "running",
    "cancel_requested",
    "succeeded",
    "incomplete",
    "failed",
    "cancelled",
    "timed_out",
)
TERMINAL_STATUS = ("succeeded", "incomplete", "failed", "cancelled", "timed_out")
BILLING_STATUS = ("reserved", "pending_settlement", "settled", "released", "disputed")
JOB_STATE = ("runnable", "leased", "done", "dead")
LLM_CALL_STATUS = ("intent", "in_flight", "completed", "failed", "ambiguous")
USAGE_STATUS = ("actual", "unknown", "estimated")
COST_STATUS = ("calculated", "reconciled", "unknown")
VERDICTS = ("pass", "fail", "quarantine")
EVIDENCE_KINDS = ("quote", "bars", "indicator", "fx", "news")
TRUST_CLASSES = ("untrusted_external", "validated_numeric", "application_generated")
SIGNALS = ("BUY", "SELL", "HOLD", "NO_SIGNAL")
EXPORT_KINDS = ("runs", "usage", "account_data")
EXPORT_FORMATS = ("csv", "json")
EXPORT_STATUS = ("queued", "running", "ready", "failed", "expired")


# ================================================================ identity and accounts
users = _table(
    "users",
    "identity",
    "member identities",
    _id(),
    sa.Column("email", sa.Text, nullable=False),  # normalised (lower-case, trimmed)
    _ts("email_verified_at"),
    sa.Column("password_hash", sa.Text),  # Argon2id; null for future OIDC-only users
    sa.Column("display_name", sa.Text, nullable=False),
    sa.Column("locale", sa.Text, nullable=False, server_default="en"),
    sa.Column("timezone", sa.Text, nullable=False, server_default="UTC"),
    sa.Column("default_model_profile_id", UUID(as_uuid=True)),
    sa.Column("status", sa.Text, nullable=False, server_default="active"),
    sa.Column("terms_version", sa.Text, nullable=False),
    sa.Column("privacy_version", sa.Text, nullable=False),
    _created(),
    _ts("updated_at", nullable=False, default_now=True),
    _ts("deleted_at"),
    sa.UniqueConstraint("email"),
    _enum("status", USER_STATUS),
    sa.CheckConstraint("email = lower(btrim(email))", name="email_normalised"),
)

auth_identities = _table(
    "auth_identities",
    "identity",
    "external identities (OIDC; deferred, DV-01)",
    _id(),
    _fk("user_id", "users.id"),
    sa.Column("issuer", sa.Text, nullable=False),
    sa.Column("subject", sa.Text, nullable=False),
    _created(),
    sa.UniqueConstraint("issuer", "subject"),
)

accounts = _table(
    "accounts",
    "identity",
    "personal accounts: the isolation boundary",
    _id(),
    _fk("owner_user_id", "users.id"),
    sa.Column("currency", sa.Text, nullable=False, server_default="INR"),
    sa.Column("status", sa.Text, nullable=False, server_default="active"),
    sa.Column("plan_id", UUID(as_uuid=True), sa.ForeignKey("plans.id", ondelete="RESTRICT")),
    _created(),
    sa.UniqueConstraint("owner_user_id"),  # version 1: one personal account per user
    sa.CheckConstraint("currency = 'INR'", name="currency_inr"),
    _enum("status", ("active", "suspended", "deleted")),
)

account_memberships = _table(
    "account_memberships",
    "identity",
    "user to account role membership",
    _id(),
    _account(),
    _fk("user_id", "users.id"),
    sa.Column("role", sa.Text, nullable=False),
    _created(),
    sa.UniqueConstraint("account_id", "user_id"),
    _enum("role", ROLES),
)

sessions = _table(
    "sessions",
    "auth_short",
    "server-backed sessions; only token hashes stored",
    _id(),
    sa.Column("token_hash", BYTEA, nullable=False),
    _fk("user_id", "users.id"),
    _account(),
    _created(),
    _ts("last_seen_at", nullable=False, default_now=True),
    _ts("idle_expires_at", nullable=False),
    _ts("absolute_expires_at", nullable=False),
    _ts("fresh_until"),  # re-authentication freshness (5 minutes)
    _ts("revoked_at"),
    sa.Column("revoked_reason", sa.Text),
    sa.Column("user_agent_summary", sa.Text),
    sa.Column("ip_prefix", sa.Text),
    sa.Column("csrf_secret", BYTEA, nullable=False),
    sa.UniqueConstraint("token_hash"),
    sa.Index("ix_sessions_active", "user_id", postgresql_where=sa.text("revoked_at IS NULL")),
)

auth_tokens = _table(
    "auth_tokens",
    "auth_short",
    "single-use verification and reset tokens (hashed)",
    _id(),
    _fk("user_id", "users.id"),
    sa.Column("kind", sa.Text, nullable=False),
    sa.Column("token_hash", BYTEA, nullable=False),
    _created(),
    _ts("expires_at", nullable=False),
    _ts("consumed_at"),
    sa.UniqueConstraint("token_hash"),
    _enum("kind", AUTH_TOKEN_KINDS),
)

email_outbox = _table(
    "email_outbox",
    "auth_short",
    "outgoing email queue; delivered by the worker",
    _id(),
    _account(nullable=True),
    sa.Column("user_id", UUID(as_uuid=True), sa.ForeignKey("users.id", ondelete="RESTRICT")),
    sa.Column("to_email", sa.Text, nullable=False),
    sa.Column("template", sa.Text, nullable=False),
    sa.Column("payload", JSONB, nullable=False),  # contains one-time links; purged after send
    _created(),
    _ts("sent_at"),
    sa.Column("attempts", sa.Integer, nullable=False, server_default="0"),
    sa.Column("last_error", sa.Text),
    sa.Index("ix_email_outbox_unsent", "created_at", postgresql_where=sa.text("sent_at IS NULL")),
)

audit_events = _table(
    "audit_events",
    "security_12m",
    "security, lifecycle, budget and ledger audit trail",
    _id(),
    _account(nullable=True),
    sa.Column("actor_user_id", UUID(as_uuid=True)),
    sa.Column("actor_kind", sa.Text, nullable=False),
    sa.Column("action", sa.Text, nullable=False),
    sa.Column("target_type", sa.Text),
    sa.Column("target_id", sa.Text),
    sa.Column("request_id", sa.Text),
    sa.Column("run_id", UUID(as_uuid=True)),
    sa.Column("outcome", sa.Text, nullable=False),
    sa.Column("metadata", JSONB, nullable=False, server_default=sa.text("'{}'::jsonb")),
    _ts("occurred_at", nullable=False, default_now=True),
    _enum("actor_kind", ("user", "system", "admin_cli")),
    sa.Index("ix_audit_events_account_time", "account_id", "occurred_at"),
)

security_events = _table(
    "security_events",
    "security_12m",
    "security detections (injection, abuse, revocation)",
    _id(),
    _account(nullable=True),
    sa.Column("kind", sa.Text, nullable=False),
    sa.Column("severity", sa.Text, nullable=False),
    sa.Column("request_id", sa.Text),
    sa.Column("run_id", UUID(as_uuid=True)),
    sa.Column("subject_id", sa.Text),
    sa.Column("metadata", JSONB, nullable=False, server_default=sa.text("'{}'::jsonb")),
    _ts("occurred_at", nullable=False, default_now=True),
    _enum("severity", ("info", "low", "medium", "high", "critical")),
)

# ================================================================ catalogue and pricing
instruments = _table(
    "instruments",
    "config",
    "instrument catalogue (D004-D006)",
    _id(),
    sa.Column("canonical_id", sa.Text, nullable=False),
    sa.Column("display_name", sa.Text, nullable=False),
    sa.Column("metal", sa.Text, nullable=False),
    sa.Column("venue", sa.Text, nullable=False),
    sa.Column("data_mode", sa.Text, nullable=False),
    sa.Column("instrument_type", sa.Text, nullable=False),
    sa.Column("contract_expiry", sa.Date),
    sa.Column("quote_currency", sa.Text, nullable=False),
    sa.Column("unit", sa.Text, nullable=False),
    sa.Column("timezone", sa.Text, nullable=False),
    sa.Column("calendar", sa.Text, nullable=False),
    sa.Column("supported_horizons", ARRAY(sa.Text), nullable=False),
    sa.Column("freshness_policy", JSONB, nullable=False),
    sa.Column("enabled", sa.Boolean, nullable=False, server_default=sa.false()),
    sa.Column("unavailable_reason", sa.Text),
    sa.Column("verification_evidence", JSONB),
    _created(),
    _ts("updated_at", nullable=False, default_now=True),
    sa.UniqueConstraint("canonical_id"),
    _enum("data_mode", DATA_MODES),
    _enum("instrument_type", INSTRUMENT_TYPES),
    sa.CheckConstraint("enabled OR unavailable_reason IS NOT NULL", name="disabled_has_reason"),
)

provider_mappings = _table(
    "provider_mappings",
    "config",
    "licensed provider symbol per instrument",
    _id(),
    _fk("instrument_id", "instruments.id"),
    sa.Column("provider", sa.Text, nullable=False),
    sa.Column("provider_symbol", sa.Text, nullable=False),
    sa.Column("function_or_interval", sa.Text, nullable=False, server_default=""),
    sa.Column("entitlement", sa.Text, nullable=False),
    _ts("verified_at"),
    _created(),
    sa.UniqueConstraint("instrument_id", "provider", "function_or_interval"),
)

model_profiles = _table(
    "model_profiles",
    "config",
    "approved model profiles (D019, D096)",
    _id(),
    sa.Column("slug", sa.Text, nullable=False),
    sa.Column("display_name", sa.Text, nullable=False),
    sa.Column("provider", sa.Text, nullable=False, server_default="anthropic"),
    sa.Column("primary_model_id", sa.Text, nullable=False),
    sa.Column(
        "approved_fallback_model_ids",
        ARRAY(sa.Text),
        nullable=False,
        server_default=sa.text("'{}'::text[]"),
    ),
    sa.Column("branch_effort", sa.Text, nullable=False),
    sa.Column("signal_effort", sa.Text, nullable=False),
    sa.Column("max_tokens_branch", sa.Integer, nullable=False),
    sa.Column("max_tokens_signal", sa.Integer, nullable=False),
    sa.Column("max_tokens_repair", sa.Integer, nullable=False),
    sa.Column("run_input_token_budget", TOKENS, nullable=False),
    sa.Column("run_output_token_budget", TOKENS, nullable=False),
    sa.Column("context_limit_tokens", TOKENS, nullable=False),
    sa.Column("enabled", sa.Boolean, nullable=False, server_default=sa.true()),
    sa.Column("is_default", sa.Boolean, nullable=False, server_default=sa.false()),
    _created(),
    sa.UniqueConstraint("slug"),
    _enum("branch_effort", ("low", "medium", "high")),
    _enum("signal_effort", ("low", "medium", "high")),
)

rate_cards = _table(
    "rate_cards",
    "config",
    "versioned rate cards; immutable once active (D056)",
    _id(),
    sa.Column("version", sa.Text, nullable=False),
    sa.Column("status", sa.Text, nullable=False, server_default="draft"),
    _ts("effective_from"),
    _ts("effective_to"),
    sa.Column("demo_fx_usd_inr", MONEY, nullable=False),
    sa.Column("demo_markup", MONEY, nullable=False),
    sa.Column("fx_rule", sa.Text, nullable=False),
    sa.Column("source_note", sa.Text, nullable=False),
    _ts("activated_at"),
    _created(),
    sa.UniqueConstraint("version"),
    _enum("status", RATE_CARD_STATUS),
)

rate_card_items = _table(
    "rate_card_items",
    "config",
    "per-model token-category rates; null price = unverified",
    _id(),
    _fk("rate_card_id", "rate_cards.id"),
    sa.Column("model_id", sa.Text, nullable=False),
    sa.Column("token_category", sa.Text, nullable=False),
    sa.Column("provider_usd_per_mtok", MONEY),  # null: not verified -> cost_status unknown
    sa.Column("verified", sa.Boolean, nullable=False, server_default=sa.false()),
    sa.Column("source_note", sa.Text, nullable=False),
    sa.UniqueConstraint("rate_card_id", "model_id", "token_category"),
    _enum("token_category", TOKEN_CATEGORIES),
    sa.CheckConstraint(
        "NOT verified OR provider_usd_per_mtok IS NOT NULL", name="verified_has_price"
    ),
)

plans = _table(
    "plans",
    "config",
    "demo plans and limits (subscriptions deferred, DV-04)",
    _id(),
    sa.Column("slug", sa.Text, nullable=False),
    sa.Column("display_name", sa.Text, nullable=False),
    sa.Column("active_runs", sa.Integer, nullable=False),
    sa.Column("queued_runs", sa.Integer, nullable=False),
    sa.Column("sse_streams", sa.Integer, nullable=False),
    sa.Column("daily_topup_limit_inr", MONEY, nullable=False),
    sa.Column("is_default", sa.Boolean, nullable=False, server_default=sa.false()),
    _created(),
    sa.UniqueConstraint("slug"),
)

# ================================================================ demo accounting
wallets = _table(
    "wallets",
    "product_12m",
    "demo wallet projection (rebuildable from postings)",
    _id(),
    _account(),
    sa.Column("currency", sa.Text, nullable=False, server_default="INR"),
    sa.Column("posted_credit", MONEY, nullable=False, server_default="0"),
    sa.Column("posted_debit", MONEY, nullable=False, server_default="0"),
    sa.Column("reserved", MONEY, nullable=False, server_default="0"),
    sa.Column("pending_settlement", MONEY, nullable=False, server_default="0"),
    sa.Column("payment_mode", sa.Text, nullable=False, server_default="simulated"),
    _ts("updated_at", nullable=False, default_now=True),
    sa.UniqueConstraint("account_id"),
    sa.CheckConstraint("payment_mode = 'simulated'", name="simulated"),
    sa.CheckConstraint(
        "posted_credit - posted_debit - reserved - pending_settlement >= 0",
        name="available_non_negative",
    ),
    sa.CheckConstraint("reserved >= 0 AND pending_settlement >= 0", name="holds_non_negative"),
)

research_runs = _table(
    "research_runs",
    "product_12m",
    "research runs: lifecycle, timing, versions, billing state",
    _id(),
    _account(),
    _fk("user_id", "users.id"),
    sa.Column(
        "conversation_id",
        UUID(as_uuid=True),
        sa.ForeignKey("conversations.id", ondelete="RESTRICT"),
    ),
    _fk("estimate_id", "estimates.id"),
    _fk("instrument_id", "instruments.id"),
    sa.Column("instrument_snapshot", JSONB, nullable=False),
    sa.Column("horizon", sa.Text, nullable=False),
    _fk("model_profile_id", "model_profiles.id"),
    sa.Column("question_message_id", UUID(as_uuid=True)),
    sa.Column("status", sa.Text, nullable=False, server_default="queued"),
    sa.Column("billing_status", sa.Text, nullable=False, server_default="reserved"),
    sa.Column("generation", sa.Integer, nullable=False, server_default="0"),
    _ts("submitted_at", nullable=False, default_now=True),
    _ts("started_at"),
    _ts("report_ready_at"),
    _ts("cancel_requested_at"),
    sa.Column("cancel_reason", sa.Text),
    _ts("terminal_at"),
    sa.Column("time_to_first_activity_ms", sa.BigInteger),
    sa.Column("time_to_first_model_token_ms", sa.BigInteger),
    sa.Column("cancel_stop_calls_ms", sa.BigInteger),
    sa.Column("cancel_stop_branches_ms", sa.BigInteger),
    sa.Column("outcome_reason", sa.Text),
    sa.Column("error_code", sa.Text),
    sa.Column("versions", JSONB, nullable=False),
    sa.Column("limits", JSONB, nullable=False),
    sa.Column("currency", sa.Text, nullable=False, server_default="INR"),
    sa.Column("authorized_max_charge", MONEY, nullable=False),
    sa.Column("reserved_amount", MONEY, nullable=False),
    sa.Column("settled_charge", MONEY),
    sa.Column("pending_amount", MONEY, nullable=False, server_default="0"),
    sa.Column("released_amount", MONEY, nullable=False, server_default="0"),
    sa.Column("latest_event_seq", sa.BigInteger, nullable=False, server_default="0"),
    sa.Column("observability_gap", sa.Boolean, nullable=False, server_default=sa.false()),
    sa.UniqueConstraint("estimate_id"),
    _enum("status", RUN_STATUS),
    _enum("billing_status", BILLING_STATUS),
    _enum("horizon", HORIZONS),
    sa.CheckConstraint(
        "(status IN ('succeeded','incomplete','failed','cancelled','timed_out'))"
        " = (terminal_at IS NOT NULL)",
        name="terminal_iff_terminal_at",
    ),
    sa.Index("ix_research_runs_account_submitted", "account_id", "submitted_at"),
    sa.Index("ix_research_runs_account_terminal", "account_id", "terminal_at"),
    sa.Index(
        "ix_research_runs_active",
        "account_id",
        postgresql_where=sa.text("status IN ('queued','running','cancel_requested')"),
    ),
)

reservations = _table(
    "reservations",
    "product_12m",
    "demo credit holds per run",
    _id(),
    _account(),
    _fk("wallet_id", "wallets.id"),
    _fk("run_id", "research_runs.id"),
    sa.Column("amount", MONEY, nullable=False),
    sa.Column("settled_amount", MONEY, nullable=False, server_default="0"),
    sa.Column("released_amount", MONEY, nullable=False, server_default="0"),
    sa.Column("pending_amount", MONEY, nullable=False, server_default="0"),
    sa.Column("status", sa.Text, nullable=False, server_default="active"),
    _ts("expires_at"),  # pending holds release after 24 h (R043)
    _created(),
    _ts("updated_at", nullable=False, default_now=True),
    sa.UniqueConstraint("run_id"),
    _enum("status", RESERVATION_STATUS),
    sa.CheckConstraint("amount > 0", name="amount_positive"),
)

reservation_events = _table(
    "reservation_events",
    "product_12m",
    "immutable hold transition history",
    _id(),
    _account(),
    _fk("reservation_id", "reservations.id"),
    sa.Column("kind", sa.Text, nullable=False),
    sa.Column("amount", MONEY, nullable=False),
    sa.Column("metadata", JSONB, nullable=False, server_default=sa.text("'{}'::jsonb")),
    _ts("occurred_at", nullable=False, default_now=True),
    _enum("kind", ("created", "settled", "released", "pending", "pending_released")),
)

ledger_transactions = _table(
    "ledger_transactions",
    "product_12m",
    "balanced demo journal headers (D063); immutable",
    _id(),
    _account(),
    sa.Column("business_event_id", sa.Text, nullable=False),
    sa.Column("kind", sa.Text, nullable=False),
    sa.Column("payment_mode", sa.Text, nullable=False, server_default="simulated"),
    sa.Column("currency", sa.Text, nullable=False, server_default="INR"),
    sa.Column(
        "reverses_id",
        UUID(as_uuid=True),
        sa.ForeignKey("ledger_transactions.id", ondelete="RESTRICT"),
    ),
    sa.Column("run_id", UUID(as_uuid=True)),
    sa.Column("checkout_id", UUID(as_uuid=True)),
    sa.Column("refund_id", UUID(as_uuid=True)),
    sa.Column("rate_card_version", sa.Text),
    sa.Column("rounding_adjustment", MONEY, nullable=False, server_default="0"),
    sa.Column("description", sa.Text, nullable=False),
    sa.Column("operator_reason", sa.Text),
    _ts("posted_at", nullable=False, default_now=True),
    sa.UniqueConstraint("business_event_id"),
    _enum("kind", LEDGER_KINDS),
    sa.CheckConstraint("payment_mode = 'simulated'", name="simulated"),
    sa.Index("ix_ledger_transactions_account_posted", "account_id", "posted_at"),
)

ledger_postings = _table(
    "ledger_postings",
    "product_12m",
    "signed postings; sum to zero per txn+currency; immutable",
    _id(),
    _fk("transaction_id", "ledger_transactions.id"),
    _account(),
    sa.Column("ledger_account", sa.Text, nullable=False),
    sa.Column("amount", MONEY, nullable=False),
    sa.Column("currency", sa.Text, nullable=False),
    _created(),
    sa.CheckConstraint("amount <> 0", name="amount_non_zero"),
    sa.CheckConstraint(
        "ledger_account ~ '^wallet:[0-9a-f-]{36}$' OR ledger_account IN "
        "('sim_topup_source','sim_usage_income','sim_refunds','sim_adjustments')",
        name="known_ledger_account",
    ),
    sa.Index("ix_ledger_postings_transaction", "transaction_id"),
    sa.Index("ix_ledger_postings_account_ledger", "account_id", "ledger_account"),
)

checkouts = _table(
    "checkouts",
    "product_12m",
    "simulated top-up checkouts (D054, D114)",
    _id(),
    _account(),
    _fk("user_id", "users.id"),
    sa.Column("amount", MONEY, nullable=False),
    sa.Column("currency", sa.Text, nullable=False, server_default="INR"),
    sa.Column("status", sa.Text, nullable=False, server_default="created"),
    sa.Column("payment_mode", sa.Text, nullable=False, server_default="simulated"),
    sa.Column("return_path", sa.Text, nullable=False),
    _ts("expires_at", nullable=False),
    _created(),
    _ts("updated_at", nullable=False, default_now=True),
    _ts("completed_at"),
    _enum("status", CHECKOUT_STATUS),
    sa.CheckConstraint("payment_mode = 'simulated'", name="simulated"),
    sa.CheckConstraint("amount > 0", name="amount_positive"),
)

refunds = _table(
    "refunds",
    "product_12m",
    "simulated refunds; reverse demo credit only",
    _id(),
    _account(),
    _fk("user_id", "users.id"),
    _fk("checkout_id", "checkouts.id"),
    sa.Column("amount", MONEY, nullable=False),
    sa.Column("currency", sa.Text, nullable=False, server_default="INR"),
    sa.Column("reason", sa.Text, nullable=False),
    sa.Column("status", sa.Text, nullable=False, server_default="requested"),
    sa.Column("payment_mode", sa.Text, nullable=False, server_default="simulated"),
    sa.Column("ledger_transaction_id", UUID(as_uuid=True)),
    _created(),
    _ts("completed_at"),
    _enum("status", ("requested", "succeeded", "rejected")),
    sa.CheckConstraint("payment_mode = 'simulated'", name="simulated"),
    sa.CheckConstraint("amount > 0", name="amount_positive"),
)

payment_events = _table(
    "payment_events",
    "product_12m",
    "trusted dummy payment notifications (D064)",
    _id(),
    _account(),
    sa.Column("event_id", sa.Text, nullable=False),
    sa.Column(
        "checkout_id", UUID(as_uuid=True), sa.ForeignKey("checkouts.id", ondelete="RESTRICT")
    ),
    sa.Column("refund_id", UUID(as_uuid=True), sa.ForeignKey("refunds.id", ondelete="RESTRICT")),
    sa.Column("event_type", sa.Text, nullable=False),
    sa.Column("sequence", sa.Integer, nullable=False),
    sa.Column("payment_mode", sa.Text, nullable=False, server_default="simulated"),
    sa.Column("trusted_origin", sa.Text, nullable=False),
    sa.Column("payload", JSONB, nullable=False, server_default=sa.text("'{}'::jsonb")),
    _ts("received_at", nullable=False, default_now=True),
    _ts("processed_at"),
    sa.Column("processing_status", sa.Text, nullable=False, server_default="pending"),
    sa.Column("retry_count", sa.Integer, nullable=False, server_default="0"),
    sa.UniqueConstraint("event_id"),
    _enum("event_type", PAYMENT_EVENT_TYPES),
    _enum("processing_status", ("pending", "processed", "ignored", "failed")),
    sa.CheckConstraint("payment_mode = 'simulated'", name="simulated"),
    sa.Index(
        "ix_payment_events_pending",
        "received_at",
        postgresql_where=sa.text("processing_status = 'pending'"),
    ),
)

receipts = _table(
    "receipts",
    "product_12m",
    "sample receipts: Simulation - not a tax invoice",
    _id(),
    _account(),
    sa.Column(
        "checkout_id", UUID(as_uuid=True), sa.ForeignKey("checkouts.id", ondelete="RESTRICT")
    ),
    sa.Column("refund_id", UUID(as_uuid=True), sa.ForeignKey("refunds.id", ondelete="RESTRICT")),
    sa.Column("kind", sa.Text, nullable=False),
    sa.Column("amount", MONEY, nullable=False),
    sa.Column("currency", sa.Text, nullable=False, server_default="INR"),
    sa.Column("sample", sa.Boolean, nullable=False, server_default=sa.true()),
    sa.Column("payment_mode", sa.Text, nullable=False, server_default="simulated"),
    sa.Column("ledger_transaction_id", UUID(as_uuid=True), nullable=False),
    _ts("issued_at", nullable=False, default_now=True),
    sa.UniqueConstraint("ledger_transaction_id"),
    _enum("kind", ("topup", "refund")),
    sa.CheckConstraint("sample", name="always_sample"),
    sa.CheckConstraint("payment_mode = 'simulated'", name="simulated"),
)

# ================================================================ real spend (USD, real)
real_budget_accounts = _table(
    "real_budget_accounts",
    "financial_policy",
    "real provider-spend caps per UTC day (D060)",
    _id(),
    sa.Column("scope", sa.Text, nullable=False),
    sa.Column("scope_key", sa.Text, nullable=False),  # 'platform' or account UUID
    sa.Column("period", sa.Date, nullable=False),
    sa.Column("limit_usd", MONEY, nullable=False),
    sa.Column("spent_usd", MONEY, nullable=False, server_default="0"),
    sa.Column("reserved_usd", MONEY, nullable=False, server_default="0"),
    _ts("updated_at", nullable=False, default_now=True),
    sa.UniqueConstraint("scope", "scope_key", "period"),
    _enum("scope", ("platform_day", "account_day")),
    sa.CheckConstraint("spent_usd >= 0 AND reserved_usd >= 0", name="non_negative"),
)

real_spend_reservations = _table(
    "real_spend_reservations",
    "financial_policy",
    "worst-case real spend held per run",
    _id(),
    _account(),
    _fk("run_id", "research_runs.id"),
    sa.Column("period", sa.Date, nullable=False),
    sa.Column("amount_usd", MONEY, nullable=False),
    sa.Column("spent_usd", MONEY, nullable=False, server_default="0"),
    sa.Column("released_usd", MONEY, nullable=False, server_default="0"),
    sa.Column("status", sa.Text, nullable=False, server_default="active"),
    _created(),
    _ts("updated_at", nullable=False, default_now=True),
    sa.UniqueConstraint("run_id"),
    _enum("status", RESERVATION_STATUS),
)

expense_reconciliation_items = _table(
    "expense_reconciliation_items",
    "financial_policy",
    "provider-invoice reconciliation (skeleton; DV-07)",
    _id(),
    _account(nullable=True),
    sa.Column("llm_call_id", UUID(as_uuid=True)),
    sa.Column("kind", sa.Text, nullable=False),
    sa.Column("status", sa.Text, nullable=False, server_default="open"),
    sa.Column("provider_amount_usd", MONEY),
    sa.Column("calculated_amount_usd", MONEY),
    sa.Column("notes", sa.Text),
    _created(),
    _ts("resolved_at"),
    _enum("status", ("open", "matched", "absorbed", "disputed")),
)

# ================================================================ research
estimates = _table(
    "estimates",
    "product_12m",
    "immutable request snapshots with price ceiling (D059)",
    _id(),
    _account(),
    _fk("user_id", "users.id"),
    sa.Column("normalized_request", JSONB, nullable=False),
    sa.Column("request_fingerprint", sa.Text, nullable=False),
    _fk("instrument_id", "instruments.id"),
    sa.Column("horizon", sa.Text, nullable=False),
    _fk("model_profile_id", "model_profiles.id"),
    sa.Column("approved_models", ARRAY(sa.Text), nullable=False),
    sa.Column("limits", JSONB, nullable=False),
    sa.Column("rate_card_version", sa.Text, nullable=False),
    sa.Column("currency", sa.Text, nullable=False, server_default="INR"),
    sa.Column("min_charge", MONEY, nullable=False),
    sa.Column("estimated_charge", MONEY, nullable=False),
    sa.Column("max_charge", MONEY, nullable=False),
    sa.Column("max_real_usd", MONEY, nullable=False),
    sa.Column("question", sa.Text),
    sa.Column(
        "conversation_id",
        UUID(as_uuid=True),
        sa.ForeignKey("conversations.id", ondelete="RESTRICT"),
    ),
    sa.Column("message_cutoff_id", UUID(as_uuid=True)),
    _ts("expires_at", nullable=False),
    sa.Column("consumed_by_run_id", UUID(as_uuid=True)),
    _ts("consumed_at"),
    _created(),
    _enum("horizon", HORIZONS),
    sa.CheckConstraint("question IS NULL OR char_length(question) <= 4000", name="question_len"),
    sa.CheckConstraint(
        "min_charge <= estimated_charge AND estimated_charge <= max_charge", name="charge_order"
    ),
)

idempotency_records = _table(
    "idempotency_records",
    "ephemeral_7d",
    "Idempotency-Key outcomes, kept >= 7 days (D083)",
    _id(),
    _account(),
    sa.Column("method", sa.Text, nullable=False),
    sa.Column("route", sa.Text, nullable=False),
    sa.Column("key", UUID(as_uuid=True), nullable=False),
    sa.Column("body_hash", sa.Text, nullable=False),
    sa.Column("response_status", sa.Integer),
    sa.Column("response_body", JSONB),
    sa.Column("resource_id", UUID(as_uuid=True)),
    _created(),
    _ts("expires_at", nullable=False),
    sa.UniqueConstraint("account_id", "method", "route", "key"),
)

conversations = _table(
    "conversations",
    "product_12m",
    "interaction history containers",
    _id(),
    _account(),
    _fk("user_id", "users.id"),
    sa.Column("title", sa.Text),
    _created(),
    _ts("updated_at", nullable=False, default_now=True),
    sa.CheckConstraint("title IS NULL OR char_length(title) <= 200", name="title_len"),
    sa.Index("ix_conversations_account_updated", "account_id", "updated_at"),
)

messages = _table(
    "messages",
    "product_12m",
    "immutable conversation messages (<= 4000 chars)",
    _id(),
    _account(),
    _fk("conversation_id", "conversations.id"),
    sa.Column("user_id", UUID(as_uuid=True), sa.ForeignKey("users.id", ondelete="RESTRICT")),
    sa.Column("role", sa.Text, nullable=False),
    sa.Column("content", sa.Text, nullable=False),
    sa.Column("client_message_id", UUID(as_uuid=True)),
    sa.Column("run_id", UUID(as_uuid=True)),
    _created(),
    sa.UniqueConstraint("conversation_id", "client_message_id"),
    _enum("role", ("user", "assistant", "system")),
    sa.CheckConstraint("char_length(content) BETWEEN 1 AND 4000", name="content_len"),
    sa.Index("ix_messages_conversation_created", "conversation_id", "created_at"),
)

feedback = _table(
    "feedback",
    "product_12m",
    "one rating per user and run",
    _id(),
    _account(),
    _fk("run_id", "research_runs.id"),
    _fk("user_id", "users.id"),
    sa.Column("rating", sa.Text, nullable=False),
    sa.Column("comment", sa.Text),
    _created(),
    _ts("updated_at", nullable=False, default_now=True),
    sa.UniqueConstraint("run_id", "user_id"),
    _enum("rating", ("positive", "negative")),
    sa.CheckConstraint("comment IS NULL OR char_length(comment) <= 2000", name="comment_len"),
)

run_nodes = _table(
    "run_nodes",
    "activity_90d",
    "graph node attempts",
    _id(),
    _account(),
    _fk("run_id", "research_runs.id"),
    sa.Column("node_id", sa.Text, nullable=False),
    sa.Column("status", sa.Text, nullable=False),
    sa.Column("attempt", sa.Integer, nullable=False, server_default="1"),
    sa.Column("generation", sa.Integer, nullable=False),
    _ts("started_at", nullable=False, default_now=True),
    _ts("ended_at"),
    sa.Column("output_ref", sa.Text),
    sa.Column("error_code", sa.Text),
    sa.UniqueConstraint("run_id", "node_id", "attempt"),
    _enum("status", ("running", "completed", "failed", "skipped", "cancelled")),
)

jobs = _table(
    "jobs",
    "ephemeral_7d",
    "durable execution intent with leases and fencing (D026, D053)",
    _id(),
    _account(),
    _fk("run_id", "research_runs.id"),
    sa.Column("state", sa.Text, nullable=False, server_default="runnable"),
    sa.Column("priority", sa.Integer, nullable=False, server_default="0"),
    _ts("available_at", nullable=False, default_now=True),
    sa.Column("lease_owner", sa.Text),
    _ts("lease_expires_at"),
    sa.Column("generation", sa.Integer, nullable=False, server_default="0"),
    sa.Column("attempts", sa.Integer, nullable=False, server_default="0"),
    sa.Column("last_error", sa.Text),
    _created(),
    _ts("updated_at", nullable=False, default_now=True),
    sa.UniqueConstraint("run_id"),
    _enum("state", JOB_STATE),
    sa.Index("ix_jobs_runnable", "available_at", postgresql_where=sa.text("state = 'runnable'")),
    sa.Index("ix_jobs_leased", "lease_expires_at", postgresql_where=sa.text("state = 'leased'")),
)

job_attempts = _table(
    "job_attempts",
    "activity_90d",
    "job claim history",
    _id(),
    _account(),
    _fk("job_id", "jobs.id"),
    sa.Column("generation", sa.Integer, nullable=False),
    sa.Column("worker_id", sa.Text, nullable=False),
    _ts("started_at", nullable=False, default_now=True),
    _ts("ended_at"),
    sa.Column("outcome", sa.Text),
    sa.Column("error", sa.Text),
)

outbox = _table(
    "outbox",
    "ephemeral_7d",
    "transactional outbox for notifications",
    _id(),
    _account(nullable=True),
    sa.Column("topic", sa.Text, nullable=False),
    sa.Column("payload", JSONB, nullable=False),
    _created(),
    _ts("published_at"),
    sa.Index(
        "ix_outbox_unpublished", "created_at", postgresql_where=sa.text("published_at IS NULL")
    ),
)

incidents = _table(
    "incidents",
    "platform",
    "operator-visible incidents (dead jobs, ledger mismatch)",
    _id(),
    _account(nullable=True),
    sa.Column("kind", sa.Text, nullable=False),
    sa.Column("run_id", UUID(as_uuid=True)),
    sa.Column("details", JSONB, nullable=False, server_default=sa.text("'{}'::jsonb")),
    _created(),
    _ts("resolved_at"),
)

llm_calls = _table(
    "llm_calls",
    "financial_policy",
    "model call intents and outcomes (D038, D061)",
    _id(),
    _account(),
    _fk("run_id", "research_runs.id"),
    sa.Column("node_id", sa.Text, nullable=False),
    sa.Column("attempt", sa.Integer, nullable=False),
    sa.Column("generation", sa.Integer, nullable=False),
    sa.Column("call_key", sa.Text, nullable=False),
    sa.Column("purpose", sa.Text, nullable=False),
    sa.Column("status", sa.Text, nullable=False, server_default="intent"),
    sa.Column("request_fingerprint", sa.Text, nullable=False),
    sa.Column("model_requested", sa.Text, nullable=False),
    sa.Column("model_used", sa.Text),
    sa.Column("effort", sa.Text),
    sa.Column("max_tokens", sa.Integer, nullable=False),
    sa.Column("provider_request_id", sa.Text),
    sa.Column("provider_message_id", sa.Text),
    sa.Column("stop_reason", sa.Text),
    sa.Column("refusal_category", sa.Text),
    sa.Column("error_code", sa.Text),
    _ts("started_at", nullable=False, default_now=True),
    _ts("ended_at"),
    sa.Column("duration_ms", sa.BigInteger),
    sa.Column("time_to_first_token_ms", sa.BigInteger),
    sa.Column("billable", sa.Boolean, nullable=False, server_default=sa.true()),
    sa.Column("nonbillable_reason", sa.Text),
    sa.UniqueConstraint("call_key"),
    _enum("status", LLM_CALL_STATUS),
    _enum("purpose", ("branch", "signal", "repair", "judge")),
    sa.Index("ix_llm_calls_run", "run_id"),
)

usage_records = _table(
    "usage_records",
    "financial_policy",
    "measured token usage and calculated cost",
    _id(),
    _account(),
    _fk("run_id", "research_runs.id"),
    _fk("llm_call_id", "llm_calls.id"),
    sa.Column("usage_status", sa.Text, nullable=False),
    sa.Column("input_tokens", TOKENS),  # null = unknown, never zero-filled
    sa.Column("output_tokens", TOKENS),
    sa.Column("cache_read_input_tokens", TOKENS),
    sa.Column("cache_creation_input_tokens", TOKENS),
    sa.Column("rate_card_version", sa.Text, nullable=False),
    sa.Column("provider_cost_usd", MONEY),
    sa.Column("cost_status", sa.Text, nullable=False),
    sa.Column("customer_charge_inr_simulated", MONEY),
    sa.Column("billable", sa.Boolean, nullable=False),
    _created(),
    sa.UniqueConstraint("llm_call_id"),
    _enum("usage_status", USAGE_STATUS),
    _enum("cost_status", COST_STATUS),
    sa.CheckConstraint(
        "usage_status <> 'actual' OR (input_tokens IS NOT NULL AND output_tokens IS NOT NULL)",
        name="actual_has_tokens",
    ),
    sa.CheckConstraint(
        "usage_status <> 'unknown' OR cost_status = 'unknown'", name="unknown_usage_unknown_cost"
    ),
    sa.Index(
        "ix_usage_records_unsettled", "run_id", postgresql_where=sa.text("usage_status = 'unknown'")
    ),
)

tool_calls = _table(
    "tool_calls",
    "activity_90d",
    "MCP tool calls",
    _id(),
    _account(),
    _fk("run_id", "research_runs.id"),
    sa.Column("node_id", sa.Text, nullable=False),
    sa.Column("call_key", sa.Text, nullable=False),
    sa.Column("generation", sa.Integer, nullable=False),
    sa.Column("tool_name", sa.Text, nullable=False),
    sa.Column("tool_version", sa.Text, nullable=False),
    sa.Column("args_hash", sa.Text, nullable=False),
    sa.Column("status", sa.Text, nullable=False, server_default="intent"),
    _ts("started_at", nullable=False, default_now=True),
    sa.Column("duration_ms", sa.BigInteger),
    sa.Column("result_ref", sa.Text),
    sa.Column("error_code", sa.Text),
    sa.UniqueConstraint("call_key"),
    _enum("status", ("intent", "completed", "failed", "ambiguous")),
)

validation_results = _table(
    "validation_results",
    "activity_90d",
    "typed validation verdicts with reasons",
    _id(),
    _account(),
    _fk("run_id", "research_runs.id"),
    sa.Column("boundary", sa.Text, nullable=False),
    sa.Column("subject_id", sa.Text, nullable=False),
    sa.Column("verdict", sa.Text, nullable=False),
    sa.Column("reasons", ARRAY(sa.Text), nullable=False),
    sa.Column("policy_version", sa.Text, nullable=False),
    _created(),
    _enum("verdict", VERDICTS),
)

evidence_items = _table(
    "evidence_items",
    "activity_90d",
    "validated evidence (D108)",
    _id(),
    _account(),
    _fk("run_id", "research_runs.id"),
    sa.Column("kind", sa.Text, nullable=False),
    sa.Column("source_id", sa.Text, nullable=False),
    sa.Column("source_uri", sa.Text),
    sa.Column("source_title", sa.Text),
    _ts("observed_at", nullable=False),
    _ts("fetched_at", nullable=False),
    _ts("valid_until"),
    sa.Column("content_hash", sa.Text, nullable=False),
    sa.Column("trust_class", sa.Text, nullable=False),
    sa.Column(
        "taint_flags", ARRAY(sa.Text), nullable=False, server_default=sa.text("'{}'::text[]")
    ),
    sa.Column("quarantined", sa.Boolean, nullable=False, server_default=sa.false()),
    sa.Column("instrument_id", UUID(as_uuid=True)),
    sa.Column("unit", sa.Text),
    sa.Column("currency", sa.Text),
    sa.Column("payload", JSONB, nullable=False),
    sa.Column("artifact_key", sa.Text),
    sa.Column("validator_version", sa.Text, nullable=False),
    _created(),
    _enum("kind", EVIDENCE_KINDS),
    _enum("trust_class", TRUST_CLASSES),
    sa.Index("ix_evidence_items_run", "run_id"),
)

reports = _table(
    "reports",
    "product_12m",
    "final structured reports",
    _id(),
    _account(),
    _fk("run_id", "research_runs.id"),
    sa.Column("report", JSONB, nullable=False),
    sa.Column("signal", sa.Text, nullable=False),
    sa.Column("confidence_score", sa.Numeric(4, 3)),
    sa.Column("confidence_label", sa.Text),
    sa.Column("calibration_status", sa.Text, nullable=False, server_default="uncalibrated"),
    sa.Column("complete", sa.Boolean, nullable=False),
    _ts("data_as_of"),
    sa.Column("freshness_state", sa.Text, nullable=False),
    _created(),
    sa.UniqueConstraint("run_id"),
    _enum("signal", SIGNALS),
    sa.CheckConstraint(
        "confidence_label IS NULL OR confidence_label IN ('low','medium','high')",
        name="confidence_label_enum",
    ),
    sa.CheckConstraint(
        "confidence_score IS NULL OR confidence_score BETWEEN 0 AND 1", name="confidence_range"
    ),
)

report_citations = _table(
    "report_citations",
    "product_12m",
    "report citations to evidence",
    _id(),
    _account(),
    _fk("report_id", "reports.id"),
    _fk("evidence_id", "evidence_items.id"),
    sa.Column("source_title", sa.Text, nullable=False),
    sa.Column("source_url", sa.Text),
    _ts("observed_at", nullable=False),
    sa.Column("content_hash", sa.Text, nullable=False),
    sa.UniqueConstraint("report_id", "evidence_id"),
)


def _events_table(name: str, description: str) -> sa.Table:
    return _table(
        name,
        "sse_30d",
        description,
        _id(),
        _account(),
        sa.Column("scope_id", UUID(as_uuid=True), nullable=False),
        sa.Column("seq", sa.BigInteger, nullable=False),
        sa.Column("event_type", sa.Text, nullable=False),
        sa.Column("schema_version", sa.Text, nullable=False, server_default="1.0"),
        sa.Column("payload", JSONB, nullable=False),
        sa.Column("request_id", sa.Text),
        _ts("occurred_at", nullable=False, default_now=True),
        _ts("expires_at", nullable=False),
        sa.UniqueConstraint("scope_id", "seq"),
        sa.CheckConstraint("octet_length(payload::text) <= 32768", name="payload_32kb"),
        sa.Index(f"ix_{name}_expires", "expires_at"),
    )


run_events = _events_table("run_events", "replayable per-run SSE events (D085)")
account_events = _events_table("account_events", "replayable per-account SSE events (D085)")

account_event_cursors = _table(
    "account_event_cursors",
    "sse_30d",
    "per-account SSE sequence allocator",
    _account(),
    sa.Column("last_seq", sa.BigInteger, nullable=False, server_default="0"),
    sa.PrimaryKeyConstraint("account_id"),
)

# ================================================================ exports, privacy, platform
export_jobs = _table(
    "export_jobs",
    "product_12m",
    "asynchronous owned-data exports",
    _id(),
    _account(),
    _fk("user_id", "users.id"),
    sa.Column("kind", sa.Text, nullable=False),
    sa.Column("format", sa.Text, nullable=False),
    _ts("from_ts", nullable=False),
    _ts("to_ts", nullable=False),
    sa.Column("status", sa.Text, nullable=False, server_default="queued"),
    sa.Column("artifact_key", sa.Text),
    sa.Column("artifact_sha256", sa.Text),
    sa.Column("row_count", sa.BigInteger),
    sa.Column("error", sa.Text),
    _created(),
    _ts("completed_at"),
    _ts("expires_at"),
    _enum("kind", EXPORT_KINDS),
    _enum("format", EXPORT_FORMATS),
    _enum("status", EXPORT_STATUS),
)

artifacts = _table(
    "artifacts",
    "activity_90d",
    "object references (local ArtifactStore in MVP)",
    _id(),
    _account(),
    sa.Column("key", sa.Text, nullable=False),
    sa.Column("sha256", sa.Text, nullable=False),
    sa.Column("size_bytes", sa.BigInteger, nullable=False),
    sa.Column("content_type", sa.Text, nullable=False),
    sa.Column("retention_class", sa.Text, nullable=False),
    _created(),
    _ts("deleted_at"),
    sa.UniqueConstraint("key"),
)

deletion_requests = _table(
    "deletion_requests",
    "security_12m",
    "account deletion requests (R054)",
    _id(),
    _account(),
    _fk("user_id", "users.id"),
    sa.Column("status", sa.Text, nullable=False, server_default="scheduled"),
    _ts("requested_at", nullable=False, default_now=True),
    _ts("scheduled_at", nullable=False),
    _ts("completed_at"),
    _enum("status", ("scheduled", "processing", "completed", "cancelled")),
)

tombstones = _table(
    "tombstones",
    "security_12m",
    "deletion tombstones, re-applied after restore (R055)",
    _id(),
    _account(),
    sa.Column("subject_table", sa.Text, nullable=False),
    sa.Column("subject_id", UUID(as_uuid=True)),
    sa.Column("reason", sa.Text, nullable=False),
    _created(),
)

trace_outbox = _table(
    "trace_outbox",
    "activity_90d",
    "buffered LangSmith exports (R058)",
    _id(),
    _account(nullable=True),
    sa.Column("run_id", UUID(as_uuid=True)),
    sa.Column("payload", JSONB, nullable=False),
    sa.Column("attempts", sa.Integer, nullable=False, server_default="0"),
    sa.Column("last_error", sa.Text),
    _created(),
    _ts("exported_at"),
)

evaluation_runs = _table(
    "evaluation_runs",
    "platform",
    "evaluation harness runs (R069)",
    _id(),
    sa.Column("dataset_version", sa.Text, nullable=False),
    sa.Column("evaluator_version", sa.Text, nullable=False),
    sa.Column("model_id", sa.Text, nullable=False),
    sa.Column("prompt_version", sa.Text, nullable=False),
    sa.Column("graph_version", sa.Text, nullable=False),
    sa.Column("summary", JSONB, nullable=False, server_default=sa.text("'{}'::jsonb")),
    _created(),
)

evaluation_scores = _table(
    "evaluation_scores",
    "platform",
    "per-case evaluation checks",
    _id(),
    _fk("evaluation_run_id", "evaluation_runs.id"),
    sa.Column("case_id", sa.Text, nullable=False),
    sa.Column("check_name", sa.Text, nullable=False),
    sa.Column("passed", sa.Boolean, nullable=False),
    sa.Column("score", sa.Numeric(6, 4)),
    sa.Column("details", JSONB, nullable=False, server_default=sa.text("'{}'::jsonb")),
    sa.UniqueConstraint("evaluation_run_id", "case_id", "check_name"),
)

provider_health = _table(
    "provider_health",
    "platform",
    "dependency status and circuit-breaker evidence",
    _id(),
    sa.Column("provider", sa.Text, nullable=False),
    sa.Column("status", sa.Text, nullable=False),
    sa.Column("circuit_state", sa.Text, nullable=False),
    sa.Column("last_error", sa.Text),
    _ts("updated_at", nullable=False, default_now=True),
    sa.UniqueConstraint("provider"),
    _enum("circuit_state", ("closed", "open", "half_open")),
)

# Tables whose rows belong to one account and are protected by RLS (migration 0002).
ACCOUNT_OWNED_TABLES: tuple[str, ...] = tuple(
    t.name for t in metadata.sorted_tables if "account_id" in t.c and t.name != "accounts"
)

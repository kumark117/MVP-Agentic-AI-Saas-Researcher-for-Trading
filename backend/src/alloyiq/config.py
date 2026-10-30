"""Process configuration, read from the environment (see .env.example).

Only settings that are used by code exist here; later phases add theirs.
Secrets are typed as SecretStr so they never appear in reprs or logs.
"""

from __future__ import annotations

from decimal import Decimal
from functools import lru_cache
from typing import Literal

from pydantic import SecretStr
from pydantic_settings import BaseSettings, SettingsConfigDict

_DEV_SECRET = "dev-insecure-session-secret-change-me"  # noqa: S105 - rejected in prod by check()


class Settings(BaseSettings):
    model_config = SettingsConfigDict(extra="ignore", case_sensitive=False)

    app_env: Literal["dev", "test", "prod"] = "dev"
    public_origin: str = "http://localhost:8080"
    deployment_version: str = "dev"
    log_level: str = "INFO"

    # Built by docker-compose.yml from per-role passwords (ADR-007).
    database_url: SecretStr | None = None
    database_worker_url: SecretStr | None = None
    redis_url: str | None = None

    session_secret: SecretStr = SecretStr(_DEV_SECRET)

    smtp_host: str = "mailpit"
    smtp_port: int = 1025
    email_from: str = "no-reply@alloyiq.local"
    # Hosted SMTP (e.g. Gmail: smtp.gmail.com:587, STARTTLS, an app password). Mailpit needs none.
    smtp_username: str | None = None
    smtp_password: SecretStr | None = None
    smtp_starttls: bool = False
    # Hourly digest of sign-ups and research runs to the owner; empty disables it.
    owner_alert_email: str | None = None
    owner_alert_timezone: str = "Asia/Kolkata"

    # Argon2id cost (D013); tests lower these.
    argon2_time_cost: int = 3
    argon2_memory_kib: int = 64 * 1024
    argon2_parallelism: int = 4

    mcp_host: str = "0.0.0.0"  # noqa: S104 - private compose network only, never routed by Caddy
    mcp_port: int = 8100
    mcp_url: str = "http://mcp:8100/mcp"
    mcp_service_token: SecretStr = SecretStr("dev-mcp-token-change-me")
    mcp_allowed_hosts: list[str] = ["mcp:8100", "localhost:8100", "127.0.0.1:8100"]
    # "in_process" runs the same five tools inside the worker, for hosts without private
    # services (Render free tier; DV-15). Validation and policy are identical.
    tool_gateway: Literal["mcp", "in_process"] = "mcp"

    # Data providers. "fixtures" serves SYNTHETIC data only (tests, demos without keys);
    # it is refused in prod.
    provider_mode: Literal["live", "fixtures"] = "live"
    # "fake" answers with FakeLLMGateway (SYNTHETIC responses and token counts) for e2e
    # journeys without spending money; refused in prod like PROVIDER_MODE=fixtures.
    llm_mode: Literal["live", "fake"] = "live"
    alphavantage_api_key: SecretStr | None = None
    tavily_api_key: SecretStr | None = None
    enable_yahoo_reference: bool = False
    anthropic_api_key: SecretStr | None = None

    # Real-money guards (REAL USD) and demo assumptions (simulated INR); see .env.example.
    real_spend_per_run_usd: Decimal = Decimal("1.50")
    real_spend_account_day_usd: Decimal = Decimal("5.00")
    real_spend_platform_day_usd: Decimal = Decimal("20.00")
    demo_fx_usd_inr: Decimal = Decimal("85.00")
    demo_markup: Decimal = Decimal("1.25")
    demo_opening_credit_inr: Decimal = Decimal("500.00")
    demo_daily_topup_limit_inr: Decimal = Decimal("1000.00")
    demo_checkout_max_inr: Decimal = Decimal("500.00")

    worker_concurrency: int = 2  # jobs executed at once per worker process
    artifact_dir: str = "./data/artifacts"  # local ArtifactStore volume
    # "postgres" keeps artifact bytes in the artifacts row, for hosts where the api and
    # worker share no disk (Render; DV-15).
    artifact_backend: Literal["local", "postgres"] = "local"
    langsmith_api_key: SecretStr | None = None
    langsmith_project: str = "alloyiq-dev"

    @property
    def is_prod(self) -> bool:
        return self.app_env == "prod"

    @property
    def session_cookie(self) -> str:
        # ADR-003: __Host- prefix and Secure only where browsers accept them (HTTPS).
        return "__Host-alloyiq_sid" if self.is_prod else "alloyiq_sid"

    @property
    def csrf_cookie(self) -> str:
        return "__Host-alloyiq_csrf" if self.is_prod else "alloyiq_csrf"

    def check(self) -> None:
        if self.is_prod and self.session_secret.get_secret_value() == _DEV_SECRET:
            raise RuntimeError("SESSION_SECRET must be set in production")
        if self.is_prod and self.llm_mode == "fake":
            raise RuntimeError(
                "LLM_MODE=fake (synthetic model output) is not allowed in production"
            )
        if self.is_prod and not self.public_origin.startswith("https://"):
            raise RuntimeError("PUBLIC_ORIGIN must be an https:// origin in production")
        if self.is_prod and self.provider_mode == "fixtures":
            raise RuntimeError(
                "PROVIDER_MODE=fixtures (synthetic data) is not allowed in production"
            )


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    s = Settings()
    s.check()
    return s


def secret_or_none(value: SecretStr | None) -> str | None:
    """An empty env var (KEY=) means "not configured"; a SecretStr object is always truthy."""
    raw = value.get_secret_value().strip() if value is not None else ""
    # Docker Compose reads "KEY=   # comment" as the value "# comment": treat it as unset.
    if raw.startswith("#"):
        return None
    return raw or None

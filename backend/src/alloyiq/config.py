"""Process configuration, read from the environment (see .env.example).

Only settings that are used by code exist here; later phases add theirs.
Secrets are typed as SecretStr so they never appear in reprs or logs.
"""

from __future__ import annotations

from functools import lru_cache
from typing import Literal

from pydantic import SecretStr
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(extra="ignore", case_sensitive=False)

    app_env: Literal["dev", "test", "prod"] = "dev"
    public_origin: str = "http://localhost:8080"
    deployment_version: str = "dev"
    log_level: str = "INFO"

    # Built by docker-compose.yml from per-role passwords (ADR-007).
    database_url: SecretStr | None = None
    database_worker_url: SecretStr | None = None

    mcp_host: str = "0.0.0.0"  # noqa: S104 - private compose network only, never routed by Caddy
    mcp_port: int = 8100

    @property
    def is_prod(self) -> bool:
        return self.app_env == "prod"


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    return Settings()

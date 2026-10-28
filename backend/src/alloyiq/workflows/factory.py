"""Builds the production RunExecutor (the LangGraph research workflow)."""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Any

from alloyiq.adapters.anthropic_gateway import AnthropicLLMGateway
from alloyiq.adapters.artifacts import LocalArtifactStore
from alloyiq.adapters.pg_listen import asyncpg_dsn
from alloyiq.application.budget import BudgetService, SpendCaps
from alloyiq.config import Settings, secret_or_none
from alloyiq.domain.llm import LLMGateway
from alloyiq.repositories.db import Database
from alloyiq.tools.gateway import MCPToolGateway
from alloyiq.workflows.graph import ResearchWorkflow
from alloyiq.workflows.runner import RunExecutor, RunHandle, RunResult

CHECKPOINT_SCHEMA = "graph"


class UnavailableWorkflow:
    """Ends a run as failed with an explicit reason; never fabricates a report."""

    def __init__(self, reason: str = "workflow_not_installed") -> None:
        self.reason = reason

    async def execute(self, handle: RunHandle) -> RunResult:
        await handle.activity(None, "unavailable", f"Research is unavailable: {self.reason}.")
        return RunResult("failed", self.reason, "PROVIDER_UNAVAILABLE")


def checkpoint_dsn(sqlalchemy_url: str) -> str:
    sep = "&" if "?" in sqlalchemy_url else "?"
    return asyncpg_dsn(sqlalchemy_url) + f"{sep}options=-c%20search_path%3D{CHECKPOINT_SCHEMA}"


class PostgresCheckpointer:
    """One AsyncPostgresSaver per run (graph schema; created by migration 0004)."""

    def __init__(self, dsn: str) -> None:
        self.dsn = dsn
        self._setup_done = False
        self._lock = asyncio.Lock()

    @asynccontextmanager
    async def __call__(self) -> AsyncIterator[Any]:
        from langgraph.checkpoint.postgres.aio import AsyncPostgresSaver

        async with AsyncPostgresSaver.from_conn_string(self.dsn) as saver:
            async with self._lock:
                if not self._setup_done:
                    await saver.setup()  # idempotent; creates checkpoint tables in `graph`
                    self._setup_done = True
            yield saver


def build_workflow(settings: Settings, db: Database) -> RunExecutor:
    key = secret_or_none(settings.anthropic_api_key)
    gateway: LLMGateway
    if settings.llm_mode == "fake":
        from alloyiq.adapters.fake_llm import FakeLLMGateway

        gateway = FakeLLMGateway()  # SYNTHETIC output for e2e journeys; refused in prod
    elif key is None:
        return UnavailableWorkflow("ANTHROPIC_API_KEY is not configured")
    else:
        gateway = AnthropicLLMGateway(key)
    if not settings.database_worker_url:
        return UnavailableWorkflow("DATABASE_WORKER_URL is not configured")
    tools = MCPToolGateway(settings.mcp_url, settings.mcp_service_token.get_secret_value())
    caps = SpendCaps(
        settings.real_spend_per_run_usd,
        settings.real_spend_account_day_usd,
        settings.real_spend_platform_day_usd,
    )
    return ResearchWorkflow(
        gateway=gateway,
        tool_sessions=tools.session,
        artifacts=LocalArtifactStore(settings.artifact_dir),
        budget=BudgetService(caps),
        checkpointer=PostgresCheckpointer(
            checkpoint_dsn(settings.database_worker_url.get_secret_value())
        ),
        langsmith_api_key=secret_or_none(settings.langsmith_api_key),
        langsmith_project=settings.langsmith_project,
    )

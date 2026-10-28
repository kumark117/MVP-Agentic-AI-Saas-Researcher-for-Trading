"""Builds the production RunExecutor. P6 replaces UnavailableWorkflow with the graph."""

from __future__ import annotations

from alloyiq.config import Settings
from alloyiq.repositories.db import Database
from alloyiq.workflows.runner import RunExecutor, RunHandle, RunResult


class UnavailableWorkflow:
    """Ends a run as failed with an explicit reason; never fabricates a report."""

    async def execute(self, handle: RunHandle) -> RunResult:
        await handle.activity(None, "unavailable", "The research workflow is not installed.")
        return RunResult("failed", "workflow_not_installed", "PROVIDER_UNAVAILABLE")


def build_workflow(settings: Settings, db: Database) -> RunExecutor:
    return UnavailableWorkflow()

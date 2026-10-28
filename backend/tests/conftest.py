"""Suite-wide test configuration."""

from __future__ import annotations

import asyncio
import sys

import pytest


@pytest.fixture(scope="session")
def event_loop_policy() -> asyncio.AbstractEventLoopPolicy:
    # psycopg's async mode (LangGraph checkpointer) cannot run on Windows' default
    # ProactorEventLoop. Containers run Linux, where the default loop is fine.
    if sys.platform == "win32":
        return asyncio.WindowsSelectorEventLoopPolicy()  # type: ignore[attr-defined,unused-ignore]
    return asyncio.DefaultEventLoopPolicy()

"""Versions stored on every run (build prompt section 4 version policy; D105)."""

from __future__ import annotations

import hashlib
from functools import lru_cache
from pathlib import Path

from alloyiq import __version__

GRAPH_VERSION = "graph-1.0"
PROMPTS_DIR = Path(__file__).resolve().parents[1] / "agents" / "prompts"


@lru_cache(maxsize=1)
def prompt_version() -> str:
    """sha256 over the versioned system prompts (agents/prompts/*.md), first 12 hex chars."""
    files = sorted(PROMPTS_DIR.glob("*.md")) if PROMPTS_DIR.is_dir() else []
    if not files:
        return "prompts-none"
    h = hashlib.sha256()
    for f in files:
        h.update(f.name.encode())
        h.update(f.read_bytes())
    return "prompts-" + h.hexdigest()[:12]


def run_versions(policy_version: str, rate_card_version: str) -> dict[str, str]:
    return {
        "graph": GRAPH_VERSION,
        "prompt": prompt_version(),
        "policy": policy_version,
        "rate_card": rate_card_version,
        "application": __version__,
    }

"""ArtifactStore (build prompt section 4): content-addressed files on a local volume, with
sha256, size and ownership recorded in PostgreSQL (`artifacts`). Large or raw payloads
(provider responses, tool envelopes) live here, not in graph checkpoints (D032, D077).
"""

from __future__ import annotations

import asyncio
import hashlib
from pathlib import Path
from typing import Protocol
from uuid import UUID

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncConnection


class ArtifactStore(Protocol):
    async def put(
        self,
        conn: AsyncConnection,
        *,
        account_id: UUID,
        key: str,
        data: bytes,
        content_type: str,
        retention_class: str = "activity_90d",
    ) -> str: ...

    async def get(self, conn: AsyncConnection, key: str) -> bytes | None: ...


class LocalArtifactStore:
    def __init__(self, root: str | Path) -> None:
        self.root = Path(root)

    def _path(self, sha: str) -> Path:
        return self.root / sha[:2] / sha

    async def put(
        self,
        conn: AsyncConnection,
        *,
        account_id: UUID,
        key: str,
        data: bytes,
        content_type: str,
        retention_class: str = "activity_90d",
    ) -> str:
        sha = hashlib.sha256(data).hexdigest()
        path = self._path(sha)

        def write() -> None:
            path.parent.mkdir(parents=True, exist_ok=True)
            if not path.exists():
                tmp = path.with_suffix(".tmp")
                tmp.write_bytes(data)
                tmp.replace(path)

        await asyncio.to_thread(write)
        await conn.execute(
            text(
                "INSERT INTO artifacts (account_id, key, sha256, size_bytes, content_type,"
                " retention_class) VALUES (:a, :k, :s, :n, :c, :r) ON CONFLICT (key) DO NOTHING"
            ),
            {
                "a": account_id,
                "k": key,
                "s": sha,
                "n": len(data),
                "c": content_type,
                "r": retention_class,
            },
        )
        return key

    async def get(self, conn: AsyncConnection, key: str) -> bytes | None:
        sha = (
            await conn.execute(
                text("SELECT sha256 FROM artifacts WHERE key = :k AND deleted_at IS NULL"),
                {"k": key},
            )
        ).scalar()
        if sha is None:
            return None
        path = self._path(str(sha))
        data = await asyncio.to_thread(lambda: path.read_bytes() if path.exists() else None)
        if data is None or hashlib.sha256(data).hexdigest() != sha:
            return None  # missing or tampered: never trust it
        return data

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

from alloyiq.config import Settings


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

    async def remove(self, sha: str) -> int:
        """Delete the file for a content hash no live artifact row uses (retention)."""
        path = self._path(sha)

        def unlink() -> int:
            if path.exists():
                path.unlink()
                return 1
            return 0

        return await asyncio.to_thread(unlink)

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


class PgArtifactStore:
    """Artifact bytes in the `artifacts` row itself (migration 0006), for hosts where the
    api and worker share no disk (Render; DV-15). Rows keep the same RLS as metadata, and
    the purge clears `content` when it marks a row deleted."""

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
        await conn.execute(
            text(
                "INSERT INTO artifacts (account_id, key, sha256, size_bytes, content_type,"
                " retention_class, content) VALUES (:a, :k, :s, :n, :c, :r, :d)"
                " ON CONFLICT (key) DO NOTHING"
            ),
            {
                "a": account_id,
                "k": key,
                "s": hashlib.sha256(data).hexdigest(),
                "n": len(data),
                "c": content_type,
                "r": retention_class,
                "d": data,
            },
        )
        return key

    async def get(self, conn: AsyncConnection, key: str) -> bytes | None:
        row = (
            await conn.execute(
                text("SELECT sha256, content FROM artifacts WHERE key = :k AND deleted_at IS NULL"),
                {"k": key},
            )
        ).one_or_none()
        if row is None or row.content is None:
            return None
        data = bytes(row.content)
        return data if hashlib.sha256(data).hexdigest() == row.sha256 else None


def build_artifact_store(settings: Settings) -> ArtifactStore:
    if settings.artifact_backend == "postgres":
        return PgArtifactStore()
    return LocalArtifactStore(settings.artifact_dir)

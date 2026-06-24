from __future__ import annotations

from collections.abc import Callable
from typing import Any
from uuid import uuid4

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from henry.db.models import ChannelMemory, ChannelStateRow
from henry.memory.summarizer import MemorySnapshotSummarizer, SnapshotSummarizer
from henry.types import ChannelState, ConversationTranscript, MemoryItem


SessionFactory = async_sessionmaker[AsyncSession] | Callable[[], AsyncSession]


class PostgresMemory:
    def __init__(
        self,
        sessionmaker: SessionFactory,
        summarizer: SnapshotSummarizer | None = None,
    ) -> None:
        self._sessionmaker = sessionmaker
        self._summarizer = summarizer or MemorySnapshotSummarizer()

    async def remember(
        self,
        channel_id: str,
        content: str,
        kind: str = "fact",
        metadata: dict | None = None,
    ) -> None:
        item_metadata = dict(metadata or {})
        path = _memory_path(kind, item_metadata)

        async with self._sessionmaker() as session:
            existing = await session.get(ChannelMemory, {"channel_id": channel_id, "path": path})
            if existing is None:
                session.add(
                    ChannelMemory(
                        channel_id=channel_id,
                        path=path,
                        content=content,
                        kind=kind,
                        item_metadata=item_metadata,
                    )
                )
            else:
                existing.content = content
                existing.kind = kind
                existing.item_metadata = item_metadata
            await session.commit()

    async def recall(self, channel_id: str, query: str, k: int = 8) -> list[MemoryItem]:
        terms = [term for term in query.lower().split() if term]

        async with self._sessionmaker() as session:
            result = await session.execute(
                select(ChannelMemory)
                .where(ChannelMemory.channel_id == channel_id)
                .order_by(ChannelMemory.updated_at.desc(), ChannelMemory.path.asc())
            )
            rows = list(result.scalars())

        scored = [(_score(row, terms), row) for row in rows]
        if terms:
            scored = [(score, row) for score, row in scored if score > 0]
        # Stable sort by score only: ties keep the SQL order (updated_at desc, path asc),
        # so equal/zero-term matches surface most-recent-first instead of alphabetically.
        scored.sort(key=lambda item: -item[0])

        return [_to_memory_item(row, score=score) for score, row in scored[: max(k, 0)]]

    async def get(self, channel_id: str, path: str) -> MemoryItem | None:
        async with self._sessionmaker() as session:
            row = await session.get(ChannelMemory, {"channel_id": channel_id, "path": path})

        if row is None:
            return None
        return _to_memory_item(row)

    async def list_paths(self, channel_id: str) -> list[str]:
        async with self._sessionmaker() as session:
            result = await session.execute(
                select(ChannelMemory.path)
                .where(ChannelMemory.channel_id == channel_id)
                .order_by(ChannelMemory.path)
            )
            return list(result.scalars())

    async def snapshot(self, channel_id: str) -> ChannelState:
        async with self._sessionmaker() as session:
            row = await session.get(ChannelStateRow, channel_id)

        if row is None:
            return ChannelState(channel_id=channel_id)
        return ChannelState(
            channel_id=row.channel_id,
            rolling_summary=row.rolling_summary,
            open_tasks=list(row.open_tasks),
            key_facts=list(row.key_facts),
        )

    async def refresh_snapshot(self, channel_id: str, transcript: ConversationTranscript) -> None:
        current = await self.snapshot(channel_id)
        next_state = await self._summarizer.summarize(channel_id, transcript, current)

        async with self._sessionmaker() as session:
            row = await session.get(ChannelStateRow, channel_id)
            if row is None:
                session.add(
                    ChannelStateRow(
                        channel_id=channel_id,
                        rolling_summary=next_state.rolling_summary,
                        open_tasks=next_state.open_tasks,
                        key_facts=next_state.key_facts,
                    )
                )
            else:
                row.rolling_summary = next_state.rolling_summary
                row.open_tasks = next_state.open_tasks
                row.key_facts = next_state.key_facts
            await session.commit()


def _memory_path(kind: str, metadata: dict[str, Any]) -> str:
    raw_path = metadata.get("path")
    if isinstance(raw_path, str) and raw_path.strip():
        return raw_path.strip()
    path = f"{kind}/{uuid4().hex}"
    metadata["path"] = path
    return path


def _score(row: ChannelMemory, terms: list[str]) -> float:
    if not terms:
        return 1.0

    haystack = " ".join(
        [
            row.path,
            row.kind,
            row.content,
            " ".join(str(value) for value in row.item_metadata.values()),
        ]
    ).lower()
    return float(sum(haystack.count(term) for term in terms))


def _to_memory_item(row: ChannelMemory, score: float | None = None) -> MemoryItem:
    return MemoryItem(
        path=row.path,
        content=row.content,
        kind=row.kind,
        metadata=dict(row.item_metadata),
        created_at=row.created_at,
        score=score,
    )

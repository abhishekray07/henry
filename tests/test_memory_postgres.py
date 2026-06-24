from datetime import datetime, timezone
from pathlib import Path

import pytest
from sqlalchemy.ext.asyncio import create_async_engine
from sqlalchemy.pool import NullPool

from henry.db.models import Base, ChannelMemory
from henry.db.session import make_sessionmaker
from henry.memory import PostgresMemory
from henry.types import ConversationTranscript, ThreadMessage


@pytest.mark.asyncio
async def test_postgres_memory_write_snapshot_recall_and_channel_isolation(tmp_path: Path) -> None:
    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'db.sqlite'}", poolclass=NullPool)
    try:
        async with engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)

        memory = PostgresMemory(make_sessionmaker(engine))
        await memory.remember("C1", "The deploy window is Tuesday", metadata={"path": "facts/deploy"})
        await memory.remember("C2", "The deploy window is Friday", metadata={"path": "facts/deploy"})

        transcript = ConversationTranscript(
            channel_id="C1",
            thread_ts="T1",
            messages=(ThreadMessage(role="user", text="Remember that billing launch is blocked."),),
        )
        await memory.refresh_snapshot("C1", transcript)

        snapshot = await memory.snapshot("C1")
        c1_results = await memory.recall("C1", "Tuesday")
        c2_results = await memory.recall("C2", "Tuesday")

        assert "billing launch is blocked" in snapshot.rolling_summary
        assert [item.path for item in c1_results] == ["facts/deploy"]
        assert c1_results[0].content == "The deploy window is Tuesday"
        assert c2_results == []
        assert await memory.list_paths("C1") == ["facts/deploy"]
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_get_returns_item_by_exact_path(tmp_path: Path) -> None:
    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'db.sqlite'}", poolclass=NullPool)
    try:
        async with engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)

        memory = PostgresMemory(make_sessionmaker(engine))
        # Many memories whose paths share a prefix, so a search query would rank ambiguously.
        for i in range(60):
            await memory.remember("C1", f"shared note {i}", metadata={"path": f"facts/{i:02d}"})

        hit = await memory.get("C1", "facts/57")
        miss = await memory.get("C1", "facts/does-not-exist")
        other_channel = await memory.get("C2", "facts/57")

        assert hit is not None
        assert hit.path == "facts/57"
        assert hit.content == "shared note 57"
        assert miss is None
        assert other_channel is None
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_recall_empty_query_returns_most_recent_first(tmp_path: Path) -> None:
    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'db.sqlite'}", poolclass=NullPool)
    try:
        async with engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)

        memory = PostgresMemory(make_sessionmaker(engine))
        # "zzz" sorts last by path but is updated more recently, so recency must win over path order.
        await memory.remember("C1", "first", metadata={"path": "aaa"})
        await memory.remember("C1", "second", metadata={"path": "zzz"})

        sessionmaker = make_sessionmaker(engine)
        async with sessionmaker() as session:
            older = await session.get(ChannelMemory, {"channel_id": "C1", "path": "aaa"})
            newer = await session.get(ChannelMemory, {"channel_id": "C1", "path": "zzz"})
            older.updated_at = datetime(2026, 1, 1, tzinfo=timezone.utc)
            newer.updated_at = datetime(2026, 6, 1, tzinfo=timezone.utc)
            await session.commit()

        results = await memory.recall("C1", "")

        assert [item.path for item in results] == ["zzz", "aaa"]
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_postgres_memory_opens_fresh_session_per_method(tmp_path: Path) -> None:
    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'db.sqlite'}", poolclass=NullPool)
    try:
        async with engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)

        real_sessionmaker = make_sessionmaker(engine)
        sessions_opened = 0

        def counted_sessionmaker():
            nonlocal sessions_opened
            sessions_opened += 1
            return real_sessionmaker()

        memory = PostgresMemory(counted_sessionmaker)
        await memory.remember("C1", "alpha")
        await memory.recall("C1", "alpha")
        await memory.list_paths("C1")
        await memory.snapshot("C1")

        assert sessions_opened == 4
    finally:
        await engine.dispose()

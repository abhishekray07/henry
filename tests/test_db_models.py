from sqlalchemy.schema import CreateTable
from sqlalchemy.dialects import postgresql
from sqlalchemy.ext.asyncio import create_async_engine
from sqlalchemy.pool import NullPool

import pytest

from henry.db.models import Base, ChannelConfig, ChannelMemory, Task
from henry.db.session import make_sessionmaker


@pytest.mark.asyncio
async def test_models_create_on_sqlite_for_unit_tests() -> None:
    engine = create_async_engine("sqlite+aiosqlite:///:memory:", poolclass=NullPool)
    try:
        async with engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)
    finally:
        await engine.dispose()


def test_postgres_schema_uses_jsonb_and_indexes() -> None:
    ddl = str(CreateTable(ChannelConfig.__table__).compile(dialect=postgresql.dialect()))

    assert "JSONB" in ddl
    assert ChannelMemory.__table__.c.metadata.type.compile(dialect=postgresql.dialect()) == "JSONB"
    assert "ix_task_status_run_at" in {index.name for index in Task.__table__.indexes}


def test_metadata_has_deterministic_naming_convention() -> None:
    assert Base.metadata.naming_convention["pk"] == "pk_%(table_name)s"
    assert Base.metadata.naming_convention["uq"] == "uq_%(table_name)s_%(column_0_name)s"


def test_sessionmaker_disables_expiration() -> None:
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    try:
        sessionmaker = make_sessionmaker(engine)
        assert sessionmaker.kw["expire_on_commit"] is False
    finally:
        engine.sync_engine.dispose()

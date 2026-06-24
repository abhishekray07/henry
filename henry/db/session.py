from __future__ import annotations

from typing import Any

from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine


def make_engine(settings: Any):
    return create_async_engine(settings.database_url)


def make_sessionmaker(engine):
    return async_sessionmaker(engine, expire_on_commit=False)

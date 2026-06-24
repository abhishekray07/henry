from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager


class ThreadLocks:
    """Single-process per-thread lock registry.

    This is intentionally scoped to one Python process. A multi-replica deployment
    should replace it with Postgres advisory locks keyed by channel and thread.
    """

    def __init__(self) -> None:
        self._registry_lock = asyncio.Lock()
        self._locks: dict[tuple[str, str], asyncio.Lock] = {}

    async def _get_lock(self, channel_id: str, thread_ts: str) -> asyncio.Lock:
        key = (channel_id, thread_ts)
        async with self._registry_lock:
            lock = self._locks.get(key)
            if lock is None:
                lock = asyncio.Lock()
                self._locks[key] = lock
            return lock

    @asynccontextmanager
    async def acquire(self, channel_id: str, thread_ts: str) -> AsyncIterator[None]:
        lock = await self._get_lock(channel_id, thread_ts)
        async with lock:
            yield

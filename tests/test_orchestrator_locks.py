from __future__ import annotations

import asyncio

import pytest

from henry.orchestrator.locks import ThreadLocks


@pytest.mark.asyncio
async def test_same_thread_lock_serializes_work() -> None:
    locks = ThreadLocks()
    first_entered = asyncio.Event()
    release_first = asyncio.Event()
    second_entered = asyncio.Event()

    async def first() -> None:
        async with locks.acquire("C1", "T1"):
            first_entered.set()
            await release_first.wait()

    async def second() -> None:
        await first_entered.wait()
        async with locks.acquire("C1", "T1"):
            second_entered.set()

    first_task = asyncio.create_task(first())
    second_task = asyncio.create_task(second())
    await first_entered.wait()
    await asyncio.sleep(0)

    assert not second_entered.is_set()

    release_first.set()
    await asyncio.wait_for(second_entered.wait(), timeout=1)
    await asyncio.gather(first_task, second_task)


@pytest.mark.asyncio
async def test_different_thread_locks_can_run_concurrently() -> None:
    locks = ThreadLocks()
    both_entered = asyncio.Event()
    entered: set[str] = set()

    async def worker(thread_ts: str) -> None:
        async with locks.acquire("C1", thread_ts):
            entered.add(thread_ts)
            if len(entered) == 2:
                both_entered.set()
            await both_entered.wait()

    await asyncio.wait_for(asyncio.gather(worker("T1"), worker("T2")), timeout=1)
    assert entered == {"T1", "T2"}

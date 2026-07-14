from __future__ import annotations

import httpx
import pytest

from henry.contracts import AgentDeps
from henry.settings import Settings
from henry.testing import FakeMemory, FakeSandbox
from henry.types import ChannelContext


@pytest.fixture
async def agent_deps():
    deps = AgentDeps(
        ctx=ChannelContext(channel_id="C1", thread_ts="T1", run_id="R1"),
        memory=FakeMemory(),
        sandbox=FakeSandbox(),
        http=httpx.AsyncClient(),
        settings=Settings(default_model="test"),
    )
    yield deps
    await deps.http.aclose()


@pytest.fixture
def stub_agent_tools(monkeypatch) -> None:
    """Strip the built-in memory/sandbox tools so tests exercise only the integration under test."""
    monkeypatch.setattr("henry.agent.runner.memory_tools", lambda: [])
    monkeypatch.setattr("henry.agent.runner.sandbox_tools", lambda: [])

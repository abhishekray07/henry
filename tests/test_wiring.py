from __future__ import annotations

import httpx
import pytest

from henry.agent._tools import memory_tools, sandbox_tools
from henry.config.registry import ResolvedConfig
from henry.db.models import Base, ChannelConfig
from henry.settings import Settings
from henry.testing import FakeIntegration, FakeSandbox
from henry.types import ChannelContext
from henry.wiring import RunSettings, build_runtime


async def test_build_runtime_wires_real_components_and_validates_integrations() -> None:
    settings = Settings(database_url="sqlite+aiosqlite:///:memory:", default_model="test")
    runtime = build_runtime(
        settings,
        http=httpx.AsyncClient(),
        sandbox=FakeSandbox(),
        integrations={"fake": FakeIntegration()},
    )
    try:
        async with runtime.engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)

        async with runtime.sessionmaker() as session:
            session.add(ChannelConfig(channel_id="C-ok", enabled_integrations=["fake"]))
            session.add(ChannelConfig(channel_id="C-bad", enabled_integrations=["missing"]))
            await session.commit()

        good = await runtime.load_config(_event("C-ok"))
        assert good.enabled_integrations == ["fake"]

        with pytest.raises(ValueError, match="unknown integrations"):
            await runtime.load_config(_event("C-bad"))
    finally:
        await runtime.close()


async def test_deps_factory_projects_channel_config_into_run_settings() -> None:
    settings = Settings(
        database_url="sqlite+aiosqlite:///:memory:", default_model="base:model", max_run_usd=1.0
    )
    runtime = build_runtime(
        settings,
        http=httpx.AsyncClient(),
        sandbox=FakeSandbox(),
        integrations={"fake": FakeIntegration()},
    )
    try:
        deps = await runtime.deps_factory(
            ChannelContext(channel_id="C1", thread_ts="T1", run_id="R1"),
            ResolvedConfig(
                model="channel:model",
                enabled_integrations=["fake"],
                system_prompt="channel prompt",
                ambient_on=False,
                budget_caps={"max_run_usd": 2.5},
            ),
        )

        assert isinstance(deps.settings, RunSettings)
        assert deps.settings.default_model == "channel:model"
        assert deps.settings.system_prompt == "channel prompt"
        assert deps.settings.enabled_integrations == ("fake",)
        assert deps.settings.max_run_usd == 2.5
        assert deps.settings.github_token == settings.github_token
    finally:
        await runtime.close()


def test_run_settings_uses_env_default_model_when_channel_model_is_empty() -> None:
    settings = Settings(default_model="env:model")
    run_settings = RunSettings(
        settings,
        ResolvedConfig(model="", system_prompt="prompt"),
    )

    assert run_settings.default_model == "env:model"


async def test_handle_event_uses_configured_transcript_fetcher() -> None:
    from henry.testing import FakeAgentRunner
    from henry.types import ConversationTranscript, ThreadMessage

    settings = Settings(database_url="sqlite+aiosqlite:///:memory:", default_model="test")
    runtime = build_runtime(
        settings,
        http=httpx.AsyncClient(),
        sandbox=FakeSandbox(),
        integrations={"fake": FakeIntegration()},
    )
    thread_transcript = ConversationTranscript(
        channel_id="C-ok",
        thread_ts="T1",
        messages=(ThreadMessage(role="user", text="earlier context", user="U1", ts="0.5"),),
    )
    fetched_for: list[str] = []

    async def fetcher(event):
        fetched_for.append(event.event_id)
        return thread_transcript

    runtime.transcript_fetcher = fetcher
    runtime.runner = FakeAgentRunner()
    try:
        async with runtime.engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)

        await runtime.handle_event(_event("C-ok"))

        assert fetched_for == ["Ev-C-ok"]
        assert runtime.runner.calls[0][2] is thread_transcript
    finally:
        await runtime.close()


def test_agent_tool_bridge_uses_real_memory_and_sandbox_factories() -> None:
    assert {tool.__name__ for tool in memory_tools()} == {"read_memory", "write_memory", "search_memory"}
    assert {tool.__name__ for tool in sandbox_tools()} == {
        "run_bash",
        "write_file",
        "read_file",
        "clone_repo",
    }


def _event(channel_id: str):
    from henry.contracts import SlackEvent

    return SlackEvent(
        channel_id=channel_id,
        thread_ts="T1",
        user="U1",
        text="hello",
        event_id=f"Ev-{channel_id}",
        event_ts="1.0",
        is_mention=True,
    )

from __future__ import annotations

from pydantic_ai import RunContext, UsageLimits
from pydantic_ai.toolsets import FunctionToolset

from henry.agent.runner import PydanticAgentRunner
from henry.contracts import AgentDeps, ToolSpec
from henry.interfaces import Integration
from henry.settings import Settings
from henry.testing import FakeIntegration
from henry.types import ConversationTranscript, ThreadMessage


def _transcript() -> ConversationTranscript:
    return ConversationTranscript(
        channel_id="C1",
        thread_ts="T1",
        messages=(ThreadMessage(role="user", user="U1", ts="1.0", text="please echo"),),
    )


def _scoped(deps: AgentDeps, enabled) -> AgentDeps:
    return AgentDeps(
        ctx=deps.ctx,
        memory=deps.memory,
        sandbox=deps.sandbox,
        http=deps.http,
        settings=_ScopedSettings(Settings(default_model="test"), enabled),
    )


async def test_runner_calls_integration_tool_and_maps_usage(stub_agent_tools, agent_deps) -> None:
    runner = PydanticAgentRunner([FakeIntegration()], model="test")

    result = await runner.run(agent_deps, "echo the request", _transcript())

    assert result.status == "ok"
    assert '"echo":"C1:a"' in result.output
    assert result.usage.requests == 2
    assert result.usage.tool_calls == 1
    assert result.usage.input_tokens > 0
    assert result.usage.cost_usd is None


async def test_runner_maps_usage_limit_to_budget_status(agent_deps) -> None:
    runner = PydanticAgentRunner(model="test", usage_limits=UsageLimits(request_limit=0))

    result = await runner.run(agent_deps, "hello")

    assert result.status == "budget_exceeded"
    assert result.error is not None
    assert "request_limit" in result.error


async def test_runner_maps_tool_exception_to_error_status(stub_agent_tools, agent_deps) -> None:
    runner = PydanticAgentRunner([ExplodingIntegration()], model="test")

    result = await runner.run(agent_deps, "trigger tool")

    assert result.status == "error"
    assert result.error is not None
    assert "ValueError" in result.error
    assert "boom" in result.error


class ExplodingIntegration:
    name = "exploding"
    auth_type = "none"
    allowed_domains: tuple[str, ...] = ()

    def tools(self) -> list[ToolSpec]:
        async def explode(ctx: RunContext[AgentDeps], text: str) -> str:
            raise ValueError(f"boom in {ctx.deps.ctx.channel_id}: {text}")

        return [explode]

    def prompt_fragment(self) -> str:
        return "Exploding integration is available."


def test_exploding_integration_satisfies_protocol() -> None:
    assert isinstance(ExplodingIntegration(), Integration)


class _ScopedSettings:
    """Wraps Settings with an explicit enabled_integrations, like RunSettings does."""

    def __init__(self, base: Settings, enabled) -> None:
        self._base = base
        self.enabled_integrations = enabled

    def __getattr__(self, name: str):
        return getattr(self._base, name)


class ToolsetIntegration:
    """Integration that contributes tools via a toolset instead of tools()."""

    name = "shouter"
    auth_type = "none"
    allowed_domains: tuple[str, ...] = ()

    def tools(self) -> list[ToolSpec]:
        return []

    def prompt_fragment(self) -> str:
        return "Shouter toolset is available."

    def toolset(self):
        def shout(text: str) -> str:
            return f"SHOUTED:{text.upper()}"

        return FunctionToolset([shout])


async def test_wildcard_enables_all_integrations(stub_agent_tools, agent_deps) -> None:
    deps = _scoped(agent_deps, "*")
    runner = PydanticAgentRunner([FakeIntegration()], model="test")

    result = await runner.run(deps, "echo the request", _transcript())

    assert result.status == "ok"
    assert '"echo"' in result.output


async def test_runner_invokes_tools_from_provider_toolsets(stub_agent_tools, agent_deps) -> None:
    runner = PydanticAgentRunner([ToolsetIntegration()], model="test")

    result = await runner.run(agent_deps, "shout something", _transcript())

    assert result.status == "ok"
    assert "SHOUTED:" in result.output


async def test_disabled_provider_contributes_no_toolset(stub_agent_tools, agent_deps) -> None:
    deps = _scoped(agent_deps, [])
    runner = PydanticAgentRunner([ToolsetIntegration()], model="test")

    result = await runner.run(deps, "shout something", _transcript())

    assert result.status == "ok"
    assert "SHOUTED:" not in result.output


class BrokenToolsetIntegration(ToolsetIntegration):
    name = "broken"

    def toolset(self):
        raise RuntimeError("server unreachable")


async def test_unreachable_toolset_degrades_instead_of_failing_the_run(
    stub_agent_tools, agent_deps
) -> None:
    """One dead external server must cost only its own tools, never the whole run."""
    runner = PydanticAgentRunner([BrokenToolsetIntegration(), ToolsetIntegration()], model="test")

    result = await runner.run(agent_deps, "shout something", _transcript())

    assert result.status == "ok"
    assert "SHOUTED:" in result.output


async def test_run_without_tools_survives_a_dead_toolset(stub_agent_tools, agent_deps) -> None:
    runner = PydanticAgentRunner([BrokenToolsetIntegration()], model="test")

    result = await runner.run(agent_deps, "just say hi, no tools needed")

    assert result.status == "ok"

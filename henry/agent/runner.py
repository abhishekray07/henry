from __future__ import annotations

import functools
import logging
from collections.abc import Sequence
from contextlib import AsyncExitStack
from decimal import Decimal
from typing import Any

from pydantic_ai import Agent, UsageLimits
from pydantic_ai.exceptions import ModelRetry, UsageLimitExceeded
from pydantic_ai.messages import ModelResponse
from pydantic_ai.models import Model
from pydantic_ai.usage import RunUsage as PydanticRunUsage

from henry.agent._tools import memory_tools, sandbox_tools
from henry.agent.model import build_model
from henry.agent.prompt import build_instructions
from henry.contracts import AgentDeps, RunResult, RunUsage
from henry.interfaces import Integration, ToolsetProvider
from henry.sanitize import neutralize_delimiters as _neutralize_delimiters
from henry.types import ConversationTranscript

_LOG = logging.getLogger(__name__)

DEFAULT_INSTRUCTIONS = (
    "You are Henry, a helpful AI teammate in Slack. Answer clearly, use tools when they are useful, "
    "and keep channel-specific memory separate from model-visible user input."
)


def _feed_errors_back(tool: Any, integration_name: str) -> Any:
    """Turn integration tool failures into retries the model can react to.

    External-facing tools fail for reasons the model can work around (missing
    credentials, 4xx/5xx, network); a raw exception would kill the whole run.
    Unresolved retries still exhaust the retry budget and end as an error.
    """

    @functools.wraps(tool)
    async def wrapper(*args: Any, **kwargs: Any) -> Any:
        try:
            return await tool(*args, **kwargs)
        except ModelRetry:
            raise
        except Exception as exc:
            raise ModelRetry(
                f"The {integration_name} tool call failed: {exc}. "
                "You may adjust the arguments and retry, or proceed without this tool."
            ) from exc

    return wrapper


def _is_configured(integration: Integration, settings: Any) -> bool:
    checker = getattr(integration, "is_configured", None)
    if checker is None:
        return True
    return bool(checker(settings))


class PydanticAgentRunner:
    def __init__(
        self,
        integrations: Sequence[Integration] = (),
        *,
        base_instructions: str = DEFAULT_INSTRUCTIONS,
        model: Model | str | None = None,
        usage_limits: UsageLimits | None = None,
    ) -> None:
        self._integrations = tuple(integrations)
        self._base_instructions = base_instructions
        self._model = model
        self._usage_limits = usage_limits or UsageLimits()

    async def run(
        self,
        deps: AgentDeps,
        user_prompt: str,
        transcript: ConversationTranscript | None = None,
    ) -> RunResult:
        toolset_names: tuple[str, ...] = ()
        try:
            snapshot = await deps.memory.snapshot(deps.ctx.channel_id)
            integrations = self._active_integrations(deps)
            providers = [i for i in integrations if isinstance(i, ToolsetProvider)]
            async with AsyncExitStack() as stack:
                toolsets: list[Any] = []
                unavailable: set[str] = set()
                # Connect each external toolset individually so one unreachable server
                # costs only its own tools instead of failing the whole run.
                for provider in providers:
                    try:
                        toolsets.append(await stack.enter_async_context(provider.toolset()))
                    except Exception:
                        _LOG.warning(
                            "toolset %r is unavailable; running without its tools",
                            provider.name,
                            exc_info=True,
                        )
                        unavailable.add(provider.name)
                toolset_names = tuple(p.name for p in providers if p.name not in unavailable)
                instructions = build_instructions(
                    getattr(deps.settings, "system_prompt", self._base_instructions)
                    or self._base_instructions,
                    snapshot,
                    [
                        integration.prompt_fragment()
                        if integration.name not in unavailable
                        else f"The {integration.name} integration is temporarily unavailable."
                        for integration in integrations
                    ],
                )
                agent = Agent(
                    self._build_model(deps),
                    deps_type=AgentDeps,
                    instructions=instructions,
                    tools=[
                        *[
                            _feed_errors_back(tool, integration.name)
                            for integration in integrations
                            for tool in integration.tools()
                        ],
                        *memory_tools(),
                        *sandbox_tools(),
                    ],
                    toolsets=toolsets or None,
                )
                result = await agent.run(
                    self._render_prompt(user_prompt, transcript),
                    deps=deps,
                    usage_limits=self._usage_limits,
                )
            return RunResult(
                output=str(result.output),
                usage=_map_usage(result.usage, _compute_cost_usd(result.all_messages())),
                status="ok",
            )
        except UsageLimitExceeded as exc:
            return RunResult(
                output="Henry stopped because the configured run budget was exceeded.",
                status="budget_exceeded",
                error=str(exc),
            )
        except Exception as exc:
            return _error_result(exc, toolset_names)

    def _build_model(self, deps: AgentDeps) -> Model | str:
        if isinstance(self._model, str):
            return build_model(self._model, deps.settings, deps.http)
        if self._model is not None:
            return self._model
        return build_model(deps.settings.default_model, deps.settings, deps.http)

    def _active_integrations(self, deps: AgentDeps) -> tuple[Integration, ...]:
        enabled = getattr(deps.settings, "enabled_integrations", None)
        if enabled is None or enabled == "*":
            # Wildcard means "everything usable", not "everything that exists": an
            # integration without its credentials would only hand the model tools
            # that fail. Explicitly named integrations below are always honored.
            return tuple(
                integration
                for integration in self._integrations
                if _is_configured(integration, deps.settings)
            )

        by_name = {integration.name: integration for integration in self._integrations}
        unknown = set(enabled) - set(by_name)
        if unknown:
            names = ", ".join(sorted(unknown))
            raise ValueError(f"unknown integrations configured for agent runner: {names}")
        return tuple(by_name[name] for name in enabled)

    @staticmethod
    def _render_prompt(user_prompt: str, transcript: ConversationTranscript | None) -> str:
        safe_prompt = _neutralize_delimiters(user_prompt)
        if transcript is None:
            return safe_prompt
        return (
            "<slack_thread>\n"
            f"{_neutralize_delimiters(transcript.render())}\n"
            "</slack_thread>\n\n"
            "<user_request>\n"
            f"{safe_prompt}\n"
            "</user_request>"
        )


def _map_usage(usage: PydanticRunUsage, cost_usd: float | None) -> RunUsage:
    return RunUsage(
        input_tokens=usage.input_tokens,
        output_tokens=usage.output_tokens,
        requests=usage.requests,
        tool_calls=usage.tool_calls,
        cost_usd=cost_usd,
    )


def _compute_cost_usd(messages: Sequence[Any]) -> float | None:
    total = Decimal("0")
    found = False
    for message in messages:
        if not isinstance(message, ModelResponse):
            continue
        try:
            price = message.cost()
        except Exception:
            continue
        total_price = getattr(price, "total_price", None)
        if total_price is None:
            continue
        total += Decimal(str(total_price))
        found = True
    return float(total) if found else None


def _error_result(exc: Exception, toolset_names: tuple[str, ...] = ()) -> RunResult:
    detail = f"{type(exc).__name__}: {exc}"
    if toolset_names:
        detail += f" (external toolsets active: {', '.join(toolset_names)})"
    return RunResult(
        output="Henry could not complete the request because the agent run failed.",
        status="error",
        error=detail,
    )

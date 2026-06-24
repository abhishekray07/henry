from __future__ import annotations

from collections.abc import Sequence
from decimal import Decimal
from typing import Any

from pydantic_ai import Agent, UsageLimits
from pydantic_ai.exceptions import ModelAPIError, ToolRetryError, UnexpectedModelBehavior, UsageLimitExceeded
from pydantic_ai.messages import ModelResponse
from pydantic_ai.models import Model
from pydantic_ai.usage import RunUsage as PydanticRunUsage

from henry.agent._tools import memory_tools, sandbox_tools
from henry.agent.model import build_model
from henry.agent.prompt import build_instructions
from henry.contracts import AgentDeps, RunResult, RunUsage
from henry.interfaces import Integration
from henry.types import ConversationTranscript


DEFAULT_INSTRUCTIONS = (
    "You are Henry, a helpful AI teammate in Slack. Answer clearly, use tools when they are useful, "
    "and keep channel-specific memory separate from model-visible user input."
)


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
        try:
            snapshot = await deps.memory.snapshot(deps.ctx.channel_id)
            instructions = build_instructions(
                self._base_instructions,
                snapshot,
                [integration.prompt_fragment() for integration in self._integrations],
            )
            agent = Agent(
                self._build_model(deps),
                deps_type=AgentDeps,
                instructions=instructions,
                tools=[
                    *[tool for integration in self._integrations for tool in integration.tools()],
                    *memory_tools(),
                    *sandbox_tools(),
                ],
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
        except (ModelAPIError, ToolRetryError, UnexpectedModelBehavior) as exc:
            return _error_result(exc)
        except Exception as exc:
            return _error_result(exc)

    def _build_model(self, deps: AgentDeps) -> Model | str:
        if isinstance(self._model, str):
            return build_model(self._model, deps.settings, deps.http)
        if self._model is not None:
            return self._model
        return build_model(deps.settings.default_model, deps.settings, deps.http)

    @staticmethod
    def _render_prompt(user_prompt: str, transcript: ConversationTranscript | None) -> str:
        if transcript is None:
            return user_prompt
        return (
            "<slack_thread>\n"
            f"{transcript.render()}\n"
            "</slack_thread>\n\n"
            "<user_request>\n"
            f"{user_prompt}\n"
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


def _error_result(exc: Exception) -> RunResult:
    return RunResult(
        output="Henry could not complete the request because the agent run failed.",
        status="error",
        error=f"{type(exc).__name__}: {exc}",
    )

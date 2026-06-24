from __future__ import annotations

from typing import Any, Protocol

from pydantic import BaseModel, Field
from pydantic_ai import Agent
from pydantic_ai.models import KnownModelName, Model

from henry.types import ChannelState, ConversationTranscript


class SnapshotUpdate(BaseModel):
    rolling_summary: str = ""
    open_tasks: list[dict[str, Any]] = Field(default_factory=list)
    key_facts: list[dict[str, Any]] = Field(default_factory=list)


class SnapshotSummarizer(Protocol):
    async def summarize(
        self,
        channel_id: str,
        transcript: ConversationTranscript,
        current: ChannelState | None = None,
    ) -> ChannelState: ...


class MemorySnapshotSummarizer:
    def __init__(self, model: Model | KnownModelName | str | None = None) -> None:
        self._model = model

    async def summarize(
        self,
        channel_id: str,
        transcript: ConversationTranscript,
        current: ChannelState | None = None,
    ) -> ChannelState:
        if self._model is None:
            return _fallback_summary(channel_id, transcript, current)

        agent = Agent(
            self._model,
            output_type=SnapshotUpdate,
            instructions=(
                "Refresh a compact Slack channel memory snapshot. "
                "Keep the rolling summary short, preserve durable facts, and list only explicit open tasks."
            ),
        )
        result = await agent.run(_summarizer_prompt(transcript, current))
        update = result.output
        return ChannelState(
            channel_id=channel_id,
            rolling_summary=update.rolling_summary,
            open_tasks=update.open_tasks,
            key_facts=update.key_facts,
        )


def _summarizer_prompt(transcript: ConversationTranscript, current: ChannelState | None) -> str:
    existing = current or ChannelState(channel_id=transcript.channel_id)
    return "\n".join(
        [
            "<current_snapshot>",
            f"rolling_summary: {existing.rolling_summary}",
            f"open_tasks: {existing.open_tasks}",
            f"key_facts: {existing.key_facts}",
            "</current_snapshot>",
            "<thread_transcript>",
            transcript.render(),
            "</thread_transcript>",
        ]
    )


def _fallback_summary(
    channel_id: str,
    transcript: ConversationTranscript,
    current: ChannelState | None,
) -> ChannelState:
    rendered = transcript.render()
    summary = rendered[:1000]
    if current and current.rolling_summary and not summary:
        summary = current.rolling_summary
    return ChannelState(
        channel_id=channel_id,
        rolling_summary=summary,
        open_tasks=list(current.open_tasks) if current else [],
        key_facts=list(current.key_facts) if current else [],
    )

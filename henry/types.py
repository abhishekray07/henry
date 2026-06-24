from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Literal


@dataclass(frozen=True)
class ChannelContext:
    channel_id: str
    thread_ts: str
    actor_user_id: str | None = None
    run_id: str = ""


@dataclass
class MemoryItem:
    path: str
    content: str
    kind: str = "fact"
    metadata: dict[str, Any] = field(default_factory=dict)
    created_at: datetime | None = None
    score: float | None = None


@dataclass
class ChannelState:
    channel_id: str
    rolling_summary: str = ""
    open_tasks: list[dict[str, Any]] = field(default_factory=list)
    key_facts: list[dict[str, Any]] = field(default_factory=list)


@dataclass(frozen=True)
class ThreadMessage:
    role: Literal["user", "assistant", "system"]
    text: str
    user: str | None = None
    ts: str | None = None


@dataclass(frozen=True)
class ConversationTranscript:
    channel_id: str
    thread_ts: str
    messages: tuple[ThreadMessage, ...]

    def render(self) -> str:
        lines: list[str] = []
        for message in self.messages:
            speaker = message.role
            if message.user:
                speaker = f"{speaker}:{message.user}"
            ts = f" [{message.ts}]" if message.ts else ""
            lines.append(f"{speaker}{ts}: {message.text}")
        return "\n".join(lines)

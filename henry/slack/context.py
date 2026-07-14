from __future__ import annotations

from collections.abc import Awaitable, Callable, Mapping, Sequence
from typing import Any

from henry.contracts import SlackEvent
from henry.types import ConversationTranscript, ThreadMessage

DEFAULT_SLACK_CHUNK_LIMIT = 3900


def _messages_from_replies(
    replies: Mapping[str, Any] | Sequence[Mapping[str, Any]],
) -> Sequence[Mapping[str, Any]]:
    if isinstance(replies, Mapping):
        messages = replies.get("messages", ())
        if isinstance(messages, Sequence) and not isinstance(messages, (str, bytes)):
            return messages
        return ()
    return replies


def _message_role(message: Mapping[str, Any], bot_user_id: str | None) -> str:
    if bot_user_id and message.get("user") == bot_user_id:
        return "assistant"
    if message.get("bot_id") or message.get("subtype") == "bot_message":
        return "assistant"
    return "user"


def build_transcript(
    *,
    channel_id: str,
    thread_ts: str,
    replies: Mapping[str, Any] | Sequence[Mapping[str, Any]],
    bot_user_id: str | None = None,
) -> ConversationTranscript:
    messages: list[ThreadMessage] = []
    for message in _messages_from_replies(replies):
        text = str(message.get("text") or "")
        if not text:
            continue
        messages.append(
            ThreadMessage(
                role=_message_role(message, bot_user_id),  # type: ignore[arg-type]
                text=text,
                user=message.get("user"),
                ts=message.get("ts"),
            )
        )
    return ConversationTranscript(channel_id=channel_id, thread_ts=thread_ts, messages=tuple(messages))


def make_transcript_fetcher(
    client: Any,
    *,
    bot_user_id: str | None = None,
    limit: int = 200,
) -> Callable[[SlackEvent], Awaitable[ConversationTranscript]]:
    """Build a transcript fetcher that reads the full thread via conversations.replies."""

    async def fetch(event: SlackEvent) -> ConversationTranscript:
        replies = await client.conversations_replies(
            channel=event.channel_id,
            ts=event.thread_ts,
            limit=limit,
        )
        return build_transcript(
            channel_id=event.channel_id,
            thread_ts=event.thread_ts,
            replies=replies.get("messages") or (),
            bot_user_id=bot_user_id,
        )

    return fetch


def build_slack_event(body: Mapping[str, Any], *, is_mention: bool = True) -> SlackEvent:
    event = body.get("event")
    if not isinstance(event, Mapping):
        raise ValueError("Slack body is missing event payload")

    event_id = str(body.get("event_id") or "")
    channel_id = str(event.get("channel") or "")
    event_ts = str(event.get("ts") or "")
    thread_ts = str(event.get("thread_ts") or event_ts)
    user = str(event.get("user") or "")

    if not event_id:
        raise ValueError("Slack body is missing outer event_id")
    if not channel_id:
        raise ValueError("Slack event is missing channel")
    if not thread_ts:
        raise ValueError("Slack event is missing thread timestamp")

    return SlackEvent(
        channel_id=channel_id,
        thread_ts=thread_ts,
        user=user,
        text=str(event.get("text") or ""),
        event_id=event_id,
        event_ts=event_ts,
        is_mention=is_mention,
    )


def split_for_slack(text: str, *, limit: int = DEFAULT_SLACK_CHUNK_LIMIT) -> list[str]:
    if limit < 1:
        raise ValueError("limit must be positive")
    if text == "":
        return [""]

    chunks: list[str] = []
    start = 0
    while start < len(text):
        end = min(start + limit, len(text))
        if end < len(text):
            newline = text.rfind("\n", start, end + 1)
            if newline > start:
                end = newline + 1
        chunks.append(text[start:end])
        start = end
    return chunks

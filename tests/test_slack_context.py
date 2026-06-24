from __future__ import annotations

from henry.contracts import SlackEvent
from henry.slack.context import build_slack_event, build_transcript, split_for_slack
from henry.types import ConversationTranscript


def test_build_transcript_from_conversation_replies_payload() -> None:
    transcript = build_transcript(
        channel_id="C123",
        thread_ts="1719170000.000100",
        replies={
            "messages": [
                {"user": "U1", "text": "first", "ts": "1719170000.000100"},
                {"user": "B1", "text": "second", "ts": "1719170001.000100"},
                {"subtype": "bot_message", "text": "third", "ts": "1719170002.000100"},
            ]
        },
        bot_user_id="B1",
    )

    assert isinstance(transcript, ConversationTranscript)
    assert transcript.channel_id == "C123"
    assert transcript.thread_ts == "1719170000.000100"
    assert [message.role for message in transcript.messages] == ["user", "assistant", "assistant"]
    assert "first" in transcript.render()
    assert "third" in transcript.render()


def test_build_slack_event_uses_outer_event_id_and_thread_ts_fallback() -> None:
    event = build_slack_event(
        {
            "event_id": "Ev123",
            "event": {
                "channel": "C123",
                "user": "U123",
                "text": "<@B123> hello",
                "ts": "1719170000.000100",
            },
        }
    )

    assert event == SlackEvent(
        channel_id="C123",
        thread_ts="1719170000.000100",
        user="U123",
        text="<@B123> hello",
        event_id="Ev123",
        event_ts="1719170000.000100",
        is_mention=True,
    )


def test_split_for_slack_preserves_text_and_limits_chunk_size() -> None:
    text = "alpha\n" + ("x" * 32) + "\n" + ("y" * 14)

    chunks = split_for_slack(text, limit=20)

    assert "".join(chunks) == text
    assert all(len(chunk) <= 20 for chunk in chunks)
    assert split_for_slack("", limit=20) == [""]

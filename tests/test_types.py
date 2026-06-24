from dataclasses import FrozenInstanceError

import pytest

from henry.contracts import AgentDeps, RunResult, RunUsage, SlackEvent
from henry.testing import FakeMemory, FakeSandbox
from henry.types import ChannelContext, ConversationTranscript, ThreadMessage


def test_core_types_construct_and_render() -> None:
    transcript = ConversationTranscript(
        channel_id="C123",
        thread_ts="171.1",
        messages=(
            ThreadMessage(role="user", text="remember launch date", user="U1", ts="171.1"),
            ThreadMessage(role="assistant", text="noted"),
        ),
    )

    rendered = transcript.render()

    assert "remember launch date" in rendered
    assert "assistant: noted" in rendered
    assert transcript.channel_id == "C123"


def test_frozen_host_context_and_messages() -> None:
    ctx = ChannelContext(channel_id="C1", thread_ts="T1")
    message = ThreadMessage(role="user", text="hello")

    with pytest.raises(FrozenInstanceError):
        ctx.channel_id = "C2"  # type: ignore[misc]
    with pytest.raises(FrozenInstanceError):
        message.text = "changed"  # type: ignore[misc]


def test_agent_contract_dtos_construct() -> None:
    deps = AgentDeps(
        ctx=ChannelContext(channel_id="C1", thread_ts="T1"),
        memory=FakeMemory(),
        sandbox=FakeSandbox(),
        http=None,  # type: ignore[arg-type]
        settings=object(),
    )
    result = RunResult(output="done", usage=RunUsage(input_tokens=1, output_tokens=2, requests=1, cost_usd=0.01))
    event = SlackEvent(
        channel_id="C1",
        thread_ts="T1",
        user="U1",
        text="hi",
        event_ts="E1",
        is_mention=True,
    )

    assert deps.ctx.channel_id == "C1"
    assert result.status == "ok"
    assert event.is_mention is True

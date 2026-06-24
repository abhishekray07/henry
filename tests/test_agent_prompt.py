from __future__ import annotations

from henry.agent.prompt import build_instructions
from henry.types import ChannelState


def test_build_instructions_includes_fragments_and_channel_memory() -> None:
    snapshot = ChannelState(
        channel_id="C1",
        rolling_summary="The team is planning a launch.",
        key_facts=[{"fact": "API freeze on Friday"}],
        open_tasks=[{"task": "write rollout checklist"}],
    )

    instructions = build_instructions("Base instructions", snapshot, ["GitHub tools are available."])

    assert instructions.startswith("Base instructions")
    assert "GitHub tools are available." in instructions
    assert "<channel_memory>" in instructions
    assert "C1" in instructions
    assert "The team is planning a launch." in instructions
    assert "API freeze on Friday" in instructions
    assert "write rollout checklist" in instructions

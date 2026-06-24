from pydantic_ai.messages import ModelResponse, TextPart
from pydantic_ai.models.function import FunctionModel

from henry.memory import MemorySnapshotSummarizer
from henry.types import ConversationTranscript, ThreadMessage


async def test_memory_snapshot_summarizer_uses_function_model_output() -> None:
    async def summarize(messages, info):
        return ModelResponse(
            parts=[
                TextPart(
                    """
                    {
                      "rolling_summary": "Billing launch is blocked on QA.",
                      "open_tasks": [{"task": "Finish QA"}],
                      "key_facts": [{"fact": "Launch owner is Ada"}]
                    }
                    """
                )
            ]
        )

    transcript = ConversationTranscript(
        channel_id="C1",
        thread_ts="T1",
        messages=(ThreadMessage(role="user", text="Billing launch is blocked on QA."),),
    )

    state = await MemorySnapshotSummarizer(FunctionModel(summarize)).summarize("C1", transcript)

    assert state.channel_id == "C1"
    assert state.rolling_summary == "Billing launch is blocked on QA."
    assert state.open_tasks == [{"task": "Finish QA"}]
    assert state.key_facts == [{"fact": "Launch owner is Ada"}]

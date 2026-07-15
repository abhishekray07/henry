import pytest

from henry.contracts import AgentDeps, RunResult
from henry.sandbox_types import ExecRequest, ExecResult, SandboxPolicy
from henry.testing import FakeAgentRunner, FakeIntegration, FakeMemory, FakeSandbox
from henry.types import ChannelContext, ConversationTranscript, ThreadMessage


async def test_fake_memory_is_channel_scoped() -> None:
    memory = FakeMemory()
    await memory.remember("C1", "alpha launch fact")
    await memory.remember("C2", "alpha secret")

    assert [item.content for item in await memory.recall("C1", "alpha")] == ["alpha launch fact"]
    assert await memory.list_paths("C2") == ["fact/1"]


async def test_fake_sandbox_records_calls_and_returns_canned_result() -> None:
    sandbox = FakeSandbox(canned_result=ExecResult(exit_code=7, stdout="out", stderr="err"))

    session = await sandbox.start(SandboxPolicy(image="test"))
    await sandbox.write_file(session, "/workspace/a.txt", b"hello")
    result = await sandbox.exec(session, ExecRequest(cmd=["cat", "a.txt"]))
    content = await sandbox.read_file(session, "/workspace/a.txt")
    await sandbox.destroy(session)

    assert result.exit_code == 7
    assert content == b"hello"
    assert [call[0] for call in sandbox.calls] == ["start", "write_file", "exec", "read_file", "destroy"]


@pytest.mark.asyncio
async def test_fake_agent_runner_returns_run_result() -> None:
    deps = AgentDeps(
        ctx=ChannelContext(channel_id="C1", thread_ts="T1"),
        memory=FakeMemory(),
        sandbox=FakeSandbox(),
        http=None,  # type: ignore[arg-type]
        settings=object(),
    )
    transcript = ConversationTranscript("C1", "T1", (ThreadMessage(role="user", text="hello"),))
    runner = FakeAgentRunner(result=RunResult(output="ok"))

    result = await runner.run(deps, "hello", transcript)

    assert result.output == "ok"
    assert runner.calls == [(deps, "hello", transcript)]


def test_fake_integration_exposes_tool_spec() -> None:
    tools = FakeIntegration().tools()

    assert len(tools) == 1
    assert tools[0].__name__ == "echo"


async def test_fake_sandbox_exec_cell_returns_canned_and_records() -> None:
    from henry.sandbox_types import CellOutput, CellResult, SandboxPolicy

    canned = CellResult(
        status="ok",
        outputs=[CellOutput(output_type="execute_result", data={"text/plain": "42"})],
    )
    sandbox = FakeSandbox(canned_cell=canned)
    session = await sandbox.start(SandboxPolicy())
    result = await sandbox.exec_cell(session, "40 + 2")

    assert result.outputs[0].data["text/plain"] == "42"
    assert ("exec_cell", session, "40 + 2") in sandbox.calls

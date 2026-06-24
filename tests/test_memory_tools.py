import inspect
from types import SimpleNamespace

from henry.contracts import AgentDeps
from henry.memory import memory_tools
from henry.sandbox_types import ExecResult
from henry.testing import FakeMemory, FakeSandbox
from henry.types import ChannelContext


async def test_memory_tools_are_host_channel_scoped() -> None:
    memory = FakeMemory()
    await memory.remember("C2", "do not leak this")
    deps = AgentDeps(
        ctx=ChannelContext(channel_id="C1", thread_ts="T1"),
        memory=memory,
        sandbox=FakeSandbox(canned_result=ExecResult(exit_code=0, stdout="", stderr="")),
        http=None,  # type: ignore[arg-type]
        settings=object(),
    )
    ctx = SimpleNamespace(deps=deps)
    tools = {tool.__name__: tool for tool in memory_tools()}

    assert "channel_id" not in inspect.signature(tools["write_memory"]).parameters
    assert "channel_id" not in inspect.signature(tools["search_memory"]).parameters
    assert "channel_id" not in inspect.signature(tools["read_memory"]).parameters

    assert await tools["write_memory"](ctx, "alpha launch fact", path="facts/alpha") == "Memory saved."
    assert await tools["search_memory"](ctx, "alpha") == [
        {
            "path": "fact/1",
            "content": "alpha launch fact",
            "kind": "fact",
            "metadata": {"path": "facts/alpha"},
            "created_at": None,
            "score": None,
        }
    ]
    assert await tools["search_memory"](ctx, "leak") == []


async def test_read_memory_returns_snapshot_and_paths() -> None:
    memory = FakeMemory()
    await memory.remember("C1", "alpha launch fact")
    deps = AgentDeps(
        ctx=ChannelContext(channel_id="C1", thread_ts="T1"),
        memory=memory,
        sandbox=FakeSandbox(),
        http=None,  # type: ignore[arg-type]
        settings=object(),
    )
    ctx = SimpleNamespace(deps=deps)
    tools = {tool.__name__: tool for tool in memory_tools()}

    result = await tools["read_memory"](ctx)

    assert result["rolling_summary"] == ""
    assert result["paths"] == ["fact/1"]


async def test_read_memory_by_path_fetches_exact_item() -> None:
    memory = FakeMemory()
    await memory.remember("C1", "alpha launch fact")
    deps = AgentDeps(
        ctx=ChannelContext(channel_id="C1", thread_ts="T1"),
        memory=memory,
        sandbox=FakeSandbox(),
        http=None,  # type: ignore[arg-type]
        settings=object(),
    )
    ctx = SimpleNamespace(deps=deps)
    tools = {tool.__name__: tool for tool in memory_tools()}

    found = await tools["read_memory"](ctx, path="fact/1")
    missing = await tools["read_memory"](ctx, path="fact/404")

    assert found["item"]["content"] == "alpha launch fact"
    assert missing["item"] is None

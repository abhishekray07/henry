from __future__ import annotations

from henry.contracts import ToolSpec
from henry.memory.tools import memory_tools as _memory_tools
from henry.sandbox.tools import sandbox_tools as _sandbox_tools


def memory_tools() -> list[ToolSpec]:
    return _memory_tools()


def sandbox_tools() -> list[ToolSpec]:
    return _sandbox_tools()

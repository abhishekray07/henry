"""Stdio MCP server that exits on its first-ever tool call, then recovers."""

import os
from pathlib import Path

from mcp.server.fastmcp import FastMCP

server = FastMCP("flaky")


@server.tool()
def flaky(text: str) -> str:
    marker = Path(os.environ["HENRY_TEST_FLAKY_MARKER"])
    if not marker.exists():
        marker.write_text("died once", encoding="utf-8")
        os._exit(1)
    return f"recovered:{text}"


if __name__ == "__main__":
    server.run()

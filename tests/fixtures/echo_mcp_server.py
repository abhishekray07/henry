"""Minimal stdio MCP server for end-to-end tests."""

import os
from pathlib import Path

from mcp.server.fastmcp import FastMCP

pid_file = os.environ.get("HENRY_TEST_PID_FILE")
if pid_file:
    Path(pid_file).write_text(str(os.getpid()), encoding="utf-8")

server = FastMCP("echo")


@server.tool()
def echo_upper(text: str) -> str:
    return f"ECHO:{text.upper()}"


@server.tool()
def hidden_tool(text: str) -> str:
    return "HIDDEN-TOOL-RAN"


if __name__ == "__main__":
    server.run()

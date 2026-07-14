"""Stdio MCP server whose tool errors for unknown ids, like a real 404."""

from mcp.server.fastmcp import FastMCP

server = FastMCP("lookup")

KNOWN_CONVERSATION = "3384773514"


@server.tool()
def get_conversation(conversation_id: str) -> str:
    if conversation_id != KNOWN_CONVERSATION:
        raise ValueError(f"Client error '404 Not Found' for conversation {conversation_id}")
    return f"conversation {KNOWN_CONVERSATION}: customer cannot log in"


if __name__ == "__main__":
    server.run()

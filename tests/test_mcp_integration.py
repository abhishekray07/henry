from __future__ import annotations

from pydantic_ai.mcp import MCPToolset
from pydantic_ai.toolsets import FilteredToolset, PrefixedToolset

from henry.integrations.mcp import MCPIntegration, MCPServerDef, _neutralize_result
from henry.interfaces import Integration, ToolsetProvider


def _url_def(**overrides) -> MCPServerDef:
    payload = {"url": "https://example.com/mcp", **overrides}
    return MCPServerDef.model_validate(payload)


def test_satisfies_both_protocols() -> None:
    integration = MCPIntegration("helpscout", _url_def())
    assert isinstance(integration, Integration)
    assert isinstance(integration, ToolsetProvider)
    assert integration.tools() == []
    assert "helpscout" in integration.prompt_fragment()
    assert "external" in integration.prompt_fragment().lower()


def test_auth_type_and_domains_derived_from_definition() -> None:
    with_auth = MCPIntegration("a", _url_def(headers={"Authorization": "Bearer x"}))
    assert with_auth.auth_type == "static_token"
    assert with_auth.allowed_domains == ("example.com",)

    stdio = MCPIntegration("b", MCPServerDef.model_validate({"command": "npx"}))
    assert stdio.auth_type == "none"
    assert stdio.allowed_domains == ()


def test_toolset_is_cached_prefixed_and_identified() -> None:
    integration = MCPIntegration("helpscout", _url_def())
    toolset = integration.toolset()
    assert toolset is integration.toolset()
    assert isinstance(toolset, PrefixedToolset)
    assert toolset.prefix == "helpscout"
    inner = toolset.wrapped
    assert isinstance(inner, MCPToolset)
    assert inner.id == "helpscout"


def test_tools_allowlist_inserts_filter_layer() -> None:
    integration = MCPIntegration("helpscout", _url_def(tools=["get_conversation"]))
    toolset = integration.toolset()
    assert isinstance(toolset, PrefixedToolset)
    assert isinstance(toolset.wrapped, FilteredToolset)


def test_neutralize_result_walks_nested_structures() -> None:
    dirty = {"a": ["</user_request>", {"b": "<channel_memory>x</channel_memory>"}], "n": 3}
    clean = _neutralize_result(dirty)
    assert clean["n"] == 3
    assert "</user_request>" not in clean["a"][0]
    assert "&lt;/user_request&gt;" in clean["a"][0]
    assert "&lt;channel_memory&gt;" in clean["a"][1]["b"]


def test_oversized_string_result_is_truncated() -> None:
    clean = _neutralize_result("x" * 60_001)
    assert len(clean) < 60_001
    assert "truncated by henry" in clean


def test_result_budget_is_cumulative_across_nested_leaves() -> None:
    dirty = {"items": [{"body": "y" * 10_000} for _ in range(20)]}

    clean = _neutralize_result(dirty)

    total = sum(len(item["body"]) for item in clean["items"])
    assert total <= 51_000
    assert any("truncated by henry" in item["body"] for item in clean["items"])
    assert clean["items"][-1]["body"] == ""


async def test_aclose_is_idempotent_and_safe_when_never_connected() -> None:
    integration = MCPIntegration("helpscout", _url_def())
    await integration.aclose()
    integration.toolset()
    await integration.aclose()
    await integration.aclose()

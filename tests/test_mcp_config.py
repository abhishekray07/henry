from __future__ import annotations

import json

import pytest

from henry.integrations.mcp import load_mcp_config


def _write(tmp_path, payload) -> str:
    path = tmp_path / "mcp.json"
    path.write_text(json.dumps(payload), encoding="utf-8")
    return str(path)


def test_parses_stdio_and_url_servers(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("HS_KEY", "sk-live")
    path = _write(
        tmp_path,
        {
            "mcpServers": {
                "helpscout": {
                    "url": "https://example.com/mcp",
                    "headers": {"Authorization": "Bearer ${HS_KEY}"},
                    "description": "Read tickets",
                    "tools": ["get_conversation"],
                    "read_timeout": 30,
                },
                "files": {"command": "npx", "args": ["-y", "server-fs", "${MISSING:-/data}"]},
            }
        },
    )

    definitions = load_mcp_config(path, explicit=True)

    assert definitions["helpscout"].url == "https://example.com/mcp"
    assert definitions["helpscout"].headers["Authorization"] == "Bearer sk-live"
    assert definitions["helpscout"].tools == ["get_conversation"]
    assert definitions["helpscout"].on_tool_error == "error"
    assert definitions["helpscout"].read_timeout == 30
    assert definitions["files"].command == "npx"
    assert definitions["files"].args[-1] == "/data"


def test_undefined_env_var_raises_with_server_name(tmp_path, monkeypatch) -> None:
    monkeypatch.delenv("NOPE", raising=False)
    path = _write(tmp_path, {"mcpServers": {"s1": {"url": "https://x/${NOPE}"}}})

    with pytest.raises(ValueError, match="s1.*NOPE"):
        load_mcp_config(path, explicit=True)


def test_server_must_be_stdio_xor_url(tmp_path) -> None:
    both = _write(tmp_path, {"mcpServers": {"s1": {"command": "npx", "url": "https://x"}}})
    with pytest.raises(ValueError, match="exactly one"):
        load_mcp_config(both, explicit=True)

    neither = _write(tmp_path, {"mcpServers": {"s1": {"description": "empty"}}})
    with pytest.raises(ValueError, match="exactly one"):
        load_mcp_config(neither, explicit=True)


@pytest.mark.parametrize(
    ("server", "message"),
    [
        ({"url": "https://x", "args": ["--oops"]}, "stdio-only.*args"),
        ({"url": "https://x", "env": {"KEY": "value"}}, "stdio-only.*env"),
        ({"url": "https://x", "cwd": "/tmp"}, "stdio-only.*cwd"),
        ({"command": "npx", "headers": {"X-Key": "value"}}, "HTTP-only.*headers"),
    ],
)
def test_transport_specific_fields_cannot_cross_transport_groups(tmp_path, server, message) -> None:
    path = _write(tmp_path, {"mcpServers": {"s1": server}})

    with pytest.raises(ValueError, match=message):
        load_mcp_config(path, explicit=True)


def test_invalid_server_name_rejected(tmp_path) -> None:
    path = _write(tmp_path, {"mcpServers": {"my server!": {"url": "https://x"}}})
    with pytest.raises(ValueError, match="my server!"):
        load_mcp_config(path, explicit=True)


def test_long_server_name_warns_about_provider_tool_limits(tmp_path, caplog) -> None:
    name = "a" * 33
    path = _write(tmp_path, {"mcpServers": {name: {"url": "https://x"}}})

    with caplog.at_level("WARNING", logger="henry.integrations.mcp"):
        load_mcp_config(path, explicit=True)

    assert any(name in record.getMessage() for record in caplog.records)


def test_missing_file_explicit_raises_default_returns_empty(tmp_path) -> None:
    missing = str(tmp_path / "nope.json")
    with pytest.raises(FileNotFoundError):
        load_mcp_config(missing, explicit=True)
    assert load_mcp_config(missing, explicit=False) == {}


def test_missing_default_file_emits_debug_signal(tmp_path, caplog) -> None:
    missing = tmp_path / "nope.json"

    with caplog.at_level("DEBUG", logger="henry.integrations.mcp"):
        assert load_mcp_config(missing, explicit=False) == {}

    assert any(str(missing) in record.getMessage() for record in caplog.records)


def test_malformed_json_raises(tmp_path) -> None:
    path = tmp_path / "mcp.json"
    path.write_text("{not json", encoding="utf-8")
    with pytest.raises(ValueError, match="mcp.json"):
        load_mcp_config(str(path), explicit=True)


def test_non_object_json_root_rejected(tmp_path) -> None:
    for payload in ("null", "[1, 2]", '"servers"'):
        path = tmp_path / "mcp.json"
        path.write_text(payload, encoding="utf-8")
        with pytest.raises(ValueError, match="mcpServers"):
            load_mcp_config(str(path), explicit=True)


def test_validation_errors_never_disclose_expanded_secrets(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("SECRET_TOKEN", "sk-SENTINEL-do-not-leak")
    path = _write(
        tmp_path,
        {
            "mcpServers": {
                "s1": {
                    "command": "npx",
                    "url": "https://x",
                    "headers": {"Authorization": "Bearer ${SECRET_TOKEN}"},
                }
            }
        },
    )

    with pytest.raises(ValueError) as excinfo:
        load_mcp_config(path, explicit=True)

    assert "sk-SENTINEL-do-not-leak" not in str(excinfo.value)


def test_env_expansion_works_in_non_string_fields(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("TIMEOUT_SECS", "45")
    monkeypatch.delenv("UNSET_TIMEOUT", raising=False)
    path = _write(
        tmp_path,
        {
            "mcpServers": {
                "s1": {"url": "https://x", "read_timeout": "${TIMEOUT_SECS:-30}"},
                "s2": {"url": "https://y", "init_timeout": "${UNSET_TIMEOUT:-2.5}"},
            }
        },
    )

    definitions = load_mcp_config(path, explicit=True)

    assert definitions["s1"].read_timeout == 45.0
    assert definitions["s2"].init_timeout == 2.5


def test_missing_default_config_logs_a_visible_note(tmp_path, caplog) -> None:
    import logging

    with caplog.at_level(logging.INFO, logger="henry.integrations.mcp"):
        definitions = load_mcp_config(tmp_path / "mcp.json", explicit=False)

    assert definitions == {}
    messages = [record.getMessage() for record in caplog.records]
    assert any("no MCP servers configured" in message and "mcp.json" in message for message in messages)

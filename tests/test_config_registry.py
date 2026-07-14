import pytest
from pydantic import ValidationError

from henry.config.registry import ResolvedConfig, load_channel_config


class FakeSession:
    def __init__(self, row=None) -> None:
        self.row = row

    async def get_channel_config(self, channel_id: str):
        assert channel_id == "C123"
        return self.row


async def test_load_channel_config_merges_defaults_with_row() -> None:
    row = {
        "channel_id": "C123",
        "model": "openai:gpt-test",
        "enabled_integrations": ["github"],
        "budget_caps": {"max_run_usd": 2.5},
    }

    resolved = await load_channel_config(FakeSession(row), "C123", known_integrations={"github"})

    assert resolved.model == "openai:gpt-test"
    assert resolved.enabled_integrations == ["github"]
    assert resolved.system_prompt
    assert resolved.budget_caps["max_run_usd"] == 2.5


class FakeOrmSession:
    """No ``get_channel_config`` → exercises the ``session.get(ChannelConfig, ...)`` path."""

    def __init__(self, row) -> None:
        self.row = row

    async def get(self, _model, pk):
        assert pk == "C123"
        return self.row


async def test_load_channel_config_reads_orm_row_attributes() -> None:
    from henry.db.models import ChannelConfig

    row = ChannelConfig(
        channel_id="C123",
        model="openai:gpt-orm",
        system_prompt="orm prompt",
        enabled_integrations=["github"],
        ambient_on=True,
        budget_caps={"max_run_usd": 4.0},
    )

    resolved = await load_channel_config(FakeOrmSession(row), "C123", known_integrations={"github"})

    assert resolved.model == "openai:gpt-orm"
    assert resolved.system_prompt == "orm prompt"
    assert resolved.enabled_integrations == ["github"]
    assert resolved.ambient_on is True
    assert resolved.budget_caps["max_run_usd"] == 4.0


async def test_unknown_config_keys_are_rejected() -> None:
    with pytest.raises(ValidationError):
        ResolvedConfig.model_validate(
            {
                "model": "openai:gpt-test",
                "enabled_integrations": [],
                "system_prompt": "hello",
                "ambient_on": False,
                "budget_caps": {},
                "unexpected": True,
            }
        )


async def test_unknown_integrations_are_rejected() -> None:
    row = {"channel_id": "C123", "enabled_integrations": ["missing"]}

    with pytest.raises(ValueError, match="unknown integrations"):
        await load_channel_config(FakeSession(row), "C123", known_integrations={"github"})


async def test_empty_model_resolves_to_empty_and_defers_to_runtime_default() -> None:
    row = {"channel_id": "C123", "model": " "}

    resolved = await load_channel_config(FakeSession(row), "C123")

    assert resolved.model == ""


async def test_defaults_enable_all_integrations() -> None:
    resolved = await load_channel_config(FakeSession(None), "C123", known_integrations={"github"})

    assert resolved.enabled_integrations == "*"


async def test_wildcard_is_rejected_in_channel_rows() -> None:
    row = {"channel_id": "C123", "enabled_integrations": "*"}

    with pytest.raises(ValueError, match="explicit list"):
        await load_channel_config(FakeSession(row), "C123", known_integrations={"github"})

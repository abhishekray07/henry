from __future__ import annotations

import os

import pytest

import henry.app
from henry.app import validate_startup_settings
from henry.settings import Settings


def test_main_exports_dotenv_vars_before_running(tmp_path, monkeypatch) -> None:
    """Provider keys like ANTHROPIC_API_KEY live in .env but are read from os.environ."""
    (tmp_path / ".env").write_text("FAKE_PROVIDER_KEY=sk-test\n", encoding="utf-8")
    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv("FAKE_PROVIDER_KEY", raising=False)
    seen: dict[str, str | None] = {}

    async def fake_amain() -> None:
        seen["key"] = os.environ.get("FAKE_PROVIDER_KEY")

    monkeypatch.setattr(henry.app, "amain", fake_amain)
    try:
        henry.app.main()
        assert seen["key"] == "sk-test"
    finally:
        os.environ.pop("FAKE_PROVIDER_KEY", None)


def test_validate_startup_settings_requires_slack_tokens() -> None:
    with pytest.raises(RuntimeError, match="HENRY_SLACK_BOT_TOKEN, HENRY_SLACK_APP_TOKEN"):
        validate_startup_settings(Settings(slack_bot_token="", slack_app_token=""))


def test_validate_startup_settings_accepts_configured_slack_tokens() -> None:
    validate_startup_settings(Settings(slack_bot_token="xoxb-test", slack_app_token="xapp-test"))

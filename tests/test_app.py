from __future__ import annotations

import pytest

from henry.app import validate_startup_settings
from henry.settings import Settings


def test_validate_startup_settings_requires_slack_tokens() -> None:
    with pytest.raises(RuntimeError, match="HENRY_SLACK_BOT_TOKEN, HENRY_SLACK_APP_TOKEN"):
        validate_startup_settings(Settings(slack_bot_token="", slack_app_token=""))


def test_validate_startup_settings_accepts_configured_slack_tokens() -> None:
    validate_startup_settings(Settings(slack_bot_token="xoxb-test", slack_app_token="xapp-test"))

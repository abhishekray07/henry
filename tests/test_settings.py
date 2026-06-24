from henry import branding
from henry.settings import Settings


def test_branding_constants_are_centralized() -> None:
    assert branding.APP_NAME
    assert branding.BOT_DISPLAY_NAME
    assert branding.PACKAGE_NAME == "henry"


def test_settings_read_henry_environment(monkeypatch) -> None:
    monkeypatch.setenv("HENRY_DATABASE_URL", "postgresql+asyncpg://example/db")
    monkeypatch.setenv("HENRY_SLACK_BOT_TOKEN", "xoxb-test")
    monkeypatch.setenv("HENRY_DEFAULT_MODEL", "openai:gpt-test")
    monkeypatch.setenv("HENRY_LITELLM_BASE_URL", "https://litellm.example")
    monkeypatch.setenv("UNRELATED_DATABASE_URL", "ignored")

    settings = Settings()

    assert settings.database_url == "postgresql+asyncpg://example/db"
    assert settings.slack_bot_token == "xoxb-test"
    assert settings.default_model == "openai:gpt-test"
    assert settings.litellm_base_url == "https://litellm.example"

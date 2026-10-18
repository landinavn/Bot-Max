"""Загрузка адреса ProxyAPI и ключа из .env и окружения."""

import pytest

import config


@pytest.mark.parametrize("base_url", [None, "", "   "])
def test_proxyapi_is_default_base_url(tmp_path, monkeypatch, base_url):
    monkeypatch.setattr(config, "BASE_DIR", tmp_path)
    monkeypatch.setenv("MAX_BOT_TOKEN", "test-token")
    monkeypatch.delenv("OPENAI_BASE_URL", raising=False)
    if base_url is not None:
        monkeypatch.setenv("OPENAI_BASE_URL", base_url)

    assert config.Config.from_env().openai_base_url == "https://api.proxyapi.ru/openai/v1"


def test_proxyapi_settings_are_loaded_from_dotenv(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "BASE_DIR", tmp_path)
    for name in ("MAX_BOT_TOKEN", "IMAGE_API_KEY", "OPENAI_BASE_URL"):
        # Зарегистрировать восстановление даже для изначально отсутствующих переменных.
        monkeypatch.setenv(name, "")
        monkeypatch.delenv(name, raising=False)
    (tmp_path / ".env").write_text(
        "MAX_BOT_TOKEN=test-token\n"
        "IMAGE_API_KEY=test-proxyapi-key\n"
        "OPENAI_BASE_URL=https://api.proxyapi.ru/openai/v1\n"
    )

    settings = config.Config.from_env()

    assert settings.openai_base_url == "https://api.proxyapi.ru/openai/v1"
    assert settings.image_api_key == "test-proxyapi-key"


def test_environment_overrides_dotenv(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "BASE_DIR", tmp_path)
    (tmp_path / ".env").write_text(
        "IMAGE_API_KEY=dotenv-key\n"
        "OPENAI_BASE_URL=https://api.proxyapi.ru/openai/v1\n"
    )
    monkeypatch.setenv("MAX_BOT_TOKEN", "test-token")
    monkeypatch.setenv("IMAGE_API_KEY", "environment-key")
    monkeypatch.setenv("OPENAI_BASE_URL", " https://custom-api.example.com/v1/ ")

    settings = config.Config.from_env()

    assert settings.openai_base_url == "https://custom-api.example.com/v1/"
    assert settings.image_api_key == "environment-key"

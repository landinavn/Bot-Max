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
    for name in ("MAX_BOT_TOKEN", "IMAGE_API_KEY", "OPENAI_BASE_URL", "IMAGE_QUALITY", "IMAGE_MODEL"):
        # Зарегистрировать восстановление даже для изначально отсутствующих переменных.
        monkeypatch.setenv(name, "")
        monkeypatch.delenv(name, raising=False)
    (tmp_path / ".env").write_text(
        "MAX_BOT_TOKEN=test-token\n"
        "IMAGE_API_KEY=test-proxyapi-key\n"
        "OPENAI_BASE_URL=https://api.proxyapi.ru/openai/v1\n"
        "IMAGE_MODEL=test-dotenv-model\n"
        "IMAGE_QUALITY=low\n"
    )

    settings = config.Config.from_env()

    assert settings.openai_base_url == "https://api.proxyapi.ru/openai/v1"
    assert settings.image_api_key == "test-proxyapi-key"
    assert settings.image_model == "test-dotenv-model"
    assert settings.image_quality == "low"


def test_environment_overrides_dotenv(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "BASE_DIR", tmp_path)
    (tmp_path / ".env").write_text(
        "IMAGE_API_KEY=dotenv-key\n"
        "OPENAI_BASE_URL=https://api.proxyapi.ru/openai/v1\n"
        "IMAGE_QUALITY=low\n"
        "IMAGE_MODEL=test-dotenv-model\n"
    )
    monkeypatch.setenv("MAX_BOT_TOKEN", "test-token")
    monkeypatch.setenv("IMAGE_API_KEY", "environment-key")
    monkeypatch.setenv("OPENAI_BASE_URL", " https://custom-api.example.com/v1/ ")
    monkeypatch.setenv("IMAGE_QUALITY", " high ")
    monkeypatch.setenv("IMAGE_MODEL", " test-environment-model ")

    settings = config.Config.from_env()

    assert settings.openai_base_url == "https://custom-api.example.com/v1/"
    assert settings.image_api_key == "environment-key"
    assert settings.image_quality == "high"
    assert settings.image_model == "test-environment-model"


@pytest.mark.parametrize("quality", [None, "", "   "])
def test_missing_image_model_and_default_quality(tmp_path, monkeypatch, quality):
    monkeypatch.setattr(config, "BASE_DIR", tmp_path)
    monkeypatch.setenv("MAX_BOT_TOKEN", "test-token")
    monkeypatch.delenv("IMAGE_MODEL", raising=False)
    monkeypatch.delenv("IMAGE_QUALITY", raising=False)
    if quality is not None:
        monkeypatch.setenv("IMAGE_QUALITY", quality)

    settings = config.Config.from_env()

    assert settings.image_model == ""
    assert settings.image_quality == "high"
    assert config.Config(token="test-token", photo_path=tmp_path).image_quality == "high"
    assert config.Config(token="test-token", photo_path=tmp_path).image_model == ""

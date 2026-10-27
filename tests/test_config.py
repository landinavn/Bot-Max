"""Загрузка ключа OpenAI, прокси и других настроек из .env и окружения."""

import pytest

import config


@pytest.mark.parametrize("proxy_url", [None, "", "   "])
def test_openai_proxy_is_optional(tmp_path, monkeypatch, proxy_url):
    monkeypatch.setattr(config, "BASE_DIR", tmp_path)
    monkeypatch.setenv("MAX_BOT_TOKEN", "test-token")
    monkeypatch.delenv("OPENAI_PROXY_URL", raising=False)
    if proxy_url is not None:
        monkeypatch.setenv("OPENAI_PROXY_URL", proxy_url)

    assert config.Config.from_env().openai_proxy_url is None


def test_openai_settings_are_loaded_from_dotenv(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "BASE_DIR", tmp_path)
    for name in ("MAX_BOT_TOKEN", "IMAGE_API_KEY", "OPENAI_PROXY_URL", "IMAGE_QUALITY", "IMAGE_MODEL", "ADMIN_USER_ID"):
        # Зарегистрировать восстановление даже для изначально отсутствующих переменных.
        monkeypatch.setenv(name, "")
        monkeypatch.delenv(name, raising=False)
    (tmp_path / ".env").write_text(
        "MAX_BOT_TOKEN=test-token\n"
        "IMAGE_API_KEY=test-openai-key\n"
        "OPENAI_PROXY_URL=http://94.249.177.108:8888\n"
        "IMAGE_MODEL=test-dotenv-model\n"
        "IMAGE_QUALITY=low\n"
        "ADMIN_USER_ID=456\n"
    )

    settings = config.Config.from_env()

    assert settings.openai_proxy_url == "http://94.249.177.108:8888"
    assert settings.image_api_key == "test-openai-key"
    assert settings.image_model == "test-dotenv-model"
    assert settings.image_quality == "low"
    assert settings.admin_user_id == 456


def test_environment_overrides_dotenv(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "BASE_DIR", tmp_path)
    (tmp_path / ".env").write_text(
        "IMAGE_API_KEY=dotenv-key\n"
        "OPENAI_PROXY_URL=http://94.249.177.108:8888\n"
        "IMAGE_QUALITY=low\n"
        "IMAGE_MODEL=test-dotenv-model\n"
        "ADMIN_USER_ID=456\n"
    )
    monkeypatch.setenv("MAX_BOT_TOKEN", "test-token")
    monkeypatch.setenv("IMAGE_API_KEY", "environment-key")
    monkeypatch.setenv("OPENAI_PROXY_URL", " http://custom-proxy.example.com:8080 ")
    monkeypatch.setenv("IMAGE_QUALITY", " high ")
    monkeypatch.setenv("IMAGE_MODEL", " test-environment-model ")
    monkeypatch.setenv("ADMIN_USER_ID", " 789 ")

    settings = config.Config.from_env()

    assert settings.openai_proxy_url == "http://custom-proxy.example.com:8080"
    assert settings.image_api_key == "environment-key"
    assert settings.image_quality == "high"
    assert settings.image_model == "test-environment-model"
    assert settings.admin_user_id == 789


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


@pytest.mark.parametrize("value,expected", [(None, None), ("", None), ("   ", None), (" 456 ", 456)])
def test_admin_user_id_from_environment(tmp_path, monkeypatch, value, expected):
    monkeypatch.setattr(config, "BASE_DIR", tmp_path)
    monkeypatch.setenv("MAX_BOT_TOKEN", "test-token")
    monkeypatch.delenv("ADMIN_USER_ID", raising=False)
    if value is not None:
        monkeypatch.setenv("ADMIN_USER_ID", value)
    assert config.Config.from_env().admin_user_id == expected


@pytest.mark.parametrize("value", ["not-an-id", "0", "-456", "1.5"])
def test_invalid_admin_user_id_is_rejected(tmp_path, monkeypatch, value):
    monkeypatch.setattr(config, "BASE_DIR", tmp_path)
    monkeypatch.setenv("MAX_BOT_TOKEN", "test-token")
    monkeypatch.setenv("ADMIN_USER_ID", value)
    with pytest.raises(ValueError, match="ADMIN_USER_ID"):
        config.Config.from_env()


def test_bot_link_is_loaded_and_normalized(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "BASE_DIR", tmp_path)
    monkeypatch.setenv("MAX_BOT_TOKEN", "test-token")
    monkeypatch.setenv("BOT_LINK", " https://max.ru/another_bot/ ")
    assert config.Config.from_env().bot_link == "https://max.ru/another_bot"


@pytest.mark.parametrize("value", ["http://max.ru/bot", "https://evil.test/bot", "https://max.ru/bot?start=x", "https://max.ru/bot/extra"])
def test_invalid_bot_link_is_rejected(tmp_path, monkeypatch, value):
    monkeypatch.setattr(config, "BASE_DIR", tmp_path)
    monkeypatch.setenv("MAX_BOT_TOKEN", "test-token")
    monkeypatch.setenv("BOT_LINK", value)
    with pytest.raises(ValueError, match="BOT_LINK"):
        config.Config.from_env()


def test_ttapi_key_loads_dotenv_environment_and_is_hidden(tmp_path, monkeypatch):
    monkeypatch.setattr(config, 'BASE_DIR', tmp_path)
    monkeypatch.setenv('MAX_BOT_TOKEN', 'test-token')
    monkeypatch.setenv('TTAPI_KEY', '')
    monkeypatch.delenv('TTAPI_KEY')
    (tmp_path / '.env').write_text('TTAPI_KEY=dotenv-test-key\n')
    settings = config.Config.from_env()
    assert settings.ttapi_key == 'dotenv-test-key'
    assert 'dotenv-test-key' not in repr(settings)
    monkeypatch.setenv('TTAPI_KEY', ' environment-test-key ')
    settings = config.Config.from_env()
    assert settings.ttapi_key == 'environment-test-key'
    assert 'environment-test-key' not in repr(settings)
    monkeypatch.setenv('TTAPI_KEY', '  ')
    assert config.Config.from_env().ttapi_key is None

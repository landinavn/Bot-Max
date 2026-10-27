"""Настройки из .env и окружения; окружение имеет приоритет."""

import os
from dataclasses import dataclass, field
from pathlib import Path
from urllib.parse import urlsplit
import re

from dotenv import load_dotenv

BASE_DIR = Path(__file__).resolve().parent
MAX_API_URL = "https://platform-api.max.ru"
DEFAULT_BOT_LINK = "https://max.ru/se14596751_1_bot"


def validate_bot_link(value: str) -> str:
    parsed = urlsplit(value)
    if (parsed.scheme != "https" or parsed.netloc != "max.ru" or parsed.query or parsed.fragment
            or not re.fullmatch(r"/[A-Za-z0-9_.-]+/?", parsed.path)):
        raise ValueError("BOT_LINK должен быть публичной ссылкой https://max.ru/имя_бота без параметров.")
    return value.rstrip("/")


@dataclass(frozen=True)
class Config:
    token: str = field(repr=False)
    photo_path: Path
    image_api_key: str | None = field(default=None, repr=False)
    image_model: str = ""
    storage_bucket: str | None = None
    storage_endpoint_url: str = "https://storage.yandexcloud.net"
    storage_region: str = "ru-central1"
    storage_access_key_id: str | None = field(default=None, repr=False)
    storage_secret_access_key: str | None = field(default=None, repr=False)
    storage_public_base_url: str | None = None
    prodamus_domain: str | None = None
    prodamus_secret_key: str | None = field(default=None, repr=False)
    prodamus_return_url: str | None = None
    prodamus_webhook_url: str | None = None
    webhook_host: str = "0.0.0.0"
    webhook_port: int = 8080
    openai_proxy_url: str | None = None
    image_quality: str = "high"
    admin_user_id: int | None = None
    bot_link: str = DEFAULT_BOT_LINK
    ttapi_key: str | None = field(default=None, repr=False)

    @classmethod
    def from_env(cls) -> "Config":
        load_dotenv(BASE_DIR / ".env", override=False)
        token = os.environ.get("MAX_BOT_TOKEN", "").strip()
        if not token:
            raise ValueError("Задайте MAX_BOT_TOKEN в переменных окружения.")

        photo_path = Path(os.environ.get("PHOTO_PATH", "assets/photo.jpg")).expanduser()
        if not photo_path.is_absolute():
            photo_path = BASE_DIR / photo_path
        image_api_key = os.environ.get("IMAGE_API_KEY", "").strip() or None
        image_model = os.environ.get("IMAGE_MODEL", "").strip()
        admin_raw = os.environ.get("ADMIN_USER_ID", "").strip()
        try:
            admin_user_id = int(admin_raw) if admin_raw else None
        except ValueError:
            raise ValueError("ADMIN_USER_ID должен быть положительным числовым user_id или пустым.") from None
        if admin_user_id is not None and admin_user_id <= 0:
            raise ValueError("ADMIN_USER_ID должен быть положительным числовым user_id или пустым.")
        return cls(
            token=token,
            photo_path=photo_path,
            image_api_key=image_api_key,
            ttapi_key=os.environ.get("TTAPI_KEY", "").strip() or None,
            image_model=image_model,
            admin_user_id=admin_user_id,
            bot_link=validate_bot_link(os.environ.get("BOT_LINK", "").strip() or DEFAULT_BOT_LINK),
            image_quality=os.environ.get("IMAGE_QUALITY", "").strip() or "high",
            openai_proxy_url=os.environ.get("OPENAI_PROXY_URL", "").strip() or None,
            storage_bucket=os.environ.get("STORAGE_BUCKET", "").strip() or None,
            storage_endpoint_url=os.environ.get(
                "STORAGE_ENDPOINT_URL", "https://storage.yandexcloud.net"
            ).strip() or "https://storage.yandexcloud.net",
            storage_region=os.environ.get("STORAGE_REGION", "ru-central1").strip(),
            storage_access_key_id=os.environ.get("STORAGE_ACCESS_KEY_ID", "").strip() or None,
            storage_secret_access_key=os.environ.get("STORAGE_SECRET_ACCESS_KEY", "").strip() or None,
            storage_public_base_url=os.environ.get("STORAGE_PUBLIC_BASE_URL", "").strip() or None,
            prodamus_domain=os.environ.get("PRODAMUS_DOMAIN", "").strip() or None,
            prodamus_secret_key=os.environ.get("PRODAMUS_SECRET_KEY", "").strip() or None,
            prodamus_return_url=os.environ.get("PRODAMUS_RETURN_URL", "").strip() or None,
            prodamus_webhook_url=os.environ.get("PRODAMUS_WEBHOOK_URL", "").strip() or None,
            webhook_host=os.environ.get("WEBHOOK_HOST", "0.0.0.0").strip(),
            webhook_port=int(os.environ.get("WEBHOOK_PORT", "8080")),
        )

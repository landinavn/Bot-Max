"""Настройки из .env и окружения; окружение имеет приоритет."""

import os
from dataclasses import dataclass, field
from pathlib import Path

from dotenv import load_dotenv

BASE_DIR = Path(__file__).resolve().parent
MAX_API_URL = "https://platform-api.max.ru"


@dataclass(frozen=True)
class Config:
    token: str = field(repr=False)
    photo_path: Path
    image_api_key: str | None = field(default=None, repr=False)
    image_model: str = "gpt-image-2.5-sunburst"
    storage_bucket: str | None = None
    storage_endpoint_url: str = "https://storage.yandexcloud.net"
    storage_region: str = "ru-central1"
    storage_access_key_id: str | None = field(default=None, repr=False)
    storage_secret_access_key: str | None = field(default=None, repr=False)
    storage_public_base_url: str | None = None
    prodamus_domain: str | None = None
    prodamus_secret_key: str | None = field(default=None, repr=False)

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
        image_model = os.environ.get("IMAGE_MODEL", "gpt-image-2.5-sunburst").strip()
        return cls(
            token=token,
            photo_path=photo_path,
            image_api_key=image_api_key,
            image_model=image_model,
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
        )

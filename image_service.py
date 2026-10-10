"""Загрузка в MAX и создание плакатов через OpenAI Images API."""

import asyncio
import base64
import binascii
import logging
from pathlib import Path
from urllib.parse import quote, urlsplit
from uuid import uuid4

import aiohttp
import boto3
from botocore.exceptions import (
    BotoCoreError,
    ClientError,
    ConnectTimeoutError,
    ReadTimeoutError,
)
from botocore.session import Session as BotocoreSession
from maxapi import Bot
from maxapi.enums.attachment import AttachmentType
from maxapi.enums.upload_type import UploadType
from maxapi.types import Attachment, InputMedia, InputMediaBuffer, Message, OtherAttachmentPayload

from config import Config, MAX_API_URL

OPENAI_IMAGE_EDIT_URL = "https://api.openai.com/v1/images/edits"
MAX_PHOTO_BYTES = 50 * 1024 * 1024
logger = logging.getLogger(__name__)


async def upload_photo(file_bytes: bytes) -> str:
    """Загрузить фото в MAX и вернуть token для payload вложения image.

    Токен бота загружается из MAX_BOT_TOKEN через Config.from_env().
    maxapi получает URL загрузки, отправляет байты и извлекает media token.
    Ошибки MAX API передаются вызывающему коду; HTTP-сессия закрывается всегда.
    """
    if not isinstance(file_bytes, bytes):
        raise TypeError("file_bytes должен быть bytes.")
    if not file_bytes:
        raise ValueError("Фото не должно быть пустым.")

    config = Config.from_env()
    bot = Bot(token=config.token, auto_requests=False)
    bot.set_api_url(MAX_API_URL)
    try:
        logger.info("Запрос MAX API: загрузка фото (%d байт).", len(file_bytes))
        attachment = await bot.upload_media(
            InputMediaBuffer(
                buffer=file_bytes,
                filename="photo",
                type=UploadType.IMAGE,
            )
        )
        token = attachment.payload.token
        if not token.strip():
            raise RuntimeError("MAX вернул пустой media token.")
        return token
    except Exception as exc:
        logger.error("Ошибка загрузки фото в MAX (%s).", type(exc).__name__)
        raise
    finally:
        await bot.close_session()


class GenerationNotConfigured(RuntimeError):
    """Не заданы ключ или модель генерации изображений."""


class ImageGenerationError(RuntimeError):
    """Ошибка загрузки исходного фото или API генерации."""


class GenerationTimeoutError(ImageGenerationError):
    """API или хранилище не ответило за отведённое время."""


class UnsupportedPhotoFormatError(ImageGenerationError):
    """Фото имеет неподдерживаемый формат или не может быть прочитано."""


class GenerationLimitError(ImageGenerationError):
    """Достигнута квота или ограничена частота запросов API."""

    def __init__(self, *, quota_exhausted: bool = False, retry_after: int | None = None):
        self.quota_exhausted = quota_exhausted
        self.retry_after = retry_after
        super().__init__(
            "Лимит генераций исчерпан." if quota_exhausted
            else "Превышен лимит запросов генерации."
        )


async def _raise_api_error(response: aiohttp.ClientResponse) -> None:
    """Классифицировать ошибку API, не выводя исходный ответ с данными пользователя."""
    code = ""
    try:
        payload = await response.json()
        if isinstance(payload, dict) and isinstance(payload.get("error"), dict):
            raw_code = payload["error"].get("code")
            code = raw_code if isinstance(raw_code, str) else ""
    except (ValueError, aiohttp.ContentTypeError):
        pass
    quota_exhausted = code in {
        "insufficient_quota", "quota_exceeded", "billing_hard_limit_reached",
        "billing_limit_reached", "usage_limit_reached",
    }
    if response.status == 429 or quota_exhausted:
        retry_after = response.headers.get("Retry-After", "")
        seconds = int(retry_after) if retry_after.isdigit() and len(retry_after) <= 6 else None
        raise GenerationLimitError(quota_exhausted=quota_exhausted, retry_after=seconds)
    if response.status in {400, 415, 422} and code in {
        "invalid_image", "invalid_image_format", "unsupported_image_format", "image_parse_error",
    }:
        raise UnsupportedPhotoFormatError("Фото не удалось прочитать. Используйте PNG, JPEG или WebP.")
    if response.status == 415:
        raise UnsupportedPhotoFormatError("Формат фото не поддерживается API.")
    raise ImageGenerationError(f"API генерации вернул ошибку HTTP {response.status}.")


async def _download_photo(session: aiohttp.ClientSession, photo_url: str) -> bytes:
    logger.info("HTTP GET: загрузка исходного фото.")
    async with session.get(photo_url, timeout=aiohttp.ClientTimeout(total=60)) as response:
        if response.status != 200:
            raise ImageGenerationError(f"Не удалось скачать фото: HTTP {response.status}.")
        data = bytearray()
        async for chunk in response.content.iter_chunked(64 * 1024):
            data.extend(chunk)
            if len(data) >= MAX_PHOTO_BYTES:
                raise ImageGenerationError("Фото должно быть меньше 50 МБ.")
    if not data:
        raise ImageGenerationError("Получено пустое фото.")
    return bytes(data)


def _photo_format(data: bytes) -> tuple[str, str]:
    if data.startswith(b"\x89PNG\r\n\x1a\n"):
        return "photo.png", "image/png"
    if data.startswith(b"\xff\xd8\xff"):
        return "photo.jpg", "image/jpeg"
    if data.startswith(b"RIFF") and data[8:12] == b"WEBP":
        return "photo.webp", "image/webp"
    raise UnsupportedPhotoFormatError("Фото должно быть в формате PNG, JPEG или WebP.")


def _validate_inputs(photo_url: str, sphere: str) -> None:
    if not isinstance(photo_url, str):
        raise TypeError("photo_url должен быть строкой.")
    parsed = urlsplit(photo_url)
    if parsed.scheme != "https" or not parsed.hostname or parsed.username or parsed.password:
        raise ValueError("photo_url должен быть HTTPS URL без учётных данных.")
    if not isinstance(sphere, str) or not 1 <= len(sphere.strip()) <= 200:
        raise ValueError("Сфера жизни должна содержать от 1 до 200 символов.")


async def _generate_image_bytes(
    photo_url: str, sphere: str, config: Config, count: int = 1
) -> list[bytes]:
    if not config.image_api_key or not config.image_model:
        raise GenerationNotConfigured("Задайте IMAGE_API_KEY и IMAGE_MODEL.")
    prompt = (
        "Создай вдохновляющий плакат — карту желаний — на основе приложенного фото. "
        f"Сфера жизни: {sphere.strip()}. "
        "Обязательные требования к портрету: keep the face exactly as in the original photo; "
        "natural head position; photorealistic portrait; do not change facial features. "
        "Сохрани исходный ракурс и положение головы, направление взгляда, выражение лица, "
        "возраст, пропорции лица и текстуру кожи. Не поворачивай и не наклоняй голову, "
        "не искажай шею и не стилизуй лицо. "
        "Приоритет — точное сохранение лица; меняй окружение и тематические детали, "
        "а не лицо или позу головы. "
        "Покажи его в позитивном образе желаемого будущего, связанном с этой сферой. "
        "Добавь тематические элементы коллажа, гармоничные цвета и выразительную композицию. "
        "Без надписей и водяных знаков."
    )
    try:
        async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=300)) as session:
            photo_bytes = await _download_photo(session, photo_url)
            filename, mime_type = _photo_format(photo_bytes)
            form = aiohttp.FormData()
            form.add_field("model", config.image_model)
            form.add_field("prompt", prompt)
            form.add_field("n", str(count))
            form.add_field("size", "1024x1536")
            form.add_field("output_format", "png")
            form.add_field("image", photo_bytes, filename=filename, content_type=mime_type)
            # Ключ отправляется только OpenAI, а не на сервер исходного фото.
            logger.info("HTTP POST OpenAI Images API: генерация %d изображений.", count)
            async with session.post(
                OPENAI_IMAGE_EDIT_URL,
                data=form,
                headers={"Authorization": f"Bearer {config.image_api_key}"},
                allow_redirects=False,
            ) as response:
                if response.status != 200:
                    await _raise_api_error(response)
                try:
                    payload = await response.json()
                except (ValueError, aiohttp.ContentTypeError) as exc:
                    raise ImageGenerationError("OpenAI вернул некорректный JSON.") from exc
    except asyncio.TimeoutError as exc:
        raise GenerationTimeoutError("Превышено время ожидания ответа API.") from exc
    except aiohttp.ClientError as exc:
        raise ImageGenerationError("Не удалось связаться с API генерации.") from exc

    try:
        items = payload["data"]
        if not isinstance(items, list) or len(items) != count:
            raise ValueError("Unexpected number of images")
        images = []
        for item in items:
            encoded = item["b64_json"]
            if not isinstance(encoded, str):
                raise ValueError("Invalid base64 type")
            image_bytes = base64.b64decode(encoded, validate=True)
            if not image_bytes.startswith(b"\x89PNG\r\n\x1a\n"):
                raise ValueError("Invalid PNG response")
            images.append(image_bytes)
    except (KeyError, IndexError, TypeError, ValueError, binascii.Error) as exc:
        raise ImageGenerationError("В ответе OpenAI нет корректного PNG-изображения.") from exc
    return images


def _storage_client(config: Config):
    """Подготовка хранилища до платного запроса генерации."""
    if not config.storage_bucket:
        raise GenerationNotConfigured("Задайте STORAGE_BUCKET.")
    if not config.storage_access_key_id or not config.storage_secret_access_key:
        raise GenerationNotConfigured("Задайте STORAGE_ACCESS_KEY_ID и STORAGE_SECRET_ACCESS_KEY.")
    for url in (config.storage_endpoint_url, config.storage_public_base_url):
        if url:
            parsed = urlsplit(url)
            if (
                parsed.scheme != "https" or not parsed.hostname
                or parsed.username or parsed.password or parsed.query or parsed.fragment
            ):
                raise GenerationNotConfigured("Адрес хранилища/CDN должен быть HTTPS URL без секретов.")
    # Явные ключи Yandex не зависят от AWS_PROFILE облачного окружения.
    botocore_session = BotocoreSession()
    botocore_session.get_component("config_store").set_config_variable("profile", None)
    session = boto3.Session(
        aws_access_key_id=config.storage_access_key_id,
        aws_secret_access_key=config.storage_secret_access_key,
        region_name=config.storage_region,
        botocore_session=botocore_session,
    )
    return session.client("s3", endpoint_url=config.storage_endpoint_url)


def _store_image(client, config: Config, image_bytes: bytes) -> str:
    key = f"vision-boards/{uuid4().hex}.png"
    logger.info("Запрос Yandex Object Storage: сохранение изображения (%d байт).", len(image_bytes))
    client.put_object(
        Bucket=config.storage_bucket, Key=key, Body=image_bytes, ContentType="image/png"
    )
    public_base = (
        config.storage_public_base_url
        or f"https://storage.yandexcloud.net/{quote(config.storage_bucket, safe='')}"
    )
    return f"{public_base.rstrip('/')}/{key}"


async def _generate_and_store(
    photo_url: str, sphere: str, count: int = 1
) -> list[str]:
    _validate_inputs(photo_url, sphere)
    config = Config.from_env()
    if not config.image_api_key or not config.image_model:
        raise GenerationNotConfigured("Задайте IMAGE_API_KEY и IMAGE_MODEL.")
    client = None
    try:
        client = await asyncio.to_thread(_storage_client, config)
        images = await _generate_image_bytes(photo_url, sphere, config, count=count)
        result_urls = []
        for image_bytes in images:
            result_url = await asyncio.to_thread(_store_image, client, config, image_bytes)
            if urlsplit(result_url).scheme != "https":
                raise ImageGenerationError("Хранилище вернуло ссылку без HTTPS.")
            result_urls.append(result_url)
        try:
            async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=30)) as session:
                for result_url in result_urls:
                    logger.info("HTTP HEAD: проверка публичной ссылки изображения.")
                    async with session.head(result_url) as response:
                        if response.status != 200:
                            raise ImageGenerationError(
                                "Плакат сохранён, но публичная ссылка недоступна. "
                                "Проверьте публичное чтение объектов в бакете/CDN."
                            )
        except asyncio.TimeoutError as exc:
            raise GenerationTimeoutError("Хранилище не ответило вовремя.") from exc
        except aiohttp.ClientError as exc:
            raise ImageGenerationError("Не удалось проверить публичную ссылку плаката.") from exc
        return result_urls
    except (ConnectTimeoutError, ReadTimeoutError) as exc:
        raise GenerationTimeoutError("Хранилище не ответило вовремя.") from exc
    except (BotoCoreError, ClientError) as exc:
        raise ImageGenerationError("Не удалось подключиться к хранилищу или сохранить плакат.") from exc
    finally:
        if client is not None:
            await asyncio.to_thread(client.close)


async def generate_vision_board(photo_url: str, sphere: str) -> str:
    """Создать плакат через OpenAI и вернуть постоянный публичный HTTPS URL.

    Входное фото скачивается по HTTPS и передаётся в OpenAI Images API вместе
    с промптом из сферы жизни. Ответ base64 декодируется и загружается
    в Yandex Object Storage. Бакет должен разрешать публичное чтение объектов.
    Для собственного CDN можно задать STORAGE_PUBLIC_BASE_URL.
    Генерация не повторяется автоматически, чтобы не создавать дубликаты.
    """
    return (await _generate_and_store(photo_url, sphere))[0]


async def generate_vision_boards(photo_url: str, sphere: str) -> list[str]:
    """Получить три варианта одним запросом n=3 и вернуть их публичные URL."""
    return await _generate_and_store(photo_url, sphere, count=3)


def image_from_url(url: str) -> Attachment:
    """Вложение image по публичному URL для MAX."""
    return Attachment(type=AttachmentType.IMAGE, payload=OtherAttachmentPayload(url=url))


async def generate_poster(photo_url: str, sphere: str) -> Attachment:
    """Фото по публичному URL для bot.send_message(attachments=[image])."""
    result_url = await generate_vision_board(photo_url, sphere)
    # Python-эквивалент Attachment::image(url): type=image, payload.url.
    return image_from_url(result_url)


class ImageService:
    def __init__(self, photo_path: Path) -> None:
        self.photo_path = photo_path

    async def send_photo(self, message: Message, caption: str) -> None:
        if not self.photo_path.is_file():
            raise FileNotFoundError("Изображение не найдено. Проверьте PHOTO_PATH.")

        await message.answer(
            text=caption,
            attachments=[InputMedia(str(self.photo_path), type=UploadType.IMAGE)],
        )

"""Загрузка в MAX и создание плакатов через OpenAI Images API."""

import asyncio
import base64
import binascii
import logging
from io import BytesIO
from pathlib import Path
from tempfile import TemporaryDirectory
from urllib.parse import quote, urlsplit
from uuid import uuid4

import aiohttp
import boto3
import httpx
from openai import AsyncOpenAI, APIConnectionError, APIStatusError, APITimeoutError
from botocore.exceptions import (
    BotoCoreError,
    ClientError,
    ConnectTimeoutError,
    ReadTimeoutError,
)
from botocore.session import Session as BotocoreSession
from maxapi import Bot
from maxapi.enums.upload_type import UploadType
from maxapi.types import AttachmentUpload, InputMedia, InputMediaBuffer, Message
from PIL import Image, ImageDraw, ImageFont, ImageOps

from config import Config
from max_client import create_max_bot, log_max_error

MAX_PHOTO_BYTES = 50 * 1024 * 1024
MAX_UPLOAD_BYTES = 1_000_000
OPENAI_BASE_URL = "https://api.openai.com/v1"
logger = logging.getLogger(__name__)

FONT_PATH = Path(__file__).resolve().parent / "font.ttf"
POSTCARD_PROMPTS = {
    "mode_ny": (
        "Beautiful New Year greeting card. Place the person from the reference photo in a cozy winter outfit. "
        "Add festive elements: Christmas tree, snow, garlands, gifts. Leave a large empty space at the bottom. "
        "If the person in the reference photo is wearing a summer hat, cap, or no hat, replace it with a stylish winter knitted hat (beanie) to match the snowy atmosphere. Keep the person's face exactly as in the reference."
    ),
    "mode_ny_family": (
        "Beautiful New Year family greeting card. Place all people from the reference photo together in a cozy winter atmosphere. "
        "Keep all faces exactly as in the reference. Add Christmas tree, snow, gifts. Leave a large empty space at the bottom. "
        "If the person in the reference photo is wearing a summer hat, cap, or no hat, replace it with a stylish winter knitted hat (beanie) to match the snowy atmosphere. Keep the person's face exactly as in the reference."
    ),
    "mode_bday_young": (
        "Vibrant Birthday greeting card. Place the young person from the reference photo in a celebratory pose "
        "with balloons, confetti, and a festive cake. Leave a large empty space at the bottom."
    ),
    "mode_bday_old": (
        "Elegant Birthday greeting card for a mature person (50-70 years old). Do NOT make the person look younger, "
        "keep natural wrinkles. Place them in a sophisticated, warm setting with elegant flowers, a cake, and soft lighting. "
        "Leave a large empty space at the bottom."
    ),
    "mode_8march": (
        "Beautiful 8 March greeting card. Place the woman from the reference photo surrounded by spring flowers, "
        "tulips, and mimosa. Warm spring lighting. Leave a large empty space at the bottom."
    ),
    "mode_goodmorning": (
        "Beautiful Good Morning greeting card. Warm sunrise lighting, coffee cup, soft morning atmosphere. "
        "Place the person from the photo looking fresh and happy. Leave a large empty space at the bottom."
    ),
}
MAX_CARD_TEXT_LENGTH = 500


def _postcard_font(size: int):
    try:
        return ImageFont.truetype(str(FONT_PATH), size=size)
    except OSError as exc:
        raise GenerationNotConfigured("Не удалось загрузить шрифт font.ttf для открытки.") from exc


def validate_card_text(text: str) -> None:
    if not isinstance(text, str) or not text.strip() or len(text) > MAX_CARD_TEXT_LENGTH:
        raise ValueError(f"Текст открытки должен содержать от 1 до {MAX_CARD_TEXT_LENGTH} символов.")


def _wrap_card_text(text: str, draw, font, max_width: float) -> str:
    lines = []
    for paragraph in text.split("\n"):
        line = ""
        for word in paragraph.split(" "):
            candidate = line + " " + word if line else word
            if line and draw.textlength(candidate, font=font) > max_width:
                lines.append(line)
                line = word
            else:
                line = candidate
        lines.append(line)
    return "\n".join(lines)


def add_text_to_image(image_bytes: bytes, text: str) -> bytes:
    """Наложить именно текст пользователя, с переносами и подбором размера."""
    validate_card_text(text)
    with Image.open(BytesIO(image_bytes)) as source:
        image = ImageOps.exif_transpose(source).convert("RGB")
    width, height = image.size
    draw = ImageDraw.Draw(image)
    low, high = 1, max(1, height // 15)
    fitted = None
    while low <= high:
        size = (low + high) // 2
        font = _postcard_font(size)
        stroke = max(1, size // 25)
        spacing = max(1, size // 5)
        wrapped = _wrap_card_text(text, draw, font, width * 0.9 - 2 * stroke)
        box = draw.multiline_textbbox((0, 0), wrapped, font=font, spacing=spacing,
                                      stroke_width=stroke, align="center")
        if box[2] - box[0] <= width * 0.9 and box[3] - box[1] <= height * 0.2:
            fitted = (font, stroke, spacing, wrapped, box)
            low = size + 1
        else:
            high = size - 1
    if fitted is None:
        raise ImageGenerationError("Размер открытки слишком мал для поздравления.")
    font, stroke, spacing, wrapped, (left, top, right, bottom) = fitted
    x = (width - (right - left)) / 2 - left
    y = height * 0.89 - (bottom - top) / 2 - top
    draw.multiline_text((round(x), round(y)), wrapped, font=font, fill="white", align="center",
                       spacing=spacing, stroke_width=stroke, stroke_fill="black")
    buffer = BytesIO()
    image.save(buffer, format="PNG")
    return buffer.getvalue()


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
    async with create_max_bot(config.token, auto_requests=False) as bot:
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
            await log_max_error(exc, logger)
            raise


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


def _raise_api_error(response: httpx.Response) -> None:
    """Записать полный ответ OpenAI в лог и классифицировать ошибку API."""
    body = response.text
    logger.error("OpenAI Images API error: HTTP %s; response body: %s", response.status_code, body)
    code = ""
    try:
        payload = response.json()
        if isinstance(payload, dict) and isinstance(payload.get("error"), dict):
            raw_code = payload["error"].get("code")
            code = raw_code if isinstance(raw_code, str) else ""
    except ValueError:
        pass
    quota_exhausted = code in {
        "insufficient_quota", "quota_exceeded", "billing_hard_limit_reached",
        "billing_limit_reached", "usage_limit_reached",
    }
    if response.status_code == 429 or quota_exhausted:
        retry_after = response.headers.get("Retry-After", "")
        seconds = int(retry_after) if retry_after.isdigit() and len(retry_after) <= 6 else None
        raise GenerationLimitError(quota_exhausted=quota_exhausted, retry_after=seconds)
    if response.status_code in {400, 415, 422} and code in {
        "invalid_image", "invalid_image_format", "unsupported_image_format", "image_parse_error",
    }:
        raise UnsupportedPhotoFormatError("Фото не удалось прочитать. Используйте PNG, JPEG или WebP.")
    if response.status_code == 415:
        raise UnsupportedPhotoFormatError("Формат фото не поддерживается API.")
    raise ImageGenerationError(f"API генерации вернул ошибку HTTP {response.status_code}.")


async def _download_photo(session: aiohttp.ClientSession, photo_url: str) -> bytes:
    logger.info("HTTP GET: загрузка изображения.")
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
        raise ValueError("Описание мечты должно содержать от 1 до 200 символов.")


async def _generate_image_bytes(
    photo_url: str, sphere: str, config: Config, count: int = 1, *, prompt_override: str | None = None
) -> list[bytes]:
    if not config.image_api_key or not config.image_model:
        raise GenerationNotConfigured("Задайте IMAGE_API_KEY и IMAGE_MODEL.")
    prompt = (
        "Create a realistic photographic vision board collage showing the person "
        "from the reference photo living their described dream.\n\n"

        "IDENTITY — HIGHEST PRIORITY:\n"
        "Use the uploaded person's photo as the identity reference. "
        "Preserve their individual facial proportions, face shape, nose, eyes, "
        "lips, jawline, skin tone, age, hair color, and hairstyle. "
        "Keep the original head angle and natural expression whenever possible. "
        "Do not replace their face with a similar-looking person or an idealized model. "
        "Do not beautify, reshape, smooth, or stylize their facial features. "
        "Adapt the scene and camera angle to the reference face, rather than "
        "changing the face to fit the scene. "
        "If a separate collage example is provided, use it only for layout and visual style, "
        "never as a source of facial identity.\n\n"

        "COLLAGE LAYOUT:\n"
        "Create one cohesive vertical collage with a large central photograph "
        "occupying approximately 60% of the composition. "
        "In this main photograph, show the person naturally participating in their dream, "
        "with their face clearly visible and large enough to recognize. "
        "Surround the main photograph with 5–7 smaller photographic panels "
        "showing relevant locations, objects, close-up details, and atmospheric moments "
        "from the same dream. "
        "Separate the panels with thin white borders and slightly angled edges, "
        "like an elegant editorial photo collage. "
        "Keep borders away from the person's face. "
        "Supporting panels should mainly show scenery and details, "
        "rather than repeating the person's portrait.\n\n"

        "PHOTOGRAPHIC REALISM:\n"
        "Every panel must look like a genuine camera photograph. "
        "Use believable lighting, natural skin texture, realistic materials, "
        "accurate reflections, and photographic depth of field. "
        "Make the composition vivid, inspiring, and visually rich, "
        "with harmonious colors and flattering natural light. "
        "Avoid illustration, painting, cartoon style, CGI, plastic skin, "
        "excessive retouching, and artificial HDR effects. "
        "Maintain correct anatomy, natural hands, and realistic body proportions.\n\n"

        "FOLLOW THE DREAM:\n"
        "Accurately represent the user's specific setting, activities, objects, "
        "colors, and other requested details. "
        "Choose supporting panels that reinforce this particular dream. "
        "Do not automatically add cars, yachts, or luxury accessories "
        "unless they are relevant to the description.\n\n"

        "NO TEXT:\n"
        "Do not add titles, captions, motivational quotes, letters, typography, "
        "watermarks, or decorative text. "
        "The result must be a photographic collage, not an advertising poster.\n\n"

        f"User's dream description: {sphere.strip()}"
    )
    if prompt_override is not None:
        prompt = prompt_override
    try:
        async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=60)) as session:
            photo_bytes = await _download_photo(session, photo_url)
        filename, mime_type = _photo_format(photo_bytes)
        logger.info("HTTP POST OpenAI Images API: генерация %d изображений.", count)
        logger.info(
            "Отправляю запрос с моделью: %s, качество: %s",
            config.image_model, config.image_quality,
        )
        logger.info("Эндпоинт API: %s/images/edits, quality: %s", OPENAI_BASE_URL, config.image_quality)
        async with httpx.AsyncClient(
            proxy=config.openai_proxy_url, timeout=300, trust_env=False, follow_redirects=False,
        ) as http_client:
            async with AsyncOpenAI(
                api_key=config.image_api_key, base_url=OPENAI_BASE_URL,
                http_client=http_client, max_retries=0,
            ) as client:
                raw = await client.images.with_raw_response.edit(
                    model=config.image_model, quality=config.image_quality,
                    prompt=prompt, size="1024x1536",
                    image=(filename, photo_bytes, mime_type),
                )
                response = raw.http_response
                if response.status_code != 200:
                    _raise_api_error(response)
                try:
                    payload = response.json()
                except ValueError as exc:
                    logger.error(
                        "OpenAI Images API invalid JSON: HTTP %s; response body: %s",
                        response.status_code, response.text,
                    )
                    raise ImageGenerationError("OpenAI вернул некорректный JSON.") from exc
    except APIStatusError as exc:
        _raise_api_error(exc.response)
    except (asyncio.TimeoutError, APITimeoutError) as exc:
        logger.error("Подробная ошибка OpenAI: %r; причина: %r", exc, exc.__cause__)
        raise GenerationTimeoutError("Превышено время ожидания ответа API.") from exc
    except (aiohttp.ClientError, APIConnectionError) as exc:
        logger.error("Подробная ошибка OpenAI: %r; причина: %r", exc, exc.__cause__)
        raise ImageGenerationError(f"Ошибка API: {exc}") from exc
    except ImageGenerationError:
        # HTTP-ответ уже записан в _raise_api_error; сохранить тип ошибки.
        raise
    except Exception as exc:
        logger.error("Подробная ошибка OpenAI: %r", exc)
        raise ImageGenerationError(f"Ошибка API: {exc}") from exc

    try:
        items = payload["data"]
        if not isinstance(items, list) or len(items) != count:
            raise ValueError("Unexpected number of images")
        images = []
        for item in items:
            if not isinstance(item, dict):
                raise ValueError("Invalid image response")
            if "b64_json" in item:
                encoded = item["b64_json"]
                if not isinstance(encoded, str):
                    raise ValueError("Invalid base64 type")
                image_bytes = base64.b64decode(encoded, validate=True)
            else:
                image_url = item["url"]
                if not isinstance(image_url, str):
                    raise ValueError("Invalid image URL")
                parsed = urlsplit(image_url)
                if parsed.scheme != "https" or not parsed.hostname or parsed.username or parsed.password:
                    raise ValueError("Invalid image URL")
                try:
                    # Отдельная сессия без ключа API: ссылка может вести на CDN.
                    async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=60)) as session:
                        image_bytes = await _download_photo(session, image_url)
                except asyncio.TimeoutError as exc:
                    raise GenerationTimeoutError("Превышено время скачивания результата API.") from exc
                except aiohttp.ClientError as exc:
                    raise ImageGenerationError("Не удалось скачать результат API.") from exc
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


def _store_image(client, config: Config, image_path: Path) -> str:
    key = f"vision-boards/{uuid4().hex}.png"
    logger.info("Запрос Yandex Object Storage: сохранение изображения (%d байт).", image_path.stat().st_size)
    with image_path.open("rb") as image_file:
        client.put_object(
            Bucket=config.storage_bucket, Key=key, Body=image_file, ContentType="image/png"
        )
    public_base = (
        config.storage_public_base_url
        or f"https://storage.yandexcloud.net/{quote(config.storage_bucket, safe='')}"
    )
    return f"{public_base.rstrip('/')}/{key}"


async def _generate_and_store(
    photo_url: str, sphere: str, count: int = 1, *, prompt_override: str | None = None,
    postcard_text: str | None = None
) -> list[str]:
    _validate_inputs(photo_url, sphere if prompt_override is None else "Открытка")
    config = Config.from_env()
    if not config.image_api_key or not config.image_model:
        raise GenerationNotConfigured("Задайте IMAGE_API_KEY и IMAGE_MODEL.")
    if postcard_text is not None:
        validate_card_text(postcard_text)
        # Проверить наличие шрифта до платного запроса к модели.
        await asyncio.to_thread(_postcard_font, 16)
    client = None
    try:
        client = await asyncio.to_thread(_storage_client, config)
        kwargs = {"prompt_override": prompt_override} if prompt_override is not None else {}
        images = await _generate_image_bytes(photo_url, sphere, config, count=count, **kwargs)
        result_urls = []
        for image_bytes in images:
            if postcard_text is not None:
                image_bytes = await asyncio.to_thread(add_text_to_image, image_bytes, postcard_text)
            with TemporaryDirectory(prefix="botmax-images-") as directory:
                image_path = Path(directory) / "generated.png"
                await asyncio.to_thread(image_path.write_bytes, image_bytes)
                result_url = await asyncio.to_thread(_store_image, client, config, image_path)
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
    с описанием мечты в промпте. Результат из b64_json или url сохраняется
    во временный файл и загружается в Yandex Object Storage; файл удаляется
    после загрузки или ошибки. Бакет должен разрешать публичное чтение объектов.
    Для собственного CDN можно задать STORAGE_PUBLIC_BASE_URL.
    Генерация не повторяется автоматически, чтобы не создавать дубликаты.
    """
    return (await _generate_and_store(photo_url, sphere))[0]


async def generate_vision_boards(photo_url: str, sphere: str) -> list[str]:
    """Получить одно изображение (n=1), сохранив интерфейс списка URL."""
    return await _generate_and_store(photo_url, sphere, count=1)


async def generate_postcards(photo_url: str, mode: str, card_text: str) -> list[str]:
    """Создать открытку по режиму, наложить поздравление и сохранить в S3."""
    if mode not in POSTCARD_PROMPTS:
        raise ValueError(f"Неизвестный режим открытки: {mode}")
    validate_card_text(card_text)
    prompt = (
        POSTCARD_PROMPTS[mode] + "\n\nPreserve every reference person's identity, facial features, "
        "hairstyle, age and skin tone. Do not remove or add people. Use photorealistic photography, "
        "vivid harmonious colors and natural anatomy. Keep the bottom 22% empty for a greeting "
        "added later by software; keep faces outside this area. Do not add any text or watermarks."
    )
    return await _generate_and_store(photo_url, "Открытка", prompt_override=prompt, postcard_text=card_text)


def _prepare_max_image(data: bytes) -> tuple[bytes, str]:
    """Сохранить небольшое изображение или сжать большую копию для MAX."""
    filename, _ = _photo_format(data)
    if len(data) <= MAX_UPLOAD_BYTES:
        return data, filename
    with Image.open(BytesIO(data)) as source:
        image = ImageOps.exif_transpose(source).convert("RGBA")
        background = Image.new("RGB", image.size, "white")
        background.paste(image, mask=image.getchannel("A"))
    while True:
        for quality in (90, 80, 70):
            buffer = BytesIO()
            background.save(buffer, format="JPEG", quality=quality, optimize=True)
            result = buffer.getvalue()
            if len(result) <= MAX_UPLOAD_BYTES:
                logger.info("Сжатие для MAX: %d → %d байт.", len(data), len(result))
                return result, "poster.jpg"
        background = background.resize(
            (max(1, background.width * 3 // 4), max(1, background.height * 3 // 4)),
            Image.Resampling.LANCZOS,
        )


async def upload_image_for_max(bot: Bot, image_url: str) -> AttachmentUpload:
    """Скачать результат из S3 и загрузить байты в MAX, получив media token."""
    _validate_inputs(image_url, "poster")
    try:
        async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=60)) as session:
            data = await _download_photo(session, image_url)
    except asyncio.TimeoutError as exc:
        raise GenerationTimeoutError("Не удалось вовремя скачать плакат для MAX.") from exc
    except aiohttp.ClientError as exc:
        raise ImageGenerationError("Не удалось скачать плакат для MAX.") from exc
    data, filename = await asyncio.to_thread(_prepare_max_image, data)
    logger.info("Запрос MAX API: загрузка плаката (%d байт).", len(data))
    attachment = await bot.upload_media(
        InputMediaBuffer(buffer=data, filename=filename, type=UploadType.IMAGE)
    )
    token = attachment.payload.token
    if not isinstance(token, str) or not token.strip():
        raise ImageGenerationError("MAX вернул пустой media token.")
    return attachment


async def generate_poster(photo_url: str, sphere: str) -> AttachmentUpload:
    """Создать плакат и вернуть вложение с media token MAX."""
    result_url = await generate_vision_board(photo_url, sphere)
    config = Config.from_env()
    async with create_max_bot(config.token, auto_requests=False) as bot:
        return await upload_image_for_max(bot, result_url)


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

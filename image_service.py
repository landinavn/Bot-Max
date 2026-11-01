"""Загрузка в MAX и создание плакатов через OpenAI Images API."""

import asyncio
import base64
import binascii
import logging
import re
import textwrap
from contextlib import asynccontextmanager
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
from PIL import Image, ImageDraw, ImageFont, ImageOps, ImageStat

from config import Config
from max_client import create_max_bot, log_max_error

MAX_PHOTO_BYTES = 50 * 1024 * 1024
MAX_UPLOAD_BYTES = 1_000_000
OPENAI_BASE_URL = "https://api.openai.com/v1"
logger = logging.getLogger(__name__)

FONT_PATH = Path(__file__).resolve().parent / "font.ttf"
MAX_THEME_INFO_LENGTH = 500
FACE_ORIENTATION_REQUIREMENT = (
    "FACE ORIENTATION: If the person in the reference photo is looking sideways or in profile, "
    "turn their head and face to look directly at the camera. The eyes must look straight ahead."
)
POSTCARD_PROMPTS = {
    "mode_teacher": (
        "Create a beautiful, elegant greeting card for a teacher. The background and thematic elements "
        "must be directly related to the following subject: {teacher_info}. Preserve the person's face "
        "exactly as in the reference photo. Add elegant typography elements. DO NOT ADD text unless specified."
    ),
    "mode_coach": (
        "Create a beautiful, dynamic greeting card for a sports coach. The background and thematic elements "
        "must be directly related to the following sport: {coach_info}. Preserve the person's face "
        "exactly as in the reference photo. Add elegant typography elements. DO NOT ADD text unless specified."
    ),
    "mode_ny": (
        "Beautiful New Year greeting card. Place the person from the reference photo in a cozy winter outfit. "
        "Add festive elements: Christmas tree, snow, garlands, gifts. Leave a large empty space at the bottom. "
        "If the person in the reference photo is wearing a summer hat, cap, or no hat, replace it with a stylish winter knitted hat (beanie) to match the snowy atmosphere. Keep the person's face exactly as in the reference."
    ),
    "mode_ny_family": (
        "Replace headwear with a winter hat ONLY if the person in the reference photo is wearing a hat/cap, or if it's a child. "
        "Otherwise, keep the original hairstyle. Place all people from the reference photo together in a cozy winter atmosphere. "
        "Keep all faces exactly as in the reference. Add Christmas tree, snow, gifts. Leave a large empty space at the bottom."
    ),
    "mode_bday_old_elegant": (
        "Elegant Birthday card for a mature person (50+). Keep natural age, wrinkles, and facial features. "
        "Use soft lighting, elegant flowers, and minimal decorations. Leave empty space for text."
    ),
    "mode_halloween": (
        "Cozy Halloween card with pumpkins, autumn leaves, and warm fairy lights. Keep it cute and not scary. "
        "Preserve the person's face exactly. Leave empty space for text."
    ),
    "mode_ny_cozy": (
        "Cozy New Year greeting card. Warm fireplace, soft blankets, fairy lights, hot cocoa. "
        "Place the person from the reference photo in a cozy winter sweater. Keep the face exactly as in the reference. Leave empty space for text."
    ),
    "mode_ny_fairytale": (
        "Fairy-tale winter greeting card. Snow-covered pine trees, ice castle in the background, magical glowing lights. "
        "Place the person in an elegant winter coat. Keep the face exactly as in the reference. Leave empty space for text."
    ),
    "mode_ny_elegant": (
        "Elegant New Year party greeting card. Champagne glasses, golden decorations, festive bokeh. "
        "Place the person in a stylish evening outfit. Keep the face exactly as in the reference. Leave empty space for text."
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
    "mode_modern_card": (
        "Aesthetic scrapbook style greeting card. The reference photo is placed in the center or slightly to the right with torn paper edges. "
        "The background is a soft, blurred autumn or spring cityscape with warm lighting. "
        "Add beautiful decorative elements: drawn hearts, brush strokes, soft pastel colors, subtle doodles. "
        "Keep the person's face EXACTLY as in the reference photo. "
        "Leave plenty of empty space on the left side or at the top for custom text. Cinematic lighting, highly detailed."
    ),
}
POSTCARD_PROMPTS = {
    mode: prompt + ' ' + FACE_ORIENTATION_REQUIREMENT
    for mode, prompt in POSTCARD_PROMPTS.items()
}

MAX_CARD_TEXT_LENGTH = 200


def _postcard_font(size: int):
    try:
        return ImageFont.truetype(str(FONT_PATH), size=size)
    except OSError as exc:
        raise GenerationNotConfigured("Не удалось загрузить шрифт font.ttf для открытки.") from exc


def add_preview_watermark(image_bytes: bytes) -> bytes:
    """Полупрозрачный PREVIEW по центру; исходные байты не меняются."""
    with Image.open(BytesIO(image_bytes)) as source:
        image = source.convert('RGBA')
    overlay = Image.new('RGBA', image.size, (0, 0, 0, 0))
    draw = ImageDraw.Draw(overlay)
    size = max(1, int(min(image.width * 0.15, image.height * 0.25)))
    try:
        font = _postcard_font(size)
    except GenerationNotConfigured:
        font = ImageFont.load_default(size=size)
    bbox = draw.textbbox((0, 0), 'PREVIEW', font=font)
    width, height = bbox[2] - bbox[0], bbox[3] - bbox[1]
    if width > image.width * 0.9:
        size = max(1, int(size * image.width * 0.9 / width))
        try:
            font = _postcard_font(size)
        except GenerationNotConfigured:
            font = ImageFont.load_default(size=size)
        bbox = draw.textbbox((0, 0), 'PREVIEW', font=font)
        width, height = bbox[2] - bbox[0], bbox[3] - bbox[1]
    position = ((image.width - width) / 2 - bbox[0], (image.height - height) / 2 - bbox[1])
    draw.text(position, 'PREVIEW', font=font, fill=(255, 255, 255, 110),
              stroke_width=max(1, size // 30), stroke_fill=(0, 0, 0, 65))
    output = BytesIO()
    Image.alpha_composite(image, overlay).save(output, format='PNG')
    return output.getvalue()


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


_EMOJI_RE = re.compile(
    r"[0-9#*]\ufe0f?\u20e3|[\U0001F000-\U0001FAFF\u2600-\u27BF"
    r"\u00A9\u00AE\u203C\u2049\u2122\u2139\u2194-\u2199\u21A9\u21AA"
    r"\u231A\u231B\u2328\u23CF\u23E9-\u23F3\u23F8-\u23FA\u24C2"
    r"\u25AA\u25AB\u25B6\u25C0\u25FB-\u25FE\u2934\u2935\u2B05-\u2B07"
    r"\u2B1B\u2B1C\u2B50\u2B55\u3030\u303D\u3297\u3299\u200D\uFE0E\uFE0F\u20E3]"
)


def add_text_to_image(image_bytes: bytes, text: str, mode: str | None = None) -> bytes:
    """Наложить текст; при ошибке эмодзи повторить отрисовку без них."""
    validate_card_text(text)
    try:
        return _render_card_text(image_bytes, text, mode)
    except (UnicodeError, OSError, ValueError):
        clean_text = _EMOJI_RE.sub("", text)
        if clean_text == text:
            raise
        logger.warning("Шрифт не смог отрисовать эмодзи; повторяю наложение без эмодзи.")
        # Новая отрисовка начинается с исходника, без следов неудачной попытки.
        return _render_card_text(image_bytes, clean_text, mode)


def _render_card_text(image_bytes: bytes, text: str, mode: str | None) -> bytes:
    with Image.open(BytesIO(image_bytes)) as source:
        image = ImageOps.exif_transpose(source).convert("RGB")
    if not text.strip():
        buffer = BytesIO()
        image.save(buffer, format="PNG")
        return buffer.getvalue()
    width, height = image.size
    draw = ImageDraw.Draw(image)
    modern = mode == "mode_modern_card"
    if modern:
        # Короткие строки как в скрапбукинге, включая очень длинные слова.
        text = "\n".join(
            "\n".join(textwrap.wrap(paragraph, width=28, break_long_words=True,
                                   break_on_hyphens=False, replace_whitespace=False))
            if paragraph else "" for paragraph in text.split("\n")
        )
    max_width = width * (0.37 if modern else 0.9)
    max_height = height * (0.7 if modern else 0.2)
    align = "left" if modern else "center"
    low, high = 1, max(1, height // (24 if modern else 15))
    probe = _postcard_font(high)
    missing = probe.getmask("\U0010FFFF")
    for char in text:
        if _EMOJI_RE.fullmatch(char) and char not in "\u200d\ufe0e\ufe0f":
            glyph = probe.getmask(char)
            if glyph.size == missing.size and bytes(glyph) == bytes(missing):
                # FreeType часто рисует пустой квадрат вместо исключения.
                raise UnicodeError("Шрифт не поддерживает эмодзи.")
    fitted = None
    while low <= high:
        size = (low + high) // 2
        font = _postcard_font(size)
        stroke = max(1, size // 25)
        spacing = max(1, size // (3 if modern else 5))
        wrapped = _wrap_card_text(text, draw, font, max_width - 2 * stroke)
        box = draw.multiline_textbbox((0, 0), wrapped, font=font, spacing=spacing,
                                      stroke_width=stroke, align=align)
        if box[2] - box[0] <= max_width and box[3] - box[1] <= max_height:
            fitted = (font, stroke, spacing, wrapped, box)
            low = size + 1
        else:
            high = size - 1
    if fitted is None:
        raise ImageGenerationError("Размер открытки слишком мал для поздравления.")
    font, stroke, spacing, wrapped, (left, top, right, bottom) = fitted
    x = width * 0.05 - left if modern else (width - (right - left)) / 2 - left
    y = height * (0.5 if modern else 0.89) - (bottom - top) / 2 - top
    region = ((int(width * 0.05), int(height * 0.15), max(1, int(width * 0.42)), max(1, int(height * 0.85)))
              if modern else (0, int(height * 0.78), width, height))
    brightness = ImageStat.Stat(image.crop(region).convert("L")).mean[0]
    light_background = brightness >= 150
    draw.multiline_text((round(x), round(y)), wrapped, font=font,
                       fill="#2C3E50" if light_background else "#FFFFFF", align=align,
                       spacing=spacing, stroke_width=stroke,
                       stroke_fill="white" if light_background else "black")
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


async def fix_text_with_ai(text: str) -> str:
    """Исправить поздравление через тот же OpenAI endpoint и прокси, что и фото."""
    validate_card_text(text)
    config = Config.from_env()
    if not config.image_api_key:
        raise GenerationNotConfigured("Не задан IMAGE_API_KEY для коррекции текста.")
    instruction = (
        "Ты профессиональный корректор. Исправь орфографические и грамматические "
        "ошибки в следующем тексте для открытки, но сохрани смысл и имена. "
        "Сохрани все эмодзи и переносы строк. "
        "Верни ТОЛЬКО исправленный текст без кавычек и комментариев:"
    )
    try:
        async with httpx.AsyncClient(
            proxy=config.openai_proxy_url, timeout=60, trust_env=False, follow_redirects=False,
        ) as http_client:
            async with AsyncOpenAI(
                api_key=config.image_api_key, base_url=OPENAI_BASE_URL,
                http_client=http_client, max_retries=0,
            ) as client:
                result = await client.chat.completions.create(
                    model="gpt-4o-mini", temperature=0,
                    messages=[
                        {"role": "system", "content": instruction},
                        {"role": "user", "content": text},
                    ],
                )
    except APIStatusError as exc:
        logger.error("Коррекция текста OpenAI: HTTP %s; ответ: %s", exc.status_code, exc.response.text)
        _raise_api_error(exc.response)
    except APITimeoutError as exc:
        raise GenerationTimeoutError("OpenAI не успел исправить текст.") from exc
    except APIConnectionError as exc:
        logger.error("Ошибка соединения OpenAI при коррекции текста: %s", exc)
        raise ImageGenerationError("Не удалось подключиться к OpenAI для коррекции текста.") from exc
    if not result.choices or result.choices[0].finish_reason != "stop":
        raise ImageGenerationError("OpenAI не вернул полное исправленное поздравление.")
    fixed_text = result.choices[0].message.content
    try:
        validate_card_text(fixed_text)
    except ValueError as exc:
        raise ImageGenerationError("OpenAI вернул некорректный текст поздравления.") from exc
    return fixed_text.strip()


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


async def count_people_in_photo(image_url: str) -> int:
    """Посчитать людей через vision gpt-4o-mini; неопределённый результат — 0."""
    try:
        _validate_inputs(image_url, 'Подсчёт людей')
        config = Config.from_env()
        if not config.image_api_key:
            logger.warning('Подсчёт людей недоступен: не задан IMAGE_API_KEY. Возвращаю 0.')
            return 0
        async with httpx.AsyncClient(
            proxy=config.openai_proxy_url, timeout=60, trust_env=False, follow_redirects=False,
        ) as http_client:
            async with AsyncOpenAI(api_key=config.image_api_key, base_url=OPENAI_BASE_URL,
                                   http_client=http_client, max_retries=0) as client:
                result = await client.chat.completions.create(model='gpt-4o-mini', temperature=0, max_tokens=10,
                    messages=[{'role': 'user', 'content': [
                        {'type': 'text', 'text': "Count the number of people in this image. Return ONLY the number, nothing else. If you can't count, return 0."},
                        {'type': 'image_url', 'image_url': {'url': image_url, 'detail': 'auto'}},
                    ]}])
        if result.choices and result.choices[0].finish_reason == 'stop':
            text = result.choices[0].message.content
            if isinstance(text, str) and re.fullmatch(r'[0-9]+', text.strip()):
                return int(text.strip())
        logger.warning('OpenAI не вернул корректное число людей. Возвращаю 0.')
    except Exception as exc:
        logger.warning('Не удалось подсчитать людей (%s). Возвращаю 0.', type(exc).__name__)
    return 0


@asynccontextmanager
async def _image_api_client(config: Config):
    """Одинаковые прокси и таймаут для карт, открыток и каждого кадра фотосессии."""
    logger.info(
        "OpenAI Images: прокси %s, таймаут 300.0 с",
        "настроен" if config.openai_proxy_url else "не задан",
    )
    async with httpx.AsyncClient(
        proxy=config.openai_proxy_url, timeout=300.0, trust_env=False, follow_redirects=False,
    ) as http_client:
        async with AsyncOpenAI(
            api_key=config.image_api_key, base_url=OPENAI_BASE_URL,
            http_client=http_client, max_retries=0,
        ) as client:
            yield client


def _photosession_style_references(style_reference_url=None, style_reference_urls=None):
    if style_reference_url is not None and style_reference_urls is not None:
        raise ValueError('Укажите один способ передачи образцов стиля.')
    if style_reference_urls is not None:
        if not isinstance(style_reference_urls, (list, tuple)) or not 1 <= len(style_reference_urls) <= 2:
            raise ValueError('Нужны один или два предыдущих кадра.')
        references = tuple(style_reference_urls)
    else:
        references = (style_reference_url,) if style_reference_url is not None else ()
    for url in references:
        _validate_inputs(url, 'Фотосессия')
    return references


async def _generate_image_bytes(
    photo_url: str, sphere: str, config: Config, count: int = 1, *, prompt_override: str | None = None,
    style_reference_url: str | None = None, style_reference_urls: tuple[str, ...] | None = None,
    image_size: str = '1024x1536'
) -> list[bytes]:
    if image_size not in ('1024x1536', '1536x1024'):
        raise ValueError('Неподдерживаемый формат кадра.')
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
    references = _photosession_style_references(style_reference_url, style_reference_urls)
    try:
        async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=60)) as session:
            photo_bytes = await _download_photo(session, photo_url)
            style_images = [await _download_photo(session, url) for url in references]
        filename, mime_type = _photo_format(photo_bytes)
        image_input = (filename, photo_bytes, mime_type)
        if style_images:
            style_files = []
            for index, style_bytes in enumerate(style_images):
                style_name, style_mime = _photo_format(style_bytes)
                style_files.append((f'style-{index}-' + style_name, style_bytes, style_mime))
            image_input = [image_input, *style_files]
        logger.info("HTTP POST OpenAI Images API: генерация %d изображений.", count)
        logger.info(
            "Отправляю запрос с моделью: %s, качество: %s",
            config.image_model, config.image_quality,
        )
        logger.info("Эндпоинт API: %s/images/edits, quality: %s", OPENAI_BASE_URL, config.image_quality)
        async with _image_api_client(config) as client:
            raw = await client.images.with_raw_response.edit(
                model=config.image_model, quality=config.image_quality,
                prompt=prompt, size=image_size,
                image=image_input,
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
    postcard_text: str | None = None, postcard_mode: str | None = None, preview_watermark: bool = False,
    style_reference_url: str | None = None, style_reference_urls: tuple[str, ...] | None = None,
    image_size: str | None = None
) -> list[str]:
    _validate_inputs(photo_url, sphere if prompt_override is None else "Открытка")
    _photosession_style_references(style_reference_url, style_reference_urls)
    if image_size is not None and image_size not in ('1024x1536', '1536x1024'):
        raise ValueError('Неподдерживаемый формат кадра.')
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
        if style_reference_url is not None:
            kwargs['style_reference_url'] = style_reference_url
        if style_reference_urls is not None:
            kwargs['style_reference_urls'] = tuple(style_reference_urls)
        if image_size is not None:
            kwargs['image_size'] = image_size
        images = await _generate_image_bytes(photo_url, sphere, config, count=count, **kwargs)
        result_urls = []
        for image_bytes in images:
            if postcard_text is not None:
                image_bytes = await asyncio.to_thread(add_text_to_image, image_bytes, postcard_text, mode=postcard_mode)
            if preview_watermark:
                image_bytes = await asyncio.to_thread(add_preview_watermark, image_bytes)
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


async def generate_postcards(photo_url: str, mode: str, card_text: str, *, theme_info: str | None = None, preview: bool = False) -> list[str]:
    """Создать открытку по режиму, наложить поздравление и сохранить в S3."""
    if mode not in POSTCARD_PROMPTS:
        raise ValueError(f"Неизвестный режим открытки: {mode}")
    validate_card_text(card_text)
    base_prompt = POSTCARD_PROMPTS[mode]
    if mode in {'mode_teacher', 'mode_coach'}:
        if not isinstance(theme_info, str) or not theme_info.strip() or len(theme_info) > MAX_THEME_INFO_LENGTH:
            raise ValueError(f'Напишите имя и предмет или спорт — от 1 до {MAX_THEME_INFO_LENGTH} символов.')
        base_prompt = base_prompt.format(teacher_info=theme_info.strip(), coach_info=theme_info.strip())
    modern = mode == "mode_modern_card"
    layout = (
        "Keep the left 42% of the image light, uncluttered and free of people or decoration, "
        "for text added later by software. Place the reference portrait on the right, outside this area. "
        "Keep the portrait photorealistic; decorative elements may be hand-drawn. "
        if modern else
        "Use photorealistic photography, vivid harmonious colors and natural anatomy. "
        "Keep the bottom 22% empty for a greeting added later by software; keep faces outside this area. "
    )
    prompt = (
        base_prompt + "\n\nPreserve every reference person's identity, facial features, "
        "hairstyle, age and skin tone. Do not remove or add people. "
        + layout + "Do not add any text or watermarks."
    )
    kwargs = {'preview_watermark': True} if preview else {}
    return await _generate_and_store(photo_url, "Открытка", prompt_override=prompt, postcard_text=card_text, postcard_mode=mode, **kwargs)


PHOTOSESSION_BODY_ANATOMY_REQUIREMENT = (
    "CRITICAL BODY & ANATOMY REQUIREMENT:  - Preserve the EXACT body type, weight, and proportions of the "
    "people as seen in the reference photo. Do NOT slim or idealize their figures.  - If the reference "
    "photo is a selfie (only showing the face/upper body), generate the lower body naturally and "
    "proportionally to the visible upper body.  - Ensure each person has exactly 2 arms, 2 legs, and "
    "exactly 5 fingers on each hand. No extra or missing limbs. "
    "HEAD POSITION: You may naturally change the head turn, tilt and gaze from the reference photo to suit "
    "the scene, pose, camera angle and interaction. Follow any explicit gaze direction specified for this frame. "
    "Keep the person recognizable, preserving their facial features, age and proportions. Maintain a realistic "
    "head-to-neck connection, natural neck rotation and alignment with the shoulders. No extreme twists, "
    "unnatural head angles, stretched necks or distorted faces."
)

NY_FAMILY_THREE_PROMPTS = (
    (
        "Create a highly realistic, professional New Year family photoshoot. The image must feature EXACTLY "
        "the same 3 people from the reference photo. Preserve their exact faces, hairstyles, skin tones, and "
        "ages perfectly.  CLOTHING: Dress all 3 people in matching red and green tartan plaid pajamas.  "
        "BACKGROUND: Cozy interior, beautifully decorated Christmas tree with gold and white ornaments.  POSE "
        "& CAMERA ANGLE: Full-length wide shot. The 3 people are sitting together on the floor in front of the "
        "tree, smiling and looking at the camera. Their entire bodies, from head to toe, must be fully visible "
        "in the frame, not just a close-up of their faces.  Exactly 3 people. "
        + PHOTOSESSION_BODY_ANATOMY_REQUIREMENT
        + " DO NOT ADD ANY TEXT."
    ),
    (
        "Create a highly realistic, professional New Year family photoshoot. The image must feature EXACTLY "
        "the same 3 people from the reference photo. Preserve their exact faces perfectly.  CLOTHING: Same "
        "matching red and green tartan plaid pajamas.  BACKGROUND: A large, beautifully decorated Christmas "
        "tree with glowing lights and ornaments must be clearly visible in the frame, surrounding them.  POSE "
        "& CAMERA ANGLE: Top-down bird's-eye view, as if the camera is mounted on the ceiling looking directly "
        "down at them. The 3 people are lying on the floor together on a cozy rug. Waist-up shot, upper bodies "
        "visible. They are looking up at the camera and smiling.  "
        + PHOTOSESSION_BODY_ANATOMY_REQUIREMENT
        + " DO NOT ADD ANY TEXT."
    ),
    (
        "Create a highly realistic, professional New Year family photoshoot. The image must feature EXACTLY "
        "the same 3 people from the reference photo. Preserve their exact faces perfectly.  CLOTHING: Same "
        "matching red and green tartan plaid pajamas.  BACKGROUND: Same cozy interior with the decorated "
        "Christmas tree.  POSE & CAMERA ANGLE: Waist-up shot. Only the upper half of their bodies is visible "
        "in the frame. If the reference photo has 2 adults and 1 child, the adults are gently kissing the "
        "child on the cheeks. If the reference photo has 1 adult and 2 children, the adult is hugging and "
        "kissing the children.  Exactly 3 people. "
        + PHOTOSESSION_BODY_ANATOMY_REQUIREMENT
        + " DO NOT ADD ANY TEXT."
    ),
)


NY_FAMILY_PLUS_PROMPTS = (
    (
        "Create a highly realistic, professional New Year photoshoot. Focus ONLY on the children from the "
        "reference photo. Do NOT include the adults.  CLOTHING: IGNORE the clothing in the reference photo. "
        "Dress boys in smart casual button-down shirts (no jackets) and trousers. Dress girls in simple, "
        "elegant dresses.  BACKGROUND: The children are lying on a cozy rug right next to a beautifully "
        "decorated Christmas tree, surrounded by glowing lights and wrapped gifts.  POSE: The children are "
        "lying on the floor, looking at the camera.  "
        + PHOTOSESSION_BODY_ANATOMY_REQUIREMENT
        + " DO NOT ADD ANY TEXT."
    ),
    (
        "Create a highly realistic, professional New Year photoshoot. The image must feature EXACTLY the same "
        "people from the reference photo (3 or more people).  CLOTHING: IGNORE the clothing in the reference "
        "photo. Men and boys MUST wear smart casual button-down shirts (no jackets), trousers, and stylish "
        "sneakers. Girls and women MUST wear simple, elegant dresses.  BACKGROUND: Cozy interior with a "
        "beautifully decorated Christmas tree.  POSE: Full-length wide shot. The whole family is together. "
        "Some are standing elegantly, others are sitting. Everyone is looking at the camera.  "
        + PHOTOSESSION_BODY_ANATOMY_REQUIREMENT
        + " DO NOT ADD ANY TEXT."
    ),
    (
        "Create a highly realistic, professional New Year photoshoot. Focus ONLY on the adult parents (man and "
        "woman) from the reference photo.  CLOTHING: IGNORE the clothing in the reference photo. The man MUST "
        "wear a smart casual button-down shirt (no jacket) and trousers. The woman MUST wear a simple, elegant "
        "dress.  BACKGROUND: Cozy interior with a beautifully decorated Christmas tree.  POSE: Full-length "
        "shot. The parents are sitting elegantly together on a sofa. CRITICAL: The woman's legs and feet must "
        "be positioned naturally and elegantly.  "
        + PHOTOSESSION_BODY_ANATOMY_REQUIREMENT
        + " DO NOT ADD ANY TEXT."
    ),
)
NY_FAMILY_TODDLERS_PROMPTS = (
    (
        "Create a highly realistic, professional New Year family photoshoot in a vintage, rustic style. The "
        "image must feature EXACTLY the same people from the reference photo. Preserve their exact faces and "
        "body proportions perfectly.  CLOTHING: IGNORE the clothing in the reference photo. Dress the adults "
        "in elegant cream and beige knit sweaters. Dress the children in cozy, warm beige winter outfits.  "
        "BACKGROUND: A beautifully decorated Christmas tree with vintage ornaments, warm golden lighting, "
        "wooden floor, and antique wooden toys.  POSE: Full-length shot. The adults are standing elegantly "
        "near the tree, holding the toddlers safely in their arms.  "
        + PHOTOSESSION_BODY_ANATOMY_REQUIREMENT
        + " DO NOT ADD ANY TEXT."
    ),
    (
        "Create a highly realistic, professional New Year family photoshoot in a vintage, rustic style. The "
        "image must feature EXACTLY the same people from the reference photo.  CLOTHING: IGNORE the clothing "
        "in the reference photo. The adults MUST be wearing elegant cream and beige knit sweaters. The "
        "children MUST be wearing cozy, warm beige winter outfits (the exact same outfits as described "
        "before).  BACKGROUND: In front of the decorated tree on a wooden floor.  POSE: Medium close-up. The "
        "whole family is sitting together on the floor. The adults are holding the toddlers close.  "
        + PHOTOSESSION_BODY_ANATOMY_REQUIREMENT
        + " DO NOT ADD ANY TEXT."
    ),
    (
        "Create a highly realistic, professional New Year photoshoot in a vintage, rustic style. Focus ONLY on "
        "the children from the reference photo.  CLOTHING: IGNORE the clothing in the reference photo. The "
        "children MUST be wearing cozy, warm beige winter outfits.  BACKGROUND: On a cozy rug right next to "
        "the tree, surrounded by warm lights and vintage wooden toys.  POSE: The toddler/baby is LYING on "
        "their back or tummy. If there is an older child, they are SITTING next to the toddler.  "
        + PHOTOSESSION_BODY_ANATOMY_REQUIREMENT
        + " DO NOT ADD ANY TEXT."
    ),
)
MOM_DAUGHTER_IDENTITY = (
    "Create a highly realistic, professional editorial New Year photoshoot. Use the client's uploaded "
    "reference photo ONLY for the identities and real body proportions of the mother and her daughter. "
    "Preserve their exact facial features, skin tones, natural ages, hair colors and hairstyles, weight, "
    "body types and relative height. Do NOT slim, elongate, beautify, age or de-age either person. "
    "Do not transfer the appearance of any other model. IGNORE the client's original clothing, pose and background. "
)
MOM_DAUGHTER_SCENE = (
    "STYLE & PALETTE: Fashion editorial Christmas photography, vertical portrait, natural skin texture, "
    "crisp direct-flash highlights combined with cool window daylight and warm golden fairy-light bokeh. "
    "A very large dark-green Christmas tree with gold and ivory baubles, warm tiny lights, muted grey walls, "
    "tall windows, pale rug and terrazzo floor. Olive/chartreuse green, ivory, gold and neutral grey palette. "
)
MOM_DAUGHTER_MOTHER_CLOTHING = (
    "MOTHER'S CLOTHING: An olive/chartreuse green satin floor-length halter-neck evening dress, "
    "sleeveless with a high softly gathered neckline and natural fabric folds, silver earrings and stacked silver bracelets. "
    "When feet are visible, delicate silver strappy heels. Use this exact outfit and color in every frame. "
)
MOM_DAUGHTER_DAUGHTER_CLOTHING = (
    "DAUGHTER'S CLOTHING: An age-appropriate sleeveless ivory/white dress with soft feather-like fringe and "
    "subtle iridescent texture. Preserve her real age and proportions; never give a child an adult body. "
    "Use this exact outfit and color in each shared frame. "
)
MOM_DAUGHTER_ANATOMY = (
    PHOTOSESSION_BODY_ANATOMY_REQUIREMENT +
    " Check the anatomy of every visible person before finalizing: each arm must connect to its own shoulder, "
    "each leg to its own hip, and each hand to its own wrist. Exactly two hands per person, five fingers per hand. "
    "No merged hands, detached hands, duplicated fingers, extra arms or legs. Keep naturally occluded limbs "
    "occluded; do not invent additional visible hands to explain an embrace. "
)
NY_MOM_DAUGHTER_PROMPTS = (
    MOM_DAUGHTER_IDENTITY + MOM_DAUGHTER_SCENE + MOM_DAUGHTER_MOTHER_CLOTHING + MOM_DAUGHTER_DAUGHTER_CLOTHING +
    "PEOPLE: EXACTLY TWO people, only the client's mother and daughter. "
    "POSE & CAMERA: Intimate vertical medium portrait at eye level. The mother is seated in the foreground, "
    "her daughter stands closely behind and gently embraces her shoulders, leaning her head toward her mother's head. "
    "Both have peaceful soft smiles and naturally open eyes, looking toward the camera. Keep both people's eyes open. "
    "The daughter's two hands rest separately on the "
    "mother's shoulders; the mother's hands remain naturally distinct from the daughter's. "
    "Crop around the mother's waist, showing the daughter's upper body and white textured dress behind. "
    "The illuminated tree fills the background with softly blurred golden lights. "
    + MOM_DAUGHTER_ANATOMY + "DO NOT ADD ANY TEXT.",
    MOM_DAUGHTER_IDENTITY + MOM_DAUGHTER_SCENE + MOM_DAUGHTER_MOTHER_CLOTHING + MOM_DAUGHTER_DAUGHTER_CLOTHING +
    "PEOPLE: EXACTLY TWO people, only the client's mother and daughter. "
    "POSE & CAMERA: Vertical seated environmental portrait from a low eye-level camera. "
    "Both sit together on the pale rug in front of the tree, mother behind and to the left, daughter slightly "
    "in front, sitting close beside her. The mother leans back on one supporting arm, with her other arm relaxed "
    "along her own bent knee; the daughter supports herself naturally with her own hands. "
    "The mother looks upward to frame-left in three-quarter profile with a gentle smile. The daughter looks "
    "up at her mother, smiling. Include their seated bodies, the mother's flowing green skirt, the daughter's "
    "white fringed dress and naturally positioned legs extending toward frame-left. Do not make limbs overlap impossibly. "
    + MOM_DAUGHTER_ANATOMY + "DO NOT ADD ANY TEXT.",
    MOM_DAUGHTER_IDENTITY + MOM_DAUGHTER_SCENE + MOM_DAUGHTER_MOTHER_CLOTHING +
    "PEOPLE: EXACTLY ONE person: ONLY the mother from the client's reference. Do NOT include her daughter "
    "or any other person in this frame. "
    "POSE & CAMERA: Vertical full-length fashion portrait, entire body from head to shoes visible, "
    "camera slightly below chest level. The mother stands beside a warm terracotta-orange wall on frame-right; "
    "a tall Christmas tree and large cool-daylight windows are behind her on frame-left. "
    "One arm is raised with that hand resting lightly on the top of her head and a clearly connected elbow; "
    "the other arm hangs relaxed alongside her body. She looks upward to frame-left in three-quarter profile. "
    "Her legs and feet are naturally balanced, one foot slightly in front of the other, no twisted ankles. "
    "Show the full olive satin dress and silver heels on the terrazzo floor. Preserve her real build and leg length. "
    + MOM_DAUGHTER_ANATOMY + "DO NOT ADD ANY TEXT.",
)
RABBIT_IDENTITY = (
    "Create a highly realistic professional New Year family photograph, vertical portrait. "
    "Use the client's uploaded photo as the ONLY authority for the identities of the mother and her son. "
    "Preserve each included person's exact facial features, skin tone, natural age, hair color and hairstyle, "
    "real weight, body type and body proportions. Do not slim, elongate, beautify, age or de-age anyone. "
    "IGNORE the original clothing and background. Do not copy the appearance of any other model. "
)
RABBIT_SCENE = (
    "STYLE, LIGHTING & PALETTE: The same cozy Scandinavian Christmas interior throughout the photoshoot. "
    "Large bright windows with sheer white curtains, soft winter daylight, warm tiny golden tree lights, "
    "cream and ivory walls, wooden floor and a muted red-blue-beige patterned vintage rug with a pale knitted rug nearby. "
    "A natural green Christmas tree with ivory and muted red ornaments, small wrapped presents and a soft blue-grey armchair. "
    "Airy, gentle photographic light with detailed natural skin, soft shadows and shallow depth of field. "
    "Keep identical cream, warm beige, pale ice-blue, muted grey, natural green and warm gold color grading in all frames. "
)
RABBIT_MOTHER_CLOTHING = (
    "MOTHER'S OUTFIT: A cozy ivory-white fluffy long-sleeved knit sweater with a modest neckline, "
    "cream relaxed trousers and neutral cream socks. Use exactly this same outfit, color and texture in every frame where she appears. "
)
RABBIT_SON_CLOTHING = (
    "SON'S OUTFIT: A cream high-neck winter knit sweater with a clear pale ice-blue and muted grey Nordic "
    "snowflake/Fair Isle pattern, light beige trousers and cream knitted socks. Preserve his real age and "
    "body proportions, never give a child an adult body. Keep exactly the same outfit and pattern in frames 2 and 3. "
)
RABBIT_ANIMAL = (
    "RABBIT: EXACTLY ONE calm, medium-sized domestic cinnamon-brown rabbit with a cream muzzle and chest, "
    "long upright ears and dark eyes, a tiny loose dusty-pink ribbon at its neck. Keep this rabbit's exact "
    "fur color, markings, size and ribbon identical in all three frames. No additional animals. "
    "The rabbit must have natural rabbit anatomy: four paws, two ears, no human hands or fingers. "
    "Support its body gently and securely; do not squeeze it or hold it by its ears. "
)
RABBIT_ANATOMY = (
    PHOTOSESSION_BODY_ANATOMY_REQUIREMENT +
    " Count HUMAN limbs separately from the rabbit's paws. Each human arm must connect to its own shoulder, "
    "each hand to its own wrist, each leg to its own hip. No merged hands, detached limbs or duplicated fingers. "
    "Keep naturally hidden limbs hidden, rather than inventing additional visible hands during contact with the rabbit. "
)
NY_RABBIT_PROMPTS = (
    RABBIT_IDENTITY + RABBIT_SCENE + RABBIT_MOTHER_CLOTHING + RABBIT_ANIMAL +
    "PEOPLE: EXACTLY ONE human, ONLY the mother from the client photo. Do NOT include her son or anyone else. "
    "POSE & CAMERA: Low floor-level vertical medium portrait. The mother lies on her stomach on the rug "
    "beside the Christmas tree, resting on her forearms. The single rabbit sits securely on the rug directly "
    "in front of her. Her two hands gently support its body from either side without covering its face. "
    "Her head is turned slightly toward frame-left in three-quarter profile, eyes naturally open and a soft smile. "
    "The tree and wrapped presents occupy frame-left; a bright sheer-curtained window and blurred blue-grey armchair are behind. "
    + RABBIT_ANATOMY + "DO NOT ADD ANY TEXT.",
    RABBIT_IDENTITY + RABBIT_SCENE + RABBIT_SON_CLOTHING + RABBIT_ANIMAL +
    "PEOPLE: EXACTLY ONE human, ONLY the son from the client photo. Do NOT include his mother or anyone else. "
    "POSE & CAMERA: Vertical environmental portrait from the boy's seated chest level, side view. "
    "He sits on the patterned rug beside the large bright window, legs naturally extended toward frame-left. "
    "The rabbit rests securely across his lap and the rug. His two existing hands gently pet and support the rabbit, "
    "one on its back and the other at its side, with distinct correctly connected wrists. "
    "He looks down at the rabbit with naturally open eyes and a tender expression. Show his cream and pale-blue "
    "Nordic sweater, beige trousers and socked feet; keep a few Christmas details at the edges of the bright window scene. "
    + RABBIT_ANATOMY + "DO NOT ADD ANY TEXT.",
    RABBIT_IDENTITY + RABBIT_SCENE + RABBIT_MOTHER_CLOTHING + RABBIT_SON_CLOTHING + RABBIT_ANIMAL +
    "PEOPLE: EXACTLY TWO humans, ONLY the mother and son from the client photo. "
    "POSE & CAMERA: Vertical wide seated family portrait at low eye level in front of the decorated Christmas tree. "
    "Mother and son sit comfortably close together on the same patterned rug, their whole seated bodies and feet visible. "
    "The SAME single brown rabbit rests between them on a supported lap. Mother supports its body with one hand "
    "and rests her other hand on her own knee; the son gently pets its back with one hand and rests his other hand on his own knee. "
    "Both people look toward the camera with naturally open eyes and warm smiles. Keep their hands spatially distinct "
    "from each other and from the rabbit's paws. The tree, presents and softly lit window remain clearly visible behind them. "
    "Use the mother's outfit from the earlier mother frame and the son's outfit from the earlier son frame, with identical fabrics and colors. "
    + RABBIT_ANATOMY + "DO NOT ADD ANY TEXT.",
)
NY_FAMILY_PHOTOSESSION_PROMPTS = {
    "fam3": NY_FAMILY_THREE_PROMPTS,
    "fam4": NY_FAMILY_PLUS_PROMPTS,
    "toddlers": NY_FAMILY_TODDLERS_PROMPTS,
    "mom_daughter": NY_MOM_DAUGHTER_PROMPTS,
    "rabbit": NY_RABBIT_PROMPTS,
}

FIVE_MOM_IDENTITY = (
    "Create a highly realistic professional fashion photograph of the mother and daughter from the client's "
    "uploaded photo. Use ONLY their exact identities, natural ages, facial features, skin tones, hair colors, "
    "body types, weight and proportions. Do not slim, elongate, idealize, age or de-age them. "
    "IGNORE the client's original clothes and background. Keep children age-appropriate. "
    "NO HEADWEAR on anyone: no hats, kokoshniks, crowns, headbands, bows, horns or costume ears. "
    "Preserve their own hair color and length. All visible eyes are naturally open, never closed. "
)
FIVE_MOM_ROOM = (
    "SETTING & COLOR: A classic Christmas studio with warm ivory-grey wall panels, shallow curved steps, "
    "a large dark-green wreath high behind the people, two natural fir trees framing them, tiny warm lights, "
    "silver tinsel, red and gold baubles, wrapped white/red/gold gifts with red ribbons, red velvet curtains "
    "at the sides and a pale floor. Consistent rich festive red, ivory and deep green palette, soft frontal "
    "photographic light, natural skin texture and realistic contrast. Same location and lighting in frames 1 and 3. "
)
FIVE_MOM_ROOM_CLOTHES = (
    "MATCHING OUTFITS FOR FRAMES 1 AND 3: Mother wears an ivory textured long-sleeved knit top, "
    "high-waisted bright-red wide-leg floor-length trousers and pale gold low heels. Daughter wears a rich-red "
    "velvet short puff-sleeved blouse with large round silver buttons, flowing cream satin wide-leg trousers "
    "and dark burgundy Mary Jane flats. Keep every fabric, color and accessory identical across frames 1 and 3. "
)
FIVE_MOM_PINK_STUDIO = (
    "SETTING & COLOR: Smooth seamless coral/rose-red studio backdrop and floor, soft even frontal studio "
    "light with natural skin texture, photographic realism and no Christmas-tree scenery in these candy portraits. "
    "Keep the same backdrop color and candy appearance in frames 2, 4 and 5. "
)
FIVE_MOM_GIRL_CANDY_CLOTHES = (
    "DAUGHTER'S EXACT OUTFIT FOR FRAMES 2 AND 5: A modest opaque ivory-white blouse with a high standing "
    "lace collar, delicate lace bib/trim, long sleeves and small pale buttons, with long glossy red bead necklaces. "
    "Where the lower body is visible, age-appropriate black tailored shorts, opaque red tights and black lace-up ankle boots. "
    "No leather jacket over the blouse. Keep her blouse, collar, sleeves, beads and colors identical in frames 2 and 5. "
)
FIVE_MOM_ROOSTER = (
    "PROP: EXACTLY ONE large translucent ruby-red caramel lollipop sculpted in the recognizable shape of a "
    "traditional ROOSTER, including its comb and curved tail, on a straight wooden stick. Not a round or heart-shaped candy. "
    "Keep the same red rooster design, color and size in frames 2, 4 and 5. The candy may occlude an eye as "
    "specified, but never deform or replace the face or eyeball. "
)
FIVE_MOM_ANATOMY = (
    PHOTOSESSION_BODY_ANATOMY_REQUIREMENT +
    " Each person's hands and wrists must remain distinct, correctly attached to their own arms and shoulders. "
    "No merged hands, duplicated fingers, extra limbs, disconnected wrists or twisted feet. "
    "Do not invent visible limbs where they are naturally occluded. DO NOT ADD ANY TEXT."
)
NY_MOM_DAUGHTER_FIVE_PROMPTS = (
    FIVE_MOM_IDENTITY + FIVE_MOM_ROOM + FIVE_MOM_ROOM_CLOTHES +
    "PEOPLE: EXACTLY TWO people, only mother and daughter. POSE & CAMERA: Horizontal wide environmental "
    "portrait at seated eye level, their complete seated bodies and feet visible between both trees. "
    "They sit close together on the pale floor, legs folded naturally to the side, mother to frame-right and "
    "daughter to frame-left. Mother gently holds her daughter near the shoulder with one hand; other hands "
    "rest naturally on their own laps. BOTH turn their faces forward, look directly at the CAMERA and smile warmly. "
    "Do not turn them toward each other, do not kiss, and do not close their eyes. No lollipop in this frame. "
    + FIVE_MOM_ANATOMY,
    FIVE_MOM_IDENTITY + FIVE_MOM_PINK_STUDIO + FIVE_MOM_GIRL_CANDY_CLOTHES + FIVE_MOM_ROOSTER +
    "PEOPLE: EXACTLY ONE person, only the daughter. POSE & CAMERA: Tight vertical frontal portrait from chest "
    "to above the head, at eye level. She looks directly into the camera with a calm natural expression. "
    "Her hand holds the wooden stick below the candy; the large red rooster occupies frame-left and covers "
    "one eye, while the OTHER eye stays naturally open and clearly visible. Keep her facial features intact. "
    "Show the white high lace collar, red beads and natural uncovered hair. "
    + FIVE_MOM_ANATOMY,
    FIVE_MOM_IDENTITY + FIVE_MOM_ROOM + FIVE_MOM_ROOM_CLOTHES +
    "PEOPLE: EXACTLY TWO people, only mother and daughter. POSE & CAMERA: Vertical full-length portrait, "
    "both complete bodies from head to shoes visible. They stand close together in a gentle side embrace "
    "between the Christmas trees, mother to frame-right and daughter to frame-left, daughter leaning slightly "
    "toward her mother. Their arms wrap naturally around each other's back and waist with distinct hands. "
    "Both look toward the camera with relaxed warm expressions and naturally open eyes. The wreath, steps "
    "and red-ribbon gifts are clearly visible. Use exactly the outfits established in frame 1. No lollipop here. "
    + FIVE_MOM_ANATOMY,
    FIVE_MOM_IDENTITY + FIVE_MOM_PINK_STUDIO + FIVE_MOM_ROOSTER +
    "MOTHER'S OUTFIT: A plain opaque white long-sleeved blouse/top, natural uncovered hair, no headwear. "
    "PEOPLE: EXACTLY ONE person, only the mother. POSE & CAMERA: Tight vertical frontal portrait, head and "
    "upper torso visible. Mother looks directly at the camera with a soft natural expression, no exaggerated lips. "
    "She holds the wooden stick with one hand below the large rooster candy. The candy occupies frame-right, "
    "covering ONE eye; the other eye remains open and visible. Keep the same red rooster and coral backdrop as frame 2. "
    + FIVE_MOM_ANATOMY,
    FIVE_MOM_IDENTITY + FIVE_MOM_PINK_STUDIO + FIVE_MOM_GIRL_CANDY_CLOTHES + FIVE_MOM_ROOSTER +
    "MOTHER'S OUTFIT: An opaque black long-sleeved top, long red gloves, red bead necklace, a white/ivory lace "
    "midi skirt with an opaque lining and black ankle boots. No horns, headband or any other headwear. "
    "PEOPLE: EXACTLY TWO people, only mother and daughter. POSE & CAMERA: Vertical full-body side-view "
    "portrait. Mother crouches comfortably at frame-left so her face is near the daughter's height; daughter "
    "stands at frame-right. They face each other with playful happy expressions, gently sticking out their "
    "tongues toward opposite sides of the SAME single rooster lollipop between them. Daughter holds the "
    "wooden stick with one hand; mother's gloved hand steadies it lower down, with clearly separate hands "
    "and wrists. Their other hands rest naturally on their own knee or at their side. "
    "Daughter wears EXACTLY her white lace-collar blouse and red beads from frame 2, with red tights, "
    "black shorts and boots; do not replace this outfit with a black leather jacket. Show both complete bodies and feet. "
    + FIVE_MOM_ANATOMY,
)
RED_WOMAN_IDENTITY = (
    "Create a photorealistic professional Christmas fashion portrait of EXACTLY ONE woman from the FIRST, "
    "original client photo. Preserve her recognizable facial identity, facial structure, age, skin tone, "
    "eye color, hair color, height, build and real body proportions. Never copy the reference model's face "
    "or body. Apply light, tasteful festive makeup and a subtle fresh-looking skin retouch, retaining "
    "natural skin texture and age. Do NOT add red lipstick unless red lipstick is already present in the "
    "original client photo; otherwise keep natural lip color. If her hair is loose, give it modest festive "
    "volume or soft curls without changing its length or color. If her hair is arranged in an updo or "
    "another hairstyle, keep that hairstyle and make it neater and more polished. Keep the same finished "
    "hair and makeup across the entire set. For a fuller figure, gently smooth skin folds and use flattering "
    "fabric drape and natural lighting, while preserving her actual body size, weight distribution and "
    "proportions; do not make her thin or reshape her body. No invented tattoos, no extra people. "
)
RED_WOMAN_CHRISTMAS_ROOM = (
    "SETTING: Warm ivory/beige classical wall panels, a very large dark evergreen circular wreath centered "
    "high on the wall with tiny warm fairy lights and red/gold ornaments, hanging fine silver garlands "
    "and snowflakes, cream semicircular steps with narrow gold edging, dark green Christmas trees at the "
    "sides, red, white and metallic-gold gift boxes with satin ribbons and large red baubles. "
    "Palette: rich Christmas red, ivory, dark evergreen and muted warm gold; soft warm editorial lighting. "
)
RED_WOMAN_TROUSERS = (
    "OUTFIT: High-waisted bright red tailored wide-leg palazzo trousers with front pleats, a tucked-in "
    "shimmering ivory/champagne asymmetric knit top with one long sleeve and one bare shoulder, light "
    "champagne pointed-toe heels, gold earrings and a gold wristwatch. If the woman has a very full figure, "
    "replace the one-sleeve top with a matching elegant ivory/champagne top with TWO long sleeves and "
    "covered shoulders. Keep whichever sleeve design is chosen IDENTICAL in frames 1 and 2. "
)
RED_WOMAN_SATIN_DRESS = (
    "OUTFIT: A festive vivid crimson/cherry-red glossy satin evening dress, softly draped neckline, "
    "thin silver jeweled straps and a natural thigh slit, silver crystal necklace, sparkly silver "
    "accessories and red embellished heels. This exact same satin dress, color, cut, straps and jewelry "
    "must be used in frames 3 and 4. No jacket, trousers or white skirt. "
)
RED_WOMAN_CURTAIN = (
    "SETTING: A rich deep burgundy-red velvet curtain with vertical folds as the entire studio backdrop; "
    "glamorous direct-flash editorial lighting, rich red and silver tones, gently falling white artificial "
    "snow in the foreground without obscuring the face. No Christmas room, shelves, fireplace or trees. "
)
RED_WOMAN_ANATOMY = (
    PHOTOSESSION_BODY_ANATOMY_REQUIREMENT +
    " Exactly one head, two anatomically correct arms, two hands and two legs belonging to this woman; "
    "five fingers per hand, natural shoulders, wrists, knees and ankles. No duplicated, fused or detached "
    "limbs, no invented fingers or visible limbs behind occlusions. Keep all requested poses comfortable "
    "and physically possible for her real body. DO NOT ADD ANY TEXT. "
)
NY_RED_WOMAN_PROMPTS = (
    RED_WOMAN_IDENTITY + RED_WOMAN_CHRISTMAS_ROOM + RED_WOMAN_TROUSERS +
    "POSE & CAMERA: Vertical three-quarter portrait from above the head to the lower legs, woman standing "
    "in front of the wreath and steps. Her sleeved arm curves gently above her head, hand relaxed near "
    "her hair; other hand rests on her hip. Body angled slightly, head turned naturally toward frame-left "
    "with a soft confident expression and open eyes. Preserve the reference composition and camera height. "
    + RED_WOMAN_ANATOMY,
    RED_WOMAN_IDENTITY + RED_WOMAN_CHRISTMAS_ROOM + RED_WOMAN_TROUSERS +
    "POSE & CAMERA: Vertical full-length environmental portrait. Woman sits sideways on the cream "
    "semicircular steps with her red trouser legs extended diagonally toward the lower-right; both shoes "
    "visible. One forearm rests on a white gift box with a red ribbon at frame-left; the other hand rests "
    "on her thigh. She looks up naturally toward frame-right with a happy smile. Large wreath above, "
    "Christmas tree at frame-right, gifts around the steps. Match the exact outfit established in frame 1. "
    + RED_WOMAN_ANATOMY,
    RED_WOMAN_IDENTITY + RED_WOMAN_CURTAIN + RED_WOMAN_SATIN_DRESS +
    "POSE & CAMERA: Vertical seated fashion portrait, three-quarter body, the woman sitting on a low "
    "dark stool, knees angled toward frame-left, torso gently turned toward the camera, open eyes and "
    "a soft smile. One hand raised near her head holds silver tinsel and a violet Christmas bauble; "
    "other hand rests naturally near her hip holding the other end of the SAME tinsel with a violet "
    "bauble. Tinsel drapes loosely along her arms and lap, never replaces fingers or limbs. "
    + RED_WOMAN_ANATOMY,
    RED_WOMAN_IDENTITY + RED_WOMAN_SATIN_DRESS +
    "SETTING: The modern dark Christmas living room from reference 4: charcoal walls, a black marble "
    "mantel/fireplace at frame-left with fine pale veining, a tall Christmas tree decorated with dark red "
    "and gold ornaments, illuminated horizontal shelves with small frames and candles, round crystal "
    "ceiling lamp, dark upholstered seating and a small wreath at frame-right. Warm restrained lighting. "
    "POSE & CAMERA: Vertical full-length standing portrait, complete figure and shoes visible. One hand "
    "rests lightly on the black marble mantel at frame-left, other hand rests naturally at her hip; "
    "head turns toward frame-right with a relaxed expression. Wear EXACTLY the red satin dress and "
    "accessories established in frame 3; do not copy the gray jacket or white skirt of reference 4. "
    + RED_WOMAN_ANATOMY,
    RED_WOMAN_IDENTITY + RED_WOMAN_CURTAIN +
    "OUTFIT: The dress from reference 5: rich red velvet fitted short party dress with a wide "
    "off-shoulder neckline and two large sculptural puff sleeves, plus broad polished gold cuff bracelets. "
    "Do NOT use the thin-strapped satin dress from frame 3 in this frame. "
    "POSE & CAMERA: Vertical seated three-quarter portrait, body angled toward frame-left. Both forearms "
    "are raised naturally forward, two separate open palms face upward at chest level as she playfully "
    "blows or tosses artificial snow. She looks upward toward frame-left, smiling. Preserve the pose and "
    "velvet dress from reference 5 but use EXACTLY the burgundy curtain setting from frame 3. "
    + RED_WOMAN_ANATOMY,
)
NY_PHOTOSESSION_PROMPTS_BY_TARIFF = {
    590: NY_FAMILY_PHOTOSESSION_PROMPTS,
    990: {"mom_daughter_5": NY_MOM_DAUGHTER_FIVE_PROMPTS, "woman_red": NY_RED_WOMAN_PROMPTS},
}


CARTOON_THEME_IMAGE_BASE = (
    'Transform the person from the reference photo into a 3D animated movie character in the style of Pixar/Disney. '
    'STYLE REQUIREMENTS (Absolute Priority): '
    '- 3D rendered cartoon style, smooth stylized skin, big expressive eyes, slightly exaggerated cute facial features. '
    '- The character must look like a high-quality 3D animation, NOT a real photograph. '
    'IDENTITY REQUIREMENTS: '
    '- Despite the cartoon style, the character must be 100% recognizable as the person in the reference photo. '
    '- Keep the exact same hair color, hairstyle, and overall facial structure. '
    + FACE_ORIENTATION_REQUIREMENT + ' SCENE & BACKGROUND: - '
)
CARTOON_IMAGE_BACKGROUNDS = {
    'theme_ny': 'Christmas tree with glowing fairy lights, gentle falling snow, magical winter atmosphere.',
    'theme_bday': 'Colorful balloons, festive confetti, warm glowing candles, birthday party.',
    'theme_bday_kid': "Soft pastel balloons, cute toys, gentle warm lighting, cheerful kids' party.",
    'theme_valentine': 'Floating red and pink hearts, rose petals, soft romantic pink lighting.',
    'theme_feb23': 'A gently waving St. George ribbon, a softly glowing red star, subtle falling snow, patriotic atmosphere.',
    'theme_mar8': 'Spring tulips and mimosa flowers, warm spring sunlight, gentle petals.',
    'theme_mother': 'Warm golden hour lighting, gentle floating soft particles (like dandelion fluff), very cozy and tender atmosphere.',
    'theme_father': 'Warm workshop lighting, subtle tools in the background, strong and caring atmosphere.',
}

CARTOON_CLOTHING = {
    'theme_ny': 'CLOTHING: Cozy winter sweater.',
    'theme_bday': 'CLOTHING: Elegant party outfit.',
    'theme_bday_kid': 'CLOTHING: Cute colorful kids outfit.',
    'theme_valentine': 'CLOTHING: Romantic elegant outfit.',
    'theme_feb23': 'CLOTHING: Stylized military uniform or smart tactical shirt (no weapons, just clothing).',
    'theme_mar8': 'CLOTHING: Elegant spring dress.',
    'theme_mother': 'CLOTHING: Cozy, warm, tender clothing.',
    'theme_father': 'CLOTHING: Casual smart clothes (polo or button-down shirt).',
}
CARTOON_CLOTHING_STYLE = (
    'IGNORE the clothing in the reference photo. All clothing must be stylized 3D Pixar/Disney animation, '
    'NOT photorealistic clothing. '
)
CARTOON_IMAGE_PROMPTS = {
    theme: CARTOON_THEME_IMAGE_BASE.removesuffix('SCENE & BACKGROUND: - ')
        + CARTOON_CLOTHING_STYLE + CARTOON_CLOTHING[theme] + ' SCENE & BACKGROUND: - ' + background
    for theme, background in CARTOON_IMAGE_BACKGROUNDS.items()
}



CARTOON_AVATAR_PROMPT = (
    CARTOON_THEME_IMAGE_BASE
    + 'Use a soft blurred Christmas tree background. '
    'Keep the same people, without adding or removing anyone. Preserve their age and skin tone. '
    'No text, captions or watermarks.'
)
CARTOON_AVATAR_THEMES = {
    'mode_ny': 'Classic festive New Year styling.',
    'mode_ny_family': 'Cozy family New Year styling.',
    'mode_ny_cozy': 'Cozy knitwear and warm festive lights.',
    'mode_ny_fairytale': 'Magical winter fairy-tale styling.',
    'mode_ny_elegant': 'Elegant festive evening styling.',
    'mode_bday_young': 'Bright cheerful birthday styling.',
    'mode_bday_old': 'Warm mature birthday styling, preserve natural age.',
    'mode_bday_old_elegant': 'Understated elegant birthday styling, preserve natural age.',
    'mode_modern_card': 'Soft pastel scrapbook decorative accents.',
    'mode_halloween': 'Cute cozy autumn pumpkin accents, not scary.',
    'mode_goodmorning': 'Warm gentle morning lighting and soft colors.',
    'mode_8march': 'Soft spring floral accents.',
}


async def generate_cartoon_avatar(photo_url: str, mode: str) -> str:
    if mode not in CARTOON_AVATAR_THEMES and mode not in CARTOON_IMAGE_PROMPTS:
        raise ValueError('Неизвестный стиль мульт-аватара.')
    urls = await _generate_and_store(photo_url, 'Мульт-аватар', count=1,
        prompt_override=(CARTOON_IMAGE_PROMPTS[mode] if mode in CARTOON_IMAGE_PROMPTS else
                         CARTOON_AVATAR_PROMPT + ' ' + CARTOON_AVATAR_THEMES[mode]))
    if len(urls) != 1:
        raise ImageGenerationError('Ожидался один мульт-аватар.')
    return urls[0]


async def generate_photosession_frame(photo_url: str, prompt: str, *, style_reference_url: str | None = None,
                                      style_reference_urls: tuple[str, ...] | None = None,
                                      style_reference_scope: str = 'all',
                                      image_size: str | None = None) -> str:
    """Один /images/edits через общий клиент с OPENAI_PROXY_URL и таймаутом 300 с.

    Результат сохраняется отдельным файлом в S3, без надписи Pillow.
    """
    if not isinstance(prompt, str) or not prompt.strip():
        raise ValueError("Не задан промпт фотосессии.")
    if style_reference_scope not in ('all', 'wardrobe', 'setting'):
        raise ValueError('Неизвестная роль образца фотосессии.')
    kwargs = {}
    if image_size is not None:
        if image_size not in ('1024x1536', '1536x1024'):
            raise ValueError('Неподдерживаемый формат кадра.')
        kwargs['image_size'] = image_size
    references = _photosession_style_references(style_reference_url, style_reference_urls)
    if style_reference_url is not None:
        kwargs['style_reference_url'] = style_reference_url
    if style_reference_urls is not None:
        kwargs['style_reference_urls'] = tuple(style_reference_urls)
    if references:
        style_roles = ('The SECOND image is the first generated frame: match '
                       'its exact outfits, fabric colors, jewelry, lighting, color grading and location. '
                       if len(references) == 1 else
                       'The SECOND and THIRD images are earlier generated frames: match each included person\'s '
                       'corresponding outfit, colors and accessories, and the same animal, lighting and location. ')
        if style_reference_scope == 'wardrobe':
            style_roles = ('The SECOND image is an earlier generated frame: match ONLY its exact outfit, '
                           'fabric, color, accessories, finished hairstyle and makeup. Do NOT copy its '
                           'background, lighting or pose; use the setting specified in this frame prompt. ')
        elif style_reference_scope == 'setting':
            style_roles = ('The SECOND image is an earlier generated frame: match ONLY its background, '
                           'lighting, color grading, finished hairstyle and makeup. Do NOT copy its '
                           'outfit or pose; use the new clothing specified in this frame prompt. ')
        prompt += (' INPUT ROLES: The FIRST image is the original client photo: use it as the authority for '
                   'identity, age and body proportions. ' + style_roles +
                   'If a person is absent from a style image, follow the outfit specified for that person '
                   'in this prompt; never put another person\'s clothes on them. '
                   'Do not copy previous poses or framing; follow the new frame instructions and the required '
                   'number of people for this frame. DO NOT ADD ANY TEXT.')
    urls = await _generate_and_store(photo_url, "Фотосессия", count=1, prompt_override=prompt, **kwargs)
    if len(urls) != 1:
        raise ImageGenerationError("Ожидался один кадр фотосессии.")
    return urls[0]


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


async def create_cartoon_avatar_preview(original_image_url: str) -> str:
    """Отдельная копия с PREVIEW: чистый оригинал остаётся для Kling."""
    _validate_inputs(original_image_url, 'Мульт-аватар')
    config = Config.from_env()
    async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=60)) as session:
        data = await _download_photo(session, original_image_url)
    marked = await asyncio.to_thread(add_preview_watermark, data)
    client = await asyncio.to_thread(_storage_client, config)
    try:
        with TemporaryDirectory(prefix='cartoon-preview-') as directory:
            path = Path(directory)/'preview.png'
            await asyncio.to_thread(path.write_bytes, marked)
            url = await asyncio.to_thread(_store_image, client, config, path)
        if urlsplit(url).scheme != 'https':
            raise ImageGenerationError('Превью аватара не получило HTTPS-ссылку.')
        return url
    finally:
        await asyncio.to_thread(client.close)

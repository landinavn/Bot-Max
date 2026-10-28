"""Проверки MAX/OpenAI с моками HTTP и хранилища, без реальных ключей."""

import asyncio
import base64
import logging
import re
from dataclasses import replace
from email.parser import BytesParser
from email.policy import default
from pathlib import Path
import random
from io import BytesIO
from unittest.mock import AsyncMock, Mock
from types import SimpleNamespace

import aiohttp
import httpx
import pytest
import respx
from aioresponses import aioresponses
from maxapi.enums.upload_type import UploadType
from maxapi.types import AttachmentPayload, AttachmentUpload
from PIL import Image

import image_service as service
import max_client
from config import Config

IMAGE_EDIT_URL = "https://api.openai.com/v1/images/edits"
PHOTO_URL = "https://photos.example.com/source.png"
PUBLIC_URL = re.compile(
    r"https://storage\.yandexcloud\.net/test-bucket/vision-boards/[a-f0-9]{32}\.png"
)
PNG = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/"
    "x8AAwMCAO+aH1sAAAAASUVORK5CYII="
)
ENCODED_PNG = base64.b64encode(PNG).decode()


@pytest.fixture
def config(monkeypatch):
    settings = Config(
        token="test-max-token",
        photo_path=Path("unused.png"),
        image_api_key="test-openai-key",
        image_model="test-image-model",
        storage_bucket="test-bucket",
        storage_access_key_id="test-storage-access",
        storage_secret_access_key="test-storage-secret",
    )
    monkeypatch.setattr(service.Config, "from_env", lambda: settings)
    return settings


@pytest.fixture
def max_bot(monkeypatch):
    bot = Mock()
    bot.upload_media = AsyncMock(
        return_value=AttachmentUpload(
            type=UploadType.IMAGE, payload=AttachmentPayload(token="media-token")
        )
    )
    bot.close_session = AsyncMock()
    factory = Mock(return_value=bot)
    monkeypatch.setattr(max_client, "Bot", factory)
    return bot, factory


@pytest.fixture
def storage(monkeypatch):
    client = Mock()
    client.uploaded_images = []
    client.put_object.side_effect = lambda **kwargs: client.uploaded_images.append(kwargs["Body"].read())
    factory = Mock(return_value=client)
    monkeypatch.setattr(service, "_storage_client", factory)
    return client


@pytest.fixture
def api():
    # Неописанный HTTP-запрос завершается ошибкой, а не уходит в сеть.
    with aioresponses() as mock, respx.mock(assert_all_called=False) as http:
        def post(url, *, payload=None, status=200, headers=None, body=None, content_type=None, exception=None):
            route = http.post(url)
            if exception is not None:
                return route.mock(side_effect=exception)
            response_headers = dict(headers or {})
            if content_type:
                response_headers["Content-Type"] = content_type
            response = httpx.Response(
                status, headers=response_headers,
                **({"content": body} if body is not None else {"json": payload}),
            )
            return route.mock(return_value=response)
        yield SimpleNamespace(
            get=mock.get, head=mock.head, post=post, requests=mock.requests, http=http,
        )


def register_generation(api, *, count=1, payload=None, status=200, headers=None):
    api.get(PHOTO_URL, body=PNG, content_type="image/png")
    if payload is None:
        payload = {"data": [{"b64_json": ENCODED_PNG} for _ in range(count)]}
    api.post(IMAGE_EDIT_URL, payload=payload, status=status, headers=headers)
    api.head(PUBLIC_URL, status=200, repeat=True)


def post_requests(api):
    return [call.request for call in api.http.calls if call.request.method == "POST"]


async def multipart_fields(request):
    """Разобрать настоящий multipart, который OpenAI SDK отправляет через httpx."""
    body = await request.aread()
    message = BytesParser(policy=default).parsebytes(
        f"Content-Type: {request.headers['Content-Type']}\r\nMIME-Version: 1.0\r\n\r\n".encode()
        + body
    )
    # httpx кодирует текстовые поля UTF-8 без отдельного Content-Type у поля.
    for part in message.iter_parts():
        if not part.get_filename():
            part.set_param("charset", "utf-8", header="Content-Type")
    return {
        part.get_param("name", header="content-disposition"): part
        for part in message.iter_parts()
    }


async def test_upload_photo_returns_media_token(config, max_bot):
    bot, factory = max_bot

    assert await service.upload_photo(PNG) == "media-token"

    factory.assert_called_once()
    assert factory.call_args.kwargs["token"] == config.token
    assert factory.call_args.kwargs["auto_requests"] is False
    connector = factory.call_args.kwargs["default_connection"].kwargs["connector"]
    assert connector._ssl is False
    assert connector.closed
    bot.upload_media.assert_awaited_once()
    media = bot.upload_media.call_args.args[0]
    assert media.buffer == PNG
    assert media.type == UploadType.IMAGE
    bot.close_session.assert_awaited_once()


@pytest.mark.parametrize("value,error", [(b"", ValueError), ("image", TypeError), (None, TypeError)])
async def test_upload_photo_rejects_invalid_input(value, error, max_bot):
    _, factory = max_bot
    with pytest.raises(error):
        await service.upload_photo(value)
    factory.assert_not_called()


async def test_upload_photo_closes_session_after_api_error(config, max_bot):
    bot, _ = max_bot
    failure = RuntimeError("Ошибка MAX API")
    bot.upload_media.side_effect = failure

    with pytest.raises(RuntimeError) as caught:
        await service.upload_photo(PNG)

    assert caught.value is failure
    bot.close_session.assert_awaited_once()


async def test_upload_result_to_max_uses_bytes_and_token(api, max_bot):
    bot, _ = max_bot
    url = "https://storage.example.com/result.png"
    api.get(url, body=PNG)

    attachment = await service.upload_image_for_max(bot, url)

    assert attachment is bot.upload_media.return_value
    assert attachment.model_dump()["payload"] == {"token": "media-token"}
    media = bot.upload_media.await_args.args[0]
    assert media.buffer == PNG
    assert media.filename.endswith(".png")
    assert media.type == UploadType.IMAGE
    bot.close_session.assert_not_awaited()
    for calls in api.requests.values():
        assert all("Authorization" not in call.kwargs.get("headers", {}) for call in calls)


async def test_upload_result_compresses_large_image(api, max_bot):
    bot, _ = max_bot
    source = Image.frombytes("RGB", (2048, 1536), random.Random(42).randbytes(2048 * 1536 * 3))
    buffer = BytesIO()
    source.save(buffer, format="PNG")
    data = buffer.getvalue()
    assert len(data) > 2_500_000
    url = "https://storage.example.com/large.png"
    api.get(url, body=data)

    await service.upload_image_for_max(bot, url)

    media = bot.upload_media.await_args.args[0]
    assert len(media.buffer) <= 1_000_000
    assert media.filename.endswith(".jpg")
    with Image.open(BytesIO(media.buffer)) as compressed:
        assert compressed.format == "JPEG"
        assert compressed.width <= source.width
        assert compressed.height <= source.height


async def test_upload_result_rejects_empty_token(api, max_bot):
    bot, _ = max_bot
    bot.upload_media.return_value.payload.token = ""
    url = "https://storage.example.com/result.png"
    api.get(url, body=PNG)
    with pytest.raises(service.ImageGenerationError, match="пустой media token"):
        await service.upload_image_for_max(bot, url)


async def test_upload_result_does_not_upload_after_download_error(api, max_bot):
    bot, _ = max_bot
    url = "https://storage.example.com/result.png"
    api.get(url, status=404)
    with pytest.raises(service.ImageGenerationError, match="HTTP 404"):
        await service.upload_image_for_max(bot, url)
    bot.upload_media.assert_not_awaited()


async def test_generate_poster_returns_uploaded_token_and_closes_bot(config, api, max_bot, monkeypatch):
    bot, _ = max_bot
    url = "https://storage.example.com/result.png"
    generator = AsyncMock(return_value=url)
    monkeypatch.setattr(service, "generate_vision_board", generator)
    api.get(url, body=PNG)

    image = await service.generate_poster(PHOTO_URL, "Карьера")

    assert image is bot.upload_media.return_value
    bot.close_session.assert_awaited_once()


async def test_upload_photo_rejects_empty_token(config, max_bot):
    bot, _ = max_bot
    bot.upload_media.return_value.payload.token = ""
    with pytest.raises(RuntimeError, match="пустой media token"):
        await service.upload_photo(PNG)
    bot.close_session.assert_awaited_once()


@pytest.mark.parametrize("entrypoint", ["single", "list"])
@pytest.mark.parametrize("dream", [
    "Машина тесла с бордовым салоном, я за рулем",
    "Я на пляже на Мальдивах",
    "я" * 200,
])
async def test_generation_uploads_photo_and_parses_response(config, storage, api, entrypoint, dream):
    count = 1
    register_generation(api, count=count)

    if entrypoint == "single":
        urls = [await service.generate_vision_board(PHOTO_URL, dream)]
    else:
        urls = await service.generate_vision_boards(PHOTO_URL, dream)

    assert len(urls) == count
    assert len(set(urls)) == count
    assert all(PUBLIC_URL.fullmatch(url) for url in urls)
    requests = post_requests(api)
    assert len(requests) == 1
    request = requests[0]
    assert request.headers["Authorization"] == f"Bearer {config.image_api_key}"
    assert dream.encode("utf-8") in await request.aread()
    fields = await multipart_fields(request)
    assert set(fields) == {"model", "image", "prompt", "size", "quality"}
    assert fields["image"].get_payload(decode=True) == PNG
    assert fields["image"].get_content_type() == "image/png"
    assert fields["image"].get_filename() == "photo.png"
    assert fields["model"].get_content() == config.image_model
    assert fields["quality"].get_content() == "high"
    assert fields["size"].get_content() == "1024x1536"
    prompt = fields["prompt"].get_content()
    assert dream in prompt
    assert prompt.startswith("Create a realistic photographic vision board collage")
    sections = (
        "IDENTITY — HIGHEST PRIORITY:", "COLLAGE LAYOUT:",
        "PHOTOGRAPHIC REALISM:", "FOLLOW THE DREAM:", "NO TEXT:",
        "User's dream description:",
    )
    positions = [prompt.index(section) for section in sections]
    assert positions == sorted(positions)
    assert "Do not replace their face with a similar-looking person or an idealized model." in prompt
    assert "occupying approximately 60% of the composition" in prompt
    assert "5–7 smaller photographic panels" in prompt
    assert "thin white borders and slightly angled edges" in prompt
    assert "Every panel must look like a genuine camera photograph." in prompt
    assert "Do not add titles, captions, motivational quotes, letters, typography, watermarks, or decorative text." in prompt
    assert prompt.endswith(f"User's dream description: {dream.strip()}")
    # https://developers.openai.com/api/reference/resources/images/methods/edit
    assert len(prompt) <= 32000
    assert storage.put_object.call_count == count
    for call, url in zip(storage.put_object.call_args_list, urls):
        assert storage.uploaded_images == [PNG] * count
        assert call.kwargs["Body"].closed
        assert call.kwargs["ContentType"] == "image/png"
        assert url == f"https://storage.yandexcloud.net/test-bucket/{call.kwargs['Key']}"
    for (method, _), calls in api.requests.items():
        if method in {"GET", "HEAD"}:
            assert all("Authorization" not in call.kwargs.get("headers", {}) for call in calls)
    storage.close.assert_called_once()


@pytest.mark.parametrize(
    "payload",
    [
        {}, {"data": []}, {"data": [{}]},
        {"data": [{"b64_json": "invalid!"}]},
        {"data": [{"b64_json": 123}]},
        {"data": [{"b64_json": base64.b64encode(b"not a PNG").decode()}]},
    ],
    ids=["missing-data", "empty-data", "missing-image", "bad-base64", "wrong-type", "not-png"],
)
async def test_generation_rejects_invalid_response(config, storage, api, payload):
    register_generation(api, payload=payload)
    with pytest.raises(service.ImageGenerationError, match="корректного PNG"):
        await service.generate_vision_board(PHOTO_URL, "Карьера")
    storage.put_object.assert_not_called()
    storage.close.assert_called_once()


@pytest.mark.parametrize("response_format", ["b64_json", "url", "both"])
async def test_generation_uploads_temp_file_and_removes_it(
    config, storage, api, tmp_path, monkeypatch, response_format,
):
    result_url = "https://cdn.example.com/generated.png"
    item = {"b64_json": ENCODED_PNG} if response_format == "b64_json" else {"url": result_url}
    if response_format == "both":
        item["b64_json"] = ENCODED_PNG
    register_generation(api, payload={"data": [item]})
    if response_format == "url":
        api.get(result_url, body=PNG)
    original_temp_directory = service.TemporaryDirectory
    monkeypatch.setattr(
        service, "TemporaryDirectory", lambda **kwargs: original_temp_directory(dir=tmp_path, **kwargs),
    )
    paths = []

    def upload(**kwargs):
        path = Path(kwargs["Body"].name)
        assert path.is_file()
        assert path.read_bytes() == PNG
        assert kwargs["Body"].read() == PNG
        paths.append(path)

    storage.put_object.side_effect = upload

    public_url = await service.generate_vision_board(PHOTO_URL, "Карьера")

    assert PUBLIC_URL.fullmatch(public_url)
    assert len(paths) == 1
    assert not paths[0].exists()
    assert not list(tmp_path.iterdir())
    for (method, _), calls in api.requests.items():
        if method == "GET":
            assert all("Authorization" not in call.kwargs.get("headers", {}) for call in calls)
    storage.close.assert_called_once()


async def test_generation_removes_temp_file_after_storage_error(config, storage, api, tmp_path, monkeypatch):
    register_generation(api)
    original_temp_directory = service.TemporaryDirectory
    monkeypatch.setattr(
        service, "TemporaryDirectory", lambda **kwargs: original_temp_directory(dir=tmp_path, **kwargs),
    )
    paths = []

    def upload(**kwargs):
        paths.append(Path(kwargs["Body"].name))
        assert paths[0].is_file()
        raise service.ClientError({"Error": {"Code": "AccessDenied"}}, "PutObject")

    storage.put_object.side_effect = upload
    with pytest.raises(service.ImageGenerationError, match="сохранить плакат"):
        await service.generate_vision_board(PHOTO_URL, "Карьера")

    assert paths and not paths[0].exists()
    assert not list(tmp_path.iterdir())
    assert storage.put_object.call_args.kwargs["Body"].closed
    storage.close.assert_called_once()


@pytest.mark.parametrize("url", [None, 123, "http://cdn.example.com/image.png", "https://user:pass@cdn.example.com/image.png"])
async def test_generation_rejects_invalid_image_url(config, storage, api, url):
    register_generation(api, payload={"data": [{"url": url}]})
    with pytest.raises(service.ImageGenerationError, match="корректного PNG"):
        await service.generate_vision_board(PHOTO_URL, "Карьера")
    storage.put_object.assert_not_called()


@pytest.mark.parametrize("failure,error", [
    (asyncio.TimeoutError(), service.GenerationTimeoutError),
    (aiohttp.ClientConnectionError("CDN unavailable"), service.ImageGenerationError),
])
async def test_generation_handles_result_download_error(config, storage, api, failure, error):
    result_url = "https://cdn.example.com/generated.png"
    register_generation(api, payload={"data": [{"url": result_url}]})
    api.get(result_url, exception=failure)
    with pytest.raises(error):
        await service.generate_vision_board(PHOTO_URL, "Карьера")
    storage.put_object.assert_not_called()
    storage.close.assert_called_once()


async def test_generation_rejects_incomplete_batch(config, storage, api):
    register_generation(api, count=2)
    with pytest.raises(service.ImageGenerationError):
        await service.generate_vision_boards(PHOTO_URL, "Карьера")
    storage.put_object.assert_not_called()


@pytest.mark.parametrize("proxy_url", [
    None,
    "http://94.249.177.108:8888",
    "http://custom-proxy.example.com:8080",
])
@pytest.mark.parametrize("status", [200, 401])
async def test_generation_uses_direct_openai_and_closes_proxy_client(config, api, monkeypatch, proxy_url, status):
    settings = replace(config, openai_proxy_url=proxy_url)
    monkeypatch.setenv("OPENAI_BASE_URL", "https://old-api.example.com/v1")
    original_init = httpx.AsyncClient.__init__
    clients = []

    def init(client, *args, **kwargs):
        clients.append((client, kwargs))
        original_init(client, *args, **kwargs)

    monkeypatch.setattr(httpx.AsyncClient, "__init__", init)
    api.get(PHOTO_URL, body=PNG, content_type="image/png")
    api.post(IMAGE_EDIT_URL, status=status, payload={"data": [{"b64_json": ENCODED_PNG}]})

    if status == 200:
        assert await service._generate_image_bytes(PHOTO_URL, "Карьера", settings) == [PNG]
    else:
        with pytest.raises(service.ImageGenerationError):
            await service._generate_image_bytes(PHOTO_URL, "Карьера", settings)

    assert len(clients) == 1
    client, kwargs = clients[0]
    assert kwargs["proxy"] == proxy_url
    assert kwargs["trust_env"] is False
    assert kwargs["follow_redirects"] is False
    assert kwargs.get("verify", True) is True
    assert client.is_closed
    assert len(post_requests(api)) == 1
    request = post_requests(api)[0]
    assert str(request.url) == IMAGE_EDIT_URL
    assert request.headers["Authorization"] == "Bearer test-openai-key"


async def test_openai_request_does_not_follow_redirects(config, api):
    api.get(PHOTO_URL, body=PNG)
    api.post(
        IMAGE_EDIT_URL, status=302, body="Unexpected redirect",
        headers={"Location": "https://other-api.example.com/images/edits"},
    )
    with pytest.raises(service.ImageGenerationError, match="HTTP 302"):
        await service._generate_image_bytes(PHOTO_URL, "Карьера", config)
    assert len(api.http.calls) == 1
    assert str(api.http.calls[0].request.url) == IMAGE_EDIT_URL


@pytest.mark.parametrize("quality", ["low", "medium", "high"])
async def test_generation_sends_configured_quality(config, api, caplog, quality):
    settings = replace(config, image_model="configured-image-model", image_quality=quality)
    register_generation(api)

    with caplog.at_level(logging.INFO, logger=service.__name__):
        assert await service._generate_image_bytes(PHOTO_URL, "Карьера", settings) == [PNG]

    fields = await multipart_fields(post_requests(api)[0])
    assert fields["model"].get_content() == "configured-image-model"
    assert fields["quality"].get_content() == quality
    assert set(fields) == {"model", "image", "prompt", "size", "quality"}
    assert (
        service.__name__, logging.INFO,
        f"Отправляю запрос с моделью: configured-image-model, качество: {quality}",
    ) in caplog.record_tuples
    assert (
        service.__name__, logging.INFO,
        f"Эндпоинт API: {IMAGE_EDIT_URL}, quality: {quality}",
    ) in caplog.record_tuples


async def test_generation_rejects_invalid_json(config, storage, api, caplog):
    api.get(PHOTO_URL, body=PNG)
    api.post(IMAGE_EDIT_URL, body="{", content_type="application/json")
    with pytest.raises(service.ImageGenerationError, match="некорректный JSON"):
        await service.generate_vision_board(PHOTO_URL, "Карьера")
    assert (
        service.__name__, logging.ERROR,
        "OpenAI Images API invalid JSON: HTTP 200; response body: {",
    ) in caplog.record_tuples
    storage.put_object.assert_not_called()


@pytest.mark.parametrize(
    "status,code,error",
    [
        (429, "rate_limit_exceeded", service.GenerationLimitError),
        (429, "insufficient_quota", service.GenerationLimitError),
        (400, "billing_hard_limit_reached", service.GenerationLimitError),
        (400, "invalid_image_format", service.UnsupportedPhotoFormatError),
        (415, None, service.UnsupportedPhotoFormatError),
        (500, None, service.ImageGenerationError),
    ],
)
async def test_generation_classifies_api_errors(config, storage, api, status, code, error):
    register_generation(
        api, status=status, payload={"error": {"code": code}}, headers={"Retry-After": "30"}
    )
    with pytest.raises(error) as caught:
        await service.generate_vision_board(PHOTO_URL, "Карьера")
    if isinstance(caught.value, service.GenerationLimitError):
        assert caught.value.retry_after == 30
        assert caught.value.quota_exhausted == (code != "rate_limit_exceeded")
    assert len(post_requests(api)) == 1
    storage.put_object.assert_not_called()
    storage.close.assert_called_once()


@pytest.mark.parametrize(
    "status,body,error",
    [
        (401, '{"error":{"code":"invalid_api_key","message":"Incorrect API key"}}', service.ImageGenerationError),
        (429, '{"error":{"code":"insufficient_quota","message":"Quota exceeded"}}', service.GenerationLimitError),
        (400, '{"error":{"code":"moderation_blocked","message":"Request blocked by moderation"}}', service.ImageGenerationError),
        (502, "<html>Bad gateway</html>\nUpstream unavailable", service.ImageGenerationError),
    ],
)
async def test_generation_logs_full_api_error(config, storage, api, caplog, status, body, error):
    api.get(PHOTO_URL, body=PNG)
    api.post(IMAGE_EDIT_URL, status=status, body=body)
    with caplog.at_level(logging.ERROR, logger=service.__name__):
        with pytest.raises(error):
            await service.generate_vision_board(PHOTO_URL, "Карьера")
    assert (
        service.__name__, logging.ERROR,
        f"OpenAI Images API error: HTTP {status}; response body: {body}",
    ) in caplog.record_tuples
    storage.put_object.assert_not_called()


@pytest.mark.parametrize("failure,error", [
    (httpx.ConnectError("OpenAI proxy connection failed"), service.ImageGenerationError),
    (httpx.ReadTimeout("OpenAI proxy timed out"), service.GenerationTimeoutError),
    (RuntimeError("Unexpected OpenAI failure"), service.ImageGenerationError),
])
async def test_generation_logs_exception_details(config, storage, api, caplog, failure, error):
    api.get(PHOTO_URL, body=PNG)
    api.post(IMAGE_EDIT_URL, exception=failure)

    with pytest.raises(error) as caught:
        await service.generate_vision_board(PHOTO_URL, "Карьера")

    assert caught.value.__cause__.__cause__ is failure
    assert "Подробная ошибка OpenAI:" in caplog.text
    assert repr(failure) in caplog.text
    storage.put_object.assert_not_called()
    storage.close.assert_called_once()


@pytest.mark.parametrize("stage", ["download", "generation"])
async def test_generation_handles_timeout(config, storage, api, stage):
    if stage == "download":
        api.get(PHOTO_URL, exception=asyncio.TimeoutError())
    else:
        api.get(PHOTO_URL, body=PNG)
        api.post(IMAGE_EDIT_URL, exception=httpx.ReadTimeout("OpenAI timed out"))
    with pytest.raises(service.GenerationTimeoutError):
        await service.generate_vision_board(PHOTO_URL, "Карьера")
    storage.put_object.assert_not_called()
    storage.close.assert_called_once()


@pytest.mark.parametrize("body,error", [(b"", service.ImageGenerationError), (b"GIF89a", service.UnsupportedPhotoFormatError)])
async def test_generation_rejects_invalid_photo(config, storage, api, body, error):
    api.get(PHOTO_URL, body=body)
    with pytest.raises(error):
        await service.generate_vision_board(PHOTO_URL, "Карьера")
    assert not post_requests(api)
    storage.put_object.assert_not_called()


async def test_generation_does_not_return_private_url(config, storage, api):
    api.get(PHOTO_URL, body=PNG)
    api.post(IMAGE_EDIT_URL, payload={"data": [{"b64_json": ENCODED_PNG}]})
    api.head(PUBLIC_URL, status=403)
    with pytest.raises(service.ImageGenerationError, match="публичная ссылка недоступна"):
        await service.generate_vision_board(PHOTO_URL, "Карьера")
    storage.put_object.assert_called_once()
    storage.close.assert_called_once()


@pytest.mark.parametrize("missing_setting", ["image_api_key", "image_model"])
async def test_generation_requires_key_and_model_before_http(config, storage, api, monkeypatch, missing_setting):
    monkeypatch.setattr(service.Config, "from_env", lambda: replace(config, **{missing_setting: None}))
    with pytest.raises(service.GenerationNotConfigured):
        await service.generate_vision_board(PHOTO_URL, "Карьера")
    assert not api.requests
    storage.put_object.assert_not_called()


@pytest.mark.parametrize("mode", list(service.POSTCARD_PROMPTS))
async def test_postcard_uses_template_and_stores_text_overlay(config, storage, api, mode):
    buffer = BytesIO()
    Image.new("RGB", (1024, 1536), "#243c58").save(buffer, format="PNG")
    generated = buffer.getvalue()
    register_generation(api, payload={"data": [{"b64_json": base64.b64encode(generated).decode()}]})
    urls = await service.generate_postcards(PHOTO_URL, mode, "Счастья тебе, Аня!")
    assert len(urls) == 1
    assert storage.uploaded_images == [service.add_text_to_image(generated, "Счастья тебе, Аня!", mode=mode)]
    assert storage.uploaded_images[0] != generated
    fields = await multipart_fields(post_requests(api)[0])
    prompt = fields["prompt"].get_content()
    assert prompt.startswith(service.POSTCARD_PROMPTS[mode])
    assert "Preserve every reference person's identity" in prompt
    if mode == "mode_modern_card":
        assert "Keep the left 42%" in prompt
        assert "bottom 22%" not in prompt
    else:
        assert "Keep the bottom 22% empty" in prompt
    assert "COLLAGE LAYOUT" not in prompt
    assert set(fields) == {"model", "image", "prompt", "size", "quality"}


@pytest.mark.parametrize("mode,greeting", [
    ("ny", "С Новым 2027 годом!"),
    ("mode_ny_family", "С Новым 2027 годом!"),
    ("bday_young", "С Днём Рождения!"),
    ("mode_bday_old", "С Днём Рождения!"),
    ("8march", "С 8 Марта!"),
])
@pytest.mark.parametrize("size", [(1024, 1536), (256, 768), (640, 360)])
def test_greeting_is_legible_centered_and_does_not_cover_portrait(monkeypatch, mode, greeting, size):
    from PIL import ImageChops
    source = Image.new("RGB", size, "#306080")
    buffer = BytesIO()
    source.save(buffer, format="PNG")
    drawn = []
    real_text = service.ImageDraw.ImageDraw.text

    def record_text(self, xy, text, *args, **kwargs):
        drawn.append(text)
        return real_text(self, xy, text, *args, **kwargs)

    monkeypatch.setattr(service.ImageDraw.ImageDraw, "text", record_text)
    result = Image.open(BytesIO(service.add_text_to_image(buffer.getvalue(), greeting))).convert("RGB")
    assert " ".join(drawn).split() == greeting.split()
    assert result.size == source.size
    box = ImageChops.difference(result, source).getbbox()
    assert box is not None
    left, top, right, bottom = box
    width, height = size
    assert top >= height * 0.78  # The portrait area above stays unchanged.
    assert bottom <= height * 0.99
    assert left >= width * 0.04 and right <= width * 0.96
    assert abs((left + right) / 2 - width / 2) <= max(2, width * 0.01)  # Font side bearings.
    colors = {color for _, color in result.crop(box).getcolors(maxcolors=width * height)}
    assert (255, 255, 255) in colors
    assert (0, 0, 0) in colors


async def test_missing_font_prevents_paid_generation(monkeypatch, config, storage, tmp_path):
    monkeypatch.setattr(service, "FONT_PATH", tmp_path / "missing-font.ttf")
    generate = AsyncMock()
    monkeypatch.setattr(service, "_generate_image_bytes", generate)
    with pytest.raises(service.GenerationNotConfigured, match="font.ttf"):
        await service.generate_postcards(PHOTO_URL, "mode_ny", "Анна, с праздником!")
    generate.assert_not_awaited()
    storage.put_object.assert_not_called()


def test_font_path_does_not_depend_on_working_directory(monkeypatch, tmp_path):
    monkeypatch.chdir(tmp_path)
    assert service._postcard_font(32).getbbox("С Днём Рождения!")[2] > 0


async def test_unknown_postcard_mode_does_not_call_api(monkeypatch):
    generate = AsyncMock()
    monkeypatch.setattr(service, "_generate_and_store", generate)
    with pytest.raises(ValueError, match="Неизвестный режим"):
        await service.generate_postcards(PHOTO_URL, "mode_unknown", "С праздником!")
    generate.assert_not_awaited()


async def test_user_text_is_overlayed_but_not_sent_to_image_model(config, storage, api):
    buffer = BytesIO()
    Image.new("RGB", (1024, 1536), "#243c58").save(buffer, format="PNG")
    generated = buffer.getvalue()
    custom = "С добрым утром, Алёна!\nПусть всё получится!"
    register_generation(api, payload={"data": [{"b64_json": base64.b64encode(generated).decode()}]})
    await service.generate_postcards(PHOTO_URL, "mode_goodmorning", custom)
    assert storage.uploaded_images == [service.add_text_to_image(generated, custom)]
    fields = await multipart_fields(post_requests(api)[0])
    prompt = fields["prompt"].get_content()
    assert "Good Morning" in prompt and custom not in prompt
    assert "Leave a large empty space at the bottom" in prompt


@pytest.mark.parametrize("text", ["", " ", "я" * 201])
async def test_invalid_user_text_is_rejected_before_paid_request(monkeypatch, text):
    generate = AsyncMock()
    monkeypatch.setattr(service, "_generate_and_store", generate)
    with pytest.raises(ValueError, match="Текст открытки"):
        await service.generate_postcards(PHOTO_URL, "mode_ny", text)
    generate.assert_not_awaited()


def test_long_multiline_custom_text_fits_bottom_and_keeps_all_words(monkeypatch):
    from PIL import ImageChops
    source = Image.new("RGB", (1024, 1536), "#324056")
    buffer = BytesIO()
    source.save(buffer, format="PNG")
    custom = "Любимая Аня, с днём рождения!\n" + "Пусть мечты сбываются и каждый день дарит радость! " * 3
    assert len(custom) <= 200
    drawn = []
    real_text = service.ImageDraw.ImageDraw.text

    def capture(self, xy, text, *args, **kwargs):
        drawn.append(text)
        return real_text(self, xy, text, *args, **kwargs)

    monkeypatch.setattr(service.ImageDraw.ImageDraw, "text", capture)
    result = Image.open(BytesIO(service.add_text_to_image(buffer.getvalue(), custom))).convert("RGB")
    assert " ".join(drawn).split() == custom.split()
    assert "\n" in "\n".join(drawn)
    box = ImageChops.difference(source, result).getbbox()
    assert box is not None and box[1] >= 1536 * 0.78 and box[3] <= 1536 * 0.99


@pytest.mark.parametrize("size", [(1024, 1536), (512, 768), (1536, 1024)])
def test_modern_card_uses_short_dark_lines_on_left_and_keeps_portrait_area(monkeypatch, size):
    from PIL import ImageChops
    source = Image.new("RGB", size, "#fff3df")
    buffer = BytesIO()
    source.save(buffer, format="PNG")
    text = (
        "Дорогая Аня, с праздником!\n"
        "Пусть каждый день приносит радость, улыбки и исполнение желаний. "
        "Твоё счастье очень важно для нас!\n" + "Вдохновение" * 5
    )
    rendered = []
    original = service.ImageDraw.ImageDraw.multiline_text

    def capture(self, xy, text, *args, **kwargs):
        rendered.append((text, kwargs))
        return original(self, xy, text, *args, **kwargs)

    monkeypatch.setattr(service.ImageDraw.ImageDraw, "multiline_text", capture)
    result = Image.open(BytesIO(service.add_text_to_image(buffer.getvalue(), text, mode="mode_modern_card"))).convert("RGB")
    assert len(rendered) == 1
    content, kwargs = rendered[0]
    assert max(map(len, content.splitlines())) <= 28
    assert "".join(content.split()) == "".join(text.split())
    assert kwargs["align"] == "left" and kwargs["fill"] == "#2C3E50"
    box = ImageChops.difference(source, result).getbbox()
    assert box is not None
    width, height = size
    assert width * 0.04 <= box[0] <= width * 0.06
    assert box[2] <= width * 0.43
    assert box[1] >= height * 0.14 and box[3] <= height * 0.86
    assert ImageChops.difference(
        source.crop((int(width * 0.45), 0, width, height)),
        result.crop((int(width * 0.45), 0, width, height)),
    ).getbbox() is None
    colors = {color for _, color in result.getcolors(maxcolors=width * height)}
    assert (44, 62, 80) in colors


def correction_response(text, finish_reason="stop"):
    return {
        "id": "correction", "object": "chat.completion", "created": 1, "model": "gpt-4o-mini",
        "choices": [{"index": 0, "finish_reason": finish_reason,
                     "message": {"role": "assistant", "content": text}}],
    }


async def test_text_correction_uses_same_proxy_and_key(config, api, monkeypatch):
    import json
    settings = replace(config, openai_proxy_url="http://proxy.example:8888")
    monkeypatch.setattr(service.Config, "from_env", lambda: settings)
    original_init = httpx.AsyncClient.__init__
    clients = []

    def init(client, *args, **kwargs):
        clients.append((client, kwargs))
        original_init(client, *args, **kwargs)

    monkeypatch.setattr(httpx.AsyncClient, "__init__", init)
    api.post("https://api.openai.com/v1/chat/completions", payload=correction_response("Счастья, Аня!\nУлыбайся!"))
    assert await service.fix_text_with_ai("Счастя, Аня!\nУлыбайся!") == "Счастья, Аня!\nУлыбайся!"
    request = post_requests(api)[0]
    body = json.loads(request.content)
    assert body["model"] == "gpt-4o-mini"
    assert body["messages"][1] == {"role": "user", "content": "Счастя, Аня!\nУлыбайся!"}
    assert "сохрани смысл и имена" in body["messages"][0]["content"]
    assert request.headers["Authorization"] == "Bearer test-openai-key"
    client, kwargs = clients[0]
    assert kwargs["proxy"] == settings.openai_proxy_url
    assert kwargs["trust_env"] is False
    assert kwargs["follow_redirects"] is False
    assert client.is_closed
    assert len(post_requests(api)) == 1


@pytest.mark.parametrize("text,reason", [(None, "stop"), ("", "stop"), (" " * 10, "stop"), ("а" * 201, "stop"), ("обрыв", "length"), (None, "content_filter")])
async def test_correction_rejects_invalid_or_incomplete_answer(config, api, text, reason):
    api.post("https://api.openai.com/v1/chat/completions", payload=correction_response(text, reason))
    with pytest.raises(service.ImageGenerationError):
        await service.fix_text_with_ai("С праздником, Аня!")


async def test_correction_logs_api_response_and_does_not_retry(config, api, caplog):
    api.post("https://api.openai.com/v1/chat/completions", status=400,
             payload={"error": {"message": "model not available", "code": "unsupported_model"}})
    with pytest.raises(service.ImageGenerationError), caplog.at_level(logging.ERROR):
        await service.fix_text_with_ai("Счастя, Аня!")
    assert "HTTP 400" in caplog.text and "model not available" in caplog.text
    assert len(post_requests(api)) == 1


async def test_correction_timeout(config, api):
    api.post("https://api.openai.com/v1/chat/completions", exception=httpx.ReadTimeout("timeout"))
    with pytest.raises(service.GenerationTimeoutError):
        await service.fix_text_with_ai("Счастя, Аня!")


async def test_correction_connection_failure(config, api):
    api.post("https://api.openai.com/v1/chat/completions", exception=httpx.ConnectError("unreachable"))
    with pytest.raises(service.ImageGenerationError):
        await service.fix_text_with_ai("Счастя, Аня!")


def test_family_new_year_headwear_is_conditional():
    prompt = service.POSTCARD_PROMPTS["mode_ny_family"]
    assert "ONLY if" in prompt and "or if it's a child" in prompt
    assert "Otherwise, keep the original hairstyle" in prompt
    assert "or no hat" not in prompt


@pytest.mark.parametrize("mode,details", [
    ("mode_bday_old_elegant", ["50+", "wrinkles", "minimal decorations"]),
    ("mode_halloween", ["pumpkins", "cute and not scary"]),
    ("mode_ny_cozy", ["fireplace", "hot cocoa", "winter sweater"]),
    ("mode_ny_fairytale", ["ice castle", "elegant winter coat"]),
    ("mode_ny_elegant", ["Champagne glasses", "golden decorations", "evening outfit"]),
])
def test_new_card_prompts_match_requested_theme(mode, details):
    for detail in details:
        assert detail in service.POSTCARD_PROMPTS[mode]


@pytest.mark.parametrize("mode", ["mode_ny", "mode_modern_card"])
@pytest.mark.parametrize("light", [True, False])
def test_text_color_and_outline_follow_background_in_text_area(monkeypatch, mode, light):
    from PIL import ImageDraw
    source = Image.new("RGB", (400, 600), "black" if light else "white")
    painter = ImageDraw.Draw(source)
    area = (20, 90, 168, 510) if mode == "mode_modern_card" else (0, 468, 400, 600)
    painter.rectangle(area, fill="white" if light else "black")
    buffer = BytesIO()
    source.save(buffer, format="PNG")
    captured = []
    original = service.ImageDraw.ImageDraw.multiline_text
    def draw(self, xy, text, *args, **kwargs):
        captured.append(kwargs)
        return original(self, xy, text, *args, **kwargs)
    monkeypatch.setattr(service.ImageDraw.ImageDraw, "multiline_text", draw)
    result = service.add_text_to_image(buffer.getvalue(), "Счастья, Аня!", mode)
    assert captured[0]["fill"] == ("#2C3E50" if light else "#FFFFFF")
    assert captured[0]["stroke_fill"] == ("white" if light else "black")
    assert captured[0]["stroke_width"] >= 1
    assert Image.open(BytesIO(result)).size == source.size


def test_emoji_drawing_error_retries_from_clean_original(monkeypatch, caplog):
    source = Image.new("RGB", (400, 600), "#dce4f5")
    buffer = BytesIO()
    source.save(buffer, format="PNG")
    captured = []
    original = service.ImageDraw.ImageDraw.multiline_text
    def draw(self, xy, text, *args, **kwargs):
        captured.append(text)
        if "❤" in text:
            self.rectangle((0, 0, 40, 40), fill="red")
            raise UnicodeError("emoji render failure")
        return original(self, xy, text, *args, **kwargs)
    monkeypatch.setattr(service.ImageDraw.ImageDraw, "multiline_text", draw)
    result = Image.open(BytesIO(service.add_text_to_image(buffer.getvalue(), "Счастья, Аня! ❤️\n2027")))
    assert len(captured) == 2 and "❤" in captured[0]
    assert captured[1].split() == ["Счастья,", "Аня!", "2027"]
    assert result.getpixel((5, 5)) == source.getpixel((5, 5))
    assert "без эмодзи" in caplog.text


def test_missing_emoji_glyph_is_removed_instead_of_drawing_a_box(monkeypatch):
    source = Image.new("RGB", (400, 600), "white")
    buffer = BytesIO()
    source.save(buffer, format="PNG")
    original_mask = service.ImageFont.FreeTypeFont.getmask
    def mask(self, text, *args, **kwargs):
        return original_mask(self, "\U0010FFFF" if text == "🎉" else text, *args, **kwargs)
    monkeypatch.setattr(service.ImageFont.FreeTypeFont, "getmask", mask)
    actual = service.add_text_to_image(buffer.getvalue(), "Аня, поздравляю 🎉")
    expected = service.add_text_to_image(buffer.getvalue(), "Аня, поздравляю ")
    assert actual == expected


def test_supported_heart_emoji_is_preserved(monkeypatch):
    source = Image.new("RGB", (400, 600), "white")
    buffer = BytesIO()
    source.save(buffer, format="PNG")
    captured = []
    original = service.ImageDraw.ImageDraw.multiline_text
    def draw(self, xy, text, *args, **kwargs):
        captured.append(text)
        return original(self, xy, text, *args, **kwargs)
    monkeypatch.setattr(service.ImageDraw.ImageDraw, "multiline_text", draw)
    service.add_text_to_image(buffer.getvalue(), "Любви ❤️")
    assert len(captured) == 1 and "❤️" in captured[0]


def test_emoji_only_fallback_returns_image_without_overlay(monkeypatch):
    source = Image.new("RGB", (400, 600), "#345678")
    buffer = BytesIO()
    source.save(buffer, format="PNG")
    original_mask = service.ImageFont.FreeTypeFont.getmask
    def mask(self, text, *args, **kwargs):
        return original_mask(self, "\U0010FFFF" if text == "🎉" else text, *args, **kwargs)
    monkeypatch.setattr(service.ImageFont.FreeTypeFont, "getmask", mask)
    result = Image.open(BytesIO(service.add_text_to_image(buffer.getvalue(), "🎉")))
    assert result.tobytes() == source.tobytes()


def test_fallback_removes_joined_emoji_flags_and_keycaps_but_keeps_digits():
    assert service._EMOJI_RE.sub("", "Аня 👨‍👩‍👧‍👦 🇷🇺 1️⃣ 2027 ❤️") == "Аня    2027 "


def test_unrelated_render_error_is_not_swallowed(monkeypatch):
    source = Image.new("RGB", (400, 600), "white")
    buffer = BytesIO()
    source.save(buffer, format="PNG")
    monkeypatch.setattr(service.ImageDraw.ImageDraw, "multiline_text", Mock(side_effect=ValueError("invalid drawing")))
    with pytest.raises(ValueError, match="invalid drawing"):
        service.add_text_to_image(buffer.getvalue(), "С праздником, Аня!")


async def test_photosession_frame_uses_edits_storage_pipeline_without_text_overlay(monkeypatch):
    generate = AsyncMock(return_value=["https://cdn.example/frame.png"])
    monkeypatch.setattr(service, "_generate_and_store", generate)
    for prompt in service.NY_FAMILY_THREE_PROMPTS:
        assert await service.generate_photosession_frame(PHOTO_URL, prompt) == "https://cdn.example/frame.png"
    assert generate.await_count == 3
    assert [c.kwargs for c in generate.await_args_list] == [
        {"count": 1, "prompt_override": p} for p in service.NY_FAMILY_THREE_PROMPTS
    ]
    assert all("DO NOT ADD ANY TEXT." in p and "3 people" in p for p in service.NY_FAMILY_THREE_PROMPTS)
    assert "sitting together" in service.NY_FAMILY_THREE_PROMPTS[0]
    assert "lying on the floor" in service.NY_FAMILY_THREE_PROMPTS[1]
    assert "2 adults and 1 child" in service.NY_FAMILY_THREE_PROMPTS[2]
    assert "1 adult and 2 children" in service.NY_FAMILY_THREE_PROMPTS[2]


@pytest.mark.parametrize("proxy_url", ["http://94.249.177.108:8888", "http://custom-proxy.example.com:8080"])
@pytest.mark.parametrize("outcome", ["success", "http_error", "timeout"])
async def test_photosession_public_path_passes_proxy_and_300_second_timeout(
    config, storage, api, monkeypatch, proxy_url, outcome,
):
    settings = replace(config, openai_proxy_url=proxy_url)
    monkeypatch.setattr(service.Config, "from_env", lambda: settings)
    clients = []
    original_init = httpx.AsyncClient.__init__
    def capture_init(client, *args, **kwargs):
        clients.append((client, kwargs))
        original_init(client, *args, **kwargs)
    monkeypatch.setattr(httpx.AsyncClient, "__init__", capture_init)
    api.get(PHOTO_URL, body=PNG, content_type="image/png")
    if outcome == "timeout":
        api.post(IMAGE_EDIT_URL, exception=httpx.ReadTimeout("photosession timeout"))
    else:
        api.post(IMAGE_EDIT_URL, status=401 if outcome == "http_error" else 200,
                 payload={"error": {"message": "bad key"}} if outcome == "http_error" else
                         {"data": [{"b64_json": ENCODED_PNG}]})
    api.head(PUBLIC_URL, status=200, repeat=True)
    prompt = service.NY_FAMILY_THREE_PROMPTS[0]
    if outcome == "success":
        result = await service.generate_photosession_frame(PHOTO_URL, prompt)
        assert PUBLIC_URL.fullmatch(result)
        assert storage.uploaded_images == [PNG]
    else:
        expected_error = service.GenerationTimeoutError if outcome == "timeout" else service.ImageGenerationError
        with pytest.raises(expected_error):
            await service.generate_photosession_frame(PHOTO_URL, prompt)
        storage.put_object.assert_not_called()
    assert len(clients) == 1
    client, kwargs = clients[0]
    assert kwargs["proxy"] == proxy_url
    assert kwargs["timeout"] == 300.0
    assert kwargs["trust_env"] is False
    assert client.is_closed
    storage.close.assert_called_once()
    requests = post_requests(api)
    assert len(requests) == 1
    assert str(requests[0].url) == IMAGE_EDIT_URL
    assert requests[0].headers["Authorization"] == "Bearer test-openai-key"
    fields = await multipart_fields(requests[0])
    assert fields["model"].get_content() == settings.image_model
    assert fields["quality"].get_content() == settings.image_quality
    assert fields["prompt"].get_content() == prompt


@pytest.mark.parametrize('mode', ['RGB', 'RGBA'])
def test_preview_watermark_is_centered_translucent_and_preserves_image_size(mode):
    from PIL import ImageChops
    original = Image.new(mode, (640, 960), (30, 60, 90, 255) if mode == 'RGBA' else (30, 60, 90))
    source = BytesIO()
    original.save(source, format='PNG')
    data = source.getvalue()
    output = service.add_preview_watermark(data)
    assert source.getvalue() == data
    with Image.open(BytesIO(output)) as result:
        assert result.size == original.size and result.format == 'PNG'
        assert result.getpixel((0, 0)) == (30, 60, 90, 255)
        bounds = ImageChops.difference(result.convert('RGB'), original.convert('RGB')).getbbox()
        assert bounds is not None
        left, top, right, bottom = bounds
        assert 0 < left < 320 < right < 640 and 320 < top < 480 < bottom < 640
        assert abs((left + right) / 2 - 320) < 5
        assert abs((top + bottom) / 2 - 480) < 5
        assert result.convert('RGB').getchannel('R').getextrema()[1] < 255


def test_preview_watermark_uses_fallback_if_card_font_is_unavailable(monkeypatch, tmp_path):
    monkeypatch.setattr(service, 'FONT_PATH', tmp_path / 'missing.ttf')
    source = BytesIO()
    Image.new('RGB', (300, 200), 'navy').save(source, format='PNG')
    result = service.add_preview_watermark(source.getvalue())
    with Image.open(BytesIO(result)) as image:
        assert image.size == (300, 200)


@pytest.mark.parametrize('mode', ['mode_ny_family', 'mode_modern_card', 'mode_halloween'])
async def test_cartoon_avatar_uses_one_image_shared_openai_pipeline_and_identity_prompt(monkeypatch, mode):
    generate = AsyncMock(return_value=['https://cdn.example/avatar.png'])
    monkeypatch.setattr(service, '_generate_and_store', generate)
    assert await service.generate_cartoon_avatar(PHOTO_URL, mode) == 'https://cdn.example/avatar.png'
    args, kwargs = generate.await_args
    assert args == (PHOTO_URL, 'Мульт-аватар') and kwargs['count'] == 1
    prompt = kwargs['prompt_override']
    assert '3D Pixar/Disney style avatar' in prompt
    assert 'Preserve the exact face' in prompt and 'soft blurred Christmas tree background' in prompt
    assert service.CARTOON_AVATAR_THEMES[mode] in prompt
    assert 'postcard_text' not in kwargs


async def test_cartoon_avatar_invalid_style_never_calls_openai(monkeypatch):
    generate = AsyncMock()
    monkeypatch.setattr(service, '_generate_and_store', generate)
    with pytest.raises(ValueError, match='Неизвестный стиль'):
        await service.generate_cartoon_avatar(PHOTO_URL, 'mode_invalid')
    generate.assert_not_awaited()

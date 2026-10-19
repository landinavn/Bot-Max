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
    assert fields["image"].get_payload(decode=True) == PNG
    assert fields["image"].get_content_type() == "image/png"
    assert fields["image"].get_filename() == "photo.png"
    assert fields["model"].get_content() == config.image_model
    assert fields["quality"].get_content() == "high"
    assert fields["n"].get_content() == str(count)
    prompt = fields["prompt"].get_content()
    assert dream in prompt
    assert prompt.startswith("Создай вдохновляющий плакат — карту желаний — на основе приложенного фото. ")
    assert "keep the face exactly as in the original photo; natural head position; photorealistic portrait; do not change facial features." in prompt
    assert "ANATOMY REQUIREMENTS (critical):\n" in prompt
    assert "Exactly 5 fingers on each hand, correctly shaped and positioned" in prompt
    assert "Hands and feet must not be cropped awkwardly unless part of natural composition\n\n" in prompt
    assert f"Описание мечты пользователя: {dream.strip()}\n\nИзобрази человека" in prompt
    assert prompt.endswith("Без надписей и водяных знаков.")
    # https://developers.openai.com/api/reference/resources/images/methods/edit
    assert len(prompt) <= 32000
    assert fields["output_format"].get_content() == "png"
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

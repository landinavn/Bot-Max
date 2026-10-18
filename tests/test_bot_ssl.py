"""SSL-исключение клиента MAX и сохранение TLS у остальных клиентов."""

import ssl
import logging
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest
from aiohttp import ClientRequest, TCPConnector
from yarl import URL

from bot import MaxAPIConnector
import max_client


@pytest.mark.parametrize(
    "url",
    [
        "https://platform-api.max.ru/me",
        "https://platform-api2.max.ru/me",
        "https://upload.max.ru/upload",
        "https://upload.example-cdn.test/upload",
    ],
)
async def test_max_client_disables_ssl_for_api_and_uploads(url):
    async with MaxAPIConnector() as connector:
        context = connector._get_ssl_context(ClientRequest("GET", URL(url)))
        assert context is not None
        assert context.verify_mode == ssl.CERT_NONE
        assert context.check_hostname is False
    assert connector.closed


async def test_http_does_not_use_ssl_context():
    async with MaxAPIConnector() as connector:
        request = ClientRequest("GET", URL("http://platform-api.max.ru/me"))
        assert connector._get_ssl_context(request) is None


@pytest.mark.parametrize("url", [
    "https://api.proxyapi.ru/openai/v1/images/edits",
    "https://storage.yandexcloud.net/bucket/photo.png",
    "https://shop.payform.ru/",
])
async def test_other_http_clients_still_verify_ssl(url):
    async with TCPConnector() as connector:
        context = connector._get_ssl_context(ClientRequest("GET", URL(url)))
        assert context.verify_mode == ssl.CERT_REQUIRED
        assert context.check_hostname is True


@pytest.mark.parametrize("fail", [False, True])
async def test_shared_max_client_passes_connector_and_closes_it(monkeypatch, fail):
    bot = Mock(close_session=AsyncMock())
    factory = Mock(return_value=bot)
    monkeypatch.setattr(max_client, "Bot", factory)
    try:
        async with max_client.create_max_bot("test-token", auto_requests=False) as created:
            assert created is bot
            connector = factory.call_args.kwargs["default_connection"].kwargs["connector"]
            assert connector._ssl is False
            assert not connector.closed
            bot.set_api_url.assert_called_once_with("https://platform-api.max.ru")
            if fail:
                raise RuntimeError("handler failed")
    except RuntimeError:
        assert fail
    bot.close_session.assert_awaited_once()
    assert connector.closed


@pytest.mark.parametrize("text_kind", ["string", "callable", "async"])
async def test_max_error_logs_full_response_text(caplog, text_kind):
    logger = logging.getLogger("test-max-errors")
    body = "MAX response with full details\nUnknown recipient"
    text = body if text_kind == "string" else (lambda: body)
    if text_kind == "async":
        text = AsyncMock(return_value=body)
    error = RuntimeError("send failed")
    error.response = SimpleNamespace(text=text)

    with caplog.at_level(logging.ERROR):
        await max_client.log_max_error(error, logger)

    assert "Детали ошибки MAX: send failed" in caplog.text
    assert f"Ответ сервера: {body}" in caplog.text

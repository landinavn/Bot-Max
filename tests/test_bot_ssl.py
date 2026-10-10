"""Проверка границы исключения TLS для MAX."""

import ssl

import pytest
from aiohttp import ClientRequest
from yarl import URL

from bot import MaxAPIConnector


@pytest.mark.parametrize(
    "url,unverified",
    [
        ("https://platform-api.max.ru/me", True),
        ("https://platform-api2.max.ru/me", False),
        ("https://api.openai.com/v1/images/edits", False),
        ("https://storage.yandexcloud.net/bucket/photo.png", False),
        ("https://platform-api.max.ru.example.com/me", False),
        ("https://sub.platform-api.max.ru/me", False),
        ("https://proxy.example.com/", False),
    ],
)
async def test_ssl_verification_is_disabled_only_for_max(url, unverified):
    async with MaxAPIConnector() as connector:
        context = connector._get_ssl_context(ClientRequest("GET", URL(url)))
        assert context is not None
        assert context.verify_mode == (
            ssl.CERT_NONE if unverified else ssl.CERT_REQUIRED
        )
        assert context.check_hostname is not unverified
    assert connector.closed


async def test_http_does_not_use_ssl_context():
    async with MaxAPIConnector() as connector:
        request = ClientRequest("GET", URL("http://platform-api.max.ru/me"))
        assert connector._get_ssl_context(request) is None

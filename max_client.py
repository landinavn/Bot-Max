"""Общий HTTP-клиент MAX для сообщений, polling и загрузки файлов."""

import ssl
import logging
from contextlib import asynccontextmanager

from aiohttp import TCPConnector, ClientConnectionError
from maxapi import Bot
from maxapi.client.default import DefaultConnectionProperties

from config import MAX_API_URL


class MaxAPIConnector(TCPConnector):
    """System CA, hostname and validity checks, without insecure fallback."""

    def __init__(self) -> None:
        super().__init__(ssl=ssl.create_default_context())

    async def connect(self, req, traces, timeout):
        if req.url.scheme != 'https':
            raise ClientConnectionError('MAX transport requires HTTPS')
        return await super().connect(req, traces, timeout)


@asynccontextmanager
async def create_max_bot(token: str, *, auto_requests: bool = True):
    # Сессии ProxyAPI, S3 и Prodamus создаются отдельно с проверкой TLS.
    async with MaxAPIConnector() as connector:
        bot = Bot(
            token=token,
            auto_requests=auto_requests,
            default_connection=DefaultConnectionProperties(trust_env=True, connector=connector),
        )
        try:
            bot.set_api_url(MAX_API_URL)
            yield bot
        finally:
            await bot.close_session()


async def log_max_error(exc: Exception, logger: logging.Logger) -> None:
    """Never inspect the exception or a provider response, even outside service."""
    logger.error('MAX request failed')

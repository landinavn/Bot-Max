"""Общий HTTP-клиент MAX для сообщений, polling и загрузки файлов."""

import ssl
import logging
from contextlib import asynccontextmanager

from aiohttp import TCPConnector, ClientConnectionError
from maxapi import Bot
from maxapi.client.default import DefaultConnectionProperties

from config import MAX_API_URL
from safe_logging import install_safe_logging


class MaxAPIConnector(TCPConnector):
    """System CA validation for MAX API and upload hosts; no insecure fallback."""

    def __init__(self) -> None:
        super().__init__(ssl=ssl.create_default_context())

    async def connect(self, req, traces, timeout):
        # Also reject automatic HTTPS -> HTTP redirects from API/upload hosts.
        if req.url.scheme != 'https':
            raise ClientConnectionError('MAX transport requires HTTPS')
        return await super().connect(req, traces, timeout)


@asynccontextmanager
async def create_max_bot(token: str, *, auto_requests: bool = True):
    install_safe_logging()
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


async def log_max_error(exc: Exception, logger) -> None:
    """Never inspect/log SDK raw bodies, headers, URLs, or exception messages."""
    from safe_logging import get_logger
    # Accept legacy callers passing a stdlib Logger as well.
    secure = get_logger(logger.name)
    status = getattr(exc, 'status_code', None)
    if type(status) is not int:
        status = getattr(exc, 'code', None)
    secure.event('request_failed', level=logging.ERROR, product='max', http_status=status, error=exc)

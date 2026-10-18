"""Общий HTTP-клиент MAX для сообщений, polling и загрузки файлов."""

import inspect
import logging
from contextlib import asynccontextmanager

from aiohttp import TCPConnector
from maxapi import Bot
from maxapi.client.default import DefaultConnectionProperties

from config import MAX_API_URL


class MaxAPIConnector(TCPConnector):
    """SSL-исключение для отдельного клиента MAX, включая upload-серверы."""

    def __init__(self) -> None:
        super().__init__(ssl=False)


@asynccontextmanager
async def create_max_bot(token: str, *, auto_requests: bool = True):
    # Сессии ProxyAPI, S3 и Prodamus создаются отдельно с проверкой TLS.
    async with MaxAPIConnector() as connector:
        bot = Bot(
            token=token,
            auto_requests=auto_requests,
            default_connection=DefaultConnectionProperties(trust_env=True, connector=connector),
        )
        bot.set_api_url(MAX_API_URL)
        try:
            yield bot
        finally:
            await bot.close_session()


async def log_max_error(exc: Exception, logger: logging.Logger) -> None:
    """Вывести ответ native maxapi либо исключения с response.text."""
    logger.error("Детали ошибки MAX: %s", exc, exc_info=True)
    raw = getattr(exc, "raw", None)
    if raw is not None:
        logger.error("Ответ сервера MAX (код %s): %s", getattr(exc, "code", None), raw)
    response = getattr(exc, "response", None)
    if response is not None:
        try:
            text = getattr(response, "text", None)
            if callable(text):
                text = text()
            if inspect.isawaitable(text):
                text = await text
            if text is not None:
                logger.error("Ответ сервера: %s", text)
        except Exception as read_error:
            logger.error("Не удалось прочитать ответ MAX: %s", read_error)

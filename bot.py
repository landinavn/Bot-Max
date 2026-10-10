"""Запуск бота: python bot.py. Long polling используется для разработки."""

import asyncio
import logging
import ssl

from aiohttp import ClientRequest, TCPConnector
from maxapi import Bot, Dispatcher
from maxapi.client.default import DefaultConnectionProperties
from maxapi.client.ssl import create_default_ssl_context
from maxapi.context import MemoryContext
from maxapi.context.isolation import SimpleEventIsolation

from config import Config, MAX_API_URL
from database import init_db
from handlers import create_router

logger = logging.getLogger(__name__)


class MaxAPIConnector(TCPConnector):
    """Исключение проверки сертификата только для HTTPS API MAX."""

    def __init__(self) -> None:
        super().__init__(ssl=create_default_ssl_context())
        self._max_ssl_context = ssl._create_unverified_context()

    def _get_ssl_context(self, req: ClientRequest) -> ssl.SSLContext | None:
        if req.url.scheme == "https" and req.url.host == "platform-api.max.ru":
            return self._max_ssl_context
        return super()._get_ssl_context(req)


def configure_logging() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )


async def main() -> None:
    configure_logging()
    config = Config.from_env()
    await init_db()
    # maxapi не закрывает переданный извне connector: контекст закрывает его здесь.
    async with MaxAPIConnector() as connector:
        bot = Bot(
            token=config.token,
            default_connection=DefaultConnectionProperties(
                trust_env=True, connector=connector,
            ),
        )
        bot.set_api_url(MAX_API_URL)
        dispatcher = Dispatcher(
            storage=MemoryContext,
            use_create_task=True,
            event_isolation=SimpleEventIsolation(),
        )
        dispatcher.include_routers(create_router())
        try:
            logger.info("Запуск получения обновлений MAX.")
            await dispatcher.start_polling(bot)
        finally:
            try:
                await dispatcher.shutdown()
            finally:
                await bot.close_session()


if __name__ == "__main__":
    configure_logging()
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        pass
    except Exception as exc:
        logger.error("Бот остановлен из-за ошибки (%s).", type(exc).__name__)
        raise SystemExit(1) from None

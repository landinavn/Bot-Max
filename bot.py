"""Запуск бота: python bot.py. Long polling используется для разработки."""

import asyncio
import logging

from maxapi import Dispatcher
from maxapi.context import MemoryContext
from maxapi.context.isolation import SimpleEventIsolation

from config import Config
from max_client import MaxAPIConnector, create_max_bot
from database import init_db
from handlers import create_router
from prodamus import ProdamusPayments
from webhook import start_webhook_server
from audio_orders import PaidAudioWorker
from musical_orders import PaidMusicalWorker
from cartoon_orders import PaidCartoonWorker

logger = logging.getLogger(__name__)


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
    payments = None
    if any((config.prodamus_domain, config.prodamus_secret_key,
            config.prodamus_return_url, config.prodamus_webhook_url)):
        try:
            payments = ProdamusPayments(config)
        except ValueError as exc:
            logger.error("Ошибка настройки Prodamus: %s", exc)
            raise
    async with create_max_bot(config.token) as bot:
        dispatcher = Dispatcher(
            storage=MemoryContext,
            use_create_task=True,
            event_isolation=SimpleEventIsolation(),
        )
        dispatcher.include_routers(create_router(payments=payments, admin_user_id=config.admin_user_id, bot_link=config.bot_link))
        webhook_runner = None
        audio_task = None
        musical_task = None
        cartoon_task = None
        musical_worker = PaidMusicalWorker(bot)
        audio_worker = PaidAudioWorker(bot, payments=payments, admin_user_id=config.admin_user_id)
        try:
            if payments is not None:
                webhook_runner = await start_webhook_server(
                    payments, config.webhook_host, config.webhook_port, audio_worker=audio_worker, musical_worker=musical_worker,
                )
            else:
                audio_task = asyncio.create_task(audio_worker.run())
                musical_task = asyncio.create_task(musical_worker.run())
            cartoon_task = asyncio.create_task(PaidCartoonWorker(bot).run())
            logger.info("Запуск получения обновлений MAX.")
            await dispatcher.start_polling(bot)
        finally:
            try:
                if cartoon_task is not None:
                    cartoon_task.cancel()
                    try:
                        await cartoon_task
                    except asyncio.CancelledError:
                        pass
                if musical_task is not None:
                    musical_task.cancel()
                    try:
                        await musical_task
                    except asyncio.CancelledError:
                        pass
                if audio_task is not None:
                    audio_task.cancel()
                    try:
                        await audio_task
                    except asyncio.CancelledError:
                        pass
                if webhook_runner is not None:
                    await webhook_runner.cleanup()
            finally:
                await dispatcher.shutdown()


if __name__ == "__main__":
    configure_logging()
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        pass
    except Exception as exc:
        logger.error("Бот остановлен из-за ошибки: %s", exc, exc_info=True)
        raise SystemExit(1) from None

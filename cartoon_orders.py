"""Доставка запроса фото после подтверждённой оплаты мульт-заказа."""
import asyncio
import logging
import time

from maxapi.types import CallbackButton
from maxapi.utils.inline_keyboard import InlineKeyboardBuilder

from database import pending_cartoon_notifications, update_cartoon_order

logger = logging.getLogger(__name__)
CARTOON_PAID_PHOTO_PROMPT = (
    '✅ Оплата получена! 📸 Отправьте четкое фото (анфас, хорошее освещение). '
    'Мы нарисуем вашего персонажа!\n\n'
    'Если вы уже перешли в другой раздел, нажмите «Продолжить создание».'
)


class PaidCartoonWorker:
    def __init__(self, bot, *, poll_interval=2):
        self.bot = bot
        self.poll_interval = poll_interval

    async def process(self):
        for order in await pending_cartoon_notifications():
            try:
                keyboard = InlineKeyboardBuilder().row(CallbackButton(
                    text='🎬 Продолжить создание', payload='cartoon_resume:' + order['order_id'])).as_markup()
                sent = await self.bot.send_message(user_id=order['user_id'], text=CARTOON_PAID_PHOTO_PROMPT,
                                                   attachments=[keyboard])
                if sent is None:
                    raise RuntimeError('MAX не подтвердил запрос фото.')
                await update_cartoon_order(order['order_id'], photo_notified=1)
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.exception('Не удалось запросить фото для мульт-заказа %s:', order['order_id'])
                await update_cartoon_order(order['order_id'], next_attempt_at=int(time.time()) + 60)

    async def run(self):
        while True:
            try:
                await self.process()
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.exception('Ошибка очереди оплаченных мульт-заказов:')
            await asyncio.sleep(self.poll_interval)

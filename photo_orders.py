"""Notify users and the admin about durable, paid photoshoots awaiting fulfillment."""
import asyncio
import logging
import time

from maxapi.types import LinkButton
from maxapi.utils.inline_keyboard import InlineKeyboardBuilder

from audio_orders import SUPPORT_URL
from database import get_pending_photo_jobs, update_photo_order

logger = logging.getLogger(__name__)


class PaidPhotoWorker:
    def __init__(self, bot, *, admin_user_id=None, poll_interval=2):
        self.bot = bot
        self.admin_user_id = admin_user_id
        self.poll_interval = poll_interval

    async def process(self):
        jobs = await get_pending_photo_jobs(notifications=True, notify_admin=self.admin_user_id is not None)
        for order in jobs:
            try:
                if not order['user_notified']:
                    keyboard = InlineKeyboardBuilder().row(LinkButton(text='🆘 Поддержка',url=SUPPORT_URL)).as_markup()
                    sent = await self.bot.send_message(
                        user_id=order['user_id'],
                        text=f"✅ Оплата получена! Фотосессия за {order['amount_kopecks'] // 100} ₽ сохранена в очереди. "
                             f"Номер заказа: {order['order_id']}. Фото и детали будут запрошены при обработке заказа.",
                        attachments=[keyboard])
                    if sent is None:
                        raise RuntimeError('MAX не подтвердил уведомление об оплате фотосессии.')
                    await update_photo_order(order['order_id'],user_notified=1)
                if self.admin_user_id is not None and not order['admin_notified']:
                    sent = await self.bot.send_message(
                        user_id=self.admin_user_id,
                        text=f"📸 Оплаченная фотосессия в очереди: {order['order_id']}\n"
                             f"Пользователь: {order['user_id']}\nТариф: {order['tariff']} ₽\n"
                             f"Стиль: {order['style'] or 'не выбран'}\nСюжет: {order['template'] or 'не выбран'}")
                    if sent is None:
                        raise RuntimeError('MAX не подтвердил уведомление администратора.')
                    await update_photo_order(order['order_id'],admin_notified=1)
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.exception('Не удалось доставить уведомление о фотозаказе %s:',order['order_id'])
                await update_photo_order(order['order_id'],next_attempt_at=int(time.time())+60)

    async def run(self):
        while True:
            try:
                await self.process()
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.exception('Ошибка очереди оплаченных фотосессий:')
            await asyncio.sleep(self.poll_interval)

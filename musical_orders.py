"""Постоянная очередь создания и отправки оплаченных музыкальных открыток."""
import asyncio
import logging
import time

from maxapi.types import LinkButton, CallbackButton
from maxapi.utils.inline_keyboard import InlineKeyboardBuilder

from database import (get_musical_order, update_musical_order, claim_musical_order,
                      pending_musical_orders, reset_musical_workers)
from media_service import create_musical_video, upload_video_for_max, MUSICAL_VIDEO_WAIT_MESSAGE
from audio_orders import SUPPORT_URL
from config import DEFAULT_BOT_LINK, validate_bot_link
from video_delivery import send_video_share
from promo_service import send_repeat_offer

logger = logging.getLogger(__name__)


class PaidMusicalWorker:
    def __init__(self, bot, *, renderer=None, uploader=None, poll_interval=2, bot_link=DEFAULT_BOT_LINK, admin_user_id=None):
        self.bot = bot
        self.admin_user_id = admin_user_id
        self.bot_link = validate_bot_link(bot_link)
        self.renderer = renderer or create_musical_video
        self.uploader = uploader or upload_video_for_max
        self.poll_interval = poll_interval

    async def run(self):
        initialized = False
        while True:
            try:
                if not initialized:
                    await reset_musical_workers()
                    initialized = True
                for order_id in await pending_musical_orders():
                    await self.process(order_id)
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.exception('Ошибка очереди музыкальных открыток:')
            await asyncio.sleep(self.poll_interval)

    async def process(self, order_id):
        waiting=await get_musical_order(order_id)
        if waiting and waiting['status']=='paid' and waiting['prepaid'] and not waiting['input_ready']:
            if not waiting['user_notified']:
                try:
                    sent=await self.bot.send_message(user_id=waiting['user_id'],
                        text='✅ Оплата музыкальной открытки получена! Нажмите кнопку, чтобы загрузить фото и продолжить создание.',
                        attachments=[InlineKeyboardBuilder().row(CallbackButton(
                            text='🎵 Продолжить создание',payload='music_resume:'+order_id)).as_markup()])
                    if sent is None:
                        raise RuntimeError('MAX не подтвердил уведомление об оплате.')
                    await update_musical_order(order_id,user_notified=1)
                except Exception:
                    logger.exception('Не удалось отправить уведомление о предоплате %s:',order_id)
                    await update_musical_order(order_id,next_attempt_at=int(time.time())+60)
            return
        if not await claim_musical_order(order_id):
            return
        order = None
        try:
            order = await get_musical_order(order_id)
            result = order['result_url']
            if not result:
                if not order['prepaid']:
                    try:
                        await self.bot.send_message(user_id=order['user_id'], text=MUSICAL_VIDEO_WAIT_MESSAGE)
                    except Exception:
                        logger.warning('Не удалось отправить уведомление о сборке заказа %s.', order_id)
                result = await self.renderer(order['image_url'], order['audio_url'])
                if not isinstance(result, str) or not result.startswith('https://'):
                    raise ValueError('Не получена HTTPS-ссылка на музыкальную открытку.')
                await asyncio.shield(update_musical_order(order_id, result_url=result))
            video = await self.uploader(self.bot, result)
            keyboard = InlineKeyboardBuilder().row(LinkButton(text='⬇️ Скачать MP4', url=result)).as_markup()
            sent = await self.bot.send_message(user_id=order['user_id'],
                text='🎵 Ваша музыкальная открытка готова!', attachments=[video, keyboard])
            if sent is None:
                raise RuntimeError('MAX не подтвердил отправку музыкальной открытки.')
            await update_musical_order(order_id, delivered=1, error=None,promo_due=1)
            await send_repeat_offer(self.bot,order['user_id'],'music_card','music:'+order_id,admin_user_id=self.admin_user_id)
            await send_video_share(self.bot, order['user_id'], bot_link=self.bot_link)
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception('Ошибка музыкального заказа %s:', order_id)
            attempts = (order or {}).get('attempts', 0) + 1
            await update_musical_order(order_id, attempts=attempts, failed=int(attempts >= 3),
                next_attempt_at=int(time.time()) + 60, error='render_or_delivery_failed')
            if attempts >= 3 and order:
                try:
                    await self.bot.send_message(user_id=order['user_id'], text=
                        'Не удалось выдать музыкальную открытку. Оплата сохранена. Напишите в поддержку и укажите заказ ' + order_id,
                        attachments=[InlineKeyboardBuilder().row(LinkButton(text='🆘 Поддержка', url=SUPPORT_URL)).as_markup()])
                except Exception:
                    logger.exception('Не удалось сообщить об ошибке заказа %s:', order_id)
        finally:
            await asyncio.shield(update_musical_order(order_id, running=0))

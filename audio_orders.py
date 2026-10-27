"""Доставка оплаченных аудиозаказов из постоянной очереди SQLite."""

import asyncio
import json
import logging
from datetime import datetime, timezone

from maxapi.types import CallbackButton
from maxapi.utils.inline_keyboard import InlineKeyboardBuilder

from audio_service import generate_song, wait_for_song, song_tracks, AudioNotConfigured, SongFailed
from database import (get_audio_order, update_audio_order, pending_audio_orders,
                      claim_audio_order, reset_audio_workers)

logger = logging.getLogger(__name__)


class PaidAudioWorker:
    def __init__(self, bot, *, generator=generate_song, waiter=wait_for_song, poll_interval=2):
        self.bot, self.generator, self.waiter = bot, generator, waiter
        self.poll_interval = poll_interval

    async def run(self):
        await reset_audio_workers()
        while True:
            try:
                orders = await pending_audio_orders()
                if orders:
                    await asyncio.gather(*(self.process(order_id) for order_id in orders))
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                logger.error('Ошибка очереди аудиозаказов (%s).', type(exc).__name__)
            await asyncio.sleep(self.poll_interval)

    async def notify(self, user_id, text, **kwargs):
        await self.bot.send_message(user_id=user_id, text=text, **kwargs)

    async def process(self, order_id):
        if not await claim_audio_order(order_id):
            return
        order = await get_audio_order(order_id)
        try:
            tracks = json.loads(order['tracks_json']) if order['tracks_json'] else None
            if not tracks:
                job_id = order['job_id']
                if not job_id:
                    if order['submit_started']:
                        await self.fail(order, 'Не удалось подтвердить отправку песни в студию. Обратитесь в поддержку и укажите заказ ' + order_id)
                        return
                    if not order['error']:
                        try:
                            await self.notify(order['user_id'], '🎵 Оплата получена! Генерирую ваши треки... Это займет около 1-2 минут.')
                        except Exception:
                            logger.warning('Не удалось отправить прогресс аудиозаказа %s.', order_id)
                    await update_audio_order(order_id, submit_started=1)
                    job_id = await self.generator(order['prompt'], order['tags'], order['title'])
                    await asyncio.shield(update_audio_order(order_id, job_id=job_id))
                result = await self.waiter(job_id)
                tracks = song_tracks(result)
                await update_audio_order(order_id, tracks_json=json.dumps(tracks, ensure_ascii=False), error=None)
            text = '🎧 Ваша песня готова!' if len(tracks) == 1 else '🎧 Ваши треки готовы!'
            keyboard = InlineKeyboardBuilder()
            for index, track in enumerate(tracks):
                text += f"\n\nВариант {index + 1}: {track['title']}\n{track['audioUrl']}"
                keyboard.row(CallbackButton(text=f'Скачать вариант {index + 1}', payload=f'audio_paid_download:{order_id}:{index}'))
            keyboard.row(CallbackButton(text='Главное меню', payload='back_to_main'))
            await self.notify(order['user_id'], text, attachments=[keyboard.as_markup()])
            await update_audio_order(order_id, delivered=1, error=None)
        except asyncio.CancelledError:
            raise
        except AudioNotConfigured:
            await update_audio_order(order_id, submit_started=0, error='not_configured', next_attempt_at=self.later())
            if order['error'] != 'not_configured':
                await self.safe_notify(order, 'Оплата получена. Музыкальная студия временно недоступна; заказ сохранён, повторно платить не нужно.')
        except SongFailed:
            await self.fail(order, 'Генерация песни завершилась ошибкой. Оплата сохранена. Обратитесь в поддержку и укажите заказ ' + order_id)
        except TimeoutError:
            await update_audio_order(order_id, error='timeout', next_attempt_at=self.later())
            if order['error'] != 'timeout':
                await self.safe_notify(order, 'Песня ещё создаётся. Продолжу проверять тот же заказ, повторно платить не нужно.')
        except Exception as exc:
            logger.error('Ошибка оплаченного аудиозаказа %s (%s).', order_id, type(exc).__name__)
            current = await get_audio_order(order_id)
            if current['submit_started'] and not current['job_id']:
                await self.fail(order, 'Не удалось подтвердить отправку песни в студию. Обратитесь в поддержку и укажите заказ ' + order_id)
            else:
                await update_audio_order(order_id, error='retry', next_attempt_at=self.later())
                if not current['tracks_json'] and order['error'] != 'retry':
                    await self.safe_notify(order, 'Музыкальная студия пока не ответила. Оплата и заказ сохранены; продолжу проверять задачу, повторно платить не нужно.')
        finally:
            await asyncio.shield(update_audio_order(order_id, running=0))

    @staticmethod
    def later():
        return int(datetime.now(timezone.utc).timestamp()) + 60

    async def safe_notify(self, order, text):
        try:
            keyboard = InlineKeyboardBuilder().row(CallbackButton(text='Написать в поддержку', payload='help_support')).as_markup()
            await self.notify(order['user_id'], text, attachments=[keyboard])
        except Exception:
            logger.warning('Не удалось уведомить пользователя об аудиозаказе %s.', order['order_id'])

    async def fail(self, order, text):
        await update_audio_order(order['order_id'], failed=1, error='generation_failed')
        await self.safe_notify(order, text)

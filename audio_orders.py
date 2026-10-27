"""Доставка оплаченных аудиозаказов из постоянной очереди SQLite."""

import asyncio
import json
import logging
from datetime import datetime, timezone

from maxapi.types import CallbackButton, LinkButton
from maxapi.utils.inline_keyboard import InlineKeyboardBuilder

from audio_service import generate_song, wait_for_song, song_tracks, write_song_lyrics, make_protected_preview, AudioNotConfigured, SongFailed
from database import (get_audio_order, update_audio_order, pending_audio_orders,
                      claim_audio_order, reset_audio_workers)

logger = logging.getLogger(__name__)
SUPPORT_URL = 'https://max.ru/u/f9LHodD0cOL0i7XGM4uyyUo-bFy4nt4Hqc-XMNmvGygT83X2wUkr6O7DRXo'


class PaidAudioWorker:
    def __init__(self, bot, *, generator=generate_song, waiter=wait_for_song, poll_interval=2,
                 lyric_writer=None, preview_maker=None, payments=None, admin_user_id=None):
        self.bot, self.generator, self.waiter = bot, generator, waiter
        self.poll_interval = poll_interval
        self.lyric_writer = lyric_writer or write_song_lyrics
        self.preview_maker = preview_maker or make_protected_preview
        self.payments, self.admin_user_id = payments, admin_user_id

    async def deliver_preview(self, order):
        keyboard = InlineKeyboardBuilder().row(LinkButton(text='▶️ Послушать превью', url=order['preview_url']))
        unlocked = order['status'] == 'paid' or order['free_access']
        if unlocked:
            keyboard.row(CallbackButton(text='Скачать полный MP3', payload='audio_paid_download:' + order['order_id'] + ':0'))
        elif order['user_id'] == self.admin_user_id:
            keyboard.row(CallbackButton(text='👑 Получить полный трек бесплатно', payload='audio_admin_full:' + order['order_id']))
        else:
            link = order['payment_link']
            if not link and self.payments is not None:
                try:
                    link = await self.payments.create_audio_payment_link(
                        order['user_id'], 'gift', order['scenario'], order['details'], order['style'],
                        order['prompt'], order['tags'], order['title'], order_id=order['order_id'])
                except Exception:
                    logger.warning('Не удалось подготовить оплату превью %s.', order['order_id'])
            keyboard.row(LinkButton(text='Оплатить 490 ₽ и получить полный трек', url=link) if link else
                         CallbackButton(text='Оплатить 490 ₽ и получить полный трек', payload='audio_gift_checkout:' + order['order_id']))
        keyboard.row(CallbackButton(text='Главное меню', payload='back_to_main'))
        text = ('🎧 Полная песня уже доступна. Ниже — фрагмент и кнопка скачивания MP3.' if unlocked else
                '🎧 Ваша песня готова! Послушайте фрагмент. Если всё нравится, оплатите доступ к полному MP3 без защиты, минусовке и тексту.')
        await self.notify(order['user_id'],
            text + '\n\nМинусовка пока недоступна; полный трек и текст выдаются автоматически.\n\n' + order['preview_url'],
            attachments=[keyboard.as_markup()])

    @staticmethod
    def result_actions(keyboard, order):
        if order['audio_format'] == 'gift':
            keyboard.row(CallbackButton(text='Скачать минусовку', payload='audio_paid_instrumental:' + order['order_id']))
            keyboard.row(CallbackButton(text='Текст песни', payload='audio_paid_lyrics:' + order['order_id']))
        if not order['regeneration_used']:
            keyboard.row(CallbackButton(text='🔄 Не то, ещё раз (бесплатно)', payload='audio_regenerate:' + order['order_id']))
        keyboard.row(LinkButton(text='🆘 Написать в поддержку', url=SUPPORT_URL))
        return keyboard

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
                    lyrics = order['lyrics'] or await self.lyric_writer(order['prompt'], order['tags'], order['title'])
                    await update_audio_order(order_id, lyrics=lyrics)
                    if not order['error']:
                        try:
                            await self.notify(order['user_id'], '🎵 Генерирую ваши треки... Это займет около 1-2 минут.')
                        except Exception:
                            logger.warning('Не удалось отправить прогресс аудиозаказа %s.', order_id)
                    await update_audio_order(order_id, submit_started=1)
                    job_id = await self.generator(lyrics, order['tags'], order['title'])
                    await asyncio.shield(update_audio_order(order_id, job_id=job_id))
                result = await self.waiter(job_id)
                tracks = song_tracks(result)
                await update_audio_order(order_id, tracks_json=json.dumps(tracks, ensure_ascii=False), error=None)
            order = await get_audio_order(order_id)
            if order['status'] != 'paid' and not order['free_access']:
                if not order['preview_url']:
                    preview_url = await self.preview_maker(tracks[0]['audioUrl'])
                    if preview_url in {track['audioUrl'] for track in tracks}:
                        raise ValueError('Превью не должно быть полным треком.')
                    await update_audio_order(order_id, preview_url=preview_url)
                    order = await get_audio_order(order_id)
                # Платёж мог прийти, пока создавался фрагмент.
                if order['status'] != 'paid' and not order['free_access']:
                    await self.deliver_preview(order)
                    await update_audio_order(order_id, preview_delivered=1)
                    return
            text = '🎧 Ваша песня готова!' if len(tracks) == 1 else '🎧 Ваши треки готовы!'
            keyboard = InlineKeyboardBuilder()
            for index, track in enumerate(tracks):
                text += f"\n\nВариант {index + 1}: {track['title']}\n{track['audioUrl']}"
                keyboard.row(CallbackButton(text=f'Скачать вариант {index + 1}', payload=f'audio_paid_download:{order_id}:{index}'))
            self.result_actions(keyboard, order)
            keyboard.row(CallbackButton(text='Главное меню', payload='back_to_main'))
            await self.notify(order['user_id'], text, attachments=[keyboard.as_markup()])
            await update_audio_order(order_id, delivered=1, error=None)
        except asyncio.CancelledError:
            raise
        except AudioNotConfigured:
            await update_audio_order(order_id, submit_started=0, error='not_configured', next_attempt_at=self.later())
            if order['error'] != 'not_configured':
                await self.safe_notify(order, 'Музыкальная студия временно недоступна; заказ сохранён. Не создавайте повторный заказ.')
        except SongFailed:
            await self.fail(order, 'Генерация песни завершилась ошибкой. Заказ сохранён. Обратитесь в поддержку и укажите заказ ' + order_id)
        except TimeoutError:
            await update_audio_order(order_id, error='timeout', next_attempt_at=self.later())
            if order['error'] != 'timeout':
                await self.safe_notify(order, 'Песня ещё создаётся. Продолжу проверять тот же заказ.')
        except Exception as exc:
            logger.error('Ошибка оплаченного аудиозаказа %s (%s).', order_id, type(exc).__name__)
            current = await get_audio_order(order_id)
            if current['submit_started'] and not current['job_id']:
                await self.fail(order, 'Не удалось подтвердить отправку песни в студию. Обратитесь в поддержку и укажите заказ ' + order_id)
            else:
                await update_audio_order(order_id, error='retry', next_attempt_at=self.later())
                if not current['tracks_json'] and order['error'] != 'retry':
                    await self.safe_notify(order, 'Музыкальная студия пока не ответила. Заказ сохранён; продолжу проверять задачу.')
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

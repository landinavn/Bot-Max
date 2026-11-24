"""Постоянная очередь создания и отправки оплаченных музыкальных открыток."""
import asyncio
import json
import logging
import time

from maxapi.types import LinkButton, CallbackButton
from maxapi.utils.inline_keyboard import InlineKeyboardBuilder
from result_navigation import result_keyboard as completed_keyboard

from database import (get_musical_order, update_musical_order, claim_musical_order,
                      pending_musical_orders, reset_musical_workers, save_musical_draft, set_musical_inputs)
from media_service import create_musical_video, upload_video_for_max, MUSICAL_VIDEO_WAIT_MESSAGE
from audio_orders import SUPPORT_URL
from config import DEFAULT_BOT_LINK, validate_bot_link
from video_delivery import send_video_share
from promo_service import send_repeat_offer

from audio_service import generate_song, wait_for_song, song_audio_url, validate_song_arguments

logger = logging.getLogger(__name__)


class SongSubmissionUnknown(RuntimeError):
    """A music POST may have succeeded; never submit another automatically."""


class PaidMusicalWorker:
    def __init__(self, bot, *, renderer=None, uploader=None, poll_interval=2, song_generator=None, song_waiter=None, bot_link=DEFAULT_BOT_LINK, admin_user_id=None):
        self.bot = bot
        self.admin_user_id = admin_user_id
        self.bot_link = validate_bot_link(bot_link)
        self.renderer = renderer or create_musical_video
        self.uploader = uploader or upload_video_for_max
        self.poll_interval = poll_interval
        self.song_generator = song_generator or generate_song
        self.song_waiter = song_waiter or wait_for_song

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

    async def prepare_upsell(self, order):
        """Called only under a paid-order claim; resume the same music job after restart."""
        draft = json.loads(order['draft'])
        validate_song_arguments(draft.get('lyrics'), draft.get('tags'), draft.get('title'))
        async def checkpoint():
            await save_musical_draft(order['order_id'], order['user_id'], draft, 'phrase')
        if not draft.get('audio_url'):
            if not draft.get('song_job_id'):
                if draft.get('song_submitting'):
                    raise SongSubmissionUnknown('Неизвестен результат отправки песни.')
                draft['song_submitting'] = True
                await checkpoint()
                try:
                    job_id = await self.song_generator(draft['lyrics'], draft['tags'], draft['title'])
                    if not isinstance(job_id, str) or not job_id.strip():
                        raise ValueError('Не получен номер задания песни.')
                except asyncio.CancelledError:
                    raise
                except Exception as exc:
                    raise SongSubmissionUnknown('Требуется проверка отправленного запроса песни.') from exc
                draft['song_job_id'] = job_id
                await asyncio.shield(checkpoint())
            result = await self.song_waiter(draft['song_job_id'])
            draft['audio_url'] = song_audio_url(result)
            await asyncio.shield(checkpoint())
        await set_musical_inputs(order['order_id'], order['user_id'], order['image_url'], draft['audio_url'])

    async def process(self, order_id):
        waiting=await get_musical_order(order_id)
        if waiting and waiting['status']=='paid' and waiting['prepaid'] and waiting['tariff'] != 'upsell' and not waiting['input_ready']:
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
            if order['prepaid'] and order['tariff'] == 'upsell' and not order['input_ready']:
                if not order['user_notified']:
                    try:
                        sent = await self.bot.send_message(user_id=order['user_id'], text=
                            '✅ Оплата получена! Создаём песню и музыкальную открытку. Готовое видео пришлём сюда.')
                        if sent is not None:
                            await update_musical_order(order_id, user_notified=1)
                    except Exception:
                        logger.warning('Не удалось сообщить о создании оплаченного апгрейда %s.', order_id)
                await self.prepare_upsell(order)
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
            keyboard = completed_keyboard(InlineKeyboardBuilder().row(LinkButton(text='⬇️ Скачать MP4', url=result)))
            sent = await self.bot.send_message(user_id=order['user_id'],
                text='🎵 Ваша музыкальная открытка готова!', attachments=[video, keyboard])
            if sent is None:
                raise RuntimeError('MAX не подтвердил отправку музыкальной открытки.')
            if order.get('mode')=='mode_mother':
                from mother_stats import record
                await record(order['user_id'],'mother_music_ready',order_id)
            await update_musical_order(order_id, delivered=1, error=None,promo_due=1)
            await send_repeat_offer(self.bot,order['user_id'],'music_card','music:'+order_id,admin_user_id=self.admin_user_id)
            await send_video_share(self.bot, order['user_id'], bot_link=self.bot_link)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            logger.exception('Ошибка музыкального заказа %s:', order_id)
            attempts = (order or {}).get('attempts', 0) + 1
            failed = isinstance(exc, SongSubmissionUnknown) or attempts >= 3
            await update_musical_order(order_id, attempts=attempts, failed=int(failed),
                next_attempt_at=int(time.time()) + 60,
                error='song_submission_unknown' if isinstance(exc, SongSubmissionUnknown) else 'render_or_delivery_failed')
            if failed and order:
                try:
                    await self.bot.send_message(user_id=order['user_id'], text=
                        'Не удалось выдать музыкальную открытку. Оплата сохранена. Напишите в поддержку и укажите заказ ' + order_id,
                        attachments=[InlineKeyboardBuilder().row(LinkButton(text='🆘 Поддержка', url=SUPPORT_URL)).as_markup()])
                except Exception:
                    logger.exception('Не удалось сообщить об ошибке заказа %s:', order_id)
        finally:
            await asyncio.shield(update_musical_order(order_id, running=0))

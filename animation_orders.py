"""Durable prepaid photo-animation jobs. A saved Kling task is never recreated on retry."""
import asyncio
import logging
import time

from maxapi.types import CallbackButton, LinkButton
from maxapi.utils.inline_keyboard import InlineKeyboardBuilder

from audio_orders import SUPPORT_URL
from database import (animation_work_items, get_animation_order, update_animation_order,
    get_animation_job, claim_animation_job, update_animation_job)
from media_service import upload_video_for_max
from video_service import (generate_video_from_avatar, PHOTO_ANIMATION_PROMPT,
                           VideoTaskFailed, _is_https_url, poll_video_task)

logger = logging.getLogger(__name__)


class PaidAnimationWorker:
    def __init__(self,bot,*,generator=None,poller=None,uploader=None,admin_user_id=None,poll_interval=3):
        self.bot=bot
        self.generator=generator or generate_video_from_avatar
        self.poller=poller or poll_video_task
        self.uploader=uploader or upload_video_for_max
        self.admin_user_id=admin_user_id
        self.poll_interval=poll_interval

    async def notify(self,order_id):
        order=await get_animation_order(order_id)
        keyboard=InlineKeyboardBuilder().row(CallbackButton(text='📷 Загрузить фотографию',
            payload='animation_resume:'+order_id)).as_markup()
        sent=await self.bot.send_message(user_id=order['user_id'],
            text=f"✅ Пакет оживления фото доступен: {order['quantity']} фото. Каждый ролик — 5 секунд. Нажмите кнопку и отправьте фото.",
            attachments=[keyboard])
        if sent is None: raise RuntimeError('MAX не подтвердил запрос фото.')
        await update_animation_order(order_id,notified=1)

    async def process(self,job_id):
        if not await claim_animation_job(job_id): return
        job=await get_animation_job(job_id)
        try:
            result=job['video_url']
            if not result:
                if job['status']=='creating' and not job['task_id']:
                    raise ValueError('Результат отправки платного запроса неизвестен; требуется ручная сверка.')
                if not job['task_id']:
                    # Commit before a paid POST. An interrupted unknown POST goes to support, never a second charge.
                    await update_animation_job(job_id,status='creating')
                async def save_task(task_id):
                    await asyncio.shield(update_animation_job(job_id,task_id=task_id,status='processing'))
                if job['task_id']:
                    result=await self.poller(job['task_id'])
                else:
                    result=await self.generator(job['image_url'],PHOTO_ANIMATION_PROMPT,mode='photo_animation',
                        task_id=None,on_task_created=save_task)
                if not _is_https_url(result):
                    raise ValueError('Kling не вернул HTTPS-ссылку на видео.')
                await asyncio.shield(update_animation_job(job_id,video_url=result,status='ready'))
            video=await self.uploader(self.bot,result)
            keyboard=InlineKeyboardBuilder().row(LinkButton(text='⬇️ Скачать видео',url=result)).row(
                CallbackButton(text='📷 Ещё фотография',payload='animation_resume:'+job['order_id'])).as_markup()
            sent=await self.bot.send_message(user_id=job['user_id'],text='✨ Ваша оживлённая фотография готова! Видео — 5 секунд.',
                                             attachments=[video,keyboard])
            if sent is None: raise RuntimeError('MAX не подтвердил отправку видео.')
            await update_animation_job(job_id,status='delivered',error=None)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            logger.exception('Ошибка оживления фото %s:',job_id)
            current=await get_animation_job(job_id)
            attempts=current['attempts']+1
            # A known processing task or already rendered MP4 may be retried without another paid POST.
            retry=bool(attempts<3 and (current['video_url'] or
                (current['task_id'] and not isinstance(exc,VideoTaskFailed))))
            status=('ready' if current['video_url'] else 'processing') if retry else 'manual'
            await update_animation_job(job_id,status=status,
                attempts=attempts,next_attempt_at=int(time.time())+60,error='generation_or_delivery_failed')
            if not retry:
                try:
                    await self.bot.send_message(user_id=job['user_id'],text=
                        '😔 Не удалось завершить оживление фото. Оплата и фотография сохранены. '
                        'Мы поможем завершить заказ. Укажите в поддержке номер '+job_id,
                        attachments=[InlineKeyboardBuilder().row(LinkButton(text='🆘 Поддержка',url=SUPPORT_URL)).as_markup()])
                    if self.admin_user_id is not None:
                        await self.bot.send_message(user_id=self.admin_user_id,
                            text=f"⚠️ Оживление фото требует проверки: {job_id}, клиент {job['user_id']}. Платный запрос автоматически не повторяется.")
                except Exception:
                    logger.exception('Не удалось уведомить об ошибке оживления %s:',job_id)
        finally:
            await asyncio.shield(update_animation_job(job_id,running=0))

    async def run(self):
        first=True
        while True:
            try:
                notices,jobs=await animation_work_items(reset=first)
                first=False
                for order_id in notices:
                    try: await self.notify(order_id)
                    except Exception:
                        logger.exception('Не доставлен запрос фото %s:',order_id)
                        await update_animation_order(order_id,notify_next_attempt_at=int(time.time())+60)
                await asyncio.gather(*(self.process(job_id) for job_id in jobs))
            except asyncio.CancelledError: raise
            except Exception: logger.exception('Ошибка очереди оживления фото:')
            await asyncio.sleep(self.poll_interval)

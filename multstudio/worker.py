"""Durable admin-only worker. GET retries are safe; unknown paid POSTs are manual."""
import asyncio
import json
import logging
import time

from . import repository as repo
from .adapters import TemporaryPollError, KlingTaskFailed
from .render import Renderer
from .diagnostics import log_preflight_failure
from .prompts import validate_scene_image_references

logger = logging.getLogger(__name__)


class Worker:
    def __init__(self, bot, admin_id, settings, providers, assets):
        self.bot, self.admin_id, self.settings = bot, admin_id, settings
        self.providers, self.assets = providers, assets
        self.renderer = Renderer(assets)
        self._notice_lock = asyncio.Lock()
        # Reuse the same MAX token while its video is processing / delivery retries.
        # This cache never contains provider tasks and cannot resubmit a paid job.
        self._media_uploads = {}

    async def heartbeat(self, key):
        while True:
            await asyncio.sleep(30)
            await repo.update_job(key, lease_until=int(time.time())+120)

    async def save_result(self, job, snap, *, data=None, url=None, suffix='png'):
        await self.assets.retain(snap, job['kind'], data=data, url=url, suffix=suffix, job_id=job['id'])
        await repo.update_job(job['id'],status='publishing')
        await self.publish_result(job)

    async def publish_result(self, job):
        _,result_url=await self.assets.result_for_job(job)
        current = await repo.job(job['id'])
        await repo.update_job(job['id'], status='done', actual=0 if job['kind']=='assemble' else current['actual'],
                              result_url=result_url,lease_until=0,error=None,notified=0)
        logger.info('Результат сохранён: %s',result_url)
        await self.notices()

    async def execute(self, item):
        key, kind = item['id'], item['kind']
        snap = json.loads(item['snapshot'])
        snap['job_id'] = key
        preflight = item['status']=='preparing'
        beat = asyncio.create_task(self.heartbeat(key))
        try:
            if item['status']=='publishing':
                await self.publish_result(item)
                return
            if kind == 'video' and item['status'] == 'polling':
                result = dict(url=item['result_url']) if item['result_url'] else await self.providers.kling.poll(item['provider_id'])
                if result is None:
                    await repo.update_job(key, lease_until=0, next_attempt=int(time.time())+15)
                    return
                await repo.update_job(key, result_url=result['url'], usage=repo.encoded(result.get('usage', {})))
                await self.save_result(item,snap,url=result['url'],suffix='mp4')
                return
            if kind == 'assemble':
                path = await self.renderer.assemble(snap)
                await self.save_result(item,snap,data=path.read_bytes(),suffix='mp4')
                return
            self.providers.preflight(kind)
            if kind == 'image':
                validate_scene_image_references(snap)
            # Persist exact approved inputs before the paid request.
            await self.assets.retain(snap,'job_source',data=repo.encoded(snap).encode(),suffix='json',job_id=key)
            image_url = None
            if kind == 'video':
                image_url = await self.assets.asset_url(snap['assets']['image'])
            if not await repo.begin_submission(key):
                return
            preflight = False
            if kind == 'image':
                data = await self.providers.image(snap)
                await self.save_result(item,snap,data=data)
            elif kind == 'video':
                task = await self.providers.kling.submit(image_url, repo.encoded(snap['spec']) +
                    f" Complete the main action within {snap['spec']['duration']} seconds, then hold naturally. "
                    'Preserve all reference identities and apartment. No subtitles, advertising or audio.', snap['quote']['duration'])
                await asyncio.shield(repo.update_job(key,status='polling',provider_id=task,lease_until=0,
                                                    next_attempt=int(time.time())+15))
            elif kind == 'speech':
                paths = []
                for line in snap['spec']['dialogue']:
                    data = await self.providers.speech_line(snap,line)
                    relative, checksum = await self.assets.write(data,'mp3',job_id=key,name=f'speech-line-{len(paths)+1}.mp3')
                    await repo.add_asset(snap['project_id'],snap['target'],snap['id'],'speech_line',
                        snap['context_hash'],relative,checksum,job_id=key,metadata=repo.encoded(line))
                    paths.append(self.assets.path(relative))
                result = await self.renderer.speech(paths)
                await self.save_result(item,snap,data=result.read_bytes(),suffix='mp3')
        except asyncio.CancelledError:
            # Keep submitting + lease as durable evidence; recovery never resubmits it.
            raise
        except Exception as exc:
            # Diagnose before persistence: even a subsequent SQLite error must not hide the original stack.
            if preflight:
                log_preflight_failure(logger,key,exc,self.settings,self.providers)
            current = await repo.job(key)
            attempts = current['attempts']+1
            polling = current['status']=='polling' and current['provider_id']
            if current['status']=='publishing' and attempts < 5:
                await repo.update_job(key,status='publishing',attempts=attempts,lease_until=0,
                    next_attempt=int(time.time())+min(300,15*2**attempts),error='temporary_publication_failure')
            elif polling and attempts < 5 and (isinstance(exc, TemporaryPollError) or current['result_url']):
                await repo.update_job(key,status='polling',attempts=attempts,lease_until=0,
                    next_attempt=int(time.time())+min(300,15*2**attempts),error='temporary_poll_or_download')
            else:
                state = ('failed' if isinstance(exc,KlingTaskFailed) else
                         'preflight_failed' if current['status']=='preparing' else 'manual')
                await repo.update_job(key,status=state,error=type(exc).__name__,lease_until=0,notified=0)
            # Do not log exception contents: provider errors may contain credentials/personal text.
            if not preflight:
                logger.warning('Мультстудия: задание %s требует проверки (%s).',key,type(exc).__name__)
        finally:
            beat.cancel()
            try:
                await beat
            except asyncio.CancelledError:
                pass

    async def notices(self):
        if not callable(getattr(self.bot,'send_message',None)):
            return
        async with self._notice_lock:
            await self._notices()

    async def _notices(self):
        async with repo.connect() as db:
            items = await repo.rows(db, "SELECT * FROM cartoon_jobs WHERE status IN ('done','manual','failed','preflight_failed') AND notified=0 LIMIT 10")
        for item in items:
            text = f"Мультстудия: {item['kind']}, задание {item['id']}, статус {item['status']}. "
            if item['actual'] is None and item['status'] != 'preflight_failed':
                text += 'Фактическую стоимость сверьте с провайдером; резерв бюджета сохранён. '
            text += 'Откройте /multstudio → проект → Сцены → Задания и расходы.'
            try:
                if item['status']=='done' and repo.is_media_job(item):
                    _,url=await self.assets.result_for_job(item)
                    sent=await self.send_result_to_admin(item['id'],item['kind'],url)
                else:
                    sent=await self.bot.send_message(user_id=self.admin_id,text=text)
                if sent is not None:
                    await repo.update_job(item['id'],notified=1)
                    self._media_uploads.pop(item['id'],None)
                    logger.info('Мультстудия: результат доставлен job=%s kind=%s',item['id'],item['kind'])
            except Exception as exc:
                logger.warning('Мультстудия: не доставлено уведомление job=%s kind=%s error=%s; повторится только доставка.',
                               item['id'],item['kind'],type(exc).__name__)

    async def send_result_to_admin(self, job_id, kind, public_url):
        """Send a retained result only; never enter generation or billing paths."""
        from maxapi.types import CallbackButton, LinkButton
        from maxapi.utils.inline_keyboard import InlineKeyboardBuilder
        item=await repo.job(job_id)
        asset=await repo.job_result(job_id)
        if not item or item['status']!='done' or item['kind']!=kind or not asset:
            raise ValueError('Готовый результат задания не найден.')
        media=self._media_uploads.get(job_id)
        image=repo.is_image_job(item)
        if media is None:
            if image:
                from image_service import upload_image_for_max
                media=await upload_image_for_max(self.bot,public_url)
            elif kind=='speech':
                from media_service import upload_audio_for_max
                media=await upload_audio_for_max(self.bot,public_url)
            elif kind in ('video','assemble','episode_assembly'):
                from media_service import upload_video_for_max
                media=await upload_video_for_max(self.bot,public_url)
            else:
                raise ValueError('Для этого типа задания нет медиа-доставки.')
            expected='image' if image else 'audio' if kind=='speech' else 'video'
            if getattr(media.type,'value',media.type)!=expected:
                raise ValueError('MAX вернул вложение неверного типа.')
            self._media_uploads[job_id]=media
        attachments=[media]
        if image:
            character=item['target']=='character'
            text='✅ Эталон сгенерирован!' if character else '✅ Изображение сцены сгенерировано!'
            approve='studio:approve_reference:'+job_id if character else 'studio:approve:'+asset['id']
            keyboard=InlineKeyboardBuilder().row(CallbackButton(text='✅ Утвердить',payload=approve)).row(
                CallbackButton(text='🔄 Перегенерировать',payload=f"studio:make:image:{item['target']}:{item['target_id']}")).as_markup()
            attachments.append(keyboard)
        elif kind=='video':
            snap=json.loads(item['snapshot'])
            number=snap.get('number')
            if number is None:
                scene=await repo.get('scene',item['target_id'])
                number=scene['number'] if scene else '?'
            text=f'🎬 Сцена {number} оживлена! Посмотрите результат.'
            keyboard=InlineKeyboardBuilder().row(CallbackButton(
                text='✅ Утвердить',payload='studio:approve_video:'+asset['id'])).row(CallbackButton(
                text='🔄 Перегенерировать',payload='studio:make:video:scene:'+item['target_id'])).as_markup()
            attachments.append(keyboard)
        elif kind=='speech':
            text='🎧 Озвучка готова'
        else:
            text='🎉 Мультфильм собран! Скачайте или отправьте в соцсети.'
            attachments.append(InlineKeyboardBuilder().row(LinkButton(
                text='📥 Скачать MP4',url=public_url)).as_markup())
        return await self.bot.send_message(user_id=self.admin_id,text=text,attachments=attachments)

    async def lane(self):
        while True:
            try:
                item = await repo.claim()
                if item:
                    await self.execute(item)
                else:
                    await asyncio.sleep(3)
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.warning('Мультстудия: временная ошибка очереди.')
                await asyncio.sleep(5)

    async def run(self):
        if not self.admin_id or not self.settings.enabled:
            return
        lanes = [asyncio.create_task(self.lane()) for _ in range(self.settings.concurrency)]
        try:
            while True:
                await self.notices()
                await asyncio.sleep(5)
        finally:
            for task in lanes:
                task.cancel()
            await asyncio.gather(*lanes, return_exceptions=True)

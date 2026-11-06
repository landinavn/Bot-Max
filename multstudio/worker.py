"""Durable admin-only worker. GET retries are safe; unknown paid POSTs are manual."""
import asyncio
import json
import logging
import time

from . import repository as repo
from .adapters import TemporaryPollError, KlingTaskFailed
from .render import Renderer
from .diagnostics import log_preflight_failure

logger = logging.getLogger(__name__)


class Worker:
    def __init__(self, bot, admin_id, settings, providers, assets):
        self.bot, self.admin_id, self.settings = bot, admin_id, settings
        self.providers, self.assets = providers, assets
        self.renderer = Renderer(assets)
        self._notice_lock = asyncio.Lock()

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
                attachments=[]
                if item['status']=='done' and repo.is_image_job(item):
                    from image_service import upload_image_for_max
                    from maxapi.types import CallbackButton
                    from maxapi.utils.inline_keyboard import InlineKeyboardBuilder
                    asset,url=await self.assets.result_for_job(item)
                    image=await upload_image_for_max(self.bot,url)
                    character=item['target']=='character'
                    text='✅ Эталон сгенерирован!' if character else '✅ Изображение сцены сгенерировано!'
                    approve='studio:approve_reference:'+item['id'] if character else 'studio:approve:'+asset['id']
                    keyboard=InlineKeyboardBuilder().row(CallbackButton(text='✅ Утвердить',payload=approve)).row(
                        CallbackButton(text='🔄 Перегенерировать',payload=f"studio:make:image:{item['target']}:{item['target_id']}")).as_markup()
                    attachments=[image,keyboard]
                sent = await self.bot.send_message(user_id=self.admin_id,text=text,**({'attachments':attachments} if attachments else {}))
                if sent is not None:
                    await repo.update_job(item['id'],notified=1)
            except Exception:
                logger.warning('Мультстудия: не доставлено уведомление %s.',item['id'])

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

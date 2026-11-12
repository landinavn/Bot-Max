"""Administrative Ded Moroz workflow. SQLite survives restart; paid POSTs never auto-repeat."""
import asyncio
import json
import logging
import os
import shutil
from dataclasses import dataclass, field
from pathlib import Path
from uuid import uuid4

import aiosqlite
from maxapi.types import CallbackButton, InputMedia
from maxapi.enums import UploadType
from maxapi.utils.inline_keyboard import InlineKeyboardBuilder

import database
from config import Config, BASE_DIR
from heygen import HeyGenAdapter
from multstudio.adapters import YandexTTSAdapter
from multstudio.assets import Assets, https_url
from multstudio.uploads import png_bytes

logger=logging.getLogger(__name__)
STUB='🎅 Поздравление от Деда Мороза уже в разработке! Скоро здесь можно будет заказать персональное видео с вашим ребёнком.'
TEMPLATE=('Здравствуй, {name}! Это я, Дедушка Мороз! Я весь год наблюдал за тобой. '
    'И знаешь что? Ты меня очень порадовал! Я слышал, что ты {fact1}. А ещё ты {fact2}. '
    'И особенно мне нравится, что ты {fact3}. Молодец! Я тобой очень горжусь! '
    'В новом году у меня для тебя будет маленькое задание: {task}. Я знаю, у тебя всё получится! '
    'Я получил твоё письмо, {name}. И приготовил для тебя кое-что очень особенное! '
    'С Новым годом, {name}! До встречи!')
GREETING=(
    '🎅 Добро пожаловать в мастерскую Деда Мороза!\n\n'
    'Давайте создадим персональное видео-поздравление для вашего ребёнка.\n\n'
    'Как зовут ребёнка или как вы ласково его называете? (Например: «Маша» или «Машенька»)')
BLANK_TEMPLATE=(
    'Здравствуй, [ИМЯ]! Это я, Дедушка Мороз!\n'
    'Я весь год наблюдал за тобой. И знаешь что? Ты меня очень порадовал!\n'
    'Я слышал, что ты [УВЛЕЧЕНИЕ].\n'
    'А ещё ты [ДОСТИЖЕНИЕ].\n'
    'И особенно мне нравится, что ты [КАЧЕСТВО].\n'
    'Молодец! Я тобой очень горжусь!\n'
    'В новом году у меня для тебя будет маленькое задание: [ЗАДАНИЕ].\n'
    'Я знаю, у тебя всё получится!\n'
    'Я получил твоё письмо, [ИМЯ]. И приготовил для тебя кое-что очень особенное!\n'
    'С Новым годом, [ИМЯ]! До встречи!')
EXPLANATION=(
    'Отлично! Чтобы Дед Мороз поздравил вашего ребёнка по-настоящему лично, нам нужно добавить немного деталей.\n\n'
    'Вот как будет звучать поздравление:\n\n«'+BLANK_TEMPLATE+'»\n\n'
    'Мы зададим вам несколько коротких вопросов по очереди.')
PHOTO_PROMPT='📸 Отправьте фотографию ребёнка. Она появится в видео на несколько секунд. Лучше всего подойдёт чёткое фото анфас.'
AUDIO_GENERATING='🎙 Генерирую озвучку Деда Мороза. Это займет около минуты...'
QUESTIONS=[
    ('name',GREETING),
    ('fact1','Заполните: «Я слышал, что ты [УВЛЕЧЕНИЕ]».\n\nНапишите фразу так, как она должна звучать после «что ты…». Например: «любишь рисовать», «занимаешься футболом», «обожаешь конструкторы».'),
    ('fact2','Заполните: «А ещё ты [ДОСТИЖЕНИЕ]».\n\nНапишите фразу так, как она должна звучать после «ты…». Например: «научился плавать», «пошёл в первый класс», «выиграл соревнование».'),
    ('fact3','Заполните: «И особенно мне нравится, что ты [КАЧЕСТВО]».\n\nНапишите качество в нужной форме. Например: «добрый», «любознательный», «смелый», «весёлый», «заботливый».'),
    ('task','Заполните: «В новом году у меня для тебя будет маленькое задание: [ЗАДАНИЕ]».\n\nНапример: «помогать маме», «читать перед сном», «делать зарядку».'),
]


@dataclass(frozen=True)
class Settings:
    root: Path
    examples: Path
    heygen_key: str | None=field(default=None,repr=False)
    tts_key: str | None=field(default=None,repr=False)
    avatar_id: str | None=None
    reference_url: str | None=None

    @classmethod
    def from_config(cls,config):
        return cls(Path(os.getenv('STUDIO_FILES_DIR','').strip() or str(BASE_DIR/'studio_files')),BASE_DIR/'examples',
                   os.getenv('HEYGEN_API_KEY','').strip() or None,
                   os.getenv('YANDEX_TTS_API_KEY','').strip() or None,
                   os.getenv('DED_MOROZ_AVATAR_ID','').strip() or None,
                   os.getenv('DED_MOROZ_REF_URL','').strip() or None)

    def files(self):
        # Required audio decorations belong to the video phase, never to TTS.
        return [self.examples/'audio/jingle_bells.mp3',self.examples/'audio/ny_music.mp3']

    def ready(self,config,*,avatar_known=False,stage='audio'):
        if stage not in ('audio','video'):raise ValueError('Неизвестный этап подготовки.')
        if not config.storage_bucket or not config.storage_public_base_url:raise ValueError('Настройте Yandex Cloud STORAGE_BUCKET и STORAGE_PUBLIC_BASE_URL.')
        if stage=='audio':
            if not self.tts_key:raise ValueError('Задайте YANDEX_TTS_API_KEY.')
            return
        if not self.heygen_key:raise ValueError('Задайте HEYGEN_API_KEY.')
        required=self.files()
        if not (avatar_known or self.avatar_id):
            if self.reference_url:https_url(self.reference_url)
            else:required.append(self.examples/'ded_moroz_ref.png')
        missing=[str(p.relative_to(self.examples)) for p in required if not p.is_file()]
        if missing:raise ValueError('Добавьте файлы examples/: '+', '.join(missing))
        if not shutil.which('ffmpeg') or not shutil.which('ffprobe'):raise ValueError('Установите FFmpeg и ffprobe.')


async def init_tables(db):
    await db.execute('''CREATE TABLE IF NOT EXISTS ded_moroz_orders (
        id TEXT PRIMARY KEY,user_id INTEGER NOT NULL,details TEXT NOT NULL DEFAULT '{}',
        text TEXT,version INTEGER NOT NULL DEFAULT 0,edit_used INTEGER NOT NULL DEFAULT 0,
        status TEXT NOT NULL DEFAULT 'input',photo_path TEXT,photo_url TEXT,audio_path TEXT,audio_url TEXT,
        video_id TEXT,video_url TEXT,raw_path TEXT,output_path TEXT,output_url TEXT,
        preview_sent INTEGER NOT NULL DEFAULT 0,preview_mid TEXT,preview_version INTEGER,notified INTEGER NOT NULL DEFAULT 0,error TEXT,
        next_attempt INTEGER NOT NULL DEFAULT 0,created_at TEXT DEFAULT CURRENT_TIMESTAMP)''')
    await db.execute("CREATE UNIQUE INDEX IF NOT EXISTS dm_active_user ON ded_moroz_orders(user_id) WHERE status NOT IN ('done','cancelled')")
    await db.execute('''CREATE TABLE IF NOT EXISTS ded_moroz_avatar (
        singleton INTEGER PRIMARY KEY CHECK(singleton=1),status TEXT NOT NULL,
        request_key TEXT NOT NULL,look_id TEXT,group_id TEXT,reference_url TEXT)''')


async def read(sql,args=(),*,all_rows=False):
    async with aiosqlite.connect(database.DATABASE_PATH,timeout=30) as db:
        db.row_factory=aiosqlite.Row
        async with db.execute(sql,args) as cur:
            return [dict(x) for x in await cur.fetchall()] if all_rows else (dict(row) if (row:=await cur.fetchone()) else None)


async def get(key):return await read('SELECT * FROM ded_moroz_orders WHERE id=?',(key,))


async def update(key,**values):
    allowed={'status','photo_path','photo_url','text','audio_path','audio_url','video_id','video_url',
             'raw_path','output_path','output_url','preview_sent','preview_mid','preview_version','notified','error','next_attempt','details'}
    if not values or set(values)-allowed:raise ValueError('Некорректное поле заказа.')
    async with aiosqlite.connect(database.DATABASE_PATH,timeout=30) as db:
        await db.execute('UPDATE ded_moroz_orders SET '+','.join(k+'=?' for k in values)+' WHERE id=?',(*values.values(),key));await db.commit()


async def transition(key,user,status,new_status,*,version=None):
    async with aiosqlite.connect(database.DATABASE_PATH,timeout=30) as db:
        cur=await db.execute('UPDATE ded_moroz_orders SET status=? WHERE id=? AND user_id=? AND status=?'+
            (' AND version=?' if version is not None else ''),(new_status,key,user,status,*(() if version is None else (version,))))
        await db.commit();return cur.rowcount==1


async def begin(user):
    async with aiosqlite.connect(database.DATABASE_PATH,timeout=30) as db:
        await db.execute('BEGIN IMMEDIATE');db.row_factory=aiosqlite.Row
        row=await db.execute("SELECT * FROM ded_moroz_orders WHERE user_id=? AND status NOT IN ('done','cancelled')",(user,))
        prior=await row.fetchone()
        if prior:return dict(prior)
        key=uuid4().hex;await db.execute('INSERT INTO ded_moroz_orders(id,user_id) VALUES (?,?)',(key,user));await db.commit()
    return await get(key)


def keyboard(order):
    b=InlineKeyboardBuilder().row(CallbackButton(text='✅ Всё верно, создать видео',payload='dm_audio_ok'))
    if not order['edit_used']:b.row(CallbackButton(text='✏️ Изменить текст',payload='dm_audio_edit'))
    return b.as_markup()


async def resume(key,user):
    row=await get(key)
    if not row or row['user_id']!=user or row['status']!='manual':
        raise ValueError('Нет приостановленного заказа этого администратора.')
    prior=(row['error'] or '').split(':',1)[0]
    if row['video_id']:
        target='polling'
    elif prior in ('audio_queued','audio_publish','audio_ready','video_queued','download','render','publish','deliver'):
        target=prior
    else:
        raise ValueError('Результат платного запроса неизвестен. Сначала сверьте запрос у провайдера; повтор автоматически запрещён.')
    if not await transition(key,user,'manual',target):raise ValueError('Статус заказа уже изменился.')
    await update(key,error=None,next_attempt=0)
    return target


async def request_edit(key,user,version):
    async with aiosqlite.connect(database.DATABASE_PATH,timeout=30) as db:
        cur=await db.execute("UPDATE ded_moroz_orders SET edit_used=1,status='editing' WHERE id=? AND user_id=? AND version=? AND edit_used=0 AND status='audio_ready'",(key,user,version))
        await db.commit();return cur.rowcount==1


async def revised(key,user,text):
    if not text or not text.strip() or len(text)>4500:raise ValueError('Введите текст от 1 до 4500 символов.')
    async with aiosqlite.connect(database.DATABASE_PATH,timeout=30) as db:
        cur=await db.execute("UPDATE ded_moroz_orders SET text=?,version=version+1,status='audio_queued',audio_path=NULL,audio_url=NULL,preview_sent=0,preview_mid=NULL,preview_version=NULL WHERE id=? AND user_id=? AND status='editing' AND edit_used=1",(text.strip(),key,user))
        await db.commit();return cur.rowcount==1


async def tell(event,text=None,**kwargs):
    from handlers import _reply
    return await _reply(event,text,**kwargs)


async def enter(event,context,admin):
    from handlers import PosterStates,navigation_keyboard
    user=event.get_ids()[1]
    if user!=admin or admin is None:
        await tell(event,STUB,attachments=[navigation_keyboard((('⬅️ Назад','back_to_main'),))]);return
    row=await begin(user)
    await context.clear();await context.update_data(dm_order=row['id'])
    if row['status']=='input':
        data=json.loads(row['details']);remaining=[q for k,q in QUESTIONS if k not in data]
        if remaining:
            await context.set_state(PosterStates.ded_moroz_input)
            if set(data)=={'name'}:await tell(event,EXPLANATION)
            await tell(event,remaining[0])
        else:
            await context.set_state(PosterStates.ded_moroz_photo)
            await tell(event,PHOTO_PROMPT)
    elif row['status']=='editing':
        await context.set_state(PosterStates.ded_moroz_edit);await tell(event,'Отправьте исправленный текст целиком. Доступна одна правка.')
    elif row['status']=='audio_ready':
        await update(row['id'],preview_sent=0);await tell(event,'Повторно отправляю сохранённую озвучку; новая генерация не запускается.')
    else:
        await tell(event,f"Заказ {row['id']}: {row['status']}. Состояние сохранено.")


async def collect(event,context,admin,config=None,settings=None):
    from handlers import PosterStates,is_bot_message
    if event.get_ids()[1]!=admin or admin is None or is_bot_message(event):return
    row=await get((await context.get_data()).get('dm_order'))
    if not row or row['user_id']!=admin:return
    text=(event.message.body.text or '').strip()
    state=await context.get_state()
    if state==PosterStates.ded_moroz_edit:
        if await revised(row['id'],admin,text):
            await context.clear();await tell(event,'Создаю новую озвучку. Дальнейшая правка недоступна.')
        return
    if state==PosterStates.ded_moroz_input and row['status']=='input':
        if not text or len(text)>300:raise ValueError('Введите фразу от 1 до 300 символов.')
        data=json.loads(row['details']);key=next((k for k,_ in QUESTIONS if k not in data),None)
        if key is None:return
        data[key]=text
        await update(row['id'],details=json.dumps(data,ensure_ascii=False))
        next_question=next((q for k,q in QUESTIONS if k not in data),None)
        if key=='name':await tell(event,EXPLANATION)
        if next_question:await tell(event,next_question)
        else:
            await context.set_state(PosterStates.ded_moroz_photo)
            await tell(event,PHOTO_PROMPT)
    elif state==PosterStates.ded_moroz_photo and row['status']=='input':
        config=config or Config.from_env();settings=settings or Settings.from_config(config)
        avatar=await read('SELECT * FROM ded_moroz_avatar WHERE singleton=1')
        settings.ready(config,avatar_known=bool(avatar and avatar['look_id']))
        attachments=event.message.body.attachments or []
        images=[a for a in attachments if a.type in ('image','file') and getattr(a.payload,'url',None)]
        if len(images)!=1:raise ValueError('Отправьте один JPG/PNG через «Файл».')
        assets=Assets(settings.root,config)
        attempt=uuid4().hex
        source,_=await assets.download(images[0].payload.url,'bin',job_id=row['id'],name='child-source-'+attempt+'.bin',limit=20*1024*1024,accept='image/jpeg, image/png')
        png=await asyncio.to_thread(png_bytes,assets.path(source))
        path,_=await assets.write(png,'png',job_id=row['id'],name='child-photo-'+attempt+'.png')
        alias=assets.path(f'ded_moroz_{admin}/child_photo.png');alias.parent.mkdir(parents=True,exist_ok=True)
        tmp=alias.with_name('.child-'+uuid4().hex+'.png');await asyncio.to_thread(tmp.write_bytes,png);await asyncio.to_thread(os.replace,tmp,alias)
        url=await assets.publish(path)
        text=TEMPLATE.format(**json.loads(row['details']))
        await update(row['id'],photo_path=path,photo_url=url,text=text)
        if await transition(row['id'],admin,'input','audio_queued'):
            await context.clear();await tell(event,AUDIO_GENERATING)


async def callback(event,context,admin):
    from handlers import PosterStates
    if event.get_ids()[1]!=admin or admin is None:return
    parts=event.callback.payload.split(':')
    if len(parts)==1:
        mid=getattr(getattr(event.message,'body',None),'mid',None)
        row=await read("SELECT * FROM ded_moroz_orders WHERE user_id=? AND preview_mid=? AND preview_version=version AND status='audio_ready' AND preview_sent=1",(admin,mid))
        if not row:
            await tell(event,'Эта кнопка уже использована или относится к предыдущей версии озвучки.');return
        parts=[parts[0],row['id'],str(row['version'])]
    if len(parts)!=3:return
    action,key,version=parts
    try:version=int(version)
    except ValueError:return
    row=await get(key)
    if not row or row['user_id']!=admin:return
    if action=='dm_audio_edit' and await request_edit(key,admin,version):
        await context.clear();await context.update_data(dm_order=key);await context.set_state(PosterStates.ded_moroz_edit)
        await tell(event,'Отправьте новый текст целиком. Перегенерация озвучки платная; правка доступна один раз.')
    elif action=='dm_audio_ok' and await transition(key,admin,'audio_ready','video_queued',version=version):
        await context.clear();await tell(event,'Утверждённая озвучка сохранена. Запускаю HeyGen Avatar IV и монтаж.')
    else:await tell(event,'Эта кнопка уже использована или относится к предыдущей версии озвучки.')


class Worker:
    def __init__(self,bot,admin,config,settings=None,tts=None,heygen=None):
        self.bot,self.admin,self.config=bot,admin,config
        self.settings=settings or Settings.from_config(config)
        self.assets=Assets(self.settings.root,config)
        self.tts=tts or YandexTTSAdapter(self.settings.tts_key)
        self.heygen=heygen or HeyGenAdapter(self.settings.heygen_key)
        self.uploads={}

    async def avatar(self):
        row=await read('SELECT * FROM ded_moroz_avatar WHERE singleton=1')
        if self.settings.avatar_id:
            return self.heygen.identifier(self.settings.avatar_id)
        if row and row['status']=='ready':return row['look_id']
        if row and row['status'] in ('submitting','manual'):
            raise ValueError('Создание аватара требует сверки; задайте подтверждённый DED_MOROZ_AVATAR_ID.')
        if not row:
            reference=self.settings.reference_url
            if not reference:
                data=await asyncio.to_thread((self.settings.examples/'ded_moroz_ref.png').read_bytes)
                path,_=await self.assets.write(data,'png',name='ded-moroz-reference-'+uuid4().hex+'.png')
                reference=await self.assets.publish(path)
            request_key='dm-avatar-'+uuid4().hex
            async with aiosqlite.connect(database.DATABASE_PATH,timeout=30) as db:
                await db.execute('INSERT INTO ded_moroz_avatar(singleton,status,request_key,reference_url) VALUES (1,\'submitting\',?,?)',(request_key,reference));await db.commit()
            result=await self.heygen.create_avatar(reference,request_key)
            async with aiosqlite.connect(database.DATABASE_PATH,timeout=30) as db:
                await db.execute("UPDATE ded_moroz_avatar SET status='polling',look_id=?,group_id=? WHERE singleton=1",(result['look_id'],result.get('group_id')));await db.commit()
            row=await read('SELECT * FROM ded_moroz_avatar WHERE singleton=1')
        status=await self.heygen.avatar_status(row['look_id'])
        if status.get('status')=='failed':raise ValueError('HeyGen отклонил аватар.')
        if status.get('status')!='completed':return None
        async with aiosqlite.connect(database.DATABASE_PATH,timeout=30) as db:
            await db.execute("UPDATE ded_moroz_avatar SET status='ready' WHERE singleton=1");await db.commit()
        return row['look_id']

    async def process(self,row):
        key=row['id'];state=row['status']
        if row['user_id']!=self.admin:return
        if state=='audio_queued':
            self.settings.ready(self.config,avatar_known=bool(await read('SELECT look_id FROM ded_moroz_avatar WHERE look_id IS NOT NULL')))
            if not row['photo_path'] or not self.assets.path(row['photo_path']).is_file():raise ValueError('Не найдено сохранённое фото ребёнка.')
            if not await transition(key,self.admin,'audio_queued','audio_submitting'):return
            data=await self.tts.synthesize(row['text'],'filipp','good',0.95)
            path,_=await self.assets.write(data,'mp3',job_id=key,name=f"speech-v{row['version']}.mp3")
            await update(key,audio_path=path,status='audio_publish')
        elif state=='audio_publish':
            url=await self.assets.publish(row['audio_path']);await update(key,audio_url=url,status='audio_ready',preview_sent=0)
        elif state=='audio_ready' and not row['preview_sent']:
            token_key=('audio',key,row['version'])
            audio=self.uploads.get(token_key)
            if audio is None:
                audio=await self.bot.upload_media(InputMedia(path=str(self.assets.path(row['audio_path'])),type=UploadType.AUDIO))
                self.uploads[token_key]=audio
            chunks=[row['text'][i:i+3500] for i in range(0,len(row['text']),3500)]
            for chunk in chunks[:-1]:
                if await self.bot.send_message(user_id=self.admin,text=chunk) is None:raise ValueError('MAX не подтвердил текст.')
            sent=await self.bot.send_message(user_id=self.admin,text=chunks[-1],attachments=[audio,keyboard(row)])
            if sent is None:raise ValueError('MAX не подтвердил аудио.')
            message=getattr(sent,'message',sent)  # maxapi returns SendedMessage(message=Message(...)).
            mid=getattr(getattr(message,'body',None),'mid',None)
            if not isinstance(mid,str) or not mid:raise RuntimeError('MAX не вернул ID сообщения с озвучкой.')
            await update(key,preview_sent=1,preview_mid=mid,preview_version=row['version'])
        elif state=='video_queued':
            self.settings.ready(self.config,avatar_known=bool(await read('SELECT look_id FROM ded_moroz_avatar WHERE look_id IS NOT NULL')),stage='video')
            for field_name in ('photo_path','audio_path'):
                if not row[field_name] or not self.assets.path(row[field_name]).is_file():raise ValueError('Не найден сохранённый исходник: '+field_name)
            https_url(row['audio_url'])
            avatar=await self.avatar()
            if not avatar:return
            if not await transition(key,self.admin,'video_queued','video_submitting'):return
            video=await self.heygen.create_video(avatar,row['audio_url'],'dm-video-'+key)
            await update(key,video_id=video,status='polling')
        elif state=='polling':
            result=await self.heygen.video_status(row['video_id'])
            if result.get('status')=='failed':raise ValueError('HeyGen не смог создать видео.')
            if result.get('status')=='completed':await update(key,video_url=result['video_url'],status='download')
        elif state=='download':
            try:
                path,_=await self.assets.download(row['video_url'],'mp4',job_id=key,name='heygen.mp4',limit=500*1024*1024)
            except Exception as exc:
                if getattr(getattr(exc,'response',None),'status_code',None) in (403,404):
                    await update(key,status='polling')
                    return
                raise
            await update(key,raw_path=path,status='render')
        elif state=='render':
            from video_service import render_ded_moroz
            target=self.assets.path(key+'/greeting.mp4')
            await render_ded_moroz(self.assets.path(row['raw_path']),self.assets.path(row['photo_path']),
                self.assets.path(row['audio_path']),self.settings.examples,target,json.loads(row['details'])['name'])
            await update(key,output_path=target.relative_to(self.assets.root).as_posix(),status='publish')
        elif state=='publish':
            url=await self.assets.publish(row['output_path'])
            await update(key,output_url=url,status='deliver')
        elif state=='deliver':
            token_key=('video',key)
            video=self.uploads.get(token_key)
            if video is None:
                video=await self.bot.upload_media(InputMedia(path=str(self.assets.path(row['output_path'])),type=UploadType.VIDEO))
                self.uploads[token_key]=video
            sent=await self.bot.send_message(user_id=self.admin,text='🎉 Поздравление от Деда Мороза готово! Можете скачать видео.',attachments=[video])
            if sent is None:raise ValueError('MAX не подтвердил видео.')
            async with aiosqlite.connect(database.DATABASE_PATH,timeout=30) as db:
                await db.execute('BEGIN IMMEDIATE')
                cursor=await db.execute('SELECT notified,status FROM ded_moroz_orders WHERE id=?',(key,))
                prior=await cursor.fetchone()
                if prior and prior[1]=='deliver':
                    await db.execute("UPDATE ded_moroz_orders SET status='done',notified=1,error=NULL WHERE id=?",(key,))
                    if not prior[0]:await database.increment_counter('dm_video_created',user_id=self.admin,connection=db)
                await db.commit()
            self.uploads={k:v for k,v in self.uploads.items() if k[1]!=key}

    async def tick(self):
        now=database._now_ts()
        rows=await read("SELECT * FROM ded_moroz_orders WHERE user_id=? AND status NOT IN ('input','editing','done','cancelled','manual','audio_submitting','video_submitting') AND next_attempt<=? AND (status!='audio_ready' OR preview_sent=0) ORDER BY created_at LIMIT 1",(self.admin,now),all_rows=True)
        for row in rows:
            try:await self.process(row)
            except asyncio.CancelledError:raise
            except Exception as exc:
                logger.exception('Дед Мороз: ошибка этапа %s заказа %s',row['status'],row['id'])
                current=await get(row['id'])
                ambiguous=current['status'] in ('audio_submitting','video_submitting')
                avatar=await read('SELECT status FROM ded_moroz_avatar WHERE singleton=1')
                terminal=ambiguous or (avatar and avatar['status'] in ('submitting','manual')) or isinstance(exc,ValueError)
                await update(row['id'],status='manual' if terminal else current['status'],error=current['status']+':'+type(exc).__name__,next_attempt=now+60)
                if terminal:
                    try:await self.bot.send_message(user_id=self.admin,text=f"⚠️ Заказ Деда Мороза {row['id']} требует проверки. Состояние и файлы сохранены; платный запрос автоматически не повторяется.")
                    except Exception:logger.exception('Дед Мороз: не удалось отправить предупреждение')

    async def recover_interrupted(self):
        # An interrupted paid POST without a saved ID is never sent again after restart.
        async with aiosqlite.connect(database.DATABASE_PATH,timeout=30) as db:
            await db.execute("UPDATE ded_moroz_orders SET error=status||':interrupted_paid_submission',status='manual' WHERE status IN ('audio_submitting','video_submitting')");await db.commit()

    async def run(self):
        await self.recover_interrupted()
        while True:
            try:await self.tick()
            except asyncio.CancelledError:raise
            except Exception:logger.exception('Ошибка очереди Деда Мороза')
            await asyncio.sleep(3)

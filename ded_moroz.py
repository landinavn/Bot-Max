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
from heygen_tts import (HeyGenTTSAdapter, HeyGenSpeechError, prepare_speech_text, display_text,
                        DEFAULT_VOICE_ID, DEFAULT_MODEL, DEFAULT_SPEED, DEFAULT_ECHO_DELAY_MS, DEFAULT_ECHO_DECAY)
from multstudio.assets import Assets, https_url
from multstudio.uploads import png_bytes

logger=logging.getLogger(__name__)
STUB='🎅 Поздравление от Деда Мороза уже в разработке! Скоро здесь можно будет заказать персональное видео с вашим ребёнком.'
TEMPLATE=('[deep voice] Охо-хо-хо! [chuckles] [raspy] Здравствуй, [pause] {name}! Это я, Дедушка Мороз! '
    '[slowly] Я весь год наблюдал за тобой. [pause] И знаешь что? '
    '[excited] Ты меня очень порадовал! [raspy] Я слышал, что ты {fact1}. '
    '[pause] А ещё ты {fact2}. [happy] И особенно мне нравится, что ты {fact3}. '
    '[laughs] Молодец! [deep voice] Я тобой очень горжусь! [serious] В новом году у меня для тебя '
    'будет маленькое задание: {task}. [warm] Я знаю, у тебя всё получится! '
    '[pause] Я получил твоё письмо, {name}. [excited] И приготовил для тебя кое-что '
    'очень особенное! [deep voice] С Новым годом, {name}! [pause] [raspy] До встречи!')
LEGACY_DEFAULT_TEMPLATE=('[warm] Охо-хо-хо! [chuckles] Здравствуй, [pause] {name}! Это я, Дедушка Мороз! [slowly] Я весь год '
 'наблюдал за тобой. [pause] И знаешь что? [excited] Ты меня очень порадовал! [warm] Я слышал, что ты '
 '{fact1}. [pause] А ещё ты {fact2}. [happy] И особенно мне нравится, что ты {fact3}. [laughs] Молодец! Я '
 'тобой очень горжусь! [serious] В новом году у меня для тебя будет маленькое задание: {task}. [warm] Я '
 'знаю, у тебя всё получится! [pause] Я получил твоё письмо, {name}. [excited] И приготовил для тебя кое-что '
 'очень особенное! [happy] С Новым годом, {name}! [pause] До встречи!')
GREETING=(
    '🎅 Добро пожаловать в мастерскую Деда Мороза!\n\n'
    'Давайте создадим персональное видео-поздравление для вашего ребёнка.\n\n'
    'Как зовут ребёнка или как вы ласково его называете? (Например: «Маша» или «Машенька»)')
BLANK_TEMPLATE=(
    'Охо-хо-хо! Здравствуй, [ИМЯ]! Это я, Дедушка Мороз!\n'
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
    voice_id: str | None=DEFAULT_VOICE_ID
    avatar_id: str | None=None
    reference_url: str | None=None
    tts_model: str=DEFAULT_MODEL
    speech_speed: float=DEFAULT_SPEED
    echo_delay_ms: float=DEFAULT_ECHO_DELAY_MS
    echo_decay: float=DEFAULT_ECHO_DECAY

    @classmethod
    def from_config(cls,config):
        return cls(Path(os.getenv('STUDIO_FILES_DIR','').strip() or str(BASE_DIR/'studio_files')),BASE_DIR/'examples',
                   os.getenv('HEYGEN_API_KEY','').strip() or None,
                   os.getenv('DED_MOROZ_HEYGEN_VOICE_ID','').strip() or DEFAULT_VOICE_ID,
                   os.getenv('DED_MOROZ_AVATAR_ID','').strip() or None,
                   os.getenv('DED_MOROZ_REF_URL','').strip() or None,
                   os.getenv('DED_MOROZ_TTS_MODEL','').strip() or DEFAULT_MODEL,
                   float((os.getenv('DED_MOROZ_SPEECH_SPEED') or str(DEFAULT_SPEED)).replace(',','.')),
                   float((os.getenv('DED_MOROZ_ECHO_DELAY_MS') or str(DEFAULT_ECHO_DELAY_MS)).replace(',','.')),
                   float((os.getenv('DED_MOROZ_ECHO_DECAY') or str(DEFAULT_ECHO_DECAY)).replace(',','.')))

    def files(self):
        # Required audio decorations belong to the video phase, never to TTS.
        return [self.examples/'audio/jingle_bells.mp3',self.examples/'audio/ny_music.mp3']

    def ready(self,config,*,avatar_known=False,stage='audio'):
        if stage not in ('audio','video'):raise ValueError('Неизвестный этап подготовки.')
        if not config.storage_bucket or not config.storage_public_base_url:raise ValueError('Настройте Yandex Cloud STORAGE_BUCKET и STORAGE_PUBLIC_BASE_URL.')
        if stage=='audio':
            if not self.heygen_key:raise ValueError('Задайте HEYGEN_API_KEY.')
            HeyGenTTSAdapter.identifier(self.voice_id)
            HeyGenTTSAdapter.model_name(self.tts_model)
            HeyGenTTSAdapter.delivery_settings(self.speech_speed,self.echo_delay_ms,self.echo_decay)
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
    # Additive migration: existing previews and generated videos remain usable.
    cursor = await db.execute('PRAGMA table_info(ded_moroz_orders)')
    columns = {row[1] for row in await cursor.fetchall()}
    for name in ('speech_provider_url', 'approved_audio_url', 'approved_audio_path'):
        if name not in columns:
            await db.execute(f'ALTER TABLE ded_moroz_orders ADD COLUMN {name} TEXT')
    if 'regeneration_attempts' not in columns:
        await db.execute('ALTER TABLE ded_moroz_orders ADD COLUMN regeneration_attempts INTEGER NOT NULL DEFAULT 0')
        # Each previous version was an explicitly requested regeneration or text revision.
        await db.execute('UPDATE ded_moroz_orders SET regeneration_attempts=MAX(version,0)')
    await db.execute("""CREATE TABLE IF NOT EXISTS ded_moroz_audio_versions (
        order_id TEXT NOT NULL, version INTEGER NOT NULL, text TEXT NOT NULL,
        voice_id TEXT, provider_url TEXT NOT NULL, path TEXT, public_url TEXT,
        created_at TEXT DEFAULT CURRENT_TIMESTAMP, PRIMARY KEY(order_id, version))""")
    cursor=await db.execute('PRAGMA table_info(ded_moroz_audio_versions)')
    audio_columns={row[1] for row in await cursor.fetchall()}
    for name,definition in (('provider',"TEXT NOT NULL DEFAULT 'legacy'"),('submitted_text','TEXT'),
                            ('model','TEXT'),('request_sha256','TEXT')):
        if name not in audio_columns:
            await db.execute(f'ALTER TABLE ded_moroz_audio_versions ADD COLUMN {name} {definition}')
    await db.execute("""INSERT OR IGNORE INTO ded_moroz_audio_versions
        (order_id,version,text,voice_id,provider_url,path,public_url)
        SELECT id,version,text,NULL,COALESCE(speech_provider_url,audio_url),audio_path,audio_url
        FROM ded_moroz_orders WHERE audio_path IS NOT NULL AND audio_url IS NOT NULL
        AND text IS NOT NULL""")
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
             'raw_path','output_path','output_url','preview_sent','preview_mid','preview_version','notified','error','next_attempt','details','speech_provider_url','approved_audio_url','approved_audio_path'}
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


def can_regenerate(order, admin):
    # admin is the trusted config.admin_user_id passed by the router/worker, never callback data.
    return (admin is not None and order['user_id']==admin) or order.get('regeneration_attempts',0)<1


async def finish_regeneration(db, cursor, key, user_id):
    if cursor.rowcount!=1:
        await db.commit()
        return False
    row=await db.execute('SELECT regeneration_attempts FROM ded_moroz_orders WHERE id=?',(key,))
    attempts=(await row.fetchone())[0]
    await db.commit()
    logger.info(f"Перегенерация аудио для {user_id}. Попытка: {attempts}")
    return True


def keyboard(order, *, admin=None):
    b=InlineKeyboardBuilder().row(CallbackButton(text='✅ Всё верно, создать видео',payload='dm_audio_approve'))
    if can_regenerate(order,admin):
        b.row(CallbackButton(text='🔄 Перегенерировать озвучку',payload='dm_audio_regenerate'))
        if order['user_id']==admin:
            b.row(CallbackButton(text='✏️ Изменить текст',payload='dm_audio_edit'))
    return b.as_markup()


def default_text_for_retry(row):
    try:
        details=json.loads(row['details'])
        candidate=TEMPLATE.format(**details)
        if row['text'] in (candidate,LEGACY_DEFAULT_TEMPLATE.format(**details)):
            return candidate
    except (ValueError,KeyError,TypeError):
        pass
    return row['text']


async def regenerate(key, user, version, *, admin=None):
    """One accepted request per preview; admin unlimited, ordinary users one retry."""
    row=await get(key)
    if not row or row['user_id']!=user:return False
    text=default_text_for_retry(row)
    async with aiosqlite.connect(database.DATABASE_PATH,timeout=30) as db:
        cur=await db.execute("""UPDATE ded_moroz_orders SET text=?,version=version+1,regeneration_attempts=regeneration_attempts+1,
            status='audio_queued',audio_path=NULL,audio_url=NULL,speech_provider_url=NULL,
            approved_audio_url=NULL,approved_audio_path=NULL,
            preview_sent=0,preview_mid=NULL,preview_version=NULL,error=NULL,next_attempt=0
            WHERE id=? AND user_id=? AND version=? AND status='audio_ready'
            AND (? OR regeneration_attempts<1)""",(text,key,user,version,admin is not None and user==admin))
        return await finish_regeneration(db,cur,key,user)


async def approve(key, user, version):
    async with aiosqlite.connect(database.DATABASE_PATH,timeout=30) as db:
        cur=await db.execute("""UPDATE ded_moroz_orders SET status='video_queued',
            approved_audio_url=audio_url,approved_audio_path=audio_path
            WHERE id=? AND user_id=? AND version=? AND status='audio_ready'
            AND audio_url IS NOT NULL AND audio_path IS NOT NULL""",(key,user,version))
        await db.commit();return cur.rowcount==1


async def resume(key,user):
    row=await get(key)
    if not row or row['user_id']!=user or row['status']!='manual':
        raise ValueError('Нет приостановленного заказа этого администратора.')
    prior=(row['error'] or '').split(':',1)[0]
    if row['video_id']:
        target='polling'
    elif prior in ('audio_queued','audio_download','audio_publish','audio_ready','video_queued','download','render','publish','deliver'):
        target=prior
    else:
        raise ValueError('Результат платного запроса неизвестен. Сначала сверьте запрос у провайдера; повтор автоматически запрещён.')
    if not await transition(key,user,'manual',target):raise ValueError('Статус заказа уже изменился.')
    await update(key,error=None,next_attempt=0)
    return target


async def request_edit(key,user,version,*,admin=None):
    async with aiosqlite.connect(database.DATABASE_PATH,timeout=30) as db:
        cur=await db.execute("UPDATE ded_moroz_orders SET edit_used=1,status='editing' WHERE id=? AND user_id=? AND version=? AND status='audio_ready' AND (? OR (edit_used=0 AND regeneration_attempts<1))",(key,user,version,admin is not None and user==admin))
        await db.commit();return cur.rowcount==1


async def revised(key,user,text,*,admin=None):
    if not text or not text.strip() or len(text)>4500:raise ValueError('Введите текст от 1 до 4500 символов.')
    async with aiosqlite.connect(database.DATABASE_PATH,timeout=30) as db:
        cur=await db.execute("UPDATE ded_moroz_orders SET text=?,version=version+1,regeneration_attempts=regeneration_attempts+1,status='audio_queued',audio_path=NULL,audio_url=NULL,speech_provider_url=NULL,approved_audio_url=NULL,approved_audio_path=NULL,preview_sent=0,preview_mid=NULL,preview_version=NULL WHERE id=? AND user_id=? AND status='editing' AND edit_used=1 AND (? OR regeneration_attempts<1)",(text.strip(),key,user,admin is not None and user==admin))
        return await finish_regeneration(db,cur,key,user)


def manual_audio_retry_allowed(row):
    stage=(row['error'] or '').split(':',1)[0]
    return (row['status']=='manual' and stage in ('audio_queued','audio_submitting','audio_download','audio_publish','audio_ready')
            and bool(row['text'] and row['photo_path']) and not any(row[x] for x in ('video_id','raw_path','output_path','output_url')))


async def retry_manual_audio(key,user,version,*,admin,config=None,settings=None):
    if admin is None or user!=admin:return False
    row=await get(key)
    if not row or row['user_id']!=user or row['version']!=version or not manual_audio_retry_allowed(row):return False
    config=config or Config.from_env();settings=settings or Settings.from_config(config)
    settings.ready(config,stage='audio')
    if not Assets(settings.root,config).path(row['photo_path']).is_file():raise ValueError('Не найдено сохранённое фото ребёнка.')
    async with aiosqlite.connect(database.DATABASE_PATH,timeout=30) as db:
        cur=await db.execute("""UPDATE ded_moroz_orders SET text=?,status='audio_queued',version=version+1,
            regeneration_attempts=regeneration_attempts+1,audio_path=NULL,audio_url=NULL,speech_provider_url=NULL,
            approved_audio_path=NULL,approved_audio_url=NULL,preview_sent=0,preview_mid=NULL,preview_version=NULL,
            error=NULL,next_attempt=0 WHERE id=? AND user_id=? AND status='manual' AND version=? AND error=?
            AND video_id IS NULL AND raw_path IS NULL AND output_path IS NULL AND output_url IS NULL""",
            (default_text_for_retry(row),key,user,version,row['error']))
        return await finish_regeneration(db,cur,key,user)


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
        await context.set_state(PosterStates.ded_moroz_edit);await tell(event,'Отправьте исправленный текст целиком.')
    elif row['status']=='manual':
        b=InlineKeyboardBuilder()
        text=f"⚠️ Поздравление приостановлено. Заказ: {row['id']}. Текст, фото и готовые файлы сохранены."
        if manual_audio_retry_allowed(row):
            text+='\n\nМожно запустить новую платную озвучку голосом «Дед мороз» в HeyGen. Предыдущий неизвестный результат запроса при необходимости сверьте у провайдера. Нажмите кнопку, чтобы начать новую попытку с сохранённым текстом и фото.'
            b.row(CallbackButton(text='🎙 Перегенерировать озвучку',payload=f"dm_manual_regenerate:{row['id']}:{row['version']}"))
        else:
            text+='\nДля продолжения сохранённого этапа используйте /dm_resume '+row['id']
        await tell(event,text,attachments=[b.as_markup()] if manual_audio_retry_allowed(row) else [])
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
        if await revised(row['id'],admin,text,admin=admin):
            await context.clear();await tell(event,'Создаю новую озвучку. После прослушивания можно снова изменить текст или перегенерировать аудио.')
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


async def callback(event,context,admin,config=None,settings=None):
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
    if action=='dm_manual_regenerate':
        try:accepted=await retry_manual_audio(key,admin,version,admin=admin,config=config,settings=settings)
        except ValueError as exc:
            await tell(event,str(exc));return
        if accepted:
            await context.clear();await tell(event,AUDIO_GENERATING)
        else:await tell(event,'Состояние заказа уже изменилось. Откройте раздел заново.')
    elif action=='dm_audio_edit' and await request_edit(key,admin,version,admin=admin):
        await context.clear();await context.update_data(dm_order=key);await context.set_state(PosterStates.ded_moroz_edit)
        await tell(event,'Отправьте новый текст целиком. Перегенерация озвучки платная; для администратора число попыток не ограничено.')
    elif action=='dm_audio_regenerate' and await regenerate(key,admin,version,admin=admin):
        await context.clear();await tell(event,AUDIO_GENERATING)
    elif action in ('dm_audio_ok','dm_audio_approve') and await approve(key,admin,version):
        await context.clear();await tell(event,'Утверждённая озвучка сохранена. Запускаю HeyGen Avatar IV и монтаж.')
    else:await tell(event,'Эта кнопка уже использована или относится к предыдущей версии озвучки.')


class Worker:
    def __init__(self,bot,admin,config,settings=None,tts=None,heygen=None):
        self.bot,self.admin,self.config=bot,admin,config
        self.settings=settings or Settings.from_config(config)
        self.assets=Assets(self.settings.root,config)
        self.heygen=heygen or HeyGenAdapter(self.settings.heygen_key)
        self.tts=tts or HeyGenTTSAdapter(self.settings.heygen_key,self.settings.voice_id,self.assets,model=self.settings.tts_model,
            speed=self.settings.speech_speed,echo_delay_ms=self.settings.echo_delay_ms,echo_decay=self.settings.echo_decay)
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

    async def store_audio_result(self,row,path):
        key=row['id'];text=prepare_speech_text(row['text'])
        signature=HeyGenTTSAdapter(None,self.settings.voice_id,self.assets,model=self.settings.tts_model,
            speed=self.settings.speech_speed,echo_delay_ms=self.settings.echo_delay_ms,echo_decay=self.settings.echo_decay).fingerprint(text)
        async with aiosqlite.connect(database.DATABASE_PATH,timeout=30) as db:
            await db.execute("""INSERT INTO ded_moroz_audio_versions
                (order_id,version,text,voice_id,provider_url,path,provider,submitted_text,model,request_sha256)
                VALUES (?,?,?,?,?,?,?,?,?,?)""",
                (key,row['version'],row['text'],self.settings.voice_id,'',path,'heygen',text,self.settings.tts_model,signature))
            await db.execute("UPDATE ded_moroz_orders SET speech_provider_url=NULL,audio_path=?,status='audio_publish',error=NULL,next_attempt=0 WHERE id=?",(path,key))
            await db.commit()

    async def process(self,row):
        key=row['id'];state=row['status']
        if row['user_id']!=self.admin:return
        if state=='audio_queued':
            self.settings.ready(self.config,avatar_known=bool(await read('SELECT look_id FROM ded_moroz_avatar WHERE look_id IS NOT NULL')))
            if not row['photo_path'] or not self.assets.path(row['photo_path']).is_file():raise ValueError('Не найдено сохранённое фото ребёнка.')
            text=prepare_speech_text(row['text'])
            if not text or len(text)>10000:raise ValueError('После очистки тегов текст озвучки должен содержать от 1 до 10000 символов.')
            if not await transition(key,self.admin,'audio_queued','audio_submitting'):return
            path=await self.tts.generate_speech(text,job_id=key,version=row['version'])
            if not self.assets.path(path).is_file():raise ValueError('HeyGen не сохранил MP3 этой версии.')
            # Persisted MP3 and manifest can be reused after interruption; no second POST.
            await self.store_audio_result(row,path)
        elif state=='audio_download':
            path,_=await self.assets.download(row['speech_provider_url'],'mp3',job_id=key,
                name=f"speech-v{row['version']}.mp3",limit=30*1024*1024)
            async with aiosqlite.connect(database.DATABASE_PATH,timeout=30) as db:
                await db.execute('UPDATE ded_moroz_audio_versions SET path=? WHERE order_id=? AND version=?',(path,key,row['version']))
                await db.execute("UPDATE ded_moroz_orders SET audio_path=?,status='audio_publish' WHERE id=?",(path,key))
                await db.commit()
        elif state=='audio_publish':
            url=await self.assets.publish(row['audio_path'])
            async with aiosqlite.connect(database.DATABASE_PATH,timeout=30) as db:
                await db.execute("UPDATE ded_moroz_audio_versions SET public_url=?,provider_url=CASE WHEN provider IN ('elevenlabs','fish_audio','heygen') THEN ? ELSE provider_url END WHERE order_id=? AND version=?",(url,url,key,row['version']))
                await db.execute("UPDATE ded_moroz_orders SET audio_url=?,status='audio_ready',preview_sent=0 WHERE id=?",(url,key))
                await db.commit()
        elif state=='audio_ready' and not row['preview_sent']:
            token_key=('audio',key,row['version'])
            audio=self.uploads.get(token_key)
            if audio is None:
                audio=await self.bot.upload_media(InputMedia(path=str(self.assets.path(row['audio_path'])),type=UploadType.AUDIO))
                self.uploads[token_key]=audio
            preview_text=display_text(prepare_speech_text(row['text']))
            chunks=[preview_text[i:i+3500] for i in range(0,len(preview_text),3500)]
            for chunk in chunks:
                if await self.bot.send_message(user_id=self.admin,text=chunk) is None:raise ValueError('MAX не подтвердил текст.')
            sent=await self.bot.send_message(user_id=self.admin,text='🎧 Прослушайте озвучку Деда Мороза. Всё нравится?',attachments=[audio,keyboard(row,admin=self.admin)])
            if sent is None:raise ValueError('MAX не подтвердил аудио.')
            message=getattr(sent,'message',sent)  # maxapi returns SendedMessage(message=Message(...)).
            mid=getattr(getattr(message,'body',None),'mid',None)
            if not isinstance(mid,str) or not mid:raise RuntimeError('MAX не вернул ID сообщения с озвучкой.')
            await update(key,preview_sent=1,preview_mid=mid,preview_version=row['version'])
        elif state=='video_queued':
            self.settings.ready(self.config,avatar_known=bool(await read('SELECT look_id FROM ded_moroz_avatar WHERE look_id IS NOT NULL')),stage='video')
            sources={'photo_path':row['photo_path'],
                     'audio_path':row['approved_audio_path'] or row['audio_path']}
            for field_name,path in sources.items():
                if not path or not self.assets.path(path).is_file():raise ValueError('Не найден сохранённый исходник: '+field_name)
            approved_url=row['approved_audio_url'] or row['audio_url']  # Already-approved legacy orders.
            https_url(approved_url)
            avatar=await self.avatar()
            if not avatar:return
            if not await transition(key,self.admin,'video_queued','video_submitting'):return
            video=await self.heygen.create_video(avatar,approved_url,'dm-video-'+key)
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
                self.assets.path(row['approved_audio_path'] or row['audio_path']),self.settings.examples,target,json.loads(row['details'])['name'])
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
                prefix='audio_queued' if isinstance(exc,HeyGenSpeechError) and exc.safe_to_retry and current['status']=='audio_submitting' else current['status']
                reason=exc.code if isinstance(exc,HeyGenSpeechError) else type(exc).__name__
                await update(row['id'],status='manual' if terminal else current['status'],error=prefix+':'+reason,next_attempt=now+60)
                if terminal:
                    try:await self.bot.send_message(user_id=self.admin,text=f"⚠️ Заказ Деда Мороза {row['id']} требует проверки. Состояние и файлы сохранены; платный запрос автоматически не повторяется."+(f" Причина: {exc}" if isinstance(exc,HeyGenSpeechError) else ""))
                    except Exception:logger.exception('Дед Мороз: не удалось отправить предупреждение')

    async def recover_interrupted(self):
        # Reuse a verified completed HeyGen MP3 if the process stopped before SQLite commit.
        if isinstance(self.tts,HeyGenTTSAdapter):
            rows=await read("SELECT * FROM ded_moroz_orders WHERE user_id=? AND status='audio_submitting'",(self.admin,),all_rows=True)
            for row in rows:
                try:
                    path=await self.tts.cached_speech(row['text'],job_id=row['id'],version=row['version'])
                    if path:
                        await self.store_audio_result(row,path)
                    elif await self.tts.read_source(row['text'],job_id=row['id'],version=row['version']):
                        await update(row['id'],status='audio_queued',error=None,next_attempt=0)
                except Exception:
                    logger.exception('Дед Мороз: проверка сохранённой озвучки заказа %s',row['id'])
        # An interrupted paid POST without a verified file is never sent again after restart.
        async with aiosqlite.connect(database.DATABASE_PATH,timeout=30) as db:
            await db.execute("UPDATE ded_moroz_orders SET error=status||':interrupted_paid_submission',status='manual' WHERE status IN ('audio_submitting','video_submitting')");await db.commit()

    async def run(self):
        await self.recover_interrupted()
        while True:
            try:await self.tick()
            except asyncio.CancelledError:raise
            except Exception:logger.exception('Ошибка очереди Деда Мороза')
            await asyncio.sleep(3)

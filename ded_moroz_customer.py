"""Result actions, customer reviews and an optional compact iPhone-friendly MP4."""
import asyncio
import logging
from uuid import uuid4

import aiosqlite
from maxapi.types import CallbackButton,LinkButton,InputMedia
from maxapi.enums import UploadType
from maxapi.utils.inline_keyboard import InlineKeyboardBuilder

import database
import ded_moroz_sales as sales
from media_service import audio_duration,run_media_process

logger=logging.getLogger(__name__)


def result_keyboard(row,bot_link):
    b=InlineKeyboardBuilder()
    from ded_moroz_family import is_family
    b.row(CallbackButton(text='🎅 Сделать ещё',payload='cat_ded_moroz_family' if is_family(row['details']) else 'dm_another'))
    b.row(CallbackButton(text='📱 Версия для телефона',payload='dm_mobile:'+row['id']))
    if row.get('output_url'):b.row(LinkButton(text='⬇️ Скачать видео',url=row['output_url']))
    b.row(CallbackButton(text='🔗 Поделиться',payload='dm_share:'+row['id']))
    b.row(CallbackButton(text='✍️ Написать отзыв',payload='dm_review:'+row['id']))
    return b.as_markup()


async def compact_video(source,target,*,preview_seconds=None):
    if not source.is_file():raise ValueError('Исходное видео отсутствует.')
    seconds=await audio_duration(source)
    if seconds>90 and preview_seconds is None:raise ValueError('Слишком длинное видео для компактной версии.')
    target.parent.mkdir(parents=True,exist_ok=True)
    temporary=target.with_name('.mobile-'+uuid4().hex+'.mp4')
    options=['-t',str(preview_seconds)] if preview_seconds is not None else []
    try:
        await run_media_process('ffmpeg','-nostdin','-v','error','-y','-i',str(source),*options,
            '-map','0:v:0','-map','0:a:0','-vf','scale=1280:720:force_original_aspect_ratio=decrease,pad=1280:720:(ow-iw)/2:(oh-ih)/2,setsar=1',
            '-r','30','-c:v','libx264','-profile:v','main','-level:v','3.1','-tag:v','avc1',
            '-preset','fast','-crf','23','-maxrate','2M','-bufsize','4M','-pix_fmt','yuv420p',
            '-c:a','aac','-profile:a','aac_low','-b:a','128k','-ac','2','-ar','48000',
            '-movflags','+faststart',str(temporary),timeout=1200)
        if temporary.stat().st_size>25*1024*1024:raise ValueError('Компактный файл превышает 25 МБ.')
        temporary.replace(target)
    finally:temporary.unlink(missing_ok=True)
    return target


async def last_result(event,admin,config):
    from ded_moroz import read,tell
    from multstudio.assets import Assets
    from ded_moroz import Settings
    user=event.get_ids()[1]
    row=await read("SELECT * FROM ded_moroz_orders WHERE user_id=? AND status='done' ORDER BY rowid DESC LIMIT 1",(user,))
    if not row or not await sales.allowed(row,admin):
        await tell(event,'У вас пока нет готового поздравления.');return
    assets=Assets(Settings.from_config(config).root,config)
    video=await event.bot.upload_media(InputMedia(path=str(assets.path(row['output_path'])),type=UploadType.VIDEO))
    if getattr(video,'type',None)!='video':raise RuntimeError('MAX не вернул видео.')
    await tell(event,'🎅 Ваше готовое поздравление. Можно скачать оригинал или получить компактную версию для телефона.',attachments=[video,result_keyboard(row,config.bot_link)])


async def action(event,context,admin,config):
    from ded_moroz import get,tell
    from handlers import PosterStates
    from urllib.parse import urlencode
    action,key=event.callback.payload.split(':',1)
    row=await get(key);user=event.get_ids()[1]
    if not row or row['user_id']!=user or row['status']!='done' or not await sales.allowed(row,admin):return
    if action=='dm_share':
        await database.increment_counter('dm_share_click',user_id=user)
        b=InlineKeyboardBuilder().row(LinkButton(text='🎅 Пригласить друзей',url=config.bot_link+'?'+urlencode({'start':'ded_moroz'})))
        await tell(event,'Чтобы поделиться самим поздравлением, нажмите «Переслать» у видео. А по этой ссылке друзья смогут заказать своё:',attachments=[b.as_markup()])
    elif action=='dm_review':
        await context.clear();await context.update_data(dm_review_order=key);await context.set_state(PosterStates.ded_moroz_review)
        await tell(event,'✍️ Напишите отзыв о поздравлении одним сообщением. Я передам его создателю бота. До 1500 символов.')
    elif action=='dm_mobile':
        async with aiosqlite.connect(database.DATABASE_PATH,timeout=30) as db:
            await db.execute("""INSERT INTO ded_moroz_mobile_jobs(order_id,source_path) VALUES (?,?)
                ON CONFLICT(order_id) DO UPDATE SET
                status=CASE WHEN source_path=excluded.source_path AND status!='error' THEN status ELSE 'queued' END,
                path=CASE WHEN source_path=excluded.source_path THEN path ELSE NULL END,
                url=CASE WHEN source_path=excluded.source_path THEN url ELSE NULL END,
                source_path=excluded.source_path,notified=0,next_attempt=0,error=NULL""",(key,row['output_path']))
            await db.commit()
        await database.increment_counter('dm_mobile_click',user_id=user)
        await tell(event,'📱 Подготовлю более компактное видео для телефона и пришлю сюда. Повторная оплата не нужна.')


async def review(event,context,admin):
    from ded_moroz import get,tell
    key=(await context.get_data()).get('dm_review_order');row=await get(key);user=event.get_ids()[1]
    if not row or row['user_id']!=user or row['status']!='done' or not await sales.allowed(row,admin):return
    text=(event.message.body.text or '').strip()
    if not text or len(text)>1500:
        await tell(event,'Напишите отзыв текстом, от 1 до 1500 символов.');return
    async with aiosqlite.connect(database.DATABASE_PATH,timeout=30) as db:
        cur=await db.execute('INSERT OR IGNORE INTO ded_moroz_reviews(order_id,user_id,text) VALUES (?,?,?)',(key,user,text))
        if cur.rowcount:await database.increment_counter('dm_review_received',user_id=user,connection=db)
        await db.commit()
    await context.clear();await tell(event,'Спасибо за отзыв! Передам его создателю бота ❤️')


async def process_auxiliary(worker):
    from ded_moroz import read,get
    now=database._now_ts()
    for item in await read('SELECT * FROM ded_moroz_reviews WHERE delivered=0 AND next_attempt<=? LIMIT 5',(now,),all_rows=True):
        try:
            sent=await worker.bot.send_message(user_id=worker.admin,text=f"✍️ Отзыв о поздравлении Деда Мороза\nПользователь: {item['user_id']}\nЗаказ: {item['order_id']}\n\n{item['text']}")
            if sent is None:raise RuntimeError('MAX не подтвердил отзыв.')
            async with aiosqlite.connect(database.DATABASE_PATH,timeout=30) as db:
                await db.execute('UPDATE ded_moroz_reviews SET delivered=1 WHERE order_id=?',(item['order_id'],));await db.commit()
        except Exception:
            logger.exception('Не удалось передать отзыв Деда Мороза')
            async with aiosqlite.connect(database.DATABASE_PATH,timeout=30) as db:
                await db.execute('UPDATE ded_moroz_reviews SET next_attempt=? WHERE order_id=?',(now+60,item['order_id']));await db.commit()
    jobs=await read("SELECT * FROM ded_moroz_mobile_jobs WHERE notified=0 AND status IN ('queued','publish','done') AND next_attempt<=? LIMIT 1",(now,),all_rows=True)
    for item in jobs:
        row=await get(item['order_id'])
        if not row or row['status']!='done' or not await sales.allowed(row,worker.admin):continue
        try:
            if item['source_path']!=row['output_path']:
                async with aiosqlite.connect(database.DATABASE_PATH,timeout=30) as db:
                    await db.execute("UPDATE ded_moroz_mobile_jobs SET status='queued',source_path=?,path=NULL,url=NULL WHERE order_id=?",(row['output_path'],row['id']));await db.commit()
                continue
            if item['status']=='queued':
                target=worker.assets.path(row['id']+'/mobile-'+uuid4().hex+'.mp4')
                await compact_video(worker.assets.path(item['source_path']),target)
                item['path']=target.relative_to(worker.assets.root).as_posix();item['status']='publish'
                async with aiosqlite.connect(database.DATABASE_PATH,timeout=30) as db:
                    await db.execute("UPDATE ded_moroz_mobile_jobs SET path=?,status='publish' WHERE order_id=?",(item['path'],row['id']));await db.commit()
            if item['status']=='publish':
                item['url']=await worker.assets.publish(item['path'])
                async with aiosqlite.connect(database.DATABASE_PATH,timeout=30) as db:
                    await db.execute("UPDATE ded_moroz_mobile_jobs SET url=?,status='done' WHERE order_id=?",(item['url'],row['id']));await db.commit()
            token_key=('mobile',row['id'],item['path'])
            video=worker.uploads.get(token_key)
            if video is None:
                video=await worker.bot.upload_media(InputMedia(path=str(worker.assets.path(item['path'])),type=UploadType.VIDEO))
                if getattr(video,'type',None)!='video':raise RuntimeError('MAX вернул другой тип вложения.')
                worker.uploads[token_key]=video
            b=InlineKeyboardBuilder().row(LinkButton(text='⬇️ Скачать компактное видео',url=item['url']))
            sent=await worker.bot.send_message(user_id=row['user_id'],text='📱 Компактная версия готова! На iPhone откройте видео → «Поделиться» → «Сохранить видео». Если сначала сохранили в «Файлы», откройте MP4 там и повторите эти действия.',attachments=[video,b.as_markup()])
            if sent is None:raise RuntimeError('MAX не подтвердил компактное видео.')
            async with aiosqlite.connect(database.DATABASE_PATH,timeout=30) as db:
                await db.execute('UPDATE ded_moroz_mobile_jobs SET notified=1 WHERE order_id=?',(row['id'],));await db.commit()
            worker.uploads.pop(token_key,None)
        except Exception as exc:
            logger.exception('Не удалось подготовить/отправить компактное поздравление %s',row['id'])
            async with aiosqlite.connect(database.DATABASE_PATH,timeout=30) as db:
                await db.execute("UPDATE ded_moroz_mobile_jobs SET next_attempt=?,error=?,status=CASE WHEN ? THEN 'error' ELSE status END WHERE order_id=?",(now+60,type(exc).__name__,isinstance(exc,ValueError),row['id']));await db.commit()

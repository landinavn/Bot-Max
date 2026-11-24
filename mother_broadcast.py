"""Mother's Day reminder: shared example, admin preview, resumable explicit broadcast."""
import asyncio
import logging
import time
from pathlib import Path

import aiosqlite
from maxapi.types import CallbackButton, InputMedia
from maxapi.enums.upload_type import UploadType
from maxapi.utils.inline_keyboard import InlineKeyboardBuilder

import database

EXAMPLE=Path(__file__).resolve().parent/'examples'/'den_mama.mp4'
CAMPAIGN='mother_day_reminder_20261129'
TEXT=('🔔 Напоминание: День матери в этом году отмечают 29 ноября. Самое время задуматься о подарке!\n\n'
    'В этот день хочется сказать маме самые важные слова — выразить ей благодарность и любовь. '
    'Сделать это за вас может мультфильм с ее участием и поздравительной песней!\n\n'
    'Нажимай старт и попробуй уже сейчас!')
logger=logging.getLogger(__name__)
_example_cache={}
SCHEMA='''CREATE TABLE IF NOT EXISTS mother_broadcast_recipients (
    campaign TEXT NOT NULL,user_id INTEGER NOT NULL,next_message INTEGER NOT NULL DEFAULT 0,
    completed INTEGER NOT NULL DEFAULT 0,PRIMARY KEY(campaign,user_id))'''


async def example_attachment(bot):
    if not EXAMPLE.is_file() or not 0<EXAMPLE.stat().st_size<=100*1024*1024:
        raise ValueError('Добавьте examples/den_mama.mp4 — пример мульт-открытки ко Дню матери.')
    key=(id(bot),str(EXAMPLE),EXAMPLE.stat().st_mtime_ns,EXAMPLE.stat().st_size)
    cached=_example_cache.get(key)
    if cached and cached[0]>time.monotonic():return cached[1]
    attachment=await bot.upload_media(InputMedia(path=str(EXAMPLE),type=UploadType.VIDEO))
    if getattr(attachment,'type',None)!='video':
        raise ValueError('MAX не подтвердил загрузку видео-примера.')
    for old in list(_example_cache):
        if _example_cache[old][0]<=time.monotonic():_example_cache.pop(old,None)
    _example_cache[key]=(time.monotonic()+300,attachment)
    return attachment


async def messages(bot):
    video=await example_attachment(bot)
    keyboard=InlineKeyboardBuilder().row(CallbackButton(text='🚀 Старт',payload='mother_card_start')).row(
        CallbackButton(text='🏠 Главное меню',payload='back_to_main')).as_markup()
    return [('🎬 Пример мульт-открытки ко Дню матери:',[video]),(TEXT,[keyboard])]


async def preview(event,*,confirm=False):
    from handlers import _reply
    content=await messages(event.bot)
    for text,attachments in content:
        sent=await _reply(event,text,attachments=attachments)
        if sent is None:raise RuntimeError('MAX не подтвердил предпросмотр.')
        await asyncio.sleep(.5)
    if confirm:
        await _reply(event,'Это предпросмотр. Рассылка начнётся после подтверждения. Повторный запуск продолжит с сохранённого места.',
            attachments=[InlineKeyboardBuilder().row(CallbackButton(text='📣 Подтвердить рассылку',payload='mother_broadcast_confirm')).as_markup()])


async def progress(user,index,total):
    async with aiosqlite.connect(database.DATABASE_PATH,timeout=30) as db:
        await db.execute('BEGIN IMMEDIATE')
        row=await (await db.execute('SELECT completed FROM mother_broadcast_recipients WHERE campaign=? AND user_id=?',(CAMPAIGN,user))).fetchone()
        completed=int(index==total)
        await db.execute('''INSERT INTO mother_broadcast_recipients(campaign,user_id,next_message,completed) VALUES(?,?,?,?)
            ON CONFLICT(campaign,user_id) DO UPDATE SET next_message=MAX(next_message,excluded.next_message),
            completed=MAX(completed,excluded.completed)''',(CAMPAIGN,user,index,completed))
        if completed and not (row and row[0]):
            await database.increment_counter('mother_broadcast_delivered',user_id=user,connection=db)
        await db.commit()


async def send(event,lock):
    from handlers import _reply,max_error_status
    if lock.locked():
        await _reply(event,'Рассылка уже выполняется.');return
    async with lock:
        users=await database.get_active_user_ids()
        if not users:
            await _reply(event,'✅ Рассылка завершена. Успешно: 0, Ошибок: 0.');return
        # Upload/validate the single shared example before contacting any subscribers.
        content=await messages(event.bot)
        async with aiosqlite.connect(database.DATABASE_PATH,timeout=30) as db:
            await db.execute(SCHEMA);await db.commit()
        success=failed=0;stopped=False
        for user in users:
            current=await database.get_user(user)
            if not current or not current['is_active']:continue
            async with aiosqlite.connect(database.DATABASE_PATH,timeout=30) as db:
                row=await (await db.execute('SELECT next_message,completed FROM mother_broadcast_recipients WHERE campaign=? AND user_id=?',(CAMPAIGN,user))).fetchone()
            if row and row[1]:continue
            try:
                for index,(text,attachments) in enumerate(content):
                    if row and index<row[0]:continue
                    sent=await event.bot.send_message(user_id=user,text=text,attachments=attachments)
                    if sent is None:raise RuntimeError('MAX не подтвердил отправку.')
                    await progress(user,index+1,len(content))
                    await asyncio.sleep(.5)
                success+=1
            except Exception as exc:
                failed+=1
                logger.warning('Рассылка ко Дню матери: user=%s error_type=%s',user,type(exc).__name__)
                await database.increment_counter('mother_broadcast_errors',user_id=user)
                status=max_error_status(exc)
                if status==403:await database.mark_user_inactive(user)
                if status in (401,429):stopped=True;break
                await asyncio.sleep(.5)
        await _reply(event,('⏸ Рассылка остановлена.' if stopped else '✅ Рассылка завершена.')+f' Успешно: {success}, Ошибок: {failed}.')

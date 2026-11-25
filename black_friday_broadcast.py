"""Explicit admin broadcast with durable delivery status and campaign expiry."""
import asyncio
import logging

import aiosqlite
from maxapi.types import CallbackButton
from maxapi.utils.inline_keyboard import InlineKeyboardBuilder

import database
import black_friday as friday

logger = logging.getLogger(__name__)
TEXT = ('🖤 Чёрная пятница уже началась — скидка 20% на всё!\n\n'
        'Самое время подготовить подарки близким или порадовать себя ✨\n\n'
        '💌 Открытки с вашим фото\n'
        '🎵 Персональные песни и музыкальные открытки\n'
        '🎬 Мультфильмы с вами в главной роли\n'
        '🎅 Поздравления от Деда Мороза для одного или нескольких детей\n'
        '📸 Нейрофотосессии, оживление фотографий и карты желаний\n\n'
        'А уже скоро 29 ноября — День матери 💐 Подарите маме мультфильм с её участием '
        'и песней, которая скажет о вашей любви!\n\n'
        '⏰ Акция действует с 25 ноября до 29 ноября, 23:59 по московскому времени.\n\n'
        '👇 Выбирайте подарок!')
SCHEMA = '''CREATE TABLE IF NOT EXISTS black_friday_recipients (
    campaign TEXT NOT NULL,user_id INTEGER NOT NULL,status TEXT NOT NULL DEFAULT 'pending',
    PRIMARY KEY(campaign,user_id))'''


def keyboard():
    return InlineKeyboardBuilder().row(CallbackButton(text='🎁 Выбрать подарок',payload='black_friday_menu')).as_markup()


async def preview(event, *, confirm=False):
    from handlers import _reply
    await _reply(event,TEXT,attachments=[keyboard()])
    if confirm and friday.active():
        await _reply(event,'Это предпросмотр. Подписчикам пока ничего не отправлено. '
                     'Нажмите кнопку, чтобы начать рассылку. Повторный запуск пропустит получивших сообщение.',
                     attachments=[InlineKeyboardBuilder().row(CallbackButton(
                         text='📣 Подтвердить рассылку',payload='black_friday_broadcast_confirm')).as_markup()])
    elif confirm:
        await _reply(event,'Рассылка доступна только с 25 по 29 ноября 2026 года, до 23:59 МСК.')


async def report(event):
    from handlers import _reply
    async with aiosqlite.connect(database.DATABASE_PATH,timeout=30) as db:
        await db.execute(SCHEMA)
        async with db.execute('SELECT status,COUNT(*) FROM black_friday_recipients WHERE campaign=? GROUP BY status',
                              (friday.CAMPAIGN,)) as cursor:
            counts=dict(await cursor.fetchall())
        await db.commit()
    await _reply(event,f"🖤 Чёрная пятница: доставлено всего — {counts.get('sent',0)}; "
                 f"требуют проверки — {counts.get('sending',0)+counts.get('uncertain',0)}; "
                 f"недоступны — {counts.get('inactive',0)}.")


async def set_status(user,status):
    async with aiosqlite.connect(database.DATABASE_PATH,timeout=30) as db:
        await db.execute('UPDATE black_friday_recipients SET status=? WHERE campaign=? AND user_id=?',
                         (status,friday.CAMPAIGN,user))
        await db.commit()


async def send(event,lock):
    from handlers import _reply,max_error_status
    if not friday.active():
        await _reply(event,'Акция не действует. Рассылка не запущена.');return
    if lock.locked():
        await _reply(event,'Рассылка уже выполняется.');return
    async with lock:
        success=failed=0;stopped=False
        users=await database.get_active_user_ids()
        async with aiosqlite.connect(database.DATABASE_PATH,timeout=30) as db:
            await db.execute(SCHEMA);await db.commit()
        for user in users:
            if not friday.active():stopped=True;break
            current=await database.get_user(user)
            if not current or not current['is_active']:continue
            async with aiosqlite.connect(database.DATABASE_PATH,timeout=30) as db:
                await db.execute('BEGIN IMMEDIATE')
                await db.execute("INSERT INTO black_friday_recipients(campaign,user_id) VALUES (?,?) ON CONFLICT DO NOTHING",(friday.CAMPAIGN,user))
                cursor=await db.execute("UPDATE black_friday_recipients SET status='sending' WHERE campaign=? AND user_id=? AND status='pending'",(friday.CAMPAIGN,user))
                claimed=cursor.rowcount;await db.commit()
            if not claimed:continue
            try:
                result=await event.bot.send_message(user_id=user,text=TEXT,attachments=[keyboard()])
                if result is None:
                    await set_status(user,'uncertain');failed+=1
                else:
                    await set_status(user,'sent');success+=1
            except asyncio.CancelledError:
                # Do not blindly resend an API call with an unknown outcome after restart.
                raise
            except Exception as exc:
                failed+=1;status=max_error_status(exc)
                logger.warning('Чёрная пятница: user=%s status=%s error_type=%s',user,status,type(exc).__name__)
                await set_status(user,'inactive' if status==403 else 'pending' if status in (401,429) else 'uncertain')
                if status==403:await database.mark_user_inactive(user)
                if status in (401,429):stopped=True;break
            await asyncio.sleep(.5)
        await _reply(event,('⏸ Рассылка остановлена.' if stopped else '✅ Рассылка завершена.')+
                     f' В этом запуске доставлено: {success}, ошибок: {failed}.')
        await report(event)

"""Admin-reviewed launch campaign, durable recipients, no automatic broadcast."""
from safe_logging import get_logger
import asyncio
import logging
from datetime import datetime
from zoneinfo import ZoneInfo

import aiosqlite
from maxapi.types import CallbackButton
from maxapi.utils.inline_keyboard import InlineKeyboardBuilder

import database
import ded_moroz_sales as sales

CAMPAIGN='ded_moroz_family_launch_20261119'
logger=get_logger(__name__)
def campaign_open():
    today=datetime.fromtimestamp(database._now_ts(),ZoneInfo('Europe/Moscow')).date()
    return sales.enabled() and datetime(2026,11,19).date()<=today<=datetime(2026,11,22).date()


def announcement():
    return ('🎅 Новинка: одно поздравление Деда Мороза для нескольких детей!\n\n'
        '👧👦 Для 2–6 детей: Дед Мороз назовёт каждого по имени, расскажет его личный факт и пожелает что-то особенное.\n'
        '📸 Общее фото появится в новогодней рамке. Если фото нет — можно без него.\n'
        '🎧 Перед видео вы прослушаете озвучку и сможете один раз исправить данные и выбрать вариант.\n\n'
        +sales.family_price_text()+'\n\n🎁 Начните с выбора количества детей. Сначала оплата, затем данные и создание поздравления.')



async def messages(bot):
    from ded_moroz_family import example_attachment
    video=await example_attachment(bot)
    b=InlineKeyboardBuilder().row(
        CallbackButton(text='🎅 Старт',payload='dm_family_launch'),
        CallbackButton(text='🏠 Главный экран',payload='back_to_main'),
    )
    return [('🎬 Пример семейного поздравления Деда Мороза:',[video]),(announcement(),[b.as_markup()])]


async def preview(event,*,confirm=False):
    from ded_moroz import tell
    for text,attachments in await messages(event.bot):
        await tell(event,text,attachments=attachments);await asyncio.sleep(.5)
    if confirm:
        b=InlineKeyboardBuilder().row(CallbackButton(text='📣 Подтвердить рассылку',payload='dm_family_broadcast_confirm'))
        await tell(event,'Это предпросмотр. Отправка подписчикам начнётся только по кнопке подтверждения, до 22 ноября 23:59 МСК. Повторный запуск продолжит с сохранённого места.',attachments=[b.as_markup()])


async def send(event,lock):
    from ded_moroz import read,tell
    from handlers import max_error_status
    if not campaign_open():
        await tell(event,'Рассылка семейной акции доступна до 22 ноября 2026 года, 23:59 МСК. Отправка не запущена.');return
    if lock.locked():
        await tell(event,'Рассылка уже выполняется.');return
    async with lock:
        content=await messages(event.bot)
        success=failed=0;stopped=False
        for user in await database.get_active_user_ids():
            if not campaign_open():stopped=True;break
            row=await read('SELECT * FROM ded_moroz_broadcast WHERE campaign=? AND user_id=?',(CAMPAIGN,user))
            if row and row['completed']:continue
            current=await database.get_user(user)
            if not current or not current['is_active']:continue
            try:
                for index,(text,attachments) in enumerate(content):
                    if row and index<row['next_message']:continue
                    if not campaign_open():stopped=True;break
                    result=await event.bot.send_message(user_id=user,text=text,attachments=attachments)
                    if result is None:raise RuntimeError('MAX не подтвердил рассылку.')
                    async with aiosqlite.connect(database.DATABASE_PATH,timeout=30) as db:
                        await db.execute('''INSERT INTO ded_moroz_broadcast(campaign,user_id,next_message,completed) VALUES (?,?,?,?)
                            ON CONFLICT(campaign,user_id) DO UPDATE SET next_message=MAX(next_message,excluded.next_message),completed=MAX(completed,excluded.completed)''',
                            (CAMPAIGN,user,index+1,int(index==len(content)-1)))
                        await db.commit()
                    await asyncio.sleep(.5)
                if stopped:break
                success+=1
            except Exception as exc:
                logger.error('Рассылка Деда Мороза: пользователь %s, ошибка %s',user,type(exc).__name__)
                failed+=1
                status=max_error_status(exc)
                if status==403:await database.mark_user_inactive(user)
                if status in (401,429):stopped=True;break
                await asyncio.sleep(.5)
        await tell(event,('⏸ Рассылка остановлена.' if stopped else '✅ Рассылка завершена.')+f' Успешно: {success}, Ошибок: {failed}.')

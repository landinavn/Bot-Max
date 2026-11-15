"""Admin-reviewed launch campaign, durable recipients, no automatic broadcast."""
import asyncio
import logging
from datetime import datetime
from zoneinfo import ZoneInfo

import aiosqlite
from maxapi.types import CallbackButton,InputMedia
from maxapi.enums import UploadType
from maxapi.utils.inline_keyboard import InlineKeyboardBuilder

import database
import ded_moroz_sales as sales

CAMPAIGN='ded_moroz_launch_20261116'
logger=logging.getLogger(__name__)
TEXT=('🎅 Дедушка Мороз готовит персональное поздравление вашему ребёнку!\n\n'
      'Укажите имя и несколько фактов: что ребёнок любит, чему научился и чем вас радует. '
      'Дед Мороз скажет об этом в видео! Фотография ребёнка появится в красивой новогодней рамке.\n\n'
      '🎬 Прикреплён короткий пример — начало поздравления. Полный ролик около минуты, до 70 секунд. '
      'Перед созданием видео можно прослушать озвучку, один раз исправить данные и выбрать первый или второй вариант.\n\n'
      '🎁 Акционные три дня — 16, 17 и 18 ноября: 990 ₽.\n'
      'С 19 ноября до конца ноября — 1490 ₽. В декабре — 1990 ₽.\n'
      'Цена 990 ₽ действует до 18 ноября 23:59 МСК.\n\nНажмите «Старт», чтобы заказать поздравление!')


async def messages(bot):
    if not sales.EXAMPLE.is_file():raise ValueError('Сначала подготовьте examples/ded_moroz_preview.mp4 командой ded_moroz_prepare_example.')
    await sales.check_example()
    video=await bot.upload_media(InputMedia(path=str(sales.EXAMPLE),type=UploadType.VIDEO))
    if getattr(video,'type',None)!='video':raise ValueError('MAX не вернул видео-пример.')
    b=InlineKeyboardBuilder().row(CallbackButton(text='🎅 Старт',payload='dm_launch_start'))
    return [('🎬 Начало поздравления Деда Мороза:',[video]),(TEXT,[b.as_markup()])]


async def preview(event,*,confirm=False):
    from ded_moroz import tell
    for text,attachments in await messages(event.bot):
        await tell(event,text,attachments=attachments);await asyncio.sleep(.5)
    if confirm:
        b=InlineKeyboardBuilder().row(CallbackButton(text='📣 Подтвердить рассылку',payload='dm_broadcast_confirm'))
        await tell(event,'Это предпросмотр. Отправка подписчикам начнётся только по кнопке подтверждения, не ранее 16 ноября. Повторный запуск продолжит с сохранённого места.',attachments=[b.as_markup()])


async def send(event,lock):
    from ded_moroz import read,tell
    from handlers import max_error_status
    today=datetime.fromtimestamp(database._now_ts(),ZoneInfo('Europe/Moscow')).date()
    if not sales.enabled() or not (datetime(2026,11,16).date()<=today<=datetime(2026,11,18).date()):
        await tell(event,'Рассылка с этой акцией доступна 16–18 ноября 2026 года. Сейчас отправка не запущена.');return
    if lock.locked():
        await tell(event,'Рассылка уже выполняется.');return
    async with lock:
        content=await messages(event.bot)
        success=failed=0;stopped=False
        for user in await database.get_active_user_ids():
            day=datetime.fromtimestamp(database._now_ts(),ZoneInfo('Europe/Moscow')).date()
            if day>datetime(2026,11,18).date() or not sales.enabled():stopped=True;break
            row=await read('SELECT * FROM ded_moroz_broadcast WHERE campaign=? AND user_id=?',(CAMPAIGN,user))
            if row and row['completed']:continue
            current=await database.get_user(user)
            if not current or not current['is_active']:continue
            try:
                for index,(text,attachments) in enumerate(content):
                    if row and index<row['next_message']:continue
                    result=await event.bot.send_message(user_id=user,text=text,attachments=attachments)
                    if result is None:raise RuntimeError('MAX не подтвердил рассылку.')
                    async with aiosqlite.connect(database.DATABASE_PATH,timeout=30) as db:
                        await db.execute('''INSERT INTO ded_moroz_broadcast(campaign,user_id,next_message,completed) VALUES (?,?,?,?)
                            ON CONFLICT(campaign,user_id) DO UPDATE SET next_message=MAX(next_message,excluded.next_message),completed=MAX(completed,excluded.completed)''',
                            (CAMPAIGN,user,index+1,int(index==len(content)-1)))
                        await db.commit()
                    await asyncio.sleep(.5)
                success+=1
            except Exception as exc:
                logger.error('Рассылка Деда Мороза: пользователь %s, ошибка %s',user,type(exc).__name__)
                failed+=1
                status=max_error_status(exc)
                if status==403:await database.mark_user_inactive(user)
                if status in (401,429):stopped=True;break
                await asyncio.sleep(.5)
        await tell(event,('⏸ Рассылка остановлена.' if stopped else '✅ Рассылка завершена.')+f' Успешно: {success}, Ошибок: {failed}.')

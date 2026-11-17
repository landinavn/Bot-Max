"""Paid Ded Moroz entitlements; Moscow prices, signed-receipt grants and public offer."""
import json
import os
import asyncio
from weakref import WeakValueDictionary
from datetime import datetime
from uuid import uuid4
from zoneinfo import ZoneInfo

import aiosqlite
from maxapi.types import CallbackButton,LinkButton,InputMedia
from maxapi.enums import UploadType
from maxapi.utils.inline_keyboard import InlineKeyboardBuilder

import database
from config import BASE_DIR
from ded_moroz_intro import INTRO_SECONDS

PRODUCT='Персональное видеопоздравление Деда Мороза'
EXAMPLE=BASE_DIR/'examples/ded_moroz_preview.mp4'
PRICE_TEXT=('🎁 До 18 ноября включительно — 990 ₽.\n'
            'С 19 по 30 ноября — 1490 ₽.\nВ декабре — 1990 ₽.\nВсе даты — по московскому времени.')
DESCRIPTION=('🎅 Персональное поздравление от Дедушки Мороза вашему ребёнку!\n\n'
    'Укажите имя ребёнка и несколько коротких фактов: увлечение, достижение, качество и маленькое задание на год. '
    'Дед Мороз озвучит их в поздравлении. Добавьте фотографию ребёнка — она тоже появится в видео, в новогодней рамке.\n\n'
    '🎬 Пример — начало поздравления. Полный ролик обычно около минуты, максимум 70 секунд.\n'
    '🎧 Перед созданием видео вы прослушаете озвучку. Можно один раз изменить данные ребёнка, '
    'получить второй вариант и выбрать, какой оставить.\n\n'+PRICE_TEXT)
LIMITS={'name':(24,2),'fact1':(40,6),'fact2':(40,6),'fact3':(20,2),'task':(45,7)}
MAX_SPEECH_SECONDS=70-INTRO_SECONDS-.5
CHECKOUT_LOCKS=WeakValueDictionary()


def enabled():return os.getenv('DED_MOROZ_PUBLIC_ENABLED','1').strip().lower() not in ('0','false','no')


def price_at(timestamp=None):
    value=datetime.fromtimestamp(database._now_ts() if timestamp is None else timestamp,ZoneInfo('Europe/Moscow'))
    if value.year!=2026 or value.month not in (11,12):return None
    if value.month==12:return 199000
    if value.day<15:return None
    return 99000 if value.day<=18 else 149000


def validate_field(field,value):
    chars,words=LIMITS[field]
    if len(value)>chars or len(value.split())>words:
        raise ValueError(f'Пожалуйста, сократите ответ: до {chars} символов и {words} слов. Это нужно, чтобы поздравление уложилось в 70 секунд.')
    return value


async def init_tables(db):
    await db.execute('''CREATE TABLE IF NOT EXISTS ded_moroz_payments (
        order_id TEXT PRIMARY KEY,user_id INTEGER NOT NULL,amount_kopecks INTEGER NOT NULL,
        product_name TEXT NOT NULL,package_id TEXT NOT NULL,credits INTEGER NOT NULL DEFAULT 0,
        status TEXT NOT NULL DEFAULT 'pending',payment_link TEXT,provider_payment_id TEXT UNIQUE,
        claimed_id TEXT UNIQUE,notified INTEGER NOT NULL DEFAULT 0,next_attempt INTEGER NOT NULL DEFAULT 0,
        created_at TEXT DEFAULT CURRENT_TIMESTAMP,paid_at TEXT)''')
    await db.execute('''CREATE TABLE IF NOT EXISTS ded_moroz_reviews (
        order_id TEXT PRIMARY KEY,user_id INTEGER NOT NULL,text TEXT NOT NULL,
        delivered INTEGER NOT NULL DEFAULT 0,next_attempt INTEGER NOT NULL DEFAULT 0)''')
    await db.execute('''CREATE TABLE IF NOT EXISTS ded_moroz_broadcast (
        campaign TEXT NOT NULL,user_id INTEGER NOT NULL,next_message INTEGER NOT NULL DEFAULT 0,
        completed INTEGER NOT NULL DEFAULT 0,PRIMARY KEY(campaign,user_id))''')
    await db.execute('''CREATE TABLE IF NOT EXISTS ded_moroz_mobile_jobs (
        order_id TEXT PRIMARY KEY,status TEXT NOT NULL DEFAULT 'queued',source_path TEXT,path TEXT,url TEXT,
        notified INTEGER NOT NULL DEFAULT 0,next_attempt INTEGER NOT NULL DEFAULT 0,error TEXT)''')


async def payment(order_id):
    async with aiosqlite.connect(database.DATABASE_PATH,timeout=30) as db:
        db.row_factory=aiosqlite.Row
        async with db.execute('SELECT * FROM ded_moroz_payments WHERE order_id=?',(order_id,)) as cur:
            row=await cur.fetchone()
            return dict(row,order_type='ded_moroz') if row else None


def validate_order(row):
    if (row['amount_kopecks'] not in (99000,149000,199000) or row['credits']!=0
        or row['product_name']!=PRODUCT or row['package_id']!='ded_moroz_'+str(row['amount_kopecks']//100)):
        raise ValueError('Некорректный тариф поздравления Деда Мороза.')


async def checkout(user_id):
    if isinstance(user_id,bool) or not isinstance(user_id,int) or user_id<=0:
        raise ValueError('Некорректный пользователь.')
    price=price_at()
    if not enabled() or price is None:raise ValueError('Покупка поздравления сейчас недоступна.')
    async with aiosqlite.connect(database.DATABASE_PATH,timeout=30) as db:
        await db.execute('BEGIN IMMEDIATE');db.row_factory=aiosqlite.Row
        async with db.execute("SELECT * FROM ded_moroz_payments WHERE user_id=? AND status='pending' AND amount_kopecks=? ORDER BY rowid DESC LIMIT 1",(user_id,price)) as cur:
            row=await cur.fetchone()
        if row:return dict(row,order_type='ded_moroz')
        key='dm_'+uuid4().hex
        await db.execute('INSERT INTO ded_moroz_payments(order_id,user_id,amount_kopecks,product_name,package_id) VALUES (?,?,?,?,?)',
                         (key,user_id,price,PRODUCT,'ded_moroz_'+str(price//100)))
        await database.increment_counter('dm_checkout',user_id=user_id,connection=db)
        await db.commit()
    return await payment(key)


async def set_link(key,link):
    async with aiosqlite.connect(database.DATABASE_PATH,timeout=30) as db:
        await db.execute("UPDATE ded_moroz_payments SET payment_link=? WHERE order_id=? AND status='pending'",(link,key));await db.commit()


async def allowed(row,admin):
    if admin is not None and row['user_id']==admin:return True
    from ded_moroz_family import is_family
    if is_family(row['details']):return False
    key=row.get('payment_order_id')
    pay=await payment(key) if key else None
    return bool(pay and pay['user_id']==row['user_id'] and pay['status']=='paid' and pay['claimed_id']==row['id'])


async def _claim(db,user_id):
    async with db.execute("SELECT * FROM ded_moroz_orders WHERE user_id=? AND status NOT IN ('done','cancelled')",(user_id,)) as cur:
        active=await cur.fetchone()
    if active:return active['id']
    async with db.execute("SELECT * FROM ded_moroz_payments WHERE user_id=? AND status='paid' AND claimed_id IS NULL ORDER BY rowid LIMIT 1",(user_id,)) as cur:
        pay=await cur.fetchone()
    if not pay:return None
    key=uuid4().hex
    await db.execute('INSERT INTO ded_moroz_orders(id,user_id,payment_order_id) VALUES (?,?,?)',(key,user_id,pay['order_id']))
    await db.execute('UPDATE ded_moroz_payments SET claimed_id=? WHERE order_id=?',(key,pay['order_id']))
    return key


async def claim(user_id,admin=None):
    from ded_moroz import get
    async with aiosqlite.connect(database.DATABASE_PATH,timeout=30) as db:
        await db.execute('BEGIN IMMEDIATE');db.row_factory=aiosqlite.Row
        key=await _claim(db,user_id);await db.commit()
    row=await get(key) if key else None
    return row if row and await allowed(row,admin) else None


async def mark_paid(key,payment_id):
    async with aiosqlite.connect(database.DATABASE_PATH,timeout=30) as db:
        await db.execute('BEGIN IMMEDIATE');db.row_factory=aiosqlite.Row
        async with db.execute('SELECT * FROM ded_moroz_payments WHERE order_id=?',(key,)) as cur:row=await cur.fetchone()
        if not row:raise ValueError('Заказ Деда Мороза не найден.')
        validate_order(row)
        if row['status']=='paid':
            if row['provider_payment_id']!=payment_id:raise ValueError('Другой платёж для оплаченного заказа.')
            return False
        if not payment_id:raise ValueError('Нет номера платежа.')
        for table in ('payment_orders','audio_orders','musical_orders','cartoon_orders','photo_orders','animation_orders','ded_moroz_payments'):
            async with db.execute(f'SELECT 1 FROM {table} WHERE provider_payment_id=?',(payment_id,)) as cur:
                if await cur.fetchone():raise ValueError('Платёж уже использован.')
        await db.execute("UPDATE ded_moroz_payments SET status='paid',provider_payment_id=?,paid_at=CURRENT_TIMESTAMP WHERE order_id=?",(payment_id,key))
        await db.execute('INSERT INTO users(user_id) VALUES (?) ON CONFLICT(user_id) DO NOTHING',(row['user_id'],))
        await database.increment_purchases(row['user_id'],connection=db)
        await database.increment_counter('buy_ded_moroz',user_id=row['user_id'],connection=db)
        await database.increment_counter('buy_ded_moroz_'+str(row['amount_kopecks']//100),user_id=row['user_id'],connection=db)
        await _claim(db,row['user_id'])
        await db.commit();return True


async def offer(event,*,admin=None):
    from ded_moroz import tell
    if not enabled() and event.get_ids()[1]!=admin:
        await tell(event,'Поздравление Деда Мороза скоро появится.');return
    try:
        await check_example()
        video=await event.bot.upload_media(InputMedia(path=str(EXAMPLE),type=UploadType.VIDEO))
        if getattr(video,'type',None)!='video':raise ValueError('MAX не вернул видео-пример.')
        await tell(event,'🎬 Начало персонального поздравления:',attachments=[video])
    except Exception:await tell(event,'Пример скоро появится.')
    buttons=InlineKeyboardBuilder().row(CallbackButton(text='🎅 Начать',payload='dm_start'))
    buttons.row(CallbackButton(text='🎬 Мои готовые поздравления',payload='dm_last'))
    buttons.row(CallbackButton(text='⬅️ Назад',payload='back_to_main'))
    await tell(event,DESCRIPTION,attachments=[buttons.as_markup()])


async def start(event,context,admin,payments):
    from ded_moroz import enter,tell
    user=event.get_ids()[1]
    if await claim(user,admin):
        await enter(event,context,admin);return
    await context.clear()
    if admin is None:
        await tell(event,'Мастерская временно недоступна. Попробуйте позже.');return
    if not EXAMPLE.is_file():
        await tell(event,'Скоро здесь появится пример поздравления. Заказ пока недоступен.');return
    if payments is None:
        await tell(event,'Оплата временно недоступна. Попробуйте позже.');return
    try:
        from ded_moroz import Settings,read
        avatar=await read('SELECT look_id FROM ded_moroz_avatar WHERE singleton=1')
        settings=Settings.from_config(payments.config)
        settings.ready(payments.config,stage='audio')
        settings.ready(payments.config,stage='video',avatar_known=bool(avatar and avatar['look_id']))
        await check_example()
    except Exception:
        await tell(event,'Мастерская временно недоступна. Заказ не оплачен, попробуйте позже.');return
    try:
        lock=CHECKOUT_LOCKS.setdefault(user,asyncio.Lock())
        async with lock:
            if await claim(user,admin):
                await enter(event,context,admin);return
            row=await checkout(user)
            link=await payments.create_payment_link(user,order_id=row['order_id'],amount=row['amount_kopecks']//100,description=row['product_name'])
    except ValueError as exc:
        await tell(event,str(exc));return
    buttons=InlineKeyboardBuilder().row(LinkButton(text=f"💳 Оплатить {row['amount_kopecks']//100} ₽",url=link))
    buttons.row(CallbackButton(text='✅ Я оплатил(а), продолжить',payload='dm_continue'))
    await tell(event,f"🎅 Персональное поздравление — {row['amount_kopecks']//100} ₽.\nПосле оплаты вернитесь в бот — начнём с имени ребёнка.",attachments=[buttons.as_markup()])


async def paid_notifications(bot):
    from ded_moroz import read
    now=database._now_ts()
    for row in await read("SELECT * FROM ded_moroz_payments WHERE status='paid' AND notified=0 AND next_attempt<=? LIMIT 10",(now,),all_rows=True):
        try:
            buttons=InlineKeyboardBuilder().row(CallbackButton(text='🎅 Продолжить поздравление',payload='dm_continue'))
            result=await bot.send_message(user_id=row['user_id'],text='✅ Оплата поздравления получена! Вернитесь к заказу — Дедушка Мороз ждёт имя и факты о ребёнке.',attachments=[buttons.as_markup()])
            if result is None:raise RuntimeError('MAX не подтвердил оплату.')
            async with aiosqlite.connect(database.DATABASE_PATH,timeout=30) as db:
                await db.execute('UPDATE ded_moroz_payments SET notified=1 WHERE order_id=?',(row['order_id'],));await db.commit()
        except Exception:
            async with aiosqlite.connect(database.DATABASE_PATH,timeout=30) as db:
                await db.execute('UPDATE ded_moroz_payments SET next_attempt=? WHERE order_id=?',(now+60,row['order_id']));await db.commit()


async def statistics(admin=None):
    async with aiosqlite.connect(database.DATABASE_PATH,timeout=30) as db:
        async with db.execute("SELECT COUNT(*),COALESCE(SUM(amount_kopecks),0) FROM ded_moroz_payments WHERE status='paid' AND (? IS NULL OR user_id!=?)",(admin,admin)) as cur:
            paid,revenue=await cur.fetchone()
        async with db.execute("SELECT COUNT(*) FROM ded_moroz_orders WHERE payment_order_id IS NOT NULL AND status='done' AND (? IS NULL OR user_id!=?)",(admin,admin)) as cur:
            ready=(await cur.fetchone())[0]
        async with db.execute("SELECT COUNT(*) FROM ded_moroz_orders WHERE payment_order_id IS NOT NULL AND status='manual' AND (? IS NULL OR user_id!=?)",(admin,admin)) as cur:
            attention=(await cur.fetchone())[0]
    return {'paid':paid,'revenue':revenue,'ready':ready,'attention':attention}


async def check_example():
    from media_service import audio_duration
    if not EXAMPLE.is_file() or await audio_duration(EXAMPLE)>15.1:
        raise ValueError('Нужен короткий пример до 15 секунд.')

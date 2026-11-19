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
FAMILY_PRODUCT='Персональное видеопоздравление Деда Мороза для нескольких детей'
EXAMPLE=BASE_DIR/'examples/ded_moroz_preview.mp4'
PRICE_TEXT=('🎁 Стоимость поздравления:\n'
            'До 30 ноября — 1490 ₽.\nВ декабре — 1990 ₽.\nВсе даты — по московскому времени.')
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


def family_price_at(timestamp=None):
    value=datetime.fromtimestamp(database._now_ts() if timestamp is None else timestamp,ZoneInfo('Europe/Moscow'))
    if value.year!=2026 or value.month not in (11,12):return None
    if value.month==12:return 199000
    if value.day<19:return None
    return 99000 if value.day<=22 else 149000


def family_price_text(timestamp=None):
    price=family_price_at(timestamp)
    if price is None:return 'Заказ семейного поздравления сейчас недоступен.'
    if price==99000:
        return ('🔥 До 22 ноября 23:59 МСК — 990 ₽.\n'
                'С 23 по 30 ноября — 1490 ₽.\nВ декабре — 1990 ₽.')
    if price==149000:return '🎁 До 30 ноября — 1490 ₽.\nВ декабре — 1990 ₽.'
    return '🎁 В декабре — 1990 ₽.'


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
    async with db.execute('PRAGMA table_info(ded_moroz_payments)') as cur:
        columns={r[1] for r in await cur.fetchall()}
    if 'variant' not in columns:
        await db.execute("ALTER TABLE ded_moroz_payments ADD COLUMN variant TEXT NOT NULL DEFAULT 'single'")
    if 'children_count' not in columns:
        await db.execute('ALTER TABLE ded_moroz_payments ADD COLUMN children_count INTEGER')
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
    row=dict(row)
    variant=row.get('variant','single')
    family=variant=='family'
    expected=FAMILY_PRODUCT if family else PRODUCT
    prefix='ded_moroz_family_' if family else 'ded_moroz_'
    n=row.get('children_count')
    if (variant not in ('single','family') or row['amount_kopecks'] not in (99000,149000,199000)
        or row['credits']!=0 or row['product_name']!=expected
        or row['package_id']!=prefix+str(row['amount_kopecks']//100)
        or (family and (type(n) is not int or not 2<=n<=6)) or (not family and n is not None)):
        raise ValueError('Некорректный тариф поздравления Деда Мороза.')


async def checkout(user_id,*,variant='single',children_count=None):
    if isinstance(user_id,bool) or not isinstance(user_id,int) or user_id<=0:
        raise ValueError('Некорректный пользователь.')
    if variant not in ('single','family') or (variant=='family' and (type(children_count) is not int or not 2<=children_count<=6)) or (variant=='single' and children_count is not None):
        raise ValueError('Выберите корректный тип поздравления и количество детей.')
    price=family_price_at() if variant=='family' else price_at()
    if not enabled() or price is None:raise ValueError('Покупка поздравления сейчас недоступна.')
    family=variant=='family'
    product=FAMILY_PRODUCT if family else PRODUCT
    package=('ded_moroz_family_' if family else 'ded_moroz_')+str(price//100)
    async with aiosqlite.connect(database.DATABASE_PATH,timeout=30) as db:
        await db.execute('BEGIN IMMEDIATE');db.row_factory=aiosqlite.Row
        async with db.execute("SELECT * FROM ded_moroz_payments WHERE user_id=? AND status='pending' AND amount_kopecks=? AND variant=? AND children_count IS ? ORDER BY rowid DESC LIMIT 1",(user_id,price,variant,children_count)) as cur:
            row=await cur.fetchone()
        if row:return dict(row,order_type='ded_moroz')
        key='dm_'+uuid4().hex
        await db.execute('INSERT INTO ded_moroz_payments(order_id,user_id,amount_kopecks,product_name,package_id,variant,children_count) VALUES (?,?,?,?,?,?,?)',
                         (key,user_id,price,product,package,variant,children_count))
        await database.increment_counter('dm_family_checkout' if family else 'dm_checkout',user_id=user_id,connection=db)
        await db.commit()
    return await payment(key)


async def set_link(key,link):
    async with aiosqlite.connect(database.DATABASE_PATH,timeout=30) as db:
        await db.execute("UPDATE ded_moroz_payments SET payment_link=? WHERE order_id=? AND status='pending'",(link,key));await db.commit()


async def allowed(row,admin):
    if admin is not None and row['user_id']==admin:return True
    from ded_moroz_family import is_family
    key=row.get('payment_order_id')
    pay=await payment(key) if key else None
    if not (pay and pay['user_id']==row['user_id'] and pay['status']=='paid' and pay['claimed_id']==row['id']):return False
    details=json.loads(row['details'])
    if is_family(details):
        return pay.get('variant')=='family' and pay.get('children_count')==details.get('children_count')
    return pay.get('variant','single')=='single'


async def _claim(db,user_id,payment_key=None):
    if payment_key:
        async with db.execute("SELECT * FROM ded_moroz_payments WHERE order_id=? AND user_id=? AND status='paid'",(payment_key,user_id)) as cur:
            pay=await cur.fetchone()
        if not pay:return None
        if pay['claimed_id']:return pay['claimed_id']
    else:
        async with db.execute("SELECT * FROM ded_moroz_payments WHERE user_id=? AND status='paid' AND variant='single' AND claimed_id IS NULL ORDER BY rowid LIMIT 1",(user_id,)) as cur:
            pay=await cur.fetchone()
        if not pay:
            async with db.execute("SELECT * FROM ded_moroz_orders WHERE user_id=? AND COALESCE(json_extract(details,'$.variant'),'single')='single' AND status NOT IN ('done','cancelled') ORDER BY rowid DESC LIMIT 1",(user_id,)) as cur:
                active=await cur.fetchone()
            return active['id'] if active else None
    pay=dict(pay);validate_order(pay)
    details=dict(variant='family',children_count=pay['children_count']) if pay.get('variant')=='family' else {}
    key=uuid4().hex
    await db.execute('INSERT INTO ded_moroz_orders(id,user_id,payment_order_id,details) VALUES (?,?,?,?)',(key,user_id,pay['order_id'],json.dumps(details,ensure_ascii=False)))
    if details:
        await database.increment_counter('dm_family_started',user_id=user_id,connection=db)
        await database.increment_counter('dm_family_children_'+str(pay['children_count']),user_id=user_id,connection=db)
    await db.execute('UPDATE ded_moroz_payments SET claimed_id=? WHERE order_id=?',(key,pay['order_id']))
    return key


async def claim(user_id,admin=None,*,payment_key=None):
    from ded_moroz import get
    async with aiosqlite.connect(database.DATABASE_PATH,timeout=30) as db:
        await db.execute('BEGIN IMMEDIATE');db.row_factory=aiosqlite.Row
        key=await _claim(db,user_id,payment_key);await db.commit()
    row=await get(key) if key else None
    return row if row and await allowed(row,admin) else None


async def mark_paid(key,payment_id):
    async with aiosqlite.connect(database.DATABASE_PATH,timeout=30) as db:
        await db.execute('BEGIN IMMEDIATE');db.row_factory=aiosqlite.Row
        async with db.execute('SELECT * FROM ded_moroz_payments WHERE order_id=?',(key,)) as cur:row=await cur.fetchone()
        if not row:raise ValueError('Заказ Деда Мороза не найден.')
        row=dict(row);validate_order(row)
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
        prefix='buy_ded_moroz_family' if row.get('variant')=='family' else 'buy_ded_moroz'
        await database.increment_counter(prefix,user_id=row['user_id'],connection=db)
        await database.increment_counter(prefix+'_'+str(row['amount_kopecks']//100),user_id=row['user_id'],connection=db)
        await _claim(db,row['user_id'],key)
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
    user=event.get_ids()[1]
    from ded_moroz import read
    active=await read("SELECT * FROM ded_moroz_orders WHERE user_id=? AND COALESCE(json_extract(details,'$.variant'),'single')='single' AND status NOT IN ('done','cancelled') ORDER BY rowid DESC LIMIT 5",(user,),all_rows=True)
    if admin is not None and user==admin:
        buttons.row(CallbackButton(text='➕ Новое тестовое поздравление',payload='dm_single_new:'+uuid4().hex))
    elif active:
        buttons.row(CallbackButton(text='➕ Заказать ещё одно (новый счёт)',payload='dm_another'))
    for row in active:
        if await allowed(row,admin):
            buttons.row(CallbackButton(text='Продолжить · '+row['id'][:8]+' · '+row['status'],payload='dm_open:'+row['id']))
    buttons.row(CallbackButton(text='🎬 Мои готовые поздравления',payload='dm_last'))
    buttons.row(CallbackButton(text='⬅️ Назад',payload='back_to_main'))
    await tell(event,DESCRIPTION,attachments=[buttons.as_markup()])


async def start(event,context,admin,payments,*,new=False):
    from ded_moroz import enter,tell
    user=event.get_ids()[1]
    active=await claim(user,admin) if not new else None
    if active:
        await enter(event,context,admin,key=active['id']);return
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
            active=await claim(user,admin) if not new else None
            if active:
                await enter(event,context,admin,key=active['id']);return
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
            claimed=await claim(row['user_id'],payment_key=row['order_id'])
            if not claimed:raise ValueError('Нет заказа для подтверждённой оплаты.')
            buttons=InlineKeyboardBuilder().row(CallbackButton(text='🎅 Продолжить поздравление',payload='dm_open:'+claimed['id']))
            result=await bot.send_message(user_id=row['user_id'],text='✅ Оплата поздравления получена! Вернитесь к заказу — заполним имена и факты.',attachments=[buttons.as_markup()])
            if result is None:raise RuntimeError('MAX не подтвердил оплату.')
            async with aiosqlite.connect(database.DATABASE_PATH,timeout=30) as db:
                await db.execute('UPDATE ded_moroz_payments SET notified=1 WHERE order_id=?',(row['order_id'],));await db.commit()
        except Exception:
            async with aiosqlite.connect(database.DATABASE_PATH,timeout=30) as db:
                await db.execute('UPDATE ded_moroz_payments SET next_attempt=? WHERE order_id=?',(now+60,row['order_id']));await db.commit()


async def statistics(admin=None,variant='single'):
    if variant not in ('single','family'):raise ValueError('Неизвестный вид поздравления.')
    async with aiosqlite.connect(database.DATABASE_PATH,timeout=30) as db:
        async with db.execute("SELECT COUNT(*),COALESCE(SUM(amount_kopecks),0) FROM ded_moroz_payments WHERE status='paid' AND variant=? AND (? IS NULL OR user_id!=?)",(variant,admin,admin)) as cur:
            paid,revenue=await cur.fetchone()
        async with db.execute("SELECT COUNT(*) FROM ded_moroz_orders o JOIN ded_moroz_payments p ON p.order_id=o.payment_order_id AND p.claimed_id=o.id WHERE o.status='done' AND p.variant=? AND (? IS NULL OR o.user_id!=?)",(variant,admin,admin)) as cur:
            ready=(await cur.fetchone())[0]
        async with db.execute("SELECT COUNT(*) FROM ded_moroz_orders o JOIN ded_moroz_payments p ON p.order_id=o.payment_order_id AND p.claimed_id=o.id WHERE o.status='manual' AND p.variant=? AND (? IS NULL OR o.user_id!=?)",(variant,admin,admin)) as cur:
            attention=(await cur.fetchone())[0]
    return {'paid':paid,'revenue':revenue,'ready':ready,'attention':attention}


async def check_example():
    from media_service import audio_duration
    if not EXAMPLE.is_file() or await audio_duration(EXAMPLE)>15.1:
        raise ValueError('Нужен короткий пример до 15 секунд.')

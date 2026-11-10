"""11.11 campaign: Moscow deadline, recipient eligibility and durable entitlements."""
from datetime import datetime
from zoneinfo import ZoneInfo
from uuid import uuid4

import aiosqlite
import database

DEADLINE = int(datetime(2026,11,12,tzinfo=ZoneInfo('Europe/Moscow')).timestamp())
PACKAGES = {
    '111': (11100,3,'Акция 11.11 — 3 генерации (только изображения)',True,0),
    '333': (33300,11,'Акция 11.11 — 11 генераций (только изображения)',True,0),
    '799': (79900,15,'Акция 11.11 — 15 генераций + оживление фото в подарок',False,1),
}
IMAGE_ONLY = ('Этот кредит можно использовать только для создания изображений. '
              'Для других услуг используйте обычные кредиты или оплатите отдельно')
ONCE = 'Этот пакет можно купить только один раз'
EXPIRED = 'Акция 11.11 завершена. Выберите обычный тариф.'
TEXT = ('🔥 Акция 11.11! Только сегодня и до 23:59 11 ноября:\n\n'
        '• 111 ₽ — 3 генерации (только изображения, один раз)\n'
        '• 333 ₽ — 11 генераций (только изображения)\n'
        '• 799 ₽ — 15 генераций + оживление фото в подарок!\n\n'
        '🎁 Купленные генерации можно использовать позже, без ограничения по времени. '
        'Скидки не суммируются с другими акциями.\n\n'
        '👇 Выберите пакет в меню покупок!')
ANIMATION_EXAMPLE = 'оживленное фото.mp4'


def now():
    return database._now_ts()


def open_now():
    return now()<DEADLINE


def code_for(order):
    package=order['package_id'] or ''
    return package.removeprefix('promo1111_') if package.startswith('promo1111_') else None


def validate_order(order):
    code=code_for(order)
    offer=PACKAGES.get(code)
    if (not offer or (order['amount_kopecks'],order['credits'],order['product_name'])!=offer[:3]
            or order['promo1111_expires_at']!=DEADLINE or order['promo_campaign_id']!='1111_2026'):
        raise ValueError('Некорректный пакет акции 11.11.')
    return code,offer


def image_balance(user):
    return user['balance']+user['promo_image_balance'] if user else 0


async def init(db):
    for table,columns in (
        ('users',{'promo_image_balance':'INTEGER NOT NULL DEFAULT 0 CHECK(promo_image_balance>=0)',
                  'free_animation_count':'INTEGER NOT NULL DEFAULT 0 CHECK(free_animation_count>=0)'}),
        ('payment_orders',{'payment_link':'TEXT','promo1111_expires_at':'INTEGER'}),
        ('animation_orders',{'promo_bonus':'INTEGER NOT NULL DEFAULT 0'}),
    ):
        async with db.execute('PRAGMA table_info('+table+')') as cursor:
            present={r[1] for r in await cursor.fetchall()}
        for name,definition in columns.items():
            if name not in present:
                await db.execute('ALTER TABLE '+table+' ADD COLUMN '+name+' '+definition)
    await db.execute('''CREATE TABLE IF NOT EXISTS promo1111_recipients (
        user_id INTEGER PRIMARY KEY, activated_at INTEGER, completed INTEGER NOT NULL DEFAULT 0,
        next_message INTEGER NOT NULL DEFAULT 0)''')
    # One permanent checkout per user for the single-use pack: retries reuse it.
    await db.execute("CREATE UNIQUE INDEX IF NOT EXISTS promo1111_single_checkout ON payment_orders(user_id) WHERE package_id='promo1111_111'")


async def recipient(user_id):
    async with aiosqlite.connect(database.DATABASE_PATH,timeout=30) as db:
        db.row_factory=aiosqlite.Row
        async with db.execute('SELECT * FROM promo1111_recipients WHERE user_id=?',(user_id,)) as cursor:
            row=await cursor.fetchone()
            return dict(row) if row else None


async def progress(user_id,index,*,activate=False):
    async with aiosqlite.connect(database.DATABASE_PATH,timeout=30) as db:
        await db.execute('''INSERT INTO promo1111_recipients(user_id,next_message,activated_at,completed) VALUES (?,?,?,?)
            ON CONFLICT(user_id) DO UPDATE SET next_message=MAX(next_message,excluded.next_message),
            activated_at=COALESCE(activated_at,excluded.activated_at),completed=MAX(completed,excluded.completed)''',
            (user_id,index,now() if activate else None,int(activate)))
        await db.commit()


async def eligible(user_id):
    if not open_now() or not user_id:
        return False
    row=await recipient(user_id)
    return bool(row and row['activated_at'] is not None and row['activated_at']<=now()<DEADLINE)


async def create_order(user_id,code):
    if isinstance(user_id,bool) or not isinstance(user_id,int) or user_id<=0 or code not in PACKAGES:
        raise ValueError('Неизвестный пакет 11.11.')
    async with aiosqlite.connect(database.DATABASE_PATH,timeout=30) as db:
        await db.execute('BEGIN IMMEDIATE'); db.row_factory=aiosqlite.Row
        if not open_now(): raise database.PromoUnavailableError(EXPIRED)
        async with db.execute('SELECT activated_at FROM promo1111_recipients WHERE user_id=?',(user_id,)) as cur:
            row=await cur.fetchone()
        if not row or row[0] is None or not row[0]<=now()<DEADLINE:
            raise database.PromoUnavailableError('Акция доступна после получения рассылки 11.11.')
        package='promo1111_'+code
        if code=='111':
            async with db.execute('SELECT * FROM payment_orders WHERE user_id=? AND package_id=?',(user_id,package)) as cur:
                existing=await cur.fetchone()
            if existing:
                if existing['status']=='paid': raise database.PromoUnavailableError(ONCE)
                return dict(existing)
        amount,credits,title,_,_=PACKAGES[code]
        key=f'promo_{code}_{user_id}_{uuid4().hex}'
        await db.execute('''INSERT INTO payment_orders(order_id,user_id,credits,amount_kopecks,product_name,
            package_id,promo_campaign_id,promo1111_expires_at) VALUES (?,?,?,?,?,?,?,?)''',
            (key,user_id,credits,amount,title,package,'1111_2026',DEADLINE))
        await db.commit()
    return dict(await database.get_payment_order(key))


async def save_link(order_id,link):
    if not open_now(): raise database.PromoUnavailableError(EXPIRED)
    async with aiosqlite.connect(database.DATABASE_PATH,timeout=30) as db:
        await db.execute('UPDATE payment_orders SET payment_link=? WHERE order_id=? AND status=\'pending\'',(link,order_id))
        await db.commit()


async def claim_animation(user_id):
    """Reserve a bonus as a durable one-photo order; repeated clicks reuse it."""
    async with aiosqlite.connect(database.DATABASE_PATH,timeout=30) as db:
        await db.execute('BEGIN IMMEDIATE'); db.row_factory=aiosqlite.Row
        async with db.execute('''SELECT o.* FROM animation_orders o WHERE user_id=? AND promo_bonus=1
            AND status='paid' AND NOT EXISTS(SELECT 1 FROM animation_jobs j WHERE j.order_id=o.order_id)
            ORDER BY created_at LIMIT 1''',(user_id,)) as cur:
            row=await cur.fetchone()
        if row: return dict(row)
        result=await db.execute('UPDATE users SET free_animation_count=free_animation_count-1 WHERE user_id=? AND free_animation_count>0',(user_id,))
        if not result.rowcount: raise ValueError('Нет доступного бонусного оживления.')
        key=f'animate_bonus_{user_id}_{uuid4().hex}'
        await db.execute('''INSERT INTO animation_orders(order_id,user_id,tariff,amount_kopecks,quantity,
            product_name,package_id,status,promo_bonus,notified) VALUES (?,?,'promo1111_bonus',0,1,
            'Оживление фото — подарок 11.11','promo1111_animation','paid',1,1)''',(key,user_id))
        await db.commit()
    return await database.get_animation_order(key)

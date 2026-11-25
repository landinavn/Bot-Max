"""One dated promotion. Prices are persisted once, never recalculated on payment."""
from datetime import datetime
import re
from zoneinfo import ZoneInfo

CAMPAIGN = 'black_friday_20261125'
MOSCOW = ZoneInfo('Europe/Moscow')
START = int(datetime(2026, 11, 25, tzinfo=MOSCOW).timestamp())
END = int(datetime(2026, 11, 30, tzinfo=MOSCOW).timestamp())
TABLES = ('payment_orders', 'audio_orders', 'musical_orders', 'cartoon_orders',
          'photo_orders', 'animation_orders', 'ded_moroz_payments')
NOTICE = ('🖤 Чёрная пятница: скидка 20% на все услуги до воскресенья, '
          '29 ноября, 23:59 МСК! В меню указаны цены со скидкой. '
          'С другими скидками не суммируется; если персональная цена выгоднее, сохраним её.')


def now():
    import database
    return database._now_ts()


def active(timestamp=None):
    return START <= (now() if timestamp is None else timestamp) < END


def discount(amount):
    # The existing checkout accepts whole roubles. Round down in the customer's favour.
    return amount * 80 // 10000 * 100


def price(amount):
    return discount(amount) if active() else amount


def menu_text(text):
    """Only call on catalogue copy, never on invoices, lyrics or statistics."""
    if not active():
        return text
    return re.sub(r'(?<![\d.])(\d+(?:[ \u00a0]\d{3})*) ₽', lambda m: f'{discount(int(m[1].replace(" ","").replace(chr(160),""))*100)//100} ₽', text)


def fields(amount, *, eligible=True):
    stamp = now()
    if not eligible or not active(stamp):
        return {}
    return dict(amount_kopecks=discount(amount), sale_campaign=CAMPAIGN,
                sale_base_amount=amount, sale_created_at=stamp)


def base_order(order):
    """Validate persisted campaign identity before restoring its base price for tariff checks."""
    row = dict(order)
    if not row.get('sale_campaign'):
        return row
    amount = row.get('sale_base_amount')
    if (row['sale_campaign'] != CAMPAIGN or type(amount) is not int or amount <= 0
            or type(row.get('sale_created_at')) is not int or not active(row['sale_created_at'])
            or row['amount_kopecks'] != discount(amount) or row.get('promo_token')):
        raise ValueError('Некорректная сохранённая цена Чёрной пятницы.')
    row['amount_kopecks'] = amount
    row.pop('sale_campaign', None)
    return row


async def init(db):
    for table in TABLES:
        async with db.execute('PRAGMA table_info('+table+')') as cursor:
            columns = {r[1] for r in await cursor.fetchall()}
        for name, kind in (('sale_campaign','TEXT'), ('sale_base_amount','INTEGER'), ('sale_created_at','INTEGER')):
            if name not in columns:
                await db.execute(f'ALTER TABLE {table} ADD COLUMN {name} {kind}')


async def apply(db, table, order_id, *, eligible=True):
    if table not in TABLES:
        raise ValueError('Неизвестный тип заказа.')
    async with db.execute(f'SELECT amount_kopecks FROM {table} WHERE order_id=?', (order_id,)) as cursor:
        row = await cursor.fetchone()
    values = fields(row[0], eligible=eligible)
    if values:
        await db.execute(f'UPDATE {table} SET '+','.join(k+'=?' for k in values)+' WHERE order_id=?',
                         (*values.values(), order_id))

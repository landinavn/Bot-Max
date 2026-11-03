"""One-time fulfilment of a payment verified by the owner outside the webhook.

No account is inferred from neighbouring log events. The customer supplies the
order number and payment email; only the email digest is retained here.
"""
import hashlib
import hmac

import aiosqlite

import database


RECOVERY_NUMBER = '17-01032-83290'
PROVIDER_PAYMENT_ID = '01a18940-5f4e-7d42-9523-1d74196e026a'
EMAIL_DIGEST = 'f098aa8162b2d66c8cc53c20a57296e17d4925f399d0e04c045f1cd152e819b0'
PHOTO_ORDER_ID = 'photo_recovery_17_01032_83290'


class RecoveryUnavailable(ValueError):
    """The receipt was already attached elsewhere; manual reconciliation is needed."""


def email_matches(value):
    if not isinstance(value, str):
        return False
    digest = hashlib.sha256(value.strip().casefold().encode()).hexdigest()
    return hmac.compare_digest(digest, EMAIL_DIGEST)


async def claim_photo_recovery(user_id, order_number, email):
    """Commit the paid package, one gift credit and receipt ownership atomically."""
    if isinstance(user_id, bool) or not isinstance(user_id, int) or user_id <= 0:
        raise ValueError('Некорректный пользователь.')
    if order_number != RECOVERY_NUMBER or not email_matches(email):
        return None
    async with aiosqlite.connect(database.DATABASE_PATH, timeout=30) as db:
        db.row_factory = aiosqlite.Row
        await db.execute('BEGIN IMMEDIATE')
        async with db.execute('SELECT * FROM payment_recoveries WHERE provider_payment_id=?',
                              (PROVIDER_PAYMENT_ID,)) as cursor:
            existing = await cursor.fetchone()
        if existing:
            if existing['user_id'] != user_id:
                raise RecoveryUnavailable('Заказ уже закреплён за другим аккаунтом.')
            return dict(existing)
        for table in ('payment_orders', 'audio_orders', 'musical_orders',
                      'cartoon_orders', 'photo_orders', 'animation_orders'):
            async with db.execute(f'SELECT 1 FROM {table} WHERE provider_payment_id=?',
                                  (PROVIDER_PAYMENT_ID,)) as cursor:
                if await cursor.fetchone():
                    raise RecoveryUnavailable('Этот платёж уже учтён. Обратитесь в поддержку.')
        async with db.execute('SELECT 1 FROM photo_orders WHERE order_id=?', (PHOTO_ORDER_ID,)) as cursor:
            if await cursor.fetchone():
                raise RecoveryUnavailable('Заказ уже существует. Обратитесь в поддержку.')
        await db.execute('INSERT INTO users(user_id) VALUES (?) ON CONFLICT(user_id) DO NOTHING', (user_id,))
        # Suppress the generic payment notification: the custom apology is tracked
        # in payment_recoveries and retried on the next message or /start.
        await db.execute("""INSERT INTO photo_orders (
            order_id,user_id,tariff,amount_kopecks,product_name,status,
            provider_payment_id,paid_at,generation_status,style,user_notified
        ) VALUES (?,?,590,59000,?,'paid',?,CURRENT_TIMESTAMP,'queued','photo_style_ny',1)""",
            (PHOTO_ORDER_ID, user_id, database.PHOTO_TARIFFS[590][1], PROVIDER_PAYMENT_ID))
        await db.execute('UPDATE users SET balance=balance+1 WHERE user_id=?', (user_id,))
        await database.increment_purchases(user_id, connection=db)
        await database._record_sale_counters(db, 'photo', {'user_id': user_id, 'tariff': 590})
        await database.increment_counter('gift_payment_recovery', user_id=user_id, connection=db)
        await db.execute("""INSERT INTO payment_recoveries (
            provider_payment_id,order_number,user_id,photo_order_id,bonus_credits
        ) VALUES (?,?,?,?,1)""", (PROVIDER_PAYMENT_ID, RECOVERY_NUMBER, user_id, PHOTO_ORDER_ID))
        await db.commit()
    return {'provider_payment_id': PROVIDER_PAYMENT_ID, 'order_number': RECOVERY_NUMBER,
            'user_id': user_id, 'photo_order_id': PHOTO_ORDER_ID, 'bonus_credits': 1, 'notified': 0}


async def pending_recovery_notification(user_id):
    async with aiosqlite.connect(database.DATABASE_PATH, timeout=30) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute('SELECT * FROM payment_recoveries WHERE user_id=? AND notified=0',
                              (user_id,)) as cursor:
            row = await cursor.fetchone()
    return dict(row) if row else None


async def mark_recovery_notified(user_id):
    async with aiosqlite.connect(database.DATABASE_PATH, timeout=30) as db:
        await db.execute('UPDATE payment_recoveries SET notified=1 WHERE user_id=?', (user_id,))
        await db.commit()

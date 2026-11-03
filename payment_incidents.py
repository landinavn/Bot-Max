"""Durable diagnostics for signed successful payments rejected by validation.

No raw webhook, email, phone, signature or secret is persisted. An incident is
not permission to credit a user: validation stays strict and the owner reviews it.
"""
import asyncio
import hashlib
import json
import logging
import re
import time

import aiosqlite

import database

logger = logging.getLogger(__name__)

REASONS = {
    'Нет номера заказа или платежа.': 'missing_identifiers',
    'Заказ не найден или его тип неоднозначен.': 'unknown_or_ambiguous_order',
    'Валюта не соответствует заказу.': 'currency_mismatch',
    'Сумма не соответствует заказу.': 'amount_mismatch',
    'Платёжная форма не соответствует заказу.': 'domain_mismatch',
    'Некорректный состав заказа.': 'invalid_products',
    'Товар не соответствует заказу.': 'product_mismatch',
    'Пользователь не соответствует заказу.': 'user_mismatch',
    'Пакет не соответствует заказу.': 'credits_mismatch',
    'Идентификатор пакета не соответствует заказу.': 'package_mismatch',
    'Метаданные покупателя не соответствуют заказу.': 'customer_metadata_mismatch',
    'Метаданные пакета не соответствуют заказу.': 'package_metadata_mismatch',
    'Неизвестный тариф оживления фото.': 'animation_tariff_mismatch',
    'Неизвестный аудиотариф.': 'audio_tariff_mismatch',
    'Неизвестный фототариф.': 'photo_tariff_mismatch',
    'Неизвестный музыкальный тариф.': 'musical_tariff_mismatch',
    'Неизвестный мульт-тариф.': 'cartoon_tariff_mismatch',
    'Неизвестный пакет.': 'unknown_credit_package',
    'Некорректная скидка мульт-тарифа.': 'cartoon_discount_mismatch',
    'Платёж уже использован.': 'payment_already_used',
    'Платёж уже использован другим заказом.': 'payment_already_used',
    'Заказ уже оплачен другим платежом.': 'receipt_conflict',
}


def safe_identifier(value):
    return value if isinstance(value, str) and re.fullmatch(r'[A-Za-z0-9_-]{1,200}', value) else ''


def incident_fields(data, error):
    payment_id = safe_identifier(data.get('order_id'))
    merchant_id = safe_identifier(data.get('order_num'))
    amount = data.get('sum', data.get('amount', ''))
    amount = str(amount) if isinstance(amount, (str, int, float)) and not isinstance(amount, bool) else ''
    if not re.fullmatch(r'\d{1,12}(?:[.,]\d{1,2})?', amount):
        amount = ''
    if isinstance(error, json.JSONDecodeError):
        reason = 'invalid_customer_metadata_json'
    else:
        reason = REASONS.get(str(error), 'invalid_payment_data')
    identity = 'payment:' + payment_id if payment_id else json.dumps([merchant_id, amount, reason])
    key = hashlib.sha256(identity.encode()).hexdigest()
    return dict(incident_key=key, provider_payment_id=payment_id, merchant_order_id=merchant_id,
                amount_text=amount, reason_code=reason)


async def record_payment_incident(data, error):
    fields = incident_fields(data, error)
    async with aiosqlite.connect(database.DATABASE_PATH, timeout=30) as db:
        await db.execute("""INSERT INTO payment_incidents (
            incident_key,provider_payment_id,merchant_order_id,amount_text,reason_code
        ) VALUES (:incident_key,:provider_payment_id,:merchant_order_id,:amount_text,:reason_code)
        ON CONFLICT(incident_key) DO UPDATE SET
            occurrences=occurrences+1,last_seen=CURRENT_TIMESTAMP,
            reason_code=excluded.reason_code""", fields)
        await db.commit()
    return fields


async def resolve_payment_incident(payment_id):
    if not safe_identifier(payment_id):
        return
    async with aiosqlite.connect(database.DATABASE_PATH, timeout=30) as db:
        await db.execute('UPDATE payment_incidents SET resolved=1 WHERE provider_payment_id=?', (payment_id,))
        await db.commit()


class PaymentIncidentWorker:
    def __init__(self, bot, *, admin_user_id=None, poll_interval=5):
        self.bot = bot
        self.admin_user_id = admin_user_id
        self.poll_interval = poll_interval

    async def process(self):
        if self.admin_user_id is None:
            return
        async with aiosqlite.connect(database.DATABASE_PATH, timeout=30) as db:
            db.row_factory = aiosqlite.Row
            async with db.execute('''SELECT * FROM payment_incidents
                WHERE resolved=0 AND admin_notified=0 AND next_attempt_at<=?
                ORDER BY first_seen LIMIT 20''', (int(time.time()),)) as cursor:
                incidents = [dict(row) for row in await cursor.fetchall()]
        for row in incidents:
            try:
                sent = await self.bot.send_message(user_id=self.admin_user_id, text=(
                    '⚠️ Подписанное уведомление об успешной оплате не обработано ботом.\n'
                    f"Платёж Prodamus: {row['provider_payment_id'] or 'не указан'}\n"
                    f"Заказ бота: {row['merchant_order_id'] or 'не указан'}\n"
                    f"Сумма из уведомления: {row['amount_text'] or 'не распознана'} ₽\n"
                    f"Причина: {row['reason_code']}\n"
                    'По этому уведомлению начисление не выполнено. '
                    'Проверьте актуальный статус заказа и платёж в Prodamus перед восстановлением. '
                    'Повторно выставлять счёт клиенту не нужно.'
                ))
                if sent is None:
                    raise RuntimeError('MAX не подтвердил уведомление о проблеме оплаты.')
                async with aiosqlite.connect(database.DATABASE_PATH, timeout=30) as db:
                    await db.execute('UPDATE payment_incidents SET admin_notified=1 WHERE incident_key=?',
                                     (row['incident_key'],))
                    await db.commit()
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.exception('Не удалось уведомить о платёжном инциденте %s', row['incident_key'])
                async with aiosqlite.connect(database.DATABASE_PATH, timeout=30) as db:
                    await db.execute('UPDATE payment_incidents SET next_attempt_at=? WHERE incident_key=?',
                                     (int(time.time()) + 60, row['incident_key']))
                    await db.commit()

    async def run(self):
        if self.admin_user_id is None:
            logger.warning('ADMIN_USER_ID не задан: проблемы оплаты сохраняются только в payment_incidents и журнале.')
            return
        while True:
            try:
                await self.process()
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.exception('Ошибка обработки платёжных инцидентов')
            await asyncio.sleep(self.poll_interval)

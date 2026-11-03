from types import SimpleNamespace
from unittest.mock import AsyncMock

import aiosqlite
import pytest

import database
from payment_incidents import PaymentIncidentWorker, record_payment_incident, incident_fields
from prodamus import sign_payload
from webhook import create_webhook_app
from test_sales_stats import sales_payments, make_order, deliver_webhook


async def incidents():
    async with aiosqlite.connect(database.DATABASE_PATH) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute('SELECT * FROM payment_incidents') as cursor:
            return [dict(row) for row in await cursor.fetchall()]


async def legacy_receipt(service, *, signature=True, status='success', is_test='0'):
    import json
    data = {'order_id': 'verified-legacy-payment', 'order_num': '17-01032-83290',
            'payment_status': status, 'is_test': is_test, 'sum': '590.00',
            'products': [{'name': 'Фотосессия Попробовать ', 'price': '590.00', 'quantity': '1'}],
            'customer_email': 'private@example.test', 'customer_phone': '+79999999999'}
    app = create_webhook_app(service)
    handler = next(r.handler for r in app.router.routes() if r.method == 'POST')
    request = SimpleNamespace(content_type='application/json', text=AsyncMock(return_value=json.dumps(data)),
                              headers={'Sign': sign_payload(data, service.secret) if signature else 'bad-signature'})
    return await handler(request)


async def test_unknown_shared_receipt_is_durable_and_alerted_once(credit_db, sales_payments, caplog):
    for _ in range(3):
        assert (await legacy_receipt(sales_payments)).status == 400
    rows = await incidents()
    assert len(rows) == 1 and rows[0]['occurrences'] == 3
    assert rows[0]['reason_code'] == 'unknown_or_ambiguous_order'
    assert rows[0]['merchant_order_id'] == '17-01032-83290'
    assert not any('private@example.test' in str(r) or '+79999999999' in str(r) for r in rows)
    assert 'private@example.test' not in caplog.text and sales_payments.secret not in caplog.text
    assert 'payment_id=verified-legacy-payment' in caplog.text
    bot = SimpleNamespace(send_message=AsyncMock(return_value=object()))
    worker = PaymentIncidentWorker(bot, admin_user_id=999)
    await worker.process()
    await credit_db.init_db()
    assert (await legacy_receipt(sales_payments)).status == 400
    await worker.process()
    bot.send_message.assert_awaited_once()
    assert bot.send_message.await_args.kwargs['user_id'] == 999
    assert 'unknown_or_ambiguous_order' in bot.send_message.await_args.kwargs['text']
    assert (await incidents())[0]['occurrences'] == 4
    assert (await credit_db.get_global_stats())['total_purchases'] == 0


@pytest.mark.parametrize('kwargs,status', [
    ({'signature': False}, 403), ({'status': 'pending'}, 200), ({'is_test': '1'}, 200),
])
async def test_untrusted_test_or_unpaid_callbacks_do_not_create_incidents(credit_db, sales_payments, kwargs, status):
    assert (await legacy_receipt(sales_payments, **kwargs)).status == status
    assert await incidents() == []


async def test_valid_retry_resolves_incident_without_double_credit(credit_db, sales_payments):
    order = await make_order(credit_db, 'credit', 1)
    assert (await deliver_webhook(sales_payments, order, payment_id='receipt-retry', sum='1.00')).status == 400
    assert (await incidents())[0]['reason_code'] == 'amount_mismatch'
    for _ in range(2):
        assert (await deliver_webhook(sales_payments, order, payment_id='receipt-retry')).status == 200
    assert (await incidents())[0]['resolved'] == 1
    assert (await credit_db.get_user(101))['balance'] == 1
    assert (await credit_db.get_user(101))['total_purchases'] == 1
    bot = SimpleNamespace(send_message=AsyncMock())
    await PaymentIncidentWorker(bot, admin_user_id=999).process()
    bot.send_message.assert_not_awaited()


@pytest.mark.parametrize('failure', [None, RuntimeError('MAX down')])
async def test_admin_notification_retries_without_losing_incident(credit_db, sales_payments, monkeypatch, failure):
    await legacy_receipt(sales_payments)
    bot = SimpleNamespace(send_message=AsyncMock(return_value=None))
    if failure:
        bot.send_message.side_effect = failure
    worker = PaymentIncidentWorker(bot, admin_user_id=999)
    await worker.process()
    row = (await incidents())[0]
    assert not row['admin_notified'] and row['next_attempt_at'] > 0
    bot.send_message.side_effect = None
    bot.send_message.return_value = object()
    async with aiosqlite.connect(database.DATABASE_PATH) as db:
        await db.execute('UPDATE payment_incidents SET next_attempt_at=0')
        await db.commit()
    await worker.process()
    assert (await incidents())[0]['admin_notified'] == 1


async def test_unconfigured_admin_keeps_evidence_without_sending(credit_db, sales_payments):
    await legacy_receipt(sales_payments)
    bot = SimpleNamespace(send_message=AsyncMock())
    await PaymentIncidentWorker(bot).process()
    bot.send_message.assert_not_awaited()
    assert not (await incidents())[0]['admin_notified']


async def test_database_failure_requires_provider_retry(credit_db, sales_payments, monkeypatch):
    import webhook
    monkeypatch.setattr(webhook, 'record_payment_incident', AsyncMock(side_effect=aiosqlite.OperationalError('database locked')))
    assert (await legacy_receipt(sales_payments)).status == 503


def test_diagnostic_fields_do_not_echo_arbitrary_inputs():
    fields = incident_fields({'order_id': 'secret@example.test', 'order_num': '<script>',
                              'sum': 'secret@example.test'}, ValueError('sensitive arbitrary exception'))
    assert not fields['provider_payment_id'] and not fields['merchant_order_id'] and not fields['amount_text']
    assert fields['reason_code'] == 'invalid_payment_data'

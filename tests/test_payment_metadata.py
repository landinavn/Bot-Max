"""Signed optional provider text cannot break fully bound paid orders."""
import json
from html import escape
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from animation_orders import PaidAnimationWorker
from prodamus import sign_payload
from webhook import create_webhook_app
from test_sales_stats import sales_payments, make_order
from test_payment_incidents import incidents


def receipt(order, extra):
    return {'order_num': order['order_id'], 'order_id': 'metadata-provider-receipt',
            'payment_status': 'success', 'sum': f"{order['amount_kopecks']/100:.2f}", 'currency': 'rub',
            'products': [{'name': order['product_name'], 'price': f"{order['amount_kopecks']/100:.2f}", 'quantity': '1'}],
            '_param_user_id': str(order['user_id']), '_param_credits': str(order['credits']),
            '_param_package_id': order['package_id'], 'customer_extra': extra}


async def post(service, data, *, bad_signature=False):
    app = create_webhook_app(service)
    handler = next(r.handler for r in app.router.routes() if r.method == 'POST')
    request = SimpleNamespace(content_type='application/json', text=AsyncMock(return_value=json.dumps(data)),
                              headers={'Sign': 'bad' if bad_signature else sign_payload(data, service.secret)})
    return await handler(request)


@pytest.mark.parametrize('repeat', [False, True])
@pytest.mark.parametrize('extra', ['', 'ordinary provider text', '{invalid json', 'private@example.test +79999999999'])
async def test_animation_199_and_249_with_provider_text_are_paid_once(credit_db, sales_payments, caplog, repeat, extra):
    if repeat:
        prior = await credit_db.create_animation_order(456, 'one')
        await credit_db.mark_animation_paid(prior['order_id'], 'previous-199-receipt')
        await credit_db.update_animation_order(prior['order_id'], notified=1)
    order = await credit_db.create_animation_order(456, 'one')
    assert order['amount_kopecks'] == (24900 if repeat else 19900)
    data = receipt(order, extra)
    for _ in range(2):
        assert (await post(sales_payments, data)).status == 200
    saved = await credit_db.get_animation_order(order['order_id'])
    assert saved['status'] == 'paid' and saved['provider_payment_id'] == data['order_id']
    assert (await credit_db.get_user(456))['total_purchases'] == 1 + int(repeat)
    assert (await credit_db.get_all_counters())['buy_animation_' + order['tariff']] == 1
    assert await incidents() == []
    assert extra == '' or extra not in caplog.text
    assert sales_payments.secret not in caplog.text
    bot = SimpleNamespace(send_message=AsyncMock(return_value=object()))
    worker = PaidAnimationWorker(bot)
    notices, _ = await credit_db.animation_work_items()
    for key in notices:
        await worker.notify(key)
    assert (await credit_db.get_animation_order(order['order_id']))['notified'] == 1
    assert bot.send_message.await_args.kwargs['user_id'] == 456
    assert bot.send_message.await_args.kwargs['attachments'][0].payload.buttons[0][0].payload == 'animation_resume:' + order['order_id']
    assert order['order_id'] not in (await credit_db.animation_work_items())[0]


@pytest.mark.parametrize('missing', ['_param_user_id', '_param_credits', '_param_package_id'])
async def test_malformed_metadata_requires_all_binding_fields(credit_db, sales_payments, missing):
    order = await credit_db.create_animation_order(456, 'one')
    data = receipt(order, 'not-json')
    data.pop(missing)
    assert (await post(sales_payments, data)).status == 400
    assert (await credit_db.get_animation_order(order['order_id']))['status'] == 'pending'
    assert (await incidents())[0]['reason_code'] == 'invalid_customer_metadata_json'


@pytest.mark.parametrize('changes', [
    {'_param_user_id': '999'}, {'_param_credits': '99'}, {'_param_package_id': 'animation_five'},
    {'sum': '249.00'}, {'currency': 'usd'}, {'order_num': 'animate_unknown'},
    {'products': [{'name': 'wrong', 'price': '199.00', 'quantity': '1'}]},
])
async def test_fallback_never_bypasses_order_checks(credit_db, sales_payments, changes):
    order = await credit_db.create_animation_order(456, 'one')
    data = receipt(order, 'not-json')
    data.update(changes)
    assert (await post(sales_payments, data)).status == 400
    assert (await credit_db.get_animation_order(order['order_id']))['status'] == 'pending'


async def test_fallback_requires_valid_signature(credit_db, sales_payments):
    order = await credit_db.create_animation_order(456, 'one')
    assert (await post(sales_payments, receipt(order, ''), bad_signature=True)).status == 403
    assert (await credit_db.get_animation_order(order['order_id']))['status'] == 'pending'
    assert await incidents() == []


@pytest.mark.parametrize('extra', [
    {'user_id': 999, 'credits': 0}, {'user_id': 456, 'credits': 1},
    {'user_id': 456, 'credits': 0, 'package_id': 'animation_five'}, [], None, 'note',
])
@pytest.mark.parametrize('html_encoded', [False, True])
async def test_readable_conflicting_metadata_is_never_ignored(credit_db, sales_payments, extra, html_encoded):
    order = await credit_db.create_animation_order(456, 'one')
    encoded = json.dumps(extra)
    if html_encoded:
        encoded = escape(encoded, quote=True)
    assert (await post(sales_payments, receipt(order, encoded))).status == 400
    assert (await credit_db.get_animation_order(order['order_id']))['status'] == 'pending'


@pytest.mark.parametrize('representation', ['json', 'html', 'object'])
async def test_valid_metadata_formats_match_saved_order(credit_db, sales_payments, representation):
    order = await credit_db.create_animation_order(456, 'one')
    extra = {'user_id': 456, 'credits': 0, 'package_id': order['package_id']}
    if representation != 'object':
        extra = json.dumps(extra)
    if representation == 'html':
        extra = escape(extra, quote=True)
    data = receipt(order, extra)
    data.pop('_param_user_id')  # Correct readable metadata still works for older receipts.
    assert (await post(sales_payments, data)).status == 200


@pytest.mark.parametrize('kind,tariff', [('credit', 3), ('audio', 'fun'), ('musical', 'direct'),
                                         ('cartoon', 'short'), ('photo', 590)])
async def test_fully_bound_provider_text_works_for_other_products(credit_db, sales_payments, kind, tariff):
    order = await make_order(credit_db, kind, tariff)
    data = receipt(order, 'provider note')
    for _ in range(2):
        assert (await post(sales_payments, data)).status == 200
    user = await credit_db.get_user(order['user_id'])
    assert user['total_purchases'] == 1
    assert user['balance'] == (3 if kind == 'credit' else 0)


async def test_correct_retry_resolves_original_incident(credit_db, sales_payments):
    order = await credit_db.create_animation_order(456, 'one')
    data = receipt(order, '')
    incomplete = dict(data)
    incomplete.pop('_param_package_id')
    assert (await post(sales_payments, incomplete)).status == 400
    assert (await incidents())[0]['resolved'] == 0
    assert (await post(sales_payments, data)).status == 200
    assert (await incidents())[0]['resolved'] == 1

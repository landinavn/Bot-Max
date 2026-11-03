"""Fresh Prodamus links for published photoshoots without replacing the order."""
from unittest.mock import AsyncMock

import pytest

from handlers import create_router
from prodamus import sign_link_payload
from test_handlers import (
    callback_event, ready_context, dispatch_callback, event_replies,
    max_image_upload, isolated_example_directory, example_photos, text_correction,
)
from test_sales_stats import sales_payments, deliver_webhook


@pytest.mark.parametrize('tariff', [590, 990])
@pytest.mark.parametrize('promo', [False, True])
async def test_expired_photo_link_is_replaced_on_each_checkout(credit_db, sales_payments, tariff, promo):
    if promo:
        await credit_db.set_promo(456, 'photo')
    key = await credit_db.create_photo_order(f'photo_refresh_{tariff}', 456, tariff)
    order = await credit_db.get_photo_order(key)
    old = 'https://link.payform.ru/?paymentLinkId=expired-photo'
    await credit_db.set_photo_payment_link(key, old)
    links = [
        'https://link.payform.ru/?paymentLinkId=fresh-photo-1',
        'https://link.payform.ru/?paymentLinkId=fresh-photo-2',
    ]
    sales_payments._request_payment_link = AsyncMock(side_effect=links)
    kwargs = dict(order_id=key, amount=order['amount_kopecks'] // 100, description=order['product_name'])
    assert await sales_payments.create_payment_link(456, **kwargs) == links[0]
    assert await sales_payments.create_payment_link(456, **kwargs) == links[1]
    assert sales_payments._request_payment_link.await_count == 2
    saved = await credit_db.get_photo_order(key)
    assert saved['payment_link'] == links[1]
    assert saved['order_id'] == key and saved['status'] == 'pending'
    assert saved['amount_kopecks'] == (tariff * 80 if promo else tariff * 100)
    assert (await credit_db.get_all_counters()).get(f'buy_photo_{tariff}', 0) == 0
    for request in sales_payments._request_payment_link.await_args_list:
        data = request.args[0]
        assert data['order_id'] == key and data['_param_user_id'] == '456'
        assert data['_param_package_id'] == f'photo_{tariff}' and data['_param_credits'] == '0'
        assert data['products'] == [{
            'name': order['product_name'], 'price': f"{kwargs['amount']}.00", 'quantity': '1',
        }]
        assert data['payments_limit'] == '1'
        assert sign_link_payload({k: v for k, v in data.items() if k != 'sign'}, sales_payments.secret) == data['sign']
    # Refreshing the URL does not change webhook attribution or duplicate the sale.
    assert (await deliver_webhook(sales_payments, saved, payment_id='photo-refresh-paid')).status == 200
    assert (await deliver_webhook(sales_payments, saved, payment_id='photo-refresh-paid')).status == 200
    assert (await credit_db.get_all_counters())[f'buy_photo_{tariff}'] == 1
    assert (await credit_db.get_photo_order(key))['status'] == 'paid'


@pytest.mark.parametrize('tariff', [590, 990])
async def test_provider_error_never_returns_the_old_photo_link(credit_db, sales_payments, tariff):
    key = await credit_db.create_photo_order(f'photo_refresh_error_{tariff}', 456, tariff)
    order = await credit_db.get_photo_order(key)
    old = 'https://link.payform.ru/?paymentLinkId=expired-photo'
    await credit_db.set_photo_payment_link(key, old)
    sales_payments._request_payment_link = AsyncMock(side_effect=RuntimeError('provider unavailable'))
    with pytest.raises(RuntimeError, match='provider unavailable'):
        await sales_payments.create_payment_link(
            456, order_id=key, amount=tariff, description=order['product_name'],
        )
    sales_payments._request_payment_link.assert_awaited_once()
    saved = await credit_db.get_photo_order(key)
    assert saved['payment_link'] == old and saved['status'] == 'pending'
    assert (await credit_db.get_all_counters()).get(f'buy_photo_{tariff}', 0) == 0


@pytest.mark.parametrize('tariff', [590, 990])
@pytest.mark.parametrize('invalid', ['foreign_user', 'wrong_amount', 'paid'])
async def test_photo_link_refresh_still_checks_owner_price_and_payment(credit_db, sales_payments, tariff, invalid):
    key = await credit_db.create_photo_order(f'photo_refresh_guard_{tariff}', 456, tariff)
    order = await credit_db.get_photo_order(key)
    await credit_db.set_photo_payment_link(key, 'https://link.payform.ru/?paymentLinkId=old-photo')
    if invalid == 'paid':
        await credit_db.mark_photo_paid(key, 'already-paid')
    with pytest.raises(ValueError):
        await sales_payments.create_payment_link(
            101 if invalid == 'foreign_user' else 456,
            order_id=key, amount=tariff + 1 if invalid == 'wrong_amount' else tariff,
            description=order['product_name'],
        )
    sales_payments._request_payment_link.assert_not_awaited()


@pytest.mark.parametrize('tariff,payload', [
    (590, 'photo_temp_fam3'),
    (590, 'photo_temp_fam4'),
    (590, 'photo_temp_toddlers'),
    (590, 'photo_temp_mom_daughter'),
    (590, 'photo_temp_rabbit'),
    (990, 'photo_temp_mom_daughter_5'),
    (990, 'photo_temp_woman_red'),
])
@pytest.mark.parametrize('promo', [False, True])
async def test_reselecting_plot_shows_fresh_link_for_the_same_order(
    credit_db, sales_payments, example_photos, tariff, payload, promo,
):
    if promo:
        await credit_db.set_promo(456, 'photo')
    links = [
        'https://link.payform.ru/?paymentLinkId=selected-photo-1',
        'https://link.payform.ru/?paymentLinkId=selected-photo-2',
    ]
    sales_payments._request_payment_link = AsyncMock(side_effect=links)
    router = create_router(admin_user_id=999, payments=sales_payments)
    context = await ready_context()
    await dispatch_callback(router, callback_event('photo_style_ny'), context)
    await dispatch_callback(router, callback_event(f'photo_tariff_{tariff}'), context)
    sales_payments._request_payment_link.assert_not_awaited()
    first = callback_event(payload)
    await dispatch_callback(router, first, context)
    key = (await context.get_data())['photo_order_id']
    saved = await credit_db.get_photo_order(key)
    second = callback_event(payload)
    await dispatch_callback(router, second, context)
    assert (await context.get_data())['photo_order_id'] == key
    assert (await credit_db.get_photo_order(key))['template'] == saved['template']
    assert first.send.await_args.kwargs['attachments'][0].payload.buttons[0][0].url == links[0]
    assert second.send.await_args.kwargs['attachments'][0].payload.buttons[0][0].url == links[1]
    assert sales_payments._request_payment_link.await_count == 2
    assert (await credit_db.get_all_counters()).get(f'buy_photo_{tariff}', 0) == 0

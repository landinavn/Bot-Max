"""The selected plot must be previewed before any personal invoice is created."""
from unittest.mock import AsyncMock

import pytest
import handlers
from handlers import create_router, PosterStates
from test_handlers import (callback_event, ready_context, dispatch_callback, event_replies,
                           max_image_upload, isolated_example_directory, example_photos, text_correction)
from test_sales_stats import sales_payments

PLOTS = [(payload, key, 990 if payload in handlers.PHOTO_NY_STANDARD_TEMPLATES else 590)
         for payload, (_, key) in handlers.PHOTO_NY_ALL_TEMPLATES.items()]


@pytest.mark.parametrize('payload,template,tariff', PLOTS)
@pytest.mark.parametrize('promo', [False, True])
async def test_every_plot_preview_precedes_invoice(credit_db, sales_payments, example_photos, payload, template, tariff, promo):
    if promo:
        await credit_db.set_promo(456, 'photo')
    router = create_router(admin_user_id=999, payments=sales_payments)
    context = await ready_context()
    if promo:
        await dispatch_callback(router, callback_event('promo_photo'), context)
    await dispatch_callback(router, callback_event('photo_style_ny'), context)
    await dispatch_callback(router, callback_event(f'photo_tariff_{tariff}'), context)
    assert 'photo_order_id' not in await context.get_data()
    sales_payments._request_payment_link.assert_not_awaited()
    assert await credit_db.get_promo_reservation(456, 'photo') is None
    event = callback_event(payload)
    chronology = []
    async def send(*args, **kwargs):
        if kwargs.get('text') == 'Примеры работ:':
            chronology.append('preview_sent')
    event.send.side_effect = send
    original = sales_payments._request_payment_link.side_effect
    async def link(data):
        chronology.append('invoice')
        return await original(data)
    sales_payments._request_payment_link.side_effect = link
    await dispatch_callback(router, event, context)
    assert chronology == ['preview_sent', 'invoice']
    assert event.bot.upload_media.await_args.args[0].path.endswith(handlers.PHOTO_TEMPLATE_EXAMPLE_FILES[template])
    order = await credit_db.get_photo_order((await context.get_data())['photo_order_id'])
    assert order['template'] == template and order['tariff'] == tariff
    expected = tariff * 80 // 100 if promo else tariff
    assert order['amount_kopecks'] == expected * 100
    assert sales_payments._request_payment_link.await_args.args[0]['products'][0]['price'] == f'{expected}.00'
    sales_payments._request_payment_link.assert_awaited_once()


@pytest.mark.parametrize('failure', ['missing','upload','send'])
async def test_missing_or_failed_preview_cannot_create_invoice(credit_db, sales_payments, example_photos, failure):
    await credit_db.set_promo(456, 'photo')
    router = create_router(admin_user_id=999, payments=sales_payments)
    context = await ready_context()
    await dispatch_callback(router, callback_event('photo_style_ny'), context)
    await dispatch_callback(router, callback_event('photo_tariff_990'), context)
    event = callback_event('photo_temp_woman_red')
    if failure == 'missing':
        (example_photos / handlers.WOMAN_RED_EXAMPLE_FILE).unlink()
    elif failure == 'upload':
        event.bot.upload_media.side_effect = RuntimeError('upload failed')
    else:
        event.send.side_effect = [RuntimeError('send failed'), None]
    await dispatch_callback(router, event, context)
    sales_payments._request_payment_link.assert_not_awaited()
    assert 'photo_order_id' not in await context.get_data()
    assert await credit_db.get_promo_reservation(456, 'photo') is None
    assert 'Счёт не выставлен' in event.send.await_args.args[0]


async def test_changing_plot_does_not_reassign_previously_issued_invoice(credit_db, sales_payments, example_photos):
    router = create_router(admin_user_id=999, payments=sales_payments)
    context = await ready_context()
    await dispatch_callback(router, callback_event('photo_style_ny'), context)
    await dispatch_callback(router, callback_event('photo_tariff_990'), context)
    await dispatch_callback(router, callback_event('photo_temp_woman_red'), context)
    first = (await context.get_data())['photo_order_id']
    await dispatch_callback(router, callback_event('photo_temp_mom_daughter_5'), context)
    second = (await context.get_data())['photo_order_id']
    assert first != second
    assert (await credit_db.get_photo_order(first))['template'] == 'woman_red'
    assert (await credit_db.get_photo_order(second))['template'] == 'mom_daughter_5'
    await dispatch_callback(router, callback_event('photo_temp_mom_daughter_5'), context)
    assert (await context.get_data())['photo_order_id'] == second


async def test_reserved_discount_still_requires_plot_and_preview(credit_db, sales_payments, example_photos):
    await credit_db.set_promo(456, 'photo')
    router = create_router(admin_user_id=999, payments=sales_payments)
    context = await ready_context()
    await dispatch_callback(router, callback_event('photo_style_ny'), context)
    await dispatch_callback(router, callback_event('photo_tariff_990'), context)
    await dispatch_callback(router, callback_event('photo_temp_woman_red'), context)
    first = (await context.get_data())['photo_order_id']
    sales_payments._request_payment_link.reset_mock()
    await context.clear()
    await dispatch_callback(router, callback_event('promo_photo'), context)
    await dispatch_callback(router, callback_event('photo_style_ny'), context)
    await dispatch_callback(router, callback_event('photo_tariff_990'), context)
    sales_payments._request_payment_link.assert_not_awaited()
    await dispatch_callback(router, callback_event('photo_temp_mom_daughter_5'), context)
    sales_payments._request_payment_link.assert_not_awaited()
    assert (await credit_db.get_photo_order(first))['template'] == 'woman_red'
    restored = callback_event('photo_temp_woman_red')
    await dispatch_callback(router, restored, context)
    assert restored.send.await_args.kwargs['attachments'][0].payload.buttons[0][0].text == 'Оплатить 792 ₽'
    assert (await context.get_data())['photo_order_id'] == first

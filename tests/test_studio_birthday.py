"""Retired plot cannot be sold; historical paid orders can still be completed."""
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

import handlers
from photo_orders import PaidPhotoWorker
from test_handlers import (callback_event, message_event, photo_event, ready_context, dispatch_callback,
                           call_handler, event_replies, max_image_upload, text_correction)
from test_sales_stats import sales_payments, deliver_webhook


async def birthday_context():
    context = await ready_context()
    await context.update_data(photo_style='photo_style_studio', photo_tariff='photo_tariff_590',
                              tariff='trial_590', template='birthday_girl', birthday_number='11',
                              photo_preview_template='birthday_girl')
    return context


async def old_order(db, payments, *, paid=True, number='11'):
    key = await db.create_photo_order('photo_456_old_birthday', 456, 590)
    await db.update_photo_order(key, style='photo_style_studio', template='birthday_girl', birthday_number=number)
    if paid:
        assert (await deliver_webhook(payments, await db.get_photo_order(key))).status == 200
    return key


@pytest.mark.parametrize('admin', [999, 456])
async def test_studio_tariff_no_longer_offers_birthday(credit_db, sales_payments, admin):
    router = handlers.create_router(payments=sales_payments, admin_user_id=admin)
    context = await ready_context()
    await dispatch_callback(router, callback_event('photo_style_studio'), context)
    event = callback_event('photo_tariff_590')
    await dispatch_callback(router, event, context)
    event.ack.assert_awaited_once_with(notification='Скоро будет доступно…')
    assert 'birthday_girl' not in str(event.send.await_args_list)
    assert 'День рождения девочка' not in str(event.send.await_args_list)
    assert 'photo_order_id' not in await context.get_data()
    sales_payments._request_payment_link.assert_not_awaited()


@pytest.mark.parametrize('admin', [999, 456])
@pytest.mark.parametrize('payload', ['photo_temp_birthday_girl', 'photo_choose_template'])
async def test_old_buttons_do_not_restart_retired_plot(credit_db, sales_payments, admin, payload):
    router = handlers.create_router(payments=sales_payments, admin_user_id=admin)
    context = await birthday_context()
    event = callback_event(payload)
    await dispatch_callback(router, event, context)
    assert 'удалена из каталога' in event.send.await_args.args[0]
    assert await context.get_state() != handlers.PosterStates.waiting_for_birthday_number
    sales_payments._request_payment_link.assert_not_awaited()
    assert (await credit_db.get_all_counters()).get('select_photo_birthday_girl', 0) == 0


@pytest.mark.parametrize('pending', [False, True])
async def test_old_number_state_cannot_create_invoice(credit_db, sales_payments, pending):
    router = handlers.create_router(payments=sales_payments, admin_user_id=999)
    context = await birthday_context()
    if pending:
        await context.update_data(photo_order_id=await old_order(credit_db, sales_payments, paid=False))
    await context.set_state(handlers.PosterStates.waiting_for_birthday_number)
    event = message_event('12')
    await call_handler(router, 'collect_birthday_number', event, context)
    assert 'удалена из каталога' in event.message.answer.await_args.args[0]
    assert await context.get_state() == handlers.PosterStates.waiting_for_mode
    sales_payments._request_payment_link.assert_not_awaited()


@pytest.mark.parametrize('admin', [999, 456])
async def test_old_photo_state_cannot_generate_or_bill(credit_db, sales_payments, admin):
    frames = AsyncMock()
    router = handlers.create_router(payments=sales_payments, admin_user_id=admin, photosession_generator=frames)
    context = await birthday_context()
    await context.update_data(photosession=True, photosession_admin_bypass=True, photosession_session_id='old-session')
    await context.set_state(handlers.PosterStates.waiting_for_photosession_photo)
    event = photo_event()
    await call_handler(router, 'collect_photosession_photo', event, context)
    assert 'удалена из каталога' in event.message.answer.await_args.args[0]
    frames.assert_not_awaited()
    sales_payments._request_payment_link.assert_not_awaited()


@pytest.mark.parametrize('legacy', [False, True])
async def test_existing_paid_package_resumes_saved_frames(credit_db, sales_payments, legacy):
    key = await old_order(credit_db, sales_payments)
    await credit_db.update_photo_order(key, reference_url='https://photos.example.com/source.png')
    await credit_db.save_photo_result(key, 1, 'https://cdn.example/first.png')
    frames = AsyncMock(side_effect=['https://cdn.example/second.png', 'https://cdn.example/third.png'])
    router = handlers.create_router(payments=sales_payments, admin_user_id=999, photosession_generator=frames)
    context = await birthday_context() if legacy else await ready_context()
    if legacy:
        await context.update_data(photo_order_id=key)
    await credit_db.init_db()
    await dispatch_callback(router, callback_event('photo_temp_birthday_girl' if legacy else 'photo_resume:' + key), context)
    assert frames.await_count == 2
    assert '"11"' in frames.await_args_list[0].args[1]
    assert (await context.get_data())['photosession_complete'] is True
    assert await credit_db.get_photo_package_balance(key) == 0
    sales_payments._request_payment_link.assert_not_awaited()


async def test_paid_missing_number_is_collected_without_invoice(credit_db, sales_payments):
    key = await old_order(credit_db, sales_payments, number=None)
    router = handlers.create_router(payments=sales_payments, admin_user_id=999)
    context = await ready_context()
    await dispatch_callback(router, callback_event('photo_resume:' + key), context)
    assert await context.get_state() == handlers.PosterStates.waiting_for_birthday_number
    await call_handler(router, 'collect_birthday_number', message_event('7'), context)
    assert (await credit_db.get_photo_order(key))['birthday_number'] == '7'
    assert await context.get_state() == handlers.PosterStates.waiting_for_photosession_photo
    sales_payments._request_payment_link.assert_not_awaited()


async def test_paid_worker_and_stats_keep_historical_order(credit_db, sales_payments):
    key = await old_order(credit_db, sales_payments)
    bot = SimpleNamespace(send_message=AsyncMock(return_value=object()))
    await PaidPhotoWorker(bot, admin_user_id=999).process()
    buttons = bot.send_message.await_args_list[0].kwargs['attachments'][0].payload.buttons
    assert buttons[0][0].payload == 'photo_resume:' + key
    stats = await credit_db.get_photo_template_stats('birthday_girl', admin_id=999)
    assert stats['paid_packages'] == 1 and stats['revenue_kopecks'] == 59000


async def test_pending_order_cannot_resume(credit_db, sales_payments):
    key = await old_order(credit_db, sales_payments, paid=False)
    frames = AsyncMock()
    router = handlers.create_router(payments=sales_payments, admin_user_id=999, photosession_generator=frames)
    event = callback_event('photo_resume:' + key)
    await dispatch_callback(router, event, await ready_context())
    assert 'недоступен' in event.send.await_args.args[0]
    frames.assert_not_awaited()
    sales_payments._request_payment_link.assert_not_awaited()

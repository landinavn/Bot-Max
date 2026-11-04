from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

import handlers
import studio_photosessions as studio
from photo_orders import PaidPhotoWorker
from test_handlers import (callback_event, message_event, photo_event, ready_context, dispatch_callback,
                           call_handler, event_replies, example_photos, isolated_example_directory,
                           max_image_upload, text_correction)
from test_sales_stats import sales_payments, deliver_webhook


async def choose_birthday(router, context):
    await dispatch_callback(router, callback_event('photo_style_studio'), context)
    await dispatch_callback(router, callback_event('photo_tariff_590'), context)
    event = callback_event(studio.STUDIO_BIRTHDAY_CALLBACK)
    await dispatch_callback(router, event, context)
    return event


async def test_preview_and_number_are_required_before_invoice_or_photo(credit_db, sales_payments, example_photos):
    frames = AsyncMock()
    router = handlers.create_router(payments=sales_payments, admin_user_id=999, photosession_generator=frames)
    context = await ready_context()
    choice = await choose_birthday(router, context)
    assert await context.get_state() == handlers.PosterStates.waiting_for_birthday_number
    assert studio.BIRTHDAY_NUMBER_QUESTION in [c.args[0] if c.args else c.kwargs.get('text')
                                             for c in choice.send.await_args_list]
    assert (await context.get_data())['photo'] is None
    sales_payments._request_payment_link.assert_not_awaited()
    frames.assert_not_awaited()
    await call_handler(router, 'collect_birthday_number', message_event('8'), context)
    data = await context.get_data()
    order = await credit_db.get_photo_order(data['photo_order_id'])
    assert order['style'] == 'photo_style_studio' and order['template'] == 'birthday_girl'
    assert order['birthday_number'] == '8' and order['amount_kopecks'] == 59000
    assert order['status'] == 'pending' and data.get('photo') is None
    assert await context.get_state() == handlers.PosterStates.waiting_for_mode
    sales_payments._request_payment_link.assert_awaited_once()
    invoice = sales_payments._request_payment_link.await_args.args[0]
    assert invoice['order_id'] == order['order_id'] and invoice['_param_user_id'] == '456'
    assert invoice['products'][0]['price'] == '590.00'
    frames.assert_not_awaited()


@pytest.mark.parametrize('number', ['13','7','0','99','07'])
async def test_paid_resume_rehydrates_number_and_delivers_exact_three_slots(credit_db, sales_payments, example_photos, number):
    frames = AsyncMock(side_effect=[f'https://cdn.example/birthday-{i}.png' for i in range(3)])
    router = handlers.create_router(payments=sales_payments, admin_user_id=999, photosession_generator=frames)
    context = await ready_context()
    await choose_birthday(router, context)
    await call_handler(router,'collect_birthday_number',message_event(number),context)
    key = (await context.get_data())['photo_order_id']
    order = await credit_db.get_photo_order(key)
    for _ in range(2):assert (await deliver_webhook(sales_payments,order)).status == 200
    bot = SimpleNamespace(send_message=AsyncMock(return_value=object()))
    await PaidPhotoWorker(bot,admin_user_id=999).process()
    buttons = bot.send_message.await_args_list[0].kwargs['attachments'][0].payload.buttons
    assert buttons[0][0].payload == 'photo_resume:' + key
    await context.clear()
    await credit_db.init_db()
    await dispatch_callback(router,callback_event('photo_resume:'+key),context)
    assert (await context.get_data())['birthday_number'] == number
    assert await context.get_state() == handlers.PosterStates.waiting_for_photosession_photo
    await call_handler(router,'collect_photosession_photo',photo_event(),context)
    assert [c.args[1] for c in frames.await_args_list] == list(studio.birthday_prompts(number))
    assert all(c.args[0]=='https://photos.example.com/source.png' for c in frames.await_args_list)
    assert all(c.kwargs=={} for c in frames.await_args_list)  # Each frame has a different outfit/background.
    assert await credit_db.get_photo_package_balance(key) == 0
    assert await credit_db.get_photo_template_stats('birthday_girl',admin_id=999) == {
        'paid_packages':1,'revenue_kopecks':59000,'generated_photos':3,'completed_packages':1}
    assert (await credit_db.get_all_counters())['select_photo_birthday_girl'] == 1
    assert (await credit_db.get_user(456))['balance'] == 0
    sales_payments._request_payment_link.assert_awaited_once()
    await context.clear()
    await dispatch_callback(router,callback_event('photo_resume:'+key),context)
    assert frames.await_count == 3


@pytest.mark.parametrize('value',['','13 лет','100','-1','1.5','١٣','<script>13</script>'])
async def test_invalid_number_does_not_create_invoice(credit_db,sales_payments,example_photos,value):
    router=handlers.create_router(payments=sales_payments,admin_user_id=999)
    context=await ready_context()
    await choose_birthday(router,context)
    await call_handler(router,'collect_birthday_number',message_event(value),context)
    assert await context.get_state() == handlers.PosterStates.waiting_for_birthday_number
    sales_payments._request_payment_link.assert_not_awaited()


async def test_missing_preview_never_sells_package(credit_db,sales_payments,isolated_example_directory):
    router=handlers.create_router(payments=sales_payments,admin_user_id=999)
    context=await ready_context()
    await choose_birthday(router,context)
    assert await context.get_state() != handlers.PosterStates.waiting_for_birthday_number
    sales_payments._request_payment_link.assert_not_awaited()


async def test_admin_uses_same_number_and_three_frames_without_payment(credit_db,example_photos):
    frames=AsyncMock(side_effect=[f'https://cdn.example/a{i}.png' for i in range(3)])
    pay=SimpleNamespace(create_payment_link=AsyncMock())
    router=handlers.create_router(admin_user_id=456,payments=pay,photosession_generator=frames)
    context=await ready_context()
    await choose_birthday(router,context)
    await call_handler(router,'collect_birthday_number',message_event('12'),context)
    assert await context.get_state() == handlers.PosterStates.waiting_for_photosession_photo
    await call_handler(router,'collect_photosession_photo',photo_event(),context)
    assert frames.await_count == 3
    assert '"12"' in frames.await_args_list[1].args[1]
    pay.create_payment_link.assert_not_awaited()


async def test_unpaid_photo_does_not_generate(credit_db,sales_payments,example_photos):
    frames=AsyncMock()
    router=handlers.create_router(payments=sales_payments,admin_user_id=999,photosession_generator=frames)
    context=await ready_context()
    await choose_birthday(router,context)
    await call_handler(router,'collect_birthday_number',message_event('10'),context)
    await call_handler(router,'collect_photosession_photo',photo_event(),context)
    frames.assert_not_awaited()


async def test_saved_partial_result_survives_resume_and_keeps_number(credit_db,sales_payments,example_photos):
    frames=AsyncMock(side_effect=['https://cdn.example/frame-1.png',RuntimeError('temporary failure')])
    router=handlers.create_router(payments=sales_payments,admin_user_id=999,photosession_generator=frames)
    context=await ready_context()
    await choose_birthday(router,context)
    await call_handler(router,'collect_birthday_number',message_event('11'),context)
    key=(await context.get_data())['photo_order_id']
    assert (await deliver_webhook(sales_payments,await credit_db.get_photo_order(key))).status==200
    await dispatch_callback(router,callback_event('photo_resume:'+key),context)
    await call_handler(router,'collect_photosession_photo',photo_event(),context)
    assert len(await credit_db.get_photo_results(key))==1
    await context.clear()
    frames.side_effect=['https://cdn.example/frame-2.png','https://cdn.example/frame-3.png']
    await dispatch_callback(router,callback_event('photo_resume:'+key),context)
    assert frames.await_count==4  # successful first frame was not regenerated
    assert '"11"' in frames.await_args_list[-2].args[1]
    assert await credit_db.get_photo_package_balance(key)==0


def test_prompts_preserve_customer_identity_and_reference_scenes():
    prompts=studio.birthday_prompts('9')
    assert len(prompts)==3
    assert 'BLACK AND WHITE' in prompts[0] and 'BLACK AND WHITE' in prompts[2]
    assert 'FRAME 2 — COLOR' in prompts[1] and '"9"' in prompts[1]
    assert '"13"' not in prompts[1] and '{birthday_number}' not in prompts[1]
    for prompt in prompts:
        assert 'actual age' in prompt and 'body proportions' in prompt
        assert 'do not lengthen short hair' in prompt and 'preserve that kind of hairstyle' in prompt
    assert 'black leather jacket' in prompts[1]
    assert 'grey jeans' in prompts[2] and 'cast shadow' in prompts[2]


async def test_stats_show_birthday_section(credit_db):
    event=message_event('/stats')
    await call_handler(handlers.create_router(admin_user_id=456),'stats',event)
    assert 'День рождения девочка — 590 ₽ / 3 фото' in event.message.answer.await_args.args[0]


async def test_paid_missing_number_is_collected_without_another_invoice(credit_db,sales_payments,example_photos):
    router=handlers.create_router(payments=sales_payments,admin_user_id=999)
    context=await ready_context()
    await choose_birthday(router,context)
    await call_handler(router,'collect_birthday_number',message_event('6'),context)
    key=(await context.get_data())['photo_order_id']
    assert (await deliver_webhook(sales_payments,await credit_db.get_photo_order(key))).status==200
    await credit_db.update_photo_order(key,birthday_number=None)
    await context.clear()
    await dispatch_callback(router,callback_event('photo_resume:'+key),context)
    assert await context.get_state()==handlers.PosterStates.waiting_for_birthday_number
    await call_handler(router,'collect_birthday_number',message_event('6'),context)
    assert (await credit_db.get_photo_order(key))['birthday_number']=='6'
    assert await context.get_state()==handlers.PosterStates.waiting_for_photosession_photo
    sales_payments._request_payment_link.assert_awaited_once()


async def test_partial_package_cannot_change_candle_number(credit_db,sales_payments,example_photos):
    router=handlers.create_router(payments=sales_payments,admin_user_id=999)
    context=await ready_context()
    await choose_birthday(router,context)
    await call_handler(router,'collect_birthday_number',message_event('9'),context)
    key=(await context.get_data())['photo_order_id']
    assert (await deliver_webhook(sales_payments,await credit_db.get_photo_order(key))).status==200
    await credit_db.save_photo_result(key,1,'https://cdn.example/frame.png')
    await context.set_state(handlers.PosterStates.waiting_for_birthday_number)
    event=message_event('10')
    await call_handler(router,'collect_birthday_number',event,context)
    assert (await credit_db.get_photo_order(key))['birthday_number']=='9'
    assert 'другим числом' in event.message.answer.await_args.args[0]

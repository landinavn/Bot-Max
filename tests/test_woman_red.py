"""Paid five-frame solo session: ownership, continuation, styling and fulfillment."""
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
import image_service as images
from handlers import create_router, PosterStates
from photo_orders import PaidPhotoWorker
from test_handlers import (callback_event, ready_context, dispatch_callback, call_handler,
                           photo_event, message_event, event_replies, max_image_upload,
                           isolated_example_directory, text_correction)
from test_sales_stats import sales_payments, deliver_webhook
from test_image_service import (config, storage, api, PHOTO_URL, IMAGE_EDIT_URL, PNG,
                               ENCODED_PNG, PUBLIC_URL, post_requests, multipart_fields)


async def choose_red(router, context):
    await dispatch_callback(router, callback_event('photo_style_ny'), context)
    await dispatch_callback(router, callback_event('photo_tariff_990'), context)
    event = callback_event('photo_temp_woman_red')
    await dispatch_callback(router, event, context)
    return event


async def test_admin_gets_solo_photo_request_and_five_portraits_without_payment(credit_db):
    frames = AsyncMock(side_effect=[f'https://cdn.example/red-{i}.jpg' for i in range(1, 6)])
    payments = SimpleNamespace(create_payment_link=AsyncMock())
    router = create_router(admin_user_id=456, photosession_generator=frames, payments=payments)
    context = await ready_context()
    choice = await choose_red(router, context)
    assert 'одно фото женщины' in choice.send.await_args.args[0]
    assert 'Оплата пропущена' in choice.send.await_args.args[0]
    await call_handler(router, 'collect_photosession_photo', photo_event(), context)
    assert len(frames.await_args_list) == 5
    assert [c.args[1] for c in frames.await_args_list] == list(images.NY_RED_WOMAN_PROMPTS)
    assert all(c.args[0] == 'https://photos.example.com/source.png' for c in frames.await_args_list)
    expected = [
        {}, {'style_reference_url': 'https://cdn.example/red-1.jpg'}, {},
        {'style_reference_url': 'https://cdn.example/red-3.jpg', 'style_reference_scope': 'wardrobe'},
        {'style_reference_url': 'https://cdn.example/red-3.jpg', 'style_reference_scope': 'setting'},
    ]
    for index, kwargs in enumerate(expected):
        kwargs['pose_instruction'] = images.NY_RED_WOMAN_HEAD_POSES[index]
    assert [c.kwargs for c in frames.await_args_list] == expected
    payments.create_payment_link.assert_not_awaited()
    assert await credit_db.get_active_promo(456, 'photo') is None


@pytest.mark.parametrize('promo,amount', [(False, 99000), (True, 79200)])
async def test_paid_red_webhook_notification_resume_and_five_slot_delivery(credit_db, sales_payments, promo, amount):
    if promo:
        await credit_db.set_promo(456, 'photo')
    frames = AsyncMock(side_effect=[f'https://cdn.example/red-{i}.jpg' for i in range(1, 6)])
    router = create_router(admin_user_id=999, photosession_generator=frames, payments=sales_payments)
    context = await ready_context()
    await choose_red(router, context)
    key = (await context.get_data())['photo_order_id']
    order = await credit_db.get_photo_order(key)
    assert (order['tariff'], order['template'], order['amount_kopecks']) == (990, 'woman_red', amount)
    invoice = sales_payments._request_payment_link.await_args.args[0]
    assert invoice['order_id'] == key and invoice['_param_user_id'] == '456'
    assert invoice['products'][0]['price'] == f'{amount // 100}.00'
    frames.assert_not_awaited()
    assert (await deliver_webhook(sales_payments, order)).status == 200
    assert (await deliver_webhook(sales_payments, order)).status == 200
    bot = SimpleNamespace(send_message=AsyncMock(return_value=object()))
    await PaidPhotoWorker(bot, admin_user_id=999).process()
    buttons = bot.send_message.await_args_list[0].kwargs['attachments'][0].payload.buttons
    assert buttons[0][0].payload == 'photo_resume:' + key
    await context.clear()
    await dispatch_callback(router, callback_event('photo_resume:' + key), context)
    assert await context.get_state() == PosterStates.waiting_for_photosession_photo
    await call_handler(router, 'collect_photosession_photo', photo_event(), context)
    assert await credit_db.get_photo_template_stats('woman_red', admin_id=999) == {
        'paid_packages': 1, 'revenue_kopecks': amount, 'generated_photos': 5, 'completed_packages': 1}
    assert (await credit_db.get_all_counters())['buy_photo_990'] == 1
    assert await credit_db.get_active_promo(456, 'photo') is not None
    assert await credit_db.get_photo_package_balance(key) == 0
    await dispatch_callback(router, callback_event('photo_resume:' + key), context)
    assert frames.await_count == 5
    with pytest.raises(ValueError):
        await credit_db.save_photo_result(key, 6, 'https://cdn.example/extra.jpg')


async def test_resume_after_fourth_frame_failure_uses_persisted_third_dress(credit_db, sales_payments):
    key = await credit_db.create_photo_order('photo_red_recovery', 456, 990)
    await credit_db.update_photo_order(key, style='photo_style_ny', template='woman_red')
    await deliver_webhook(sales_payments, await credit_db.get_photo_order(key))
    frames = AsyncMock(side_effect=[*[f'https://cdn.example/red-{i}.jpg' for i in range(1, 4)], RuntimeError('down')])
    router = create_router(admin_user_id=999, photosession_generator=frames)
    context = await ready_context()
    await dispatch_callback(router, callback_event('photo_resume:' + key), context)
    await call_handler(router, 'collect_photosession_photo', photo_event(), context)
    assert len(await credit_db.get_photo_results(key)) == 3
    assert await credit_db.get_active_promo(456, 'photo') is None
    await context.clear()
    frames.side_effect = ['https://cdn.example/red-4.jpg', 'https://cdn.example/red-5.jpg']
    await dispatch_callback(router, callback_event('photo_resume:' + key), context)
    assert frames.await_count == 6
    assert frames.await_args_list[-2].kwargs == {
        'style_reference_url': 'https://cdn.example/red-3.jpg', 'style_reference_scope': 'wardrobe',
        'pose_instruction': images.NY_RED_WOMAN_HEAD_POSES[3]}
    assert frames.await_args.kwargs['style_reference_scope'] == 'setting'
    assert (await credit_db.get_photo_template_stats('woman_red'))['completed_packages'] == 1


@pytest.mark.parametrize('owner,tariff,paid', [(456,990,False), (101,990,True), (456,590,True)])
async def test_red_generation_requires_owned_paid_correct_tariff(credit_db, sales_payments, owner, tariff, paid):
    key = await credit_db.create_photo_order('photo_red_guard', owner, tariff)
    await credit_db.update_photo_order(key, style='photo_style_ny', template='woman_red')
    if paid:
        await deliver_webhook(sales_payments, await credit_db.get_photo_order(key))
    frames = AsyncMock()
    router = create_router(admin_user_id=999, photosession_generator=frames)
    context = await ready_context()
    await dispatch_callback(router, callback_event('photo_resume:' + key), context)
    await call_handler(router, 'collect_photosession_photo', photo_event(), context)
    frames.assert_not_awaited()


@pytest.mark.parametrize('style,tariff', [('photo_style_ny','photo_tariff_590'), ('photo_style_studio','photo_tariff_990')])
async def test_red_callback_cannot_bypass_tariff_or_style_even_for_admin(credit_db, style, tariff):
    context = await ready_context()
    await context.update_data(photo_style=style, photo_tariff=tariff)
    await dispatch_callback(create_router(admin_user_id=456), callback_event('photo_temp_woman_red'), context)
    assert await context.get_state() != PosterStates.waiting_for_photosession_photo


async def test_stats_separate_red_from_other_990_template_and_admin(credit_db, sales_payments):
    for user,template in [(456,'woman_red'), (999,'woman_red'), (456,'mom_daughter_5')]:
        await credit_db.increment_counter('select_photo_' + template, user_id=user)
        key = await credit_db.create_photo_order(f'photo_red_stats_{user}_{template}', user, 990)
        await credit_db.update_photo_order(key, style='photo_style_ny', template=template)
        await deliver_webhook(sales_payments, await credit_db.get_photo_order(key), payment_id=key)
    event = message_event('/stats')
    event.get_ids = lambda: (123,999)
    await call_handler(create_router(admin_user_id=999), 'stats', event)
    text = event.message.answer.await_args.args[0]
    block = text.split('💃 Женщина в красном — 990 ₽ / 5 фото:',1)[1].split('👑 Эксклюзив',1)[0]
    assert '- Оплачено пакетов: 1' in block and '- Выбор сюжета (клики): 1' in block
    assert '- Выручка: 990,00 ₽' in block
    assert len(text) < 4000


@pytest.mark.parametrize('scope,expected', [('wardrobe','match ONLY its exact outfit'), ('setting','match ONLY its background')])
async def test_real_image_request_keeps_original_identity_and_limited_style_role(config, storage, api, monkeypatch, scope, expected):
    monkeypatch.setattr(images.Config, 'from_env', lambda: config)
    api.get(PHOTO_URL, body=PNG, content_type='image/png')
    api.get('https://cdn.example/style.png', body=PNG, content_type='image/png')
    api.post(IMAGE_EDIT_URL, payload={'data':[{'b64_json':ENCODED_PNG}]})
    api.head(PUBLIC_URL, status=200, repeat=True)
    result = await images.generate_photosession_frame(PHOTO_URL, images.NY_RED_WOMAN_PROMPTS[3 if scope=='wardrobe' else 4],
        style_reference_url='https://cdn.example/style.png', style_reference_scope=scope)
    assert PUBLIC_URL.fullmatch(result)
    fields = await multipart_fields(post_requests(api)[0])
    prompt = fields['prompt'].get_content()
    assert expected in prompt and 'FIRST image is the original client photo' in prompt
    assert 'Do NOT add red lipstick unless' in prompt
    assert fields['size'].get_content() == '1024x1536'


@pytest.mark.parametrize('index', range(5))
async def test_every_red_frame_sends_final_head_direction_after_style_instructions(config, storage, api, monkeypatch, index):
    monkeypatch.setattr(images.Config, 'from_env', lambda: config)
    api.get(PHOTO_URL, body=PNG, content_type='image/png')
    kwargs = {}
    if index in (1,3,4):
        api.get('https://cdn.example/style.png', body=PNG, content_type='image/png')
        kwargs['style_reference_url'] = 'https://cdn.example/style.png'
        if index in (3,4):
            kwargs['style_reference_scope'] = 'wardrobe' if index == 3 else 'setting'
    api.post(IMAGE_EDIT_URL, payload={'data':[{'b64_json':ENCODED_PNG}]})
    api.head(PUBLIC_URL, status=200, repeat=True)
    await images.generate_photosession_frame(PHOTO_URL, images.NY_RED_WOMAN_PROMPTS[index],
        pose_instruction=images.NY_RED_WOMAN_HEAD_POSES[index], **kwargs)
    fields = await multipart_fields(post_requests(api)[0])
    prompt = fields['prompt'].get_content()
    assert prompt.endswith(images.NY_RED_WOMAN_HEAD_POSES[index])
    assert prompt.count('MANDATORY FINAL SHOT DIRECTION') == 1
    if index in (1,3,4):
        assert prompt.index('MANDATORY FINAL SHOT DIRECTION') > prompt.index('INPUT ROLES:')
    assert 'rotate the head and facial planes in 3D' in prompt
    assert 'Keep the original head angle' not in prompt
    assert 'HEAD MOVEMENT: You may' not in prompt
    assert 'LIGHTING FOR EVERY FRAME:' in prompt
    if index in (2,3):
        assert 'mini dress' in prompt and 'floor-length gown' in prompt
    if index == 2:
        assert 'CROUCHING elegantly' in prompt and 'stable low dark stool' in prompt
    if index == 3:
        assert 'BAR COUNTER' in prompt and 'NO fireplace opening' in prompt
    if index in (2,4):
        assert 'only a FEW broad' in prompt

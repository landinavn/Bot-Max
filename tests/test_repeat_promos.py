import asyncio
import logging
from types import SimpleNamespace
from unittest.mock import AsyncMock
from urllib.parse import parse_qsl,urlsplit
from prodamus import parse_form_fields,sign_link_payload

import aiosqlite
import pytest
from maxapi.types import AttachmentUpload

import handlers
from handlers import create_router, PosterStates
from musical_orders import PaidMusicalWorker
from photo_orders import deliver_photo_result
from promo_service import send_repeat_offer,PromoOfferWorker,REPEAT_TEXTS
from test_sales_stats import sales_payments,deliver_webhook
from test_handlers import (callback_event,ready_context,dispatch_callback,call_handler,photo_event,
                           message_event,confirm_second_song_context,event_replies,max_image_upload,isolated_example_directory,text_correction)


RATES=[
    ('music_card','direct',29900,23900,'buy_music_direct','promo_buy_music_card'),
    ('cartoon','short',59000,47200,'buy_cartoon_590','promo_buy_cartoon_590'),
    ('cartoon','long',129000,103200,'buy_cartoon_1290','promo_buy_cartoon_1290'),
    ('photo',590,59000,47200,'buy_photo_590','promo_buy_photo_590'),
    ('photo',990,99000,79200,'buy_photo_990','promo_buy_photo_990'),
    ('photo',1490,149000,119200,'buy_photo_1490','promo_buy_photo_1490'),
]


async def book(db,category,tariff,user_id=101,suffix='one'):
    if category=='music_card':
        key=await db.create_musical_order('music_'+suffix,user_id,'https://cdn.example/card.jpg','https://cdn.example/song.mp3',tariff)
        return await db.get_musical_order(key)
    if category=='cartoon':
        key=await db.create_cartoon_order('cartoon_'+suffix,user_id,'theme_ny',tariff)
        return await db.get_cartoon_order(key)
    key=await db.create_photo_order('photo_'+suffix,user_id,tariff)
    return await db.get_photo_order(key)


async def invoice(service,order):
    if order['order_type']=='musical':
        return await service.create_musical_payment_link(order_id=order['order_id'],amount=order['amount_kopecks']//100,
                                                       description=order['product_name'])
    return await service.create_payment_link(order['user_id'],order_id=order['order_id'],
                                           amount=order['amount_kopecks']//100,description=order['product_name'])


@pytest.mark.parametrize('category', ['music_card','cartoon','photo'])
async def test_promo_lifecycle_persists_expiration_and_clear(credit_db,monkeypatch,caplog,category):
    now=1800000000
    monkeypatch.setattr(credit_db,'_now_ts',lambda:now)
    with caplog.at_level(logging.INFO):
        assert await credit_db.set_promo(101,category)==now+86400
    assert f'Промо 20% выдано: user=101, category={category}, expires={now+86400}' in caplog.text
    await credit_db.init_db()
    assert await credit_db.get_active_promo(101,category)==now+86400
    now+=86400
    assert await credit_db.get_active_promo(101,category) is None
    async with aiosqlite.connect(credit_db.DATABASE_PATH) as db:
        assert not await (await db.execute('SELECT 1 FROM active_promos WHERE user_id=? AND category=?',(101,category))).fetchone()
    await credit_db.set_promo(101,category)
    await credit_db.clear_promo(101,category)
    assert await credit_db.get_active_promo(101,category) is None


@pytest.mark.parametrize('category,tariff,base,discounted,regular_key,promo_key',RATES)
async def test_discount_invoices_and_webhooks_are_atomic_and_keep_regular_sales(credit_db,sales_payments,caplog,category,tariff,base,discounted,regular_key,promo_key):
    await credit_db.record_first_start(101,'yandex_newyear')
    await credit_db.set_promo(101,category)
    with caplog.at_level(logging.INFO):
        order=await book(credit_db,category,tariff)
    assert order['order_id'].startswith(f'promo_{category}_101_')
    assert order['amount_kopecks']==discounted
    assert order['promo_base_amount_kopecks']==base
    assert order['product_name'].endswith('(скидка 20%)')
    assert f'Применена скидка 20%: user=101, category={category}, amount={discounted//100}' in caplog.text
    link=await invoice(sales_payments,order)
    fields=parse_form_fields(parse_qsl(urlsplit(link).query))
    assert fields['products']==[{'name':order['product_name'],'price':f'{discounted//100}.00','quantity':'1'}]
    assert sign_link_payload({k:v for k,v in fields.items() if k!='sign'},sales_payments.secret)==fields['sign']
    assert await credit_db.get_active_promo(101,category) is not None
    for _ in range(2):
        assert (await deliver_webhook(sales_payments,order)).status==200
    counts=await credit_db.get_all_counters(admin_id=999)
    assert counts[regular_key]==counts[promo_key]==1
    assert (await credit_db.get_user(101))['total_purchases']==1
    assert await credit_db.get_active_promo(101,category) is None
    assert (await credit_db.get_stats_by_source('yandex_newyear'))['total_revenue']*100==discounted
    await credit_db.init_db()
    assert (await credit_db.get_all_counters())[promo_key]==1


@pytest.mark.parametrize('category,tariff,base,discounted,regular_key,promo_key',RATES)
async def test_expired_promos_use_full_price(credit_db,monkeypatch,category,tariff,base,discounted,regular_key,promo_key):
    now=1800000000
    monkeypatch.setattr(credit_db,'_now_ts',lambda:now)
    await credit_db.set_promo(101,category)
    now+=86401
    order=await book(credit_db,category,tariff)
    assert order['amount_kopecks']==base and not order['order_id'].startswith('promo_')
    assert '(скидка 20%)' not in order['product_name']


async def test_invoice_expiry_honors_issued_payment_and_does_not_clear_new_offer(credit_db,sales_payments,monkeypatch):
    now=1800000000
    monkeypatch.setattr(credit_db,'_now_ts',lambda:now)
    await credit_db.set_promo(101,'music_card')
    order=await book(credit_db,'music_card','direct')
    await invoice(sales_payments,order)
    now+=86401
    assert await credit_db.get_active_promo(101,'music_card') is None
    expires=await credit_db.set_promo(101,'music_card')
    assert (await deliver_webhook(sales_payments,order)).status==200
    assert await credit_db.get_active_promo(101,'music_card')==expires
    assert (await deliver_webhook(sales_payments,order)).status==200
    assert await credit_db.get_active_promo(101,'music_card')==expires


async def test_one_offer_cannot_create_two_parallel_discounted_invoices(credit_db):
    await credit_db.set_promo(101,'cartoon')
    results=await asyncio.gather(book(credit_db,'cartoon','short',suffix='a'),
                                 book(credit_db,'cartoon','long',suffix='b'),return_exceptions=True)
    assert sum(isinstance(row,dict) for row in results)==1
    assert sum(isinstance(row,credit_db.PromoReservedError) for row in results)==1


async def test_music_upgrade_does_not_consume_direct_card_promo(credit_db,sales_payments):
    await credit_db.set_promo(101,'music_card')
    order=await book(credit_db,'music_card','upsell')
    assert order['amount_kopecks']==14900 and order['promo_token'] is None
    await deliver_webhook(sales_payments,order)
    assert await credit_db.get_active_promo(101,'music_card') is not None


@pytest.mark.parametrize('change',[{'sum':'299.00'},{'_param_user_id':'999'},{'payment_status':'failed'},{'signature':'bad'}])
async def test_invalid_webhook_does_not_burn_coupon(credit_db,sales_payments,change):
    await credit_db.set_promo(101,'music_card')
    order=await book(credit_db,'music_card','direct')
    await invoice(sales_payments,order)
    await deliver_webhook(sales_payments,order,**change)
    assert await credit_db.get_active_promo(101,'music_card') is not None
    assert (await credit_db.get_musical_order(order['order_id']))['status']=='pending'
    assert 'promo_buy_music_card' not in await credit_db.get_all_counters()


async def test_counter_error_rolls_back_payment_coupon_and_redemption(credit_db,sales_payments,monkeypatch):
    await credit_db.set_promo(101,'photo')
    order=await book(credit_db,'photo',590)
    original=credit_db.increment_counter
    async def fail(key,**kwargs):
        await original(key,**kwargs)
        if key=='promo_buy_photo_590':
            raise __import__('sqlite3').OperationalError('counter failed')
    monkeypatch.setattr(credit_db,'increment_counter',fail)
    assert (await deliver_webhook(sales_payments,order)).status==503
    assert await credit_db.get_active_promo(101,'photo') is not None
    assert (await credit_db.get_photo_order(order['order_id']))['status']=='pending'
    assert not await credit_db.get_all_counters()


async def test_musical_success_offers_once_and_admin_never_receives_promo(credit_db,sales_payments):
    for user in (101,999):
        order=await book(credit_db,'music_card','direct',user_id=user,suffix=str(user))
        await deliver_webhook(sales_payments,order,payment_id=str(user))
        bot=SimpleNamespace(send_message=AsyncMock(return_value=object()))
        renderer=AsyncMock(return_value='https://cdn.example/video.mp4')
        worker=PaidMusicalWorker(bot,renderer=renderer,uploader=AsyncMock(return_value=object()),admin_user_id=999)
        await worker.process(order['order_id'])
        first_expiry=await credit_db.get_active_promo(user,'music_card')
        await worker.process(order['order_id'])
        await PromoOfferWorker(bot,admin_user_id=999).process()
        renderer.assert_awaited_once()
        promos=[c for c in bot.send_message.await_args_list if c.kwargs.get('text')==REPEAT_TEXTS['music_card']]
        assert len(promos)==(0 if user==999 else 1)
        assert (first_expiry is None)==(user==999)
        assert await credit_db.get_active_promo(user,'music_card')==first_expiry


async def test_failed_promo_notification_retries_without_rendering_or_extending(credit_db,sales_payments):
    order=await book(credit_db,'music_card','direct')
    await deliver_webhook(sales_payments,order)
    bot=SimpleNamespace(send_message=AsyncMock(side_effect=[object(),object(),RuntimeError('promo failed'),object()]))
    render=AsyncMock(return_value='https://cdn.example/video.mp4')
    await PaidMusicalWorker(bot,renderer=render,uploader=AsyncMock(return_value=object())).process(order['order_id'])
    expires=await credit_db.get_active_promo(101,'music_card')
    bot.send_message=AsyncMock(return_value=object())
    await PromoOfferWorker(bot).process()
    await PromoOfferWorker(bot).process()
    assert bot.send_message.await_count==1
    assert await credit_db.get_active_promo(101,'music_card')==expires
    render.assert_awaited_once()


@pytest.mark.parametrize('tariff,total',[(590,3),(990,5),(1490,7)])
async def test_photo_promo_only_after_entire_package_is_generated_and_delivered(credit_db,sales_payments,tariff,total):
    order=await book(credit_db,'photo',tariff)
    await deliver_webhook(sales_payments,order)
    bot=SimpleNamespace(send_message=AsyncMock(return_value=object()))
    uploader=AsyncMock(return_value=AttachmentUpload(type='image',payload={'token':'image'}))
    for slot in range(1,total):
        await deliver_photo_result(bot,order['order_id'],slot,f'https://cdn.example/{slot}.jpg',uploader=uploader,admin_user_id=999)
        assert await credit_db.get_photo_package_balance(order['order_id'])==total-slot
        assert await credit_db.get_active_promo(101,'photo') is None
    await credit_db.save_photo_result(order['order_id'],total,f'https://cdn.example/{total}.jpg')
    assert await credit_db.get_photo_package_balance(order['order_id'])==0
    await send_repeat_offer(bot,101,'photo','photo:'+order['order_id'],admin_user_id=999)
    assert await credit_db.get_active_promo(101,'photo') is None # Last image not delivered yet.
    await deliver_photo_result(bot,order['order_id'],total,f'https://cdn.example/{total}.jpg',uploader=uploader,admin_user_id=999)
    expires=await credit_db.get_active_promo(101,'photo')
    assert expires is not None
    assert bot.send_message.await_args.kwargs['text']==REPEAT_TEXTS['photo']
    assert (await credit_db.get_photo_order(order['order_id']))['generation_status']=='completed'
    await deliver_photo_result(bot,order['order_id'],total,f'https://cdn.example/{total}.jpg',uploader=uploader,admin_user_id=999)
    assert await credit_db.get_active_promo(101,'photo')==expires
    assert sum(c.kwargs.get('text')==REPEAT_TEXTS['photo'] for c in bot.send_message.await_args_list)==1
    with pytest.raises(ValueError):
        await credit_db.save_photo_result(order['order_id'],total+1,'https://cdn.example/extra.jpg')


async def test_admin_photo_package_never_creates_promo(credit_db,sales_payments):
    order=await book(credit_db,'photo',590,user_id=999)
    await deliver_webhook(sales_payments,order)
    bot=SimpleNamespace(send_message=AsyncMock(return_value=object()))
    for slot in range(1,4):
        await deliver_photo_result(bot,order['order_id'],slot,f'https://cdn.example/{slot}.jpg',
                                  admin_user_id=999,uploader=AsyncMock(return_value=object()))
    await PromoOfferWorker(bot,admin_user_id=999).process()
    assert await credit_db.get_active_promo(999,'photo') is None
    assert bot.send_message.await_count==3


async def test_cartoon_result_creates_offer_after_success_only(credit_db):
    order=await book(credit_db,'cartoon','short',user_id=456)
    await credit_db.mark_cartoon_paid(order['order_id'],'cartoon-payment')
    context=await ready_context()
    await confirm_second_song_context(context,order_id=order['order_id'])
    event=callback_event('confirm_video_2')
    async def animate(url,prompt,**kwargs):
        if kwargs.get('on_task_created'):
            await kwargs['on_task_created']('task')
        return 'https://cdn.example/animated.mp4'
    old=handlers.generate_video_from_avatar
    handlers.generate_video_from_avatar=AsyncMock(side_effect=animate)
    try:
        await dispatch_callback(create_router(admin_user_id=999),event,context)
    finally:
        handlers.generate_video_from_avatar=old
    assert (await credit_db.get_cartoon_order(order['order_id']))['status']=='completed'
    assert await credit_db.get_active_promo(456,'cartoon') is not None
    assert any(c.kwargs.get('text')==REPEAT_TEXTS['cartoon'] for c in event.bot.send_message.await_args_list)


@pytest.mark.parametrize('category,payload', [('music_card','promo_music_card'),('cartoon','promo_cartoon'),('photo','promo_photo')])
async def test_promo_buttons_open_correct_category_and_expired_fallback(credit_db,category,payload):
    for active in (True,False):
        if active:
            await credit_db.set_promo(456,category)
        else:
            await credit_db.clear_promo(456,category)
        event=callback_event(payload)
        context=await ready_context()
        await dispatch_callback(create_router(admin_user_id=999),event,context)
        event.ack.assert_awaited_once()
        data=await context.get_data()
        if category=='music_card':
            assert data['video_product']=='direct'
        elif category=='cartoon':
            assert data['cartoon_promo_choose_tariff']
        else:
            assert data['photo_promo_flow']
        if not active:
            assert any('Срок действия скидки истёк' in str(c) for c in event.send.await_args_list)


@pytest.mark.parametrize('tariff,amount',[('short',472),('long',1032)])
async def test_promo_cartoon_theme_then_tariff_checkout(credit_db,sales_payments,tariff,amount):
    await credit_db.set_promo(456,'cartoon')
    router,context=create_router(admin_user_id=999,payments=sales_payments),await ready_context()
    await dispatch_callback(router,callback_event('promo_cartoon'),context)
    theme=callback_event('theme_ny');await dispatch_callback(router,theme,context)
    assert '472' in str(theme.send.await_args) and '1032' in str(theme.send.await_args)
    pay=callback_event('promo_cartoon_tariff:'+tariff)
    await dispatch_callback(router,pay,context)
    order=await credit_db.get_cartoon_order((await context.get_data())['cartoon_order_id'])
    assert order['amount_kopecks']==amount*100 and order['tariff']==tariff
    assert pay.send.await_args.kwargs['attachments'][0].payload.buttons[0][0].text==f'Оплатить {amount} ₽'


@pytest.mark.parametrize('tariff,amount',[(590,472),(990,792),(1490,1192)])
async def test_promo_photo_style_then_tariff_checkout(credit_db,sales_payments,tariff,amount):
    await credit_db.set_promo(456,'photo')
    router,context=create_router(admin_user_id=999,payments=sales_payments),await ready_context()
    await dispatch_callback(router,callback_event('promo_photo'),context)
    await dispatch_callback(router,callback_event('photo_style_ny'),context)
    await dispatch_callback(router,callback_event(f'photo_tariff_{tariff}'),context)
    order=await credit_db.get_photo_order((await context.get_data())['photo_order_id'])
    assert order['amount_kopecks']==amount*100 and order['style']=='photo_style_ny'


async def test_repeat_stats_block_after_sales_excludes_admin(credit_db):
    for key in [rate[-1] for rate in RATES]:
        await credit_db.increment_counter(key,user_id=101)
        await credit_db.increment_counter(key,user_id=999)
    event=message_event('/stats');event.get_ids=lambda:(123,999)
    await call_handler(create_router(admin_user_id=999),'stats',event)
    text=event.message.answer.await_args.args[0]
    assert text.index('Всего покупок:')<text.index('🔁 Повторные покупки')<text.index('📍 ИСТОЧНИКИ')
    for label in ('Музыкальные открытки','Мульт-открытки (590)','Мульт-открытки (1290)',
                  'Фотосессии (590)','Фотосессии (990)','Фотосессии (1490)'):
        assert f'- {label}: 1' in text


async def test_paid_family_package_generates_three_and_issues_promo_only_after_last_delivery(credit_db,sales_payments):
    order=await book(credit_db,'photo',590,user_id=456)
    await credit_db.update_photo_order(order['order_id'],style='photo_style_ny',template='fam3')
    await deliver_webhook(sales_payments,order)
    frames=AsyncMock(side_effect=[f'https://cdn.example/frame-{i}.jpg' for i in range(1,4)])
    router,context=create_router(admin_user_id=999,photosession_generator=frames),await ready_context()
    resume=callback_event('photo_resume:'+order['order_id'])
    await dispatch_callback(router,resume,context)
    assert await context.get_state()==PosterStates.waiting_for_photosession_photo
    photo=photo_event()
    async def sent(**kwargs):
        if kwargs.get('text','').startswith('Фотография'):
            assert await credit_db.get_active_promo(456,'photo') is None
        return object()
    photo.bot.send_message.side_effect=sent
    await call_handler(router,'collect_photosession_photo',photo,context)
    assert frames.await_count==3
    assert await credit_db.get_photo_package_balance(order['order_id'])==0
    expires=await credit_db.get_active_promo(456,'photo')
    assert expires is not None
    await dispatch_callback(router,callback_event('photo_resume:'+order['order_id']),context)
    assert frames.await_count==3
    assert await credit_db.get_active_promo(456,'photo')==expires


async def test_last_photo_delivery_failure_keeps_coupon_unissued_and_retry_uses_saved_results(credit_db,sales_payments):
    order=await book(credit_db,'photo',590,user_id=456)
    await credit_db.update_photo_order(order['order_id'],style='photo_style_ny',template='fam3')
    await deliver_webhook(sales_payments,order)
    frames=AsyncMock(side_effect=[f'https://cdn.example/frame-{i}.jpg' for i in range(1,4)])
    router,context=create_router(admin_user_id=999,photosession_generator=frames),await ready_context()
    await dispatch_callback(router,callback_event('photo_resume:'+order['order_id']),context)
    photo=photo_event()
    photo.bot.send_message.side_effect=[object(),object(),RuntimeError('last frame delivery failed')]
    await call_handler(router,'collect_photosession_photo',photo,context)
    assert await credit_db.get_photo_package_balance(order['order_id'])==0
    assert await credit_db.get_active_promo(456,'photo') is None
    await context.clear()
    retry=callback_event('photo_resume:'+order['order_id'])
    await dispatch_callback(router,retry,context)
    assert frames.await_count==3
    assert await credit_db.get_active_promo(456,'photo') is not None
    assert sum(c.kwargs.get('text','').startswith('Фотография') for c in retry.bot.send_message.await_args_list)==1


async def test_admin_manual_delivery_consumes_seven_photo_slots_and_blocks_nonadmin(credit_db,sales_payments):
    order=await book(credit_db,'photo',1490)
    await deliver_webhook(sales_payments,order)
    router=create_router(admin_user_id=999)
    unauthorized=message_event(f"/deliver_photo {order['order_id']} 1 https://cdn.example/one.jpg")
    await call_handler(router,'deliver_paid_photo',unauthorized)
    assert await credit_db.get_photo_package_balance(order['order_id'])==7
    for slot in range(1,8):
        event=message_event(f"/deliver_photo {order['order_id']} {slot} https://cdn.example/{slot}.jpg")
        event.get_ids=lambda:(123,999)
        await call_handler(router,'deliver_paid_photo',event)
        assert (await credit_db.get_active_promo(101,'photo') is not None)==(slot==7)
    assert await credit_db.get_photo_package_balance(order['order_id'])==0


async def test_reserved_coupon_button_reopens_same_invoice(credit_db,sales_payments):
    await credit_db.set_promo(456,'cartoon')
    order=await book(credit_db,'cartoon','short',user_id=456)
    link=await invoice(sales_payments,order)
    event=callback_event('promo_cartoon')
    await dispatch_callback(create_router(admin_user_id=999,payments=sales_payments),event,await ready_context())
    assert event.send.await_args.kwargs['attachments'][0].payload.buttons[0][0].url==link
    sales_payments._request_payment_link.assert_awaited_once()


async def test_music_flow_prices_direct_card_at_239_with_active_promo(credit_db,sales_payments):
    from test_handlers import prepare_teacher_video,dispatch_callback_and_phrase
    await credit_db.set_promo(456,'music_card')
    router=create_router(admin_user_id=999,payments=sales_payments,
                         postcard_generator=AsyncMock(return_value=['https://cdn.example/clean.jpg']))
    context=await ready_context()
    await prepare_teacher_video(router,context,'cat_video')
    event=callback_event('video_song:audio_fun_ny')
    await dispatch_callback_and_phrase(router,event,context)
    order=await credit_db.get_musical_order((await context.get_data())['music_checkout_id'])
    assert order['amount_kopecks']==23900 and order['order_id'].startswith('promo_music_card_')
    assert 'Оплатить 239 ₽' in str(event.send.await_args_list)
    assert await credit_db.get_active_promo(456,'music_card') is not None


@pytest.mark.parametrize('payload',['promo_music_card','promo_cartoon','promo_photo'])
async def test_admin_promo_buttons_do_not_create_or_show_offers(credit_db,payload):
    event=callback_event(payload);event.get_ids=lambda:(123,999)
    await dispatch_callback(create_router(admin_user_id=999),event,await ready_context())
    assert '20%' not in str(event.send.await_args_list)
    for category in ('music_card','cartoon','photo'):
        assert await credit_db.get_active_promo(999,category) is None


async def test_old_paid_music_retry_is_not_rebilled_when_new_promo_exists(credit_db,sales_payments):
    from test_handlers import prepare_teacher_video,dispatch_callback_and_phrase
    router=create_router(admin_user_id=999,payments=sales_payments,
                         postcard_generator=AsyncMock(return_value=['https://cdn.example/clean.jpg']))
    context=await ready_context()
    await prepare_teacher_video(router,context,'cat_video')
    await dispatch_callback_and_phrase(router,callback_event('video_song:audio_fun_ny'),context)
    order_id=(await context.get_data())['music_checkout_id']
    order=await credit_db.get_musical_order(order_id)
    await deliver_webhook(sales_payments,order)
    bot=SimpleNamespace(send_message=AsyncMock(return_value=object()))
    await PaidMusicalWorker(bot,admin_user_id=999,renderer=AsyncMock(return_value='https://cdn.example/final.mp4'),
                            uploader=AsyncMock(return_value=object())).process(order_id)
    assert await credit_db.get_active_promo(456,'music_card') is not None
    retry=callback_event('retry_music_checkout')
    await dispatch_callback(router,retry,context)
    assert (await context.get_data())['music_checkout_id']==order_id
    assert 'уже оплачен' in str(retry.send.await_args_list)
    sales_payments._request_payment_link.assert_awaited_once()
    assert await credit_db.get_promo_reservation(456,'music_card') is None


async def test_undelivered_photo_can_be_corrected_without_spending_an_extra_slot(credit_db,sales_payments):
    order=await book(credit_db,'photo',590)
    await deliver_webhook(sales_payments,order)
    with pytest.raises(ValueError):
        await credit_db.save_photo_result(order['order_id'],1,'https://')
    assert await credit_db.get_photo_package_balance(order['order_id'])==3
    await credit_db.save_photo_result(order['order_id'],1,'https://cdn.example/broken.jpg')
    await credit_db.save_photo_result(order['order_id'],1,'https://cdn.example/correct.jpg')
    assert await credit_db.get_photo_package_balance(order['order_id'])==2
    bot=SimpleNamespace(send_message=AsyncMock(return_value=object()))
    await deliver_photo_result(bot,order['order_id'],1,'https://cdn.example/correct.jpg',uploader=AsyncMock(return_value=object()))
    with pytest.raises(ValueError):
        await credit_db.save_photo_result(order['order_id'],1,'https://cdn.example/extra.jpg')
    assert await credit_db.get_active_promo(101,'photo') is None


async def test_simultaneous_offer_delivery_claims_once(credit_db):
    bot=SimpleNamespace(send_message=AsyncMock(return_value=object()))
    await asyncio.gather(*(send_repeat_offer(bot,101,'music_card','music:one',admin_user_id=999) for _ in range(5)))
    assert bot.send_message.await_count==1
    assert await credit_db.get_active_promo(101,'music_card') is not None


async def test_abandoned_offer_claim_retries_after_lease_without_extending(credit_db,monkeypatch):
    now=1800000000
    monkeypatch.setattr(credit_db,'_now_ts',lambda:now)
    await credit_db.grant_repeat_offer(101,'music_card','music:one')
    expires=await credit_db.get_active_promo(101,'music_card')
    assert await credit_db.claim_repeat_offer('music:one')
    bot=SimpleNamespace(send_message=AsyncMock(return_value=object()))
    await PromoOfferWorker(bot).process()
    bot.send_message.assert_not_awaited()
    now+=301
    await PromoOfferWorker(bot).process()
    assert bot.send_message.await_count==1
    assert await credit_db.get_active_promo(101,'music_card')==expires

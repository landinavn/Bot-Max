"""Prepaid photo animation: prices, signed receipts, quotas and durable Kling jobs."""
import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock
from urllib.parse import parse_qs,urlsplit

import aiosqlite
import pytest
from maxapi.types import AttachmentUpload

import handlers
import video_service
from animation_orders import PaidAnimationWorker
from handlers import create_router,PosterStates
from test_sales_stats import sales_payments,deliver_webhook
from test_handlers import (callback_event,ready_context,dispatch_callback,call_handler,message_event,photo_event,
                           event_replies,isolated_example_directory,max_image_upload,text_correction)


async def paid_order(db,service,package='one',user=456,payment='receipt'):
    order=await db.create_animation_order(user,package)
    assert (await deliver_webhook(service,order,payment_id=payment)).status==200
    return await db.get_animation_order(order['order_id'])


@pytest.mark.parametrize('package,tariff,amount,count',[('one','intro',199,1),('three','three',599,3),('five','five',899,5)])
async def test_real_personal_links_and_signed_webhook_prices(credit_db,sales_payments,package,tariff,amount,count):
    await credit_db.record_first_start(456,'vk_posts')
    order=await credit_db.create_animation_order(456,package)
    link=await sales_payments.create_payment_link(456,order_id=order['order_id'],amount=amount,description=order['product_name'])
    query=parse_qs(urlsplit(link).query)
    assert query['products[0][price]']==[f'{amount}.00']
    assert query['order_id']==[order['order_id']]
    assert order['tariff']==tariff and order['quantity']==count
    assert (await deliver_webhook(sales_payments,order)).status==200
    assert (await deliver_webhook(sales_payments,order)).status==200
    counters=await credit_db.get_all_counters()
    assert counters['buy_animation_'+tariff]==1
    assert (await credit_db.get_user(456))['total_purchases']==1
    source=next(s for s in await credit_db.get_source_stats() if s['source']=='vk_posts')
    assert source['buyers']==1 and source['revenue_kopecks']==amount*100
    assert (await credit_db.animation_orders_for_user(456))[0]['remaining']==count


async def test_first_price_is_once_and_concurrent_pending_checkout_is_reused(credit_db,sales_payments):
    orders=await asyncio.gather(*(credit_db.create_animation_order(456,'one') for _ in range(8)))
    assert len({o['order_id'] for o in orders})==1
    await deliver_webhook(sales_payments,orders[0])
    assert await credit_db.animation_first_price(456)==249
    second=await credit_db.create_animation_order(456,'one')
    assert second['tariff']=='one' and second['amount_kopecks']==24900
    assert (await deliver_webhook(sales_payments,second,payment_id='second')).status==200
    assert (await credit_db.get_all_counters())['buy_animation_one']==1


async def test_first_price_follows_first_paid_purchase_in_section(credit_db,sales_payments):
    await paid_order(credit_db,sales_payments,'three')
    assert (await credit_db.create_animation_order(456,'one'))['amount_kopecks']==24900


@pytest.mark.parametrize('changes',[{'sum':'249.00'},{'products':[{'name':'Оживление 5 фотографий','price':'199.00','quantity':'1'}]},
                                     {'_param_user_id':'999'},{'currency':'usd'}])
async def test_invalid_payment_does_not_unlock_photo_balance(credit_db,sales_payments,changes):
    order=await credit_db.create_animation_order(456,'one')
    assert (await deliver_webhook(sales_payments,order,**changes)).status==400
    assert (await credit_db.get_animation_order(order['order_id']))['status']=='pending'
    assert await credit_db.animation_orders_for_user(456)==[]


async def test_payment_signature_failure_and_incomplete_payment_do_not_unlock(credit_db,sales_payments):
    order=await credit_db.create_animation_order(456,'one')
    assert (await deliver_webhook(sales_payments,order,signature='invalid')).status==403
    assert (await deliver_webhook(sales_payments,order,payment_status='waiting')).status==200
    assert await credit_db.animation_orders_for_user(456)==[]


async def test_new_product_never_adds_image_credits_and_receipts_cannot_cross_products(credit_db,sales_payments):
    animation=await paid_order(credit_db,sales_payments)
    assert (await credit_db.get_user(456))['balance']==0
    await credit_db.create_payment_order('credit-check',456,1,9900,handlers.PACKAGES[1][1])
    with pytest.raises(ValueError):
        await credit_db.add_credits(456,1,payment_order_id='credit-check',provider_payment_id='receipt')
    await credit_db.add_credits(456,1,payment_order_id='credit-check',provider_payment_id='credit-receipt')
    another=await credit_db.create_animation_order(456,'three')
    assert (await deliver_webhook(sales_payments,another,payment_id='credit-receipt')).status==400
    assert (await credit_db.get_animation_order(another['order_id']))['status']=='pending'


@pytest.mark.parametrize('package,count',[('one',1),('three',3),('five',5)])
async def test_package_quotas_no_duplicate_photo_or_unpaid_access(credit_db,sales_payments,package,count):
    order=await credit_db.create_animation_order(456,package)
    with pytest.raises(ValueError):
        await credit_db.queue_animation_photo(456,order['order_id'],'https://cdn.example/a.jpg','m0')
    await deliver_webhook(sales_payments,order)
    with pytest.raises(ValueError):
        await credit_db.queue_animation_photo(999,order['order_id'],'https://cdn.example/a.jpg','stolen')
    keys=[]
    for i in range(count):
        key=await credit_db.queue_animation_photo(456,order['order_id'],'https://cdn.example/a.jpg',f'm{i}')
        assert key==await credit_db.queue_animation_photo(456,order['order_id'],'https://cdn.example/a.jpg',f'm{i}')
        keys.append(key)
    assert len(set(keys))==count
    assert (await credit_db.animation_orders_for_user(456))[0]['remaining']==0
    with pytest.raises(ValueError):
        await credit_db.queue_animation_photo(456,order['order_id'],'https://cdn.example/a.jpg','extra')


async def test_concurrent_photos_cannot_overdraw_single_unit(credit_db,sales_payments):
    order=await paid_order(credit_db,sales_payments)
    results=await asyncio.gather(*(credit_db.queue_animation_photo(456,order['order_id'],'https://cdn.example/a.jpg',str(i))
                                   for i in range(5)),return_exceptions=True)
    assert sum(isinstance(r,str) for r in results)==1
    assert sum(isinstance(r,ValueError) for r in results)==4


async def new_job(db,service):
    order=await paid_order(db,service)
    key=await db.queue_animation_photo(456,order['order_id'],'https://cdn.example/real-photo.jpg','source-mid')
    return order,key


async def test_worker_preserves_photo_and_uses_realistic_prompt_saves_task_sends_video(credit_db,sales_payments):
    order,key=await new_job(credit_db,sales_payments)
    async def generate(url,prompt,**kwargs):
        assert url=='https://cdn.example/real-photo.jpg'
        assert prompt==video_service.PHOTO_ANIMATION_PROMPT and 'no cartoon transformation' in prompt
        assert kwargs['mode']=='photo_animation'
        await kwargs['on_task_created']('kling-1')
        return 'https://cdn.example/animation.mp4'
    bot=SimpleNamespace(send_message=AsyncMock(return_value=object()))
    uploader=AsyncMock(return_value=AttachmentUpload(type='video',payload={'token':'video-token'}))
    worker=PaidAnimationWorker(bot,generator=generate,uploader=uploader)
    await worker.notify(order['order_id'])
    assert (await credit_db.get_animation_order(order['order_id']))['notified']==1
    await worker.process(key)
    job=await credit_db.get_animation_job(key)
    assert job['status']=='delivered' and job['task_id']=='kling-1'
    assert bot.send_message.await_args.kwargs['attachments'][0].type.value=='video'
    await worker.process(key)
    uploader.assert_awaited_once()
    assert (await credit_db.get_all_counters())['gen_animation']==1


async def test_delivery_retry_reuses_rendered_mp4_without_paid_post(credit_db,sales_payments):
    _,key=await new_job(credit_db,sales_payments)
    await credit_db.update_animation_job(key,status='ready',video_url='https://cdn.example/ready.mp4')
    bot=SimpleNamespace(send_message=AsyncMock(side_effect=RuntimeError('send failed')))
    generator=AsyncMock()
    worker=PaidAnimationWorker(bot,generator=generator,poller=generator,uploader=AsyncMock(return_value=AttachmentUpload(type='video',payload={'token':'v'})))
    await worker.process(key)
    await credit_db.update_animation_job(key,next_attempt_at=0)
    bot.send_message=AsyncMock(return_value=object())
    await worker.process(key)
    generator.assert_not_awaited()
    assert (await credit_db.get_animation_job(key))['status']=='delivered'
    assert (await credit_db.get_all_counters())['gen_animation']==1


async def test_interrupted_unknown_paid_post_goes_to_support_not_second_post(credit_db,sales_payments):
    _,key=await new_job(credit_db,sales_payments)
    await credit_db.update_animation_job(key,status='creating',running=1)
    await credit_db.animation_work_items(reset=True)
    generator=AsyncMock()
    bot=SimpleNamespace(send_message=AsyncMock(return_value=object()))
    await PaidAnimationWorker(bot,generator=generator,admin_user_id=999).process(key)
    generator.assert_not_awaited()
    assert (await credit_db.get_animation_job(key))['status']=='manual'
    assert any('Оплата и фотография сохранены' in str(c) for c in bot.send_message.await_args_list)
    assert (await credit_db.animation_orders_for_user(456))[0]['remaining']==0


async def test_known_task_resumes_after_timeout_without_recreating(credit_db,sales_payments):
    _,key=await new_job(credit_db,sales_payments)
    await credit_db.update_animation_job(key,status='processing',task_id='saved-task')
    generator=AsyncMock(side_effect=TimeoutError('processing'))
    bot=SimpleNamespace(send_message=AsyncMock(return_value=object()))
    worker=PaidAnimationWorker(bot,generator=generator,poller=generator,uploader=AsyncMock(return_value=AttachmentUpload(type='video',payload={'token':'v'})))
    await worker.process(key)
    assert (await credit_db.get_animation_job(key))['status']=='processing'
    await credit_db.update_animation_job(key,next_attempt_at=0)
    generator.side_effect=None;generator.return_value='https://cdn.example/video.mp4'
    await worker.process(key)
    assert generator.await_args.args==('saved-task',)
    assert (await credit_db.get_animation_job(key))['status']=='delivered'


@pytest.mark.parametrize('source',['vk_posts','dzen_posts'])
async def test_tracking_links_and_first_source_are_distinct_and_persistent(credit_db,source):
    link=handlers.campaign_bot_link('https://max.ru/custom_bot?x=1',source)
    assert parse_qs(urlsplit(link).query)=={'x':['1'],'start':[source]}
    await credit_db.record_first_start(456,source+'?yclid=abc')
    await credit_db.record_first_start(456,'yandex_ads')
    assert (await credit_db.get_user(456))['first_start_source']==source
    await credit_db.init_db()
    assert (await credit_db.get_user(456))['first_start_source']==source


async def test_stats_remove_retired_product_split_interest_and_include_links(credit_db):
    for key in ('click_cartoon_short','click_custom_order','submit_custom_order','click_animation','gen_animation'):
        await credit_db.increment_counter(key,user_id=456)
        await credit_db.increment_counter(key,user_id=999)
    event=message_event('/stats');event.get_ids=lambda:(123,999)
    await call_handler(create_router(admin_user_id=999,bot_link='https://max.ru/my_bot'),'stats',event)
    text=event.message.answer.await_args.args[0]
    assert 'Мульт-поздравление' not in text and 'Мульт-услуги' not in text and '1290' not in text
    assert '- Мульт-открытки: 1' in text and '- Эксклюзив (ручная работа): 1' in text
    assert '- Оживление фотографий: 1' in text and '- Отправлено заявок: 1' in text
    assert 'https://max.ru/my_bot?start=vk_posts' in text and 'https://max.ru/my_bot?start=dzen_posts' in text


async def test_menu_checkout_and_postpayment_fsm_photo_queue(credit_db,sales_payments):
    router=create_router(admin_user_id=999,payments=sales_payments);context=await ready_context()
    menu=callback_event('cat_animation');await dispatch_callback(router,menu,context)
    assert '199 ₽' in menu.send.await_args.args[0]
    assert (await credit_db.get_all_counters())['click_animation']==1
    pay=callback_event('animation_buy:three');await dispatch_callback(router,pay,context)
    link=pay.send.await_args.kwargs['attachments'][0].payload.buttons[0][0].url
    order_id=parse_qs(urlsplit(link).query)['order_id'][0]
    resume=callback_event('animation_resume:'+order_id);await dispatch_callback(router,resume,context)
    assert 'ещё не подтверждена' in resume.send.await_args.args[0]
    order=await credit_db.get_animation_order(order_id);await deliver_webhook(sales_payments,order)
    await dispatch_callback(router,resume,context)
    assert await context.get_state()==PosterStates.waiting_for_animation_photo
    photo=photo_event();photo.message.body.mid='source-message'
    await call_handler(router,'echo',photo,context)
    assert 'Осталось: 2' in photo.message.answer.await_args.args[0]
    await call_handler(router,'echo',photo,context)
    assert (await credit_db.animation_orders_for_user(456))[0]['remaining']==2


async def test_admin_free_testing_is_not_a_sale_or_revenue(credit_db):
    await credit_db.record_first_start(456,'vk_posts')
    router=create_router(admin_user_id=456);context=await ready_context()
    event=callback_event('animation_buy:five');await dispatch_callback(router,event,context)
    assert await context.get_state()==PosterStates.waiting_for_animation_photo
    order=(await credit_db.animation_orders_for_user(456))[0]
    assert order['is_admin']==1 and order['remaining']==5
    assert (await credit_db.get_user(456))['total_purchases']==0
    assert (await credit_db.get_all_counters()).get('buy_animation_five',0)==0
    row=next(r for r in await credit_db.get_source_stats() if r['source']=='vk_posts')
    assert row['revenue_kopecks']==0
    await credit_db.init_db()
    assert (await credit_db.get_all_counters()).get('buy_animation_five',0)==0


def test_all_cartoon_themes_have_natural_hand_movement_and_keep_backgrounds():
    for theme,prompt in video_service.CARTOON_VIDEO_PROMPTS.items():
        assert 'natural hand gestures' in prompt and 'upper-body movements' in prompt
        assert video_service.build_video_prompt(theme,6)==video_service.CROWD_VIDEO_PROMPT


@pytest.mark.parametrize('source',['vk_posts','dzen_posts'])
async def test_command_start_and_bot_started_save_social_post_source(credit_db,source):
    from maxapi.types import BotStarted,User
    router=create_router()
    context=await ready_context()
    event=message_event('/start '+source)
    await call_handler(router,'start',event,context)
    assert (await credit_db.get_user(456))['first_start_source']==source
    started=BotStarted(timestamp=1000,chat_id=123,user=User(user_id=101,first_name='New',is_bot=False),payload=source)
    started.bot=event.bot
    other=handlers.MemoryContext(chat_id=123,user_id=101)
    await call_handler(router,'bot_started',started,other)
    assert (await credit_db.get_user(101))['first_start_source']==source


async def test_animation_photos_route_through_real_dispatcher_and_cancel(credit_db,sales_payments,monkeypatch,event_replies):
    from maxapi import Dispatcher
    from maxapi.context import MemoryContext
    from maxapi.context.isolation import SimpleEventIsolation
    from maxapi.types import MessageCallback,MessageCreated,User
    order=await paid_order(credit_db,sales_payments,'three')
    monkeypatch.setattr(handlers,'_reply',event_replies)
    bot=SimpleNamespace(me=User(user_id=888,first_name='Bot',is_bot=True),
        send_message=AsyncMock(return_value=object()),send_callback=AsyncMock())
    dp=Dispatcher(storage=MemoryContext,event_isolation=SimpleEventIsolation())
    dp.include_routers(create_router(admin_user_id=999,payments=sales_payments))
    user={'user_id':456,'first_name':'Client','is_bot':False}
    def message(text=None,attachments=None,sender=None,mid='new-source'):
        return {'sender':sender or user,'recipient':{'chat_id':123,'chat_type':'dialog'},'timestamp':1,
                'body':{'mid':mid,'seq':1,'text':text,'attachments':attachments}}
    async def click(payload):
        await dp.handle(MessageCallback(timestamp=1,bot=bot,message=message(sender=bot.me.model_dump()),
            callback={'timestamp':1,'callback_id':'animation','payload':payload,'user':user}))
    try:
        await click('animation_resume:'+order['order_id'])
        image={'type':'image','payload':{'photo_id':1,'token':'source','url':'https://cdn.example/source.jpg'}}
        await dp.handle(MessageCreated(timestamp=1,bot=bot,message=message(attachments=[image])))
        assert (await credit_db.animation_orders_for_user(456))[0]['remaining']==2
        context=dp.fsm.get_context(chat_id=123,user_id=456)
        assert await context.get_state()==PosterStates.waiting_for_animation_photo
        await dp.handle(MessageCreated(timestamp=1,bot=bot,message=message('/cancel',mid='cancel')))
        assert await context.get_data()=={}
        assert await context.get_state()==PosterStates.waiting_for_mode
    finally:
        await dp.shutdown()


async def test_failed_animation_payment_transaction_rolls_back_all_sale_counters(credit_db,sales_payments,monkeypatch):
    order=await credit_db.create_animation_order(456,'one')
    original=credit_db.increment_counter
    async def broken(key,**kwargs):
        if key=='buy_animation_intro': raise RuntimeError('counter unavailable')
        return await original(key,**kwargs)
    monkeypatch.setattr(credit_db,'increment_counter',broken)
    with pytest.raises(RuntimeError):
        await deliver_webhook(sales_payments,order)
    assert (await credit_db.get_animation_order(order['order_id']))['status']=='pending'
    assert (await credit_db.get_user(456)) is None
    monkeypatch.setattr(credit_db,'increment_counter',original)
    assert (await deliver_webhook(sales_payments,order)).status==200
    assert (await credit_db.get_all_counters())['buy_animation_intro']==1


@pytest.mark.parametrize('terminal',[False,True])
async def test_poll_errors_preserve_saved_task_and_only_terminal_failure_goes_manual(credit_db,sales_payments,terminal):
    _,key=await new_job(credit_db,sales_payments)
    await credit_db.update_animation_job(key,status='processing',task_id='accepted-task')
    exc=video_service.VideoTaskFailed('provider failed') if terminal else video_service.VideoServiceError('network error',http_status=500)
    poller=AsyncMock(side_effect=exc)
    creator=AsyncMock()
    bot=SimpleNamespace(send_message=AsyncMock(return_value=object()))
    await PaidAnimationWorker(bot,generator=creator,poller=poller).process(key)
    creator.assert_not_awaited()
    assert (await credit_db.get_animation_job(key))['status']==('manual' if terminal else 'processing')

"""Campaign entitlements, signed payments and safe delivery; no live API calls."""
import asyncio
from pathlib import Path
from unittest.mock import AsyncMock

import aiosqlite
import pytest
from maxapi.exceptions import MaxApiError

import database
import handlers
import promo1111 as promo
from handlers import create_router
from prodamus import sign_link_payload
from test_sales_stats import sales_payments, deliver_webhook
from test_update_broadcast import event
from test_handlers import (call_handler, callback_event, dispatch_callback, ready_context,
                           event_replies, isolated_example_directory)


@pytest.fixture(autouse=True)
def campaign_clock(monkeypatch):
    clock=[promo.DEADLINE-86400]
    monkeypatch.setattr(database,'_now_ts',lambda:clock[0])
    return clock


async def activate(user=101):
    await promo.progress(user,3,activate=True)


async def paid(db,code='111',user=101,receipt='receipt'):
    await activate(user)
    order=await promo.create_order(user,code)
    await db.add_credits(user,order['credits'],payment_order_id=order['order_id'],provider_payment_id=receipt)
    return order


async def test_recipient_eligibility_and_moscow_midnight(credit_db,campaign_clock):
    assert promo.DEADLINE==1794430800  # 12 November 00:00 MSK == 11 November 21:00 UTC.
    assert not await promo.eligible(101)
    with pytest.raises(database.PromoUnavailableError,match='после получения'):
        await promo.create_order(101,'111')
    await promo.progress(101,2)
    assert not await promo.eligible(101)
    await activate()
    assert await promo.eligible(101)
    campaign_clock[0]=promo.DEADLINE-1
    assert await promo.eligible(101)
    await promo.create_order(101,'333')
    campaign_clock[0]=promo.DEADLINE
    assert not promo.open_now() and not await promo.eligible(101)
    for code in promo.PACKAGES:
        with pytest.raises(database.PromoUnavailableError,match='завершена'):
            await promo.create_order(101,code)


async def test_single_use_checkout_concurrency_and_paid_guard(credit_db):
    await activate()
    orders=await asyncio.gather(*(promo.create_order(101,'111') for _ in range(8)))
    assert len({o['order_id'] for o in orders})==1
    order=orders[0]
    await credit_db.add_credits(101,3,payment_order_id=order['order_id'],provider_payment_id='single')
    with pytest.raises(database.PromoUnavailableError,match=promo.ONCE):
        await promo.create_order(101,'111')
    await activate(102)
    assert (await promo.create_order(102,'111'))['user_id']==102


@pytest.mark.parametrize('code',['333','799'])
async def test_large_packages_are_unlimited(credit_db,code):
    orders=[await paid(credit_db,code,receipt=str(i)) for i in range(3)]
    assert len({o['order_id'] for o in orders})==3
    user=await credit_db.get_user(101)
    assert user['total_purchases']==3
    assert user['promo_image_balance']==(33 if code=='333' else 0)
    assert user['balance']==(45 if code=='799' else 0)
    assert user['free_animation_count']==(3 if code=='799' else 0)


@pytest.mark.parametrize('code',['111','333','799'])
async def test_signed_invoice_and_idempotent_webhook(credit_db,sales_payments,code,campaign_clock):
    await credit_db.record_first_start(101,'yandex_newyear')
    await activate()
    link=await sales_payments.create_promo1111_payment_link(101,code)
    data=sales_payments._request_payment_link.await_args.args[0]
    amount,credits,title,restricted,bonus=promo.PACKAGES[code]
    assert data['products']==[dict(name=title,price=f'{amount//100}.00',quantity='1')]
    assert data['link_expired']=='2026-11-12 00:00' and data['payments_limit']=='1'
    assert data['_param_package_id']=='promo1111_'+code and data['_param_credits']==str(credits)
    assert data['sign']==sign_link_payload({k:v for k,v in data.items() if k!='sign'},sales_payments.secret)
    order=await credit_db.get_payment_order(data['order_id'])
    assert order['payment_link']==link
    if code=='111':
        assert await sales_payments.create_promo1111_payment_link(101,code)==link
        sales_payments._request_payment_link.assert_awaited_once()
    # Valid paid callbacks may arrive late: never strand a customer's payment.
    campaign_clock[0]=promo.DEADLINE+86400
    for _ in range(2):
        result=await deliver_webhook(sales_payments,order,payment_id='provider-'+code)
        assert result.status==200 and result.text=='OK'
    u=await credit_db.get_user(101)
    assert (u['balance'],u['promo_image_balance'],u['free_animation_count'])==(
        0 if restricted else credits,credits if restricted else 0,bonus)
    counts=await credit_db.get_all_counters()
    assert counts['buy_promo_'+code]==1 and u['total_purchases']==1
    assert not counts.get('buy_credit_799') and not counts.get('buy_credit_199')
    stats=await credit_db.get_stats_by_source('yandex_newyear')
    assert stats['total_revenue']*100==amount
    await credit_db.init_db()
    assert (await credit_db.get_user(101))['promo_image_balance']==u['promo_image_balance']
    assert await credit_db.get_all_counters()==counts
    with pytest.raises(ValueError,match='завершена'):
        await sales_payments.create_promo1111_payment_link(101,code)


@pytest.mark.parametrize('change',[{'sum':'1.00'},{'_param_user_id':'999'},
    {'_param_package_id':'credits_15'},{'_param_credits':'999'},{'signature':'bad'},
    {'products':[{'name':'wrong','price':'111.00','quantity':'1'}]}])
async def test_invalid_paid_webhook_grants_nothing(credit_db,sales_payments,change):
    await activate();order=await promo.create_order(101,'111')
    await deliver_webhook(sales_payments,order,**change)
    assert (await credit_db.get_payment_order(order['order_id']))['status']=='pending'
    assert not await credit_db.get_user(101)
    assert not (await credit_db.get_all_counters()).get('buy_promo_111')


async def test_wallets_spend_only_on_images_after_campaign_ends(credit_db,campaign_clock):
    await paid(credit_db,'111')
    assert not await credit_db.deduct_credit(101)  # A generic/non-image consumer cannot spend it.
    await credit_db.add_credits(101,2)
    campaign_clock[0]=promo.DEADLINE+86400*30
    for _ in range(3):
        assert await credit_db.deduct_credit(101,allow_promo_images=True,count_generation=True,generation_event='gen_vision')
    u=await credit_db.get_user(101)
    assert (u['balance'],u['promo_image_balance'],u['total_generations'])==(2,0,3)
    assert await credit_db.deduct_credit(101,allow_promo_images=True)
    assert (await credit_db.get_user(101))['balance']==1
    counts=await credit_db.get_all_counters()
    assert counts['use_promo_image_credit']==3 and counts['gen_vision']==3


async def test_last_restricted_credit_is_atomic(credit_db):
    await paid(credit_db,'111')
    results=await asyncio.gather(*(credit_db.deduct_credit(101,allow_promo_images=True) for _ in range(8)))
    assert sum(results)==3
    assert (await credit_db.get_user(101))['promo_image_balance']==0
    assert (await credit_db.get_all_counters())['use_promo_image_credit']==3


async def test_bonus_reservation_queue_and_no_fake_sales(credit_db,campaign_clock):
    await paid(credit_db,'799')
    campaign_clock[0]=promo.DEADLINE+86400
    orders=await asyncio.gather(*(promo.claim_animation(101) for _ in range(5)))
    assert len({o['order_id'] for o in orders})==1
    order=orders[0]
    assert (order['status'],order['amount_kopecks'],order['quantity'],order['promo_bonus'])==('paid',0,1,1)
    assert await credit_db.animation_first_price(101)==199
    counts=await credit_db.get_all_counters()
    assert not counts.get('use_free_animation')
    job=await credit_db.queue_animation_photo(101,order['order_id'],'https://cdn.example/a.jpg','photo')
    assert await credit_db.queue_animation_photo(101,order['order_id'],'https://cdn.example/a.jpg','photo')==job
    with pytest.raises(ValueError,match='Нет доступного'):
        await promo.claim_animation(101)
    with pytest.raises(ValueError):
        await credit_db.queue_animation_photo(999,order['order_id'],'https://cdn.example/a.jpg','steal')
    u=await credit_db.get_user(101)
    assert (u['balance'],u['promo_image_balance'],u['free_animation_count'],u['total_purchases'])==(15,0,0,1)
    assert (await credit_db.get_all_counters())['use_free_animation']==1
    await credit_db.init_db()
    counts=await credit_db.get_all_counters()
    assert counts['buy_promo_799']==1 and not counts.get('buy_animation_promo1111_bonus')
    assert (await credit_db.create_animation_order(101,'one'))['amount_kopecks']==19900


@pytest.fixture
def media(isolated_example_directory,monkeypatch):
    for name in (*handlers.START_EXAMPLE_FILES[:4],promo.ANIMATION_EXAMPLE):
        (handlers.EXAMPLES_DIR/name).write_bytes(b'fixture')
    monkeypatch.setattr(handlers.asyncio,'sleep',AsyncMock())


async def test_broadcast_media_activation_and_repeat_safe(credit_db,media):
    for uid in (101,102,103):await credit_db.add_credits(uid,1)
    await credit_db.mark_user_inactive(102)
    e=event('/broadcast_1111');router=create_router(admin_user_id=456)
    async def send(**kwargs):
        assert not await promo.eligible(kwargs['user_id'])
        return object()
    e.bot.send_message.side_effect=send
    await call_handler(router,'broadcast_1111',e)
    assert [c.kwargs['user_id'] for c in e.bot.send_message.await_args_list]==[101]*3+[103]*3
    assert [Path(c.args[0].path).name for c in e.bot.upload_media.await_args_list]==[
        *handlers.START_EXAMPLE_FILES[:4],promo.ANIMATION_EXAMPLE]
    assert [c.args[0].type.value for c in e.bot.upload_media.await_args_list]==['image']*4+['video']
    messages=e.bot.send_message.await_args_list
    assert len(messages[0].kwargs['attachments'])==4
    assert messages[1].kwargs['attachments'][0].type.value=='video'
    assert messages[2].kwargs['text']==promo.TEXT
    button=messages[2].kwargs['attachments'][0].payload.buttons[0][0]
    assert (button.text,button.payload)==('🚀 Открыть меню','back_to_main')
    assert await promo.eligible(101) and await promo.eligible(103) and not await promo.eligible(102)
    assert 'Успешно: 2, Ошибок: 0' in e.message.answer.await_args.args[0]
    assert all(c.args==(0.5,) for c in handlers.asyncio.sleep.await_args_list)
    e.bot.send_message.reset_mock()
    await call_handler(router,'broadcast_1111',e)
    e.bot.send_message.assert_not_awaited()


@pytest.mark.parametrize('command',['broadcast_1111','broadcast_1111_preview'])
@pytest.mark.parametrize('admin',[None,999])
async def test_broadcast_admin_only(credit_db,media,command,admin):
    await credit_db.add_credits(101,1);e=event('/'+command)
    await call_handler(create_router(admin_user_id=admin),command,e)
    e.bot.send_message.assert_not_awaited();e.bot.upload_media.assert_not_awaited()
    assert not await promo.eligible(101)


async def test_broadcast_resumes_partial_delivery(credit_db,media):
    await credit_db.add_credits(101,1);e=event();router=create_router(admin_user_id=456)
    e.bot.send_message.side_effect=[object(),RuntimeError('failure')]
    await call_handler(router,'broadcast_1111',e)
    assert not await promo.eligible(101)
    assert (await promo.recipient(101))['next_message']==1
    assert 'Успешно: 0, Ошибок: 1' in e.message.answer.await_args.args[0]
    e.bot.send_message.reset_mock();e.bot.send_message.side_effect=None
    await call_handler(router,'broadcast_1111',e)
    assert e.bot.send_message.await_count==2
    assert e.bot.send_message.await_args_list[0].kwargs['attachments'][0].type.value=='video'
    assert await promo.eligible(101)


@pytest.mark.parametrize('status',[403,500,401,429])
async def test_broadcast_failures(credit_db,media,status,monkeypatch):
    for uid in (101,102):await credit_db.add_credits(uid,1)
    e=event();e.bot.send_message.side_effect=[MaxApiError(code=status,raw='failure')]+[object()]*3
    if status==403:
        monkeypatch.setattr(handlers,'mark_user_inactive',AsyncMock(side_effect=RuntimeError('database busy')))
    await call_handler(create_router(admin_user_id=456),'broadcast_1111',e)
    assert not await promo.eligible(101)
    assert e.bot.send_message.await_count==(1 if status in (401,429) else 4)
    assert await promo.eligible(102)==(status not in (401,429))
    assert 'Ошибок: 1' in e.message.answer.await_args.args[0]


async def test_missing_video_fallback_and_missing_photo_aborts(credit_db,media):
    await credit_db.add_credits(101,1);e=event();router=create_router(admin_user_id=456)
    (handlers.EXAMPLES_DIR/promo.ANIMATION_EXAMPLE).unlink()
    photo=handlers.EXAMPLES_DIR/handlers.START_EXAMPLE_FILES[0];photo.unlink()
    await call_handler(router,'broadcast_1111',e)
    e.bot.send_message.assert_not_awaited();assert not await promo.eligible(101)
    photo.write_bytes(b'fixture')
    await call_handler(router,'broadcast_1111',e)
    assert e.bot.send_message.await_args_list[1].kwargs['text']=='Пример скоро появится'
    assert await promo.eligible(101)


async def test_preview_only_admin_and_expired_broadcast_sends_nothing(credit_db,media,campaign_clock):
    await credit_db.add_credits(101,1);e=event()
    router=create_router(admin_user_id=456)
    await call_handler(router,'broadcast_1111_preview',e)
    assert e.message.answer.await_count==3
    e.bot.send_message.assert_not_awaited()
    assert await promo.eligible(456) and not await promo.eligible(101)
    campaign_clock[0]=promo.DEADLINE
    e.bot.upload_media.reset_mock()
    await call_handler(router,'broadcast_1111',e)
    e.bot.send_message.assert_not_awaited();e.bot.upload_media.assert_not_awaited()


async def test_buttons_dispatch_and_hide_after_expiry(credit_db,sales_payments,media,campaign_clock):
    await activate(456)
    router=create_router(payments=sales_payments);context=await ready_context()
    menu=await handlers.purchase_keyboard(456)
    buttons=[b for row in menu.payload.buttons for b in row]
    assert {b.payload for b in buttons}>={'buy_promo_111','buy_promo_333','buy_promo_799','buy_1','buy_15'}
    e=callback_event('buy_promo_111')
    await dispatch_callback(router,e,context)
    e.ack.assert_awaited_once();sales_payments._request_payment_link.assert_awaited_once()
    campaign_clock[0]=promo.DEADLINE
    await dispatch_callback(router,callback_event('buy_promo_333'),context)
    sales_payments._request_payment_link.assert_awaited_once()
    assert all(not b.payload.startswith('buy_promo_') for row in (await handlers.purchase_keyboard(456)).payload.buttons for b in row)


@pytest.mark.parametrize('failure',['none','timeout','bad-result','delivery'])
async def test_image_flow_uses_restricted_wallet_without_double_charge(credit_db,monkeypatch,failure):
    from test_handlers import message_event
    from image_service import GenerationTimeoutError
    from maxapi.types import AttachmentUpload
    # Explicit patches: importing fixture names inside a function does not install them.
    monkeypatch.setattr(handlers,'fix_text_with_ai',AsyncMock(side_effect=lambda text:text))
    monkeypatch.setattr(handlers,'upload_image_for_max',AsyncMock(return_value=AttachmentUpload(type='image',payload={'token':'test'})))
    await paid(credit_db,'111',user=456)
    generator=AsyncMock(return_value=['https://storage.example.com/photo.png'])
    if failure=='timeout': generator.side_effect=GenerationTimeoutError('test')
    if failure=='bad-result':generator.return_value=['invalid-url']
    router=create_router(generator);e=message_event();context=await ready_context()
    if failure=='delivery':e.bot.send_message.side_effect=RuntimeError('MAX unavailable')
    await call_handler(router,'collect_sphere',e,context)
    user=await credit_db.get_user(456)
    assert user['balance']==0
    assert user['promo_image_balance']==(3 if failure in ('timeout','bad-result') else 2)
    if failure=='delivery':
        e.bot.send_message.reset_mock(side_effect=True)
        await call_handler(router,'collect_sphere',e,context)
        generator.assert_awaited_once()
        assert (await credit_db.get_user(456))['promo_image_balance']==2


async def test_animation_example_before_offer_and_gift_dispatch(credit_db,media):
    await paid(credit_db,'111',user=456)
    await paid(credit_db,'799',user=456,receipt='bonus')
    e=callback_event('cat_animation');context=await ready_context();router=create_router()
    e.bot.upload_media=event().bot.upload_media
    await dispatch_callback(router,e,context)
    replies=e.send.await_args_list
    assert replies[0].kwargs['attachments'][0].type.value=='video'
    assert 'Оживление фотографий' in replies[1].args[0]
    assert any(b.payload=='animation_free_1111' for row in replies[1].kwargs['attachments'][0].payload.buttons for b in row)
    gift=callback_event('animation_free_1111')
    await dispatch_callback(router,gift,context)
    assert (await credit_db.get_user(456))['free_animation_count']==0
    assert (await credit_db.get_user(456))['balance']==15
    assert 'фотограф' in gift.send.await_args.args[0].lower()


@pytest.mark.parametrize('payload',['cat_audio','cat_photo','cat_animation'])
async def test_non_image_services_explain_restrictions_without_spending(credit_db,media,payload):
    await paid(credit_db,'111',user=456)
    e=callback_event(payload)
    await dispatch_callback(create_router(),e,await ready_context())
    assert any(c.args and c.args[0]==promo.IMAGE_ONLY for c in e.send.await_args_list)
    assert (await credit_db.get_user(456))['promo_image_balance']==3


@pytest.mark.parametrize('command',['broadcast_1111','broadcast_1111_preview'])
async def test_real_dispatcher_campaign_commands(credit_db,media,monkeypatch,event_replies,command):
    from maxapi import Dispatcher
    from maxapi.context import MemoryContext
    from maxapi.context.isolation import SimpleEventIsolation
    from maxapi.types import MessageCreated,User
    from types import SimpleNamespace
    monkeypatch.setattr(handlers,'_reply',event_replies)
    await credit_db.add_credits(101,1);sample=event()
    bot=SimpleNamespace(me=User(user_id=9999,first_name='Bot',is_bot=True),
                        send_message=sample.bot.send_message,upload_media=sample.bot.upload_media)
    dp=Dispatcher(storage=MemoryContext,event_isolation=SimpleEventIsolation())
    dp.include_routers(create_router(admin_user_id=456))
    incoming=MessageCreated(timestamp=1,bot=bot,message={
        'sender':{'user_id':456,'first_name':'Admin','is_bot':False},
        'recipient':{'chat_id':123,'chat_type':'dialog'},'timestamp':1,
        'body':{'mid':'campaign','seq':1,'text':'/'+command,'attachments':[]}})
    try:
        await dp.handle(incoming)
        assert await promo.eligible(456)
        assert await promo.eligible(101)==(command=='broadcast_1111')
        announcements=[c.kwargs for c in bot.send_message.await_args_list if c.kwargs.get('text')==promo.TEXT]
        assert [c['user_id'] for c in announcements]==([101,456] if command=='broadcast_1111' else [456])
    finally:await dp.shutdown()

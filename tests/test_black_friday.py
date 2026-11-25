import asyncio
from unittest.mock import AsyncMock
from urllib.parse import parse_qsl,urlsplit

import pytest
from maxapi.exceptions import MaxApiError

import black_friday as friday
import black_friday_broadcast as campaign
import database
import ded_moroz_sales as sales
import handlers
from prodamus import parse_form_fields
from test_prodamus import payments,payment_config
from test_handlers import message_event,callback_event,call_handler,dispatch_callback,ready_context,report_text,event_replies


@pytest.fixture
def sale(monkeypatch):
    monkeypatch.setattr(friday,'now',lambda:friday.START+60)
    monkeypatch.setattr(campaign.asyncio,'sleep',AsyncMock())


@pytest.mark.parametrize('timestamp,enabled',[(friday.START-1,False),(friday.START,True),(friday.END-1,True),(friday.END,False)])
def test_moscow_boundaries(timestamp,enabled):
    assert friday.active(timestamp)==enabled


def receipt(link,key):
    f=parse_form_fields(parse_qsl(urlsplit(link).query,keep_blank_values=True))
    return dict(order_num=key,order_id='paid-'+key,sum=f['products'][0]['price'],products=f['products'],
                _param_package_id=f['_param_package_id'],_param_user_id=f['_param_user_id'],_param_credits=f['_param_credits'])


@pytest.mark.parametrize('kind,tariff,base',[
    ('audio','fun',15900),('audio','gift',29000),('photo',590,49000),('photo',990,69000),('photo',1490,99000),
    ('cartoon','short',39000),('cartoon','mother',59000),('cartoon','mother_bday',59000),
    ('musical','direct',29900),('musical','upsell',14900),('animation','one',19900),('animation','three',59900),('animation','five',89900),
    ('ded_moroz','single',149000),('ded_moroz','family',149000)])
async def test_all_new_product_invoices_discount_once_and_validate_after_expiry(credit_db,payments,sale,monkeypatch,kind,tariff,base):
    if kind=='audio':
        key='audio-bf-'+tariff
        await database.create_audio_order(key,456,tariff,'birthday','Маме',None,'Слова','pop','Мама')
        row=await database.get_audio_order(key)
        link=await payments.create_audio_payment_link(456,tariff,'birthday','Маме',None,'Слова','pop','Мама',order_id=key)
        mark=database.mark_audio_paid
    elif kind=='photo':
        key=await database.create_photo_order('photo_bf_'+str(tariff),456,tariff);row=await database.get_photo_order(key);mark=database.mark_photo_paid
    elif kind=='cartoon':
        key=await database.create_cartoon_order('cartoon_bf_'+tariff,456,'theme_'+('ny' if tariff=='short' else tariff),tariff)
        row=await database.get_cartoon_order(key);mark=database.mark_cartoon_paid
    elif kind=='musical':
        key='music_bf_'+tariff
        key=await database.create_musical_order(key,456,'https://cdn.example/photo.png','https://cdn.example/song.mp3',tariff)
        row=await database.get_musical_order(key);mark=database.mark_musical_paid
    elif kind=='animation':
        row=await database.create_animation_order(456,tariff);key=row['order_id'];mark=database.mark_animation_paid
    else:
        monkeypatch.setattr(database,'_now_ts',lambda:friday.START+60)
        row=await sales.checkout(456,variant=tariff,children_count=2 if tariff=='family' else None)
        key=row['order_id'];mark=sales.mark_paid
        assert (await sales.checkout(456,variant=tariff,children_count=2 if tariff=='family' else None))['order_id']==key
    assert row['amount_kopecks']==friday.discount(base)
    assert row['sale_base_amount']==base and row['sale_campaign']==friday.CAMPAIGN
    if kind!='audio':link=await payments.create_payment_link(456,order_id=key,amount=row['amount_kopecks']//100,description=row['product_name'])
    r=receipt(link,key)
    monkeypatch.setattr(friday,'now',lambda:friday.END+1)
    validated,pid=await payments.validate_payment(r)
    assert validated['amount_kopecks']==friday.discount(base)
    assert await mark(key,pid)
    assert not await mark(key,pid)
    counters=await database.get_all_counters()
    assert counters['black_friday_paid']==1
    if kind=='photo':assert await database.get_photo_package_balance(key)==database.PHOTO_PACKAGE_SIZES[tariff]
    # A signature-valid notification still cannot change the persisted amount.
    r['sum']=str(base//100)
    with pytest.raises(ValueError):await payments.validate_payment(r)


@pytest.mark.parametrize('credits,base',[(1,9900),(3,19900),(5,29900),(15,79900)])
async def test_credit_invoice_discount_preserves_package_and_receipt(credit_db,payments,sale,monkeypatch,credits,base):
    link=await payments.create_payment_link(456,credits)
    f=parse_form_fields(parse_qsl(urlsplit(link).query,keep_blank_values=True));key=f['order_id']
    row=await database.get_payment_order(key)
    assert row['amount_kopecks']==friday.discount(base)
    assert row['credits']==credits
    monkeypatch.setattr(friday,'now',lambda:friday.END)
    order,_=await payments.validate_payment(receipt(link,key))
    assert order['package_id']=='package_'+str(credits)
    assert await database.add_credits(456,credits,payment_order_id=key,provider_payment_id='paid-'+key)
    assert not await database.add_credits(456,credits,payment_order_id=key,provider_payment_id='paid-'+key)
    assert (await database.get_user(456))['balance']==credits
    assert (await database.get_all_counters())['black_friday_paid']==1


async def test_personal_discount_not_stacked_and_old_order_not_mutated(credit_db,sale,monkeypatch):
    monkeypatch.setattr(friday,'now',lambda:friday.START-1)
    key=await database.create_photo_order('photo_before',456,590)
    before=await database.get_photo_order(key)
    monkeypatch.setattr(friday,'now',lambda:friday.START)
    assert await database.create_photo_order(key,456,590)==key
    assert await database.get_photo_order(key)==before
    await database.set_promo(456,'photo')
    key=await database.create_photo_order('photo_repeat_bf',456,590)
    row=await database.get_photo_order(key)
    assert row['amount_kopecks']==39200 and row['promo_token'] and not row['sale_campaign']
    await database.set_promo(456,'cartoon')
    key=await database.create_cartoon_order('cartoon_repeat_bf',456,'theme_ny','short')
    assert (await database.get_cartoon_order(key))['amount_kopecks']==31200


async def test_resumed_audio_does_not_reprice_or_double_discount(credit_db,sale):
    args=('audio-resume',456,'gift','birthday','Маме',None,'Слова','pop','Мама')
    await database.create_audio_order(*args);await database.create_audio_order(*args)
    row=await database.get_audio_order(args[0]);assert row['amount_kopecks']==23200


async def test_subscriber_preview_admin_confirmation_and_delivery_recovery(credit_db,sale):
    for uid in (101,102,103):await database.add_credits(uid,1)
    await database.mark_user_inactive(102)
    e=message_event('/broadcast_black_friday');router=handlers.create_router(admin_user_id=456)
    await call_handler(router,'broadcast_black_friday_preview',e)
    e.bot.send_message.assert_not_awaited()
    assert e.message.answer.await_args_list[0].args[0]==campaign.TEXT
    assert 'black_friday_broadcast_confirm' in str(e.message.answer.await_args_list)
    e.bot.send_message=AsyncMock(side_effect=[object(),MaxApiError(code=429,raw='limited')])
    await campaign.send(e,asyncio.Lock())
    e.bot.send_message=AsyncMock(return_value=object())
    await campaign.send(e,asyncio.Lock())
    assert [c.kwargs['user_id'] for c in e.bot.send_message.await_args_list]==[103]
    e.bot.send_message.reset_mock();await campaign.send(e,asyncio.Lock());e.bot.send_message.assert_not_awaited()
    assert 'доставлено всего — 2' in str(e.message.answer.await_args_list)


async def test_unknown_delivery_is_not_automatically_duplicated(credit_db,sale):
    await database.add_credits(101,1);e=message_event('')
    e.bot.send_message=AsyncMock(return_value=None)
    await campaign.send(e,asyncio.Lock());await campaign.send(e,asyncio.Lock())
    assert e.bot.send_message.await_count==1
    assert 'требуют проверки — 1' in str(e.message.answer.await_args_list)


async def test_non_admin_cannot_preview_or_send_and_expiry_stops_delivery(credit_db,sale,monkeypatch):
    e=message_event('/broadcast_black_friday')
    await call_handler(handlers.create_router(admin_user_id=999),'broadcast_black_friday_preview',e)
    e.message.answer.assert_not_awaited();e.bot.send_message.assert_not_awaited()
    await database.add_credits(101,1)
    monkeypatch.setattr(friday,'now',lambda:friday.END)
    await campaign.send(e,asyncio.Lock());e.bot.send_message.assert_not_awaited()


async def test_main_screen_notice_and_menu_prices_expire(credit_db,sale,monkeypatch):
    ctx=await ready_context();e=callback_event('black_friday_menu')
    await dispatch_callback(handlers.create_router(admin_user_id=999),e,ctx)
    assert friday.NOTICE in report_text(e.send)
    assert (await database.get_all_counters())['black_friday_click']==1
    assert '392 ₽' in friday.menu_text(handlers.PHOTO_TARIFF_TEXT)
    monkeypatch.setattr(friday,'now',lambda:friday.END)
    ctx=await ready_context();e=callback_event('back_to_main')
    await dispatch_callback(handlers.create_router(admin_user_id=999),e,ctx)
    assert friday.NOTICE not in report_text(e.send)
    assert friday.menu_text(handlers.PHOTO_TARIFF_TEXT)==handlers.PHOTO_TARIFF_TEXT


@pytest.mark.parametrize('field,value',[('sale_campaign','fake'),('sale_base_amount',29100),('sale_created_at',friday.END)])
async def test_corrupted_campaign_snapshot_rejected(credit_db,payments,sale,field,value):
    key=await database.create_photo_order('photo_bad_campaign',456,590)
    row=await database.get_photo_order(key)
    link=await payments.create_payment_link(456,order_id=key,amount=row['amount_kopecks']//100,description=row['product_name'])
    import aiosqlite
    async with aiosqlite.connect(database.DATABASE_PATH) as db:
        await db.execute('UPDATE photo_orders SET '+field+'=? WHERE order_id=?',(value,key));await db.commit()
    with pytest.raises(ValueError):await payments.validate_payment(receipt(link,key))


async def test_expiry_new_order_normal_price_and_before_sale_invoice_still_valid(credit_db,payments,monkeypatch):
    monkeypatch.setattr(friday,'now',lambda:friday.START-1)
    key=await database.create_photo_order('photo_old_invoice',456,590)
    row=await database.get_photo_order(key)
    link=await payments.create_payment_link(456,order_id=key,amount=490,description=row['product_name'])
    monkeypatch.setattr(friday,'now',lambda:friday.START)
    validated,_=await payments.validate_payment(receipt(link,key));assert validated['amount_kopecks']==49000
    monkeypatch.setattr(friday,'now',lambda:friday.END)
    key=await database.create_photo_order('photo_after_invoice',456,590)
    row=await database.get_photo_order(key);assert row['amount_kopecks']==49000 and not row['sale_campaign']


async def test_coupon_credit_price_better_than_campaign_not_discounted_again(credit_db,payments,sale):
    await database.add_credits(456,1)
    # Activate the existing introductory 149-ruble offer through the users snapshot.
    import aiosqlite
    async with aiosqlite.connect(database.DATABASE_PATH) as db:
        await db.execute('UPDATE users SET promo_expires_at=?,promo_campaign_id=? WHERE user_id=?',(database._now_ts()+3600,'bf-test',456));await db.commit()
    link=await payments.create_payment_link(456,3,promo=True)
    fields=parse_form_fields(parse_qsl(urlsplit(link).query,keep_blank_values=True))
    row=await database.get_payment_order(fields['order_id'])
    assert row['amount_kopecks']==14900 and not row['sale_campaign']


async def test_catalogue_buttons_and_payment_sum_match(credit_db,sale):
    ctx=await ready_context();e=callback_event('photo_style_ny')
    await dispatch_callback(handlers.create_router(admin_user_id=999),e,ctx)
    assert '392 ₽' in report_text(e.send) and '552 ₽' in report_text(e.send)
    buttons=e.send.await_args.kwargs['attachments'][0].payload.buttons
    assert [r[0].text for r in buttons[:3]]==['Тариф «Попробовать» (392 ₽)','Тариф «Фотосессия» (552 ₽)','Тариф «Премиум» (792 ₽)']
    ctx=await ready_context();e=callback_event('cat_cartoon')
    await dispatch_callback(handlers.create_router(admin_user_id=999),e,ctx)
    assert '472 ₽' in str(e.send.await_args) and '312 ₽' in str(e.send.await_args)


@pytest.mark.parametrize('status',[403,500])
async def test_failed_recipient_does_not_stop_next_subscriber(credit_db,sale,status):
    for user in (101,103):await database.add_credits(user,1)
    e=message_event('');e.bot.send_message=AsyncMock(side_effect=[MaxApiError(code=status,raw='failed'),object()])
    await campaign.send(e,asyncio.Lock())
    assert [c.kwargs['user_id'] for c in e.bot.send_message.await_args_list]==[101,103]
    assert (await database.get_user(101))['is_active']==(0 if status==403 else 1)


async def test_admin_callback_is_guarded_and_uses_campaign_button(credit_db,sale):
    router=handlers.create_router(admin_user_id=999);ctx=await ready_context()
    e=callback_event('black_friday_broadcast_confirm')
    await dispatch_callback(router,e,ctx);e.bot.send_message.assert_not_awaited()
    assert campaign.keyboard().payload.buttons[0][0].text=='🎁 Выбрать подарок'
    assert campaign.keyboard().payload.buttons[0][0].payload=='black_friday_menu'


async def test_campaign_stats_rebuild_preserves_base_product_identity(credit_db,sale):
    key=await database.create_photo_order('photo_stats_bf',456,590)
    await database.mark_photo_paid(key,'bf-stats')
    before=await database.get_all_counters();assert before['buy_photo_trial_490']==1
    import aiosqlite
    async with aiosqlite.connect(database.DATABASE_PATH) as db:
        await db.execute('DELETE FROM global_counters');await db.execute('DELETE FROM user_counters')
        await database._backfill_sale_counters(db,force=True);await db.commit()
    after=await database.get_all_counters()
    assert after['black_friday_paid']==1 and after['buy_photo_trial_490']==1

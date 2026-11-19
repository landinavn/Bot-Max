"""Family sale: exact entitlements, Moscow boundaries and reviewed campaign."""
import asyncio
import json
from datetime import datetime
from zoneinfo import ZoneInfo
from types import SimpleNamespace
from unittest.mock import AsyncMock
from urllib.parse import parse_qsl,urlsplit

import pytest
import database
import ded_moroz as dm
import ded_moroz_sales as sales
import ded_moroz_family as family
import ded_moroz_family_broadcast as campaign
from handlers import create_router,PosterStates
from prodamus import parse_form_fields
from test_prodamus import payments,payment_config
from test_ded_moroz import setup_dm
from test_ded_moroz_family import details
from test_ded_moroz_customer_flow import media,customer_setup
from test_handlers import event_replies,callback_event,ready_context,dispatch_callback,message_event,call_handler


def stamp(day,hour=12,minute=0,second=0,month=11):
    return datetime(2026,month,day,hour,minute,second,tzinfo=ZoneInfo('Europe/Moscow')).timestamp()


@pytest.mark.parametrize('when,expected',[(stamp(19),99000),(stamp(22,23,59,59),99000),(stamp(23,0),149000),(stamp(30,23,59,59),149000),(stamp(1,0,month=12),199000)])
def test_family_prices(when,expected):
    assert sales.family_price_at(when)==expected
    if expected!=99000:assert '— 990 ₽' not in sales.family_price_text(when)
    if when==stamp(19):assert sales.price_at(when)==149000


def test_questions_are_numbered_and_only_fact_fields_show_limits():
    d=details()
    assert family.question('child_1_name',{'variant':'family','children_count':2})==('Заполняем детей по очереди в том порядке, в котором их поздравит Дед Мороз.\nРебёнок 1 из 2\n\nКак его или её зовут дома? Напишите одно имя, например «Машенька» или «Саша».')
    for n in range(1,3):
        for part in ('name','age','gender'):assert 'символов' not in family.question(f'child_{n}_{part}',d)
        for part in ('fact','wish'):assert 'До 45 символов и 6 слов.' in family.question(f'child_{n}_{part}',d)
    for field in family.labels(details(6)):
        assert 'Для 4–6' not in family.question(field,details(6))


async def test_family_invoice_exact_price_count_and_idempotent_grant(credit_db,monkeypatch):
    monkeypatch.setattr(database,'_now_ts',lambda:stamp(19))
    invoice=await sales.checkout(789,variant='family',children_count=2)
    assert not await dm.read('SELECT * FROM ded_moroz_orders')
    assert invoice==await sales.checkout(789,variant='family',children_count=2)
    assert (await sales.checkout(789,variant='family',children_count=3))['order_id']!=invoice['order_id']
    single=await sales.checkout(789)
    assert single['amount_kopecks']==149000 and single['order_id']!=invoice['order_id']
    assert await sales.mark_paid(invoice['order_id'],'family-payment')
    assert not await sales.mark_paid(invoice['order_id'],'family-payment')
    row=await sales.claim(789,456,payment_key=invoice['order_id'])
    assert json.loads(row['details'])=={'variant':'family','children_count':2}
    assert await sales.allowed(row,456)
    assert not await sales.allowed(dict(row,user_id=456),999)
    assert not await sales.allowed(dict(row,details=json.dumps({'variant':'family','children_count':3})),456)
    counters=await database.get_all_counters()
    assert counters['buy_ded_moroz_family_990']==1
    assert not counters.get('buy_ded_moroz_990',0)
    assert (await sales.statistics(456,'family'))['paid']==1
    assert (await sales.statistics(456))['paid']==0
    await credit_db.init_db()
    assert (await database.get_all_counters())['buy_ded_moroz_family_990']==1
    assert (await database.get_user(789))['balance']==0


@pytest.mark.parametrize('n',[None,1,7,True,'2'])
async def test_invalid_counts_never_create_invoices(credit_db,n):
    with pytest.raises(ValueError):await sales.checkout(789,variant='family',children_count=n)
    assert not await dm.read('SELECT * FROM ded_moroz_payments')


async def test_payment_link_and_receipt_preserve_family_product(credit_db,payments,monkeypatch):
    monkeypatch.setattr(database,'_now_ts',lambda:stamp(19))
    row=await sales.checkout(789,variant='family',children_count=2)
    link=await payments.create_payment_link(789,order_id=row['order_id'],amount=990,description=row['product_name'])
    form=parse_form_fields(parse_qsl(urlsplit(link).query,keep_blank_values=True))
    assert 'dm_paid_'+row['order_id'] in form['urlReturn']
    data={'order_num':row['order_id'],'order_id':'family-receipt','sum':'990.00','currency':'rub','payment_status':'success','products':form['products'],
          '_param_user_id':'789','_param_credits':'0','_param_package_id':row['package_id']}
    validated=await payments.validate_payment(data)
    assert validated[0]['variant']=='family'
    data['_param_package_id']='ded_moroz_990'
    with pytest.raises(ValueError):await payments.validate_payment(data)


async def test_paid_family_enters_questions_and_can_edit_facts(credit_db,monkeypatch):
    monkeypatch.setattr(database,'_now_ts',lambda:stamp(19))
    invoice=await sales.checkout(456,variant='family',children_count=2)
    await sales.mark_paid(invoice['order_id'],'family-edit')
    row=await sales.claim(456,999,payment_key=invoice['order_id'])
    context=await ready_context();router=create_router(admin_user_id=999)
    await dispatch_callback(router,callback_event('dm_family_paid:'+invoice['order_id']),context)
    assert (await context.get_data())['dm_order']==row['id']
    assert await context.get_state()==PosterStates.ded_moroz_input
    await dm.update(row['id'],details=json.dumps(details()),status='audio_ready',text=family.tagged_text(details()),audio_path='first.mp3',audio_url='https://test/first.mp3')
    assert await dm.request_edit(row['id'],456,0,admin=999)
    assert await dm.select_fact(row['id'],456,0,'child_2_fact')
    await dm.revised(row['id'],456,'любишь баскетбол',admin=999)
    assert await dm.apply_facts(row['id'],456,0,admin=999)
    changed=await dm.get(row['id'])
    assert changed['regeneration_attempts']==1 and 'любишь баскетбол' in changed['text'] and '[drawls]' in changed['text']


async def test_public_choice_only_creates_checkout_not_free_order(credit_db,monkeypatch):
    mocked=AsyncMock();monkeypatch.setattr(family,'checkout',mocked)
    context=await ready_context();event=callback_event('dm_family_count:2')
    await dispatch_callback(create_router(admin_user_id=999),event,context)
    mocked.assert_awaited_once()
    assert not await dm.read('SELECT * FROM ded_moroz_orders')


async def test_unpaid_family_cannot_skip_to_generation(credit_db):
    row=await dm.begin(456,details=details())
    e=callback_event('dm_family_no_photo:'+row['id'])
    await dispatch_callback(create_router(admin_user_id=999),e,await ready_context())
    assert (await dm.get(row['id']))['status']=='input'


async def test_missing_example_blocks_campaign_but_offer_shows_placeholder(credit_db,tmp_path,monkeypatch):
    monkeypatch.setattr(family,'EXAMPLE',tmp_path/'absent.mp4')
    event=callback_event('cat_ded_moroz_family')
    await dispatch_callback(create_router(admin_user_id=999),event,await ready_context())
    assert 'скоро появится' in str(event.send.await_args_list)
    with pytest.raises(ValueError):await campaign.messages(event.bot)


async def test_campaign_preview_and_resumption(credit_db,monkeypatch):
    monkeypatch.setattr(database,'_now_ts',lambda:stamp(19))
    await database.grant_free_trial(456)
    monkeypatch.setattr(database,'get_active_user_ids',AsyncMock(return_value=[456]))
    monkeypatch.setattr(family,'example_attachment',AsyncMock(return_value=SimpleNamespace(type='video')))
    monkeypatch.setattr(campaign.asyncio,'sleep',AsyncMock())
    event=callback_event('dm_family_broadcast_confirm')
    event.bot.send_message=AsyncMock(return_value=object())
    content=await campaign.messages(event.bot)
    assert '990 ₽' in content[1][0] and '22 ноября' in content[1][0]
    assert 'dm_family_launch' in str(content[1][1]) and 'back_to_main' in str(content[1][1])
    await campaign.send(event,asyncio.Lock())
    assert event.bot.send_message.await_count==2
    await campaign.send(event,asyncio.Lock())
    assert event.bot.send_message.await_count==2
    monkeypatch.setattr(database,'_now_ts',lambda:stamp(23,0))
    await campaign.send(event,asyncio.Lock())
    assert event.bot.send_message.await_count==2


@pytest.mark.parametrize('bad',['signature','amount','product','package',None])
async def test_signed_family_webhook_without_network(credit_db,payments,monkeypatch,bad):
    from webhook import create_webhook_app
    from prodamus import sign_payload
    monkeypatch.setattr(database,'_now_ts',lambda:stamp(19))
    row=await sales.checkout(789,variant='family',children_count=3)
    data={'order_num':row['order_id'],'order_id':'signed-family','sum':'990.00','currency':'rub','payment_status':'success',
          'products':[{'name':row['product_name'],'price':'990.00','quantity':'1'}],
          '_param_user_id':'789','_param_credits':'0','_param_package_id':row['package_id']}
    if bad=='amount':data['sum']='1.00'
    if bad=='product':data['products'][0]['name']=sales.PRODUCT
    if bad=='package':data['_param_package_id']='ded_moroz_990'
    signature='0'*64 if bad=='signature' else sign_payload(data,payments.secret)
    request=SimpleNamespace(content_type='application/json',headers={'Sign':signature},text=AsyncMock(return_value=json.dumps(data)))
    app=create_webhook_app(payments)
    handler=next(r.handler for r in app.router.routes() if r.method=='POST')
    response=await handler(request)
    assert response.status==(403 if bad=='signature' else 400 if bad else 200)
    assert bool(await dm.read('SELECT * FROM ded_moroz_orders'))==(bad is None)
    if bad is None:
        assert (await handler(request)).status==200
        assert len(await dm.read('SELECT * FROM ded_moroz_orders',all_rows=True))==1


async def test_customer_paid_family_audio_uses_saved_take_for_video(credit_db,setup_dm,media,monkeypatch):
    from test_ded_moroz import step
    monkeypatch.setattr(database,'_now_ts',lambda:stamp(19))
    _,_,worker=setup_dm
    video,speech,_=media
    invoice=await sales.checkout(789,variant='family',children_count=2)
    await sales.mark_paid(invoice['order_id'],'family-pipeline')
    row=await sales.claim(789,456,payment_key=invoice['order_id']);key=row['id']
    d=details();d['family_confirmed']=True
    await dm.update(key,details=json.dumps(d));assert await family.queue_audio(await dm.get(key))
    async def generate(text,*,job_id,version):
        return (await worker.assets.write(speech.read_bytes(),'mp3',job_id=job_id,name=f'speech-v{version}.mp3'))[0]
    worker.tts.generate_speech.side_effect=generate
    for _ in range(4):await step(worker,key)
    take=await dm.read('SELECT * FROM ded_moroz_audio_versions WHERE order_id=?',(key,))
    assert take['duration']>0
    assert await dm.choose_audio(key,789,0,0,admin=456)
    await step(worker,key)
    worker.heygen.create_video.assert_awaited_once()
    assert worker.heygen.create_video.await_args.args[1]==take['public_url']
    assert worker.tts.generate_speech.await_count==1


async def test_broadcast_admin_gate_and_expiry_between_messages(credit_db,monkeypatch):
    monkeypatch.setattr(database,'_now_ts',lambda:stamp(22,23,59,59))
    await database.grant_free_trial(456)
    monkeypatch.setattr(database,'get_active_user_ids',AsyncMock(return_value=[456]))
    monkeypatch.setattr(family,'example_attachment',AsyncMock(return_value=SimpleNamespace(type='video')))
    event=callback_event('dm_family_broadcast_confirm')
    await dispatch_callback(create_router(admin_user_id=999),event,await ready_context())
    event.bot.send_message.assert_not_awaited()
    async def advance(_):monkeypatch.setattr(database,'_now_ts',lambda:stamp(23,0))
    monkeypatch.setattr(campaign.asyncio,'sleep',advance)
    await campaign.send(event,asyncio.Lock())
    assert event.bot.send_message.await_count==1
    row=await dm.read('SELECT * FROM ded_moroz_broadcast WHERE campaign=?',(campaign.CAMPAIGN,))
    assert row['next_message']==1 and not row['completed']


async def test_customer_checkout_before_inputs_and_resume_exact_invoice(credit_db,customer_setup,payments,monkeypatch):
    monkeypatch.setattr(database,'_now_ts',lambda:stamp(19))
    worker,router=customer_setup
    context=await ready_context()
    event=callback_event('dm_family_count:2')
    await dispatch_callback(router,event,context)
    invoice=await dm.read('SELECT * FROM ded_moroz_payments')
    assert invoice['variant']=='family' and invoice['children_count']==2 and invoice['amount_kopecks']==99000
    assert 'Оплатить 990' in str(event.send.await_args_list)
    assert not await dm.read('SELECT * FROM ded_moroz_orders')
    worker.tts.generate_speech.assert_not_awaited()
    await sales.mark_paid(invoice['order_id'],'family-checkout-ui')
    await dispatch_callback(router,callback_event('dm_family_paid:'+invoice['order_id']),context)
    row=await dm.get((await context.get_data())['dm_order'])
    assert row['payment_order_id']==invoice['order_id'] and json.loads(row['details'])['children_count']==2
    worker.tts.generate_speech.assert_not_awaited()


async def test_campaign_retry_only_sends_missing_message(credit_db,monkeypatch):
    monkeypatch.setattr(database,'_now_ts',lambda:stamp(19))
    await database.grant_free_trial(456)
    monkeypatch.setattr(database,'get_active_user_ids',AsyncMock(return_value=[456]))
    monkeypatch.setattr(family,'example_attachment',AsyncMock(return_value=SimpleNamespace(type='video')))
    monkeypatch.setattr(campaign.asyncio,'sleep',AsyncMock())
    event=callback_event('dm_family_broadcast_confirm')
    event.bot.send_message=AsyncMock(side_effect=[object(),TimeoutError()])
    await campaign.send(event,asyncio.Lock())
    row=await dm.read('SELECT * FROM ded_moroz_broadcast WHERE campaign=?',(campaign.CAMPAIGN,))
    assert row['next_message']==1 and not row['completed']
    event.bot.send_message=AsyncMock(return_value=object())
    await campaign.send(event,asyncio.Lock())
    event.bot.send_message.assert_awaited_once()
    assert 'Новинка:' in event.bot.send_message.await_args.kwargs['text']

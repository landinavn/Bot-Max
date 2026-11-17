import asyncio
from datetime import datetime
from urllib.parse import parse_qsl,urlsplit
from zoneinfo import ZoneInfo

import pytest

import database
import ded_moroz as dm
import ded_moroz_sales as sales
from prodamus import parse_form_fields
from test_prodamus import payments,payment_config,client,post_notification


def stamp(month,day,hour=0,minute=0,second=0,year=2026):
    return int(datetime(year,month,day,hour,minute,second,tzinfo=ZoneInfo('Europe/Moscow')).timestamp())


@pytest.fixture(autouse=True)
def campaign_clock(monkeypatch):
    monkeypatch.setattr(database,'_now_ts',lambda:stamp(11,16))
    monkeypatch.setenv('DED_MOROZ_PUBLIC_ENABLED','1')


@pytest.mark.parametrize('when,price',[
    (stamp(11,14,23,59,59),None),(stamp(11,15),99000),(stamp(11,18,23,59,59),99000),
    (stamp(11,19),149000),(stamp(11,30,23,59,59),149000),
    (stamp(12,1),199000),(stamp(12,31,23,59,59),199000),(stamp(1,1,year=2027),None),
])
def test_prices_at_exact_moscow_boundaries(when,price):assert sales.price_at(when)==price


@pytest.mark.parametrize('user',[True,0,-1,'789'])
async def test_checkout_requires_real_positive_user_id(credit_db,user):
    with pytest.raises(ValueError):await sales.checkout(user)
    assert not await dm.read('SELECT * FROM ded_moroz_payments')


async def receipt(payments,*,user=789,payment_id='provider-dm-1'):
    row=await sales.checkout(user)
    link=await payments.create_payment_link(user,order_id=row['order_id'],amount=row['amount_kopecks']//100,description=row['product_name'])
    data=parse_form_fields(parse_qsl(urlsplit(link).query,keep_blank_values=True))
    return {'order_num':row['order_id'],'order_id':payment_id,'sum':str(row['amount_kopecks']//100)+'.00',
        'currency':'rub','payment_status':'success','products':data['products'],
        '_param_user_id':str(user),'_param_credits':'0','_param_package_id':row['package_id']}


@pytest.mark.parametrize('encoding',['form','json','multipart'])
async def test_signed_payment_is_idempotent_and_grants_one_service_not_credits(credit_db,payments,client,encoding):
    data=await receipt(payments)
    assert not await dm.read('SELECT * FROM ded_moroz_orders')
    for _ in range(3):
        response=await post_notification(client,data,encoding=encoding)
        assert response.status==200
    row=await sales.payment(data['order_num'])
    assert row['status']=='paid' and row['claimed_id']
    orders=await dm.read('SELECT * FROM ded_moroz_orders',all_rows=True)
    assert len(orders)==1 and orders[0]['status']=='input' and orders[0]['user_id']==789
    assert await sales.allowed(orders[0],456)
    user=await database.get_user(789)
    assert user['balance']==0 and user['total_purchases']==1
    counters=await database.get_all_counters()
    assert counters['buy_ded_moroz']==1 and counters['buy_ded_moroz_990']==1


@pytest.mark.parametrize('bad',['sum','product','user','package','credits','signature'])
async def test_wrong_receipts_do_not_unlock_customer(credit_db,payments,client,bad):
    data=await receipt(payments)
    if bad=='sum':data['sum']='1.00'
    if bad=='product':data['products'][0]['name']='Другая услуга'
    if bad=='user':data['_param_user_id']='456'
    if bad=='package':data['_param_package_id']='package_15'
    if bad=='credits':data['_param_credits']='15'
    response=await post_notification(client,data,signature=('0'*64 if bad=='signature' else None))
    assert response.status in (400,403)
    assert (await sales.payment(data['order_num']))['status']=='pending'
    assert not await dm.read('SELECT * FROM ded_moroz_orders')


async def test_malformed_optional_metadata_uses_verified_independent_binding(credit_db,payments,client):
    data=await receipt(payments);data['customer_extra']='not JSON'
    assert (await post_notification(client,data)).status==200
    assert (await sales.payment(data['order_num']))['status']=='paid'


async def test_repricing_issues_new_quote_but_honors_old_signed_invoice(credit_db,payments,client,monkeypatch):
    old=await receipt(payments)
    same=await sales.checkout(789)
    assert same['order_id']==old['order_num']
    monkeypatch.setattr(database,'_now_ts',lambda:stamp(11,19))
    new=await sales.checkout(789)
    assert new['order_id']!=old['order_num'] and new['amount_kopecks']==149000
    assert (await post_notification(client,old)).status==200
    assert (await sales.payment(old['order_num']))['amount_kopecks']==99000


async def test_two_paid_invoices_create_independent_bound_orders_without_waiting(credit_db,monkeypatch):
    first=await sales.checkout(789);await sales.mark_paid(first['order_id'],'first')
    second=await sales.checkout(789);await sales.mark_paid(second['order_id'],'second')
    one=await sales.claim(789,456,payment_key=first['order_id'])
    two=await sales.claim(789,456,payment_key=second['order_id'])
    assert one['id']!=two['id'] and one['status']==two['status']=='input'
    assert one['payment_order_id']==first['order_id'] and two['payment_order_id']==second['order_id']
    assert (await sales.payment(second['order_id']))['claimed_id']==two['id']
    assert (await sales.claim(789,456,payment_key=first['order_id']))['id']==one['id']


async def test_ded_receipt_cannot_be_reused_for_animation_or_credit_package(credit_db):
    row=await sales.checkout(789);await sales.mark_paid(row['order_id'],'one-receipt')
    animation=await database.create_animation_order(789,'one')
    with pytest.raises(ValueError,match='Деда Мороза'):
        await database.mark_animation_paid(animation['order_id'],'one-receipt')
    credit='max-new-credit-order'
    await database.create_payment_order(credit,789,1,9900,'1 генерация (Открытка)')
    with pytest.raises(ValueError,match='Деда Мороза'):
        await database.add_credits(789,1,payment_order_id=credit,provider_payment_id='one-receipt')
    assert (await database.get_user(789))['balance']==0


async def test_existing_animation_receipt_cannot_pay_for_ded(credit_db):
    animation=await database.create_animation_order(789,'one')
    await database.mark_animation_paid(animation['order_id'],'used-animation')
    row=await sales.checkout(789)
    with pytest.raises(ValueError,match='уже использован'):
        await sales.mark_paid(row['order_id'],'used-animation')
    assert (await sales.payment(row['order_id']))['status']=='pending'


async def test_concurrent_paid_notifications_claim_only_one_order(credit_db):
    row=await sales.checkout(789)
    results=await asyncio.gather(*(sales.mark_paid(row['order_id'],'same') for _ in range(5)))
    assert sum(results)==1
    claimed=await asyncio.gather(*(sales.claim(789,456) for _ in range(5)))
    assert len({r['id'] for r in claimed})==1


async def test_statistics_excludes_administrator_orders(credit_db):
    for user in (456,789):
        row=await sales.checkout(user);await sales.mark_paid(row['order_id'],'payment-'+str(user))
        order=await sales.claim(user,456);await dm.update(order['id'],status='done')
    stats=await sales.statistics(456)
    assert stats=={'paid':1,'revenue':99000,'ready':1,'attention':0}

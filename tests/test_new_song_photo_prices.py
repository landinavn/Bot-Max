"""Current prices, immutable historical invoices and payment/statistics isolation."""
from urllib.parse import parse_qsl,urlsplit

import aiosqlite
import pytest

import database
from prodamus import parse_form_fields
from test_sales_stats import sales_payments,deliver_webhook


async def book_audio(fmt, suffix='new'):
    key=await database.create_audio_order('audio-price-'+fmt+'-'+suffix,101,fmt,'custom','Имя и детали',None,'words','pop','Title')
    return await database.get_audio_order(key)


async def signed_audio(payments,order):
    link=await payments.create_audio_payment_link(order['user_id'],order['audio_format'],order['scenario'],
        order['details'],order['style'],order['prompt'],order['tags'],order['title'],order_id=order['order_id'])
    return parse_form_fields(parse_qsl(urlsplit(link).query,keep_blank_values=True))


@pytest.mark.parametrize('fmt,price,counter',[('fun',159,'buy_song_159'),('gift',290,'buy_song_290')])
async def test_current_audio_invoice_and_idempotent_credit_free_payment(credit_db,sales_payments,fmt,price,counter):
    order=await book_audio(fmt)
    assert order['amount_kopecks']==price*100
    fields=await signed_audio(sales_payments,order)
    assert fields['products'][0]['price']==f'{price}.00'
    await deliver_webhook(sales_payments,order);await deliver_webhook(sales_payments,order)
    counters=await database.get_all_counters()
    assert counters[counter]==1 and not counters.get('buy_song_199') and not counters.get('buy_song_490')
    assert (await database.get_user(101))['balance']==0
    bad={'order_num':order['order_id'],'order_id':'wrong','sum':str(price+1),'products':fields['products'],'customer_extra':fields['customer_extra']}
    with pytest.raises(ValueError):await sales_payments.validate_payment(bad)


@pytest.mark.parametrize('tariff,price,size,name,counter',[
    (590,490,3,'Фотосессия Попробовать','buy_photo_trial_490'),
    (990,690,5,'Фотосессия Стандарт','buy_photo_standard_690'),
    (1490,990,7,'Фотосессия Премиум','buy_photo_premium_990')])
@pytest.mark.parametrize('promo',[False,True])
async def test_photo_prices_sizes_and_repeat_offer_snapshot(credit_db,sales_payments,tariff,price,size,name,counter,promo):
    if promo:await database.set_promo(101,'photo')
    key=await database.create_photo_order('photo_new_'+str(tariff),101,tariff)
    order=await database.get_photo_order(key)
    amount=price*80 if promo else price*100
    assert order['amount_kopecks']==amount and database.PHOTO_PACKAGE_SIZES[tariff]==size
    link=await sales_payments.create_payment_link(101,order_id=key,amount=amount//100,description=order['product_name'])
    fields=parse_form_fields(parse_qsl(urlsplit(link).query,keep_blank_values=True))
    assert fields['products'][0]['price']==f'{amount//100}.00'
    assert order['product_name']==name+(database.REPEAT_SUFFIX if promo else '')
    await deliver_webhook(sales_payments,order);await deliver_webhook(sales_payments,order)
    assert await database.get_photo_package_balance(key)==size
    c=await database.get_all_counters();assert c[counter]==1
    assert not c.get('buy_photo_'+str(tariff))
    if promo:assert c[counter.replace('buy_photo_','promo_buy_photo_',1)]==1
    assert (await database.get_user(101))['balance']==0
    # Correct money cannot pay for another package: product identity must match too.
    product=dict(fields['products'][0],name='Мульт-открытка')
    bad={'order_num':key,'order_id':'wrong','sum':str(amount//100),'products':[product],'customer_extra':fields['customer_extra']}
    with pytest.raises(ValueError):await sales_payments.validate_payment(bad)


@pytest.mark.parametrize('fmt,price,name,counter',[
    ('fun',199,'Песня по фану','buy_song_199'),('gift',490,'Песня в подарок','buy_song_490'),
    ('gift',392,database.SONG_PROMO_PRODUCT,'buy_song_490')])
async def test_historical_audio_invoices_remain_payable_without_price_mutation(credit_db,sales_payments,fmt,price,name,counter):
    order=await book_audio(fmt,'old')
    async with aiosqlite.connect(database.DATABASE_PATH) as db:
        await db.execute('UPDATE audio_orders SET amount_kopecks=?,product_name=?,payment_link=? WHERE order_id=?',(price*100,name,'https://pay.example/issued-'+fmt,order['order_id']));await db.commit()
    order=await database.get_audio_order(order['order_id']);await signed_audio(sales_payments,order)
    sales_payments._request_payment_link.assert_not_awaited()
    await deliver_webhook(sales_payments,order)
    assert (await database.get_audio_order(order['order_id']))['amount_kopecks']==price*100
    assert (await database.get_all_counters())[counter]==1


@pytest.mark.parametrize('tariff,old_price',[(590,590),(990,990),(1490,1490)])
@pytest.mark.parametrize('promo',[False,True])
async def test_historical_photo_price_and_coupon_snapshot_remain_valid(credit_db,sales_payments,monkeypatch,tariff,old_price,promo):
    # Simulate an invoice really created by the previous application, including its promo snapshot.
    current=database.PHOTO_TARIFFS
    if promo:await database.set_promo(101,'photo')
    with monkeypatch.context() as patch:
        patch.setattr(database,'PHOTO_TARIFFS',{**current,tariff:(old_price*100,current[tariff][1])})
        key=await database.create_photo_order('photo_legacy_'+str(tariff),101,tariff)
    order=await database.get_photo_order(key)
    amount=old_price*80 if promo else old_price*100
    await sales_payments.create_payment_link(101,order_id=key,amount=amount//100,description=order['product_name'])
    await deliver_webhook(sales_payments,order)
    assert (await database.get_photo_order(key))['amount_kopecks']==amount
    assert (await database.get_all_counters())['buy_photo_'+str(tariff)]==1

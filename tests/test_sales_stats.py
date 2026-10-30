"""Confirmed sales, migration, product identity, and signed webhook regression tests."""
import json
import sqlite3
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock
from urllib.parse import parse_qsl, urlsplit, urlencode

import aiosqlite
import pytest

from config import Config
from prodamus import PACKAGES, ProdamusPayments, flatten_fields, parse_form_fields, sign_link_payload, sign_payload
from webhook import create_webhook_app


@pytest.fixture
def sales_payments():
    service = ProdamusPayments(Config(
        token='test', photo_path=Path('unused'),
        prodamus_domain='test-shop.payform.ru', prodamus_secret_key='test-secret',
        prodamus_return_url='https://max.ru/test_bot',
        prodamus_webhook_url='https://bot.example/prodamus/webhook',
    ))
    async def link(data):
        return service.domain + '?' + urlencode(flatten_fields(data))
    service._request_payment_link = AsyncMock(side_effect=link)
    return service


async def deliver_webhook(service, order, *, payment_id='paid', signature=None, **changes):
    payload = {
        'order_num': order['order_id'], 'order_id': payment_id, 'payment_status': 'success',
        'sum': f"{order['amount_kopecks'] / 100:.2f}", 'currency': 'rub',
        'products': [{'name': order['product_name'], 'price': f"{order['amount_kopecks'] / 100:.2f}", 'quantity': '1'}],
        '_param_user_id': str(order['user_id']), '_param_credits': str(order['credits']),
        '_param_package_id': order['package_id'],
    }
    payload.update(changes)
    app = create_webhook_app(service)
    handler = next(route.handler for route in app.router.routes() if route.method == 'POST')
    request = SimpleNamespace(content_type='application/json', text=AsyncMock(return_value=json.dumps(payload)),
                              headers={'Sign': signature or sign_payload(payload, service.secret)})
    return await handler(request)


SALE_CASES = [
    ('credit', 1, ('buy_credit_99',)), ('credit', 3, ('buy_credit_199',)),
    ('credit_promo', 3, ('buy_credit_199','buy_credit_149')),
    ('credit', 5, ('buy_credit_299',)), ('credit', 15, ('buy_credit_799',)),
    ('audio', 'fun', ('buy_song_199',)), ('audio', 'gift', ('buy_song_490',)),
    ('gift_discount', 'gift', ('buy_song_490',)),
    ('musical', 'direct', ('buy_music_direct',)),
    ('musical', 'upsell', ('buy_music_upsell','buy_music_card_149')),
    ('cartoon', 'short', ('buy_cartoon_590',)), ('cartoon', 'long', ('buy_cartoon_1290',)),
    ('photo', 590, ('buy_photo_590',)), ('photo', 990, ('buy_photo_990',)),
    ('photo', 1490, ('buy_photo_1490',)),
]


async def make_order(db, kind, tariff, user_id=101, suffix='one'):
    key = f'{kind}_{suffix}_{user_id}'
    if kind.startswith('credit'):
        if kind == 'credit_promo':
            await db.grant_free_trial(user_id)
            await db.deduct_credit(user_id, count_generation=True)
        await db.create_payment_order(key, user_id, tariff,
            14900 if kind == 'credit_promo' else {1:9900,3:19900,5:29900,15:79900}[tariff],
            PACKAGES[tariff][1], promo=kind=='credit_promo')
        return await db.get_payment_order(key)
    if kind in ('audio','gift_discount'):
        if kind == 'gift_discount':
            await db.set_song_promo(user_id)
        await db.create_audio_order(key,user_id,tariff,'boss','Имя, припев','pop','[Verse] words','pop','Title')
        if kind == 'gift_discount':
            # Historical invoice issued before the fixed 490 RUB catalog.
            async with aiosqlite.connect(db.DATABASE_PATH) as connection:
                await connection.execute("UPDATE audio_orders SET amount_kopecks=39200,product_name=? WHERE order_id=?",
                                         (db.SONG_PROMO_PRODUCT,key))
                await connection.commit()
        return await db.get_audio_order(key)
    if kind == 'musical':
        key = 'music_' + key
        await db.create_musical_order(key,user_id,'https://cdn.example/a.jpg','https://cdn.example/song.mp3',tariff)
        return await db.get_musical_order(key)
    if kind == 'cartoon':
        key = 'cartoon_' + key
        await db.create_cartoon_order(key,user_id,'theme_ny',tariff)
        return await db.get_cartoon_order(key)
    key = 'photo_' + key
    await db.create_photo_order(key,user_id,tariff)
    return await db.get_photo_order(key)


@pytest.mark.parametrize('kind,tariff,keys', SALE_CASES)
async def test_every_product_counts_once_after_signed_webhook(credit_db,sales_payments,kind,tariff,keys):
    await credit_db.record_first_start(101,'yandex_newyear')
    order = await make_order(credit_db,kind,tariff)
    for _ in range(2):
        response = await deliver_webhook(sales_payments,order)
        assert response.status == 200 and response.text == 'OK'
    counts = await credit_db.get_all_counters(admin_id=999)
    canonical = {key for _,_,events in SALE_CASES for key in events}
    assert {key:counts[key] for key in counts.keys() & canonical} == dict.fromkeys(keys,1)
    user = await credit_db.get_user(101)
    assert user['total_purchases'] == 1
    assert user['balance'] == (tariff if kind.startswith('credit') else 0)
    source = await credit_db.get_stats_by_source('yandex_newyear',admin_id=999)
    assert source['total_users'] == source['total_buyers'] == 1
    assert source['total_revenue'] * 100 == order['amount_kopecks']
    # Restart is not another sale.
    await credit_db.init_db()
    assert await credit_db.get_all_counters(admin_id=999) == counts


@pytest.mark.parametrize('change', [{'sum':'1'}, {'is_test':'1'}, {'payment_status':'failed'},
                                     {'_param_user_id':'999'}, {'signature':'invalid'},
                                     {'products':[{'name':'Мульт-открытка','price':'590.00','quantity':'1'}]}])
async def test_invalid_photo_webhooks_never_count_sales(credit_db,sales_payments,change):
    order = await make_order(credit_db,'photo',590)
    await deliver_webhook(sales_payments,order,**change)
    assert (await credit_db.get_photo_order(order['order_id']))['status'] == 'pending'
    assert 'buy_photo_590' not in await credit_db.get_all_counters()


@pytest.mark.parametrize('tariff',[590,990,1490])
async def test_photo_personal_signed_link_matches_persisted_product(credit_db,sales_payments,tariff):
    order = await make_order(credit_db,'photo',tariff)
    link = await sales_payments.create_payment_link(101,order_id=order['order_id'],
                                                   amount=tariff,description=order['product_name'])
    assert await sales_payments.create_payment_link(101,order_id=order['order_id'],
             amount=tariff,description=order['product_name']) == link
    fields = parse_form_fields(parse_qsl(urlsplit(link).query))
    assert fields['_param_package_id'] == f'photo_{tariff}'
    assert fields['order_id'] == order['order_id']
    assert fields['products'][0]['price'] == f'{tariff}.00'
    assert sign_link_payload({k:v for k,v in fields.items() if k!='sign'},sales_payments.secret) == fields['sign']
    sales_payments._request_payment_link.assert_awaited_once()


@pytest.mark.parametrize('other_kind,other_tariff',[('credit',1),('audio','fun'),('musical','direct'),('cartoon','short')])
@pytest.mark.parametrize('photo_first',[False,True])
async def test_photo_payment_id_cannot_be_reused_across_products(credit_db,sales_payments,other_kind,other_tariff,photo_first):
    photo = await make_order(credit_db,'photo',590)
    other = await make_order(credit_db,other_kind,other_tariff,suffix='other')
    first, second = (photo,other) if photo_first else (other,photo)
    assert (await deliver_webhook(sales_payments,first)).status == 200
    assert (await deliver_webhook(sales_payments,second)).status == 400
    assert (await credit_db.get_user(101))['total_purchases'] == 1


async def test_sales_migration_restores_paid_history_once_and_excludes_admin(credit_db,sales_payments):
    expected = {}
    for i,(kind,tariff,keys) in enumerate(sorted(SALE_CASES, key=lambda item: item[0] not in ('credit_promo','gift_discount'))):
        for user in (101,999):
            order = await make_order(credit_db,kind,tariff,user_id=user,suffix=str(i))
            assert (await deliver_webhook(sales_payments,order,payment_id=f'paid-{i}-{user}')).status == 200
            if kind == 'cartoon':
                await credit_db.finish_cartoon_order(order['order_id'],user,manual=tariff=='long')
        for key in keys:
            expected[key] = expected.get(key,0) + 1
    # Simulate a pre-upgrade database containing only the legacy event names.
    async with aiosqlite.connect(credit_db.DATABASE_PATH) as db:
        for key in expected:
            await db.execute('DELETE FROM user_counters WHERE event_name=?',(key,))
            await db.execute('DELETE FROM global_counters WHERE event_name=?',(key,))
        await db.execute("DELETE FROM stats_migrations WHERE name='sales_v2'")
        await db.commit()
    await credit_db.init_db()
    await credit_db.init_db()
    filtered = await credit_db.get_all_counters(admin_id=999)
    global_counts = await credit_db.get_all_counters()
    assert {key:filtered[key] for key in expected} == expected
    assert {key:global_counts[key] for key in expected} == {k:v*2 for k,v in expected.items()}


async def test_photo_counter_failure_rolls_back_paid_receipt(credit_db,monkeypatch):
    order = await make_order(credit_db,'photo',590)
    original = credit_db.increment_counter
    async def fail_after_increment(*args,**kwargs):
        await original(*args,**kwargs)
        raise sqlite3.OperationalError('counter failed')
    monkeypatch.setattr(credit_db,'increment_counter',fail_after_increment)
    with pytest.raises(sqlite3.OperationalError):
        await credit_db.mark_photo_paid(order['order_id'],'paid')
    assert (await credit_db.get_photo_order(order['order_id']))['status'] == 'pending'
    assert await credit_db.get_user(101) is None
    assert not await credit_db.get_all_counters()


async def test_all_source_names_are_listed_once_with_confirmed_revenue(credit_db,sales_payments):
    for user,source in [(101,'vk_ads'),(102,'yandex_ads'),(103,'yandex_newyear'),(104,'partner'),(999,'partner')]:
        await credit_db.record_first_start(user)
        async with aiosqlite.connect(credit_db.DATABASE_PATH) as db:
            await db.execute('UPDATE users SET first_start_source=? WHERE user_id=?',(source,user))
            await db.commit()
        order = await make_order(credit_db,'photo',590,user_id=user)
        await deliver_webhook(sales_payments,order,payment_id=str(user))
    rows = await credit_db.get_source_stats(admin_id=999)
    assert len({r['source'] for r in rows}) == len(rows)
    assert next(r for r in rows if r['source']=='partner')['revenue_kopecks'] == 59000
    assert next(r for r in rows if r['source']=='partner')['total_users'] == 1

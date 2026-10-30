"""Audit every new invoice and dispatch by saved order, never by a shared price."""
import json
import logging
import re
from types import SimpleNamespace
from unittest.mock import AsyncMock
from urllib.parse import parse_qsl

import aiosqlite
import pytest
from aioresponses import aioresponses

import handlers
import prodamus
from audio_orders import PaidAudioWorker
from cartoon_orders import PaidCartoonWorker
from musical_orders import PaidMusicalWorker
from photo_orders import PaidPhotoWorker
from prodamus import ProdamusPayments, parse_form_fields
from test_sales_stats import sales_payments, make_order, deliver_webhook


CATALOG = [
    ('credit',1,99,'1 генерация (Открытка)'), ('credit',3,199,'3 генерации'),
    ('credit_promo',3,149,'3 генерации'), ('credit',5,299,'5 генераций'),
    ('credit',15,799,'15 генераций'),
    ('photo',590,590,'Фотосессия Попробовать'), ('photo',990,990,'Фотосессия Стандарт'),
    ('photo',1490,1490,'Фотосессия Премиум'),
    ('audio','fun',199,'Песня по фану'), ('audio','gift',490,'Песня в подарок'),
    ('musical','direct',299,'Музыкальная открытка'), ('musical','upsell',149,'Апгрейд открытки с песней'),
    ('cartoon','short',590,'Мульт-открытка'), ('cartoon','long',1290,'Мульт-поздравление (до 30 сек)'),
]


@pytest.mark.parametrize('kind,tariff,amount,name',CATALOG)
async def test_current_invoice_price_description_logging_and_dispatch(credit_db,sales_payments,caplog,kind,tariff,amount,name):
    service = sales_payments
    service._request_payment_link = ProdamusPayments._request_payment_link.__get__(service)
    with aioresponses() as api, caplog.at_level(logging.INFO,logger='prodamus'):
        api.get(re.compile(r'https://test-shop\.payform\.ru/\?.*'),body='https://test-shop.payform.ru/payment/audit')
        if kind.startswith('credit'):
            if kind=='credit_promo':
                await credit_db.grant_free_trial(101)
                await credit_db.deduct_credit(101,count_generation=True)
            await service.create_payment_link(101,tariff,promo=kind=='credit_promo')
        else:
            order=await make_order(credit_db,kind,tariff)
            if kind=='audio':
                await service.create_audio_payment_link(101,tariff,order['scenario'],order['details'],
                    order['style'],order['prompt'],order['tags'],order['title'],order_id=order['order_id'])
            elif kind=='musical':
                await service.create_musical_payment_link(order_id=order['order_id'],amount=amount,description=name)
            else:
                await service.create_payment_link(101,order_id=order['order_id'],amount=amount,description=name)
        fields=parse_form_fields(parse_qsl(next(iter(api.requests))[1].query_string))
    assert fields['products']==[{'name':name,'price':f'{amount}.00','quantity':'1'}]
    assert f"Создан счет: {name}, сумма: {amount} ₽, order_id: {fields['order_id']}" in caplog.text
    assert service.secret not in caplog.text and 'sign=' not in caplog.text
    getters={'credit':credit_db.get_payment_order,'credit_promo':credit_db.get_payment_order,
             'photo':credit_db.get_photo_order,'audio':credit_db.get_audio_order,
             'musical':credit_db.get_musical_order,'cartoon':credit_db.get_cartoon_order}
    order=await getters[kind](fields['order_id'])
    for _ in range(2):
        assert (await deliver_webhook(service,order)).status==200
    assert (await credit_db.get_user(101))['total_purchases']==1
    if kind.startswith('credit'):
        assert (await credit_db.get_user(101))['balance']==tariff
        assert not await credit_db.get_pending_photo_jobs()
        assert not await credit_db.pending_audio_orders()
        assert not await credit_db.pending_musical_orders()
        assert not await credit_db.pending_cartoon_notifications()
    else:
        assert (await credit_db.get_user(101))['balance']==0
        if kind=='photo':
            assert [job['order_id'] for job in await credit_db.get_pending_photo_jobs()]==[order['order_id']]
        elif kind=='audio':
            assert order['order_id'] in await credit_db.pending_audio_orders()
        elif kind=='musical':
            assert order['order_id'] in await credit_db.pending_musical_orders()
        else:
            assert [job['order_id'] for job in await credit_db.pending_cartoon_notifications()]==[order['order_id']]


@pytest.mark.parametrize('kind,tariff',[('photo',590),('musical','direct'),('cartoon','short')])
async def test_corrupt_pending_price_cannot_create_new_invoice(credit_db,sales_payments,kind,tariff):
    order=await make_order(credit_db,kind,tariff)
    table={'photo':'photo_orders','musical':'musical_orders','cartoon':'cartoon_orders'}[kind]
    async with aiosqlite.connect(credit_db.DATABASE_PATH) as db:
        await db.execute(f'UPDATE {table} SET amount_kopecks=100 WHERE order_id=?',(order['order_id'],))
        await db.commit()
    with pytest.raises(ValueError):
        if kind=='musical':
            await sales_payments.create_musical_payment_link(order_id=order['order_id'],amount=1,description=order['product_name'])
        else:
            await sales_payments.create_payment_link(101,order_id=order['order_id'],amount=1,description=order['product_name'])
    sales_payments._request_payment_link.assert_not_awaited()


async def test_ambiguous_same_order_id_in_two_families_is_rejected(credit_db,sales_payments):
    music=await make_order(credit_db,'musical','direct')
    await credit_db.create_payment_order(music['order_id'],101,5,29900,prodamus.PACKAGES[5][1])
    assert (await deliver_webhook(sales_payments,music)).status==400
    assert (await credit_db.get_musical_order(music['order_id']))['status']=='pending'
    assert (await credit_db.get_payment_order(music['order_id']))['status']=='pending'
    assert await credit_db.get_user(101) is None


@pytest.mark.parametrize('tariff',[590,990,1490])
async def test_photo_payment_survives_restart_and_notifies_user_and_admin_once(credit_db,sales_payments,tariff):
    order=await make_order(credit_db,'photo',tariff)
    await credit_db.update_photo_order(order['order_id'],style='photo_style_ny',template='fam3')
    bot=SimpleNamespace(send_message=AsyncMock(return_value=object()))
    worker=PaidPhotoWorker(bot,admin_user_id=999)
    await worker.process()
    bot.send_message.assert_not_awaited()
    assert (await deliver_webhook(sales_payments,order)).status==200
    await credit_db.init_db()
    await worker.process()
    await worker.process()
    assert [c.kwargs['user_id'] for c in bot.send_message.await_args_list]==[101,999]
    assert order['order_id'] in str(bot.send_message.await_args_list)
    assert 'fam3' in bot.send_message.await_args_list[1].kwargs['text']
    assert len(await credit_db.get_pending_photo_jobs())==1
    assert (await credit_db.get_user(101))['total_purchases']==1


async def test_photo_queue_continues_after_send_failure_without_losing_paid_job(credit_db,sales_payments):
    for user in (101,102):
        order=await make_order(credit_db,'photo',590,user_id=user)
        await deliver_webhook(sales_payments,order,payment_id=str(user))
    bot=SimpleNamespace(send_message=AsyncMock(side_effect=[RuntimeError('blocked'),object(),object()]))
    worker=PaidPhotoWorker(bot,admin_user_id=999)
    await worker.process()
    jobs=await credit_db.get_pending_photo_jobs()
    failed=next(j for j in jobs if j['user_id']==101)
    assert failed['user_notified']==0 and failed['next_attempt_at']>0
    assert next(j for j in jobs if j['user_id']==102)['admin_notified']==1
    await credit_db.update_photo_order(failed['order_id'],next_attempt_at=0)
    bot.send_message=AsyncMock(return_value=object())
    await worker.process()
    assert bot.send_message.await_count==2


@pytest.mark.parametrize('audio_format',['fun','gift'])
async def test_paid_audio_dispatches_music_worker(credit_db,sales_payments,audio_format):
    order=await make_order(credit_db,'audio',audio_format)
    await deliver_webhook(sales_payments,order)
    bot=SimpleNamespace(send_message=AsyncMock(return_value=object()))
    generator=AsyncMock(return_value='music-job')
    waiter=AsyncMock(return_value={'status':'SUCCESS','data':{'musics':[{'audioUrl':'https://cdn.example/song.mp3'}]}})
    worker=PaidAudioWorker(bot,generator=generator,waiter=waiter,
                           lyric_writer=AsyncMock(return_value='[Verse]\nText'))
    await worker.process(order['order_id'])
    await worker.process(order['order_id'])
    generator.assert_awaited_once()
    waiter.assert_awaited_once()
    assert (await credit_db.get_audio_order(order['order_id']))['delivered']==1


@pytest.mark.parametrize('tariff',['direct','upsell'])
async def test_paid_musical_dispatches_saved_picture_and_song_to_ffmpeg(credit_db,sales_payments,tariff):
    order=await make_order(credit_db,'musical',tariff)
    await deliver_webhook(sales_payments,order)
    bot=SimpleNamespace(send_message=AsyncMock(return_value=object()))
    renderer=AsyncMock(return_value='https://cdn.example/finished.mp4')
    worker=PaidMusicalWorker(bot,renderer=renderer,uploader=AsyncMock(return_value=object()))
    await worker.process(order['order_id'])
    await worker.process(order['order_id'])
    renderer.assert_awaited_once_with(order['image_url'],order['audio_url'])
    assert (await credit_db.get_musical_order(order['order_id']))['delivered']==1


@pytest.mark.parametrize('tariff',['short','long'])
async def test_paid_cartoon_dispatches_photo_request_before_kling(credit_db,sales_payments,tariff):
    order=await make_order(credit_db,'cartoon',tariff)
    await deliver_webhook(sales_payments,order)
    bot=SimpleNamespace(send_message=AsyncMock(return_value=object()))
    await PaidCartoonWorker(bot).process()
    assert 'фото' in bot.send_message.await_args.kwargs['text']
    assert order['order_id'] in str(bot.send_message.await_args.kwargs['attachments'])
    handlers.create_video_task.assert_not_awaited() # Avatar and song must be approved before charging Kling.


async def test_historical_gift_invoice_is_honored_but_cannot_be_newly_issued(credit_db,sales_payments):
    order=await make_order(credit_db,'gift_discount','gift')
    with pytest.raises(ValueError):
        await sales_payments.create_audio_payment_link(101,'gift',order['scenario'],order['details'],
            order['style'],order['prompt'],order['tags'],order['title'],order_id=order['order_id'])
    sales_payments._request_payment_link.assert_not_awaited()
    assert (await deliver_webhook(sales_payments,order)).status==200
    assert (await credit_db.get_audio_order(order['order_id']))['amount_kopecks']==39200


async def test_legacy_gift_preview_reprices_without_second_music_generation(credit_db,sales_payments):
    old=await make_order(credit_db,'gift_discount','gift')
    tracks=[{'audioUrl':'https://cdn.example/full-song.mp3','title':'Gift'}]
    await credit_db.update_audio_order(old['order_id'],preview_url='https://cdn.example/preview.mp3',
                                      tracks_json=json.dumps(tracks),lyrics='[Verse]\nLyrics',preview_delivered=1)
    new=await credit_db.song_checkout_order(old['order_id'])
    assert new['order_id']!=old['order_id'] and new['amount_kopecks']==49000
    assert new['tracks_json']==json.dumps(tracks)
    assert new['preview_url']=='https://cdn.example/preview.mp3'
    assert (await credit_db.get_audio_order(old['order_id']))['amount_kopecks']==39200
    await deliver_webhook(sales_payments,new)
    generator=AsyncMock()
    bot=SimpleNamespace(send_message=AsyncMock(return_value=object()))
    await PaidAudioWorker(bot,generator=generator).process(new['order_id'])
    generator.assert_not_awaited()
    assert (await credit_db.get_audio_order(new['order_id']))['delivered']==1


async def test_expired_credit_promo_blocks_new_invoice_and_honors_issued_one(credit_db,sales_payments,monkeypatch):
    clock=credit_db._now_ts()
    monkeypatch.setattr(credit_db,'_now_ts',lambda:clock)
    order=await make_order(credit_db,'credit_promo',3)
    clock+=25*3600
    with pytest.raises(credit_db.PromoUnavailableError):
        await sales_payments.create_payment_link(101,3,promo=True)
    sales_payments._request_payment_link.assert_not_awaited()
    assert (await deliver_webhook(sales_payments,order)).status==200
    assert (await credit_db.get_user(101))['balance']==3


async def test_photo_migration_recovers_unfulfilled_paid_orders_without_requeuing_completed(credit_db):
    order=await make_order(credit_db,'photo',590)
    async with aiosqlite.connect(credit_db.DATABASE_PATH) as db:
        await db.execute("UPDATE photo_orders SET status='paid',provider_payment_id='legacy' WHERE order_id=?",(order['order_id'],))
        await db.commit()
    await credit_db.init_db()
    assert [job['order_id'] for job in await credit_db.get_pending_photo_jobs()]==[order['order_id']]
    await credit_db.update_photo_order(order['order_id'],generation_status='completed')
    await credit_db.init_db()
    assert not await credit_db.get_pending_photo_jobs()

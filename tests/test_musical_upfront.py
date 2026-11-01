"""Pay for the selected card before photo/music generation; deliver only the finished video."""
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
import handlers
from handlers import create_router, PosterStates
from musical_orders import PaidMusicalWorker
from test_handlers import (callback_event, ready_context, dispatch_callback, call_handler,
    message_event, photo_event, event_replies, max_image_upload, isolated_example_directory, text_correction)
from test_sales_stats import sales_payments, deliver_webhook


async def select_card(router, context, mode='mode_ny'):
    await dispatch_callback(router, callback_event('cat_video'), context)
    event=callback_event('video_style:'+mode)
    await dispatch_callback(router, event, context)
    return event


async def test_local_video_example_precedes_style_menu(credit_db,tmp_path,monkeypatch,sales_payments):
    path=tmp_path/handlers.MUSICAL_EXAMPLE_FILE;path.write_bytes(b'video fixture')
    monkeypatch.setattr(handlers,'EXAMPLES_DIR',tmp_path)
    event=callback_event('cat_video')
    event.bot.upload_media.return_value=SimpleNamespace(type='video',payload=SimpleNamespace(token='demo-video'))
    await dispatch_callback(create_router(payments=sales_payments),event,await ready_context())
    media=event.bot.upload_media.await_args.args[0]
    assert media.path==str(path) and media.type.value=='video'
    assert event.send.await_args_list[0].kwargs['attachments'][0].type=='video'
    assert 'Пример музыкальной' in event.send.await_args_list[0].args[0]
    assert 'Выберите стиль' in event.send.await_args_list[1].args[0]
    sales_payments._request_payment_link.assert_not_awaited()


@pytest.mark.parametrize('mode',list(handlers.POSTCARD_PROMPTS))
@pytest.mark.parametrize('promo,amount',[(False,29900),(True,23900)])
async def test_invoice_after_style_and_before_photo_or_generators(credit_db,sales_payments,mode,promo,amount):
    if promo: await credit_db.set_promo(456,'music_card')
    image,song,lyrics=AsyncMock(),AsyncMock(),AsyncMock()
    router=create_router(admin_user_id=999,payments=sales_payments,postcard_generator=image,song_generator=song,lyric_writer=lyrics)
    context=await ready_context();event=await select_card(router,context,mode)
    order=await credit_db.get_musical_order((await context.get_data())['music_prepaid_id'])
    assert order['mode']==mode and order['prepaid']==1 and order['input_ready']==0
    assert order['amount_kopecks']==amount and order['image_url']==order['audio_url']==''
    assert await context.get_state()==PosterStates.waiting_for_video_payment
    assert sales_payments._request_payment_link.await_args.args[0]['products'][0]['price']==f'{amount//100}.00'
    image.assert_not_awaited();song.assert_not_awaited();lyrics.assert_not_awaited()
    resume=callback_event('music_resume:'+order['order_id'])
    await dispatch_callback(router,resume,context)
    assert 'ещё не подтверждена' in resume.send.await_args.args[0]
    # Stale FSM input cannot start a paid generation before the webhook.
    await context.set_state(PosterStates.waiting_for_card_text)
    await call_handler(router,'collect_card_text',message_event('Поздравляю!'),context)
    image.assert_not_awaited();song.assert_not_awaited()


@pytest.mark.parametrize('wait_failure',[False,True])
async def test_paid_flow_persists_inputs_and_delivers_mp4_without_audio_preview(credit_db,sales_payments,monkeypatch,wait_failure):
    preview=AsyncMock(side_effect=AssertionError('Audio preview must not run'))
    monkeypatch.setattr(handlers,'create_audio_preview',preview)
    image=AsyncMock(return_value=['https://cdn.example/clean-card.png'])
    submit=AsyncMock(return_value='music-job')
    result={'status':'SUCCESS','data':[{'audioUrl':'https://cdn.example/full.mp3'}]}
    waiter=AsyncMock(side_effect=[RuntimeError('temporary'),result] if wait_failure else [result])
    router=create_router(admin_user_id=999,payments=sales_payments,postcard_generator=image,
        song_generator=submit,song_waiter=waiter,lyric_writer=AsyncMock(return_value='Пусть счастье и радость приходят в ваш дом!'))
    context=await ready_context();await select_card(router,context)
    key=(await context.get_data())['music_prepaid_id'];order=await credit_db.get_musical_order(key)
    await deliver_webhook(sales_payments,order);await deliver_webhook(sales_payments,order)
    render=AsyncMock(return_value='https://cdn.example/final.mp4')
    video=SimpleNamespace(type='video')
    bot=SimpleNamespace(send_message=AsyncMock(return_value=object()))
    worker=PaidMusicalWorker(bot,admin_user_id=999,renderer=render,uploader=AsyncMock(return_value=video))
    await worker.process(key)
    render.assert_not_awaited()
    assert 'music_resume:'+key in str(bot.send_message.await_args)
    assert not await credit_db.claim_musical_order(key)
    await context.clear();await dispatch_callback(router,callback_event('music_resume:'+key),context)
    assert await context.get_state()==PosterStates.waiting_for_photo
    await call_handler(router,'collect_photo',photo_event(),context)
    assert (await credit_db.get_musical_order(key))['input_stage']=='text'
    await context.clear();await dispatch_callback(router,callback_event('music_resume:'+key),context)
    assert await context.get_state()==PosterStates.waiting_for_card_text
    await call_handler(router,'collect_card_text',message_event('С Новым годом, Аня!'),context)
    await dispatch_callback(router,callback_event('video_song:audio_fun_ny'),context)
    await context.clear();await dispatch_callback(router,callback_event('music_resume:'+key),context)
    phrase=message_event('Аня, с Новым годом!')
    await call_handler(router,'collect_video_song_phrase',phrase,context)
    if wait_failure:
        assert (await credit_db.get_musical_order(key))['input_ready']==0
        await context.clear();await dispatch_callback(router,callback_event('music_resume:'+key),context)
        await call_handler(router,'collect_video_song_phrase',phrase,context)
    order=await credit_db.get_musical_order(key)
    assert order['input_ready']==1
    assert order['image_url']=='https://cdn.example/clean-card.png' and order['audio_url']=='https://cdn.example/full.mp3'
    assert not any('🎧' in str(c) or 'Прослушать' in str(c) for c in phrase.message.answer.await_args_list)
    preview.assert_not_awaited();submit.assert_awaited_once();image.assert_awaited_once()
    await worker.process(key);await worker.process(key)
    render.assert_awaited_once_with(order['image_url'],order['audio_url'])
    delivered=[c for c in bot.send_message.await_args_list if c.kwargs.get('text')=='🎵 Ваша музыкальная открытка готова!']
    assert len(delivered)==1 and delivered[0].kwargs['attachments'][0].type=='video'
    assert (await credit_db.get_musical_order(key))['delivered']==1
    assert (await credit_db.get_all_counters())['buy_music_direct']==1
    sales_payments._request_payment_link.assert_awaited_once()


async def test_prepaid_inputs_reject_foreign_unpaid_and_replacement(credit_db):
    key=await credit_db.create_prepaid_musical_order('music_guard',456,'mode_ny')
    urls=('https://cdn.example/card.png','https://cdn.example/song.mp3')
    with pytest.raises(ValueError): await credit_db.set_musical_inputs(key,456,*urls)
    await credit_db.mark_musical_paid(key,'payment')
    with pytest.raises(ValueError): await credit_db.set_musical_inputs(key,101,*urls)
    assert await credit_db.set_musical_inputs(key,456,*urls)
    assert not await credit_db.set_musical_inputs(key,456,*urls)
    with pytest.raises(ValueError): await credit_db.set_musical_inputs(key,456,urls[0],'https://cdn.example/other.mp3')


async def test_admin_skips_upfront_payment(credit_db,sales_payments):
    context=await ready_context();await select_card(create_router(admin_user_id=456,payments=sales_payments),context)
    assert await context.get_state()==PosterStates.waiting_for_photo
    assert 'music_prepaid_id' not in await context.get_data()
    sales_payments._request_payment_link.assert_not_awaited()


async def test_paid_music_notification_retries_without_consuming_render_attempts(credit_db):
    key=await credit_db.create_prepaid_musical_order('music_notice',456,'mode_ny')
    await credit_db.mark_musical_paid(key,'paid-notice')
    bot=SimpleNamespace(send_message=AsyncMock(side_effect=[RuntimeError('MAX down'),object()]))
    render=AsyncMock()
    worker=PaidMusicalWorker(bot,renderer=render)
    await worker.process(key)
    order=await credit_db.get_musical_order(key)
    assert order['attempts']==0 and order['failed']==0 and order['user_notified']==0
    assert key not in await credit_db.pending_musical_orders()
    await credit_db.update_musical_order(key,next_attempt_at=0)
    await worker.process(key)
    assert (await credit_db.get_musical_order(key))['user_notified']==1
    assert key not in await credit_db.pending_musical_orders()
    render.assert_not_awaited()


async def test_foreign_resume_cannot_expose_paid_music_draft(credit_db):
    key=await credit_db.create_prepaid_musical_order('music_private',101,'mode_ny')
    await credit_db.mark_musical_paid(key,'paid-private')
    await credit_db.save_musical_draft(key,101,{'photo':{'url':'https://cdn.example/private.png'}},'text')
    context=await ready_context();before=await context.get_data()
    event=callback_event('music_resume:'+key)
    await dispatch_callback(create_router(admin_user_id=999),event,context)
    assert await context.get_data()==before
    assert 'private.png' not in str(event.send.await_args_list)
    assert 'недоступен' in event.send.await_args.args[0]


async def test_interrupted_paid_music_resumes_saved_job_without_regeneration(credit_db,sales_payments):
    import asyncio
    entered,release=asyncio.Event(),asyncio.Event()
    async def wait(job):
        entered.set()
        await release.wait()
        return 'https://cdn.example/full.mp3'
    image=AsyncMock(return_value=['https://cdn.example/clean.png'])
    submit=AsyncMock(return_value='saved-job')
    router=create_router(admin_user_id=999,payments=sales_payments,postcard_generator=image,
                         song_generator=submit,song_waiter=wait)
    context=await ready_context();await select_card(router,context)
    key=(await context.get_data())['music_prepaid_id']
    await deliver_webhook(sales_payments,await credit_db.get_musical_order(key))
    await dispatch_callback(router,callback_event('music_resume:'+key),context)
    await call_handler(router,'collect_photo',photo_event(),context)
    await call_handler(router,'collect_card_text',message_event('С праздником!'),context)
    await dispatch_callback(router,callback_event('video_song:audio_fun_ny'),context)
    task=asyncio.create_task(call_handler(router,'collect_video_song_phrase',message_event('Анна, счастья!'),context))
    try:
        await asyncio.wait_for(entered.wait(),5)
        draft=json.loads((await credit_db.get_musical_order(key))['draft'])
        assert draft['video_source_image']=='https://cdn.example/clean.png'
        assert draft['video_song_job_id']=='saved-job'
        task.cancel()
        with pytest.raises(asyncio.CancelledError): await task
    finally:
        release.set()
        if not task.done():
            task.cancel()
    await context.clear()
    await dispatch_callback(router,callback_event('music_resume:'+key),context)
    await call_handler(router,'collect_video_song_phrase',message_event('Анна, счастья!'),context)
    assert (await credit_db.get_musical_order(key))['input_ready']==1
    image.assert_awaited_once();submit.assert_awaited_once()


async def test_legacy_musical_order_migration_preserves_render_readiness(credit_db):
    import sqlite3
    await credit_db.create_musical_order('music_legacy_migration',456,
        'https://cdn.example/card.png','https://cdn.example/song.mp3','direct')
    await credit_db.mark_musical_paid('music_legacy_migration','legacy-paid')
    with sqlite3.connect(credit_db.DATABASE_PATH) as connection:
        for column in ('prepaid','input_ready','user_notified','mode','draft','input_stage'):
            connection.execute('ALTER TABLE musical_orders DROP COLUMN '+column)
    await credit_db.init_db()
    await credit_db.init_db()  # Repeated startups remain idempotent.
    order=await credit_db.get_musical_order('music_legacy_migration')
    assert order['status']=='paid' and order['prepaid']==0 and order['input_ready']==1
    assert order['image_url']=='https://cdn.example/card.png'
    assert await credit_db.claim_musical_order(order['order_id'])

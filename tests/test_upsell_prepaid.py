"""149-ruble upgrade: reviewed lyrics -> payment -> durable song -> existing card video."""
import asyncio
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

import database
import handlers
from musical_orders import PaidMusicalWorker
from test_handlers import callback_event, message_event, ready_context, dispatch_callback, call_handler, event_replies

IMAGE = 'https://cdn.example/already-created.png'
AUDIO = 'https://cdn.example/approved-song.mp3'
DRAFT = {'lyrics': '[Verse]\nСпасибо, мама, за любовь!\n[Chorus]\nПусть счастье будет вновь!',
         'tags': 'pop, female solo vocals', 'title': 'Песня для мамы'}


async def order(*, paid=False):
    key = await database.create_prepaid_musical_upsell('music_upsell', 456, IMAGE, DRAFT)
    if paid:
        await database.mark_musical_paid(key, 'payment-upsell')
    return key


def worker(**kwargs):
    return PaidMusicalWorker(SimpleNamespace(send_message=AsyncMock(return_value=object())),
        renderer=kwargs.pop('renderer', AsyncMock(return_value='https://cdn.example/final.mp4')),
        uploader=AsyncMock(return_value=object()),
        song_generator=kwargs.pop('song_generator', AsyncMock(return_value='same-song-job')),
        song_waiter=kwargs.pop('song_waiter', AsyncMock(return_value=AUDIO)), **kwargs)


async def test_upgrade_waits_for_payment_then_survives_restart_and_concurrent_claims(credit_db):
    key = await order()
    w = worker()
    await w.process(key)
    assert await database.pending_musical_orders() == []
    w.song_generator.assert_not_awaited(); w.renderer.assert_not_awaited()
    saved = await database.get_musical_order(key)
    assert saved['amount_kopecks'] == 14900 and saved['prepaid'] and not saved['input_ready']
    assert saved['image_url'] == IMAGE and not saved['audio_url']
    await database.mark_musical_paid(key, 'payment-upsell')
    assert not await database.mark_musical_paid(key, 'payment-upsell')
    await database.init_db()
    assert key in await database.pending_musical_orders()
    await asyncio.gather(w.process(key), w.process(key))
    w.song_generator.assert_awaited_once_with(DRAFT['lyrics'], DRAFT['tags'], DRAFT['title'])
    w.renderer.assert_awaited_once_with(IMAGE, AUDIO)
    saved = await database.get_musical_order(key)
    assert saved['delivered'] and saved['input_ready'] and saved['audio_url'] == AUDIO
    assert not any('загрузить фото' in c.kwargs['text'] for c in w.bot.send_message.await_args_list)


async def test_known_music_job_is_polled_after_restart_without_another_post(credit_db):
    key = await order(paid=True)
    w = worker(song_waiter=AsyncMock(side_effect=TimeoutError()))
    await w.process(key)
    saved = await database.get_musical_order(key)
    assert json.loads(saved['draft'])['song_job_id'] == 'same-song-job' and not saved['failed']
    await database.update_musical_order(key, next_attempt_at=0)
    resumed = worker()
    await resumed.process(key)
    resumed.song_generator.assert_not_awaited()
    resumed.song_waiter.assert_awaited_once_with('same-song-job')
    assert (await database.get_musical_order(key))['delivered']


@pytest.mark.parametrize('error', [TimeoutError(), asyncio.CancelledError()])
async def test_unknown_post_never_submits_paid_music_twice(credit_db, error):
    key = await order(paid=True)
    w = worker(song_generator=AsyncMock(side_effect=error))
    if isinstance(error, asyncio.CancelledError):
        with pytest.raises(asyncio.CancelledError):
            await w.process(key)
    else:
        await w.process(key)
    # Even manual reset of the worker cannot create another paid request.
    await database.update_musical_order(key, next_attempt_at=0, failed=0)
    resumed = worker()
    await resumed.process(key)
    resumed.song_generator.assert_not_awaited(); resumed.renderer.assert_not_awaited()
    saved = await database.get_musical_order(key)
    assert saved['failed'] and saved['error'] == 'song_submission_unknown'


async def test_saved_audio_survives_video_failure(credit_db):
    key = await order(paid=True)
    w = worker(renderer=AsyncMock(side_effect=RuntimeError('render failed')))
    await w.process(key)
    await database.update_musical_order(key, next_attempt_at=0)
    resumed = worker()
    await resumed.process(key)
    resumed.song_generator.assert_not_awaited(); resumed.song_waiter.assert_not_awaited()
    resumed.renderer.assert_awaited_once_with(IMAGE, AUDIO)


async def test_delivery_retry_reuses_video_and_song(credit_db):
    key = await order(paid=True)
    w = worker()
    w.uploader.side_effect = RuntimeError('MAX unavailable')
    await w.process(key)
    await database.update_musical_order(key, next_attempt_at=0)
    resumed = worker()
    await resumed.process(key)
    resumed.song_generator.assert_not_awaited(); resumed.renderer.assert_not_awaited()
    assert (await database.get_musical_order(key))['delivered']


async def test_legacy_prepared_upgrade_never_generates_another_song(credit_db):
    await database.create_musical_order('music_old', 456, IMAGE, AUDIO, 'upsell')
    await database.mark_musical_paid('music_old', 'old-payment')
    w = worker()
    await w.process('music_old')
    w.song_generator.assert_not_awaited()
    w.renderer.assert_awaited_once_with(IMAGE, AUDIO)


async def test_direct_prepaid_still_waits_for_photo(credit_db):
    await database.create_prepaid_musical_order('music_direct', 456, 'mode_ny')
    await database.mark_musical_paid('music_direct', 'direct-payment')
    w = worker()
    await w.process('music_direct')
    w.song_generator.assert_not_awaited(); w.renderer.assert_not_awaited()
    assert 'загрузить фото' in w.bot.send_message.await_args.kwargs['text']


@pytest.mark.parametrize('entry', ['upsell_music', 'upsell_video', 'upsell_animate'])
async def test_real_review_invoices_before_any_song_or_image_generation(credit_db, entry):
    payments = SimpleNamespace(create_musical_payment_link=AsyncMock(return_value='https://shop.example/pay'))
    writer = AsyncMock(return_value=DRAFT['lyrics'])
    generator, cards = AsyncMock(), AsyncMock()
    router = handlers.create_router(admin_user_id=999, payments=payments, lyric_writer=writer,
                                    song_generator=generator, postcard_generator=cards)
    ctx = await ready_context()
    await ctx.update_data(mode='mode_ny', last_image_url=IMAGE)
    await dispatch_callback(router, callback_event(entry), ctx)
    await dispatch_callback(router, callback_event('video_song:audio_fun_ny'), ctx)
    await call_handler(router, 'collect_video_song_phrase', message_event('Мама; спасибо за любовь'), ctx)
    d = await ctx.get_data()
    await dispatch_callback(router, callback_event('song_review:voice:female:'+d['song_review_token']), ctx)
    payments.create_musical_payment_link.assert_not_awaited()
    d = await ctx.get_data()
    await dispatch_callback(router, callback_event('song_review:approve:'+d['song_review_token']), ctx)
    d = await ctx.get_data()
    saved = await database.get_musical_order(d['music_checkout_id'])
    assert saved['status'] == 'pending' and saved['amount_kopecks'] == 14900
    assert saved['image_url'] == IMAGE and saved['audio_url'] == ''
    frozen = json.loads(saved['draft'])
    assert frozen['lyrics'] == DRAFT['lyrics'] and frozen['tags'].endswith('female solo vocals')
    cards.assert_not_awaited(); generator.assert_not_awaited(); writer.assert_awaited_once()
    await dispatch_callback(router, callback_event('retry_music_checkout'), ctx)
    assert payments.create_musical_payment_link.await_args.kwargs['order_id'] == saved['order_id']
    writer.assert_awaited_once(); generator.assert_not_awaited()
    await database.mark_musical_paid(saved['order_id'], 'review-payment')
    await ctx.clear()
    resume = callback_event('music_resume:'+saved['order_id'])
    await dispatch_callback(router, resume, ctx)
    assert 'автоматически' in str(resume.send.await_args_list)
    w = worker()
    await w.process(saved['order_id'])
    w.song_generator.assert_awaited_once_with(frozen['lyrics'], frozen['tags'], frozen['title'])
    w.renderer.assert_awaited_once_with(IMAGE, AUDIO)


async def test_cancellation_while_polling_keeps_known_job(credit_db):
    key = await order(paid=True)
    w = worker(song_waiter=AsyncMock(side_effect=asyncio.CancelledError()))
    with pytest.raises(asyncio.CancelledError):
        await w.process(key)
    saved = await database.get_musical_order(key)
    assert not saved['running'] and json.loads(saved['draft'])['song_job_id'] == 'same-song-job'
    resumed = worker()
    await resumed.process(key)
    resumed.song_generator.assert_not_awaited()
    assert (await database.get_musical_order(key))['delivered']


@pytest.mark.parametrize('paid', [False, True])
async def test_another_user_cannot_resume_upgrade(credit_db, paid):
    key = await order(paid=paid)
    router, ctx = handlers.create_router(admin_user_id=999), await ready_context()
    e = callback_event('music_resume:'+key)
    e.get_ids = lambda: (123, 789)
    await dispatch_callback(router, e, ctx)
    assert 'недоступен' in str(e.send.await_args_list)
    assert not (await database.get_musical_order(key))['input_ready']


# Link creation and notification validation use the real payment adapter, without HTTP.
from urllib.parse import urlsplit, parse_qsl
from test_prodamus import payments, payment_config, SECRET
from prodamus import parse_form_fields, sign_link_payload


async def test_prepaid_upgrade_link_has_149_rubles_and_accepts_matching_notification(credit_db, payments):
    key = await order()
    saved = await database.get_musical_order(key)
    link = await payments.create_musical_payment_link(order_id=key, amount=149, description=saved['product_name'])
    outgoing = payments._request_payment_link.await_args.args[0]
    assert outgoing['products'][0]['price'] == '149.00'
    assert outgoing['sign'] == sign_link_payload({k:v for k,v in outgoing.items() if k!='sign'}, SECRET)
    fields = parse_form_fields(parse_qsl(urlsplit(link).query, keep_blank_values=True))
    notification = {'order_num': key, 'order_id': 'provider-prepaid', 'sum': '149.00',
                    'currency': 'rub', 'products': fields['products'],
                    'customer_extra': fields['customer_extra'], '_param_user_id': '456',
                    '_param_credits': '0', '_param_package_id': fields['_param_package_id']}
    validated, payment_id = await payments.validate_payment(notification)
    assert validated['order_type'] == 'musical' and validated['tariff'] == 'upsell'
    assert payment_id == 'provider-prepaid'
    with pytest.raises(ValueError, match='Сумма'):
        await payments.validate_payment({**notification, 'sum': '1.00'})
    assert not await database.pending_musical_orders()
    await database.mark_musical_paid(key, payment_id)
    assert key in await database.pending_musical_orders()


@pytest.mark.parametrize('change_lyrics', [False, True])
async def test_legacy_pending_invoice_is_reused_only_for_the_same_song(credit_db, change_lyrics):
    await database.create_musical_order('music_legacy', 456, IMAGE, AUDIO, 'upsell')
    payments = SimpleNamespace(create_musical_payment_link=AsyncMock(return_value='https://shop.example/legacy'))
    router, ctx = handlers.create_router(admin_user_id=999, payments=payments), await ready_context()
    await ctx.update_data(mode='mode_ny', video_flow=True, video_product='upsell',
        video_amount_kopecks=14900, video_song_scenario='audio_fun_ny', video_song_phrase='Маме с любовью',
        video_source_image=IMAGE, video_song_url=AUDIO, video_song_source=['audio_fun_ny', 'Маме с любовью'],
        video_song_lyrics=DRAFT['lyrics'], music_checkout_id='music_legacy',
        music_checkout_source=[IMAGE, AUDIO, 'upsell', 149])
    if change_lyrics:
        await ctx.update_data(song_review_approved=True, song_review_details='Маме с любовью',
            song_review_lyrics='[Verse]\nНовый текст песни', song_review_voice='female',
            song_review_notes='Маме с любовью', song_review_tags=DRAFT['tags'], song_review_title=DRAFT['title'])
    await ctx.set_state(handlers.PosterStates.waiting_for_video_payment)
    await dispatch_callback(router, callback_event('retry_music_checkout'), ctx)
    key = (await ctx.get_data())['music_checkout_id']
    if change_lyrics:
        assert key != 'music_legacy'
        assert (await database.get_musical_order(key))['prepaid']
    else:
        assert key == 'music_legacy'
    handlers.generate_song.assert_not_awaited()

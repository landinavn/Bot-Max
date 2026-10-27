import asyncio
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from audio_orders import PaidAudioWorker
from audio_service import AudioNotConfigured, SongFailed

RESULT = {'status': 'SUCCESS', 'data': {'musics': [
    {'title': 'Один', 'audioUrl': 'https://audio.example/one.mp3'},
    {'title': 'Два', 'audioUrl': 'https://audio.example/two.mp3'},
]}}


async def order(db, name='audio-one', kind='fun', user_id=123):
    await db.create_audio_order(name, user_id, kind, 'boss' if kind == 'fun' else 'bday',
                               'Аня, любит горы', 'pop', '[Verse]\nАня', 'pop', 'Для Ани')
    return name


async def due(db, name):
    await db.update_audio_order(name, next_attempt_at=0)


async def test_audio_order_is_immutable_and_survives_reinitialization(credit_db):
    name = await order(credit_db)
    await order(credit_db)
    with pytest.raises(ValueError):
        await order(credit_db, user_id=456)
    await credit_db.init_db()
    saved = await credit_db.get_audio_order(name)
    assert saved['user_id'] == 123 and saved['details'] == 'Аня, любит горы'
    assert saved['prompt'] == '[Verse]\nАня' and saved['amount_kopecks'] == 19900


async def test_audio_payment_is_atomic_idempotent_and_counts_actual_revenue(credit_db):
    await credit_db.record_first_start(123, 'yandex_newyear')
    name = await order(credit_db)
    accepted = await asyncio.gather(*(credit_db.mark_audio_paid(name, 'provider-one') for _ in range(5)))
    assert sum(accepted) == 1
    user = await credit_db.get_user(123)
    assert user['balance'] == 0 and user['total_purchases'] == 1 and user['total_generations'] == 0
    assert (await credit_db.get_stats_by_source('yandex_newyear'))['total_revenue'] == 199
    assert (await credit_db.get_all_counters())['buy_audio_fun'] == 1
    with pytest.raises(ValueError):
        await credit_db.mark_audio_paid(name, 'another-payment')


@pytest.mark.parametrize('audio_first', [True, False])
async def test_provider_payment_cannot_be_used_for_both_audio_and_credits(credit_db, audio_first):
    name = await order(credit_db)
    await credit_db.create_payment_order('credits', 123, 3, 19900, '3 генерации')
    if audio_first:
        await credit_db.mark_audio_paid(name, 'same-id')
        with pytest.raises(ValueError):
            await credit_db.add_credits(123, 3, payment_order_id='credits', provider_payment_id='same-id')
    else:
        await credit_db.add_credits(123, 3, payment_order_id='credits', provider_payment_id='same-id')
        with pytest.raises(ValueError):
            await credit_db.mark_audio_paid(name, 'same-id')
    assert (await credit_db.get_user(123))['total_purchases'] == 1


async def test_worker_requires_payment_and_generates_paid_order_once(credit_db):
    name = await order(credit_db)
    bot = SimpleNamespace(send_message=AsyncMock())
    submit, wait = AsyncMock(return_value='job'), AsyncMock(return_value=RESULT)
    worker = PaidAudioWorker(bot, generator=submit, waiter=wait)
    await worker.process(name)
    submit.assert_not_awaited()
    await credit_db.mark_audio_paid(name, 'provider')
    await asyncio.gather(worker.process(name), worker.process(name))
    await worker.process(name)
    submit.assert_awaited_once_with('[Verse]\nАня', 'pop', 'Для Ани')
    wait.assert_awaited_once_with('job')
    saved = await credit_db.get_audio_order(name)
    assert saved['delivered'] == 1 and saved['job_id'] == 'job'
    assert len(json.loads(saved['tracks_json'])) == 2
    assert 'https://audio.example/one.mp3' in bot.send_message.await_args.kwargs['text']
    assert (await credit_db.get_user(123))['balance'] == 0


async def test_worker_resumes_saved_job_after_timeout_and_restart(credit_db):
    name = await order(credit_db)
    await credit_db.mark_audio_paid(name, 'provider')
    submit, wait = AsyncMock(return_value='job'), AsyncMock(side_effect=TimeoutError())
    worker = PaidAudioWorker(SimpleNamespace(send_message=AsyncMock()), generator=submit, waiter=wait)
    await worker.process(name)
    assert (await credit_db.get_audio_order(name))['job_id'] == 'job'
    await credit_db.init_db()
    await credit_db.reset_audio_workers()
    await due(credit_db, name)
    next_submit = AsyncMock()
    after_restart = PaidAudioWorker(SimpleNamespace(send_message=AsyncMock()), generator=next_submit,
                                    waiter=AsyncMock(return_value=RESULT))
    await after_restart.process(name)
    next_submit.assert_not_awaited()
    assert (await credit_db.get_audio_order(name))['delivered'] == 1


async def test_delivery_failure_retries_cached_tracks_without_new_generation(credit_db):
    name = await order(credit_db)
    await credit_db.mark_audio_paid(name, 'provider')
    bot = SimpleNamespace(send_message=AsyncMock(side_effect=[object(), RuntimeError('MAX'), object()]))
    submit, wait = AsyncMock(return_value='job'), AsyncMock(return_value=RESULT)
    worker = PaidAudioWorker(bot, generator=submit, waiter=wait)
    await worker.process(name)
    assert (await credit_db.get_audio_order(name))['tracks_json']
    await due(credit_db, name)
    await worker.process(name)
    submit.assert_awaited_once()
    wait.assert_awaited_once()
    assert (await credit_db.get_audio_order(name))['delivered'] == 1


async def test_unknown_submission_after_restart_is_not_repeated(credit_db):
    name = await order(credit_db)
    await credit_db.mark_audio_paid(name, 'provider')
    await credit_db.update_audio_order(name, submit_started=1, running=1)
    await credit_db.reset_audio_workers()
    submit = AsyncMock()
    bot = SimpleNamespace(send_message=AsyncMock())
    await PaidAudioWorker(bot, generator=submit).process(name)
    submit.assert_not_awaited()
    assert (await credit_db.get_audio_order(name))['failed'] == 1
    assert name in bot.send_message.await_args.kwargs['text']


@pytest.mark.parametrize('error', [SongFailed(), AudioNotConfigured()])
async def test_worker_reports_generation_errors_and_preserves_payment(credit_db, error):
    name = await order(credit_db)
    await credit_db.mark_audio_paid(name, 'provider')
    bot = SimpleNamespace(send_message=AsyncMock())
    worker = PaidAudioWorker(bot, generator=AsyncMock(side_effect=error))
    await worker.process(name)
    saved = await credit_db.get_audio_order(name)
    assert saved['status'] == 'paid' and saved['running'] == 0
    assert bot.send_message.await_count >= 2
    if isinstance(error, AudioNotConfigured):
        assert saved['submit_started'] == saved['failed'] == 0
    else:
        assert saved['failed'] == 1


async def test_worker_does_not_repeat_uncertain_post(credit_db):
    from audio_service import AudioServiceError
    name = await order(credit_db)
    await credit_db.mark_audio_paid(name, 'provider')
    submit = AsyncMock(side_effect=AudioServiceError('unknown submit outcome'))
    bot = SimpleNamespace(send_message=AsyncMock())
    worker = PaidAudioWorker(bot, generator=submit)
    await worker.process(name)
    await worker.process(name)
    submit.assert_awaited_once()
    assert (await credit_db.get_audio_order(name))['failed'] == 1
    assert 'Обратитесь в поддержку' in bot.send_message.await_args.kwargs['text']


async def test_worker_shutdown_preserves_job_for_next_worker(credit_db):
    name = await order(credit_db)
    await credit_db.mark_audio_paid(name, 'provider')
    entered = asyncio.Event()
    async def wait(job):
        entered.set()
        await asyncio.Event().wait()
    submit = AsyncMock(return_value='saved-job')
    worker = PaidAudioWorker(SimpleNamespace(send_message=AsyncMock()), generator=submit, waiter=wait)
    task = asyncio.create_task(worker.process(name))
    await entered.wait()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    saved = await credit_db.get_audio_order(name)
    assert saved['job_id'] == 'saved-job' and saved['running'] == 0
    next_submit = AsyncMock()
    next_worker = PaidAudioWorker(SimpleNamespace(send_message=AsyncMock()), generator=next_submit,
                                  waiter=AsyncMock(return_value=RESULT))
    await next_worker.process(name)
    next_submit.assert_not_awaited()
    assert (await credit_db.get_audio_order(name))['delivered'] == 1


async def test_gift_preview_before_payment_never_reveals_full_url_and_payment_reuses_job(credit_db):
    name = await order(credit_db, kind='gift')
    await credit_db.update_audio_order(name, preview_requested=1)
    submit, wait = AsyncMock(return_value='preview-job'), AsyncMock(return_value=RESULT)
    preview = AsyncMock(return_value='https://preview.example/safe.mp3')
    payments = SimpleNamespace(create_audio_payment_link=AsyncMock(return_value='https://pay.example/490'))
    bot = SimpleNamespace(send_message=AsyncMock())
    worker = PaidAudioWorker(bot, generator=submit, waiter=wait, preview_maker=preview, payments=payments)
    await worker.process(name)
    saved = await credit_db.get_audio_order(name)
    assert saved['status'] == 'pending' and saved['preview_delivered'] == 1 and saved['delivered'] == 0
    message = bot.send_message.await_args.kwargs
    assert 'https://preview.example/safe.mp3' in message['text']
    assert 'https://audio.example/one.mp3' not in message['text']
    buttons = message['attachments'][0].payload.buttons
    assert buttons[0][0].url == 'https://preview.example/safe.mp3'
    assert buttons[1][0].url == 'https://pay.example/490'
    await credit_db.mark_audio_paid(name, 'paid-after-preview')
    await worker.process(name)
    submit.assert_awaited_once()
    wait.assert_awaited_once_with('preview-job')
    assert 'https://audio.example/one.mp3' in bot.send_message.await_args.kwargs['text']
    assert (await credit_db.get_audio_order(name))['delivered'] == 1


async def test_failed_preview_exposes_no_payment_or_full_song(credit_db):
    name = await order(credit_db, kind='gift')
    await credit_db.update_audio_order(name, preview_requested=1)
    payments = SimpleNamespace(create_audio_payment_link=AsyncMock())
    bot = SimpleNamespace(send_message=AsyncMock())
    worker = PaidAudioWorker(bot, generator=AsyncMock(return_value='job'), waiter=AsyncMock(return_value=RESULT),
        preview_maker=AsyncMock(side_effect=RuntimeError('ffmpeg failed')), payments=payments)
    await worker.process(name)
    payments.create_audio_payment_link.assert_not_awaited()
    assert (await credit_db.get_audio_order(name))['preview_url'] is None
    assert all('https://audio.example/' not in call.kwargs['text'] for call in bot.send_message.await_args_list)


async def test_free_regeneration_is_atomic_persistent_and_keeps_original_purchase(credit_db):
    name = await order(credit_db)
    await credit_db.mark_audio_paid(name, 'paid')
    submit = AsyncMock(side_effect=['first-job','second-job'])
    worker = PaidAudioWorker(SimpleNamespace(send_message=AsyncMock()), generator=submit, waiter=AsyncMock(return_value=RESULT))
    await worker.process(name)
    results = await asyncio.gather(*(credit_db.request_audio_regeneration(name, 123) for _ in range(5)))
    assert sum(results) == 1
    await credit_db.init_db()
    await worker.process(name)
    assert submit.await_count == 2
    assert (await credit_db.get_user(123))['total_purchases'] == 1
    assert not await credit_db.request_audio_regeneration(name, 123)
    assert (await credit_db.get_audio_order(name))['previous_tracks_json']


async def test_unpaid_or_other_user_cannot_regenerate(credit_db):
    name = await order(credit_db, kind='gift')
    await credit_db.update_audio_order(name, preview_requested=1, tracks_json=json.dumps(RESULT['data']['musics']))
    assert not await credit_db.request_audio_regeneration(name, 123)
    await credit_db.mark_audio_paid(name, 'paid')
    await credit_db.update_audio_order(name, delivered=1)
    assert not await credit_db.request_audio_regeneration(name, 999)


async def test_checkout_failure_does_not_hide_ready_preview(credit_db):
    name=await order(credit_db,kind='gift')
    await credit_db.update_audio_order(name,preview_requested=1)
    bot=SimpleNamespace(send_message=AsyncMock())
    payments=SimpleNamespace(create_audio_payment_link=AsyncMock(side_effect=RuntimeError('Prodamus timeout')))
    worker=PaidAudioWorker(bot,generator=AsyncMock(return_value='job'),waiter=AsyncMock(return_value=RESULT),payments=payments)
    await worker.process(name)
    sent=bot.send_message.await_args.kwargs
    assert 'https://preview.example/clip.mp3' in sent['text']
    assert 'https://audio.example/one.mp3' not in sent['text']
    buttons=sent['attachments'][0].payload.buttons
    assert buttons[1][0].payload=='audio_gift_checkout:'+name
    assert (await credit_db.get_audio_order(name))['preview_delivered']==1


@pytest.mark.parametrize('access',['paid','admin'])
async def test_unlocked_gift_before_preview_is_delivered_only_once(credit_db,access):
    name=await order(credit_db,kind='gift')
    await credit_db.update_audio_order(name,preview_requested=1)
    if access=='paid':
        await credit_db.mark_audio_paid(name,'paid')
    else:
        await credit_db.grant_audio_admin_access(name,123)
    submit=AsyncMock(return_value='job')
    preview=AsyncMock()
    worker=PaidAudioWorker(SimpleNamespace(send_message=AsyncMock()),generator=submit,
        waiter=AsyncMock(return_value=RESULT),preview_maker=preview)
    await worker.process(name)
    await worker.process(name)
    assert await credit_db.pending_audio_orders()==[]
    submit.assert_awaited_once()
    preview.assert_not_awaited()

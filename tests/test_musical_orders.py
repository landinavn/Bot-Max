import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from maxapi.types import AttachmentUpload

from musical_orders import PaidMusicalWorker
from media_service import MUSICAL_VIDEO_WAIT_MESSAGE


async def paid(credit_db):
    await credit_db.create_musical_order('music_paid',456,'https://cdn.example/card.jpg','https://cdn.example/full.mp3','direct')
    await credit_db.mark_musical_paid('music_paid','provider-music-1')


async def test_paid_music_survives_lost_fsm_and_delivers_only_once(credit_db):
    await paid(credit_db)
    await credit_db.init_db()
    render=AsyncMock(return_value='https://cdn.example/final.mp4')
    upload=AsyncMock(return_value=AttachmentUpload(type='video',payload={'token':'paid-token'}))
    bot=SimpleNamespace(send_message=AsyncMock(return_value=object()))
    worker=PaidMusicalWorker(bot,renderer=render,uploader=upload)
    await asyncio.gather(worker.process('music_paid'),worker.process('music_paid'))
    render.assert_awaited_once_with('https://cdn.example/card.jpg','https://cdn.example/full.mp3')
    assert bot.send_message.await_count == 4
    assert bot.send_message.await_args_list[0].kwargs['text'] == MUSICAL_VIDEO_WAIT_MESSAGE
    assert bot.send_message.await_args.kwargs['user_id'] == 456
    assert bot.send_message.await_args_list[1].kwargs['attachments'][0].type.value == 'video'
    assert bot.send_message.await_args.kwargs['attachments'][0].payload.buttons[0][0].text == '🔗 Поделиться ботом'
    assert (await credit_db.get_musical_order('music_paid'))['delivered'] == 1
    assert await credit_db.pending_musical_orders() == []


async def test_music_delivery_retry_uses_cached_file_without_rendering_again(credit_db):
    await paid(credit_db)
    render=AsyncMock(return_value='https://cdn.example/final.mp4')
    upload=AsyncMock(return_value=object())
    bot=SimpleNamespace(send_message=AsyncMock(side_effect=[object(),RuntimeError('MAX unavailable'),object(),object()]))
    worker=PaidMusicalWorker(bot,renderer=render,uploader=upload)
    await worker.process('music_paid')
    order=await credit_db.get_musical_order('music_paid')
    assert order['running'] == 0 and order['delivered'] == 0 and order['attempts'] == 1
    assert order['result_url'] == 'https://cdn.example/final.mp4'
    await credit_db.update_musical_order('music_paid',next_attempt_at=0)
    await worker.process('music_paid')
    render.assert_awaited_once()
    assert (await credit_db.get_musical_order('music_paid'))['delivered'] == 1


async def test_unpaid_music_is_never_rendered(credit_db):
    await credit_db.create_musical_order('music_unpaid',456,'https://cdn.example/card.jpg','https://cdn.example/full.mp3','upsell')
    render=AsyncMock()
    await PaidMusicalWorker(SimpleNamespace(),renderer=render).process('music_unpaid')
    render.assert_not_awaited()


async def test_music_render_failure_is_retryable_and_does_not_block_other_orders(credit_db):
    await paid(credit_db)
    render=AsyncMock(side_effect=RuntimeError('ffmpeg failed'))
    worker=PaidMusicalWorker(SimpleNamespace(send_message=AsyncMock()),renderer=render)
    await worker.process('music_paid')
    order=await credit_db.get_musical_order('music_paid')
    assert order['running'] == 0 and order['status'] == 'paid'
    assert order['failed'] == 0 and order['next_attempt_at'] > 0


async def test_paid_music_wait_notice_precedes_render_and_is_not_repeated_on_delivery_retry(credit_db):
    await paid(credit_db)
    bot=SimpleNamespace(send_message=AsyncMock(return_value=object()))
    async def render(image_url,audio_url):
        assert bot.send_message.await_args.kwargs['text']==MUSICAL_VIDEO_WAIT_MESSAGE
        assert bot.send_message.await_args.kwargs['user_id']==456
        return 'https://cdn.example/final.mp4'
    worker=PaidMusicalWorker(bot,renderer=AsyncMock(side_effect=render),uploader=AsyncMock(side_effect=[RuntimeError('MAX upload failed'),object()]))
    await worker.process('music_paid')
    await credit_db.update_musical_order('music_paid',next_attempt_at=0)
    await worker.process('music_paid')
    worker.renderer.assert_awaited_once()
    assert sum(c.kwargs['text']==MUSICAL_VIDEO_WAIT_MESSAGE for c in bot.send_message.await_args_list)==1
    assert (await credit_db.get_musical_order('music_paid'))['delivered']==1


async def test_paid_music_share_failure_does_not_retry_paid_render_or_delivery(credit_db,caplog):
    await paid(credit_db)
    bot=SimpleNamespace(send_message=AsyncMock(side_effect=[object(),object(),RuntimeError('share failed')]))
    render=AsyncMock(return_value='https://cdn.example/paid.mp4')
    upload=AsyncMock(return_value=AttachmentUpload(type='video',payload={'token':'paid'}))
    worker=PaidMusicalWorker(bot,renderer=render,uploader=upload,bot_link='https://max.ru/custom_bot')
    await worker.process('music_paid')
    order=await credit_db.get_musical_order('music_paid')
    assert order['delivered']==1 and order['attempts']==0 and order['failed']==0
    assert bot.send_message.await_args.kwargs['attachments'][0].payload.buttons[0][0].url=='https://max.ru/custom_bot'
    await worker.process('music_paid')
    render.assert_awaited_once();upload.assert_awaited_once()

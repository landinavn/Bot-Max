import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from cartoon_orders import PaidCartoonWorker, CARTOON_PAID_PHOTO_PROMPT


async def test_cartoon_order_survives_reinitialization_and_rejects_rebinding(credit_db):
    db = credit_db
    await db.create_cartoon_order('cartoon_456_theme_ny_1', 456, 'theme_ny', 'short')
    await db.init_db()
    row = await db.get_cartoon_order('cartoon_456_theme_ny_1')
    assert (row['user_id'], row['theme'], row['tariff'], row['amount_kopecks']) == (456, 'theme_ny', 'short', 59000)
    assert row['status'] == 'pending' and row['order_type'] == 'cartoon' and row['credits'] == 0
    with pytest.raises(ValueError):
        await db.create_cartoon_order(row['order_id'], 457, 'theme_ny', 'short')
    with pytest.raises(ValueError):
        await db.create_cartoon_order(row['order_id'], 456, 'theme_bday', 'short')


@pytest.mark.parametrize('theme,tariff', [('theme_invalid','short'), ('theme_ny','free')])
async def test_invalid_cartoon_order_rejected(credit_db, theme, tariff):
    with pytest.raises(ValueError):
        await credit_db.create_cartoon_order('cartoon_456_test',456,theme,tariff)


async def test_paid_cartoon_notification_is_durable_retries_and_only_runs_after_payment(credit_db, monkeypatch):
    db = credit_db
    await db.create_cartoon_order('cartoon_456_theme_ny_1',456,'theme_ny','short')
    bot = SimpleNamespace(send_message=AsyncMock(return_value=object()))
    worker = PaidCartoonWorker(bot)
    await worker.process()
    bot.send_message.assert_not_awaited()
    assert await db.mark_cartoon_paid('cartoon_456_theme_ny_1','pay-1') is True
    assert await db.mark_cartoon_paid('cartoon_456_theme_ny_1','pay-1') is False
    assert (await db.get_user(456))['balance'] == 0
    bot.send_message.side_effect = RuntimeError('MAX unavailable')
    await worker.process()
    row = await db.get_cartoon_order('cartoon_456_theme_ny_1')
    assert row['photo_notified'] == 0 and row['next_attempt_at'] > 0
    await db.update_cartoon_order(row['order_id'],next_attempt_at=0)
    bot.send_message.side_effect = None
    await PaidCartoonWorker(bot).process()
    assert bot.send_message.await_count == 2
    assert bot.send_message.await_args.kwargs['text'] == CARTOON_PAID_PHOTO_PROMPT
    button = bot.send_message.await_args.kwargs['attachments'][0].payload.buttons[0][0]
    assert button.payload == 'cartoon_resume:' + row['order_id']
    await worker.process()
    assert bot.send_message.await_count == 2
    assert (await db.get_all_counters())['buy_cartoon_short'] == 1
    assert (await db.get_user(456))['total_purchases'] == 1


async def test_notification_failure_does_not_block_next_paid_customer(credit_db):
    db = credit_db
    for user_id in (456,457):
        await db.create_cartoon_order(f'cartoon_{user_id}_theme_ny_1',user_id,'theme_ny','long')
        await db.mark_cartoon_paid(f'cartoon_{user_id}_theme_ny_1',f'pay-{user_id}')
    bot = SimpleNamespace(send_message=AsyncMock(side_effect=[RuntimeError('blocked'), object()]))
    await PaidCartoonWorker(bot).process()
    assert bot.send_message.await_count == 2
    assert (await db.get_cartoon_order('cartoon_457_theme_ny_1'))['photo_notified'] == 1


@pytest.mark.parametrize('direction', ['cartoon_first','credits_first','audio_first','musical_first'])
async def test_provider_payment_id_cannot_be_used_across_product_families(credit_db, direction):
    db = credit_db
    await db.create_cartoon_order('cartoon_456_theme_ny_1',456,'theme_ny','short')
    await db.create_payment_order('credit-test',456,1,9900,'1 генерация')
    await db.create_audio_order('audio-test',456,'fun','boss','Сергей, лучший босс!','pop','[Verse] lyrics','pop','Босс')
    await db.create_musical_order('music_test',456,'https://cdn.example/a.png','https://cdn.example/a.mp3','direct')
    mark = {
        'credits_first': lambda: db.add_credits(456,1,payment_order_id='credit-test',provider_payment_id='shared-id'),
        'audio_first': lambda: db.mark_audio_paid('audio-test','shared-id'),
        'musical_first': lambda: db.mark_musical_paid('music_test','shared-id'),
    }
    if direction == 'cartoon_first':
        await db.mark_cartoon_paid('cartoon_456_theme_ny_1','shared-id')
        for fn in mark.values():
            with pytest.raises(ValueError):
                await fn()
    else:
        await mark[direction]()
        with pytest.raises(ValueError):
            await db.mark_cartoon_paid('cartoon_456_theme_ny_1','shared-id')


async def test_completed_cartoon_is_not_an_unused_paid_entitlement(credit_db):
    db=credit_db
    await db.create_cartoon_order('cartoon_456_theme_ny_1',456,'theme_ny','short')
    await db.mark_cartoon_paid('cartoon_456_theme_ny_1','pay-1')
    await db.finish_cartoon_order('cartoon_456_theme_ny_1',999)
    assert await db.latest_paid_cartoon(456)
    await db.finish_cartoon_order('cartoon_456_theme_ny_1',456)
    assert await db.latest_paid_cartoon(456) is None
    assert await db.mark_cartoon_paid('cartoon_456_theme_ny_1','pay-1') is False
    assert (await db.get_user(456))['total_purchases']==1

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock

import aiosqlite
import pytest
from maxapi.exceptions import MaxApiError

import database
import generation_fix_notice as notice
import handlers
from test_handlers import message_event, callback_event, call_handler, event_replies


@pytest.fixture(autouse=True)
def no_wait(monkeypatch):
    monkeypatch.setattr(handlers.asyncio, 'sleep', AsyncMock())


async def prepare(db):
    for uid in [101, 102, 103, 104, 456]:
        await db.add_credits(uid, 1)
    await db.mark_user_inactive(102)


async def test_preview_is_private_and_does_not_send_to_clients(credit_db):
    await prepare(credit_db)
    event = message_event('/broadcast_generation_fix 101 102 103 103 456 999')
    await call_handler(handlers.create_router(admin_user_id=456), 'broadcast_generation_fix', event)
    event.bot.send_message.assert_not_awaited()
    assert await notice.pending_recipients() == [101, 103]
    replies = event.message.answer.await_args_list
    assert replies[0].args[0] == notice.NOTICE_TEXT
    assert 'Ожидают отправки: 2' in replies[1].args[0]
    assert replies[1].kwargs['attachments'][0].payload.buttons[0][0].payload == 'broadcast_generation_fix_send'


async def test_confirm_sends_only_selected_active_clients_with_start_and_support(credit_db):
    await prepare(credit_db)
    await notice.prepare_audience([101,102,103],admin_user_id=456)
    event = callback_event('broadcast_generation_fix_send')
    await call_handler(handlers.create_router(admin_user_id=456), 'broadcast_generation_fix_send', event)
    assert [c.kwargs['user_id'] for c in event.bot.send_message.await_args_list] == [101,103]
    for c in event.bot.send_message.await_args_list:
        assert c.kwargs['text'] == notice.NOTICE_TEXT
        attachments = c.kwargs['attachments']
        assert len(attachments) == 1
        buttons = [b for row in attachments[0].payload.buttons for b in row]
        assert buttons[0].payload == 'open_main_menu'
        assert buttons[1].url == handlers.SUPPORT_URL
    assert await notice.selected_counts() == {'sent':2}
    assert (await credit_db.get_user(104))['is_active'] == 1  # Active non-recipient was not contacted.


async def test_current_inactivity_is_rechecked_after_preview(credit_db):
    await prepare(credit_db)
    await notice.prepare_audience([101,103],admin_user_id=456)
    await credit_db.mark_user_inactive(101)
    event = callback_event('broadcast_generation_fix_send')
    await call_handler(handlers.create_router(admin_user_id=456), 'broadcast_generation_fix_send', event)
    assert [c.kwargs['user_id'] for c in event.bot.send_message.await_args_list] == [103]
    assert await notice.selected_counts() == {'sent':1,'inactive':1}


async def test_repeat_and_restart_never_resend_confirmed_delivery(credit_db):
    await prepare(credit_db)
    await notice.prepare_audience([101],admin_user_id=456)
    router = handlers.create_router(admin_user_id=456)
    first = callback_event('broadcast_generation_fix_send')
    await call_handler(router,'broadcast_generation_fix_send',first)
    await credit_db.init_db()
    await notice.prepare_audience([101],admin_user_id=456)
    second = callback_event('broadcast_generation_fix_send')
    await call_handler(handlers.create_router(admin_user_id=456),'broadcast_generation_fix_send',second)
    first.bot.send_message.assert_awaited_once()
    second.bot.send_message.assert_not_awaited()
    assert await notice.selected_counts() == {'sent':1}


async def test_revised_audience_removes_old_pending_targets(credit_db):
    await prepare(credit_db)
    await notice.prepare_audience([101,103],admin_user_id=456)
    await notice.prepare_audience([104],admin_user_id=456)
    assert await notice.pending_recipients() == [104]


async def test_identified_recovery_customer_is_skipped_even_after_preview(credit_db):
    await prepare(credit_db)
    await notice.prepare_audience([101,103],admin_user_id=456)
    async with aiosqlite.connect(database.DATABASE_PATH) as db:
        await db.execute('''INSERT INTO payment_recoveries
            (provider_payment_id,order_number,user_id,photo_order_id,bonus_credits)
            VALUES('receipt','order-number',101,'photo-recovered',1)''')
        await db.commit()
    event = callback_event('broadcast_generation_fix_send')
    await call_handler(handlers.create_router(admin_user_id=456),'broadcast_generation_fix_send',event)
    assert [c.kwargs['user_id'] for c in event.bot.send_message.await_args_list] == [103]
    await notice.prepare_audience([101,103],admin_user_id=456)
    assert await notice.selected_counts() == {'sent':1}


async def test_concurrent_claims_only_allow_one_send(credit_db):
    await prepare(credit_db)
    await notice.prepare_audience([101],admin_user_id=456)
    claims = await asyncio.gather(*(notice.claim_recipient(101) for _ in range(5)))
    assert claims.count(True) == 1
    await credit_db.init_db()
    assert await notice.pending_recipients() == []
    assert await notice.selected_counts() == {'sending':1}


@pytest.mark.parametrize('failure',[None,RuntimeError('network timeout')])
async def test_unconfirmed_delivery_is_not_silently_resent(credit_db,failure):
    await prepare(credit_db)
    await notice.prepare_audience([101,103],admin_user_id=456)
    event = callback_event('broadcast_generation_fix_send')
    event.bot.send_message.side_effect = [failure, object()]
    router = handlers.create_router(admin_user_id=456)
    await call_handler(router,'broadcast_generation_fix_send',event)
    assert await notice.selected_counts() == {'uncertain':1,'sent':1}
    await notice.prepare_audience([101,103],admin_user_id=456)
    again = callback_event('broadcast_generation_fix_send')
    await call_handler(router,'broadcast_generation_fix_send',again)
    again.bot.send_message.assert_not_awaited()


async def test_blocked_client_does_not_block_other_targets(credit_db):
    await prepare(credit_db)
    await notice.prepare_audience([101,103],admin_user_id=456)
    event = callback_event('broadcast_generation_fix_send')
    event.bot.send_message.side_effect = [MaxApiError(code=403,raw='blocked'), object()]
    await call_handler(handlers.create_router(admin_user_id=456),'broadcast_generation_fix_send',event)
    assert not (await credit_db.get_user(101))['is_active']
    assert await notice.selected_counts() == {'inactive':1,'sent':1}


@pytest.mark.parametrize('status',[401,429])
async def test_auth_or_rate_limit_stops_and_preserves_pending(credit_db,status):
    await prepare(credit_db)
    await notice.prepare_audience([101,103],admin_user_id=456)
    event = callback_event('broadcast_generation_fix_send')
    event.bot.send_message.side_effect = MaxApiError(code=status,raw='stop')
    await call_handler(handlers.create_router(admin_user_id=456),'broadcast_generation_fix_send',event)
    event.bot.send_message.assert_awaited_once()
    assert await notice.selected_counts() == {'pending':2}
    assert 'остановлена' in event.send.await_args.args[0]


@pytest.mark.parametrize('name',['broadcast_generation_fix','broadcast_generation_fix_send'])
@pytest.mark.parametrize('admin',[None,999])
async def test_non_admin_cannot_prepare_or_confirm(credit_db,name,admin):
    event = message_event('/broadcast_generation_fix 101') if name=='broadcast_generation_fix' else callback_event('broadcast_generation_fix_send')
    await call_handler(handlers.create_router(admin_user_id=admin),name,event)
    event.bot.send_message.assert_not_awaited()
    event.message.answer.assert_not_awaited()
    assert await notice.selected_counts() == {}


async def test_no_audience_cannot_fall_back_to_all_users(credit_db):
    await prepare(credit_db)
    router = handlers.create_router(admin_user_id=456)
    event = message_event('/broadcast_generation_fix')
    await call_handler(router,'broadcast_generation_fix',event)
    confirm = callback_event('broadcast_generation_fix_send')
    await call_handler(router,'broadcast_generation_fix_send',confirm)
    confirm.bot.send_message.assert_not_awaited()
    assert await notice.selected_counts() == {}


@pytest.mark.parametrize('command',['/broadcast_generation_fix all','/broadcast_generation_fix -1',
    '/broadcast_generation_fix ٠١٢', '/broadcast_generation_fix 0'])
def test_invalid_audience_is_rejected(command):
    with pytest.raises(ValueError):notice.parse_audience(command)

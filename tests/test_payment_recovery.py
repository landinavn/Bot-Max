import asyncio
import hashlib
from types import SimpleNamespace
from unittest.mock import AsyncMock

import aiosqlite
import pytest

import database
import handlers
import payment_recovery as recovery
from test_handlers import (message_event, callback_event, ready_context, call_handler,
                           event_replies, isolated_example_directory)


@pytest.fixture(autouse=True)
def recovery_customer(monkeypatch):
    # Tests use a synthetic address; production customer data is never copied here.
    monkeypatch.setattr(recovery, 'EMAIL_DIGEST', hashlib.sha256(b'customer@example.test').hexdigest())


async def claim(uid=456):
    return await recovery.claim_photo_recovery(uid, recovery.RECOVERY_NUMBER, 'customer@example.test')


async def test_grant_creates_paid_three_frame_package_and_one_gift(credit_db):
    await credit_db.grant_free_trial(456)
    before = await credit_db.get_user(456)
    granted = await claim()
    order = await credit_db.get_photo_order(granted['photo_order_id'])
    after = await credit_db.get_user(456)
    assert order['status'] == 'paid' and order['generation_status'] == 'queued'
    assert order['tariff'] == 590 and order['amount_kopecks'] == 59000
    assert order['provider_payment_id'] == recovery.PROVIDER_PAYMENT_ID
    assert order['style'] == 'photo_style_ny' and order['template'] is None
    assert (await credit_db.get_photo_package_balance(order['order_id'])) == 3
    assert after['balance'] == before['balance'] + 1
    assert after['total_generations'] == before['total_generations']
    assert after['total_purchases'] == before['total_purchases'] + 1
    counters = await credit_db.get_all_counters()
    assert counters['buy_photo_590'] == 1 and counters['gift_payment_recovery'] == 1
    assert not counters.get('buy_1')


async def test_concurrent_retries_and_reinitialization_do_not_duplicate(credit_db):
    results = await asyncio.gather(*(claim() for _ in range(8)))
    assert len({r['photo_order_id'] for r in results}) == 1
    await credit_db.init_db()
    await claim()
    user = await credit_db.get_user(456)
    assert user['balance'] == 1 and user['total_purchases'] == 1
    assert (await credit_db.get_all_counters())['gift_payment_recovery'] == 1


@pytest.mark.parametrize('number,email', [
    ('different-order', 'customer@example.test'),
    (recovery.RECOVERY_NUMBER, 'someone@example.test'),
    (recovery.RECOVERY_NUMBER, ''),
])
async def test_unknown_credentials_never_grant(credit_db, number, email):
    assert await recovery.claim_photo_recovery(456, number, email) is None
    assert await credit_db.get_user(456) is None
    assert await credit_db.get_photo_order(recovery.PHOTO_ORDER_ID) is None


async def test_other_account_cannot_reclaim_receipt(credit_db):
    await claim()
    with pytest.raises(recovery.RecoveryUnavailable):
        await claim(999)
    assert await credit_db.get_user(999) is None
    assert (await credit_db.get_user(456))['balance'] == 1


async def test_existing_payment_on_another_product_blocks_recovery(credit_db):
    await credit_db.create_payment_order('existing', 456, 1, 9900, 'Открытка')
    async with aiosqlite.connect(database.DATABASE_PATH) as db:
        await db.execute("UPDATE payment_orders SET status='paid',provider_payment_id=?", (recovery.PROVIDER_PAYMENT_ID,))
        await db.commit()
    with pytest.raises(recovery.RecoveryUnavailable):
        await claim()
    assert await credit_db.get_photo_order(recovery.PHOTO_ORDER_ID) is None


async def test_failure_rolls_back_photo_bonus_and_statistics(credit_db, monkeypatch):
    monkeypatch.setattr(database, '_record_sale_counters', AsyncMock(side_effect=RuntimeError('fail')))
    with pytest.raises(RuntimeError):
        await claim()
    assert await credit_db.get_user(456) is None
    assert await credit_db.get_photo_order(recovery.PHOTO_ORDER_ID) is None
    assert await recovery.pending_recovery_notification(456) is None


async def test_customer_confirms_email_then_gets_apology_and_resume_button(credit_db, event_replies):
    router = handlers.create_router()
    context = await ready_context()
    entry = message_event('Восстановление заказа ' + recovery.RECOVERY_NUMBER)
    await call_handler(router, 'recover_paid_photo', entry, context)
    assert await context.get_state() == handlers.PosterStates.waiting_for_recovery_email
    assert await credit_db.get_photo_order(recovery.PHOTO_ORDER_ID) is None
    email = message_event('CUSTOMER@example.test')
    await call_handler(router, 'confirm_recovery_email', email, context)
    text = email.message.answer.await_args.args[0]
    assert 'приносим извинения' in text and '3 фотографии' in text
    assert '1 бесплатная генерация открытки' in text
    attachments = email.message.answer.await_args.kwargs['attachments']
    assert len(attachments) == 1
    buttons = [b for row in attachments[0].payload.buttons for b in row]
    assert buttons[0].payload == 'photo_resume:' + recovery.PHOTO_ORDER_ID
    assert await recovery.pending_recovery_notification(456) is None
    assert await context.get_state() == handlers.PosterStates.waiting_for_mode
    await call_handler(router, 'recover_paid_photo', entry, context)
    await call_handler(router, 'confirm_recovery_email', email, context)
    assert (await credit_db.get_user(456))['balance'] == 1


async def test_failed_notification_keeps_entitlement_and_can_be_retried(credit_db, event_replies):
    router = handlers.create_router()
    context = await ready_context()
    email = message_event('customer@example.test')
    email.message.answer.side_effect = RuntimeError('MAX down')
    with pytest.raises(RuntimeError):
        await call_handler(router, 'confirm_recovery_email', email, context)
    assert (await credit_db.get_user(456))['balance'] == 1
    pending = await recovery.pending_recovery_notification(456)
    assert pending and not pending['notified']
    email.message.answer.side_effect = None
    await handlers.notify_payment_recovery(email, pending)
    assert await recovery.pending_recovery_notification(456) is None
    assert (await credit_db.get_user(456))['balance'] == 1


async def test_none_send_result_does_not_mark_notification_delivered(credit_db, event_replies):
    pending = await claim()
    event = message_event()
    event.message.answer.return_value = None
    with pytest.raises(RuntimeError):
        await handlers.notify_payment_recovery(event, pending)
    assert await recovery.pending_recovery_notification(456)


async def test_recovered_package_resumes_plot_selection_without_payment(credit_db, event_replies, isolated_example_directory):
    await claim()
    context = await ready_context()
    event = callback_event('photo_resume:' + recovery.PHOTO_ORDER_ID)
    payments = SimpleNamespace(create_payment_link=AsyncMock())
    router = handlers.create_router(payments=payments)
    await call_handler(router, 'resume_paid_photo', event, context)
    data = await context.get_data()
    assert data['photo_order_id'] == recovery.PHOTO_ORDER_ID and data['photo_tariff'] == 'photo_tariff_590'
    assert await context.get_state() == handlers.PosterStates.waiting_for_mode
    payload = next(p for p, (_, key) in handlers.PHOTO_NY_TRIAL_TEMPLATES.items()
                   if key in handlers.NY_PHOTOSESSION_PROMPTS_BY_TARIFF[590])
    await call_handler(router, 'photo_template_checkout', callback_event(payload), context)
    assert await context.get_state() == handlers.PosterStates.waiting_for_photosession_photo
    assert (await context.get_data())['photo_order_id'] == recovery.PHOTO_ORDER_ID
    payments.create_payment_link.assert_not_awaited()
    assert (await credit_db.get_user(456))['balance'] == 1


async def test_real_dispatch_claim_and_return_after_restart(credit_db, monkeypatch, event_replies, isolated_example_directory):
    from maxapi import Dispatcher
    from maxapi.context import MemoryContext
    from maxapi.context.isolation import SimpleEventIsolation
    from maxapi.types import MessageCreated, User
    monkeypatch.setattr(handlers, '_reply', event_replies)
    # A real client had already consumed her trial before the failed payment.
    await credit_db.grant_free_trial(456)
    await credit_db.deduct_credit(456, count_generation=True)
    bot = SimpleNamespace(me=User(user_id=999, first_name='Bot', is_bot=True),
                          send_message=AsyncMock(return_value=object()))
    def event(text):
        return MessageCreated(timestamp=1793650000000, bot=bot, message={
            'sender': {'user_id': 456, 'first_name': 'Customer', 'is_bot': False},
            'recipient': {'chat_id': 123, 'chat_type': 'dialog'}, 'timestamp': 1793650000000,
            'body': {'mid': 'recovery-test', 'seq': 1, 'text': text}})
    dp = Dispatcher(storage=MemoryContext, event_isolation=SimpleEventIsolation())
    dp.include_routers(handlers.create_router())
    try:
        await dp.handle(event('Восстановление заказа ' + recovery.RECOVERY_NUMBER))
        assert await dp.fsm.get_context(chat_id=123, user_id=456).get_state() == handlers.PosterStates.waiting_for_recovery_email
        await dp.handle(event('customer@example.test'))
        assert (await credit_db.get_user(456))['balance'] == 1
        assert await recovery.pending_recovery_notification(456) is None
        # Simulate notification not being acknowledged, followed by fresh FSM.
        async with aiosqlite.connect(database.DATABASE_PATH) as db:
            await db.execute('UPDATE payment_recoveries SET notified=0')
            await db.commit()
        await dp.fsm.get_context(chat_id=123, user_id=456).clear()
        bot.send_message.reset_mock()
        await dp.handle(event('/start'))
        apologies = [c for c in bot.send_message.await_args_list if 'приносим извинения' in c.kwargs.get('text', '')]
        assert len(apologies) == 1
        assert (await credit_db.get_user(456))['balance'] == 1
        assert await recovery.pending_recovery_notification(456) is None
    finally:
        await dp.shutdown()


async def test_group_message_does_not_collect_email_or_restore(credit_db, event_replies):
    context = await ready_context()
    before = await context.get_state()
    event = message_event(recovery.RECOVERY_NUMBER, chat_type='chat')
    await call_handler(handlers.create_router(), 'recover_paid_photo', event, context)
    assert await context.get_state() == before
    assert await credit_db.get_photo_order(recovery.PHOTO_ORDER_ID) is None
    assert 'личный диалог' in event.message.answer.await_args.args[0]

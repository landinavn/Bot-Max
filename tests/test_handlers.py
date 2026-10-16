"""Callback не должен отправлять пустое подтверждение в MAX."""

from types import SimpleNamespace
from unittest.mock import AsyncMock
from inspect import signature

import pytest
from maxapi.context import MemoryContext
from maxapi.enums.update import UpdateType

from handlers import PACKAGE_OFFER_TEXT, PACKAGES_TEXT, PosterStates, create_router


@pytest.fixture(autouse=True)
async def isolated_database(credit_db):
    return credit_db


def callback_event(payload, *, deleted=False):
    async def acknowledge(*, notification=None):
        if not notification:
            raise AssertionError("MAX требует notification для ack")

    return SimpleNamespace(
        get_ids=lambda: (123, 456),
        callback=SimpleNamespace(payload=payload),
        message=None if deleted else SimpleNamespace(
            recipient=SimpleNamespace(chat_id=123), answer=AsyncMock(),
        ),
        ack=AsyncMock(side_effect=acknowledge),
        send=AsyncMock(),
        bot=SimpleNamespace(send_message=AsyncMock(return_value=object())),
    )


async def dispatch_callback(router, event, context):
    matches = [
        handler for handler in router.event_handlers
        if handler.update_type == UpdateType.MESSAGE_CALLBACK
        and all(filter_.resolve(event) for filter_ in handler.filters)
    ]
    assert len(matches) == 1
    func = matches[0].func_event
    kwargs = {"context": context} if "context" in signature(func).parameters else {}
    await func(event, **kwargs)


async def test_create_poster_acknowledges_and_requests_photo():
    context = MemoryContext(chat_id=123, user_id=456)
    await context.update_data(photo={"url": "old"}, sphere="old")
    await context.set_state(PosterStates.waiting_for_sphere)
    event = callback_event("create_poster")
    await dispatch_callback(create_router(), event, context)
    event.ack.assert_awaited_once_with(notification="Загружаю…")
    event.send.assert_awaited_once_with(
        "Отправьте одно фото для плаката. /cancel — отмена."
    )
    assert await context.get_state() == PosterStates.waiting_for_photo
    assert await context.get_data() == {}


async def test_create_poster_preserves_running_generation():
    context = MemoryContext(chat_id=123, user_id=456)
    await context.set_state(PosterStates.generating)
    await context.update_data(sphere="карьера")
    event = callback_event("create_poster")
    await dispatch_callback(create_router(), event, context)
    event.ack.assert_awaited_once()
    event.send.assert_not_awaited()
    assert await context.get_state() == PosterStates.generating
    assert await context.get_data() == {"sphere": "карьера"}


async def test_create_poster_handles_deleted_message():
    context = MemoryContext(chat_id=123, user_id=456)
    event = callback_event("create_poster", deleted=True)
    await dispatch_callback(create_router(), event, context)
    event.ack.assert_awaited_once()
    event.send.assert_not_awaited()
    assert await context.get_state() is None


@pytest.mark.parametrize("payload", ["examples", "help"])
async def test_menu_acknowledges_and_sends_response(payload):
    context = MemoryContext(chat_id=123, user_id=456)
    event = callback_event(payload)
    await dispatch_callback(create_router(), event, context)
    event.ack.assert_awaited_once_with(notification="Открываю…")
    event.send.assert_awaited_once()


async def test_share_acknowledges_and_sends_link():
    context = MemoryContext(chat_id=123, user_id=456)
    url = "https://storage.example.com/photo.png"
    await context.update_data(batch_id="batch", variants=[url])
    event = callback_event("share:batch:0")
    await dispatch_callback(create_router(), event, context)
    event.ack.assert_awaited_once_with(notification="Готовлю ссылку…")
    event.send.assert_awaited_once_with(f"Скопируйте и отправьте эту ссылку:\n{url}")


async def test_more_acknowledges_before_generating_one_image(credit_db):
    await credit_db.add_credits(456, 1)
    context = MemoryContext(chat_id=123, user_id=456)
    await context.update_data(
        batch_id="batch", sent_count=1,
        photo={"url": "https://photos.example.com/source.png"}, sphere="карьера",
    )
    event = callback_event("more:batch")
    generator = AsyncMock(return_value=["https://storage.example.com/new.png"])
    await dispatch_callback(create_router(generator), event, context)
    event.ack.assert_awaited_once_with(notification="Создаю плакат…")
    generator.assert_awaited_once_with(
        "https://photos.example.com/source.png", "карьера"
    )
    assert event.bot.send_message.await_count == 1
    assert await context.get_state() is None


def message_event(text="карьера", *, chat_id=123):
    return SimpleNamespace(
        get_ids=lambda: (chat_id, 456),
        message=SimpleNamespace(
            sender=SimpleNamespace(is_bot=False, user_id=456),
            recipient=SimpleNamespace(chat_id=chat_id),
            body=SimpleNamespace(text=text),
            answer=AsyncMock(),
        ),
        bot=SimpleNamespace(send_message=AsyncMock(return_value=object())),
    )


async def call_handler(router, name, event, context=None):
    handler = next(h.func_event for h in router.event_handlers if h.func_event.__name__ == name)
    if context is None:
        await handler(event)
    else:
        await handler(event, context)


async def ready_context(*, chat_id=123):
    context = MemoryContext(chat_id=chat_id, user_id=456)
    await context.update_data(photo={"url": "https://photos.example.com/source.png"})
    await context.set_state(PosterStates.waiting_for_sphere)
    return context


async def test_start_grants_only_one_credit_across_chats_and_restart(credit_db):
    event = message_event("/start")
    await call_handler(create_router(), "start", event, await ready_context())
    assert (await credit_db.get_user(456))["balance"] == 1
    assert event.message.answer.await_args_list[-1].args == (
        "Вам начислен 1 бесплатный кредит!",
    )
    await credit_db.deduct_credit(456)
    await credit_db.init_db()
    repeated = message_event("/start", chat_id=789)
    await call_handler(create_router(), "start", repeated, await ready_context(chat_id=789))
    assert (await credit_db.get_user(456))["balance"] == 0
    repeated.message.answer.assert_awaited_once()


async def test_buy_describes_packages_without_adding_credits(credit_db):
    event = message_event("/buy")
    await call_handler(create_router(), "buy", event)
    text = event.message.answer.await_args.args[0]
    assert "5 генераций — 299 ₽" in text
    assert "15 генераций — 699 ₽" in text
    assert "Оплата временно недоступна" in text
    assert await credit_db.get_user(456) is None


async def test_no_credits_prevents_generation(credit_db):
    generator = AsyncMock()
    event = message_event()
    context = await ready_context()
    await call_handler(create_router(generator), "collect_sphere", event, context)
    generator.assert_not_awaited()
    event.bot.send_message.assert_not_awaited()
    event.message.answer.assert_awaited_once_with(
        "У вас закончились кредиты. Купите пакет: /buy"
    )
    assert await context.get_state() == PosterStates.waiting_for_sphere
    assert not any(
        call.args and call.args[0] == PACKAGE_OFFER_TEXT
        for call in event.message.answer.await_args_list
    )


async def test_one_image_costs_one_credit_and_shows_balance_without_offer(credit_db):
    await credit_db.add_credits(456, 2)
    generator = AsyncMock(return_value=["https://storage.example.com/photo.png"])
    event = message_event()
    context = await ready_context()
    await call_handler(create_router(generator), "collect_sphere", event, context)
    assert (await credit_db.get_user(456))["balance"] == 1
    assert event.bot.send_message.await_count == 1
    event.message.answer.assert_any_await("Осталось кредитов: 1.")
    assert await context.get_state() is None
    assert not any(
        call.args and call.args[0] == PACKAGE_OFFER_TEXT
        for call in event.message.answer.await_args_list
    )


@pytest.mark.parametrize("failure", ["timeout", "bad-result"])
async def test_failed_generation_does_not_charge(credit_db, failure):
    from image_service import GenerationTimeoutError

    await credit_db.add_credits(456, 1)
    generator = AsyncMock(return_value=["not-a-valid-result"])
    if failure == "timeout":
        generator.side_effect = GenerationTimeoutError("test")
    context = await ready_context()
    await call_handler(create_router(generator), "collect_sphere", message_event(), context)
    assert (await credit_db.get_user(456))["balance"] == 1
    assert await context.get_state() == PosterStates.waiting_for_sphere


async def test_failed_image_send_retry_uses_paid_image_and_offers_once(credit_db):
    await credit_db.add_credits(456, 1)
    generator = AsyncMock(return_value=["https://storage.example.com/photo.png"])
    router = create_router(generator)
    event = message_event()
    event.bot.send_message.side_effect = RuntimeError("send failed")
    context = await ready_context()
    await call_handler(router, "collect_sphere", event, context)
    assert (await credit_db.get_user(456))["balance"] == 0
    assert (await context.get_data())["sent_count"] == 0
    event.bot.send_message.reset_mock(side_effect=True)
    await call_handler(router, "collect_sphere", event, context)
    generator.assert_awaited_once()
    assert event.bot.send_message.await_count == 1
    assert (await credit_db.get_user(456))["balance"] == 0
    assert await context.get_state() is None
    assert sum(
        bool(call.args and call.args[0] == PACKAGE_OFFER_TEXT)
        for call in event.message.answer.await_args_list
    ) == 1


async def test_concurrent_generations_across_chats_share_one_balance(credit_db):
    import asyncio

    await credit_db.add_credits(456, 1)
    generator = AsyncMock(return_value=["https://storage.example.com/photo.png"])
    router = create_router(generator)
    first, second = message_event(), message_event(chat_id=789)
    await asyncio.gather(
        call_handler(router, "collect_sphere", first, await ready_context()),
        call_handler(router, "collect_sphere", second, await ready_context(chat_id=789)),
    )
    generator.assert_awaited_once()
    assert (await credit_db.get_user(456))["balance"] == 0


async def test_last_free_credit_offers_packages_once_and_not_on_zero_balance(credit_db):
    await credit_db.grant_free_trial(456)
    generator = AsyncMock(return_value=["https://storage.example.com/photo.png"])
    router = create_router(generator)
    event = message_event()
    context = await ready_context()
    await call_handler(router, "collect_sphere", event, context)
    assert (await credit_db.get_user(456))["balance"] == 0
    offer = next(
        call for call in event.message.answer.await_args_list
        if call.args and call.args[0] == PACKAGE_OFFER_TEXT
    )
    keyboard = offer.kwargs["attachments"][0].model_dump()
    buttons = [button for row in keyboard["payload"]["buttons"] for button in row]
    assert [button["text"] for button in buttons] == [
        "Купить 5 генераций", "Купить 15 генераций",
    ]
    assert [button["payload"] for button in buttons] == ["buy_5", "buy_15"]
    # Повторное сообщение о той же сфере использует готовый оплаченный результат.
    await call_handler(router, "collect_sphere", event, context)
    assert sum(
        bool(call.args and call.args[0] == PACKAGE_OFFER_TEXT)
        for call in event.message.answer.await_args_list
    ) == 1
    more = callback_event("more:" + (await context.get_data())["batch_id"])
    await dispatch_callback(router, more, context)
    more.message.answer.assert_awaited_once_with(
        "У вас закончились кредиты. Купите пакет: /buy"
    )
    generator.assert_awaited_once()
    assert event.bot.send_message.await_count == 1
    more.bot.send_message.assert_not_awaited()


@pytest.mark.parametrize("payload", ["buy_5", "buy_15"])
async def test_package_buttons_show_payment_stub_without_charging(credit_db, payload):
    await credit_db.add_credits(456, 2)
    event = callback_event(payload)
    await dispatch_callback(create_router(), event, await ready_context())
    event.ack.assert_awaited_once()
    assert event.send.await_args.args[0] == PACKAGES_TEXT
    assert event.send.await_args.kwargs["attachments"]
    assert (await credit_db.get_user(456))["balance"] == 2


@pytest.mark.parametrize("payload,credits,price", [("buy_5", 5, "299.00"), ("buy_15", 15, "699.00")])
async def test_purchase_button_creates_signed_link_for_callback_user(credit_db, payload, credits, price):
    from pathlib import Path
    from urllib.parse import parse_qsl, urlsplit

    from config import Config
    from prodamus import ProdamusPayments, parse_form_fields, verify_signature

    payments = ProdamusPayments(Config(
        token="test-token", photo_path=Path("unused.png"),
        prodamus_domain="test-shop.payform.ru", prodamus_secret_key="test-secret",
        prodamus_return_url="https://max.ru/test_bot",
        prodamus_webhook_url="https://bot.example.com/prodamus/webhook",
    ))
    event = callback_event(payload)
    await dispatch_callback(create_router(payments=payments), event, await ready_context())
    event.ack.assert_awaited_once_with(notification="Готовлю ссылку на оплату…")
    sent = event.send.await_args
    keyboard = sent.kwargs["attachments"][0].model_dump()
    link = keyboard["payload"]["buttons"][0][0]["url"]
    assert link in sent.args[0]
    data = parse_form_fields(parse_qsl(urlsplit(link).query))
    assert verify_signature(data, data["signature"], "test-secret")
    assert data["products"][0]["price"] == price
    order = await credit_db.get_payment_order(data["order_id"])
    assert order["user_id"] == 456
    assert order["credits"] == credits
    assert await credit_db.get_user(456) is None


async def test_purchase_link_error_is_reported_without_charging(credit_db):
    payments = SimpleNamespace(create_payment_link=AsyncMock(side_effect=RuntimeError("test")))
    event = callback_event("buy_5")
    await dispatch_callback(create_router(payments=payments), event, await ready_context())
    event.send.assert_awaited_once_with(
        "Не удалось создать ссылку на оплату. Попробуйте /buy позже."
    )
    assert await credit_db.get_user(456) is None

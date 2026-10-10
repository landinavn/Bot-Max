"""Callback не должен отправлять пустое подтверждение в MAX."""

from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from maxapi.context import MemoryContext
from maxapi.enums.update import UpdateType

from handlers import PosterStates, create_router


def callback_event(payload, *, deleted=False):
    async def acknowledge(*, notification=None):
        if not notification:
            raise AssertionError("MAX требует notification для ack")

    return SimpleNamespace(
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
    await matches[0].func_event(event, context)


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
    await context.update_data(batch_id="batch", variants=[url] * 3)
    event = callback_event("share:batch:0")
    await dispatch_callback(create_router(), event, context)
    event.ack.assert_awaited_once_with(notification="Готовлю ссылку…")
    event.send.assert_awaited_once_with(f"Скопируйте и отправьте эту ссылку:\n{url}")


async def test_more_acknowledges_before_generating_variants():
    context = MemoryContext(chat_id=123, user_id=456)
    await context.update_data(
        batch_id="batch", sent_count=3,
        photo={"url": "https://photos.example.com/source.png"}, sphere="карьера",
    )
    event = callback_event("more:batch")
    generator = AsyncMock(return_value=["https://storage.example.com/new.png"] * 3)
    await dispatch_callback(create_router(generator), event, context)
    event.ack.assert_awaited_once_with(notification="Создаю варианты…")
    generator.assert_awaited_once_with(
        "https://photos.example.com/source.png", "карьера"
    )
    assert event.bot.send_message.await_count == 3
    assert await context.get_state() is None

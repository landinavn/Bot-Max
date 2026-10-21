"""Callback не должен отправлять пустое подтверждение в MAX."""

from types import SimpleNamespace
from pathlib import Path
from unittest.mock import AsyncMock, Mock, call
from inspect import signature
import logging

import pytest
from maxapi.context import MemoryContext
from maxapi.enums.update import UpdateType
from maxapi.exceptions import MaxApiError
from maxapi.types import AttachmentUpload, PhotoAttachmentPayload

import handlers
from handlers import EXAMPLE_TEXT, NO_CREDITS_TEXT, PACKAGES_TEXT, PosterStates, create_router


@pytest.fixture(autouse=True)
async def isolated_database(credit_db):
    return credit_db


@pytest.fixture(autouse=True)
def event_replies(monkeypatch):
    # Сценарии FSM проверяют содержание ответов; маршрутизация проверяется отдельно.
    real_reply = handlers._reply

    async def reply(event, *args, **kwargs):
        sender = event.send if hasattr(event, "send") else event.message.answer
        return await sender(*args, **kwargs)

    monkeypatch.setattr(handlers, "_reply", reply)
    return real_reply


@pytest.fixture(autouse=True)
def max_image_upload(monkeypatch):
    image = AttachmentUpload(type="image", payload={"token": "uploaded-poster-token"})
    upload = AsyncMock(return_value=image)
    monkeypatch.setattr(handlers, "upload_image_for_max", upload)
    return upload


def assert_purchase_keyboard(keyboard):
    markup = keyboard.model_dump()
    assert markup["type"] == "inline_keyboard"
    buttons = [button for row in markup["payload"]["buttons"] for button in row]
    assert [(button["text"], button["payload"], button["type"]) for button in buttons] == [
        ("Купить 1 генерацию — 99 ₽", "buy_1", "callback"),
        ("Купить 5 генераций — 299 ₽", "buy_5", "callback"),
        ("Купить 15 генераций — 799 ₽", "buy_15", "callback"),
    ]


def callback_event(payload, *, deleted=False):
    async def acknowledge(*, notification=None):
        if not notification:
            raise AssertionError("MAX требует notification для ack")

    reply = AsyncMock()
    return SimpleNamespace(
        get_ids=lambda: (123, 456),
        callback=SimpleNamespace(payload=payload),
        message=None if deleted else SimpleNamespace(
            recipient=SimpleNamespace(chat_id=123, chat_type="dialog"), answer=reply,
        ),
        ack=AsyncMock(side_effect=acknowledge),
        send=reply,
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
    await context.set_state(PosterStates.waiting_for_prompt)
    event = callback_event("create_poster")
    await dispatch_callback(create_router(), event, context)
    event.ack.assert_awaited_once_with(notification="Загружаю…")
    event.send.assert_awaited_once_with(
        "Отправьте одно фото для плаката. /cancel — отмена."
    )
    assert await context.get_state() == PosterStates.waiting_for_photo
    assert await context.get_data() == {"mode": "mode_vision"}


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


async def test_help_acknowledges_and_sends_response():
    context = MemoryContext(chat_id=123, user_id=456)
    event = callback_event("help")
    await dispatch_callback(create_router(), event, context)
    event.ack.assert_awaited_once_with(notification="Открываю…")
    event.send.assert_awaited_once()
    assert event.send.await_args.args == (handlers.HELP_TEXT,)
    text = event.send.await_args.args[0]
    assert text.startswith("🆘 Помощь\n\n")
    assert "Машина тесла с бордовым салоном, я за рулем" in text
    assert "Я на пляже на Мальдивах" in text
    assert "1 бесплатная генерация — для новых пользователей." in text
    assert "Пакет 5 генераций — 299 ₽." in text
    assert "Пакет 15 генераций — 799 ₽." in text
    assert text.endswith("❓ Частые вопросы:")
    keyboard = event.send.await_args.kwargs["attachments"][0].model_dump()
    buttons = [button for row in keyboard["payload"]["buttons"] for button in row]
    assert [(button["text"], button["payload"]) for button in buttons] == [
        ("Как оплатить?", "help_payment"),
        ("Не работает генерация", "help_generation"),
        ("Связаться с поддержкой", "help_support"),
    ]


@pytest.mark.parametrize("payload,answer", [
    ("help_payment", "Выберите пакет для оплаты."),
    ("help_generation", "Попробуйте отправить другое фото или повторите позже. "
     'Если ошибка повторяется, напишите в поддержку: '
     '<a href="https://max.ru/u/f9LHodD0cOL0i7XGM4uyyUo-bFy4nt4Hqc-XMNmvGygT83X2wUkr6O7DRXo">'
     'Связаться с поддержкой</a>'),
    ("help_support", 'Напишите нам: <a href="https://max.ru/u/f9LHodD0cOL0i7XGM4uyyUo-bFy4nt4Hqc-XMNmvGygT83X2wUkr6O7DRXo">Связаться с поддержкой</a>'),
])
async def test_help_buttons_respond_without_affecting_generation(payload, answer):
    context = MemoryContext(chat_id=123, user_id=456)
    await context.set_state(PosterStates.generating)
    data = {"photo": {"url": "original"}, "sphere": "мечта"}
    await context.update_data(**data)
    event = callback_event(payload)
    generator = AsyncMock()
    await dispatch_callback(create_router(generator), event, context)
    event.ack.assert_awaited_once_with(notification="Открываю…")
    if payload in {"help_support", "help_generation"}:
        event.send.assert_awaited_once_with(answer, parse_mode="html")
    else:
        event.send.assert_awaited_once()
        assert event.send.await_args.args == (answer,)
        assert_purchase_keyboard(event.send.await_args.kwargs["attachments"][0])
    generator.assert_not_awaited()
    assert await context.get_state() == PosterStates.generating
    assert await context.get_data() == data


@pytest.mark.parametrize("payload", ["help", "help_payment", "help_generation", "help_support"])
async def test_help_buttons_handle_deleted_message(payload):
    event = callback_event(payload, deleted=True)
    await dispatch_callback(create_router(), event, MemoryContext(chat_id=123, user_id=456))
    event.ack.assert_awaited_once_with(notification="Меню недоступно. Отправьте /start.")
    event.send.assert_not_awaited()


@pytest.fixture
def example_photos(tmp_path, monkeypatch):
    directory = tmp_path / "project" / "examples"
    directory.mkdir(parents=True)
    for filename in ("before.jpg", "after.jpg", *handlers.START_EXAMPLE_FILES):
        (directory / filename).write_bytes(b"local-photo")
    monkeypatch.setattr(handlers, "EXAMPLES_DIR", directory)
    # Файлы ищутся относительно проекта, а не рабочей директории процесса.
    monkeypatch.chdir(tmp_path)
    return directory


async def test_examples_uploads_local_photos_and_sends_in_order(example_photos):
    context = MemoryContext(chat_id=123, user_id=456)
    await context.set_state(PosterStates.generating)
    await context.update_data(photo={"url": "original"}, sphere="карьера")
    event = callback_event("examples")
    images = [
        AttachmentUpload(type="image", payload={"token": token})
        for token in ("before-token", "after-token")
    ]
    event.bot.upload_media = AsyncMock(side_effect=images)
    timeline = Mock()
    timeline.attach_mock(event.send, "send")
    timeline.attach_mock(event.bot.upload_media, "upload")
    generator = AsyncMock()

    await dispatch_callback(create_router(generator), event, context)

    uploads = event.bot.upload_media.await_args_list
    assert [entry.args[0].path for entry in uploads] == [
        str(example_photos / "before.jpg"), str(example_photos / "after.jpg")
    ]
    assert all(entry.args[0].type == "image" for entry in uploads)
    assert timeline.mock_calls == [
        call.send(EXAMPLE_TEXT),
        call.upload(uploads[0].args[0]),
        call.send("Фото до:", attachments=[images[0]]),
        call.upload(uploads[1].args[0]),
        call.send("Результат:", attachments=[images[1]]),
    ]
    event.ack.assert_awaited_once_with(notification="Открываю…")
    generator.assert_not_awaited()
    assert await context.get_state() == PosterStates.generating
    assert await context.get_data() == {"photo": {"url": "original"}, "sphere": "карьера"}


@pytest.mark.parametrize("missing", ["before.jpg", "after.jpg"])
async def test_examples_missing_photo_sends_fallback(example_photos, missing):
    (example_photos / missing).unlink()
    event = callback_event("examples")
    event.bot.upload_media = AsyncMock()
    await dispatch_callback(create_router(), event, MemoryContext(chat_id=123, user_id=456))
    event.send.assert_awaited_once_with("Пример временно недоступен. Попробуйте позже.")
    event.bot.upload_media.assert_not_awaited()


@pytest.mark.parametrize("failure", ["upload", "empty_token", "send"])
async def test_examples_reports_upload_and_send_failures(example_photos, failure):
    event = callback_event("examples")
    image = AttachmentUpload(type="image", payload={"token": "" if failure == "empty_token" else "token"})
    event.bot.upload_media = AsyncMock(return_value=image)
    if failure == "upload":
        event.bot.upload_media.side_effect = RuntimeError("upload failed")
    elif failure == "send":
        event.send.side_effect = [object(), None, object()]
    await dispatch_callback(create_router(), event, MemoryContext(chat_id=123, user_id=456))
    assert event.send.await_args.args == ("Не удалось отправить пример. Попробуйте позже.",)
    assert event.bot.upload_media.await_count == 1


async def test_examples_handles_deleted_menu():
    event = callback_event("examples", deleted=True)
    await dispatch_callback(create_router(), event, MemoryContext(chat_id=123, user_id=456))
    event.ack.assert_awaited_once_with(notification="Меню недоступно. Отправьте /start.")
    event.send.assert_not_awaited()


@pytest.mark.parametrize("payload", ["share", "share:batch:0"])
async def test_share_acknowledges_and_sends_link(payload):
    context = MemoryContext(chat_id=123, user_id=456)
    url = "https://storage.example.com/photo.png"
    await context.update_data(batch_id="batch", variants=[url])
    event = callback_event(payload)
    await dispatch_callback(create_router(), event, context)
    event.ack.assert_awaited_once_with(notification="Готовлю ссылку…")
    event.send.assert_awaited_once_with(
        f'Вы можете скачать картинку или поделиться ссылкой:\n<a href="{url}">{url}</a>',
        parse_mode=handlers.ParseMode.HTML,
    )


@pytest.mark.parametrize("payload", ["more", "more:batch"])
async def test_more_requests_new_dream_and_keeps_photo(credit_db, payload):
    await credit_db.add_credits(456, 1)
    context = MemoryContext(chat_id=123, user_id=456)
    await context.update_data(
        batch_id="batch", sent_count=1,
        photo={"url": "https://photos.example.com/source.png"}, sphere="карьера",
    )
    event = callback_event(payload)
    generator = AsyncMock(return_value=["https://storage.example.com/new.png"])
    await dispatch_callback(create_router(generator), event, context)
    event.ack.assert_awaited_once_with(notification="Жду новое описание мечты.")
    generator.assert_not_awaited()
    event.bot.send_message.assert_not_awaited()
    assert await context.get_state() == PosterStates.waiting_for_prompt
    data = await context.get_data()
    assert data["photo"] == {"url": "https://photos.example.com/source.png"}
    assert data["sphere"] is None
    assert data["new_batch_requested"] is True
    assert (await credit_db.get_user(456))["balance"] == 1
    event.send.assert_awaited_once()


@pytest.mark.parametrize("payload", ["more", "more:old-batch"])
@pytest.mark.parametrize("photo", [None, {}, {"token": "old-token"}])
async def test_more_without_photo_requests_new_upload(payload, photo):
    context = MemoryContext(chat_id=123, user_id=456)
    await context.update_data(photo=photo)
    event = callback_event(payload)
    generator = AsyncMock()
    await dispatch_callback(create_router(generator), event, context)
    event.ack.assert_awaited_once()
    assert await context.get_state() == PosterStates.waiting_for_photo
    assert await context.get_data() == {"mode": "mode_vision"}
    event.send.assert_awaited_once_with("Фото не сохранилось. Пожалуйста, загрузите фото заново.")
    generator.assert_not_awaited()


async def test_more_does_not_interrupt_generation():
    context = MemoryContext(chat_id=123, user_id=456)
    await context.set_state(PosterStates.generating)
    await context.update_data(photo={"url": "https://photos.example.com/source.png"})
    before = await context.get_data()
    event = callback_event("more")
    await dispatch_callback(create_router(), event, context)
    event.ack.assert_awaited_once_with(notification="Плакат уже создаётся. Подождите.")
    assert await context.get_state() == PosterStates.generating
    assert await context.get_data() == before
    event.send.assert_not_awaited()


async def test_more_then_same_dream_generates_fresh_image(credit_db):
    await credit_db.add_credits(456, 2)
    context = await ready_context()
    await context.update_data(
        sphere="карьера", variants=["https://storage.example.com/old.png"],
        batch_id="old", sent_count=1,
    )
    generator = AsyncMock(return_value=["https://storage.example.com/new.png"])
    router = create_router(generator)
    more = callback_event("more")
    await dispatch_callback(router, more, context)
    generator.assert_not_awaited()

    await call_handler(router, "collect_sphere", message_event("карьера"), context)

    generator.assert_awaited_once_with("https://photos.example.com/source.png", "карьера")
    assert (await credit_db.get_user(456))["balance"] == 1
    assert (await context.get_data())["last_image_url"] == "https://storage.example.com/new.png"
    assert await context.get_state() is None


async def test_share_after_more_keeps_previous_public_link():
    context = await ready_context()
    url = "https://storage.example.com/photo.png?a=1&b=2"
    await context.update_data(batch_id="batch", variants=[url], sent_count=1)
    router = create_router()
    await dispatch_callback(router, callback_event("more"), context)
    await context.update_data(variants=[])
    share = callback_event("share")
    await dispatch_callback(router, share, context)
    share.ack.assert_awaited_once()
    text = share.send.await_args.args[0]
    assert 'href="https://storage.example.com/photo.png?a=1&amp;b=2"' in text
    assert share.send.await_args.kwargs["parse_mode"] == handlers.ParseMode.HTML
    assert await context.get_state() == PosterStates.waiting_for_prompt


@pytest.mark.parametrize("payload", ["more", "share"])
async def test_result_buttons_ack_deleted_message(payload):
    event = callback_event(payload, deleted=True)
    await dispatch_callback(create_router(), event, await ready_context())
    event.ack.assert_awaited_once()
    event.send.assert_not_awaited()


@pytest.mark.parametrize("cached_url", [None, "https://storage.example.com/another-photo.png"])
async def test_share_recovers_clicked_image_url_from_message_after_session_reset(cached_url):
    url = "https://storage.example.com/clicked-photo.png"
    event = callback_event("share")
    keyboard = handlers.result_keyboard(url)
    event.message.body = SimpleNamespace(attachments=[keyboard])
    context = MemoryContext(chat_id=123, user_id=456)
    if cached_url:
        await context.update_data(last_image_url=cached_url)

    await dispatch_callback(create_router(), event, context)

    event.ack.assert_awaited_once()
    assert f'href="{url}"' in event.send.await_args.args[0]
    assert "another-photo" not in event.send.await_args.args[0]


async def test_share_without_result_explains_missing_link():
    event = callback_event("share")
    await dispatch_callback(create_router(), event, await ready_context())
    event.ack.assert_awaited_once_with(notification="Ссылка недоступна.")
    event.send.assert_awaited_once_with("Ссылка на картинку не сохранилась. Создайте изображение заново.")


def test_result_buttons_use_plain_more_and_share_payloads():
    markup = handlers.result_keyboard("https://storage.example.com/photo.png").model_dump()
    buttons = markup["payload"]["buttons"][0]
    assert buttons[0]["url"] == "https://storage.example.com/photo.png"
    assert [button["payload"] for button in buttons[1:]] == ["more", "share"]


def message_event(text="карьера", *, chat_id=123, chat_type="dialog"):
    return SimpleNamespace(
        get_ids=lambda: (chat_id, 456),
        message=SimpleNamespace(
            sender=SimpleNamespace(is_bot=False, user_id=456),
            recipient=SimpleNamespace(chat_id=chat_id, chat_type=chat_type),
            body=SimpleNamespace(text=text),
            answer=AsyncMock(),
        ),
        bot=SimpleNamespace(send_message=AsyncMock(return_value=object()),
                            upload_media=AsyncMock(return_value=SimpleNamespace(payload=SimpleNamespace(token="example-token")))),
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
    await context.set_state(PosterStates.waiting_for_prompt)
    return context


async def test_photo_requests_free_text_dream_without_sphere_buttons():
    context = MemoryContext(chat_id=123, user_id=456)
    event = message_event()
    photo = PhotoAttachmentPayload(photo_id=1, token="photo-token", url="https://photos.example.com/source.png")
    event.message.body = SimpleNamespace(attachments=[SimpleNamespace(type="image", payload=photo)])
    await call_handler(create_router(), "collect_photo", event, context)
    event.message.answer.assert_awaited_once_with(
        "✅ Фото получено!\n\n"
        "Теперь опиши свою мечту или желание (до 200 символов). "
        "Например: «Машина тесла с бордовым салоном, я за рулем» "
        "или «Я на пляже на Мальдивах».\n\n/cancel — отмена"
    )
    assert await context.get_state() == PosterStates.waiting_for_prompt
    assert (await context.get_data())["photo"] == photo.model_dump()


@pytest.mark.parametrize("description", [
    "Машина тесла с бордовым салоном, я за рулем",
    "Я на пляже на Мальдивах",
    "/мечта о полёте в космос",
    "я" * 200,
])
async def test_any_valid_dream_is_passed_to_generator(credit_db, description):
    await credit_db.add_credits(456, 2)
    context = await ready_context()
    generator = AsyncMock(return_value=["https://storage.example.com/dream.png"])
    event = message_event("  " + description + "  ")
    await call_handler(create_router(generator), "collect_sphere", event, context)
    generator.assert_awaited_once_with("https://photos.example.com/source.png", description)
    assert (await context.get_data())["sphere"] == description
    assert (await credit_db.get_user(456))["balance"] == 1
    assert event.bot.send_message.await_count == 1


@pytest.mark.parametrize("description,reply", [
    ("я" * 201, "Слишком длинное описание. Пожалуйста, уложись в 200 символов"),
    ("", "Напиши свою мечту или желание текстом. /cancel — отмена"),
    (" \n\t ", "Напиши свою мечту или желание текстом. /cancel — отмена"),
    (None, "Напиши свою мечту или желание текстом. /cancel — отмена"),
])
async def test_invalid_dream_preserves_photo_and_does_not_charge(credit_db, description, reply):
    await credit_db.add_credits(456, 1)
    context = await ready_context()
    before = dict(await context.get_data())
    generator = AsyncMock()
    event = message_event(description)
    await call_handler(create_router(generator), "collect_sphere", event, context)
    event.message.answer.assert_awaited_once_with(reply)
    generator.assert_not_awaited()
    event.bot.send_message.assert_not_awaited()
    assert await context.get_state() == PosterStates.waiting_for_prompt
    assert await context.get_data() == before
    assert (await credit_db.get_user(456))["balance"] == 1


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
    assert "1 генерация (открытка) — 99 ₽" in text
    assert "5 генераций — 299 ₽" in text
    assert "15 генераций — 799 ₽" in text
    assert "недоступна" not in text
    event.message.answer.assert_awaited_once()
    assert_purchase_keyboard(event.message.answer.await_args.kwargs["attachments"][0])
    assert await credit_db.get_user(456) is None


@pytest.mark.parametrize("payments_enabled", [False, True])
async def test_no_credits_prevents_generation(credit_db, payments_enabled):
    generator = AsyncMock()
    event = message_event()
    context = await ready_context()
    payments = SimpleNamespace(create_payment_link=AsyncMock()) if payments_enabled else None
    await call_handler(create_router(generator, payments=payments), "collect_sphere", event, context)
    generator.assert_not_awaited()
    event.bot.send_message.assert_not_awaited()
    event.message.answer.assert_awaited_once()
    assert event.message.answer.await_args.args == (NO_CREDITS_TEXT,)
    assert_purchase_keyboard(event.message.answer.await_args.kwargs["attachments"][0])
    assert await context.get_state() == PosterStates.waiting_for_prompt


@pytest.mark.parametrize("initial_balance", [None, 0, 3])
async def test_admin_can_generate_repeatedly_without_balance_or_deduction(
    credit_db, monkeypatch, caplog, initial_balance,
):
    if initial_balance == 0:
        await credit_db.mark_free_trial_used(456)
    elif initial_balance is not None:
        await credit_db.add_credits(456, initial_balance)
    before = await credit_db.get_user(456)
    get_user = AsyncMock(side_effect=AssertionError("Admin balance must not be checked"))
    deduct = AsyncMock(return_value=False)
    monkeypatch.setattr(handlers, "get_user", get_user)
    monkeypatch.setattr(handlers, "deduct_credit", deduct)
    generator = AsyncMock(return_value=["https://storage.example.com/photo.png"])
    router = create_router(generator, admin_user_id=456)
    context = await ready_context()
    events = [message_event()]

    with caplog.at_level(logging.INFO, logger=handlers.__name__):
        await call_handler(router, "collect_sphere", events[0], context)
        for _ in range(2):
            data = await context.get_data()
            event = callback_event(f"more:{data['batch_id']}")
            await dispatch_callback(router, event, context)
            event.bot.send_message.assert_not_awaited()
            next_dream = message_event()
            events.append(next_dream)
            await call_handler(router, "collect_sphere", next_dream, context)

    assert generator.await_count == 3
    assert all(event.bot.send_message.await_count == 1 for event in events)
    get_user.assert_not_awaited()
    deduct.assert_not_awaited()
    after = await credit_db.get_user(456)
    assert after["balance"] == (before["balance"] if before else 0)
    assert after["total_generations"] == 3
    assert after["total_purchases"] == 0
    assert await context.get_state() is None
    assert (await context.get_data())["package_offer_pending"] is False
    assert sum(
        record.getMessage() == "Пропущено списание кредитов для администратора 456"
        for record in caplog.records
    ) == 3
    for event in events:
        assert not any(
            call.args and call.args[0] == NO_CREDITS_TEXT
            for call in event.message.answer.await_args_list
        )


@pytest.mark.parametrize("admin_user_id", [None, 789])
@pytest.mark.parametrize("initial_balance", [0, 1])
async def test_normal_users_still_require_and_spend_credits(credit_db, admin_user_id, initial_balance):
    await credit_db.grant_free_trial(456)
    if initial_balance == 0:
        await credit_db.deduct_credit(456)
    generator = AsyncMock(return_value=["https://storage.example.com/photo.png"])
    event = message_event()
    await call_handler(
        create_router(generator, admin_user_id=admin_user_id),
        "collect_sphere", event, await ready_context(),
    )
    assert generator.await_count == initial_balance
    assert (await credit_db.get_user(456))["balance"] == 0
    if initial_balance == 0:
        event.bot.send_message.assert_not_awaited()
        event.message.answer.assert_awaited_once()
        assert event.message.answer.await_args.args == (NO_CREDITS_TEXT,)
        assert_purchase_keyboard(event.message.answer.await_args.kwargs["attachments"][0])


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
        call.args and call.args[0] == NO_CREDITS_TEXT
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
    assert await context.get_state() == PosterStates.waiting_for_prompt


async def test_image_upload_precedes_send_and_uses_token(credit_db, max_image_upload):
    await credit_db.add_credits(456, 1)
    url = "https://storage.example.com/photo.png"
    generator = AsyncMock(return_value=[url])
    event = message_event()
    context = await ready_context()
    timeline = Mock()
    timeline.attach_mock(max_image_upload, "upload")
    timeline.attach_mock(event.bot.send_message, "send")

    await call_handler(create_router(generator), "collect_sphere", event, context)

    max_image_upload.assert_awaited_once_with(event.bot, url)
    assert [call[0] for call in timeline.mock_calls] == ["upload", "send"]
    image, keyboard = event.bot.send_message.await_args.kwargs["attachments"]
    assert event.bot.send_message.await_args.kwargs["user_id"] == 456
    assert "chat_id" not in event.bot.send_message.await_args.kwargs
    assert image.model_dump()["payload"] == {"token": "uploaded-poster-token"}
    assert keyboard.model_dump()["type"] == "inline_keyboard"


@pytest.mark.parametrize("event_kind", ["message", "callback"])
async def test_private_replies_use_user_id_even_with_chat_id(event_replies, event_kind):
    event = message_event() if event_kind == "message" else callback_event("help")

    await event_replies(event, "Ответ")

    event.bot.send_message.assert_awaited_once_with(user_id=456, text="Ответ")


async def test_group_replies_keep_chat_id(event_replies):
    event = message_event(chat_type="chat")
    await event_replies(event, "Ответ группе")
    event.bot.send_message.assert_awaited_once_with(chat_id=123, text="Ответ группе")


async def test_poster_in_group_keeps_chat_id(credit_db):
    await credit_db.add_credits(456, 1)
    generator = AsyncMock(return_value=["https://storage.example.com/photo.png"])
    event = message_event(chat_type="chat")
    await call_handler(create_router(generator), "collect_sphere", event, await ready_context())
    kwargs = event.bot.send_message.await_args.kwargs
    assert kwargs["chat_id"] == 123
    assert "user_id" not in kwargs


@pytest.mark.parametrize("stage", ["upload", "send"])
async def test_max_error_logs_body_and_retry_uses_paid_image(
    credit_db, max_image_upload, caplog, stage,
):
    await credit_db.add_credits(456, 1)
    generator = AsyncMock(return_value=["https://storage.example.com/photo.png"])
    event = message_event()
    router = create_router(generator)
    context = await ready_context()
    failing_call = max_image_upload if stage == "upload" else event.bot.send_message
    failing_call.side_effect = MaxApiError(code=400, raw={"message": "MAX rejected media"})

    with caplog.at_level(logging.ERROR, logger=handlers.__name__):
        await call_handler(router, "collect_sphere", event, context)

    assert "Детали ошибки MAX:" in caplog.text
    assert "Ответ сервера MAX (код 400):" in caplog.text
    assert "MAX rejected media" in caplog.text
    if stage == "upload":
        event.bot.send_message.assert_not_awaited()
    assert (await context.get_data())["sent_count"] == 0
    failing_call.reset_mock(side_effect=True)

    await call_handler(router, "collect_sphere", event, context)

    generator.assert_awaited_once()
    assert (await credit_db.get_user(456))["balance"] == 0
    assert await context.get_state() is None


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
        bool(call.args and call.args[0] == NO_CREDITS_TEXT)
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


@pytest.mark.parametrize("payments_enabled", [False, True])
async def test_last_free_credit_offers_packages_once_and_not_on_zero_balance(credit_db, payments_enabled):
    await credit_db.grant_free_trial(456)
    generator = AsyncMock(return_value=["https://storage.example.com/photo.png"])
    payments = SimpleNamespace(create_payment_link=AsyncMock()) if payments_enabled else None
    router = create_router(generator, payments=payments)
    event = message_event()
    context = await ready_context()
    await call_handler(router, "collect_sphere", event, context)
    assert (await credit_db.get_user(456))["balance"] == 0
    offer = next(
        call for call in event.message.answer.await_args_list
        if call.args and call.args[0] == NO_CREDITS_TEXT
    )
    assert_purchase_keyboard(offer.kwargs["attachments"][0])
    keyboard = offer.kwargs["attachments"][0].model_dump()
    buttons = [button for row in keyboard["payload"]["buttons"] for button in row]
    assert [button["text"] for button in buttons] == [
        "Купить 1 генерацию — 99 ₽", "Купить 5 генераций — 299 ₽", "Купить 15 генераций — 799 ₽",
    ]
    assert [button["payload"] for button in buttons] == ["buy_1", "buy_5", "buy_15"]
    # Повторное сообщение о той же сфере использует готовый оплаченный результат.
    await call_handler(router, "collect_sphere", event, context)
    assert sum(
        bool(call.args and call.args[0] == NO_CREDITS_TEXT)
        for call in event.message.answer.await_args_list
    ) == 1
    more = callback_event("more:" + (await context.get_data())["batch_id"])
    await dispatch_callback(router, more, context)
    more.message.answer.assert_awaited_once()
    assert await context.get_state() == PosterStates.waiting_for_prompt
    next_dream = message_event()
    await call_handler(router, "collect_sphere", next_dream, context)
    assert next_dream.message.answer.await_args.args == (NO_CREDITS_TEXT,)
    assert_purchase_keyboard(next_dream.message.answer.await_args.kwargs["attachments"][0])
    generator.assert_awaited_once()
    assert event.bot.send_message.await_count == 1
    more.bot.send_message.assert_not_awaited()


@pytest.mark.parametrize("credits", [1, 5, 15])
async def test_payment_help_opens_packages_and_creates_selected_link(credit_db, credits):
    payments = SimpleNamespace(create_payment_link=AsyncMock(return_value="https://test-shop.payform.ru/pay"))
    generator = AsyncMock()
    router = create_router(generator, payments=payments)
    context = await ready_context()
    original = dict(await context.get_data())
    event = callback_event("help_payment")
    await dispatch_callback(router, event, context)
    text = event.send.await_args.args[0]
    assert "Выберите пакет" in text
    assert "недоступна" not in text
    keyboard = event.send.await_args.kwargs["attachments"][0]
    assert_purchase_keyboard(keyboard)
    buttons = [button for row in keyboard.model_dump()["payload"]["buttons"] for button in row]
    selected = next(button for button in buttons if button["payload"] == f"buy_{credits}")
    purchase = callback_event(selected["payload"])
    await dispatch_callback(router, purchase, context)
    payments.create_payment_link.assert_awaited_once_with(456, credits)
    assert "https://test-shop.payform.ru/pay" in purchase.send.await_args.args[0]
    assert await context.get_data() == original
    assert await context.get_state() == PosterStates.waiting_for_prompt
    assert await credit_db.get_user(456) is None
    generator.assert_not_awaited()


@pytest.mark.parametrize("payload", ["buy_1", "buy_5", "buy_15"])
async def test_package_buttons_report_missing_configuration_without_duplicate_menu(credit_db, payload):
    await credit_db.add_credits(456, 2)
    event = callback_event(payload)
    await dispatch_callback(create_router(), event, await ready_context())
    event.ack.assert_awaited_once_with(notification="Создаю ссылку…")
    event.send.assert_awaited_once_with("Не удалось создать ссылку на оплату. Обратитесь в поддержку.")
    assert (await credit_db.get_user(456))["balance"] == 2


@pytest.mark.parametrize("payload,credits,price", [("buy_1", 1, "99.00"), ("buy_5", 5, "299.00"), ("buy_15", 15, "799.00")])
async def test_purchase_button_creates_signed_link_for_callback_user(credit_db, payload, credits, price):
    from pathlib import Path
    from urllib.parse import parse_qsl, urlsplit
    import re
    from aioresponses import aioresponses

    from config import Config
    from prodamus import ProdamusPayments, parse_form_fields, sign_link_payload

    payments = ProdamusPayments(Config(
        token="test-token", photo_path=Path("unused.png"),
        prodamus_domain="test-shop.payform.ru", prodamus_secret_key="test-secret",
        prodamus_return_url="https://max.ru/test_bot",
        prodamus_webhook_url="https://bot.example.com/prodamus/webhook",
    ))
    event = callback_event(payload)
    with aioresponses() as api:
        api.get(re.compile(r"https://test-shop\.payform\.ru/\?.*"), body="https://test-shop.payform.ru/payment/abc")
        await dispatch_callback(create_router(payments=payments), event, await ready_context())
        requests = [call for calls in api.requests.values() for call in calls]
        assert len(requests) == 1
        request_url = next(iter(api.requests))[1]
        data = parse_form_fields(parse_qsl(request_url.query_string))
    event.ack.assert_awaited_once_with(notification="Создаю ссылку…")
    event.send.assert_awaited_once()
    sent = event.send.await_args
    keyboard = sent.kwargs["attachments"][0].model_dump()
    link = keyboard["payload"]["buttons"][0][0]["url"]
    assert link in sent.args[0]
    assert link == "https://test-shop.payform.ru/payment/abc"
    assert sent.args[0].startswith(f"Для оплаты пакета «{handlers.PACKAGES[credits][1]}")
    assert data["sign"] == sign_link_payload({key: value for key, value in data.items() if key != "sign"}, "test-secret")
    assert data["_param_user_id"] == "456"
    assert data["_param_package_id"] == f"package_{credits}"
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


async def test_start_shows_six_finished_examples_and_modes(example_photos):
    event = message_event("/start")
    context = await ready_context()
    await call_handler(create_router(), "start", event, context)
    assert await context.get_state() == PosterStates.waiting_for_mode
    assert await context.get_data() == {}
    greeting = event.message.answer.await_args_list[0]
    assert greeting.kwargs["text"].startswith(handlers.WELCOME_TEXT)
    assert event.bot.upload_media.await_count == 6
    assert [Path(call.args[0].path).name for call in event.bot.upload_media.await_args_list] == [
        "i (1).webp", "i (18).webp", "i (17).webp",
        "i (19).webp", "i (16).webp", "i (14).webp",
    ]
    assert len(greeting.kwargs["attachments"]) == 7
    assert "Примеры работ" in greeting.kwargs["text"]
    assert "исходное фото" not in greeting.kwargs["text"]
    buttons = greeting.kwargs["attachments"][-1].model_dump()["payload"]["buttons"]
    assert [(row[0]["text"], row[0]["payload"]) for row in buttons[:6]] == list(handlers.MODE_BUTTONS)


async def test_start_still_offers_menu_if_examples_fail(example_photos):
    event = message_event("/start")
    event.bot.upload_media.side_effect = RuntimeError("upload failed")
    await call_handler(create_router(), "start", event, await ready_context())
    assert len(event.message.answer.await_args_list[0].kwargs["attachments"]) == 1


@pytest.mark.parametrize("mode", [payload for _, payload in handlers.MODE_BUTTONS])
async def test_mode_selection_clears_previous_result_and_requests_photo(mode):
    context = await ready_context()
    await context.update_data(variants=["https://old.example/result.png"], sphere="old")
    event = callback_event(mode)
    await dispatch_callback(create_router(), event, context)
    event.ack.assert_awaited_once()
    assert await context.get_data() == {"mode": mode}
    assert await context.get_state() == PosterStates.waiting_for_photo
    assert "фото" in event.send.await_args.args[0]


def photo_event():
    event = message_event()
    event.message.body = SimpleNamespace(attachments=[SimpleNamespace(
        type="image", payload=PhotoAttachmentPayload(photo_id=1, token="source-token",
        url="https://photos.example.com/source.png"))])
    return event


@pytest.mark.parametrize("mode", list(handlers.POSTCARD_PROMPTS))
@pytest.mark.parametrize("balance,admin", [(2, False), (0, False), (0, True)])
async def test_postcard_photo_generates_without_text_and_uses_shared_balance(credit_db, mode, balance, admin):
    await credit_db.add_credits(456, balance or 1)
    if not balance:
        await credit_db.deduct_credit(456)
    context = await ready_context()
    await context.update_data(mode=mode)
    vision = AsyncMock()
    postcard = AsyncMock(return_value=["https://cdn.example/card.png"])
    router = create_router(vision, postcard_generator=postcard, admin_user_id=456 if admin else None)
    event = photo_event()
    await call_handler(router, "collect_photo", event, context)
    vision.assert_not_awaited()
    if balance or admin:
        postcard.assert_awaited_once_with("https://photos.example.com/source.png", mode)
        assert (await credit_db.get_user(456))["balance"] == (balance if admin else balance - 1)
        assert await context.get_state() is None
    else:
        postcard.assert_not_awaited()
        assert event.message.answer.await_args.args == (NO_CREDITS_TEXT,)
        assert await context.get_state() == PosterStates.waiting_for_mode
    assert all("опиши" not in str(c).lower() for c in event.message.answer.await_args_list)


async def test_postcard_send_retry_reuses_paid_result_and_more_creates_new(credit_db):
    await credit_db.add_credits(456, 3)
    context = await ready_context()
    await context.update_data(mode="mode_ny")
    postcard = AsyncMock(return_value=["https://cdn.example/card.png"])
    router = create_router(postcard_generator=postcard)
    event = photo_event()
    event.bot.send_message.side_effect = RuntimeError("MAX temporarily unavailable")
    await call_handler(router, "collect_photo", event, context)
    assert (await credit_db.get_user(456))["balance"] == 2
    assert await context.get_state() == PosterStates.waiting_for_mode
    await dispatch_callback(router, callback_event("postcard_retry"), context)
    assert postcard.await_count == 1
    assert (await credit_db.get_user(456))["balance"] == 2
    await dispatch_callback(router, callback_event("more"), context)
    assert postcard.await_count == 2
    assert (await credit_db.get_user(456))["balance"] == 1


async def test_postcard_generation_failure_does_not_charge_and_can_retry(credit_db):
    await credit_db.add_credits(456, 2)
    context = await ready_context()
    await context.update_data(mode="mode_bday_young")
    postcard = AsyncMock(side_effect=[RuntimeError("API error"), ["https://cdn.example/card.png"]])
    router = create_router(postcard_generator=postcard)
    await call_handler(router, "collect_photo", photo_event(), context)
    assert (await credit_db.get_user(456))["balance"] == 2
    await dispatch_callback(router, callback_event("postcard_retry"), context)
    assert (await credit_db.get_user(456))["balance"] == 1


async def test_vision_mode_still_requires_dream_after_photo(credit_db):
    await credit_db.add_credits(456, 2)
    context = await ready_context()
    await context.update_data(mode="mode_vision")
    generator = AsyncMock(return_value=["https://cdn.example/vision.png"])
    postcard = AsyncMock()
    router = create_router(generator, postcard_generator=postcard)
    await call_handler(router, "collect_photo", photo_event(), context)
    generator.assert_not_awaited()
    assert await context.get_state() == PosterStates.waiting_for_prompt
    await call_handler(router, "collect_sphere", message_event("Я на Мальдивах"), context)
    generator.assert_awaited_once_with("https://photos.example.com/source.png", "Я на Мальдивах")
    postcard.assert_not_awaited()
    assert (await credit_db.get_user(456))["balance"] == 1


async def test_family_mode_requests_one_group_photo():
    event = callback_event("mode_ny_family")
    await dispatch_callback(create_router(), event, await ready_context())
    assert "общее фото семьи" in event.send.await_args.args[0]


async def test_old_birthday_button_opens_young_template():
    context = await ready_context()
    await dispatch_callback(create_router(), callback_event("mode_bday"), context)
    assert (await context.get_data())["mode"] == "mode_bday_young"


@pytest.mark.parametrize("state", [None, PosterStates.waiting_for_photo, PosterStates.waiting_for_prompt, PosterStates.generating])
async def test_admin_stats_does_not_change_fsm(credit_db, state):
    await credit_db.grant_free_trial(123)
    await credit_db.increment_generations(123)
    await credit_db.create_payment_order("stats-order", 123, 5, 29900, "test")
    await credit_db.add_credits(123, 5, payment_order_id="stats-order", provider_payment_id="stats-payment")
    context = await ready_context()
    await context.set_state(state)
    before = dict(await context.get_data())
    event = message_event("/stats")
    router = create_router(admin_user_id=456)
    await call_handler(router, "stats", event)
    event.message.answer.assert_awaited_once()
    assert event.message.answer.await_args.args[0].startswith(
        "📊 Статистика бота:\n\n👥 Всего пользователей: 1\n💳 Покупателей: 1\n"
        "🖼 Всего сгенерировано картинок: 1\n💰 Всего покупок: 1"
    )
    assert await context.get_state() == state
    assert await context.get_data() == before
    names = [h.func_event.__name__ for h in router.event_handlers]
    assert names.index("stats") < names.index("collect_sphere")
    assert names.index("stats") < names.index("collect_photo")


@pytest.mark.parametrize("configured_admin,actor,is_bot", [(None, 456, False), (789, 456, False), (456, None, False), (456, 456, True)])
async def test_stats_ignores_unauthorized_or_bot_messages(monkeypatch, configured_admin, actor, is_bot):
    query = AsyncMock()
    monkeypatch.setattr(handlers, "get_global_stats", query)
    event = message_event("/stats")
    event.get_ids = lambda: (456, actor)  # A matching chat_id must not grant admin access.
    event.message.sender.is_bot = is_bot
    await call_handler(create_router(admin_user_id=configured_admin), "stats", event)
    query.assert_not_awaited()
    event.message.answer.assert_not_awaited()


async def test_generation_stats_count_new_results_not_delivery_retries(credit_db):
    await credit_db.add_credits(456, 2)
    generator = AsyncMock(return_value=["https://cdn.example/result.png"])
    router = create_router(generator)
    context = await ready_context()
    event = message_event()
    event.bot.send_message.side_effect = RuntimeError("delivery failed")
    await call_handler(router, "collect_sphere", event, context)
    assert (await credit_db.get_user(456))["total_generations"] == 1
    event.bot.send_message.side_effect = None
    await call_handler(router, "collect_sphere", event, context)
    assert generator.await_count == 1
    assert (await credit_db.get_user(456))["total_generations"] == 1
    await dispatch_callback(router, callback_event("more"), context)
    await call_handler(router, "collect_sphere", message_event("Новая мечта"), context)
    assert (await credit_db.get_user(456))["total_generations"] == 2


async def test_failed_generation_does_not_change_stats(credit_db):
    await credit_db.add_credits(456, 1)
    generator = AsyncMock(side_effect=RuntimeError("generation failed"))
    await call_handler(create_router(generator), "collect_sphere", message_event(), await ready_context())
    assert (await credit_db.get_global_stats())["total_generated"] == 0


def bot_started_event(payload=None, *, user_id=456, timestamp=1792526400000, is_bot=False):
    from maxapi.types import BotStarted, User
    return BotStarted(
        timestamp=timestamp, chat_id=123, payload=payload,
        user=User(user_id=user_id, first_name="Test", is_bot=is_bot),
        bot=SimpleNamespace(
            send_message=AsyncMock(return_value=object()),
            upload_media=AsyncMock(return_value=AttachmentUpload(type="image", payload={"token": "example-token"})),
        ),
    )


@pytest.mark.parametrize("payload,source", [("vk_ads", "vk_ads"), ("yandex_ads", "yandex_ads"), (None, "unmarked"), ("", "unmarked"), ("unknown", "unmarked")])
async def test_real_bot_started_event_tracks_source_and_shows_start(credit_db, example_photos, monkeypatch, event_replies, payload, source):
    monkeypatch.setattr(handlers, "_reply", event_replies)
    context = await ready_context()
    event = bot_started_event(payload)
    router = create_router()
    handler = next(h for h in router.event_handlers if h.update_type == UpdateType.BOT_STARTED)
    await handler.func_event(event, context)
    user = await credit_db.get_user(456)
    assert user["first_start_source"] == source
    assert user["first_started_at"] == "2026-10-20T20:00:00.000+00:00"
    assert user["balance"] == 1 and user["free_trial_used"] == 1
    assert await context.get_state() == PosterStates.waiting_for_mode
    greeting = event.bot.send_message.await_args_list[0]
    assert greeting.kwargs["user_id"] == 456 and "chat_id" not in greeting.kwargs
    assert greeting.kwargs["text"].startswith(handlers.WELCOME_TEXT)
    assert len(greeting.kwargs["attachments"]) == 7


async def test_repeated_ad_starts_and_start_command_preserve_first_touch_and_trial(credit_db, example_photos, monkeypatch, event_replies):
    monkeypatch.setattr(handlers, "_reply", event_replies)
    router = create_router()
    context = await ready_context()
    await call_handler(router, "bot_started", bot_started_event("vk_ads"), context)
    original = dict(await credit_db.get_user(456))
    await call_handler(router, "bot_started", bot_started_event("yandex_ads", timestamp=1792612800000), context)
    await call_handler(router, "start", message_event("/start"), context)
    assert dict(await credit_db.get_user(456)) == original


async def test_plain_start_then_ad_remains_unmarked(credit_db, example_photos, monkeypatch, event_replies):
    monkeypatch.setattr(handlers, "_reply", event_replies)
    router = create_router()
    context = await ready_context()
    await call_handler(router, "start", message_event("/start"), context)
    first = (await credit_db.get_user(456))["first_started_at"]
    await call_handler(router, "bot_started", bot_started_event("vk_ads"), context)
    user = await credit_db.get_user(456)
    assert user["first_start_source"] == "unmarked" and user["first_started_at"] == first
    assert user["balance"] == 1


async def test_bot_started_ignores_bot_accounts(credit_db):
    event = bot_started_event("vk_ads", is_bot=True)
    await call_handler(create_router(), "bot_started", event, await ready_context())
    assert await credit_db.get_user(456) is None
    event.bot.send_message.assert_not_awaited()


async def test_source_stats_include_generation_and_actual_paid_amount(credit_db):
    await credit_db.record_first_start(456, "vk_ads")
    await credit_db.add_credits(456, 2)
    router = create_router(AsyncMock(return_value=["https://cdn.example/result.png"]), admin_user_id=789)
    await call_handler(router, "collect_sphere", message_event(), await ready_context())
    await credit_db.create_payment_order("ad-purchase", 456, 1, 9900, "1 генерация (Открытка)")
    await credit_db.add_credits(456, 1, payment_order_id="ad-purchase", provider_payment_id="ad-payment")
    event = message_event("/stats")
    event.get_ids = lambda: (123, 789)
    await call_handler(router, "stats", event)
    text = event.message.answer.await_args.args[0]
    assert "Реклама VK\n👥 Новые пользователи: 1\n🖼 Пользователи с генерацией: 1\n💳 Покупатели: 1\n💰 Сумма оплат: 99,00 ₽" in text
    assert "Реклама Яндекса" in text and "Без метки" in text

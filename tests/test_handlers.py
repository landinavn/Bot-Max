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
def text_correction(monkeypatch):
    fixer = AsyncMock(side_effect=lambda text: text)
    monkeypatch.setattr(handlers, "fix_text_with_ai", fixer)
    return fixer


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
        ("Купить 3 генерации — 199 ₽", "buy_3", "callback"),
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
        bot=SimpleNamespace(
            send_message=AsyncMock(return_value=object()),
            upload_media=AsyncMock(return_value=AttachmentUpload(type="image", payload={"token": "example-token"})),
        ),
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
        "Отправьте одно фото для плаката. /cancel — отмена." + "\n\n" + handlers.PHOTO_GUIDANCE
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
    assert "Как это работает:" in text
    assert "описание мечты" in text
    assert "Они НЕ сгорают!" in text
    assert "OpenAI" in text and "облачном хранилище" in text
    assert "никуда не передаются" not in text
    assert "Связаться с поддержкой" in text
    assert "@ваш_ник_поддержки" not in text
    keyboard = event.send.await_args.kwargs["attachments"][0].model_dump()
    buttons = [button for row in keyboard["payload"]["buttons"] for button in row]
    assert [(button["text"], button["payload"]) for button in buttons] == [
        ("Как оплатить?", "help_payment"),
        ("Не работает генерация", "help_generation"),
        ("Связаться с поддержкой", "help_support"),
        ("🔄 Главное меню", "open_main_menu"),
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
    for filename in {"before.jpg", "after.jpg", *handlers.START_EXAMPLE_FILES, *handlers.BROADCAST_EXAMPLE_FILES,
                     *(f for files, _, _ in handlers.CATEGORY_MENUS.values() for f in files)}:
        if not Path(filename).suffix:
            filename += ".webp"
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
def test_share_callbacks_are_not_registered(payload):
    event = callback_event(payload)
    assert not [h for h in create_router().event_handlers
                if h.update_type == UpdateType.MESSAGE_CALLBACK
                and all(f.resolve(event) for f in h.filters)]


@pytest.mark.parametrize("payload", ["more", "more:batch"])
async def test_more_clears_photo_text_and_requests_new_photo(credit_db, payload):
    await credit_db.add_credits(456, 1)
    context = MemoryContext(chat_id=123, user_id=456)
    await context.update_data(
        batch_id="batch", sent_count=1,
        photo={"url": "https://photos.example.com/source.png"}, sphere="карьера",
    )
    event = callback_event(payload)
    generator = AsyncMock(return_value=["https://storage.example.com/new.png"])
    await dispatch_callback(create_router(generator), event, context)
    event.ack.assert_awaited_once_with(notification="Жду новое фото.")
    generator.assert_not_awaited()
    event.bot.send_message.assert_not_awaited()
    assert await context.get_state() == PosterStates.waiting_for_photo
    assert await context.get_data() == {"mode": "mode_vision"}
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
    event.send.assert_awaited_once_with(handlers.photo_request("Отправьте новое фото. /cancel — отмена."))
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

    await call_handler(router, "collect_photo", photo_event(), context)
    await call_handler(router, "collect_sphere", message_event("карьера"), context)

    generator.assert_awaited_once_with("https://photos.example.com/source.png", "карьера")
    assert (await credit_db.get_user(456))["balance"] == 1
    assert (await context.get_data())["last_image_url"] == "https://storage.example.com/new.png"
    assert await context.get_state() is None


@pytest.mark.parametrize("payload", ["more"])
async def test_result_buttons_ack_deleted_message(payload):
    event = callback_event(payload, deleted=True)
    await dispatch_callback(create_router(), event, await ready_context())
    event.ack.assert_awaited_once()
    event.send.assert_not_awaited()


def test_result_buttons_use_plain_more_and_share_payloads():
    markup = handlers.result_keyboard("https://storage.example.com/photo.png").model_dump()
    buttons = markup["payload"]["buttons"][0]
    assert buttons[0]["url"] == "https://storage.example.com/photo.png"
    assert [button["payload"] for button in buttons[1:]] == ["more"]


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
    assert any(reply.kwargs.get("text") == handlers.WELCOME_TEXT for reply in repeated.message.answer.await_args_list)
    assert all("начислен" not in str(reply) for reply in repeated.message.answer.await_args_list)


async def test_buy_describes_packages_without_adding_credits(credit_db):
    event = message_event("/buy")
    await call_handler(create_router(), "buy", event)
    text = event.message.answer.await_args.args[0]
    assert "1 генерация — 99 ₽" in text
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
            await call_handler(router, "collect_photo", photo_event(), context)
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
        if call.args and call.args[0] == handlers.PROMO_TEXT
    )
    assert offer.kwargs["attachments"][0].model_dump()["payload"]["buttons"][0][0]["payload"] == "buy_3_promo"
    keyboard = offer.kwargs["attachments"][0].model_dump()
    buttons = [button for row in keyboard["payload"]["buttons"] for button in row]
    assert buttons[0]["text"] == "Купить 3 генерации за 149 ₽"
    assert [button["payload"] for button in buttons] == ["buy_3_promo", "buy_1", "buy_3", "buy_5", "buy_15"]
    # Повторное сообщение о той же сфере использует готовый оплаченный результат.
    await call_handler(router, "collect_sphere", event, context)
    assert sum(
        bool(call.args and call.args[0] == handlers.PROMO_TEXT)
        for call in event.message.answer.await_args_list
    ) == 1
    more = callback_event("more:" + (await context.get_data())["batch_id"])
    await dispatch_callback(router, more, context)
    more.message.answer.assert_awaited_once()
    assert await context.get_state() == PosterStates.waiting_for_photo
    await call_handler(router, "collect_photo", photo_event(), context)
    next_dream = message_event()
    await call_handler(router, "collect_sphere", next_dream, context)
    assert next_dream.message.answer.await_args.args == (NO_CREDITS_TEXT,)
    assert "buy_3_promo" in str(next_dream.message.answer.await_args.kwargs["attachments"][0])
    generator.assert_awaited_once()
    assert event.bot.send_message.await_count == 1
    more.bot.send_message.assert_not_awaited()


@pytest.mark.parametrize("credits", [1, 3, 5, 15])
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


@pytest.mark.parametrize("payload", ["buy_1", "buy_3", "buy_5", "buy_15"])
async def test_package_buttons_report_missing_configuration_without_duplicate_menu(credit_db, payload):
    await credit_db.add_credits(456, 2)
    event = callback_event(payload)
    await dispatch_callback(create_router(), event, await ready_context())
    event.ack.assert_awaited_once_with(notification="Создаю ссылку…")
    event.send.assert_awaited_once_with("Не удалось создать ссылку на оплату. Обратитесь в поддержку.")
    assert (await credit_db.get_user(456))["balance"] == 2


@pytest.mark.parametrize("payload,credits,price", [("buy_1", 1, "99.00"), ("buy_3", 3, "199.00"), ("buy_5", 5, "299.00"), ("buy_15", 15, "799.00")])
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


async def test_start_sends_album_before_welcome_and_menu(example_photos):
    event = message_event("/start")
    context = await ready_context()
    await call_handler(create_router(), "start", event, context)
    assert await context.get_state() == PosterStates.waiting_for_mode
    assert await context.get_data() == {}
    assert [Path(c.args[0].path).name for c in event.bot.upload_media.await_args_list] == [
        "i (1).webp", "i (29).webp", "i (28).webp", "i (27).webp", "i (26).webp", "i (21).webp",
    ]
    replies = event.message.answer.await_args_list
    assert len(replies) == 3
    assert replies[0].kwargs["text"] == "Примеры работ:"
    assert len(replies[0].kwargs["attachments"]) == 6
    assert all(a.payload.token == "example-token" for a in replies[0].kwargs["attachments"])
    assert replies[1].kwargs["text"] == handlers.WELCOME_TEXT
    buttons = replies[1].kwargs["attachments"][0].model_dump()["payload"]["buttons"]
    assert [(row[0]["text"], row[0]["payload"]) for row in buttons[:-1]] == list(handlers.CATEGORY_BUTTONS)
    assert [(button["text"], button["payload"]) for button in buttons[-1]] == [
        ("🖼 Примеры", "show_examples"), ("🆘 Помощь", "show_help"),
    ]
    assert "бесплатный кредит" in replies[2].args[0]


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


@pytest.mark.parametrize("mode,event_name", [
    ("mode_ny", "gen_ny"), ("mode_ny_family", "gen_ny_family"),
    ("mode_bday_young", "gen_bday_young"), ("mode_bday_old", "gen_bday_old"),
    ("mode_8march", "gen_8march"), ("mode_goodmorning", "gen_goodmorning"),
    ("mode_modern_card", "gen_scrapbook"),
    ("mode_halloween", "gen_halloween"),
    ("mode_bday_old_elegant", "gen_bday_old_elegant"),
    ("mode_ny_cozy", "gen_ny_cozy"),
    ("mode_ny_fairytale", "gen_ny_fairytale"),
    ("mode_ny_elegant", "gen_ny_elegant"),
])
@pytest.mark.parametrize("balance,admin", [(2, False), (0, False), (0, True)])
async def test_postcard_requires_custom_text_and_uses_shared_balance(credit_db, mode, event_name, balance, admin):
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
    postcard.assert_not_awaited()
    assert await context.get_state() == PosterStates.waiting_for_card_text
    assert event.message.answer.await_args.args == (handlers.CARD_TEXT_PROMPT,)
    event = message_event("Аня, будь счастлива!")
    await call_handler(router, "collect_card_text", event, context)
    vision.assert_not_awaited()
    assert await credit_db.get_all_counters() == (
        {event_name: 1} if balance or admin else {}
    )
    if balance or admin:
        postcard.assert_awaited_once_with("https://photos.example.com/source.png", mode, "Аня, будь счастлива!")
        assert (await credit_db.get_user(456))["balance"] == (balance if admin else balance - 1)
        assert await context.get_state() is None
    else:
        postcard.assert_not_awaited()
        assert event.message.answer.await_args.args == (NO_CREDITS_TEXT,)
        assert await context.get_state() == PosterStates.waiting_for_card_text
    assert all("опиши" not in str(c).lower() for c in event.message.answer.await_args_list)


async def test_postcard_send_retry_reuses_paid_result_and_more_creates_new(credit_db):
    await credit_db.add_credits(456, 3)
    context = await ready_context()
    await context.update_data(mode="mode_ny")
    postcard = AsyncMock(return_value=["https://cdn.example/card.png"])
    router = create_router(postcard_generator=postcard)
    await call_handler(router, "collect_photo", photo_event(), context)
    event = message_event("Аня, с праздником!")
    event.bot.send_message.side_effect = RuntimeError("MAX temporarily unavailable")
    await call_handler(router, "collect_card_text", event, context)
    assert (await credit_db.get_user(456))["balance"] == 2
    assert await context.get_state() == PosterStates.waiting_for_card_text
    await dispatch_callback(router, callback_event("postcard_retry"), context)
    assert postcard.await_count == 1
    assert (await credit_db.get_user(456))["balance"] == 2
    await dispatch_callback(router, callback_event("more"), context)
    assert postcard.await_count == 1
    await call_handler(router, "collect_photo", photo_event(), context)
    await call_handler(router, "collect_card_text", message_event("Оля, поздравляю!"), context)
    assert postcard.await_count == 2
    assert (await credit_db.get_user(456))["balance"] == 1


async def test_postcard_generation_failure_does_not_charge_and_can_retry(credit_db):
    await credit_db.add_credits(456, 2)
    context = await ready_context()
    await context.update_data(mode="mode_bday_young")
    postcard = AsyncMock(side_effect=[RuntimeError("API error"), ["https://cdn.example/card.png"]])
    router = create_router(postcard_generator=postcard)
    await call_handler(router, "collect_photo", photo_event(), context)
    await call_handler(router, "collect_card_text", message_event("С днём рождения, Оля!"), context)
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
    text = event.message.answer.await_args.args[0]
    assert text.startswith(
        "📊 Статистика бота:\n\n👥 Всего запускали: 1\n🟢 Доступны для сообщений: 1\n"
        "🔴 Заблокировали/Удалили: 0\n🔄 Вернулись после 1-го дня: 0\n\n"
        "👥 Всего пользователей: 1\n💳 Покупателей: 1\n"
    )
    assert "- 5 генераций (299 ₽): 1" in text
    assert "- 1 генерация (99 ₽): 0" in text
    assert "- 15 генераций (799 ₽): 0" in text
    assert "🖼 Всего сгенерировано картинок: 1\n" in text
    assert "💰 Всего покупок: 1" in text
    for label in ("Карта желаний", "Новый год", "Семейный НГ", "ДР (Молодым)", "ДР (Старшим)", "Доброе утро", "Скрапбукинг"):
        assert f"- {label}: 0" in text
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
    assert await credit_db.get_all_counters() == {"gen_vision": 1}
    await dispatch_callback(router, callback_event("more"), context)
    await call_handler(router, "collect_photo", photo_event(), context)
    await call_handler(router, "collect_sphere", message_event("Новая мечта"), context)
    assert (await credit_db.get_user(456))["total_generations"] == 2
    assert await credit_db.get_all_counters() == {"gen_vision": 2}


async def test_failed_generation_does_not_change_stats(credit_db):
    await credit_db.add_credits(456, 1)
    generator = AsyncMock(side_effect=RuntimeError("generation failed"))
    await call_handler(create_router(generator), "collect_sphere", message_event(), await ready_context())
    assert (await credit_db.get_global_stats())["total_generated"] == 0
    assert await credit_db.get_all_counters() == {}


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
    album, greeting = event.bot.send_message.await_args_list[:2]
    assert len(album.kwargs["attachments"]) == 6
    assert greeting.kwargs["user_id"] == 456 and "chat_id" not in greeting.kwargs
    assert greeting.kwargs["text"].startswith(handlers.WELCOME_TEXT)
    assert len(greeting.kwargs["attachments"]) == 1
    assert event.bot.upload_media.await_count == 6
    assert len(event.bot.send_message.await_args_list) == 3


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


@pytest.mark.parametrize("mode", ["mode_vision", *handlers.POSTCARD_PROMPTS])
async def test_all_modes_send_persistent_bot_share_link_and_only_vision_invites_to_channel(credit_db, mode):
    from urllib.parse import urlsplit, parse_qs
    await credit_db.add_credits(456, 2)
    image_url = "https://cdn.example/generated.png"
    vision, card = AsyncMock(return_value=[image_url]), AsyncMock(return_value=[image_url])
    router = create_router(vision, postcard_generator=card, bot_link="https://max.ru/custom_bot")
    context = await ready_context()
    await context.update_data(mode=mode)
    if mode == "mode_vision":
        event = message_event("Моя мечта")
        await call_handler(router, "collect_sphere", event, context)
    else:
        await call_handler(router, "collect_photo", photo_event(), context)
        assert card.await_count == 0
        event = message_event("Оля, счастья и добра!")
        await call_handler(router, "collect_card_text", event, context)
        card.assert_awaited_once_with("https://photos.example.com/source.png", mode, "Оля, счастья и добра!")
    calls = event.message.answer.await_args_list
    share = next(c for c in calls if c.args and c.args[0] == "👇 Хочешь поделиться с друзьями?")
    button = share.kwargs["attachments"][0].model_dump()["payload"]["buttons"][0][0]
    assert button["type"] == "link" and button["text"] == "🔗 Поделиться"
    assert button["url"].startswith("https://max.ru/custom_bot?start=share_")
    payload = parse_qs(urlsplit(button["url"]).query)["start"][0]
    assert len(payload) <= 128
    assert await credit_db.get_shared_image(payload.removeprefix("share_")) == image_url
    invites = [c for c in calls if c.args and c.args[0] == handlers.CHANNEL_TEXT]
    assert len(invites) == (1 if mode == "mode_vision" else 0)
    if invites:
        button = invites[0].kwargs["attachments"][0].model_dump()["payload"]["buttons"][0][0]
        assert button["url"] == "https://max.ru/se14596751_biz"
        assert button["type"] == "link" and button["text"] == "Перейти в канал"
        assert "payload" not in button
    assert (await credit_db.get_user(456))["balance"] == 1


@pytest.mark.parametrize("entry", ["command", "bot_started"])
async def test_share_entry_shows_friends_image_and_create_own_menu(credit_db, monkeypatch, event_replies, max_image_upload, entry):
    monkeypatch.setattr(handlers, "_reply", event_replies)
    url = "https://cdn.example/friend.png"
    token = await credit_db.register_shared_image(url)
    await credit_db.init_db()  # Simulate a restart and an empty FSM session.
    router = create_router()
    context = MemoryContext(chat_id=123, user_id=789)
    if entry == "command":
        event = message_event("/start share_" + token)
        event.get_ids = lambda: (123, 789)
        name = "start"
    else:
        event = bot_started_event("share_" + token, user_id=789)
        name = "bot_started"
    await call_handler(router, name, event, context)
    max_image_upload.assert_awaited_once_with(event.bot, url)
    greeting = event.bot.send_message.await_args_list[0]
    assert greeting.kwargs["text"] == handlers.SHARED_WELCOME
    assert greeting.kwargs["user_id"] == 789
    buttons = greeting.kwargs["attachments"][-1].model_dump()["payload"]["buttons"]
    assert buttons[0][0]["text"] == "Создать свою"
    callback = callback_event(buttons[0][0]["payload"])
    callback.get_ids = lambda: (123, 789)
    await dispatch_callback(router, callback, context)
    callback.ack.assert_awaited_once()
    assert await context.get_state() == PosterStates.waiting_for_mode
    assert "photo" not in await context.get_data()
    user = await credit_db.get_user(789)
    assert user["balance"] == 1 and user["first_start_source"] == "unmarked"
    assert user["total_generations"] == 0


async def test_unregistered_share_url_is_not_downloaded(credit_db, max_image_upload):
    event = message_event("/start share_https://127.0.0.1/private")
    await call_handler(create_router(), "start", event, await ready_context())
    max_image_upload.assert_not_awaited()
    assert "недоступна" in event.message.answer.await_args_list[0].args[0]


@pytest.mark.parametrize("text", ["", "   ", "я" * 201])
async def test_invalid_custom_card_text_does_not_generate_or_charge(credit_db, text):
    await credit_db.add_credits(456, 2)
    card = AsyncMock()
    router = create_router(postcard_generator=card)
    context = await ready_context()
    await context.update_data(mode="mode_goodmorning")
    await call_handler(router, "collect_photo", photo_event(), context)
    await call_handler(router, "collect_card_text", message_event(text), context)
    card.assert_not_awaited()
    assert (await credit_db.get_user(456))["balance"] == 2
    assert await context.get_state() == PosterStates.waiting_for_card_text


async def test_failed_share_message_retry_does_not_regenerate_or_resend_image(credit_db):
    await credit_db.add_credits(456, 2)
    card = AsyncMock(return_value=["https://cdn.example/card.png"])
    router = create_router(postcard_generator=card)
    context = await ready_context()
    await context.update_data(mode="mode_goodmorning")
    await call_handler(router, "collect_photo", photo_event(), context)
    event = message_event("С добрым утром, Аня!")

    async def reply(text=None, **kwargs):
        if text == "👇 Хочешь поделиться с друзьями?":
            raise RuntimeError("share message failed")
        return object()

    event.message.answer.side_effect = reply
    await call_handler(router, "collect_card_text", event, context)
    assert event.bot.send_message.await_count == 1
    retry = callback_event("postcard_retry")
    await dispatch_callback(router, retry, context)
    card.assert_awaited_once()
    retry.bot.send_message.assert_not_awaited()
    assert (await credit_db.get_user(456))["balance"] == 1
    assert (await context.get_data())["share_prompt_sent"] is True


async def test_modern_card_button_uses_shared_photo_and_custom_text_flow(credit_db):
    assert ("🎨 Скрапбукинг", "mode_modern_card") in handlers.MODE_BUTTONS
    await credit_db.add_credits(456, 2)
    card = AsyncMock(return_value=["https://cdn.example/modern.png"])
    router = create_router(postcard_generator=card)
    context = await ready_context()
    await dispatch_callback(router, callback_event("mode_modern_card"), context)
    assert await context.get_state() == PosterStates.waiting_for_photo
    await call_handler(router, "collect_photo", photo_event(), context)
    assert await context.get_state() == PosterStates.waiting_for_card_text
    card.assert_not_awaited()
    await call_handler(router, "collect_card_text", message_event("Счастья тебе, Аня!"), context)
    card.assert_awaited_once_with("https://photos.example.com/source.png", "mode_modern_card", "Счастья тебе, Аня!")
    assert (await credit_db.get_user(456))["balance"] == 1


@pytest.mark.parametrize("mode", list(handlers.POSTCARD_PROMPTS))
@pytest.mark.parametrize("general_handler_first", [False, True])
async def test_dispatcher_routes_card_text_to_generation(
    credit_db, monkeypatch, event_replies, caplog, mode, general_handler_first, text_correction,
):
    from maxapi import Dispatcher
    from maxapi.context.isolation import SimpleEventIsolation
    from maxapi.types import MessageCreated, MessageCallback, User

    monkeypatch.setattr(handlers, "_reply", event_replies)
    await credit_db.add_credits(456, 2)
    generator = AsyncMock(return_value=["https://cdn.example/dispatched-card.png"])
    router = create_router(postcard_generator=generator)
    if general_handler_first:
        # Воспроизвести перехват общего обработчика до фильтра состояния.
        generic = next(h for h in router.event_handlers if h.func_event.__name__ == "echo")
        router.event_handlers.remove(generic)
        index = next(i for i, h in enumerate(router.event_handlers) if h.func_event.__name__ == "collect_card_text")
        router.event_handlers.insert(index, generic)
    dp = Dispatcher(storage=MemoryContext, event_isolation=SimpleEventIsolation())
    dp.include_routers(router)
    bot = SimpleNamespace(
        me=User(user_id=999, first_name="Bot", username="test_bot", is_bot=True),
        send_message=AsyncMock(return_value=object()), send_callback=AsyncMock(),
    )
    user = {"user_id": 456, "first_name": "Test", "is_bot": False}

    def message(text=None, attachments=None, sender=None):
        return {
            "sender": sender or user,
            "recipient": {"chat_id": 123, "chat_type": "dialog"}, "timestamp": 1,
            "body": {"mid": "fsm-test", "seq": 1, "text": text, "attachments": attachments},
        }

    try:
        callback = MessageCallback(
            timestamp=1, bot=bot, message=message(sender=bot.me.model_dump()),
            callback={"timestamp": 1, "callback_id": "mode", "payload": mode, "user": user},
        )
        await dp.handle(callback)
        context = dp.fsm.get_context(chat_id=123, user_id=456)
        assert await context.get_state() == PosterStates.waiting_for_photo
        photo = MessageCreated(timestamp=2, bot=bot, message=message(attachments=[{
            "type": "image", "payload": {"photo_id": 1, "token": "photo", "url": "https://cdn.example/source.png"},
        }]))
        await dp.handle(photo)
        assert await context.get_state() == PosterStates.waiting_for_card_text
        generator.assert_not_awaited()
        user_text = "Счастя тебе, Аня!"
        fixed_text = "Счастья тебе, Аня!"
        text_correction.side_effect = None
        text_correction.return_value = fixed_text
        with caplog.at_level(logging.INFO, logger=handlers.__name__):
            await dp.handle(MessageCreated(timestamp=3, bot=bot, message=message(text=user_text)))
        text_correction.assert_awaited_once_with(user_text)
        generator.assert_awaited_once_with("https://cdn.example/source.png", mode, fixed_text)
        assert f"Исходный текст: {user_text} | Исправленный: {fixed_text}" in caplog.text
        assert f"Начинаю генерацию открытки с текстом: {user_text}" in caplog.text
        replies = [call.kwargs.get("text") for call in bot.send_message.await_args_list]
        assert "⏳ Генерирую..." in replies and "Ваша открытка готова!" in replies
        assert user_text not in replies
        assert await context.get_state() is None
        assert (await credit_db.get_user(456))["balance"] == 1
        assert (await credit_db.get_user(456))["total_generations"] == 1
        assert (await credit_db.get_user(456))["last_active_at"] == "1970-01-01T00:00:00.003+00:00"
        if general_handler_first:
            assert "передаю текст из общего обработчика" in caplog.text
    finally:
        await dp.shutdown()


@pytest.mark.parametrize("photo", [None, {}, {"url": ""}])
async def test_card_text_without_photo_requests_photo_instead_of_generating(photo):
    context = await ready_context()
    await context.update_data(mode="mode_modern_card", photo=photo)
    await context.set_state(PosterStates.waiting_for_card_text)
    card = AsyncMock()
    event = message_event("С праздником, Аня!")
    await call_handler(create_router(postcard_generator=card), "collect_card_text", event, context)
    card.assert_not_awaited()
    assert await context.get_state() == PosterStates.waiting_for_photo
    assert (await context.get_data())["mode"] == "mode_modern_card"
    assert "загрузите фото заново" in event.message.answer.await_args.args[0]


async def test_general_handler_only_echoes_when_no_fsm_state():
    router = create_router()
    context = await ready_context()
    active = message_event("Новая мечта")
    await call_handler(router, "echo", active, context)
    active.message.answer.assert_not_awaited()
    await context.set_state(None)
    idle = message_event("Привет")
    await call_handler(router, "echo", idle, context)
    idle.message.answer.assert_awaited_once_with(text="Привет")


async def test_correction_failure_can_retry_without_charge(credit_db, text_correction):
    await credit_db.add_credits(456, 2)
    context = await ready_context()
    await context.update_data(mode="mode_modern_card")
    card = AsyncMock(return_value=["https://cdn.example/card.png"])
    router = create_router(postcard_generator=card)
    await call_handler(router, "collect_photo", photo_event(), context)
    text_correction.side_effect = [RuntimeError("correction unavailable"), "Счастья, Аня!"]
    await call_handler(router, "collect_card_text", message_event("Счастя, Аня!"), context)
    card.assert_not_awaited()
    assert (await credit_db.get_user(456))["balance"] == 2
    assert await context.get_state() == PosterStates.waiting_for_card_text
    await dispatch_callback(router, callback_event("postcard_retry"), context)
    card.assert_awaited_once_with("https://photos.example.com/source.png", "mode_modern_card", "Счастья, Аня!")
    assert (await credit_db.get_user(456))["balance"] == 1


async def test_correction_cached_after_image_failure_but_replaced_for_new_text(credit_db, text_correction):
    await credit_db.add_credits(456, 3)
    context = await ready_context()
    await context.update_data(mode="mode_ny")
    card = AsyncMock(side_effect=[RuntimeError("image unavailable"), ["https://cdn.example/card.png"], ["https://cdn.example/next.png"]])
    router = create_router(postcard_generator=card)
    await call_handler(router, "collect_photo", photo_event(), context)
    text_correction.side_effect = ["Счастья, Аня!", "Здоровья, Оля!"]
    await call_handler(router, "collect_card_text", message_event("Счастя, Аня!"), context)
    await dispatch_callback(router, callback_event("postcard_retry"), context)
    text_correction.assert_awaited_once_with("Счастя, Аня!")
    assert card.await_args_list[:2] == [call("https://photos.example.com/source.png", "mode_ny", "Счастья, Аня!")] * 2
    await dispatch_callback(router, callback_event("more"), context)
    await call_handler(router, "collect_photo", photo_event(), context)
    await call_handler(router, "collect_card_text", message_event("Здаровья, Оля!"), context)
    assert text_correction.await_count == 2
    assert card.await_args.args[-1] == "Здоровья, Оля!"
    assert (await credit_db.get_user(456))["balance"] == 1


async def test_no_credit_does_not_call_text_api(credit_db, text_correction):
    context = await ready_context()
    await context.update_data(mode="mode_ny")
    router = create_router(postcard_generator=AsyncMock())
    await call_handler(router, "collect_photo", photo_event(), context)
    await call_handler(router, "collect_card_text", message_event("Аня, с праздником!"), context)
    text_correction.assert_not_awaited()


async def test_stats_displays_distinct_nonzero_values_for_each_event(credit_db):
    fields = [
        ("buy_1", "1 генерация (99 ₽)"), ("buy_5", "5 генераций (299 ₽)"),
        ("buy_15", "15 генераций (799 ₽)"), ("gen_vision", "Карта желаний"),
        ("gen_ny", "Новый год"), ("gen_ny_family", "Семейный НГ"),
        ("gen_bday_young", "ДР (Молодым)"), ("gen_bday_old", "ДР (Старшим)"),
        ("gen_halloween", "Хэллоуин"), ("gen_bday_old_elegant", "ДР Старшим 2"),
        ("gen_ny_cozy", "Уютный НГ"), ("gen_ny_fairytale", "Зимняя сказка"),
        ("gen_ny_elegant", "Элегантный НГ"), ("gen_goodmorning", "Доброе утро"),
        ("gen_scrapbook", "Скрапбукинг"),
    ]
    for count, (key, _) in enumerate(fields, 1):
        for _ in range(count):
            await credit_db.increment_counter(key, user_id=123)
    await credit_db.increment_counter("gen_8march", user_id=123)
    event = message_event("/stats")
    await call_handler(create_router(admin_user_id=456), "stats", event)
    text = event.message.answer.await_args.args[0]
    for count, (_, label) in enumerate(fields, 1):
        assert f"- {label}: {count}\n" in text
    assert "📍 Источники первого запуска" in text
    assert "8 Марта" not in text
    assert (await credit_db.get_all_counters())["gen_8march"] == 1


@pytest.mark.parametrize("failure", ["missing", "upload", "send"])
async def test_start_still_shows_menu_after_example_failures(example_photos, failure, caplog):
    event = message_event("/start")
    if failure == "missing":
        (example_photos / "i (26).webp").unlink()
    elif failure == "upload":
        image = event.bot.upload_media.return_value
        async def upload(media):
            if Path(media.path).name == "i (26).webp":
                raise RuntimeError("upload failed")
            return image
        event.bot.upload_media.side_effect = upload
    else:
        event.message.answer.side_effect = [RuntimeError("album rejected"), None, None]
    await call_handler(create_router(), "start", event, await ready_context())
    replies = event.message.answer.await_args_list
    assert len(replies[0].kwargs["attachments"]) == (6 if failure == "send" else 5)
    assert replies[1].kwargs["text"] == handlers.WELCOME_TEXT
    assert len(replies[1].kwargs["attachments"]) == 1
    assert "Не удалось" in caplog.text


@pytest.mark.parametrize("mode", ["mode_vision", *handlers.POSTCARD_PROMPTS])
async def test_more_restarts_with_new_photo_then_corrected_text_or_dream(credit_db, text_correction, mode):
    await credit_db.add_credits(456, 2)
    context = await ready_context()
    await context.update_data(mode=mode, card_text="старое поздравление", fixed_card_text="старое",
                              variants=["https://cdn.example/old.png"], sent_count=1)
    card = AsyncMock(return_value=["https://cdn.example/new.png"])
    vision = AsyncMock(return_value=["https://cdn.example/new.png"])
    router = create_router(vision, postcard_generator=card)
    await dispatch_callback(router, callback_event("more"), context)
    assert await context.get_state() == PosterStates.waiting_for_photo
    assert await context.get_data() == {"mode": mode}
    event = photo_event()
    event.message.body.attachments[0].payload.url = "https://cdn.example/new-reference.png"
    await call_handler(router, "collect_photo", event, context)
    card.assert_not_awaited()
    vision.assert_not_awaited()
    text_correction.side_effect = None
    text_correction.return_value = "Счастья, Аня!"
    if mode == "mode_vision":
        assert await context.get_state() == PosterStates.waiting_for_prompt
        await call_handler(router, "collect_sphere", message_event("Путешествовать"), context)
        vision.assert_awaited_once_with("https://cdn.example/new-reference.png", "Путешествовать")
        text_correction.assert_not_awaited()
    else:
        assert await context.get_state() == PosterStates.waiting_for_card_text
        await call_handler(router, "echo", message_event("Счастя, Аня!"), context)
        text_correction.assert_awaited_once_with("Счастя, Аня!")
        card.assert_awaited_once_with("https://cdn.example/new-reference.png", mode, "Счастья, Аня!")
    assert await context.get_state() is None
    assert (await credit_db.get_user(456))["balance"] == 1


async def test_broadcast_marks_only_forbidden_and_skips_inactive_users(credit_db, monkeypatch):
    for user_id in (101, 102, 103, 104):
        await credit_db.add_credits(user_id, 1)
    await credit_db.mark_user_inactive(104)
    monkeypatch.setattr(handlers.asyncio, "sleep", AsyncMock())
    event = message_event("/broadcast Новые открытки готовы!")
    async def send(**kwargs):
        if kwargs["user_id"] == 101:
            raise MaxApiError(code=403, raw={"code": "recipient.blocked", "message": "forbidden"})
        if kwargs["user_id"] == 102:
            raise MaxApiError(code=500, raw="temporary error")
    event.bot.send_message.side_effect = send
    router = create_router(admin_user_id=456)
    await call_handler(router, "broadcast", event)
    assert [c.kwargs["user_id"] for c in event.bot.send_message.await_args_list] == [101, 102, 103]
    assert all(c.kwargs["text"] == "Новые открытки готовы!" and "chat_id" not in c.kwargs
               for c in event.bot.send_message.await_args_list)
    assert (await credit_db.get_user(101))["is_active"] == 0
    assert (await credit_db.get_user(102))["is_active"] == 1
    assert "Отправлено: 1. Недоступны (403): 1. Ошибок: 1." in event.message.answer.await_args.args[0]
    event.bot.send_message.reset_mock()
    await call_handler(router, "broadcast", event)
    assert [c.kwargs["user_id"] for c in event.bot.send_message.await_args_list] == [102, 103]


@pytest.mark.parametrize("status", [401, 429])
async def test_broadcast_stops_on_auth_or_rate_limit_without_marking_blocked(credit_db, status):
    await credit_db.add_credits(101, 1)
    await credit_db.add_credits(102, 1)
    event = message_event("/broadcast Привет!")
    event.bot.send_message.side_effect = MaxApiError(code=status, raw="API error")
    await call_handler(create_router(admin_user_id=456), "broadcast", event)
    event.bot.send_message.assert_awaited_once_with(user_id=101, text="Привет!")
    assert await credit_db.get_active_user_ids() == [101, 102]
    assert "Рассылка остановлена" in event.message.answer.await_args.args[0]


@pytest.mark.parametrize("admin,actor,is_bot", [(None, 456, False), (789, 456, False), (456, None, False), (456, 456, True)])
async def test_broadcast_ignores_unauthorized_and_bots(credit_db, admin, actor, is_bot):
    await credit_db.add_credits(101, 1)
    event = message_event("/broadcast Привет!")
    event.get_ids = lambda: (456, actor)
    event.message.sender.is_bot = is_bot
    await call_handler(create_router(admin_user_id=admin), "broadcast", event)
    event.bot.send_message.assert_not_awaited()
    event.message.answer.assert_not_awaited()


@pytest.mark.parametrize("text", ["/broadcast", "/broadcast   ", "/broadcast " + "a" * 4001])
async def test_broadcast_validates_text(credit_db, text):
    event = message_event(text)
    await call_handler(create_router(admin_user_id=456), "broadcast", event)
    event.bot.send_message.assert_not_awaited()
    assert "Используйте /broadcast" in event.message.answer.await_args.args[0]


async def test_stats_reports_active_blocked_and_returned(credit_db):
    from datetime import datetime
    def stamp(day):
        return int(datetime.fromisoformat(f"2026-10-{day}T12:00:00+00:00").timestamp() * 1000)
    await credit_db.update_user_activity(101, timestamp_ms=stamp(20))
    await credit_db.update_user_activity(101, timestamp_ms=stamp(21))
    await credit_db.update_user_activity(102, timestamp_ms=stamp(20))
    await credit_db.mark_user_inactive(102)
    event = message_event("/stats")
    await call_handler(create_router(admin_user_id=456), "stats", event)
    text = event.message.answer.await_args.args[0]
    assert "Всего запускали: 2" in text
    assert "Доступны для сообщений: 1" in text
    assert "Заблокировали/Удалили: 1" in text
    assert "Вернулись после 1-го дня: 1" in text
    assert "Проданные пакеты" in text and "Скрапбукинг" in text


@pytest.mark.parametrize("kind", ["message", "callback", "bot_started"])
async def test_activity_middleware_updates_before_handler_for_all_user_events(credit_db, kind):
    from maxapi.types import MessageCreated, MessageCallback
    event = bot_started_event(user_id=789, timestamp=1792526400000)
    user = event.user.model_dump()
    message = {"sender": user, "recipient": {"chat_id": 123, "chat_type": "dialog"},
               "timestamp": 1, "body": {"mid": "activity", "seq": 1, "text": "Привет"}}
    if kind == "message":
        event = MessageCreated(timestamp=1792526400000, message=message)
    elif kind == "callback":
        event = MessageCallback(timestamp=1792526400000, message=message,
                                callback={"timestamp": 1, "callback_id": "cb", "payload": "more", "user": user})
    await credit_db.update_user_activity(789, timestamp_ms=1792526400000)
    await credit_db.mark_user_inactive(789)
    called = False
    async def next_handler(event_object, data):
        nonlocal called
        called = True
        row = await credit_db.get_user(789)
        assert row["is_active"] == 1 and row["days_active"] == 1
        assert row["last_active_at"] is not None
        return "handled"
    middleware = create_router().outer_middlewares[0]
    assert await middleware(next_handler, event, {}) == "handled"
    assert called


async def test_activity_middleware_ignores_bot_messages(credit_db):
    event = bot_started_event(user_id=789, is_bot=True)
    handler = AsyncMock()
    await create_router().outer_middlewares[0](handler, event, {})
    assert await credit_db.get_user(789) is None
    handler.assert_awaited_once()


def test_new_menu_starts_with_five_new_year_modes_and_hides_march():
    modes = [payload for _, payload in handlers.MODE_BUTTONS]
    assert modes[:5] == ["mode_ny", "mode_ny_family", "mode_ny_cozy", "mode_ny_fairytale", "mode_ny_elegant"]
    assert len(modes) == 12 and len(set(modes)) == 12
    assert "mode_8march" not in modes
    assert {"mode_halloween", "mode_bday_old_elegant"} <= set(modes)
    assert "🎨 Первая генерация — бесплатно!" in handlers.WELCOME_TEXT


@pytest.mark.parametrize("active", [True, False])
async def test_discount_callback_uses_149_only_while_active(credit_db, active, monkeypatch):
    clock = [1800000000]
    monkeypatch.setattr(credit_db, "_now_ts", lambda: clock[0])
    await credit_db.grant_free_trial(456)
    await credit_db.deduct_credit(456, count_generation=True)
    if not active:
        clock[0] += 86400
    payments = SimpleNamespace(create_payment_link=AsyncMock(return_value="https://pay.example/three"))
    event = callback_event("buy_3_promo")
    await dispatch_callback(create_router(payments=payments), event, await ready_context())
    event.ack.assert_awaited_once()
    if active:
        payments.create_payment_link.assert_awaited_once_with(456, 3, promo=True)
        assert "149 ₽" in event.send.await_args.args[0]
    else:
        payments.create_payment_link.assert_awaited_once_with(456, 3)
        assert "199 ₽" in event.send.await_args.args[0] and "149 ₽" not in event.send.await_args.args[0]
    keyboard = await handlers.purchase_keyboard(456)
    assert ("buy_3_promo" in str(keyboard)) is active


async def test_discount_race_at_checkout_falls_back_to_regular_price(credit_db):
    await credit_db.grant_free_trial(456)
    await credit_db.deduct_credit(456, count_generation=True)
    payments = SimpleNamespace(create_payment_link=AsyncMock(side_effect=[handlers.PromoUnavailableError(), "https://pay.example/three"]))
    event = callback_event("buy_3_promo")
    await dispatch_callback(create_router(payments=payments), event, await ready_context())
    assert payments.create_payment_link.await_args_list == [call(456, 3, promo=True), call(456, 3)]
    assert "199 ₽" in event.send.await_args.args[0]


async def test_failed_first_generation_and_admin_do_not_start_discount(credit_db):
    await credit_db.grant_free_trial(456)
    event = message_event()
    await call_handler(create_router(AsyncMock(side_effect=RuntimeError("generation failed"))), "collect_sphere", event, await ready_context())
    assert (await credit_db.get_user(456))["promo_expires_at"] is None
    await call_handler(create_router(AsyncMock(return_value=["https://cdn.example/admin.png"]), admin_user_id=456), "collect_sphere", message_event(), await ready_context())
    assert (await credit_db.get_user(456))["promo_expires_at"] is None


async def test_first_free_postcard_shows_tripwire_after_delivery(credit_db):
    await credit_db.grant_free_trial(456)
    context = await ready_context()
    await context.update_data(mode="mode_ny_cozy")
    card = AsyncMock(return_value=["https://cdn.example/cozy.png"])
    router = create_router(postcard_generator=card)
    await call_handler(router, "collect_photo", photo_event(), context)
    event = message_event("Аня, с Новым годом!")
    await call_handler(router, "collect_card_text", event, context)
    assert await credit_db.is_promo_active(456)
    assert any(c.args and c.args[0] == handlers.PROMO_TEXT for c in event.message.answer.await_args_list)
    assert (await credit_db.get_all_counters())["gen_ny_cozy"] == 1
    assert (await credit_db.get_user(456))["balance"] == 0


@pytest.mark.parametrize("mode", [payload for _, payload in handlers.MODE_BUTTONS])
async def test_photo_guidance_is_shown_for_every_mode_and_more(mode):
    context = await ready_context()
    event = callback_event(mode)
    router = create_router()
    await dispatch_callback(router, event, context)
    assert handlers.PHOTO_GUIDANCE in event.send.await_args.args[0]
    assert await context.get_state() == PosterStates.waiting_for_photo
    more = callback_event("more")
    await dispatch_callback(router, more, context)
    assert handlers.PHOTO_GUIDANCE in more.send.await_args.args[0]
    if mode == "mode_ny_family":
        assert "общее фото семьи" in event.send.await_args.args[0]
        assert "общее фото семьи" in more.send.await_args.args[0]


@pytest.mark.parametrize("length", [200, 201])
async def test_greeting_text_boundary_matches_advertised_200_characters(credit_db, length):
    await credit_db.add_credits(456, 2)
    context = await ready_context()
    await context.update_data(mode="mode_ny_cozy")
    generator = AsyncMock(return_value=["https://cdn.example/card.png"])
    router = create_router(postcard_generator=generator)
    await call_handler(router, "collect_photo", photo_event(), context)
    event = message_event("я" * length)
    await call_handler(router, "collect_card_text", event, context)
    if length == 200:
        generator.assert_awaited_once()
        assert (await credit_db.get_user(456))["balance"] == 1
    else:
        generator.assert_not_awaited()
        assert (await credit_db.get_user(456))["balance"] == 2
        assert "200" in event.message.answer.await_args.args[0]
        assert await context.get_state() == PosterStates.waiting_for_card_text


def test_onboarding_and_tripwire_copy_contains_examples_and_supplied_social_proof():
    assert "Отлично!" in handlers.CARD_TEXT_PROMPT
    assert handlers.CARD_TEXT_PROMPT.count("• «") == 3
    assert "до 200 символов" in handlers.CARD_TEXT_PROMPT
    assert "/cancel" in handlers.CARD_TEXT_PROMPT
    assert "более 60 человек" in handlers.PROMO_TEXT
    assert "149 ₽" in handlers.PROMO_TEXT and "24 часа" in handlers.PROMO_TEXT


def test_photo_guidance_allows_family_and_card_prompt_mentions_emoji():
    assert "Вы можете загрузить как одного человека, так и целую семью!" in handlers.PHOTO_GUIDANCE
    assert "где хорошо видно лицо" in handlers.PHOTO_GUIDANCE
    assert "Вы можете использовать не только буквы, но и эмодзи (😊, 🎉, ❤️)." in handlers.CARD_TEXT_PROMPT
    assert "до 200 символов" in handlers.CARD_TEXT_PROMPT


async def test_album_helper_splits_only_after_explicit_attachment_count_rejection():
    event = message_event("/start")
    images = [SimpleNamespace(payload=SimpleNamespace(token=f"ordered-{i}")) for i in range(12)]
    event.message.answer.side_effect = [MaxApiError(code=400, raw={"message": "Too many attachments, maximum 10"}), None, None]
    await handlers.send_start_album(event, images)
    replies = event.message.answer.await_args_list
    assert [len(c.kwargs["attachments"]) for c in replies] == [12, 10, 2]
    assert replies[1].kwargs["attachments"] + replies[2].kwargs["attachments"] == images


@pytest.mark.parametrize("status,raw", [
    (500, "attachment timeout"), (429, "attachments rate limit"),
    (400, "attachment.not.ready"), (400, "invalid photo"),
])
async def test_start_album_does_not_split_on_unrelated_errors(example_photos, status, raw):
    event = message_event("/start")
    event.message.answer.side_effect = [MaxApiError(code=status, raw=raw), None, None]
    await call_handler(create_router(), "start", event, await ready_context())
    replies = event.message.answer.await_args_list
    assert len(replies) == 3
    assert len(replies[0].kwargs["attachments"]) == 6
    assert replies[1].kwargs["text"] == handlers.WELCOME_TEXT


async def test_start_sends_one_album_with_distinct_tokens_in_requested_order(example_photos):
    event = message_event("/start")
    event.bot.upload_media.side_effect = [SimpleNamespace(payload=SimpleNamespace(token=f"token-{i}")) for i in range(6)]
    await call_handler(create_router(), "start", event, await ready_context())
    albums = [c for c in event.message.answer.await_args_list if c.kwargs.get("text") == "Примеры работ:"]
    assert len(albums) == 1
    assert [a.payload.token for a in albums[0].kwargs["attachments"]] == [f"token-{i}" for i in range(6)]


def test_welcome_has_exact_requested_line_breaks_and_emoji():
    assert handlers.WELCOME_TEXT == (
        "Привет! Я твой личный нейро-художник 🎨\n\n"
        "Я создаю волшебные карты желаний и персональные открытки с твоим лицом или лицом твоего близкого человека. \n\n"
        "🎁 Ты можешь добавить любое пожелание, а мы красиво его оформим.\n\n"
        "🎨 Первая генерация — бесплатно!\n\n"
        "👇 Выбери, что хочешь создать:"
    )


async def test_stats_excludes_admin_everywhere_and_appends_admin_test_activity(credit_db):
    await credit_db.record_first_start(456, "vk_ads")
    await credit_db.record_first_start(123, "vk_ads")
    for _ in range(7):
        await credit_db.increment_generations(456, event_name="gen_halloween")
    await credit_db.increment_generations(123, event_name="gen_halloween")
    for order, user_id, credits, price in [("admin-order", 456, 5, 29900), ("regular-order", 123, 3, 19900)]:
        await credit_db.create_payment_order(order, user_id, credits, price, "package")
        await credit_db.add_credits(user_id, credits, payment_order_id=order, provider_payment_id=order)
    event = message_event("/stats")
    await call_handler(create_router(admin_user_id=456), "stats", event)
    text = event.message.answer.await_args.args[0]
    assert "Всего запускали: 1" in text and "Всего пользователей: 1" in text
    assert "Покупателей: 1" in text and "Всего покупок: 1" in text
    assert "- Хэллоуин: 1\n" in text and "Всего сгенерировано картинок: 1" in text
    assert "- 5 генераций (299 ₽): 0" in text
    assert "Сумма оплат: 199,00 ₽" in text and "299,00 ₽" not in text
    assert text.endswith("🛠 Тестовая активность (Админ):\n- Сгенерировано: 7\n- Покупок: 1")


async def test_admin_stats_without_existing_admin_record_shows_zero_test_activity(credit_db):
    event = message_event("/stats")
    await call_handler(create_router(admin_user_id=456), "stats", event)
    text = event.message.answer.await_args.args[0]
    assert "Всего запускали: 0" in text
    assert text.endswith("🛠 Тестовая активность (Админ):\n- Сгенерировано: 0\n- Покупок: 0")


@pytest.mark.parametrize("generated,command", [(True, "broadcast_gen"), (False, "broadcast_nogen")])
async def test_segmented_broadcast_single_message_album(credit_db, monkeypatch, example_photos, generated, command):
    for uid in (101, 102, 103):
        await credit_db.grant_free_trial(uid)
    await credit_db.increment_generations(101)
    await credit_db.mark_user_inactive(103)
    sleep = AsyncMock()
    monkeypatch.setattr(handlers.asyncio, "sleep", sleep)
    event = message_event("/" + command)
    await call_handler(create_router(admin_user_id=456), command, event)
    event.bot.send_message.assert_awaited_once()
    kwargs = event.bot.send_message.await_args.kwargs
    assert kwargs["user_id"] == (101 if generated else 102)
    assert "chat_id" not in kwargs
    assert len(kwargs["attachments"]) == 5
    assert event.bot.upload_media.await_count == 4
    assert [Path(c.args[0].path).name for c in event.bot.upload_media.await_args_list] == ["i (28).webp", "i (25).webp", "i (29).webp", "i (26).webp"]
    payload = kwargs["attachments"][-1].payload.buttons[0][0].payload
    assert payload == ("buy_promo_149" if generated else "open_main_menu")
    assert await credit_db.is_promo_active(101) == generated
    sleep.assert_awaited_once_with(0.5)


@pytest.mark.parametrize("command", ["broadcast_gen", "broadcast_nogen"])
async def test_segmented_broadcast_admin_only(credit_db, command):
    event = message_event("/" + command)
    await call_handler(create_router(admin_user_id=999), command, event)
    event.bot.upload_media.assert_not_awaited()
    event.bot.send_message.assert_not_awaited()


async def test_segmented_broadcast_missing_album_aborts_without_promo(credit_db, monkeypatch, tmp_path):
    await credit_db.increment_generations(101)
    monkeypatch.setattr(handlers, "EXAMPLES_DIR", tmp_path)
    event = message_event("/broadcast_gen")
    await call_handler(create_router(admin_user_id=456), "broadcast_gen", event)
    event.bot.send_message.assert_not_awaited()
    assert not await credit_db.is_promo_active(101)
    assert "отменена" in event.message.answer.await_args.args[0]


@pytest.mark.parametrize("status", [400, 401, 403, 413, 422, 429, 500])
async def test_segmented_broadcast_errors(credit_db, monkeypatch, example_photos, status):
    for uid in (101, 102):
        await credit_db.grant_free_trial(uid)
    monkeypatch.setattr(handlers.asyncio, "sleep", AsyncMock())
    event = message_event("/broadcast_nogen")
    event.bot.send_message.side_effect = MaxApiError(code=status, raw="API error")
    await call_handler(create_router(admin_user_id=456), "broadcast_nogen", event)
    assert event.bot.send_message.await_count == 2
    assert (await credit_db.get_user(101))["is_active"] == (0 if status == 403 else 1)
    assert event.message.answer.await_args.args[0] == "✅ Рассылка завершена! Успешно: 0, Ошибок: 2."


async def test_broadcast_promo_callback_alias(credit_db):
    await credit_db.increment_generations(456)
    await credit_db.start_broadcast_promo(456, "campaign")
    payments = SimpleNamespace(create_payment_link=AsyncMock(return_value="https://pay.example/149"))
    await call_handler(create_router(admin_user_id=456, payments=payments), "buy_callback", callback_event("buy_promo_149"))
    payments.create_payment_link.assert_awaited_once_with(456, 3, promo=True)


async def test_segmented_broadcast_reuses_uploaded_album(credit_db, monkeypatch, example_photos):
    for uid in (101, 102):
        await credit_db.increment_generations(uid)
    monkeypatch.setattr(handlers.asyncio, "sleep", AsyncMock())
    event = message_event("/broadcast_gen")
    await call_handler(create_router(admin_user_id=456), "broadcast_gen", event)
    assert event.bot.upload_media.await_count == 4
    assert [c.kwargs["user_id"] for c in event.bot.send_message.await_args_list] == [101, 102]
    assert all(len(c.kwargs["attachments"]) == 5 for c in event.bot.send_message.await_args_list)
    assert (await credit_db.get_user(101))["promo_campaign_id"] == (await credit_db.get_user(102))["promo_campaign_id"]


@pytest.mark.parametrize("command", ["broadcast_gen", "broadcast_nogen"])
async def test_segmented_broadcast_continues_after_attachment_error(credit_db, monkeypatch, example_photos, caplog, command):
    for uid in (101, 102):
        await credit_db.grant_free_trial(uid)
        if command == "broadcast_gen":
            await credit_db.increment_generations(uid)
    monkeypatch.setattr(handlers.asyncio, "sleep", AsyncMock())
    event = message_event("/" + command)
    event.bot.send_message.side_effect = [
        MaxApiError(code=400, raw={"code": "attachment.max-size-reached"}), None,
    ]
    await call_handler(create_router(admin_user_id=456), command, event)
    assert [c.kwargs["user_id"] for c in event.bot.send_message.await_args_list] == [101, 102]
    assert event.message.answer.await_args.args[0] == "✅ Рассылка завершена! Успешно: 1, Ошибок: 1."
    assert "Ошибка отправки пользователю 101:" in caplog.text
    assert "attachment.max-size-reached" in caplog.text


async def test_segmented_broadcast_continues_if_marking_blocked_fails(credit_db, monkeypatch, example_photos):
    for uid in (101, 102):
        await credit_db.grant_free_trial(uid)
    monkeypatch.setattr(handlers.asyncio, "sleep", AsyncMock())
    monkeypatch.setattr(handlers, "mark_user_inactive", AsyncMock(side_effect=RuntimeError("db error")))
    event = message_event("/broadcast_nogen")
    event.bot.send_message.side_effect = [MaxApiError(code=403, raw="forbidden"), None]
    await call_handler(create_router(admin_user_id=456), "broadcast_nogen", event)
    assert event.bot.send_message.await_count == 2
    assert event.message.answer.await_args.args[0] == "✅ Рассылка завершена! Успешно: 1, Ошибок: 1."


async def setup_reminder_users(credit_db, monkeypatch, user_ids=(101, 102)):
    now = 1800000000
    monkeypatch.setattr(credit_db, "_now_ts", lambda: now)
    for uid in user_ids:
        await credit_db.increment_generations(uid)
        await credit_db.start_broadcast_promo(uid, "reminder-campaign")
    # Через 22 часа скидка заканчивается через 2 часа.
    monkeypatch.setattr(credit_db, "_now_ts", lambda: now + 22 * 3600)
    monkeypatch.setattr(handlers.asyncio, "sleep", AsyncMock())


async def test_broadcast_reminder_text_button_and_preserved_deadline(credit_db, monkeypatch):
    await setup_reminder_users(credit_db, monkeypatch)
    before = dict(await credit_db.get_user(101))
    event = message_event("/broadcast_reminder")
    router = create_router(admin_user_id=456)
    await call_handler(router, "broadcast_reminder", event)
    assert event.message.answer.await_args_list[0].args[0] == "⏳ Начинаю рассылку напоминаний для 2 пользователей..."
    assert [c.kwargs["user_id"] for c in event.bot.send_message.await_args_list] == [101, 102]
    for c in event.bot.send_message.await_args_list:
        assert "chat_id" not in c.kwargs
        assert c.kwargs["text"] == (
            "⏳ Напоминаем! Специальное предложение (3 генерации за 149 ₽) сгорит через 2-3 часа!\n\n"
            "Не упустите шанс создать красивые открытки для себя и близких. "
            "Купленные генерации останутся с вами навсегда, вы сможете использовать их, когда захотите."
        )
        assert len(c.kwargs["attachments"]) == 1
        button = c.kwargs["attachments"][0].payload.buttons[0][0]
        assert button.text == "Купить 3 генерации за 149 ₽"
        assert button.payload == "buy_promo_149"
    assert handlers.asyncio.sleep.await_args_list == [call(0.5), call(0.5)]
    event.bot.upload_media.assert_not_awaited()
    assert event.message.answer.await_args.args[0] == "✅ Напоминания отправлены! Успешно: 2, Ошибок: 0"
    assert dict(await credit_db.get_user(101)) == before
    await call_handler(router, "broadcast_reminder", event)
    assert dict(await credit_db.get_user(101)) == before


@pytest.mark.parametrize("status", [400, 401, 403, 429, 500])
async def test_broadcast_reminder_continues_after_error(credit_db, monkeypatch, caplog, status):
    await setup_reminder_users(credit_db, monkeypatch)
    event = message_event("/broadcast_reminder")
    event.bot.send_message.side_effect = [MaxApiError(code=status, raw="reminder failure"), None]
    await call_handler(create_router(admin_user_id=456), "broadcast_reminder", event)
    assert [c.kwargs["user_id"] for c in event.bot.send_message.await_args_list] == [101, 102]
    assert event.message.answer.await_args.args[0] == "✅ Напоминания отправлены! Успешно: 1, Ошибок: 1"
    assert "Ошибка отправки пользователю 101:" in caplog.text
    assert (await credit_db.get_user(101))["is_active"] == (0 if status == 403 else 1)


@pytest.mark.parametrize("admin,actor,is_bot", [(None, 456, False), (999, 456, False), (456, None, False), (456, 456, True)])
async def test_broadcast_reminder_admin_only(credit_db, admin, actor, is_bot):
    event = message_event("/broadcast_reminder")
    event.get_ids = lambda: (123, actor)
    event.message.sender.is_bot = is_bot
    await call_handler(create_router(admin_user_id=admin), "broadcast_reminder", event)
    event.bot.send_message.assert_not_awaited()
    event.message.answer.assert_not_awaited()


async def test_broadcast_reminder_empty_segment(credit_db):
    event = message_event("/broadcast_reminder")
    await call_handler(create_router(admin_user_id=456), "broadcast_reminder", event)
    event.bot.send_message.assert_not_awaited()
    assert event.message.answer.await_args_list[0].args[0] == "⏳ Начинаю рассылку напоминаний для 0 пользователей..."
    assert event.message.answer.await_args.args[0] == "✅ Напоминания отправлены! Успешно: 0, Ошибок: 0"


async def test_broadcast_reminder_rechecks_paid_and_blocked_users(credit_db, monkeypatch):
    await setup_reminder_users(credit_db, monkeypatch, (101, 102, 103))
    await credit_db.create_payment_order("reminder-order", 103, 3, 14900, "3 генерации", promo=True)
    event = message_event("/broadcast_reminder")
    async def send(**kwargs):
        await credit_db.mark_user_inactive(102)
        await credit_db.add_credits(103, 3, payment_order_id="reminder-order", provider_payment_id="reminder-payment")
    event.bot.send_message.side_effect = send
    await call_handler(create_router(admin_user_id=456), "broadcast_reminder", event)
    event.bot.send_message.assert_awaited_once()
    assert event.message.answer.await_args.args[0] == "✅ Напоминания отправлены! Успешно: 1, Ошибок: 0"


async def test_broadcast_reminder_shares_broadcast_lock(credit_db, monkeypatch):
    await setup_reminder_users(credit_db, monkeypatch, (101,))
    router = create_router(admin_user_id=456)
    event = message_event("/broadcast_reminder")
    second = message_event("/broadcast_nogen")
    async def send(**kwargs):
        await call_handler(router, "broadcast_nogen", second)
    event.bot.send_message.side_effect = send
    await call_handler(router, "broadcast_reminder", event)
    second.bot.send_message.assert_not_awaited()
    assert second.message.answer.await_args.args[0] == "Рассылка уже выполняется. Дождитесь завершения."


@pytest.mark.parametrize("first_entry", ["bot_started", "command"])
async def test_dispatcher_welcomes_link_and_command_with_same_menu(
    credit_db, example_photos, monkeypatch, event_replies, caplog, first_entry,
):
    from maxapi import Dispatcher
    from maxapi.context.isolation import SimpleEventIsolation
    from maxapi.types import MessageCreated, User

    monkeypatch.setattr(handlers, "_reply", event_replies)
    started = bot_started_event()
    bot = started.bot
    bot.me = User(user_id=999, first_name="Bot", is_bot=True)
    command = MessageCreated(
        timestamp=started.timestamp, bot=bot,
        message={
            "sender": started.user.model_dump(),
            "recipient": {"chat_id": 123, "chat_type": "dialog"},
            "timestamp": started.timestamp,
            "body": {"mid": "welcome-test", "seq": 1, "text": "/start"},
        },
    )
    dp = Dispatcher(storage=MemoryContext, event_isolation=SimpleEventIsolation())
    dp.include_routers(create_router())
    try:
        entries = [started, command] if first_entry == "bot_started" else [command, started]
        with caplog.at_level(logging.INFO):
            await dp.handle(entries[0])
            context = dp.fsm.get_context(chat_id=123, user_id=456)
            assert await context.get_state() == PosterStates.waiting_for_mode
            await context.set_state(PosterStates.waiting_for_card_text)
            await context.update_data(mode="mode_ny", photo="old-photo", card_text="old-text")
            await dp.handle(entries[1])
        greetings = [c.kwargs for c in bot.send_message.await_args_list if c.kwargs.get("text") == handlers.WELCOME_TEXT]
        assert len(greetings) == 2
        assert all(g["user_id"] == 456 and "chat_id" not in g for g in greetings)
        assert greetings[0]["attachments"] == greetings[1]["attachments"]
        assert greetings[0]["attachments"] == [handlers.main_keyboard()]
        assert await context.get_state() == PosterStates.waiting_for_mode
        assert await context.get_data() == {}
        assert (await credit_db.get_user(456))["balance"] == 1
        assert "Получено bot_started: отправляю приветствие пользователю 456" in caplog.text
    finally:
        await dp.shutdown()


async def test_bot_started_welcomes_even_if_examples_cannot_upload(
    credit_db, example_photos, monkeypatch, event_replies,
):
    monkeypatch.setattr(handlers, "_reply", event_replies)
    event = bot_started_event()
    event.bot.upload_media.side_effect = RuntimeError("example upload unavailable")
    context = await ready_context()
    await call_handler(create_router(), "bot_started", event, context)
    greeting = event.bot.send_message.await_args_list[0].kwargs
    assert greeting["text"] == handlers.WELCOME_TEXT
    assert greeting["user_id"] == 456
    assert greeting["attachments"] == [handlers.main_keyboard()]
    assert await context.get_state() == PosterStates.waiting_for_mode


@pytest.mark.parametrize("state", [
    None, PosterStates.waiting_for_mode, PosterStates.waiting_for_photo,
    PosterStates.waiting_for_prompt, PosterStates.waiting_for_card_text,
    PosterStates.waiting_for_sphere, PosterStates.generating,
])
async def test_main_menu_button_resets_fsm_and_sends_welcome(credit_db, example_photos, state):
    await credit_db.grant_free_trial(456)
    context = await ready_context()
    await context.set_state(state)
    await context.update_data(mode="mode_ny", card_text="старый текст", retry_url="https://cdn.example/old.png")
    event = callback_event("open_main_menu")
    await dispatch_callback(create_router(), event, context)
    event.ack.assert_awaited_once_with(notification="Открываю меню…")
    assert await context.get_state() == PosterStates.waiting_for_mode
    assert await context.get_data() == {}
    greetings = [c.kwargs for c in event.send.await_args_list if c.kwargs.get("text") == handlers.WELCOME_TEXT]
    assert len(greetings) == 1
    buttons = greetings[0]["attachments"][0].payload.buttons
    assert [(row[0].text, row[0].payload) for row in buttons[:-1]] == list(handlers.CATEGORY_BUTTONS)
    assert [(button.text, button.payload) for button in buttons[-1]] == [
        ("🖼 Примеры", "show_examples"), ("🆘 Помощь", "show_help"),
    ]
    assert (await credit_db.get_user(456))["balance"] == 1


async def test_help_main_menu_button_returns_to_start(credit_db, example_photos):
    context = await ready_context()
    await context.set_state(PosterStates.waiting_for_card_text)
    router = create_router()
    help_event = callback_event("help")
    await dispatch_callback(router, help_event, context)
    button = help_event.send.await_args.kwargs["attachments"][0].payload.buttons[-1][0]
    assert button.text == "🔄 Главное меню"
    event = callback_event(button.payload)
    await dispatch_callback(router, event, context)
    assert await context.get_state() == PosterStates.waiting_for_mode
    assert await context.get_data() == {}
    assert any(c.kwargs.get("text") == handlers.WELCOME_TEXT for c in event.send.await_args_list)


async def test_main_menu_button_acknowledges_deleted_message():
    context = await ready_context()
    before = await context.get_data()
    event = callback_event("open_main_menu", deleted=True)
    await dispatch_callback(create_router(), event, context)
    event.ack.assert_awaited_once_with(notification="Отправьте /start, чтобы открыть меню.")
    event.send.assert_not_awaited()
    assert await context.get_data() == before


async def test_vision_channel_invitation_immediately_follows_image(credit_db):
    await credit_db.add_credits(456, 2)
    generator = AsyncMock(return_value=["https://cdn.example/vision.png"])
    router = create_router(generator)
    event = message_event("Моя мечта")
    messages = []
    async def image(**kwargs):
        messages.append(kwargs)
        return object()
    async def reply(text=None, **kwargs):
        messages.append({"text": text, **kwargs})
        return object()
    event.bot.send_message.side_effect = image
    event.message.answer.side_effect = reply
    await call_handler(router, "collect_sphere", event, await ready_context())
    image_index = next(i for i, message in enumerate(messages) if message.get("text") == "Ваш плакат готов!")
    invite = messages[image_index + 1]
    assert invite["text"] == (
        "✨ Понравилась твоя карта желаний?\n\n"
        "Присоединяйся к нашему каналу «Жизнь мечты | Как изменить свою жизнь». "
        "Там мы делимся вдохновением, секретами исполнения желаний и новыми шаблонами!"
    )
    button = invite["attachments"][0].model_dump()["payload"]["buttons"][0][0]
    assert button["text"] == "Перейти в канал"
    assert button["type"] == "link"
    assert button["url"] == "https://max.ru/se14596751_biz"
    assert messages[image_index + 2]["text"] == "👇 Хочешь поделиться с друзьями?"


@pytest.mark.parametrize("confirmed", [False, True])
async def test_no_channel_invitation_when_image_not_delivered(credit_db, confirmed):
    await credit_db.add_credits(456, 2)
    generator = AsyncMock(return_value=["https://cdn.example/vision.png"])
    event = message_event("Моя мечта")
    if confirmed:
        event.bot.send_message.return_value = None
    else:
        event.bot.send_message.side_effect = RuntimeError("image delivery failed")
    await call_handler(create_router(generator), "collect_sphere", event, await ready_context())
    assert not any(c.args and c.args[0] == handlers.CHANNEL_TEXT for c in event.message.answer.await_args_list)


async def test_vision_invitation_not_duplicated_after_share_retry(credit_db):
    await credit_db.add_credits(456, 2)
    generator = AsyncMock(return_value=["https://cdn.example/vision.png"])
    router = create_router(generator)
    event = message_event("Моя мечта")
    context = await ready_context()
    fail_share = True
    async def reply(text=None, **kwargs):
        nonlocal fail_share
        if text == "👇 Хочешь поделиться с друзьями?" and fail_share:
            fail_share = False
            raise RuntimeError("share failed")
        return object()
    event.message.answer.side_effect = reply
    await call_handler(router, "collect_sphere", event, context)
    await call_handler(router, "collect_sphere", event, context)
    invites = [c for c in event.message.answer.await_args_list if c.args and c.args[0] == handlers.CHANNEL_TEXT]
    assert len(invites) == 1
    generator.assert_awaited_once()
    event.bot.send_message.assert_awaited_once()
    assert (await credit_db.get_user(456))["balance"] == 1


CATEGORY_EXPECTATIONS = {
    "cat_vision": ([1], ["mode_vision", "back_to_main"]),
    "cat_cards": ([28, 22, 25, 29, 27, 21], ["sub_ny", "sub_bday", "sub_halloween", "sub_goodmorning", "back_to_main"]),
    "sub_ny": ([28, 22, 23, 24, 25], ["mode_ny", "mode_ny_family", "mode_ny_cozy", "mode_ny_fairytale", "mode_ny_elegant", "back_to_cards"]),
    "sub_bday": ([29, 27, 21], ["mode_bday_young", "mode_bday_old", "mode_bday_old_elegant", "mode_modern_card", "back_to_cards"]),
    "sub_halloween": ([26], ["mode_halloween", "back_to_cards"]),
    "sub_goodmorning": ([20], ["mode_goodmorning", "back_to_cards"]),
    "cat_photo": ([], ["photo_style_studio", "photo_style_walk", "photo_style_ny", "back_to_main"]),
    "cat_audio": ([], ["back_to_main"]),
}


@pytest.mark.parametrize("screen", list(CATEGORY_EXPECTATIONS))
async def test_category_screens_ordered_album_and_buttons(credit_db, example_photos, screen):
    context = await ready_context()
    event = callback_event(screen)
    await dispatch_callback(create_router(), event, context)
    numbers, payloads = CATEGORY_EXPECTATIONS[screen]
    event.ack.assert_awaited_once_with(notification="Открываю…")
    assert [Path(c.args[0].path).name for c in event.bot.upload_media.await_args_list] == [f"i ({n}).webp" for n in numbers]
    replies = event.send.await_args_list
    assert len(replies) == (2 if numbers else 1)
    if numbers:
        assert len(replies[0].kwargs["attachments"]) == len(numbers)
        assert replies[0].kwargs["text"] == "Примеры работ:"
    assert replies[-1].kwargs["text"] == handlers.CATEGORY_MENUS[screen][1]
    buttons = replies[-1].kwargs["attachments"][0].payload.buttons
    assert [row[0].payload for row in buttons] == payloads
    assert await context.get_state() == PosterStates.waiting_for_mode
    assert await context.get_data() == {}
    assert await credit_db.get_user(456) is None  # Навигация не начисляет и не списывает кредиты.


@pytest.mark.parametrize("payload,screen", [("back_to_main", None), ("back_to_cards", "cat_cards")])
@pytest.mark.parametrize("state", [None, PosterStates.waiting_for_photo, PosterStates.waiting_for_card_text, PosterStates.generating])
async def test_back_navigation_clears_fsm(credit_db, example_photos, payload, screen, state):
    await credit_db.grant_free_trial(456)
    context = await ready_context()
    await context.set_state(state)
    await context.update_data(mode="mode_ny", card_text="old", retry_url="https://cdn.example/old.png")
    event = callback_event(payload)
    await dispatch_callback(create_router(), event, context)
    event.ack.assert_awaited_once()
    assert await context.get_state() == PosterStates.waiting_for_mode
    assert await context.get_data() == {}
    expected = handlers.WELCOME_TEXT if screen is None else handlers.CATEGORY_MENUS[screen][1]
    assert event.send.await_args_list[-1].kwargs["text"] == expected
    assert (await credit_db.get_user(456))["balance"] == 1


MODE_SCREENS = [(screen, mode) for screen, (_, payloads) in CATEGORY_EXPECTATIONS.items()
                for mode in payloads if mode.startswith("mode_")]


@pytest.mark.parametrize("screen,mode", MODE_SCREENS)
async def test_categories_preserve_photo_and_text_generation_flow(credit_db, example_photos, screen, mode):
    await credit_db.add_credits(456, 2)
    vision = AsyncMock(return_value=["https://cdn.example/result.png"])
    card = AsyncMock(return_value=["https://cdn.example/result.png"])
    router = create_router(vision, postcard_generator=card)
    context = await ready_context()
    await dispatch_callback(router, callback_event(screen), context)
    await dispatch_callback(router, callback_event(mode), context)
    assert await context.get_state() == PosterStates.waiting_for_photo
    assert await context.get_data() == {"mode": mode}
    await call_handler(router, "collect_photo", photo_event(), context)
    assert await context.get_state() == (PosterStates.waiting_for_prompt if mode == "mode_vision" else PosterStates.waiting_for_card_text)
    vision.assert_not_awaited()
    card.assert_not_awaited()
    if mode == "mode_vision":
        await call_handler(router, "collect_sphere", message_event("Моя мечта"), context)
        vision.assert_awaited_once_with("https://photos.example.com/source.png", "Моя мечта")
    else:
        await call_handler(router, "collect_card_text", message_event("Счастья, Аня!"), context)
        card.assert_awaited_once_with("https://photos.example.com/source.png", mode, "Счастья, Аня!")
    assert (await credit_db.get_user(456))["balance"] == 1
    assert (await credit_db.get_user(456))["total_generations"] == 1
    assert await context.get_state() is None


@pytest.mark.parametrize("payload", [*CATEGORY_EXPECTATIONS, "back_to_cards", "back_to_main"])
async def test_category_navigation_handles_deleted_message(payload):
    context = await ready_context()
    before = await context.get_data()
    event = callback_event(payload, deleted=True)
    await dispatch_callback(create_router(), event, context)
    event.ack.assert_awaited_once()
    event.send.assert_not_awaited()
    assert await context.get_data() == before


async def test_category_still_shows_menu_if_examples_fail(example_photos):
    event = callback_event("sub_ny")
    event.bot.upload_media.side_effect = RuntimeError("upload unavailable")
    context = await ready_context()
    await dispatch_callback(create_router(), event, context)
    event.send.assert_awaited_once()
    assert event.send.await_args.kwargs["text"] == handlers.CATEGORY_MENUS["sub_ny"][1]
    assert await context.get_state() == PosterStates.waiting_for_mode


@pytest.mark.parametrize("payments_enabled", [False, True])
async def test_new_purchase_copy_and_existing_buttons(credit_db, payments_enabled):
    event = message_event("/buy")
    payments = Mock() if payments_enabled else None
    await call_handler(create_router(payments=payments), "buy", event)
    assert event.message.answer.await_args.args[0] == (
        "💎 Доступные пакеты генераций:\n"
        "(Кредиты подходят для создания любых Карт желаний и Открыток. Они не сгорают!)\n\n"
        "• 1 генерация — 99 ₽\n• 3 генерации — 199 ₽\n• 5 генераций — 299 ₽\n• 15 генераций — 799 ₽\n\n"
        "👇 Выбери пакет для покупки:"
    )
    assert_purchase_keyboard(event.message.answer.await_args.kwargs["attachments"][0])


async def test_navigation_only_counts_interest_clicks(credit_db, example_photos):
    await credit_db.record_first_start(456, None)
    await credit_db.grant_free_trial(456)
    await credit_db.increment_generations(456, event_name="gen_vision")
    before = dict(await credit_db.get_user(456))
    before_stats = await credit_db.get_global_stats()
    router = create_router()
    context = await ready_context()
    for payload in [*CATEGORY_EXPECTATIONS, "back_to_main", "back_to_cards", "cat_photo", "cat_audio", "cat_photo"]:
        await dispatch_callback(router, callback_event(payload), context)
    counters = await credit_db.get_all_counters()
    assert counters == {"gen_vision": 1, "click_photo": 3, "click_audio": 2}
    assert dict(await credit_db.get_user(456)) == before
    assert await credit_db.get_global_stats() == before_stats


async def test_stats_interest_counts_per_user_and_excludes_admin(credit_db, example_photos):
    router = create_router(admin_user_id=456)
    for uid, payload in [(101, "cat_photo"), (101, "cat_photo"), (102, "cat_audio"),
                         (456, "cat_photo"), (456, "cat_audio")]:
        event = callback_event(payload)
        event.get_ids = lambda uid=uid: (123, uid)
        await dispatch_callback(router, event, await ready_context())
    assert await credit_db.get_all_counters() == {"click_photo": 3, "click_audio": 2}
    filtered = await credit_db.get_all_counters(admin_id=456)
    assert filtered["click_photo"] == 2 and filtered["click_audio"] == 1
    assert filtered["gen_unclassified"] == 0
    event = message_event("/stats")
    await call_handler(router, "stats", event)
    text = event.message.answer.await_args.args[0]
    assert "🖱 Интерес к новым разделам:\n- Фотосессия (клики): 2\n- Аудиопесни (клики): 1" in text
    assert text.index("🖼 Всего сгенерировано картинок:") < text.index("🖱 Интерес к новым разделам:") < text.index("📍 Источники")
    assert "🖼 Всего сгенерировано картинок: 0" in text
    assert "8 Марта" not in text


async def test_stats_interest_defaults_to_zero(credit_db):
    event = message_event("/stats")
    await call_handler(create_router(admin_user_id=456), "stats", event)
    text = event.message.answer.await_args.args[0]
    assert "- Фотосессия (клики): 0\n- Аудиопесни (клики): 0" in text


@pytest.mark.parametrize("mode", [payload for _, payload in handlers.MODE_BUTTONS] + ["mode_bday", "create_poster", "mode_8march"])
async def test_legacy_generation_buttons_request_photo_without_counting(credit_db, mode):
    await credit_db.grant_free_trial(456)
    vision, card = AsyncMock(), AsyncMock()
    context = await ready_context()
    event = callback_event(mode)
    await dispatch_callback(create_router(vision, postcard_generator=card), event, context)
    event.ack.assert_awaited_once()
    assert await context.get_state() == PosterStates.waiting_for_photo
    expected = {"mode_bday": "mode_bday_young", "create_poster": "mode_vision"}.get(mode, mode)
    assert await context.get_data() == {"mode": expected}
    assert "фото" in event.send.await_args.args[0]
    assert (await credit_db.get_user(456))["balance"] == 1
    assert (await credit_db.get_user(456))["total_generations"] == 0
    assert await credit_db.get_all_counters() == {}
    vision.assert_not_awaited()
    card.assert_not_awaited()


@pytest.mark.parametrize("mode", [payload for _, payload in handlers.MODE_BUTTONS])
async def test_generation_events_follow_existing_modes_only_after_success(credit_db, mode):
    await credit_db.add_credits(456, 2)
    async def generate(*args):
        assert await credit_db.get_all_counters() == {}
        assert (await credit_db.get_user(456))["total_generations"] == 0
        return ["https://cdn.example/counted.png"]
    generator = AsyncMock(side_effect=generate)
    router = create_router(generator, postcard_generator=generator)
    context = await ready_context()
    await context.update_data(mode=mode)
    if mode == "mode_vision":
        await call_handler(router, "collect_sphere", message_event("Моя мечта"), context)
    else:
        await call_handler(router, "collect_photo", photo_event(), context)
        await call_handler(router, "collect_card_text", message_event("Поздравляю!"), context)
    expected_events = {
        "mode_vision": "gen_vision", "mode_ny": "gen_ny", "mode_ny_family": "gen_ny_family",
        "mode_ny_cozy": "gen_ny_cozy", "mode_ny_fairytale": "gen_ny_fairytale",
        "mode_ny_elegant": "gen_ny_elegant", "mode_bday_young": "gen_bday_young",
        "mode_bday_old": "gen_bday_old", "mode_bday_old_elegant": "gen_bday_old_elegant",
        "mode_halloween": "gen_halloween", "mode_goodmorning": "gen_goodmorning",
        "mode_modern_card": "gen_scrapbook",
    }
    assert await credit_db.get_all_counters() == {expected_events[mode]: 1}
    assert (await credit_db.get_user(456))["total_generations"] == 1


@pytest.mark.parametrize("mode", ["mode_vision", "mode_ny", "mode_halloween"])
async def test_generator_failure_does_not_count_category_generation(credit_db, mode):
    await credit_db.add_credits(456, 2)
    generator = AsyncMock(side_effect=RuntimeError("OpenAI failed"))
    router = create_router(generator, postcard_generator=generator)
    context = await ready_context()
    await context.update_data(mode=mode)
    if mode == "mode_vision":
        await call_handler(router, "collect_sphere", message_event("Моя мечта"), context)
    else:
        await call_handler(router, "collect_photo", photo_event(), context)
        await call_handler(router, "collect_card_text", message_event("Поздравляю!"), context)
    assert await credit_db.get_all_counters() == {}
    assert (await credit_db.get_user(456))["total_generations"] == 0
    assert (await credit_db.get_user(456))["balance"] == 2


@pytest.mark.parametrize("payload", ["show_examples", "show_help"])
async def test_utility_sections_preserve_fsm_and_offer_back(credit_db, example_photos, payload):
    await credit_db.grant_free_trial(456)
    router = create_router()
    context = await ready_context()
    await context.set_state(PosterStates.waiting_for_card_text)
    before = await context.get_data()
    event = callback_event(payload)
    await dispatch_callback(router, event, context)
    event.ack.assert_awaited_once_with(notification="Открываю…")
    assert await context.get_state() == PosterStates.waiting_for_card_text
    assert await context.get_data() == before
    replies = event.send.await_args_list
    if payload == "show_examples":
        assert len(replies) == 2
        assert replies[0].kwargs["text"] == "Примеры работ:"
        assert len(replies[0].kwargs["attachments"]) == 6
        assert [Path(c.args[0].path).name for c in event.bot.upload_media.await_args_list] == [
            "i (1).webp", "i (29).webp", "i (28).webp", "i (27).webp", "i (26).webp", "i (21).webp",
        ]
    else:
        event.bot.upload_media.assert_not_awaited()
        assert len(replies) == 1
        text = replies[0].args[0]
        assert text.startswith(handlers.HELP_TEXT)
        assert "Как это работает:" in text
        assert "Связаться с поддержкой</a>" in text
        assert 'href="https://max.ru/u/' in text
        assert replies[0].kwargs["parse_mode"] == handlers.ParseMode.HTML
    button = replies[-1].kwargs["attachments"][0].payload.buttons[0][0]
    assert button.text == "⬅️ Назад" and button.payload == "back_to_main"
    await dispatch_callback(router, callback_event(button.payload), context)
    assert await context.get_state() == PosterStates.waiting_for_mode
    assert await context.get_data() == {}
    assert (await credit_db.get_user(456))["balance"] == 1
    assert await credit_db.get_all_counters() == {}


@pytest.mark.parametrize("failure", ["missing", "upload", "send"])
async def test_show_examples_error_still_offers_back(credit_db, example_photos, failure):
    event = callback_event("show_examples")
    if failure == "missing":
        for name in handlers.START_EXAMPLE_FILES:
            (example_photos / name).unlink()
    elif failure == "upload":
        event.bot.upload_media.side_effect = RuntimeError("upload error")
    else:
        event.send.side_effect = [RuntimeError("album rejected"), object()]
    await dispatch_callback(create_router(), event, await ready_context())
    reply = event.send.await_args
    assert "временно недоступны" in reply.args[0]
    assert reply.kwargs["attachments"][0].payload.buttons[0][0].payload == "back_to_main"
    assert await credit_db.get_all_counters() == {}


@pytest.mark.parametrize("payload", ["show_examples", "show_help"])
async def test_utility_sections_handle_deleted_message(payload):
    event = callback_event(payload, deleted=True)
    await dispatch_callback(create_router(), event, await ready_context())
    event.ack.assert_awaited_once_with(notification="Меню недоступно. Отправьте /start.")
    event.send.assert_not_awaited()


@pytest.mark.parametrize("style,style_name", [
    ("photo_style_studio", "Студия"), ("photo_style_walk", "Прогулка"), ("photo_style_ny", "Новогодняя"),
])
@pytest.mark.parametrize("tariff", ["photo_tariff_590", "photo_tariff_990", "photo_tariff_1490"])
async def test_photo_style_tariff_preview_has_no_payment_or_generation(credit_db, example_photos, style, style_name, tariff):
    await credit_db.grant_free_trial(456)
    payments = SimpleNamespace(create_payment_link=AsyncMock())
    vision, card = AsyncMock(), AsyncMock()
    router = create_router(vision, postcard_generator=card, payments=payments)
    context = await ready_context()
    await context.set_state(PosterStates.waiting_for_card_text)
    await context.update_data(card_text="old", mode="mode_ny")
    category = callback_event("cat_photo")
    await dispatch_callback(router, category, context)
    assert category.send.await_args.kwargs["text"] == (
        "📸 Раздел «Фотосессия» уже совсем скоро заработает!\n\n"
        "Мы вовсю готовим для вас потрясающие образы. Но вы уже можете посмотреть стили и тарифы, "
        "которые будут доступны.\n\nВыбери стиль, который тебе интересен:"
    )
    buttons = category.send.await_args.kwargs["attachments"][0].payload.buttons
    assert [(row[0].text, row[0].payload) for row in buttons] == [
        ("🏙 Студия", "photo_style_studio"), ("🌳 Прогулка", "photo_style_walk"),
        ("🎄 Новогодняя", "photo_style_ny"), ("⬅️ Назад", "back_to_main"),
    ]
    event = callback_event(style)
    await dispatch_callback(router, event, context)
    event.ack.assert_awaited_once_with(notification="Показываю тарифы…")
    text = event.send.await_args.args[0]
    assert text == (
        f"Вы выбрали стиль: {style_name}.\n\nТеперь выберите тариф, который вам подходит:\n\n"
        "🔹 «Попробовать» — 590 ₽\n"
        "Вы получите 3 профессиональных фото в одном стиле. Отличный вариант, чтобы оценить качество!\n\n"
        "🔹 «Фотосессия» — 990 ₽\n"
        "Вы получите 5 фото в одном стиле. Полноценная мини-сессия для вашего портфолио или соцсетей.\n\n"
        "🔹 «Премиум» — 1490 ₽\n"
        "7 фото (5 в одном стиле и 2 в другом). Снимки в максимальном разрешении 4K — готовы к печати "
        "в большом формате! Вы сможете загрузить два разных фото для разных образов."
    )
    buttons = event.send.await_args.kwargs["attachments"][0].payload.buttons
    assert [(row[0].text, row[0].payload) for row in buttons] == [
        ("Тариф «Попробовать» (590 ₽)", "photo_tariff_590"),
        ("Тариф «Фотосессия» (990 ₽)", "photo_tariff_990"),
        ("Тариф «Премиум» (1490 ₽)", "photo_tariff_1490"), ("⬅️ Назад", "cat_photo"),
    ]
    assert await context.get_data() == {"photo_style": style}
    assert await context.get_state() == PosterStates.waiting_for_mode
    selected_tariff = callback_event(tariff)
    await dispatch_callback(router, selected_tariff, context)
    ny_trial = style == "photo_style_ny" and tariff == "photo_tariff_590"
    selected_tariff.ack.assert_awaited_once_with(notification="Показываю ссылку…" if ny_trial else "Скоро будет доступно…")
    if ny_trial:
        assert selected_tariff.send.await_args.args[0] == handlers.PHOTO_TRIAL_CHECKOUT_TEXT
        assert (await context.get_data())["tariff"] == "trial_590"
    else:
        assert selected_tariff.send.await_args.args[0] == (
            "🚀 Отличный выбор! Этот тариф скоро будет доступен. Мы уже заканчиваем настройку фотосессий, "
            "следите за обновлениями!"
        )
    back = selected_tariff.send.await_args.kwargs["attachments"][0].payload.buttons[-1][0]
    assert (back.text, back.payload) == ("⬅️ Назад", "photo_tariff_back" if ny_trial else "cat_photo")
    await dispatch_callback(router, callback_event(back.payload), context)
    assert await context.get_data() == ({"photo_style": style} if ny_trial else {})
    assert await context.get_state() == PosterStates.waiting_for_mode
    style_event = {"photo_style_studio": "click_photo_studio", "photo_style_walk": "click_photo_walk", "photo_style_ny": "click_photo_ny"}[style]
    tariff_event = {"photo_tariff_590": "click_tariff_590", "photo_tariff_990": "click_tariff_990", "photo_tariff_1490": "click_tariff_1490"}[tariff]
    assert await credit_db.get_all_counters() == {"click_photo": 1 if ny_trial else 2, style_event: 1, tariff_event: 1}
    assert (await credit_db.get_user(456))["balance"] == 1
    assert (await credit_db.get_user(456))["total_generations"] == 0
    assert (await credit_db.get_user(456))["total_purchases"] == 0
    payments.create_payment_link.assert_not_awaited()
    vision.assert_not_awaited()
    card.assert_not_awaited()


@pytest.mark.parametrize("payload", ["photo_style_studio", "photo_style_walk", "photo_style_ny",
                                     "photo_tariff_590", "photo_tariff_990", "photo_tariff_1490"])
async def test_photo_preview_handles_buttons_from_old_or_deleted_messages(credit_db, payload):
    router = create_router()
    context = await ready_context()
    await context.clear()  # Callback работает и после сброса сессии.
    event = callback_event(payload)
    await dispatch_callback(router, event, context)
    event.ack.assert_awaited_once()
    event.send.assert_awaited_once()
    deleted = callback_event(payload, deleted=True)
    before = await context.get_data()
    await dispatch_callback(router, deleted, context)
    deleted.ack.assert_awaited_once_with(notification="Меню недоступно. Отправьте /start.")
    deleted.send.assert_not_awaited()
    assert await context.get_data() == before
    assert await credit_db.get_all_counters() == {handlers.PHOTO_CLICK_EVENTS[payload]: 1}


PHOTO_CLICK_CASES = [
    ("photo_style_studio", "click_photo_studio", "Стиль: Студия"),
    ("photo_style_walk", "click_photo_walk", "Стиль: Прогулка"),
    ("photo_style_ny", "click_photo_ny", "Стиль: Новогодняя"),
    ("photo_tariff_590", "click_tariff_590", "Тариф «Попробовать» (590)"),
    ("photo_tariff_990", "click_tariff_990", "Тариф «Фотосессия» (990)"),
    ("photo_tariff_1490", "click_tariff_1490", "Тариф «Премиум» (1490)"),
]


async def test_photo_click_stats_are_separate_and_exclude_admin(credit_db):
    await credit_db.grant_free_trial(101)
    before = dict(await credit_db.get_user(101))
    router = create_router(admin_user_id=456)
    for count, (payload, _, _) in enumerate(PHOTO_CLICK_CASES, 1):
        event = callback_event(payload)
        event.get_ids = lambda: (123, 101)
        for _ in range(count):
            await dispatch_callback(router, event, await ready_context())
        await dispatch_callback(router, callback_event(payload), await ready_context())  # Тестовый клик администратора.
    all_counters = await credit_db.get_all_counters()
    filtered = await credit_db.get_all_counters(admin_id=456)
    for count, (_, event_name, _) in enumerate(PHOTO_CLICK_CASES, 1):
        assert all_counters[event_name] == count + 1
        assert filtered[event_name] == count
    assert filtered["gen_unclassified"] == 0
    assert dict(await credit_db.get_user(101)) == before
    event = message_event("/stats")
    await call_handler(router, "stats", event)
    text = event.message.answer.await_args.args[0]
    assert "📸 Интерес к Фотосессиям (клики):" in text
    assert text.index("- Аудиопесни (клики):") < text.index("📸 Интерес к Фотосессиям (клики):")
    for count, (_, _, label) in enumerate(PHOTO_CLICK_CASES, 1):
        assert f"- {label}: {count}\n" in text
    assert "🖼 Всего сгенерировано картинок: 0" in text
    assert "8 Марта" not in text


async def test_photo_click_stats_defaults_to_zero(credit_db):
    event = message_event("/stats")
    await call_handler(create_router(admin_user_id=456), "stats", event)
    text = event.message.answer.await_args.args[0]
    for _, _, label in PHOTO_CLICK_CASES:
        assert f"- {label}: 0\n" in text


@pytest.mark.parametrize("payload,event_name,label", PHOTO_CLICK_CASES)
async def test_photo_clicks_without_user_id_are_not_counted(credit_db, payload, event_name, label):
    event = callback_event(payload)
    event.get_ids = lambda: (123, None)
    await dispatch_callback(create_router(), event, await ready_context())
    event.send.assert_awaited_once()
    assert await credit_db.get_all_counters() == {}


NY_TRIAL_TEMPLATES = [
    ("👨‍👩‍👧 Семейная (3 чел)", "photo_temp_fam3", "fam3"),
    ("👨‍👩‍👧‍👦 Семейная (4+ чел)", "photo_temp_fam4", "fam4"),
    ("👶 Семейная с малышами", "photo_temp_toddlers", "toddlers"),
    ("👩‍👧 Мама + дочка", "photo_temp_mom_daughter", "mom_daughter"),
    ("🐰 С кроликом", "photo_temp_rabbit", "rabbit"),
]


@pytest.mark.parametrize("label,payload,template", NY_TRIAL_TEMPLATES[1:])
async def test_legacy_ny_template_buttons_use_real_checkout(credit_db, label, payload, template):
    await credit_db.grant_free_trial(456)
    payments = SimpleNamespace(create_payment_link=AsyncMock())
    generator = AsyncMock()
    router = create_router(generator, postcard_generator=generator, payments=payments)
    context = await ready_context()
    await dispatch_callback(router, callback_event("photo_style_ny"), context)
    tariff = callback_event("photo_tariff_590")
    await dispatch_callback(router, tariff, context)
    tariff.ack.assert_awaited_once_with(notification="Показываю ссылку…")
    assert tariff.send.await_args.args[0] == handlers.PHOTO_TRIAL_CHECKOUT_TEXT
    assert (await context.get_data())["tariff"] == "trial_590"
    event = callback_event(payload)
    await dispatch_callback(router, event, context)
    event.ack.assert_awaited_once()
    data = await context.get_data()
    assert data["photo_style"] == "photo_style_ny"
    assert data["photo_tariff"] == "photo_tariff_590"
    assert data["template"] == template
    assert data["photo_product_name"] == "Фотосессия Попробовать"
    assert data["photo_amount_kopecks"] == 59000
    assert event.send.await_args.args[0] == (
        "Вы выбрали тариф «Попробовать» (3 фото) за 590 ₽.\n\n"
        "Для продолжения, пожалуйста, оплатите заказ по ссылке ниже. После оплаты обязательно вернитесь в бота!"
    )
    buttons = event.send.await_args.kwargs["attachments"][0].model_dump()["payload"]["buttons"]
    pay = buttons[0][0]
    assert pay["type"] == "link"
    assert pay["text"] == "Оплатить 590 ₽"
    assert pay["url"] == "https://link.payform.ru/?paymentLinkId=72139edd-76b7-4dcf-b7e4-a34f6bd9c625"
    assert "payload" not in pay
    assert buttons[-1][0]["payload"] == "photo_tariff_back"
    # Повторный выбор сюжета обновляет FSM, без создания заказа пакета кредитов.
    await dispatch_callback(router, callback_event("photo_temp_rabbit"), context)
    assert (await context.get_data())["template"] == "rabbit"
    await dispatch_callback(router, callback_event("photo_tariff_back"), context)
    assert await context.get_data() == {"photo_style": "photo_style_ny"}
    assert await credit_db.get_all_counters() == {"click_photo_ny": 1, "click_tariff_590": 1}
    payments.create_payment_link.assert_not_awaited()
    generator.assert_not_awaited()
    assert (await credit_db.get_user(456))["balance"] == 1
    assert (await credit_db.get_user(456))["total_purchases"] == 0
    assert (await credit_db.get_user(456))["total_generations"] == 0


@pytest.mark.parametrize("style,tariff", [(None, None), ("photo_style_ny", None),
    ("photo_style_studio", "photo_tariff_590"), ("photo_style_ny", "photo_tariff_990")])
async def test_old_template_button_requires_ny_trial_context(credit_db, style, tariff):
    context = await ready_context()
    await context.clear()
    await context.update_data(photo_style=style, photo_tariff=tariff)
    event = callback_event("photo_temp_fam3")
    await dispatch_callback(create_router(), event, context)
    assert event.send.await_args.args[0] == (handlers.PHOTO_TARIFF_SOON_TEXT if style == "photo_style_studio" else "Выберите стиль и тариф фотосессии заново.")
    button = event.send.await_args.kwargs["attachments"][0].model_dump()["payload"]["buttons"][0][0]
    assert button["type"] == "callback" and button["payload"] == "cat_photo"
    assert "template" not in await context.get_data()
    assert await credit_db.get_all_counters() == {}


async def test_tariff_back_without_session_shows_styles(credit_db):
    context = await ready_context()
    await context.clear()
    event = callback_event("photo_tariff_back")
    await dispatch_callback(create_router(), event, context)
    assert event.send.await_args.kwargs["text"] == handlers.CATEGORY_MENUS["cat_photo"][1]
    assert await context.get_state() == PosterStates.waiting_for_mode
    assert await credit_db.get_all_counters() == {}


@pytest.mark.parametrize("payload", ["photo_tariff_back", *(p for _, p, _ in NY_TRIAL_TEMPLATES)])
async def test_ny_trial_deleted_message_does_not_change_context(credit_db, payload):
    context = await ready_context()
    before = await context.get_data()
    event = callback_event(payload, deleted=True)
    await dispatch_callback(create_router(), event, context)
    event.ack.assert_awaited_once()
    event.send.assert_not_awaited()
    assert await context.get_data() == before
    assert await credit_db.get_all_counters() == {}


async def test_ny_trial_tariff_immediately_offers_real_payment_link(credit_db):
    await credit_db.grant_free_trial(456)
    payments = SimpleNamespace(create_payment_link=AsyncMock())
    generator = AsyncMock()
    router = create_router(generator, postcard_generator=generator, payments=payments)
    context = await ready_context()
    await dispatch_callback(router, callback_event("photo_style_ny"), context)
    event = callback_event("photo_tariff_590")
    await dispatch_callback(router, event, context)
    event.ack.assert_awaited_once_with(notification="Показываю ссылку…")
    event.send.assert_awaited_once()
    assert event.send.await_args.args[0] == (
        "Вы выбрали тариф «Попробовать» (3 фото) за 590 ₽.\n\n"
        "Для продолжения, пожалуйста, оплатите заказ по ссылке ниже. После оплаты обязательно вернитесь в бота!"
    )
    data = await context.get_data()
    assert data["tariff"] == "trial_590"
    assert data["photo_tariff"] == "photo_tariff_590"
    assert data["photo_style"] == "photo_style_ny"
    assert await context.get_state() == PosterStates.waiting_for_mode
    button = event.send.await_args.kwargs["attachments"][0].model_dump()["payload"]["buttons"][0][0]
    assert button["text"] == "Оплатить 590 ₽"
    assert button["type"] == "link"
    assert button["url"] == "https://link.payform.ru/?paymentLinkId=72139edd-76b7-4dcf-b7e4-a34f6bd9c625"
    assert "payload" not in button
    assert (await credit_db.get_user(456))["balance"] == 1
    assert (await credit_db.get_user(456))["total_purchases"] == 0
    assert (await credit_db.get_user(456))["total_generations"] == 0
    payments.create_payment_link.assert_not_awaited()
    generator.assert_not_awaited()


async def family_context(with_photo=True):
    context = await ready_context()
    await context.clear()
    await context.update_data(photo_style="photo_style_ny", photo_tariff="photo_tariff_590", tariff="trial_590")
    if with_photo:
        await context.update_data(photo={"url": "https://photos.example.com/source.png"}, photosession_admin_bypass=True, photosession_session_id="test-session", photosession=True)
    await context.set_state(PosterStates.waiting_for_mode)
    return context


async def test_family_three_sequential_frames_use_same_reference_and_no_card_generation(credit_db):
    await credit_db.grant_free_trial(456)
    outputs = [f"https://cdn.example/frame-{i}.png" for i in range(3)]
    order = []
    async def generate(reference, prompt):
        index = len([v for v in order if v.startswith("generate")])
        order.append(f"generate-{index}")
        return outputs[index]
    frames = AsyncMock(side_effect=generate)
    cards, visions = AsyncMock(), AsyncMock()
    router = create_router(visions, postcard_generator=cards, photosession_generator=frames, admin_user_id=456)
    event = callback_event("photo_temp_fam3")
    async def send(**kwargs):
        order.append("send-" + str(len([v for v in order if v.startswith("send")])))
        return object()
    event.bot.send_message.side_effect = send
    context = await family_context()
    await dispatch_callback(router, event, context)
    assert order == ["generate-0", "send-0", "generate-1", "send-1", "generate-2", "send-2"]
    assert frames.await_args_list == [call("https://photos.example.com/source.png", p) for p in handlers.NY_FAMILY_THREE_PROMPTS]
    assert len(set(handlers.NY_FAMILY_THREE_PROMPTS)) == 3
    assert [c.kwargs["text"] for c in event.bot.send_message.await_args_list] == ["Фотография 1 из 3", "Фотография 2 из 3", "Фотография 3 из 3"]
    assert all(len(c.kwargs["attachments"]) == 1 for c in event.bot.send_message.await_args_list)
    assert [c.args[0] for c in event.send.await_args_list[:3]] == [f"Генерирую кадр {i} из 3..." for i in range(1, 4)]
    data = await context.get_data()
    assert data["photosession_urls"] == outputs and data["photosession_sent_count"] == 3
    assert data["photosession_complete"] is True
    await dispatch_callback(router, callback_event("photo_temp_fam3"), context)
    assert frames.await_count == 3
    cards.assert_not_awaited()
    visions.assert_not_awaited()
    assert (await credit_db.get_user(456))["balance"] == 1


async def test_family_three_requests_reference_then_generates_without_congratulation_text(credit_db):
    frames = AsyncMock(side_effect=[f"https://cdn.example/{i}.png" for i in range(3)])
    router = create_router(photosession_generator=frames, admin_user_id=456)
    context = await family_context(False)
    event = callback_event("photo_temp_fam3")
    await dispatch_callback(router, event, context)
    assert await context.get_state() == PosterStates.waiting_for_photosession_photo
    assert event.send.await_args.args[0] == "👑 Режим администратора: Оплата пропущена. Пожалуйста, загрузите общее фото семьи (3 человека) для генерации."
    frames.assert_not_awaited()
    await call_handler(router, "collect_photosession_photo", message_event("hello"), context)
    frames.assert_not_awaited()
    photo = photo_event()
    await call_handler(router, "collect_photosession_photo", photo, context)
    assert frames.await_count == 3
    assert all(c.args[0] == "https://photos.example.com/source.png" for c in frames.await_args_list)
    assert await context.get_state() == PosterStates.waiting_for_mode


@pytest.mark.parametrize("failure", ["generate", "send"])
async def test_family_three_resume_does_not_repeat_ready_frames(failure):
    frames = AsyncMock(side_effect=["https://cdn.example/1.png", RuntimeError("api error"), "https://cdn.example/2.png", "https://cdn.example/3.png"] if failure == "generate" else [f"https://cdn.example/{i}.png" for i in range(3)])
    router = create_router(photosession_generator=frames, admin_user_id=456)
    context = await family_context()
    event = callback_event("photo_temp_fam3")
    if failure == "send":
        event.bot.send_message.side_effect = [object(), RuntimeError("MAX error")]
    await dispatch_callback(router, event, context)
    assert (await context.get_data())["photosession_sent_count"] == 1
    retry = callback_event("photo_temp_fam3")
    await dispatch_callback(router, retry, context)
    assert retry.bot.send_message.await_count == 2
    assert frames.await_count == (4 if failure == "generate" else 3)
    assert (await context.get_data())["photosession_complete"] is True


@pytest.mark.parametrize("style,tariff", [("photo_style_studio", "photo_tariff_590"), ("photo_style_walk", "photo_tariff_590"), ("photo_style_ny", "photo_tariff_990")])
async def test_family_three_only_runs_for_new_year_trial(style, tariff):
    frames = AsyncMock()
    router = create_router(photosession_generator=frames, admin_user_id=456)
    context = await family_context()
    await context.update_data(photo_style=style, photo_tariff=tariff)
    event = callback_event("photo_temp_fam3")
    await dispatch_callback(router, event, context)
    frames.assert_not_awaited()
    event.bot.send_message.assert_not_awaited()


async def test_family_templates_reachable_from_payment_screen():
    router = create_router()
    context = await family_context(False)
    tariff = callback_event("photo_tariff_590")
    await dispatch_callback(router, tariff, context)
    buttons = tariff.send.await_args.kwargs["attachments"][0].payload.buttons
    choose = next(b for row in buttons for b in row if getattr(b, "payload", None) == "photo_choose_template")
    event = callback_event(choose.payload)
    await dispatch_callback(router, event, context)
    payloads = [r[0].payload for r in event.send.await_args.kwargs["attachments"][0].payload.buttons]
    assert payloads == [p for _, p, _ in NY_TRIAL_TEMPLATES] + ["photo_tariff_back"]


async def test_family_three_stops_if_navigation_clears_session():
    context = await family_context()
    async def generate(*args):
        await context.clear()
        await context.set_state(PosterStates.waiting_for_mode)
        return "https://cdn.example/result.png"
    frames = AsyncMock(side_effect=generate)
    event = callback_event("photo_temp_fam3")
    await dispatch_callback(create_router(photosession_generator=frames, admin_user_id=456), event, context)
    frames.assert_awaited_once()
    event.bot.send_message.assert_not_awaited()
    assert await context.get_data() == {}


async def test_family_three_duplicate_callback_cannot_start_second_batch():
    context = await family_context()
    duplicate = callback_event("photo_temp_fam3")
    calls = 0
    async def generate(*args):
        nonlocal calls
        calls += 1
        if calls == 1:
            await dispatch_callback(router, duplicate, context)
        return f"https://cdn.example/{calls}.png"
    frames = AsyncMock(side_effect=generate)
    router = create_router(photosession_generator=frames, admin_user_id=456)
    await dispatch_callback(router, callback_event("photo_temp_fam3"), context)
    assert frames.await_count == 3
    duplicate.ack.assert_awaited_once_with(notification="Фотографии уже создаются. Пожалуйста, подождите.")
    duplicate.bot.send_message.assert_not_awaited()


@pytest.mark.parametrize("tariff", ["photo_tariff_990", "photo_tariff_1490"])
async def test_family_three_wrong_tariff_does_not_run(tariff):
    frames = AsyncMock()
    context = await family_context()
    await context.update_data(photo_tariff=tariff)
    await dispatch_callback(create_router(photosession_generator=frames, admin_user_id=456), callback_event("photo_temp_fam3"), context)
    frames.assert_not_awaited()


async def test_admin_family_template_requests_fresh_photo_without_payment(credit_db):
    frames = AsyncMock(side_effect=[f"https://cdn.example/admin-{i}.png" for i in range(3)])
    payments = SimpleNamespace(create_payment_link=AsyncMock())
    router = create_router(admin_user_id=456, photosession_generator=frames, payments=payments)
    context = await ready_context()
    await context.update_data(photo_style="photo_style_ny", photo_tariff="photo_tariff_590")
    event = callback_event("photo_temp_fam3")
    await dispatch_callback(router, event, context)
    event.send.assert_awaited_once_with(
        "👑 Режим администратора: Оплата пропущена. Пожалуйста, загрузите общее фото семьи (3 человека) для генерации."
    )
    assert await context.get_state() == PosterStates.waiting_for_photosession_photo
    data = await context.get_data()
    assert data["template"] == "fam3" and data["photosession"] is True
    assert data["photosession_admin_bypass"] is True
    assert "photo" not in data
    frames.assert_not_awaited()
    await call_handler(router, "collect_photosession_photo", photo_event(), context)
    assert frames.await_count == 3
    assert all(c.args[0] == "https://photos.example.com/source.png" for c in frames.await_args_list)
    payments.create_payment_link.assert_not_awaited()


@pytest.mark.parametrize("admin_id", [None, 999])
async def test_nonadmin_family_template_only_receives_payment_link(credit_db, admin_id):
    frames = AsyncMock()
    router = create_router(admin_user_id=admin_id, photosession_generator=frames)
    context = await family_context()
    event = callback_event("photo_temp_fam3")
    await dispatch_callback(router, event, context)
    assert event.send.await_args.args[0] == handlers.PHOTO_TRIAL_CHECKOUT_TEXT
    button = event.send.await_args.kwargs["attachments"][0].payload.buttons[0][0]
    assert button.text == "Оплатить 590 ₽"
    assert button.url == handlers.PHOTO_TRIAL_PAYMENT_URL
    assert await context.get_state() == PosterStates.waiting_for_mode
    frames.assert_not_awaited()


async def test_nonadmin_cannot_generate_from_stale_admin_photo_state():
    frames = AsyncMock()
    context = await family_context()
    await context.update_data(template="fam3", photosession_admin_bypass=True)
    await context.set_state(PosterStates.waiting_for_photosession_photo)
    event = photo_event()
    await call_handler(create_router(admin_user_id=999, photosession_generator=frames), "collect_photosession_photo", event, context)
    frames.assert_not_awaited()
    assert event.message.answer.await_args.args[0] == handlers.PHOTO_TRIAL_CHECKOUT_TEXT
    assert await context.get_state() == PosterStates.waiting_for_mode

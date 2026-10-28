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


ROLE_SAMPLE_INFO = {'mode_teacher': 'Ирина Петровна, математика', 'mode_coach': 'Сергей Иванович, футбол'}


def role_generation_kwargs(mode):
    return {'theme_info': ROLE_SAMPLE_INFO[mode]} if mode in ROLE_SAMPLE_INFO else {}


async def provide_role_info(router, context, mode):
    event = message_event(ROLE_SAMPLE_INFO[mode])
    await call_handler(router, 'collect_card_role_info', event, context)
    return event


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


@pytest.fixture(autouse=True)
def isolated_example_directory(tmp_path, monkeypatch):
    # Результат тестов не зависит от того, добавлены ли реальные примеры в репозиторий.
    directory = tmp_path / "empty-examples"
    directory.mkdir()
    monkeypatch.setattr(handlers, "EXAMPLES_DIR", directory)


@pytest.fixture
def example_photos(tmp_path, monkeypatch):
    directory = tmp_path / "project" / "examples"
    directory.mkdir(parents=True)
    for filename in {"before.jpg", "after.jpg", *handlers.START_EXAMPLE_FILES, *handlers.BROADCAST_EXAMPLE_FILES, *handlers.PHOTO_FAMILY_EXAMPLE_FILES,
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
    if mode in ROLE_SAMPLE_INFO:
        data = await context.get_data()
        assert data['mode'] == mode and data['card_role_session']
        assert await context.get_state() == PosterStates.waiting_for_card_role_info
        assert event.send.await_args.args[0] == handlers.ROLE_CARD_QUESTIONS[mode]
    else:
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
        handlers.send_offline_conversion.assert_awaited_once_with(456, goal_id='bot_generation')
        postcard.assert_awaited_once_with("https://photos.example.com/source.png", mode, "Аня, будь счастлива!")
        assert (await credit_db.get_user(456))["balance"] == (balance if admin else balance - 1)
        assert await context.get_state() is None
        offers = [call for call in event.message.answer.await_args_list if call.args and call.args[0] == handlers.SONG_UPSELL_TEXT]
        assert len(offers) == 1
        video_offers = [call for call in event.message.answer.await_args_list if call.args and call.args[0] == handlers.VIDEO_UPSELL_TEXT]
        assert len(video_offers) == 1
        video_buttons = video_offers[0].kwargs['attachments'][0].payload.buttons
        assert [row[0].payload for row in video_buttons[:2]] == ['upsell_animate', 'upsell_video']
        assert video_buttons[2][0].text == '⬇️ Нет, просто скачать картинку'
        assert video_buttons[2][0].url == 'https://cdn.example/card.png'
        assert await credit_db.check_song_promo(456)
        buttons = offers[0].kwargs['attachments'][0].payload.buttons
        assert [row[0].payload for row in buttons] == ['upsell_song_discount', 'dismiss_upsell']
    else:
        assert not any(call.args and call.args[0] == handlers.VIDEO_UPSELL_TEXT for call in event.message.answer.await_args_list)
        assert not await credit_db.check_song_promo(456)
        handlers.send_offline_conversion.assert_not_awaited()
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
    assert handlers.send_offline_conversion.await_count == 1
    assert (await credit_db.get_user(456))["balance"] == 2
    await dispatch_callback(router, callback_event("more"), context)
    assert postcard.await_count == 1
    await call_handler(router, "collect_photo", photo_event(), context)
    await call_handler(router, "collect_card_text", message_event("Оля, поздравляю!"), context)
    assert postcard.await_count == 2
    assert handlers.send_offline_conversion.await_count == 2
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
    if mode in ROLE_SAMPLE_INFO:
        await provide_role_info(router, context, mode)
    if mode == "mode_vision":
        event = message_event("Моя мечта")
        await call_handler(router, "collect_sphere", event, context)
    else:
        await call_handler(router, "collect_photo", photo_event(), context)
        assert card.await_count == 0
        event = message_event("Оля, счастья и добра!")
        await call_handler(router, "collect_card_text", event, context)
        card.assert_awaited_once_with("https://photos.example.com/source.png", mode, "Оля, счастья и добра!", **role_generation_kwargs(mode))
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
        if mode in ROLE_SAMPLE_INFO:
            assert await context.get_state() == PosterStates.waiting_for_card_role_info
            await dp.handle(MessageCreated(timestamp=2, bot=bot, message=message(text=ROLE_SAMPLE_INFO[mode])))
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
        generator.assert_awaited_once_with("https://cdn.example/source.png", mode, fixed_text, **role_generation_kwargs(mode))
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
    if mode in ROLE_SAMPLE_INFO:
        assert await context.get_state() == PosterStates.waiting_for_card_role_info
        await provide_role_info(router, context, mode)
    else:
        assert await context.get_data() == {"mode": mode}
    assert await context.get_state() == PosterStates.waiting_for_photo
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
        card.assert_awaited_once_with("https://cdn.example/new-reference.png", mode, "Счастья, Аня!", **role_generation_kwargs(mode))
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
    assert len(modes) == 14 and len(set(modes)) == 14
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
    first_text = (await provide_role_info(router, context, mode)).message.answer.await_args.args[0] if mode in ROLE_SAMPLE_INFO else event.send.await_args.args[0]
    assert handlers.PHOTO_GUIDANCE in first_text
    assert await context.get_state() == PosterStates.waiting_for_photo
    more = callback_event("more")
    await dispatch_callback(router, more, context)
    more_text = (await provide_role_info(router, context, mode)).message.answer.await_args.args[0] if mode in ROLE_SAMPLE_INFO else more.send.await_args.args[0]
    assert handlers.PHOTO_GUIDANCE in more_text
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
    assert "🛠 Тестовая активность (Админ):\n- Сгенерировано: 7\n- Покупок: 1" in text


async def test_admin_stats_without_existing_admin_record_shows_zero_test_activity(credit_db):
    event = message_event("/stats")
    await call_handler(create_router(admin_user_id=456), "stats", event)
    text = event.message.answer.await_args.args[0]
    assert "Всего запускали: 0" in text
    assert "🛠 Тестовая активность (Админ):\n- Сгенерировано: 0\n- Покупок: 0" in text


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
    "cat_cards": ([28, 22, 25, 29, 27, 21], ["sub_ny", "sub_bday", "sub_halloween", "sub_goodmorning", "mode_teacher", "mode_coach", "back_to_main"]),
    "sub_ny": ([28, 22, 23, 24, 25], ["mode_ny", "mode_ny_family", "mode_ny_cozy", "mode_ny_fairytale", "mode_ny_elegant", "back_to_cards"]),
    "sub_bday": ([29, 27, 21], ["mode_bday_young", "mode_bday_old", "mode_bday_old_elegant", "mode_modern_card", "back_to_cards"]),
    "sub_halloween": ([26], ["mode_halloween", "back_to_cards"]),
    "sub_goodmorning": ([20], ["mode_goodmorning", "back_to_cards"]),
    "cat_photo": (["photo_main_1.webp", "photo_main_2.webp", "photo_main_3.webp"], ["photo_style_ny", "photo_style_studio", "photo_style_walk", "back_to_main"]),
    "cat_meditation": ([], ["back_to_main"]),
    "cat_audio": ([], ["audio_tariff_fun", "audio_tariff_gift", "back_to_main"]),
}


@pytest.mark.parametrize("screen", list(CATEGORY_EXPECTATIONS))
async def test_category_screens_ordered_album_and_buttons(credit_db, example_photos, screen):
    context = await ready_context()
    event = callback_event(screen)
    await dispatch_callback(create_router(), event, context)
    numbers, payloads = CATEGORY_EXPECTATIONS[screen]
    event.ack.assert_awaited_once_with(notification="Открываю…")
    assert [Path(c.args[0].path).name for c in event.bot.upload_media.await_args_list] == [f"i ({n}).webp" if isinstance(n, int) else n for n in numbers]
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
    if mode in ROLE_SAMPLE_INFO:
        await provide_role_info(router, context, mode)
    assert await context.get_state() == PosterStates.waiting_for_photo
    assert (await context.get_data())["mode"] == mode
    await call_handler(router, "collect_photo", photo_event(), context)
    assert await context.get_state() == (PosterStates.waiting_for_prompt if mode == "mode_vision" else PosterStates.waiting_for_card_text)
    vision.assert_not_awaited()
    card.assert_not_awaited()
    if mode == "mode_vision":
        await call_handler(router, "collect_sphere", message_event("Моя мечта"), context)
        vision.assert_awaited_once_with("https://photos.example.com/source.png", "Моя мечта")
    else:
        await call_handler(router, "collect_card_text", message_event("Счастья, Аня!"), context)
        card.assert_awaited_once_with("https://photos.example.com/source.png", mode, "Счастья, Аня!", **role_generation_kwargs(mode))
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
    assert counters == {"gen_vision": 1, "click_photo": 3, "click_audio": 2, "click_meditation": 1}
    assert dict(await credit_db.get_user(456)) == before
    assert await credit_db.get_global_stats() == before_stats


async def test_stats_interest_counts_per_user_and_excludes_admin(credit_db, example_photos):
    router = create_router(admin_user_id=456)
    for uid, payload in [(101, "cat_photo"), (101, "cat_photo"), (102, "cat_audio"),
                         (456, "cat_photo"), (456, "cat_audio"),
                         (101, "cat_meditation"), (101, "cat_meditation"), (456, "cat_meditation")]:
        event = callback_event(payload)
        event.get_ids = lambda uid=uid: (123, uid)
        await dispatch_callback(router, event, await ready_context())
    assert await credit_db.get_all_counters() == {"click_photo": 3, "click_audio": 2, "click_meditation": 3}
    filtered = await credit_db.get_all_counters(admin_id=456)
    assert filtered["click_photo"] == 2 and filtered["click_audio"] == 1
    assert filtered["click_meditation"] == 2
    assert filtered["gen_unclassified"] == 0
    event = message_event("/stats")
    await call_handler(router, "stats", event)
    text = event.message.answer.await_args.args[0]
    assert "🖱 Интерес к новым разделам:\n- Фотосессия (клики): 2\n- Аудиопесни (клики): 1" in text
    assert "- Медитации (клики): 2" in text
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
    assert await context.get_state() == (PosterStates.waiting_for_card_role_info if mode in ROLE_SAMPLE_INFO else PosterStates.waiting_for_photo)
    expected = {"mode_bday": "mode_bday_young", "create_poster": "mode_vision"}.get(mode, mode)
    data = await context.get_data()
    if mode in ROLE_SAMPLE_INFO:
        assert data.pop('card_role_session')
        assert event.send.await_args.args[0] == handlers.ROLE_CARD_QUESTIONS[mode]
    else:
        assert "фото" in event.send.await_args.args[0]
    assert data == {"mode": expected}
    assert (await credit_db.get_user(456))["balance"] == 1
    assert (await credit_db.get_user(456))["total_generations"] == 0
    assert await credit_db.get_all_counters() == {}
    vision.assert_not_awaited()
    card.assert_not_awaited()


@pytest.mark.parametrize("mode", [payload for _, payload in handlers.MODE_BUTTONS])
async def test_generation_events_follow_existing_modes_only_after_success(credit_db, mode):
    await credit_db.add_credits(456, 2)
    async def generate(*args, **kwargs):
        assert await credit_db.get_all_counters() == {}
        assert (await credit_db.get_user(456))["total_generations"] == 0
        return ["https://cdn.example/counted.png"]
    generator = AsyncMock(side_effect=generate)
    router = create_router(generator, postcard_generator=generator)
    context = await ready_context()
    await context.update_data(mode=mode)
    if mode in ROLE_SAMPLE_INFO:
        await provide_role_info(router, context, mode)
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
        "mode_modern_card": "gen_scrapbook", "mode_teacher": "gen_teacher", "mode_coach": "gen_coach",
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
        "📸 Ура! Новогодняя фотосессия уже доступна! 🎄\n\n"
        "Мы вовсю готовим для вас потрясающие образы, и вы уже можете попробовать наши новинки.\n\n"
        "В тарифе «Попробовать» доступны три варианта семейных фотосессий:\n"
        "• Семейная 3+ чел\n"
        "• Семейная 4+ чел\n"
        "• Семейная с малышами\n\n"
        "👇 Выбери стиль, который тебе интересен:"
    )
    buttons = category.send.await_args.kwargs["attachments"][0].payload.buttons
    assert [(row[0].text, row[0].payload) for row in buttons] == [
        ("🎄 Новогодняя", "photo_style_ny"), ("🏙 Студия", "photo_style_studio"),
        ("🌳 Прогулка", "photo_style_walk"), ("⬅️ Назад", "back_to_main"),
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
        assert selected_tariff.send.await_args_list[0].args[0] == handlers.PHOTO_TRIAL_CHECKOUT_TEXT
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
    ("👨‍👩‍👧‍👦 Семейная 3+ чел", "photo_temp_fam4", "fam4"),
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
    assert tariff.send.await_args_list[0].args[0] == handlers.PHOTO_TRIAL_CHECKOUT_TEXT
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
    expected_counters = {"click_photo_ny": 1, "click_tariff_590": 1}
    if template in {"fam3", "fam4", "toddlers"}:
        expected_counters["select_photo_" + template] = 1
    assert await credit_db.get_all_counters() == expected_counters
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
    assert event.send.await_args_list[0].args[0] == (
        "Вы выбрали тариф «Попробовать» (3 фото) за 590 ₽.\n\n"
        "Для продолжения, пожалуйста, оплатите заказ по ссылке ниже. После оплаты обязательно вернитесь в бота!"
    )
    data = await context.get_data()
    assert data["tariff"] == "trial_590"
    assert data["photo_tariff"] == "photo_tariff_590"
    assert data["photo_style"] == "photo_style_ny"
    assert await context.get_state() == PosterStates.waiting_for_mode
    button = event.send.await_args_list[0].kwargs["attachments"][0].model_dump()["payload"]["buttons"][0][0]
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
        await context.update_data(photo={"url": "https://photos.example.com/source.png"}, photosession_admin_bypass=True, photosession_session_id="test-session", photosession=True, template="fam3")
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
    assert [c.args[0] for c in event.send.await_args_list[:3]] == [f"Генерирую кадр {i} из 3... ⏳ Это может занять 3-5 минут. Пожалуйста, не закрывайте чат!" for i in range(1, 4)]
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
    buttons = tariff.send.await_args_list[0].kwargs["attachments"][0].payload.buttons
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


async def test_family_plus_admin_uses_its_three_prompts_and_original_reference(credit_db):
    await credit_db.grant_free_trial(456)
    frames = AsyncMock(side_effect=[f"https://cdn.example/plus-{i}.png" for i in range(3)])
    router = create_router(admin_user_id=456, photosession_generator=frames)
    context = await family_context(False)
    event = callback_event("photo_temp_fam4")
    await dispatch_callback(router, event, context)
    assert "3 и более человек" in event.send.await_args.args[0]
    assert (await context.get_data())["template"] == "fam4"
    assert await context.get_state() == PosterStates.waiting_for_photosession_photo
    await call_handler(router, "collect_photosession_photo", photo_event(), context)
    assert frames.await_args_list == [
        call("https://photos.example.com/source.png", prompt)
        for prompt in handlers.NY_FAMILY_PHOTOSESSION_PROMPTS["fam4"]
    ]
    assert (await context.get_data())["photosession_sent_count"] == 3
    assert (await credit_db.get_user(456))["balance"] == 1


async def test_switching_family_template_does_not_reuse_previous_family_frames():
    frames = AsyncMock(side_effect=[f"https://cdn.example/plus-{i}.png" for i in range(3)])
    router = create_router(admin_user_id=456, photosession_generator=frames)
    context = await family_context()
    await context.update_data(template="fam3", photosession_complete=True,
                              photosession_urls=["https://cdn.example/old.png"] * 3, photosession_sent_count=3)
    event = callback_event("photo_temp_fam4")
    await dispatch_callback(router, event, context)
    data = await context.get_data()
    assert data["template"] == "fam4" and "photosession_urls" not in data and "photo" not in data
    assert await context.get_state() == PosterStates.waiting_for_photosession_photo
    frames.assert_not_awaited()


async def test_family_plus_retry_keeps_template_prompts():
    frames = AsyncMock(side_effect=["https://cdn.example/1.png", RuntimeError("api error"),
                                    "https://cdn.example/2.png", "https://cdn.example/3.png"])
    router = create_router(admin_user_id=456, photosession_generator=frames)
    context = await family_context()
    await context.update_data(template="fam4")
    event = callback_event("photo_temp_fam4")
    await dispatch_callback(router, event, context)
    button = event.send.await_args.kwargs["attachments"][0].payload.buttons[0][0]
    assert button.payload == "photo_temp_fam4"
    retry = callback_event(button.payload)
    await dispatch_callback(router, retry, context)
    prompts = handlers.NY_FAMILY_PHOTOSESSION_PROMPTS["fam4"]
    assert [c.args[1] for c in frames.await_args_list] == [prompts[0], prompts[1], prompts[1], prompts[2]]
    assert (await context.get_data())["photosession_complete"] is True


@pytest.mark.parametrize("style,tariff", [("photo_style_walk", "photo_tariff_590"), ("photo_style_ny", "photo_tariff_990")])
async def test_family_plus_does_not_run_for_other_style_or_tariff(style, tariff):
    frames = AsyncMock()
    context = await family_context()
    await context.update_data(photo_style=style, photo_tariff=tariff)
    await dispatch_callback(create_router(admin_user_id=456, photosession_generator=frames), callback_event("photo_temp_fam4"), context)
    frames.assert_not_awaited()


async def test_toddlers_three_frames_use_same_reference_and_correct_prompts(credit_db):
    await credit_db.grant_free_trial(456)
    outputs = [f"https://cdn.example/toddlers-{i}.png" for i in range(3)]
    events = []
    async def generate(reference, prompt):
        index = sum(e.startswith("generate") for e in events)
        events.append(f"generate-{index}")
        return outputs[index]
    frames = AsyncMock(side_effect=generate)
    router = create_router(admin_user_id=456, photosession_generator=frames)
    context = await family_context(False)
    selected = callback_event("photo_temp_toddlers")
    await dispatch_callback(router, selected, context)
    selected.ack.assert_awaited_once()
    assert "семьи с малышом или малышами" in selected.send.await_args.args[0]
    assert await context.get_state() == PosterStates.waiting_for_photosession_photo
    assert (await context.get_data())["template"] == "toddlers"
    frames.assert_not_awaited()
    photo = photo_event()
    async def send(**kwargs):
        index = sum(e.startswith("send") for e in events)
        events.append(f"send-{index}")
        assert kwargs["text"] == f"Фотография {index + 1} из 3"
        assert len(kwargs["attachments"]) == 1
        return object()
    photo.bot.send_message.side_effect = send
    await call_handler(router, "collect_photosession_photo", photo, context)
    assert events == ["generate-0", "send-0", "generate-1", "send-1", "generate-2", "send-2"]
    assert frames.await_args_list == [call("https://photos.example.com/source.png", p)
                                     for p in handlers.NY_FAMILY_PHOTOSESSION_PROMPTS["toddlers"]]
    progress = [c.args[0] for c in photo.message.answer.await_args_list if c.args and c.args[0].startswith("Генерирую кадр")]
    assert progress == [f"Генерирую кадр {i} из 3... ⏳ Это может занять 3-5 минут. Пожалуйста, не закрывайте чат!" for i in range(1,4)]
    assert (await context.get_data())["photosession_urls"] == outputs
    assert (await context.get_data())["photosession_complete"] is True
    assert (await credit_db.get_user(456))["balance"] == 1


@pytest.mark.parametrize("failure", ["generate", "send"])
async def test_toddlers_resume_preserves_saved_frames(failure):
    frames = AsyncMock(side_effect=["https://cdn.example/1.png", RuntimeError("api error"),
                                   "https://cdn.example/2.png", "https://cdn.example/3.png"] if failure == "generate"
                      else [f"https://cdn.example/{i}.png" for i in range(3)])
    router = create_router(admin_user_id=456, photosession_generator=frames)
    context = await family_context()
    await context.update_data(template="toddlers")
    event = callback_event("photo_temp_toddlers")
    if failure == "send":
        event.bot.send_message.side_effect = [object(), RuntimeError("MAX error")]
    await dispatch_callback(router, event, context)
    retry_button = event.send.await_args.kwargs["attachments"][0].payload.buttons[0][0]
    assert retry_button.payload == "photo_temp_toddlers"
    retry = callback_event(retry_button.payload)
    await dispatch_callback(router, retry, context)
    assert frames.await_count == (4 if failure == "generate" else 3)
    assert retry.bot.send_message.await_count == 2
    assert (await context.get_data())["photosession_complete"] is True


@pytest.mark.parametrize("admin_id,style,tariff", [
    (None, "photo_style_ny", "photo_tariff_590"), (999, "photo_style_ny", "photo_tariff_590"),
    (456, "photo_style_walk", "photo_tariff_590"), (456, "photo_style_ny", "photo_tariff_990"),
])
async def test_toddlers_keeps_existing_access_and_style_restrictions(admin_id, style, tariff):
    frames = AsyncMock()
    context = await family_context()
    await context.update_data(photo_style=style, photo_tariff=tariff)
    event = callback_event("photo_temp_toddlers")
    await dispatch_callback(create_router(admin_user_id=admin_id, photosession_generator=frames), event, context)
    frames.assert_not_awaited()
    if admin_id != 456:
        assert event.send.await_args.args[0] == handlers.PHOTO_TRIAL_CHECKOUT_TEXT


@pytest.mark.parametrize("old_template", ["fam3", "fam4"])
async def test_switch_to_toddlers_requires_new_reference(old_template):
    context = await family_context()
    await context.update_data(template=old_template, photosession_complete=True,
                              photosession_urls=["https://cdn.example/old.png"] * 3)
    frames = AsyncMock()
    event = callback_event("photo_temp_toddlers")
    await dispatch_callback(create_router(admin_user_id=456, photosession_generator=frames), event, context)
    data = await context.get_data()
    assert data["template"] == "toddlers" and "photosession_urls" not in data and "photo" not in data
    assert await context.get_state() == PosterStates.waiting_for_photosession_photo
    frames.assert_not_awaited()


@pytest.mark.parametrize("payload,filenames", [
    ("cat_photo", ["photo_main_1.webp", "photo_main_2.webp", "photo_main_3.webp"]),
    ("photo_style_ny", ["photo_fam_1.webp", "photo_fam_2.webp", "photo_fam_3.webp"]),
])
async def test_photosession_album_precedes_text_and_buttons(credit_db, example_photos, payload, filenames):
    event = callback_event(payload)
    context = await ready_context()
    await dispatch_callback(create_router(), event, context)
    replies = event.send.await_args_list
    assert len(replies) == 2
    assert replies[0].kwargs["text"] == "Примеры работ:"
    assert len(replies[0].kwargs["attachments"]) == 3
    assert len(replies[1].kwargs["attachments"]) == 1
    assert [Path(c.args[0].path).name for c in event.bot.upload_media.await_args_list] == filenames
    event.ack.assert_awaited_once()


@pytest.mark.parametrize("payload", ["photo_tariff_590", "photo_choose_template"])
async def test_family_album_between_text_and_template_buttons(credit_db, example_photos, payload):
    context = await family_context(False)
    event = callback_event(payload)
    await dispatch_callback(create_router(), event, context)
    replies = event.send.await_args_list
    assert len(replies) == 3
    assert "attachments" not in replies[0].kwargs or replies[0].args[0] == handlers.PHOTO_TRIAL_CHECKOUT_TEXT
    assert replies[1].kwargs["text"] == "Примеры работ:"
    assert len(replies[1].kwargs["attachments"]) == 3
    buttons = replies[2].kwargs["attachments"][0].payload.buttons
    assert [row[0].payload for row in buttons] == [p for _, p, _ in NY_TRIAL_TEMPLATES] + ["photo_tariff_back"]
    assert [Path(c.args[0].path).name for c in event.bot.upload_media.await_args_list] == ["photo_fam_1.webp", "photo_fam_2.webp", "photo_fam_3.webp"]
    assert (await context.get_data())["photo_style"] == "photo_style_ny"


@pytest.mark.parametrize("payload", ["photo_style_studio", "photo_style_walk"])
async def test_other_photo_styles_do_not_send_ny_album(payload):
    event = callback_event(payload)
    await dispatch_callback(create_router(), event, await ready_context())
    event.bot.upload_media.assert_not_awaited()


@pytest.mark.parametrize("failure", ["missing", "upload", "send"])
async def test_photo_album_failure_keeps_navigation(credit_db, example_photos, failure):
    event = callback_event("cat_photo")
    if failure == "missing":
        for filename in handlers.PHOTO_MAIN_EXAMPLE_FILES:
            (example_photos / filename).unlink()
    elif failure == "upload":
        event.bot.upload_media.side_effect = RuntimeError("upload failed")
    else:
        event.send.side_effect = [RuntimeError("send failed"), object()]
    await dispatch_callback(create_router(), event, await ready_context())
    assert event.send.await_args.kwargs["text"] == handlers.CATEGORY_MENUS["cat_photo"][1]
    assert event.send.await_args.kwargs["attachments"][0].payload.buttons[0][0].payload == "photo_style_ny"


@pytest.mark.parametrize('entry', ['start', 'bot_started'])
@pytest.mark.parametrize('payload', ['yandex_newyear', 'campaign_yandex_newyear_2027'])
async def test_campaign_entry_preserves_first_source(credit_db, entry, payload):
    router = create_router()
    context = await ready_context()
    event = message_event('/start ' + payload) if entry == 'start' else bot_started_event(payload)
    await call_handler(router, entry, event, context)
    original = dict(await credit_db.get_user(456))
    assert original['first_start_source'] == 'yandex_newyear'
    await call_handler(router, 'start', message_event('/start vk_ads'), context)
    assert dict(await credit_db.get_user(456)) == original
    assert await context.get_state() == PosterStates.waiting_for_mode


async def test_stats_campaign_block_is_last_and_excludes_admin(credit_db):
    for user_id in (456, 789):
        await credit_db.record_first_start(user_id, 'yandex_newyear')
        await credit_db.increment_generations(user_id)
        await credit_db.create_payment_order(str(user_id), user_id, 1, 9900, 'test')
        await credit_db.add_credits(user_id, 1, payment_order_id=str(user_id), provider_payment_id=str(user_id))
    event = message_event('/stats')
    event.get_ids = lambda: (123, 789)
    await call_handler(create_router(admin_user_id=789), 'stats', event)
    text = event.message.answer.await_args.args[0]
    assert text.endswith(
        '📍 Статистика по кампании «Yandex New Year»:\n'
        '👥 Новые пользователи: 1\n🖼 Сгенерировали: 1\n💳 Покупатели: 1\n💰 Оплат: 99,00 ₽'
    )


@pytest.mark.parametrize('tariff,counter', list(handlers.AUDIO_CLICK_EVENTS.items()))
async def test_audio_tariffs_show_fun_or_gift_screen_and_return_to_workshop(credit_db, tariff, counter):
    await credit_db.grant_free_trial(456)
    before = dict(await credit_db.get_user(456))
    generator = AsyncMock()
    payments = Mock()
    router = create_router(generator, payments=payments)
    context = await ready_context()
    await context.update_data(photo={'url': 'https://example.com/old.png'}, card_text='old')
    event = callback_event(tariff)
    await dispatch_callback(router, event, context)
    event.ack.assert_awaited_once()
    if tariff == 'audio_tariff_fun':
        assert event.send.await_args.args[0] == handlers.AUDIO_FUN_MENU_TEXT
        buttons = event.send.await_args.kwargs['attachments'][0].payload.buttons
        assert [row[0].payload for row in buttons] == [
            'audio_fun_boss', 'audio_fun_friend', 'audio_fun_cat', 'audio_fun_student',
            'audio_fun_universal', 'audio_fun_meme', 'audio_fun_ny', 'cat_audio',
        ]
    else:
        assert event.send.await_args.args[0] == handlers.AUDIO_GIFT_TEXT
        buttons = event.send.await_args.kwargs['attachments'][0].payload.buttons
        assert [(row[0].text, row[0].payload) for row in buttons] == [
            ('Выбрать сценарий', 'gift_choose_scenario'), ('⬅️ Назад', 'cat_audio'),
        ]
    back = event.send.await_args.kwargs['attachments'][0].payload.buttons[-1][0]
    assert (back.text, back.payload) == ('⬅️ Назад', 'cat_audio')
    assert await context.get_state() == PosterStates.waiting_for_mode
    assert await context.get_data() == {}
    assert await credit_db.get_all_counters() == {counter: 1}
    assert dict(await credit_db.get_user(456)) == before
    generator.assert_not_awaited()
    assert payments.mock_calls == []
    workshop = callback_event(back.payload)
    await dispatch_callback(router, workshop, context)
    workshop.ack.assert_awaited_once()
    assert workshop.send.await_args.kwargs['text'] == handlers.CATEGORY_MENUS['cat_audio'][1]
    buttons = workshop.send.await_args.kwargs['attachments'][0].payload.buttons
    assert [(row[0].text, row[0].payload) for row in buttons] == [
        ('🔥 Песня по фану (199 ₽)', 'audio_tariff_fun'),
        ('🎁 Песня в подарок (490 ₽)', 'audio_tariff_gift'),
        ('⬅️ Назад', 'back_to_main'),
    ]
    assert await credit_db.get_all_counters() == {counter: 1, 'click_audio': 1}


@pytest.mark.parametrize('tariff', list(handlers.AUDIO_CLICK_EVENTS))
async def test_audio_tariff_deleted_message_is_acknowledged_without_counting(credit_db, tariff):
    event = callback_event(tariff, deleted=True)
    await dispatch_callback(create_router(), event, await ready_context())
    event.ack.assert_awaited_once()
    event.send.assert_not_awaited()
    assert await credit_db.get_all_counters() == {}


async def test_audio_stats_count_each_click_and_exclude_admin(credit_db):
    router = create_router(admin_user_id=456)
    for user_id in (101, 456):
        for payload in ('cat_audio', 'audio_tariff_fun', 'audio_tariff_fun', 'audio_tariff_gift'):
            event = callback_event(payload)
            event.get_ids = lambda user_id=user_id: (123, user_id)
            await dispatch_callback(router, event, await ready_context())
    assert await credit_db.get_all_counters() == {
        'click_audio': 2, 'click_audio_fun': 4, 'click_audio_gift': 2,
    }
    stats = message_event('/stats')
    await call_handler(router, 'stats', stats)
    text = stats.message.answer.await_args.args[0]
    assert ('- Аудиопесни (клики): 1\n- Песня по фану (199 ₽): 2\n'
            '- Песня в подарок (490 ₽): 1') in text
    assert (await credit_db.get_global_stats())['total_generated'] == 0


async def test_audio_stats_default_to_zero(credit_db):
    event = message_event('/stats')
    await call_handler(create_router(admin_user_id=456), 'stats', event)
    text = event.message.answer.await_args.args[0]
    assert '- Песня по фану (199 ₽): 0\n- Песня в подарок (490 ₽): 0' in text


@pytest.mark.parametrize('payload', list(handlers.AUDIO_FUN_SCENARIOS))
async def test_song_scenario_collects_details_without_generation_or_payment(credit_db, payload):
    await credit_db.grant_free_trial(456)
    before = dict(await credit_db.get_user(456))
    generator, card, payments = AsyncMock(), AsyncMock(), Mock()
    router = create_router(generator, postcard_generator=card, payments=payments, admin_user_id=456)
    context = await ready_context()
    await context.update_data(photo={'url': 'https://example.com/old.png'}, card_text='old')
    selected = callback_event(payload)
    await dispatch_callback(router, selected, context)
    selected.ack.assert_awaited_once()
    assert selected.send.await_args.args[0] == handlers.AUDIO_FUN_SCENARIOS[payload][1]
    scenario = payload.removeprefix('audio_fun_')
    assert await context.get_data() == {'song_scenario': scenario}
    assert await context.get_state() == PosterStates.waiting_for_song_details
    answer = message_event('  Аня, любим вино и сериалы  ')
    # Общий обработчик тоже должен маршрутизировать такое состояние.
    await call_handler(router, 'echo', answer, context)
    assert answer.message.answer.await_args.args[0] == handlers.AUDIO_FUN_PAYMENT_TEXT
    assert await context.get_state() == PosterStates.waiting_for_audio_fun_payment
    assert await context.get_data() == {'song_scenario': scenario, 'song_details': 'Аня, любим вино и сериалы'}
    assert await credit_db.get_all_counters() == {handlers.AUDIO_FUN_COUNTERS[payload]: 1}
    assert dict(await credit_db.get_user(456)) == before
    generator.assert_not_awaited()
    card.assert_not_awaited()
    assert payments.mock_calls == []


@pytest.mark.parametrize('payload', list(handlers.AUDIO_FUN_SCENARIOS))
async def test_song_deleted_callback_does_not_change_state_or_count(credit_db, payload):
    event = callback_event(payload, deleted=True)
    context = await ready_context()
    before = await context.get_data()
    await dispatch_callback(create_router(), event, context)
    event.ack.assert_awaited_once()
    event.send.assert_not_awaited()
    assert await context.get_data() == before
    assert await credit_db.get_all_counters() == {}


@pytest.mark.parametrize('text', ['', '   ', None])
async def test_song_empty_details_keep_waiting(text):
    context = await ready_context()
    await context.set_state(PosterStates.waiting_for_song_details)
    await context.update_data(song_scenario='cat')
    event = message_event(text)
    await call_handler(create_router(), 'collect_song_details', event, context)
    assert 'текстом' in event.message.answer.await_args.args[0]
    assert await context.get_state() == PosterStates.waiting_for_song_details


@pytest.mark.parametrize('destination', ['cat_audio', 'audio_tariff_fun', 'back_to_main', '/cancel'])
async def test_song_navigation_clears_details(destination):
    context = await ready_context()
    await context.set_state(PosterStates.waiting_for_song_details)
    await context.update_data(song_scenario='cat', song_details='old')
    router = create_router()
    if destination == '/cancel':
        await call_handler(router, 'cancel', message_event('/cancel'), context)
    else:
        await dispatch_callback(router, callback_event(destination), context)
    assert await context.get_state() == PosterStates.waiting_for_mode
    assert await context.get_data() == {}


async def test_song_stats_count_scenarios_and_exclude_admin(credit_db):
    router = create_router(admin_user_id=456)
    for user_id in (101, 456):
        for payload in handlers.AUDIO_FUN_SCENARIOS:
            event = callback_event(payload)
            event.get_ids = lambda user_id=user_id: (123, user_id)
            await dispatch_callback(router, event, await ready_context())
    stats = message_event('/stats')
    await call_handler(router, 'stats', stats)
    text = stats.message.answer.await_args.args[0]
    assert '🎵 Интерес к Аудиопесням (сценарии):' in text
    for payload, (label, _) in handlers.AUDIO_FUN_SCENARIOS.items():
        assert '- ' + label + ': 1' in text
        assert (await credit_db.get_all_counters())[handlers.AUDIO_FUN_COUNTERS[payload]] == 2


@pytest.mark.parametrize('general_first', [False, True])
async def test_dispatcher_routes_song_details_and_cancel(credit_db, monkeypatch, event_replies, general_first):
    from maxapi import Dispatcher
    from maxapi.context.isolation import SimpleEventIsolation
    from maxapi.types import MessageCallback, MessageCreated, User
    monkeypatch.setattr(handlers, '_reply', event_replies)
    router = create_router()
    if general_first:
        generic = next(h for h in router.event_handlers if h.func_event.__name__ == 'echo')
        router.event_handlers.remove(generic)
        index = next(i for i, h in enumerate(router.event_handlers) if h.func_event.__name__ == 'collect_song_details')
        router.event_handlers.insert(index, generic)
    dp = Dispatcher(storage=MemoryContext, event_isolation=SimpleEventIsolation())
    dp.include_routers(router)
    bot = SimpleNamespace(me=User(user_id=999, first_name='Bot', is_bot=True),
                          send_message=AsyncMock(return_value=object()), send_callback=AsyncMock())
    user = {'user_id': 456, 'first_name': 'Test', 'is_bot': False}
    def message(text=None, sender=None):
        return {'sender': sender or user, 'recipient': {'chat_id': 123, 'chat_type': 'dialog'},
                'timestamp': 1, 'body': {'mid': 'song-test', 'seq': 1, 'text': text}}
    try:
        for text in ('Барсик, орёт в 5 утра', '/cancel'):
            await dp.handle(MessageCallback(timestamp=1, bot=bot, message=message(sender=bot.me.model_dump()),
                callback={'timestamp': 1, 'callback_id': 'song', 'payload': 'audio_fun_cat', 'user': user}))
            context = dp.fsm.get_context(chat_id=123, user_id=456)
            assert await context.get_state() == PosterStates.waiting_for_song_details
            await dp.handle(MessageCreated(timestamp=2, bot=bot, message=message(text)))
            replies = [c.kwargs.get('text') for c in bot.send_message.await_args_list]
            assert (handlers.AUDIO_FUN_PAYMENT_TEXT if text != '/cancel' else 'Сбор данных отменён.') in replies
            if text == '/cancel':
                assert await context.get_state() == PosterStates.waiting_for_mode
                assert await context.get_data() == {}
            else:
                assert await context.get_state() == PosterStates.waiting_for_audio_fun_payment
                assert (await context.get_data())['song_details'] == text
    finally:
        await dp.shutdown()


async def test_gift_scenario_menu_keeps_order_and_clears_old_data(credit_db):
    context = await ready_context()
    await context.update_data(gift_scenario='cat', gift_details='old')
    event = callback_event('gift_choose_scenario')
    await dispatch_callback(create_router(), event, context)
    event.ack.assert_awaited_once()
    assert event.send.await_args.args[0].startswith('Для кого песня?')
    buttons = event.send.await_args.kwargs['attachments'][0].payload.buttons
    assert [row[0].payload for row in buttons] == [
        'gift_scen_bday', 'gift_scen_anniv', 'gift_scen_parents', 'gift_scen_child',
        'gift_scen_friend', 'gift_scen_custom', 'audio_gift_ny', 'audio_gift_teacher', 'audio_tariff_gift',
    ]
    assert await context.get_state() == PosterStates.waiting_for_mode
    assert await context.get_data() == {}
    assert await credit_db.get_all_counters() == {}


@pytest.mark.parametrize('payload', list(handlers.AUDIO_GIFT_SCENARIOS))
async def test_gift_collects_details_and_shows_test_payment_without_charging(credit_db, payload):
    await credit_db.grant_free_trial(456)
    before = dict(await credit_db.get_user(456))
    generator, card, payments = AsyncMock(), AsyncMock(), Mock()
    router = create_router(generator, postcard_generator=card, payments=payments, admin_user_id=456,
                           song_generator=AsyncMock(return_value='job'), song_waiter=AsyncMock(return_value=TT_RESULT))
    context = await ready_context()
    await context.update_data(photo={'url': 'https://example.com/old.png'}, song_scenario='cat')
    selected = callback_event(payload)
    await dispatch_callback(router, selected, context)
    selected.ack.assert_awaited_once()
    scenario = handlers.gift_scenario_name(payload)
    assert selected.send.await_args.args[0] == handlers.AUDIO_GIFT_SCENARIOS[payload][1]
    assert await context.get_state() == handlers.GIFT_SCENARIO_STATES[payload]
    assert await context.get_data() == {'gift_scenario': scenario}
    answer = message_event('  Аня, любит горы и кофе  ')
    await call_handler(router, 'echo', answer, context)
    assert 'Создаю песню и фрагмент' in answer.message.answer.await_args.args[0]
    assert await context.get_state() == PosterStates.waiting_for_gift_preview
    order_id = (await context.get_data())['audio_checkout_id']
    order = await credit_db.get_audio_order(order_id)
    assert order['preview_requested'] == 0 and order['preview_delivered'] == 1 and order['status'] == 'pending'
    assert 'attachments' not in answer.message.answer.await_args.kwargs
    payment = callback_event('gift_payment_soon')
    await dispatch_callback(router, payment, context)
    payment.ack.assert_awaited_once()
    assert 'Режим администратора' in payment.send.await_args.args[0]
    assert (await credit_db.get_audio_order(order_id))['free_access'] == 1
    assert (await context.get_data())['gift_details'] == 'Аня, любит горы и кофе'
    assert await credit_db.get_all_counters() == {handlers.AUDIO_GIFT_COUNTERS[payload]: 1}
    assert dict(await credit_db.get_user(456)) == before
    generator.assert_not_awaited()
    card.assert_not_awaited()
    assert payments.mock_calls == []


@pytest.mark.parametrize('payload', ['gift_choose_scenario', 'gift_payment_soon', *handlers.AUDIO_GIFT_SCENARIOS])
async def test_deleted_gift_callbacks_are_acknowledged_without_state_or_counter_changes(credit_db, payload):
    context = await ready_context()
    before_state, before_data = await context.get_state(), await context.get_data()
    event = callback_event(payload, deleted=True)
    await dispatch_callback(create_router(), event, context)
    event.ack.assert_awaited_once()
    event.send.assert_not_awaited()
    assert await context.get_state() == before_state
    assert await context.get_data() == before_data
    assert await credit_db.get_all_counters() == {}


@pytest.mark.parametrize('payload', list(handlers.AUDIO_GIFT_SCENARIOS))
@pytest.mark.parametrize('text', ['', '   ', None])
async def test_gift_empty_details_do_not_advance(payload, text):
    context = await ready_context()
    await context.set_state(handlers.GIFT_SCENARIO_STATES[payload])
    await context.update_data(gift_scenario=handlers.gift_scenario_name(payload))
    event = message_event(text)
    await call_handler(create_router(), 'collect_gift_details', event, context)
    assert 'текстом' in event.message.answer.await_args.args[0]
    assert await context.get_state() == handlers.GIFT_SCENARIO_STATES[payload]
    assert 'gift_details' not in await context.get_data()


@pytest.mark.parametrize('destination', ['cat_audio', 'audio_tariff_gift', 'gift_choose_scenario', 'back_to_main', '/cancel'])
@pytest.mark.parametrize('state', [PosterStates.waiting_for_gift_child, PosterStates.waiting_for_gift_preview])
async def test_gift_navigation_clears_collected_details(destination, state):
    context = await ready_context()
    await context.set_state(state)
    await context.update_data(gift_scenario='child', gift_details='Маша, 7 лет')
    router = create_router()
    if destination == '/cancel':
        await call_handler(router, 'cancel', message_event('/cancel'), context)
    else:
        await dispatch_callback(router, callback_event(destination), context)
    assert await context.get_state() == PosterStates.waiting_for_mode
    assert await context.get_data() == {}


async def test_gift_stats_count_scenarios_and_exclude_admin(credit_db):
    router = create_router(admin_user_id=456)
    stats = message_event('/stats')
    await call_handler(router, 'stats', stats)
    empty = stats.message.answer.await_args.args[0]
    for label, _ in handlers.AUDIO_GIFT_SCENARIOS.values():
        assert '- ' + label + ': 0' in empty
    for user_id in (101, 456):
        for payload in handlers.AUDIO_GIFT_SCENARIOS:
            event = callback_event(payload)
            event.get_ids = lambda user_id=user_id: (123, user_id)
            await dispatch_callback(router, event, await ready_context())
    await call_handler(router, 'stats', stats)
    text = stats.message.answer.await_args.args[0]
    assert '🎁 Интерес к Аудиопесням (Подарочный формат):' in text
    for payload, (label, _) in handlers.AUDIO_GIFT_SCENARIOS.items():
        assert '- ' + label + ': 1' in text
        assert (await credit_db.get_all_counters())[handlers.AUDIO_GIFT_COUNTERS[payload]] == 2


@pytest.mark.parametrize('general_first', [False, True])
async def test_dispatcher_routes_all_gift_states_and_cancel(credit_db, monkeypatch, event_replies, general_first):
    from maxapi import Dispatcher
    from maxapi.context.isolation import SimpleEventIsolation
    from maxapi.types import MessageCallback, MessageCreated, User
    monkeypatch.setattr(handlers, '_reply', event_replies)
    router = create_router()
    if general_first:
        generic = next(h for h in router.event_handlers if h.func_event.__name__ == 'echo')
        router.event_handlers.remove(generic)
        index = next(i for i, h in enumerate(router.event_handlers) if h.func_event.__name__ == 'collect_gift_details')
        router.event_handlers.insert(index, generic)
    dp = Dispatcher(storage=MemoryContext, event_isolation=SimpleEventIsolation())
    dp.include_routers(router)
    bot = SimpleNamespace(me=User(user_id=999, first_name='Bot', is_bot=True),
                          send_message=AsyncMock(return_value=object()), send_callback=AsyncMock())
    user = {'user_id': 456, 'first_name': 'Test', 'is_bot': False}
    def message(text=None, sender=None):
        return {'sender': sender or user, 'recipient': {'chat_id': 123, 'chat_type': 'dialog'},
                'timestamp': 1, 'body': {'mid': 'gift-test', 'seq': 1, 'text': text}}
    try:
        for payload, state in handlers.GIFT_SCENARIO_STATES.items():
            await dp.handle(MessageCallback(timestamp=1, bot=bot, message=message(sender=bot.me.model_dump()),
                callback={'timestamp': 1, 'callback_id': 'gift', 'payload': payload, 'user': user}))
            context = dp.fsm.get_context(chat_id=123, user_id=456)
            assert await context.get_state() == state
            await dp.handle(MessageCreated(timestamp=2, bot=bot, message=message('Аня, любит кофе')))
            assert await context.get_state() == PosterStates.waiting_for_gift_preview
            assert (await context.get_data())['gift_details'] == 'Аня, любит кофе'
            assert any('Создаю песню и фрагмент' in c.kwargs.get('text', '') for c in bot.send_message.await_args_list)
            await dp.handle(MessageCreated(timestamp=3, bot=bot, message=message('/cancel')))
            assert await context.get_state() == PosterStates.waiting_for_mode
            assert await context.get_data() == {}
    finally:
        await dp.shutdown()


async def test_fun_payment_screen_has_placeholder_and_back_without_charging(credit_db):
    payments = Mock()
    router = create_router(payments=payments, admin_user_id=456, song_generator=AsyncMock(return_value='job'),
                           song_waiter=AsyncMock(return_value=TT_RESULT))
    context = await ready_context()
    await dispatch_callback(router, callback_event('audio_fun_boss'), context)
    text = message_event('Сергей, любит кофе')
    await call_handler(router, 'collect_song_details', text, context)
    assert text.message.answer.await_args.args[0] == handlers.AUDIO_FUN_PAYMENT_TEXT
    buttons = text.message.answer.await_args.kwargs['attachments'][0].payload.buttons
    assert [(row[0].text, row[0].payload) for row in buttons] == [
        ('Оплатить 199 ₽ и получить 2 варианта', 'audio_fun_payment_soon'),
        ('⬅️ Назад', 'audio_tariff_fun'),
    ]
    payment = callback_event('audio_fun_payment_soon')
    await dispatch_callback(router, payment, context)
    payment.ack.assert_awaited_once()
    assert 'https://audio.example/1.mp3' in payment.send.await_args.args[0]
    assert (await context.get_data())['song_details'] == 'Сергей, любит кофе'
    assert payments.mock_calls == []
    await dispatch_callback(router, callback_event('audio_tariff_fun'), context)
    assert await context.get_data() == {}
    assert await context.get_state() == PosterStates.waiting_for_mode


@pytest.mark.parametrize('payload', ['audio_gift_preview', 'audio_download_fun_1', 'audio_download_fun_2',
                                      'audio_download_gift_mp3', 'audio_download_gift_instrumental'])
async def test_audio_file_buttons_are_placeholders(payload):
    event = callback_event(payload)
    await dispatch_callback(create_router(), event, await ready_context())
    event.ack.assert_awaited_once()
    assert event.send.await_args.args[0] == 'Аудио скоро появится'


@pytest.mark.parametrize('command,handler_name,format_name,expected_text', [
    ('/test_audio_fun_success', 'test_audio_fun_success', 'fun', handlers.AUDIO_FUN_SUCCESS_TEXT),
    ('/test_audio_gift_success', 'test_audio_gift_success', 'gift', handlers.AUDIO_GIFT_SUCCESS_TEXT),
])
async def test_audio_success_commands_show_test_screens_without_payments(credit_db, command, handler_name, format_name, expected_text):
    await credit_db.grant_free_trial(456)
    before = dict(await credit_db.get_user(456))
    generator, payments = AsyncMock(), Mock()
    router = create_router(generator, payments=payments, admin_user_id=456)
    context = await ready_context()
    event = message_event(command)
    await call_handler(router, handler_name, event, context)
    assert event.message.answer.await_args.args[0].startswith(expected_text)
    assert 'Тестовый экран' in event.message.answer.await_args.args[0]
    assert await context.get_state() == PosterStates.waiting_for_audio_test_result
    assert (await context.get_data())['audio_test_format'] == format_name
    buttons = event.message.answer.await_args.kwargs['attachments'][0].payload.buttons
    expected = (['audio_download_fun_1', 'audio_download_fun_2', 'audio_regenerate'] if format_name == 'fun'
                else ['audio_download_gift_mp3', 'audio_download_gift_instrumental', 'help_support'])
    assert [row[0].payload for row in buttons] == expected
    assert await credit_db.get_all_counters() == {}
    assert dict(await credit_db.get_user(456)) == before
    assert payments.mock_calls == []
    generator.assert_not_awaited()


@pytest.mark.parametrize('handler_name', ['test_audio_fun_success', 'test_audio_gift_success'])
@pytest.mark.parametrize('admin,actor,is_bot', [(None, 456, False), (789, 456, False), (456, 456, True)])
async def test_audio_success_commands_ignore_non_admin_or_bot(handler_name, admin, actor, is_bot):
    event = message_event('/' + handler_name)
    event.get_ids = lambda: (123, actor)
    event.message.sender.is_bot = is_bot
    context = await ready_context()
    before = await context.get_data()
    await call_handler(create_router(admin_user_id=admin), handler_name, event, context)
    event.message.answer.assert_not_awaited()
    assert await context.get_data() == before


async def test_fun_regeneration_is_once_per_test_session(credit_db):
    router = create_router(admin_user_id=456)
    context = await ready_context()
    await call_handler(router, 'test_audio_fun_success', message_event('/test_audio_fun_success'), context)
    first = callback_event('audio_regenerate')
    await dispatch_callback(router, first, context)
    first.ack.assert_awaited_once()
    assert (await context.get_data())['audio_regeneration_used'] is True
    assert 'Бесплатная перегенерация использована.' in first.send.await_args.args[0]
    assert [row[0].payload for row in first.send.await_args.kwargs['attachments'][0].payload.buttons] == [
        'audio_download_fun_1', 'audio_download_fun_2',
    ]
    duplicate = callback_event('audio_regenerate')
    await dispatch_callback(router, duplicate, context)
    duplicate.ack.assert_awaited_once()
    duplicate.send.assert_not_awaited()
    assert await credit_db.get_all_counters() == {'audio_regenerate_click': 2}


@pytest.mark.parametrize('format_name', [None, 'gift'])
async def test_regenerate_cannot_start_from_missing_or_gift_session(format_name):
    context = await ready_context()
    if format_name:
        await context.set_state(PosterStates.waiting_for_audio_test_result)
        await context.update_data(audio_test_format=format_name)
    before = await context.get_data()
    event = callback_event('audio_regenerate')
    await dispatch_callback(create_router(), event, context)
    event.ack.assert_awaited_once()
    event.send.assert_not_awaited()
    assert await context.get_data() == before


@pytest.mark.parametrize('payload', ['audio_fun_payment_soon', 'audio_gift_preview', 'audio_regenerate',
                                      'audio_download_fun_1', 'audio_download_fun_2',
                                      'audio_download_gift_mp3', 'audio_download_gift_instrumental'])
async def test_deleted_new_audio_callbacks_are_acknowledged(payload):
    event = callback_event(payload, deleted=True)
    await dispatch_callback(create_router(), event, await ready_context())
    event.ack.assert_awaited_once()
    event.send.assert_not_awaited()


async def test_workshop_copy_matches_guarantees():
    text = handlers.CATEGORY_MENUS['cat_audio'][1]
    assert '1 бесплатная перегенерация (ещё 2 варианта)' in text
    assert 'Без превью — сразу после оплаты.' in text
    assert 'превью с защитой (10–15 сек)' in text
    assert 'без водяных знаков и звуковой метки' in text


@pytest.mark.parametrize('payload,counter', [
    ('photo_temp_fam3', 'select_photo_fam3'), ('photo_temp_fam4', 'select_photo_fam4'),
    ('photo_temp_toddlers', 'select_photo_toddlers'),
])
async def test_photo_template_selection_counts_each_click_without_generation(credit_db, payload, counter):
    generator = AsyncMock()
    router = create_router(photosession_generator=generator)
    context = await ready_context()
    await context.update_data(photo_style='photo_style_ny', photo_tariff='photo_tariff_590')
    for _ in range(2):
        event = callback_event(payload)
        await dispatch_callback(router, event, context)
        event.ack.assert_awaited_once()
    assert await credit_db.get_all_counters() == {counter: 2}
    generator.assert_not_awaited()
    assert (await credit_db.get_global_stats())['total_generated'] == 0


@pytest.mark.parametrize('payload,counter', [
    ('audio_download_fun_1', 'audio_download_mp3'), ('audio_download_fun_2', 'audio_download_mp3'),
    ('audio_download_gift_mp3', 'audio_download_mp3'), ('audio_download_gift_instrumental', 'audio_download_inst'),
    ('audio_gift_preview', None),
])
async def test_audio_file_click_counters_exclude_preview(credit_db, payload, counter):
    context = await ready_context()
    router = create_router()
    for _ in range(2):
        event = callback_event(payload)
        await dispatch_callback(router, event, context)
        event.ack.assert_awaited_once()
        assert event.send.await_args.args[0] == 'Аудио скоро появится'
    assert await credit_db.get_all_counters() == ({counter: 2} if counter else {})
    assert (await credit_db.get_global_stats())['total_generated'] == 0


async def test_regeneration_attempt_is_counted_even_when_session_is_missing(credit_db):
    event = callback_event('audio_regenerate')
    await dispatch_callback(create_router(), event, await ready_context())
    event.ack.assert_awaited_once()
    event.send.assert_not_awaited()
    assert await credit_db.get_all_counters() == {'audio_regenerate_click': 1}


async def test_new_engagement_stats_zero_and_admin_exclusion(credit_db):
    expected = {
        'select_photo_fam3': ('photo_temp_fam3', '- Семейная 3+'),
        'select_photo_fam4': ('photo_temp_fam4', '- Семейная 4+'),
        'select_photo_toddlers': ('photo_temp_toddlers', '- Семейная с малышами'),
        'audio_regenerate_click': ('audio_regenerate', '- Нажали «Не то, ещё раз» (регенерация)'),
        'audio_download_mp3': ('audio_download_gift_mp3', '- Скачали MP3'),
        'audio_download_inst': ('audio_download_gift_instrumental', '- Скачали минусовку'),
    }
    router = create_router(admin_user_id=456)
    stats = message_event('/stats')
    await call_handler(router, 'stats', stats)
    text = stats.message.answer.await_args.args[0]
    for _, label in expected.values():
        assert label + ': 0' in text
    for user_id in (101, 456):
        for payload, _ in expected.values():
            context = await ready_context()
            await context.update_data(photo_style='photo_style_ny', photo_tariff='photo_tariff_590')
            event = callback_event(payload)
            event.get_ids = lambda user_id=user_id: (123, user_id)
            await dispatch_callback(router, event, context)
    counters = await credit_db.get_all_counters()
    assert {key: counters[key] for key in expected} == dict.fromkeys(expected, 2)
    await call_handler(router, 'stats', stats)
    text = stats.message.answer.await_args.args[0]
    for _, label in expected.values():
        assert label + ': 1' in text
    assert text.index('🖼 Всего сгенерировано картинок:') < text.index('📸 Выбор шаблонов Фотосессий:')
    assert text.index('📸 Выбор шаблонов Фотосессий:') < text.index('🎵 Активность Аудиопесен:')
    assert (await credit_db.get_global_stats())['total_generated'] == 0


@pytest.mark.parametrize('payload', ['photo_temp_fam3', 'photo_temp_fam4', 'photo_temp_toddlers',
    'audio_regenerate', 'audio_download_fun_1', 'audio_download_fun_2',
    'audio_download_gift_mp3', 'audio_download_gift_instrumental'])
async def test_deleted_engagement_buttons_do_not_increment(credit_db, payload):
    event = callback_event(payload, deleted=True)
    await dispatch_callback(create_router(), event, await ready_context())
    event.ack.assert_awaited_once()
    assert await credit_db.get_all_counters() == {}


TT_RESULT = {'status': 'SUCCESS', 'data': {'musics': [{'title': 'Первый', 'audioUrl': 'https://audio.example/1.mp3'}, {'title': 'Второй', 'audioUrl': 'https://audio.example/2.mp3'}]}}


async def tt_context(kind='fun'):
    context = await ready_context()
    await context.clear()
    if kind == 'fun':
        await context.update_data(song_scenario='boss', song_details='Сергей, любит кофе', song_style='rock, Russian vocals')
        await context.set_state(PosterStates.waiting_for_audio_fun_payment)
    else:
        await context.update_data(gift_scenario='bday', gift_details='Аня, любит горы')
        await context.set_state(PosterStates.waiting_for_gift_preview)
    return context


@pytest.mark.parametrize('kind', ['fun', 'gift'])
async def test_ttapi_generation_returns_two_links_and_reuses_completed_task(credit_db, kind):
    submit, wait = AsyncMock(return_value='job-123'), AsyncMock(return_value=TT_RESULT)
    payments = Mock()
    router = create_router(payments=payments, admin_user_id=456, song_generator=submit, song_waiter=wait)
    context = await tt_context(kind)
    payload = 'audio_fun_payment_soon' if kind == 'fun' else 'gift_payment_soon'
    event = callback_event(payload)
    await dispatch_callback(router, event, context)
    event.ack.assert_awaited_once()
    assert submit.await_count == wait.await_count == 1
    prompt, tags, title = submit.await_args.args
    assert ('Сергей' if kind == 'fun' else 'Аня') in prompt
    scenario_name = 'Начальник года' if kind == 'fun' else 'День рождения'
    assert scenario_name not in prompt
    assert scenario_name not in tags
    assert scenario_name in title
    assert prompt.startswith('[Verse 1]\n')
    assert prompt.index('Сергей, любит кофе' if kind == 'fun' else 'Аня, любит горы') > prompt.index('[Verse 2]')
    assert ('rock' if kind == 'fun' else 'warm emotional pop') in tags
    assert len(title) <= 80
    assert 'https://audio.example/1.mp3' in event.send.await_args.args[0]
    assert 'https://audio.example/2.mp3' in event.send.await_args.args[0]
    assert [c.args[0] for c in event.send.await_args_list[:2]] == [
        '👑 Режим администратора: Оплата пропущена. Запускаю генерацию...',
        '🎵 Генерирую ваши треки... Это займет около 1-2 минут.',
    ]
    assert await context.get_state() == PosterStates.waiting_for_audio_result
    assert (await context.get_data())['audio_job_id'] == 'job-123'
    await dispatch_callback(router, callback_event(payload), context)
    submit.assert_awaited_once()
    wait.assert_awaited_once_with('job-123')
    assert payments.mock_calls == []
    assert (await credit_db.get_global_stats())['total_generated'] == 0
    download = callback_event('audio_download_fun_2' if kind == 'fun' else 'audio_download_gift_2')
    await dispatch_callback(router, download, context)
    assert download.send.await_args.args[0] == 'https://audio.example/2.mp3'
    link = download.send.await_args.kwargs['attachments'][0].payload.buttons[0][0]
    assert link.url == 'https://audio.example/2.mp3'
    assert (await credit_db.get_all_counters())['audio_download_mp3'] == 1


async def test_ttapi_timeout_resumes_existing_job_without_resubmitting():
    submit = AsyncMock(return_value='existing-job')
    wait = AsyncMock(side_effect=[TimeoutError(), TT_RESULT])
    router = create_router(admin_user_id=456, song_generator=submit, song_waiter=wait)
    context = await tt_context()
    first = callback_event('audio_fun_payment_soon')
    await dispatch_callback(router, first, context)
    assert 'без новой генерации' in first.send.await_args.args[0]
    assert await context.get_state() == PosterStates.waiting_for_audio_fun_payment
    await dispatch_callback(router, callback_event('audio_fun_payment_soon'), context)
    submit.assert_awaited_once()
    assert [c.args for c in wait.await_args_list] == [('existing-job',), ('existing-job',)]


async def test_ttapi_delivery_failure_keeps_tracks_for_retry():
    submit, wait = AsyncMock(return_value='job'), AsyncMock(return_value=TT_RESULT)
    router = create_router(admin_user_id=456, song_generator=submit, song_waiter=wait)
    context = await tt_context()
    event = callback_event('audio_fun_payment_soon')
    event.send.side_effect = [object(), object(), RuntimeError('MAX delivery failed'), object()]
    await dispatch_callback(router, event, context)
    assert (await context.get_data())['audio_tracks']
    retry = callback_event('audio_fun_payment_soon')
    await dispatch_callback(router, retry, context)
    assert 'https://audio.example/1.mp3' in retry.send.await_args.args[0]
    submit.assert_awaited_once()
    wait.assert_awaited_once()


async def test_ttapi_concurrent_click_does_not_submit_second_task():
    import asyncio
    entered, release = asyncio.Event(), asyncio.Event()
    async def wait(job):
        entered.set()
        await release.wait()
        return TT_RESULT
    submit = AsyncMock(return_value='job')
    router = create_router(admin_user_id=456, song_generator=submit, song_waiter=wait)
    context = await tt_context()
    task = asyncio.create_task(dispatch_callback(router, callback_event('audio_fun_payment_soon'), context))
    try:
        await entered.wait()
        duplicate = callback_event('audio_fun_payment_soon')
        await dispatch_callback(router, duplicate, context)
        duplicate.ack.assert_awaited_once()
        duplicate.send.assert_not_awaited()
    finally:
        release.set()
        await task
    submit.assert_awaited_once()


async def test_ttapi_navigation_during_polling_does_not_restore_old_session():
    import asyncio
    entered, release = asyncio.Event(), asyncio.Event()
    async def wait(job):
        entered.set()
        await release.wait()
        return TT_RESULT
    router = create_router(admin_user_id=456, song_generator=AsyncMock(return_value='job'), song_waiter=wait)
    context = await tt_context()
    event = callback_event('audio_fun_payment_soon')
    task = asyncio.create_task(dispatch_callback(router, event, context))
    try:
        await entered.wait()
        await dispatch_callback(router, callback_event('cat_audio'), context)
    finally:
        release.set()
        await task
    assert event.send.await_count == 2  # Admin notice and progress, no stale result.
    assert await context.get_data() == {}
    assert await context.get_state() == PosterStates.waiting_for_mode


async def test_ttapi_uncertain_submit_blocks_duplicate_post():
    from audio_service import AudioServiceError
    submit, wait = AsyncMock(side_effect=AudioServiceError('unknown submit outcome')), AsyncMock()
    router = create_router(admin_user_id=456, song_generator=submit, song_waiter=wait)
    context = await tt_context()
    await dispatch_callback(router, callback_event('audio_fun_payment_soon'), context)
    retry = callback_event('audio_fun_payment_soon')
    await dispatch_callback(router, retry, context)
    assert 'Обратитесь в поддержку' in retry.send.await_args.args[0]
    submit.assert_awaited_once()
    wait.assert_not_awaited()


async def test_ttapi_missing_key_can_retry_after_configuration():
    from audio_service import AudioNotConfigured
    submit = AsyncMock(side_effect=[AudioNotConfigured(), 'job'])
    router = create_router(admin_user_id=456, song_generator=submit, song_waiter=AsyncMock(return_value=TT_RESULT))
    context = await tt_context()
    event = callback_event('audio_fun_payment_soon')
    await dispatch_callback(router, event, context)
    assert 'временно недоступна' in event.send.await_args.args[0]
    assert (await context.get_data())['audio_submit_started'] is False
    await dispatch_callback(router, callback_event('audio_fun_payment_soon'), context)
    assert await context.get_state() == PosterStates.waiting_for_audio_result


@pytest.mark.parametrize('kind', ['fun', 'gift'])
async def test_ttapi_old_payment_button_without_details_never_generates(kind):
    submit, wait = AsyncMock(), AsyncMock()
    event = callback_event('audio_fun_payment_soon' if kind == 'fun' else 'gift_payment_soon')
    await dispatch_callback(create_router(admin_user_id=456, song_generator=submit, song_waiter=wait), event, await ready_context())
    event.ack.assert_awaited_once()
    event.send.assert_not_awaited()
    submit.assert_not_awaited()
    wait.assert_not_awaited()


async def test_ttapi_one_gift_track_is_delivered():
    result = {'status': 'SUCCESS', 'data': {'musics': TT_RESULT['data']['musics'][:1]}}
    event = callback_event('gift_payment_soon')
    await dispatch_callback(create_router(admin_user_id=456, song_generator=AsyncMock(return_value='job'),
        song_waiter=AsyncMock(return_value=result)), event, await tt_context('gift'))
    assert 'https://audio.example/1.mp3' in event.send.await_args.args[0]
    assert 'Вариант 2' not in event.send.await_args.args[0]


@pytest.mark.parametrize('kind', ['fun', 'gift'])
@pytest.mark.parametrize('admin,actor', [(None, 456), (789, 456), (456, 789)])
@pytest.mark.parametrize('cached', [False, True])
async def test_non_admin_audio_payment_is_placeholder_without_api_calls(credit_db, kind, admin, actor, cached):
    submit, wait = AsyncMock(), AsyncMock()
    router = create_router(admin_user_id=admin, song_generator=submit, song_waiter=wait, payments=None)
    context = await tt_context(kind)
    if cached:
        await context.update_data(audio_format=kind, audio_job_id='old-job', audio_tracks=TT_RESULT['data']['musics'])
    before_state, before_data = await context.get_state(), await context.get_data()
    event = callback_event('audio_fun_payment_soon' if kind == 'fun' else 'gift_payment_soon')
    # chat_id администратора сам по себе не предоставляет доступ.
    event.get_ids = lambda: (456, actor)
    await dispatch_callback(router, event, context)
    event.ack.assert_awaited_once_with(notification='Оплата пока недоступна.')
    assert 'Оплата аудиопесен пока недоступна' in event.send.await_args.args[0]
    assert 'Режим администратора' not in event.send.await_args.args[0]
    back = event.send.await_args.kwargs['attachments'][0].payload.buttons[0][0]
    assert back.payload == ('audio_tariff_fun' if kind == 'fun' else 'gift_choose_scenario')
    submit.assert_not_awaited()
    wait.assert_not_awaited()
    assert await context.get_state() == before_state
    assert await context.get_data() == before_data
    assert await credit_db.get_all_counters() == {}


@pytest.mark.parametrize('kind', ['fun', 'gift'])
async def test_audio_checkout_creates_personal_link_and_keeps_snapshot_without_generating(credit_db, kind):
    async def create(user_id, fmt, scenario, details, style, prompt, tags, title, *, order_id):
        await credit_db.create_audio_order(order_id, user_id, fmt, scenario, details, style, prompt, tags, title)
        return 'https://link.payform.ru/personal-order'
    payments = SimpleNamespace(create_audio_payment_link=AsyncMock(side_effect=create))
    submit, wait = AsyncMock(), AsyncMock()
    router = create_router(payments=payments, admin_user_id=999, song_generator=submit, song_waiter=wait)
    context = await tt_context(kind)
    if kind == 'gift':
        data = await context.get_data()
        prompt, tags, title = handlers.build_song_request(data['gift_details'], handlers.AUDIO_GIFT_SCENARIOS['gift_scen_bday'][0], 'gift')
        await credit_db.create_audio_order('preview-invoice', 456, 'gift', 'bday', data['gift_details'], None, prompt, tags, title)
        await credit_db.update_audio_order('preview-invoice', preview_requested=1, preview_url='https://preview.example/clip.mp3', preview_delivered=1)
        await context.update_data(audio_checkout_id='preview-invoice')
    event = callback_event('audio_fun_payment_soon' if kind == 'fun' else 'gift_payment_soon')
    await dispatch_callback(router, event, context)
    event.ack.assert_awaited_once()
    link = event.send.await_args.kwargs['attachments'][0].payload.buttons[0][0]
    assert link.url == 'https://link.payform.ru/personal-order'
    data = await context.get_data()
    order_id = data['audio_checkout_id']
    saved = await credit_db.get_audio_order(order_id)
    assert saved['user_id'] == 456 and saved['audio_format'] == kind
    assert saved['amount_kopecks'] == (19900 if kind == 'fun' else 49000)
    assert saved['details'] == ('Сергей, любит кофе' if kind == 'fun' else 'Аня, любит горы')
    assert saved['status'] == 'pending'
    retry = callback_event(event.callback.payload)
    await dispatch_callback(router, retry, context)
    payments.create_audio_payment_link.assert_awaited_once()
    submit.assert_not_awaited()
    wait.assert_not_awaited()
    await credit_db.mark_audio_paid(order_id, 'paid')
    await dispatch_callback(router, retry, context)
    assert 'Оплата уже получена' in retry.send.await_args.args[0]
    payments.create_audio_payment_link.assert_awaited_once()
    await context.clear()
    assert (await credit_db.get_audio_order(order_id))['details'] == saved['details']


@pytest.mark.parametrize('kind', ['fun', 'gift'])
async def test_audio_checkout_without_details_never_creates_invoice(kind):
    payments = SimpleNamespace(create_audio_payment_link=AsyncMock())
    event = callback_event('audio_fun_payment_soon' if kind == 'fun' else 'gift_payment_soon')
    await dispatch_callback(create_router(payments=payments), event, await ready_context())
    assert 'заполните детали' in event.send.await_args.args[0]
    payments.create_audio_payment_link.assert_not_awaited()


async def test_concurrent_audio_checkout_creates_only_one_link(credit_db):
    import asyncio
    entered, release = asyncio.Event(), asyncio.Event()
    async def create(*args, **kwargs):
        entered.set()
        await release.wait()
        return 'https://link.payform.ru/one'
    payments = SimpleNamespace(create_audio_payment_link=AsyncMock(side_effect=create))
    router = create_router(payments=payments)
    context = await tt_context()
    first = asyncio.create_task(dispatch_callback(router, callback_event('audio_fun_payment_soon'), context))
    try:
        await entered.wait()
        duplicate = callback_event('audio_fun_payment_soon')
        await dispatch_callback(router, duplicate, context)
        duplicate.send.assert_not_awaited()
    finally:
        release.set()
        await first
    payments.create_audio_payment_link.assert_awaited_once()


@pytest.mark.parametrize('actor', [456, 789])
async def test_paid_audio_download_uses_persistent_order_and_checks_owner(credit_db, actor):
    import json
    await credit_db.create_audio_order('download-order', 456, 'gift', 'bday', 'Аня', None, '[Verse]\nАня', 'pop', 'title')
    await credit_db.mark_audio_paid('download-order', 'paid')
    await credit_db.update_audio_order('download-order', tracks_json=json.dumps(TT_RESULT['data']['musics']))
    event = callback_event('audio_paid_download:download-order:1')
    event.get_ids = lambda: (123, actor)
    await dispatch_callback(create_router(), event, await ready_context())
    event.ack.assert_awaited_once()
    if actor == 456:
        assert event.send.await_args.args[0] == 'https://audio.example/2.mp3'
        assert (await credit_db.get_all_counters())['audio_download_mp3'] == 1
    else:
        event.send.assert_not_awaited()
        assert 'audio_download_mp3' not in await credit_db.get_all_counters()


@pytest.mark.parametrize('kind', ['fun', 'gift'])
async def test_regular_audio_payment_screen_has_direct_url_button_before_payment(credit_db, kind):
    async def create(user_id, fmt, scenario, details, style, prompt, tags, title, *, order_id):
        await credit_db.create_audio_order(order_id, user_id, fmt, scenario, details, style, prompt, tags, title)
        return 'https://link.payform.ru/personal-checkout'
    payments = SimpleNamespace(create_audio_payment_link=AsyncMock(side_effect=create))
    submit, wait = AsyncMock(return_value='gift-job'), AsyncMock(return_value=TT_RESULT)
    router = create_router(payments=payments, song_generator=submit, song_waiter=wait)
    context = await ready_context()
    scenario = 'audio_fun_boss' if kind == 'fun' else 'gift_scen_bday'
    await dispatch_callback(router, callback_event(scenario), context)
    event = message_event('Аня, любит горы')
    await call_handler(router, 'echo', event, context)
    if kind == 'gift':
        submit.assert_awaited_once()
        wait.assert_awaited_once_with('gift-job')
        payments.create_audio_payment_link.assert_awaited_once()
        assert 'Послушайте фрагмент' in event.bot.send_message.await_args.kwargs['text']
        return
    buttons = event.message.answer.await_args.kwargs['attachments'][0].payload.buttons
    pay = next(row[0] for row in buttons if row[0].text.startswith('Оплатить'))
    assert pay.url == 'https://link.payform.ru/personal-checkout'
    saved = await credit_db.get_audio_order((await context.get_data())['audio_checkout_id'])
    assert saved['status'] == 'pending' and saved['details'] == 'Аня, любит горы'
    payments.create_audio_payment_link.assert_awaited_once()
    submit.assert_not_awaited()
    wait.assert_not_awaited()


@pytest.mark.parametrize('actor', [456, 789])
async def test_paid_regeneration_callback_checks_owner_and_does_not_create_payment(credit_db, actor):
    import json
    await credit_db.create_audio_order('regen-order',456,'gift','bday','Аня',None,'[Verse]\nАня','pop','title')
    await credit_db.mark_audio_paid('regen-order','paid')
    await credit_db.update_audio_order('regen-order', tracks_json=json.dumps(TT_RESULT['data']['musics']), delivered=1)
    payments = SimpleNamespace(create_audio_payment_link=AsyncMock())
    event = callback_event('audio_regenerate:regen-order')
    event.get_ids = lambda: (123,actor)
    router = create_router(payments=payments)
    await dispatch_callback(router,event,await ready_context())
    event.ack.assert_awaited_once()
    assert (await credit_db.get_audio_order('regen-order'))['regeneration_used'] == (1 if actor==456 else 0)
    payments.create_audio_payment_link.assert_not_awaited()
    if actor==456:
        duplicate = callback_event('audio_regenerate:regen-order')
        await dispatch_callback(router,duplicate,await ready_context())
        duplicate.send.assert_not_awaited()


async def test_admin_real_song_regenerates_saved_details_once_without_payment(credit_db):
    submit = AsyncMock(side_effect=['original-job','new-job'])
    payments = Mock()
    router = create_router(admin_user_id=456,payments=payments,song_generator=submit,song_waiter=AsyncMock(return_value=TT_RESULT))
    context = await tt_context()
    await dispatch_callback(router,callback_event('audio_fun_payment_soon'),context)
    event = callback_event('audio_regenerate')
    await dispatch_callback(router,event,context)
    assert submit.await_count==2
    assert submit.await_args_list[0].args == submit.await_args_list[1].args
    assert (await context.get_data())['audio_regeneration_used'] is True
    duplicate = callback_event('audio_regenerate')
    await dispatch_callback(router,duplicate,context)
    assert submit.await_count==2
    assert payments.mock_calls==[]


async def test_gift_full_download_is_denied_before_payment_even_when_tracks_exist(credit_db):
    import json
    await credit_db.create_audio_order('locked-gift',456,'gift','bday','Аня',None,'lyrics','pop','title')
    await credit_db.update_audio_order('locked-gift', tracks_json=json.dumps(TT_RESULT['data']['musics']),preview_url='https://preview.example/safe.mp3')
    event = callback_event('audio_paid_download:locked-gift:0')
    await dispatch_callback(create_router(),event,await ready_context())
    event.send.assert_not_awaited()
    event.ack.assert_awaited_once()


async def test_paid_download_has_regeneration_and_direct_support_link(credit_db):
    import json
    await credit_db.create_audio_order('actions',456,'gift','bday','Аня',None,'lyrics','pop','title')
    await credit_db.mark_audio_paid('actions','paid')
    await credit_db.update_audio_order('actions', tracks_json=json.dumps(TT_RESULT['data']['musics']),delivered=1)
    event = callback_event('audio_paid_download:actions:0')
    await dispatch_callback(create_router(),event,await ready_context())
    buttons = [row[0].model_dump() for row in event.send.await_args.kwargs['attachments'][0].payload.buttons]
    assert any(button.get('payload')=='audio_regenerate:actions' for button in buttons)
    assert any(button.get('url')==handlers.SUPPORT_URL for button in buttons)
    assert any(button.get('payload')=='audio_paid_instrumental:actions' for button in buttons)


async def test_same_unpaid_gift_details_reuse_preview_order_without_new_song(credit_db):
    router=create_router()
    context=await ready_context()
    for _ in range(2):
        await dispatch_callback(router,callback_event('gift_scen_bday'),context)
        event=message_event('Аня, любит горы')
        await call_handler(router,'echo',event,context)
        current=(await context.get_data())['audio_checkout_id']
        if _==0:
            original=current
        else:
            assert current==original
    assert await credit_db.pending_audio_orders()==[]
    handlers.generate_song.assert_awaited_once()


async def test_gift_checkout_from_preview_survives_lost_fsm(credit_db):
    await credit_db.create_audio_order('preview-pay',456,'gift','bday','Аня',None,'lyrics','pop','title')
    await credit_db.update_audio_order('preview-pay',preview_url='https://preview.example/clip.mp3',preview_requested=1,preview_delivered=1)
    payments=SimpleNamespace(create_audio_payment_link=AsyncMock(return_value='https://pay.example/490'))
    event=callback_event('audio_gift_checkout:preview-pay')
    await dispatch_callback(create_router(payments=payments),event,await ready_context())
    link=event.send.await_args.kwargs['attachments'][0].payload.buttons[0][0]
    assert link.url=='https://pay.example/490'
    assert payments.create_audio_payment_link.await_args.kwargs['order_id']=='preview-pay'


async def test_admin_can_unlock_preview_without_a_second_generation_or_purchase(credit_db):
    import json
    await credit_db.create_audio_order('admin-preview',456,'gift','bday','Аня',None,'lyrics','pop','title')
    await credit_db.update_audio_order('admin-preview',preview_url='https://preview.example/clip.mp3',preview_requested=1,
                                       preview_delivered=1,tracks_json=json.dumps(TT_RESULT['data']['musics']))
    event=callback_event('audio_admin_full:admin-preview')
    await dispatch_callback(create_router(admin_user_id=456),event,await ready_context())
    saved=await credit_db.get_audio_order('admin-preview')
    assert saved['free_access']==1 and saved['status']=='pending'
    assert await credit_db.pending_audio_orders()==['admin-preview']
    assert (await credit_db.get_global_stats())['total_purchases']==0


async def test_gift_preview_direct_await_is_not_blocked_by_another_admin_generation(credit_db):
    import asyncio
    entered, release = asyncio.Event(), asyncio.Event()
    async def wait(job):
        if job == 'blocked-job':
            entered.set()
            await release.wait()
        return TT_RESULT
    router = create_router(admin_user_id=456, song_generator=AsyncMock(side_effect=['blocked-job', 'gift-job']), song_waiter=wait)
    first = asyncio.create_task(dispatch_callback(router, callback_event('audio_fun_payment_soon'), await tt_context()))
    try:
        await entered.wait()
        context = await tt_context('gift')
        await context.set_state(PosterStates.waiting_for_gift_bday)
        await call_handler(router, 'collect_gift_details', message_event('Аня, любит горы'), context)
        saved = await credit_db.get_audio_order((await context.get_data())['audio_checkout_id'])
        assert saved['preview_delivered'] == 1 and saved['job_id'] == 'gift-job'
        assert saved['order_id'] not in await credit_db.pending_audio_orders()
    finally:
        release.set()
        await first


async def test_admin_direct_wait_finishes_in_the_callback_without_background_waiter(credit_db):
    import asyncio
    entered, release = asyncio.Event(), asyncio.Event()
    async def wait(job):
        entered.set()
        await release.wait()
        return 'https://audio.example/1.mp3'
    submit = AsyncMock(return_value='direct-job')
    router = create_router(admin_user_id=456, song_generator=submit, song_waiter=wait)
    context = await tt_context('gift')
    task = asyncio.create_task(dispatch_callback(router, callback_event('gift_payment_soon'), context))
    try:
        await entered.wait()
        assert not task.done()
        assert await context.get_state() == PosterStates.waiting_for_audio_generation
    finally:
        release.set()
        await task
    assert await context.get_state() == PosterStates.waiting_for_audio_result
    submit.assert_awaited_once()


@pytest.mark.parametrize('active,price', [(True, 392), (False, 490)])
async def test_song_upsell_checkout_uses_personal_order_and_current_promo(credit_db, active, price):
    import json
    await credit_db.create_audio_order('upsell-ready', 456, 'gift', 'bday', 'Аня', None, 'lyrics', 'pop', 'Для Ани')
    await credit_db.update_audio_order('upsell-ready', preview_url='https://preview.example/song.mp3',
                                     tracks_json=json.dumps(TT_RESULT['data']['musics']), preview_delivered=1)
    if active:
        await credit_db.set_song_promo(456)
    payments = Mock(create_audio_payment_link=AsyncMock(return_value='https://pay.example/personal'))
    event = callback_event('upsell_song_discount')
    await dispatch_callback(create_router(payments=payments), event, await ready_context())
    call = payments.create_audio_payment_link.await_args
    saved = await credit_db.get_audio_order(call.kwargs['order_id'])
    assert saved['user_id'] == 456 and saved['amount_kopecks'] == price * 100
    assert saved['product_name'] == (credit_db.SONG_PROMO_PRODUCT if active else 'Песня в подарок')
    assert event.send.await_args.args[0] == ('Отличный выбор! Переходите по ссылке для оплаты со скидкой:' if active else handlers.SONG_PROMO_EXPIRED_TEXT)
    button = event.send.await_args.kwargs['attachments'][0].payload.buttons[0][0]
    assert button.type == 'link' and button.url == 'https://pay.example/personal'
    assert str(price) in button.text


@pytest.mark.parametrize('active', [True, False])
async def test_song_upsell_without_song_collects_details_before_payment(credit_db, active):
    if active:
        await credit_db.set_song_promo(456)
    context = await ready_context()
    await context.update_data(photo='old photo', card_text='old text')
    payments = Mock(create_audio_payment_link=AsyncMock())
    event = callback_event('upsell_song_discount')
    await dispatch_callback(create_router(payments=payments), event, context)
    assert await context.get_data() == {}
    assert await context.get_state() == PosterStates.waiting_for_mode
    buttons = event.send.await_args.kwargs['attachments'][0].payload.buttons
    assert buttons[0][0].payload == 'gift_scen_bday'
    assert ('392' if active else '490') in event.send.await_args.args[0]
    payments.create_audio_payment_link.assert_not_awaited()


async def test_dismiss_song_upsell_preserves_current_fsm(credit_db):
    context = await ready_context()
    await context.update_data(photo='saved')
    before_state = await context.get_state()
    event = callback_event('dismiss_upsell')
    await dispatch_callback(create_router(), event, context)
    assert event.send.await_args.args[0] == 'Хорошо! Если надумаете, мы всегда здесь 😊'
    assert await context.get_state() == before_state
    assert await context.get_data() == {'photo': 'saved'}
    assert not await credit_db.check_song_promo(456)


async def test_failed_card_delivery_does_not_start_song_promotion(credit_db):
    await credit_db.add_credits(456, 2)
    context = await ready_context()
    await context.update_data(mode='mode_ny')
    router = create_router(postcard_generator=AsyncMock(return_value=['https://cdn.example/card.png']))
    await call_handler(router, 'collect_photo', photo_event(), context)
    event = message_event('С праздником!')
    event.bot.send_message.side_effect = RuntimeError('MAX unavailable')
    await call_handler(router, 'collect_card_text', event, context)
    assert not await credit_db.check_song_promo(456)
    assert not any(call.args and call.args[0] == handlers.SONG_UPSELL_TEXT for call in event.message.answer.await_args_list)


@pytest.mark.parametrize('entry', ['start', 'bot_started'])
async def test_start_saves_latest_yclid_without_changing_first_source(credit_db, example_photos, monkeypatch, event_replies, entry):
    monkeypatch.setattr(handlers, '_reply', event_replies)
    router = create_router()
    context = await ready_context()
    def start_event(payload):
        return bot_started_event(payload) if entry == 'bot_started' else message_event('/start' + (' ' + payload if payload else ''))
    await call_handler(router, entry, start_event('yandex_newyear?yclid=111111'), context)
    user = await credit_db.get_user(456)
    assert user['yclid'] == '111111' and user['first_start_source'] == 'yandex_newyear'
    first_time = user['first_started_at']
    await call_handler(router, entry, start_event('yclid=222222'), context)
    await call_handler(router, entry, start_event(None), context)
    user = await credit_db.get_user(456)
    assert user['yclid'] == '222222'
    assert user['first_started_at'] == first_time and user['first_start_source'] == 'yandex_newyear'
    assert user['user_id'] == 456 and user['balance'] == 1
    handlers.send_offline_conversion.assert_not_awaited()


async def test_vision_metrika_only_after_success_not_navigation_or_redelivery(credit_db):
    await credit_db.add_credits(456, 3)
    generator = AsyncMock(side_effect=[RuntimeError('generation failed'), ['https://cdn.example/vision.png']])
    router = create_router(generator)
    context = await ready_context()
    event = message_event('Мечта')
    await call_handler(router, 'collect_sphere', event, context)
    handlers.send_offline_conversion.assert_not_awaited()
    await call_handler(router, 'collect_sphere', event, context)
    handlers.send_offline_conversion.assert_awaited_once_with(456, goal_id='bot_generation')
    await call_handler(router, 'collect_sphere', event, context)
    handlers.send_offline_conversion.assert_awaited_once()


async def test_photosession_tracks_each_new_frame_and_does_not_repeat_on_resend(credit_db):
    frames = AsyncMock(side_effect=[f'https://cdn.example/frame-{i}.png' for i in range(3)])
    router = create_router(photosession_generator=frames, admin_user_id=456)
    context = await family_context()
    event = callback_event('photo_temp_fam3')
    # Первый кадр создан, но не отправлен. Повторная доставка не создаёт новую конверсию.
    event.bot.send_message.side_effect = RuntimeError('delivery failed')
    await dispatch_callback(router, event, context)
    assert handlers.send_offline_conversion.await_count == 1
    await dispatch_callback(router, callback_event('photo_temp_fam3'), context)
    assert frames.await_count == 3
    assert handlers.send_offline_conversion.await_args_list == [call(456, goal_id='bot_generation')]*3


async def test_metrika_http_failure_does_not_break_postcard_delivery(credit_db, monkeypatch, caplog):
    from aioresponses import aioresponses
    import metrika_service
    from config import Config
    monkeypatch.setattr(metrika_service.Config, 'from_env', lambda: Config(token='test', photo_path=Path('unused'),
        yandex_metrika_counter_id='123', yandex_metrika_oauth_token='fake-token'))
    monkeypatch.setattr(handlers, 'send_offline_conversion', metrika_service.send_offline_conversion)
    await credit_db.add_credits(456, 2)
    context = await ready_context()
    await context.update_data(mode='mode_ny')
    router = create_router(postcard_generator=AsyncMock(return_value=['https://cdn.example/card.png']))
    await call_handler(router, 'collect_photo', photo_event(), context)
    event = message_event('С праздником!')
    with aioresponses() as http:
        http.post(metrika_service.BASE_URL + '/management/v1/counter/123/offline_conversions/upload', status=500, body='service unavailable')
        await call_handler(router, 'collect_card_text', event, context)
    assert event.bot.send_message.await_args.kwargs['text'] == 'Ваша открытка готова!'
    assert (await credit_db.get_user(456))['total_generations'] == 1
    assert (await credit_db.get_user(456))['balance'] == 1
    assert 'Ошибка Метрики: HTTP 500' in caplog.text
    assert await context.get_state() is None


@pytest.mark.parametrize('failure', ['build', 'database', 'notify'])
async def test_gift_queue_failure_logs_traceback_and_allows_details_retry(credit_db, monkeypatch, caplog, failure):
    context = await tt_context('gift')
    await context.set_state(PosterStates.waiting_for_gift_bday)
    event = message_event('Аня, любит горы')
    if failure == 'build':
        monkeypatch.setattr(handlers, 'build_song_request', Mock(return_value=('lyrics', None, 'title')))
    elif failure == 'database':
        monkeypatch.setattr(handlers, 'find_gift_preview_order', AsyncMock(side_effect=RuntimeError('queue database failure')))
    else:
        event.message.answer.side_effect = [RuntimeError('MAX progress failure'), object()]
    await call_handler(create_router(), 'collect_gift_details', event, context)
    assert any(r.exc_info and 'ОШИБКА ГЕНЕРАЦИИ ПЕСНИ:' in r.message for r in caplog.records)
    assert await context.get_state() == PosterStates.waiting_for_gift_bday
    assert event.message.answer.await_args.args[0].startswith('❌ Ошибка:')
    if failure == 'build':
        assert 'Стиль музыки не задан' in event.message.answer.await_args.args[0]


async def test_gift_handler_directly_calls_ttapi_with_all_arguments(credit_db, caplog):
    from audio_orders import PaidAudioWorker
    context = await tt_context('gift')
    await context.set_state(PosterStates.waiting_for_gift_bday)
    event = message_event('Аня, любит горы')
    submit = AsyncMock(return_value='diagnostic-job')
    wait = AsyncMock(return_value=TT_RESULT)
    with caplog.at_level('INFO'):
        await call_handler(create_router(song_generator=submit, song_waiter=wait), 'collect_gift_details', event, context)
        order_id = (await context.get_data())['audio_checkout_id']
    saved = await credit_db.get_audio_order(order_id)
    submit.assert_awaited_once_with(saved['lyrics'], saved['tags'], saved['title'])
    assert all(isinstance(arg, str) and arg.strip() for arg in submit.await_args.args)
    assert 'Запускаю audio_service.generate_song для пользователя 456' in caplog.text
    assert saved['job_id'] == 'diagnostic-job'
    assert saved['preview_delivered'] == 1


@pytest.mark.parametrize('mode', ['mode_ny', 'mode_ny_family', 'mode_ny_cozy', 'mode_ny_fairytale', 'mode_ny_elegant',
    'mode_bday_young', 'mode_bday_old', 'mode_bday_old_elegant', 'mode_modern_card', 'mode_halloween', 'mode_goodmorning'])
async def test_video_299_reuses_card_fsm_but_never_generates_before_payment(credit_db, mode):
    await credit_db.grant_free_trial(456)
    cards, visions, songs = AsyncMock(), AsyncMock(), AsyncMock()
    router = create_router(visions, postcard_generator=cards, song_generator=songs)
    context = await ready_context()
    await dispatch_callback(router, callback_event('cat_video'), context)
    await dispatch_callback(router, callback_event('video_style:' + mode), context)
    assert await context.get_state() == PosterStates.waiting_for_photo
    assert (await context.get_data())['mode'] == mode
    await call_handler(router, 'collect_photo', photo_event(), context)
    assert await context.get_state() == PosterStates.waiting_for_card_text
    text = message_event('Аня, с праздником!')
    await call_handler(router, 'collect_card_text', text, context)
    assert await context.get_state() == PosterStates.waiting_for_video_song
    scenario_buttons = text.message.answer.await_args.kwargs['attachments'][0].payload.buttons
    assert any(row[0].text == '🎤 Начальник года' for row in scenario_buttons)
    assert any(row[0].text == '🎂 День рождения' for row in scenario_buttons)
    pay = callback_event('video_song:gift_scen_bday')
    await dispatch_callback(router, pay, context)
    data = await context.get_data()
    assert data['mode'] == mode and data['photo']['url'] and data['card_text'] == 'Аня, с праздником!'
    assert data['video_song_scenario'] == 'gift_scen_bday' and data['video_amount_kopecks'] == 29900
    assert await context.get_state() == PosterStates.waiting_for_video_payment
    assert pay.send.await_args.args[0] == (
        'Отлично! Мы создадим статичную открытку, оживим её (добавим анимацию) и наложим песню.\n\n'
        'Стоимость видео-подарка — 299 ₽. Это волшебство займет около 5 минут, не закрывайте чат!')
    button = pay.send.await_args.kwargs['attachments'][0].payload.buttons[0][0]
    assert button.text == 'Оплатить 299 ₽ и создать видео' and button.url == 'https://link.payform.ru/'
    cards.assert_not_awaited()
    visions.assert_not_awaited()
    songs.assert_not_awaited()
    handlers.send_offline_conversion.assert_not_awaited()
    assert (await credit_db.get_user(456))['balance'] == 1
    assert (await credit_db.get_user(456))['total_generations'] == 0


async def test_video_menu_first_and_matches_existing_style_labels(credit_db, example_photos):
    first = handlers.main_keyboard().payload.buttons[0][0]
    assert (first.text, first.payload) == ('🎬 Ожившая открытка (Видео) — Новинка 🔥', 'cat_video')
    router, context = create_router(), await ready_context()
    await context.update_data(photo={'url': 'old'}, card_text='old', audio_job_id='old')
    entry = callback_event('cat_video')
    await dispatch_callback(router, entry, context)
    assert 'old' not in str(await context.get_data())
    assert entry.send.await_args.args[0] == '🎬 Создаем настоящее видео-поздравление! Выберите стиль открытки:'
    for screen, payload in [('cat_cards', 'cat_video'), ('sub_ny', 'video_style:sub_ny'), ('sub_bday', 'video_style:sub_bday')]:
        event = callback_event(payload)
        await dispatch_callback(router, event, context)
        rows = event.send.await_args.kwargs['attachments'][0].payload.buttons
        assert [row[0].text for row in rows] == [label for label, _ in handlers.CATEGORY_MENUS[screen][2]]
        assert all(row[0].payload.startswith('video_style:') or row[0].payload == 'back_to_main' for row in rows)
    await dispatch_callback(router, callback_event('video_style:cat_cards'), context)
    assert (await context.get_data())['video_amount_kopecks'] == 29900
    await dispatch_callback(router, callback_event('back_to_main'), context)
    assert await context.get_data() == {}


@pytest.mark.parametrize('payload,price', [('upsell_animate', 199), ('upsell_video', 249)])
async def test_static_card_video_upsells_keep_source_and_do_not_generate(credit_db, payload, price):
    context = await ready_context()
    await context.update_data(mode='mode_ny', last_image_url='https://cdn.example/card.png',
                              photo={'url': 'https://cdn.example/source.jpg'}, card_text='С праздником!')
    cards, songs = AsyncMock(), AsyncMock()
    router = create_router(postcard_generator=cards, song_generator=songs)
    event = callback_event(payload)
    await dispatch_callback(router, event, context)
    first_reply = event.send.await_args_list[0]
    assert first_reply.args[0] == '🎵 Выберите сценарий песни для видео-поздравления:'
    rows = first_reply.kwargs['attachments'][0].payload.buttons
    assert [row[0].payload for row in rows[:-1]] == [
        'video_song:audio_fun_boss', 'video_song:audio_fun_friend', 'video_song:audio_fun_cat',
        'video_song:audio_fun_student', 'video_song:audio_fun_universal', 'video_song:audio_fun_meme', 'video_song:audio_fun_ny', 'audio_video_teacher']
    assert all(row[0].type == 'callback' for row in rows)
    data = await context.get_data()
    assert data['video_source_image'] == 'https://cdn.example/card.png'
    assert data['video_amount_kopecks'] == price * 100
    assert await context.get_state() == PosterStates.waiting_for_video_song
    pay = callback_event('video_song:audio_fun_ny')
    await dispatch_callback(router, pay, context)
    assert (await context.get_data())['video_song_scenario'] == 'audio_fun_ny'
    assert f'{price} ₽' in pay.send.await_args.args[0]
    button = pay.send.await_args.kwargs['attachments'][0].payload.buttons[0][0]
    assert str(price) in button.text and button.url == 'https://link.payform.ru/'
    assert await context.get_state() == PosterStates.waiting_for_video_payment
    cards.assert_not_awaited()
    songs.assert_not_awaited()


async def test_video_click_statistics_exclude_admin_and_never_increment_generations(credit_db):
    router = create_router(admin_user_id=456)
    for user_id in (101, 456):
        context = await ready_context()
        for payload in ('cat_video', 'upsell_animate', 'upsell_video'):
            await context.update_data(mode='mode_ny', last_image_url='https://cdn.example/card.png')
            event = callback_event(payload)
            event.get_ids = lambda uid=user_id: (123, uid)
            await dispatch_callback(router, event, context)
    counters = await credit_db.get_all_counters()
    assert counters == {'click_video_menu': 2, 'click_video_animate_199': 2, 'click_video_full_249': 2}
    assert (await credit_db.get_global_stats())['total_generated'] == 0
    event = message_event('/stats')
    await call_handler(router, 'stats', event)
    text = event.message.answer.await_args.args[0]
    assert '- Видео — вход в меню: 1' in text
    assert '- Анимация за 199 ₽ (клики): 1' in text
    assert '- Видео с песней за 249 ₽ (клики): 1' in text


@pytest.mark.parametrize('payload', ['video_style:mode_ny', 'video_song:gift_scen_bday', 'upsell_video', 'upsell_animate'])
async def test_stale_video_buttons_are_safe_without_fsm(payload):
    cards, songs = AsyncMock(), AsyncMock()
    event = callback_event(payload)
    await dispatch_callback(create_router(postcard_generator=cards, song_generator=songs), event, await ready_context())
    event.ack.assert_awaited_once()
    cards.assert_not_awaited()
    songs.assert_not_awaited()


async def test_video_offer_not_repeated_when_sharing_fails_after_card_delivery(credit_db):
    await credit_db.add_credits(456, 2)
    context = await ready_context()
    await context.update_data(mode='mode_ny')
    cards = AsyncMock(return_value=['https://cdn.example/card.png'])
    router = create_router(postcard_generator=cards)
    await call_handler(router, 'collect_photo', photo_event(), context)
    event = message_event('С праздником!')
    failed = False
    async def reply(text=None, **kwargs):
        nonlocal failed
        if text == '👇 Хочешь поделиться с друзьями?' and not failed:
            failed = True
            raise RuntimeError('share message failed')
        return object()
    event.message.answer.side_effect = reply
    await call_handler(router, 'collect_card_text', event, context)
    assert await context.get_state() == PosterStates.waiting_for_card_text
    retry = callback_event('postcard_retry')
    await dispatch_callback(router, retry, context)
    assert sum(call.args and call.args[0] == handlers.VIDEO_UPSELL_TEXT for call in event.message.answer.await_args_list) == 1
    assert not any(call.args and call.args[0] == handlers.VIDEO_UPSELL_TEXT for call in retry.send.await_args_list)
    cards.assert_awaited_once()
    assert (await credit_db.get_user(456))['balance'] == 1


@pytest.mark.parametrize('stage', ['submit', 'wait', 'preview'])
async def test_direct_gift_error_has_traceback_and_is_shown_to_user(credit_db, monkeypatch, caplog, stage):
    from audio_service import AudioServiceError
    await credit_db.grant_free_trial(456)
    submit = AsyncMock(return_value='saved-direct-job')
    wait = AsyncMock(return_value='https://audio.example/full.mp3')
    if stage == 'submit':
        submit.side_effect = AudioServiceError('TTAPI Error: 503 - unavailable')
    elif stage == 'wait':
        wait.side_effect = TimeoutError('Таймаут ожидания TTAPI (100 секунд)')
    else:
        monkeypatch.setattr(handlers, 'make_protected_preview', AsyncMock(side_effect=RuntimeError('ffmpeg unavailable')))
    router = create_router(song_generator=submit, song_waiter=wait)
    context = await tt_context('gift')
    await context.set_state(PosterStates.waiting_for_gift_bday)
    event = message_event('Аня, любит горы')
    await call_handler(router, 'collect_gift_details', event, context)
    assert any(r.exc_info and r.message.startswith('ОШИБКА ГЕНЕРАЦИИ ПЕСНИ:') for r in caplog.records)
    assert event.message.answer.await_args.args[0].startswith('❌ Ошибка:')
    assert await context.get_state() == PosterStates.waiting_for_gift_bday
    order = await credit_db.get_audio_order((await context.get_data())['audio_checkout_id'])
    assert order['running'] == 0 and order['status'] == 'pending'
    assert await credit_db.pending_audio_orders() == []
    assert (await credit_db.get_user(456))['balance'] == 1
    if stage == 'wait':
        assert order['job_id'] == 'saved-direct-job'
        wait.side_effect = None
        retry = message_event('Аня, любит горы')
        await call_handler(router, 'collect_gift_details', retry, context)
        submit.assert_awaited_once()
        assert wait.await_count == 2
        assert (await credit_db.get_audio_order(order['order_id']))['preview_delivered'] == 1
    if stage == 'submit':
        await call_handler(router, 'collect_gift_details', message_event('Аня, любит горы'), context)
        submit.assert_awaited_once()  # Не повторяем POST с неизвестным исходом.


async def test_direct_gift_handler_waits_and_worker_cannot_claim_its_order(credit_db):
    import asyncio
    from audio_orders import PaidAudioWorker
    entered, release = asyncio.Event(), asyncio.Event()
    async def wait(job):
        entered.set()
        await release.wait()
        return 'https://audio.example/full.mp3'
    submit = AsyncMock(return_value='direct-job')
    router = create_router(song_generator=submit, song_waiter=wait)
    context = await tt_context('gift')
    await context.set_state(PosterStates.waiting_for_gift_bday)
    event = message_event('Аня, любит горы')
    task = asyncio.create_task(call_handler(router, 'collect_gift_details', event, context))
    try:
        await entered.wait()
        assert not task.done()
        order_id = (await context.get_data())['audio_checkout_id']
        saved = await credit_db.get_audio_order(order_id)
        assert saved['job_id'] == 'direct-job' and saved['running'] == 1
        assert saved['preview_requested'] == 0
        assert order_id not in await credit_db.pending_audio_orders()
        other_submit = AsyncMock()
        await PaidAudioWorker(event.bot, generator=other_submit).process(order_id)
        other_submit.assert_not_awaited()
    finally:
        release.set()
        await task
    saved = await credit_db.get_audio_order(order_id)
    assert saved['running'] == 0 and saved['preview_delivered'] == 1
    assert 'https://audio.example/full.mp3' not in event.bot.send_message.await_args.kwargs['text']
    assert 'https://preview.example/clip.mp3' in event.bot.send_message.await_args.kwargs['text']
    submit.assert_awaited_once()


@pytest.mark.parametrize('payload,field,counter', [
    ('audio_fun_ny', 'song_scenario', 'audio_scen_ny_fun'),
    ('audio_gift_ny', 'gift_scenario', 'audio_scen_ny_gift'),
])
async def test_new_year_audio_scenarios_use_correct_fsm_prompt_and_stats(credit_db, payload, field, counter):
    router = create_router(admin_user_id=999)
    context = await ready_context()
    await dispatch_callback(router, callback_event(payload), context)
    assert (await context.get_data())[field] == 'ny'
    event = message_event('Аня, встречаем Новый год всей семьёй')
    await call_handler(router, 'echo', event, context)
    assert (await credit_db.get_all_counters())[counter] == 1
    data = await context.get_data()
    if payload == 'audio_gift_ny':
        saved = await credit_db.get_audio_order(data['audio_checkout_id'])
        assert saved['scenario'] == 'ny' and saved['amount_kopecks'] == 49000
        assert saved['preview_delivered'] == 1
        assert '2-3 minutes' in saved['tags'] and 'семейные' in saved['prompt']
        args = handlers.generate_song.await_args.args
        assert 'С Новым годом' in args[0] and args[2].startswith('🎄 Новогодняя:')
    else:
        assert await context.get_state() == PosterStates.waiting_for_audio_fun_payment
        handlers.generate_song.assert_not_awaited()  # Короткая песня ждёт оплаты.
    admin = message_event('/stats')
    admin.get_ids = lambda: (123, 999)
    await call_handler(router, 'stats', admin)
    assert '- 🎄 Новогодняя: 1' in admin.message.answer.await_args.args[0]


@pytest.mark.parametrize('entry,price', [('cat_video', 299), ('upsell_video', 249), ('upsell_animate', 199)])
@pytest.mark.parametrize('admin_id,actor,bypass', [(456, 456, True), (456, 789, False), (None, 456, False)])
async def test_video_admin_bypass_only_after_data_and_song_and_never_for_regular_users(credit_db, entry, price, admin_id, actor, bypass):
    stub = AsyncMock(return_value='https://video.example/result.mp4')
    router = create_router(admin_user_id=admin_id, video_generator=stub)
    context = await ready_context()
    if entry != 'cat_video':
        await context.update_data(mode='mode_ny', last_image_url='https://cdn.example/card.png', card_text='С Новым годом!')
    event = callback_event(entry)
    event.get_ids = lambda: (123, actor)
    await dispatch_callback(router, event, context)
    stub.assert_not_awaited()
    if entry == 'cat_video':
        await dispatch_callback(router, callback_event('video_style:mode_ny'), context)
        await call_handler(router, 'collect_photo', photo_event(), context)
        await call_handler(router, 'collect_card_text', message_event('С Новым годом, Аня!'), context)
    song = callback_event('video_song:audio_fun_ny')
    song.get_ids = lambda: (123, actor)
    await dispatch_callback(router, song, context)
    if bypass:
        stub.assert_awaited_once()
        saved = stub.await_args.args[0]
        assert saved['video_song_scenario'] == 'audio_fun_ny' and saved['video_amount_kopecks'] == price * 100
        assert song.send.await_args_list[0].args[0] == '👑 Режим администратора: Оплата пропущена. Запускаю генерацию...'
        assert song.send.await_args_list[1].args[0] == '🎬 Ваше видео с песней готово: https://video.example/result.mp4'
        assert not any('payform' in str(call) for call in song.send.await_args_list)
        assert await context.get_state() == PosterStates.waiting_for_mode
        assert (await context.get_data())['video_complete'] is True
    else:
        stub.assert_not_awaited()
        pay = song.send.await_args.kwargs['attachments'][0].payload.buttons[0][0]
        assert f'{price} ₽' in pay.text and pay.url == 'https://link.payform.ru/'
        assert await context.get_state() == PosterStates.waiting_for_video_payment
    handlers.generate_song.assert_not_awaited()
    handlers.send_offline_conversion.assert_not_awaited()


async def test_video_admin_stub_failure_has_traceback_and_allows_song_retry(credit_db, caplog):
    stub = AsyncMock(side_effect=RuntimeError('test video failed'))
    router = create_router(admin_user_id=456, video_generator=stub)
    context = await ready_context()
    await context.update_data(mode='mode_ny', last_image_url='https://cdn.example/card.png')
    await dispatch_callback(router, callback_event('upsell_animate'), context)
    event = callback_event('video_song:audio_fun_ny')
    await dispatch_callback(router, event, context)
    assert any(r.exc_info and 'Ошибка генерации видео PiAPI' in r.message for r in caplog.records)
    assert await context.get_state() == PosterStates.waiting_for_video_song
    assert 'payform' not in str(event.send.await_args_list)


@pytest.mark.parametrize('payload,kind,price,duration', [
    ('audio_fun_ny', 'fun', 19900, '30-40 seconds'),
    ('audio_gift_ny', 'gift', 49000, '2-3 minutes'),
])
async def test_new_year_checkout_keeps_scenario_format_and_regular_price(credit_db, payload, kind, price, duration):
    async def checkout(user_id, fmt, scenario, details, style, prompt, tags, title, *, order_id):
        await credit_db.create_audio_order(order_id, user_id, fmt, scenario, details, style, prompt, tags, title)
        return 'https://pay.example/personal-ny'
    payments = SimpleNamespace(create_audio_payment_link=AsyncMock(side_effect=checkout))
    router = create_router(payments=payments)
    context = await ready_context()
    await dispatch_callback(router, callback_event(payload), context)
    await call_handler(router, 'echo', message_event('Аня, Новый год с семьёй'), context)
    payments.create_audio_payment_link.assert_awaited_once()
    saved = await credit_db.get_audio_order(payments.create_audio_payment_link.await_args.kwargs['order_id'])
    assert saved['scenario'] == 'ny' and saved['audio_format'] == kind and saved['amount_kopecks'] == price
    assert duration in saved['tags'] and 'С Новым годом' in saved['prompt']
    assert saved['status'] == 'pending'


def cartoon_photo(url='https://photos.example.com/first.png'):
    event = photo_event()
    event.message.body.attachments[0].payload = PhotoAttachmentPayload(photo_id=1, token='cartoon-photo', url=url)
    return event


async def start_cartoon(router, context, entry='cat_cartoon', mode='mode_ny'):
    await dispatch_callback(router, callback_event(entry), context)
    await dispatch_callback(router, callback_event('cartoon_style:' + mode), context)
    assert await context.get_state() == PosterStates.waiting_for_avatar_photo


async def test_cartoon_main_menu_order_and_names_are_exact():
    buttons = handlers.main_keyboard().payload.buttons
    assert [(button.text, button.payload) for row in buttons for button in row] == [
        ('🎬 Ожившая открытка (Видео) — Новинка 🔥', 'cat_video'),
        ('🎬 Мульт-открытка (10-15 сек) — Новинка 🔥', 'cat_cartoon'),
        ('🎬 Мульт-поздравление (30 сек) — Новинка 🔥', 'cat_cartoon_long'),
        ('📸 Нейрофотосессия', 'cat_photo'), ('🎵 Аудиопесни', 'cat_audio'), ('🎁 Открытки', 'cat_cards'),
        ('🔮 Карта Желаний', 'cat_vision'), ('🧘 Медитации', 'cat_meditation'), ('🖼 Примеры', 'show_examples'), ('🆘 Помощь', 'show_help')]


@pytest.mark.parametrize('entry,price,minutes', [('cat_cartoon', 890, '5-10'), ('cat_cartoon_long', 1690, '15-20')])
@pytest.mark.parametrize('approve_second', [False, True])
async def test_cartoon_avatars_approval_short_songs_and_regular_payment(credit_db, entry, price, minutes, approve_second):
    await credit_db.grant_free_trial(456)
    avatar = AsyncMock(side_effect=['https://cdn.example/avatar1.png', 'https://cdn.example/avatar2.png'])
    video = AsyncMock()
    router = create_router(avatar_generator=avatar, cartoon_video_generator=video, admin_user_id=999)
    context = await ready_context()
    await start_cartoon(router, context, entry, 'mode_modern_card')
    first = cartoon_photo()
    await call_handler(router, 'echo', first, context)
    assert await context.get_state() == PosterStates.waiting_for_avatar_review
    markup = first.message.answer.await_args.kwargs['attachments']
    assert len(markup) == 2  # одно изображение и одна клавиатура
    assert [row[0].payload for row in markup[1].payload.buttons] == ['avatar_ok_1', 'avatar_bad_1']
    attempt = 1
    if approve_second:
        await dispatch_callback(router, callback_event('avatar_bad_1'), context)
        assert await context.get_state() == PosterStates.waiting_for_avatar_photo
        await call_handler(router, 'echo', cartoon_photo(), context)
        assert avatar.await_count == 1  # то же фото просим заменить
        second = cartoon_photo('https://photos.example.com/second.png')
        await call_handler(router, 'echo', second, context)
        assert [row[0].payload for row in second.message.answer.await_args.kwargs['attachments'][1].payload.buttons] == ['avatar_ok_2', 'avatar_bad_2']
        attempt = 2
    accepted = callback_event(f'avatar_ok_{attempt}')
    await dispatch_callback(router, accepted, context)
    data = await context.get_data()
    assert data['chosen_avatar'] == f'https://cdn.example/avatar{attempt}.png'
    assert data['cartoon_mode'] == 'mode_modern_card'
    assert accepted.send.await_args.args[0] == 'Теперь выберите песню для вашего мультфильма:'
    song_buttons = accepted.send.await_args.kwargs['attachments'][0].payload.buttons
    assert [row[0].payload for row in song_buttons[:-1]] == ['cartoon_song:' + key for key in handlers.AUDIO_FUN_SCENARIOS]
    pay = callback_event('cartoon_song:audio_fun_ny')
    await dispatch_callback(router, pay, context)
    assert pay.send.await_args.args[0] == f'Отлично! Стоимость — {price} ₽. Это займет около {minutes} минут.'
    button = pay.send.await_args.kwargs['attachments'][0].payload.buttons[0][0]
    assert button.text == f'Оплатить {price} ₽' and button.url == 'https://link.payform.ru/'
    assert (await context.get_data())['chosen_song'] == 'audio_fun_ny'
    assert avatar.await_count == attempt
    assert handlers.send_offline_conversion.await_count == attempt
    assert (await credit_db.get_user(456))['balance'] == 1
    assert (await credit_db.get_user(456))['total_generations'] == 0
    video.assert_not_awaited()
    handlers.generate_song.assert_not_awaited()


async def test_second_avatar_rejection_notifies_admin_once_with_client_id(credit_db):
    avatar = AsyncMock(side_effect=['https://cdn.example/one.png', 'https://cdn.example/two.png'])
    router = create_router(avatar_generator=avatar, admin_user_id=999)
    context = await ready_context()
    await start_cartoon(router, context)
    await call_handler(router, 'collect_cartoon_photo', cartoon_photo(), context)
    await dispatch_callback(router, callback_event('avatar_bad_1'), context)
    await call_handler(router, 'collect_cartoon_photo', cartoon_photo('https://photos.example.com/second.png'), context)
    rejected = callback_event('avatar_bad_2')
    await dispatch_callback(router, rejected, context)
    rejected.bot.send_message.assert_awaited_once()
    notice = rejected.bot.send_message.await_args.kwargs
    assert notice['user_id'] == 999 and 'ID клиента: 456' in notice['text']
    assert rejected.send.await_args.args[0] == 'Понимаем вас. Передаем заказ нашему художнику, он свяжется с вами в течение часа.'
    assert await context.get_state() == PosterStates.cartoon_manual_review
    repeat = callback_event('avatar_bad_2')
    await dispatch_callback(router, repeat, context)
    repeat.bot.send_message.assert_not_awaited()
    assert avatar.await_count == 2
    counters = await credit_db.get_all_counters()
    assert counters['avatar_bad_first'] == counters['avatar_bad_second'] == 1
    assert 'chosen_avatar' not in await context.get_data()


@pytest.mark.parametrize('entry', ['cat_cartoon', 'cat_cartoon_long'])
@pytest.mark.parametrize('admin_id', [456, None])
async def test_cartoon_admin_bypass_calls_video_stub_with_approved_avatar_and_song_url(credit_db, entry, admin_id):
    video = AsyncMock(return_value='https://video.example/test-result.mp4')
    router = create_router(avatar_generator=AsyncMock(return_value='https://cdn.example/avatar.png'),
                           cartoon_video_generator=video, admin_user_id=admin_id)
    context = await ready_context()
    await start_cartoon(router, context, entry)
    await call_handler(router, 'collect_cartoon_photo', cartoon_photo(), context)
    await dispatch_callback(router, callback_event('avatar_ok_1'), context)
    result = callback_event('cartoon_song:audio_fun_friend')
    await dispatch_callback(router, result, context)
    if admin_id == 456:
        video.assert_awaited_once_with('https://cdn.example/avatar.png', handlers.CARTOON_TEST_SONG_URL + '?scenario=audio_fun_friend')
        assert result.send.await_args_list[0].args[0] == '👑 Режим администратора: Оплата пропущена...'
        assert 'https://video.example/test-result.mp4' in result.send.await_args_list[1].args[0]
        assert not any('payform.ru' in str(call) for call in result.send.await_args_list)
        assert await context.get_state() == PosterStates.waiting_for_mode
    else:
        video.assert_not_awaited()
        assert 'payform.ru' in str(result.send.await_args_list)
        assert await context.get_state() == PosterStates.waiting_for_cartoon_payment
    handlers.generate_song.assert_not_awaited()


async def test_failed_avatar_delivery_reuses_generated_image_without_another_openai_call(credit_db, max_image_upload):
    avatar = AsyncMock(return_value='https://cdn.example/avatar.png')
    router = create_router(avatar_generator=avatar)
    context = await ready_context()
    await start_cartoon(router, context)
    max_image_upload.side_effect = [RuntimeError('upload failed'), AttachmentUpload(type='image', payload={'token': 'retry-token'})]
    await call_handler(router, 'collect_cartoon_photo', cartoon_photo(), context)
    assert await context.get_state() == PosterStates.waiting_for_avatar_photo
    await call_handler(router, 'collect_cartoon_photo', cartoon_photo(), context)
    assert await context.get_state() == PosterStates.waiting_for_avatar_review
    avatar.assert_awaited_once()
    handlers.send_offline_conversion.assert_awaited_once()


async def test_cartoon_interest_stats_exclude_admin_and_do_not_count_old_buttons(credit_db):
    router = create_router(admin_user_id=999, avatar_generator=AsyncMock(return_value='https://cdn.example/avatar.png'))
    for actor in (456, 999):
        context = await ready_context()
        async def click(payload):
            event = callback_event(payload)
            event.get_ids = lambda: (123, actor)
            await dispatch_callback(router, event, context)
        async def photo(url):
            event = cartoon_photo(url)
            event.get_ids = lambda: (123, actor)
            await call_handler(router, 'collect_cartoon_photo', event, context)
        # Первый заказ: принят первый аватар. Повторная старая кнопка не считается.
        await click('cat_cartoon')
        await click('cartoon_style:mode_ny')
        await photo('https://photos.example.com/first.png')
        await click('avatar_ok_1')
        await click('avatar_ok_1')
        # Второй заказ: принят второй аватар.
        await click('cat_cartoon_long')
        await click('cartoon_style:mode_ny')
        await photo('https://photos.example.com/first.png')
        await click('avatar_bad_1')
        await photo('https://photos.example.com/second.png')
        await click('avatar_ok_2')
        # Третий заказ: оба отклонены, передача художнику.
        await click('cat_cartoon')
        await click('cartoon_style:mode_ny')
        await photo('https://photos.example.com/first.png')
        await click('avatar_bad_1')
        await photo('https://photos.example.com/second.png')
        await click('avatar_bad_2')
    event = message_event('/stats')
    event.get_ids = lambda: (123, 999)
    await call_handler(router, 'stats', event)
    text = event.message.answer.await_args.args[0]
    assert '🎬 Интерес к Мульт-услугам:' in text
    for label, count in [('Мульт-открытка (клики)', 2), ('Мульт-поздравление (клики)', 1),
                         ('Первый аватар — подошёл', 1), ('Первый аватар — не похож', 2),
                         ('Второй аватар — подошёл', 1), ('Второй аватар — не подошёл', 1)]:
        assert f'- {label}: {count}' in text
    assert not any(key.startswith('gen_') for key in await credit_db.get_all_counters())


@pytest.mark.parametrize('configured_admin', [True, False])
async def test_cartoon_failed_handoff_is_retryable_and_never_claims_artist_was_notified(credit_db, caplog, configured_admin):
    router = create_router(avatar_generator=AsyncMock(return_value='https://cdn.example/avatar.png'),
                           admin_user_id=999 if configured_admin else None)
    context = await ready_context()
    await start_cartoon(router, context)
    await call_handler(router, 'collect_cartoon_photo', cartoon_photo(), context)
    await dispatch_callback(router, callback_event('avatar_bad_1'), context)
    await call_handler(router, 'collect_cartoon_photo', cartoon_photo('https://photos.example.com/second.png'), context)
    rejected = callback_event('avatar_bad_2')
    rejected.bot.send_message.side_effect = RuntimeError('admin notification failed')
    await dispatch_callback(router, rejected, context)
    assert 'Не удалось отправить заявку художнику' in rejected.send.await_args.args[0]
    assert not (await context.get_data()).get('cartoon_handoff_sent')
    assert any(record.exc_info for record in caplog.records)
    if configured_admin:
        retry = callback_event('cartoon_retry_handoff')
        await dispatch_callback(router, retry, context)
        assert (await context.get_data())['cartoon_handoff_sent'] is True
        assert retry.bot.send_message.await_args.kwargs['user_id'] == 999
        assert 'в течение часа' in retry.send.await_args.args[0]
        assert (await credit_db.get_all_counters())['avatar_bad_second'] == 1
    else:
        rejected.bot.send_message.assert_not_awaited()


async def test_cartoon_real_dispatcher_routes_photos_and_cancel_before_state_handler(credit_db, monkeypatch, event_replies):
    from maxapi import Dispatcher
    from maxapi.context.isolation import SimpleEventIsolation
    from maxapi.types import MessageCallback, MessageCreated, User
    monkeypatch.setattr(handlers, '_reply', event_replies)
    avatar = AsyncMock(return_value='https://cdn.example/avatar.png')
    router = create_router(avatar_generator=avatar)
    dp = Dispatcher(storage=MemoryContext, event_isolation=SimpleEventIsolation())
    dp.include_routers(router)
    bot = SimpleNamespace(me=User(user_id=999, first_name='Bot', is_bot=True),
                          send_message=AsyncMock(return_value=object()), send_callback=AsyncMock())
    user = {'user_id': 456, 'first_name': 'Test', 'is_bot': False}
    def message(text=None, attachments=None, sender=None):
        return {'sender': sender or user, 'recipient': {'chat_id': 123, 'chat_type': 'dialog'},
                'timestamp': 1, 'body': {'mid': 'cartoon-test', 'seq': 1, 'text': text, 'attachments': attachments}}
    async def click(payload):
        await dp.handle(MessageCallback(timestamp=1, bot=bot, message=message(sender=bot.me.model_dump()),
            callback={'timestamp': 1, 'callback_id': 'cartoon', 'payload': payload, 'user': user}))
    try:
        await click('cat_cartoon')
        await click('cartoon_style:mode_ny')
        context = dp.fsm.get_context(chat_id=123, user_id=456)
        await dp.handle(MessageCreated(timestamp=2, bot=bot, message=message('/cancel')))
        assert await context.get_data() == {}
        assert await context.get_state() == PosterStates.waiting_for_mode
        avatar.assert_not_awaited()
        await click('cat_cartoon_long')
        await click('cartoon_style:mode_ny')
        await dp.handle(MessageCreated(timestamp=3, bot=bot, message=message(attachments=[{
            'type': 'image', 'payload': {'photo_id': 1, 'token': 'source', 'url': 'https://photos.example.com/source.png'}}])))
        assert await context.get_state() == PosterStates.waiting_for_avatar_review
        avatar.assert_awaited_once()
        await click('avatar_ok_1')
        assert (await context.get_data())['chosen_avatar'] == 'https://cdn.example/avatar.png'
        await click('cartoon_song:audio_fun_ny')
        assert await context.get_state() == PosterStates.waiting_for_cartoon_payment
        assert '1690 ₽' in bot.send_message.await_args.kwargs['text']
        await dp.handle(MessageCreated(timestamp=4, bot=bot, message=message('/cancel')))
        assert await context.get_data() == {}
    finally:
        await dp.shutdown()


async def test_cartoon_cancel_during_avatar_generation_does_not_restore_cleared_session(credit_db):
    import asyncio
    entered, release = asyncio.Event(), asyncio.Event()
    async def avatar(photo_url, mode):
        entered.set()
        await release.wait()
        return 'https://cdn.example/avatar.png'
    router = create_router(avatar_generator=avatar)
    context = await ready_context()
    await start_cartoon(router, context)
    event = cartoon_photo()
    task = asyncio.create_task(call_handler(router, 'collect_cartoon_photo', event, context))
    try:
        await entered.wait()
        await call_handler(router, 'cancel', message_event('/cancel'), context)
    finally:
        release.set()
        await task
    assert await context.get_state() == PosterStates.waiting_for_mode
    assert await context.get_data() == {}
    assert not any(call.args and call.args[0] == 'Ваш мульт-аватар готов! Подходит?' for call in event.message.answer.await_args_list)


TEACHER_TEST_LYRICS = '[Verse 1]\nВаш труд согревает сердца\n[Chorus]\nСпасибо Вам\n[Verse 2]\nИрина Петровна, Вы дарите знания\n[Chorus]\nСпасибо Вам\n[Bridge]\nЗа терпение и поддержку\n[Chorus]\nСпасибо Вам'


async def prepare_teacher_video(router, context, entry):
    if entry == 'cat_video':
        await dispatch_callback(router, callback_event(entry), context)
        await dispatch_callback(router, callback_event('video_style:mode_ny'), context)
        await call_handler(router, 'collect_photo', photo_event(), context)
        await call_handler(router, 'collect_card_text', message_event('С праздником, Ирина Петровна!'), context)
    else:
        await context.update_data(mode='mode_ny', last_image_url='https://cdn.example/card.png',
                                  photo={'url': 'https://photos.example.com/source.png'}, card_text='С праздником, Ирина Петровна!')
        await dispatch_callback(router, callback_event(entry), context)


@pytest.mark.parametrize('entry,price', [('cat_video', 299), ('upsell_animate', 199), ('upsell_video', 249)])
@pytest.mark.parametrize('admin', [False, True])
async def test_video_teacher_collects_details_creates_formal_text_and_keeps_card_price(credit_db, entry, price, admin):
    writer = AsyncMock(return_value=TEACHER_TEST_LYRICS)
    video = AsyncMock(return_value='https://video.example/result.mp4')
    router = create_router(admin_user_id=456 if admin else 999, lyric_writer=writer, video_generator=video)
    context = await ready_context()
    await prepare_teacher_video(router, context, entry)
    original = await context.get_data()
    chosen = callback_event('audio_video_teacher')
    await dispatch_callback(router, chosen, context)
    assert chosen.send.await_args.args[0] == 'Напишите имя тренера/учителя, вид спорта или предмет, и одно качество или воспоминание (например: «Ирина Петровна, математика, строгая но справедливая»).'
    assert await context.get_state() == PosterStates.waiting_for_video_teacher_details
    assert 'payform' not in str(chosen.send.await_args_list)
    event = message_event('Ирина Петровна, математика, строгая но справедливая')
    await call_handler(router, 'echo', event, context)
    writer.assert_awaited_once()
    notes, tags, title = writer.await_args.args
    assert 'Ирина Петровна' in notes and '30 seconds' in tags and 'elegant pop ballad' in tags
    data = await context.get_data()
    assert data['photo'] == original['photo'] and data['card_text'] == original['card_text']
    assert data['video_amount_kopecks'] == price * 100 and data['video_product'] == original['video_product']
    assert data['video_song_lyrics'] == TEACHER_TEST_LYRICS and data['video_song_scenario'] == 'audio_video_teacher'
    assert any(call.args and TEACHER_TEST_LYRICS in call.args[0] for call in event.message.answer.await_args_list)
    if admin:
        video.assert_awaited_once()
        assert video.await_args.args[0]['video_song_lyrics'] == TEACHER_TEST_LYRICS
        assert 'payform' not in str(event.message.answer.await_args_list)
    else:
        video.assert_not_awaited()
        pay = event.message.answer.await_args.kwargs['attachments'][0].payload.buttons[0][0]
        assert f'{price} ₽' in pay.text and pay.url == 'https://link.payform.ru/'
    handlers.generate_song.assert_not_awaited()


async def test_gift_teacher_uses_formal_prompt_and_existing_paid_gift_preview(credit_db):
    writer = AsyncMock(return_value=TEACHER_TEST_LYRICS)
    router = create_router(lyric_writer=writer)
    context = await ready_context()
    selected = callback_event('audio_gift_teacher')
    await dispatch_callback(router, selected, context)
    assert selected.send.await_args.args[0] == handlers.TEACHER_DETAILS_PROMPT
    assert await context.get_state() == PosterStates.waiting_for_gift_teacher
    assert (await context.get_data())['gift_scenario'] == 'teacher'
    await call_handler(router, 'echo', message_event('Ирина Петровна, математика, поддерживает учеников'), context)
    data = await context.get_data()
    saved = await credit_db.get_audio_order(data['audio_checkout_id'])
    assert saved['scenario'] == 'teacher' and saved['amount_kopecks'] == 49000 and saved['preview_delivered'] == 1
    assert '2-3 minutes' in saved['tags'] and 'elegant pop ballad' in saved['tags']
    assert writer.await_args.args[2].startswith(handlers.TEACHER_SCENARIO_TITLE + ':')
    assert handlers.generate_song.await_args.args[0] == TEACHER_TEST_LYRICS
    assert (await credit_db.get_all_counters())['audio_scen_teacher_gift'] == 1


@pytest.mark.parametrize('entry', ['cat_video', 'upsell_animate', 'upsell_video'])
async def test_video_menus_include_all_eight_required_scenarios(entry):
    router, context = create_router(), await ready_context()
    await prepare_teacher_video(router, context, entry)
    # Повторяем показ без очистки выбранного шаблона.
    if entry == 'cat_video':
        event = message_event('С праздником, Ирина!')
        await context.set_state(PosterStates.waiting_for_card_text)
        await call_handler(router, 'collect_card_text', event, context)
        markup = event.message.answer.await_args.kwargs['attachments'][0]
    else:
        event = callback_event(entry)
        await dispatch_callback(router, event, context)
        markup = event.send.await_args.kwargs['attachments'][0]
    buttons = [row[0] for row in markup.payload.buttons]
    expected = ['🎤 Начальник года', '💖 Лучшая подруга', '🐱 Гимн кота', '🎓 Вечный студент',
                '⏰ Поздравление на бегу', '🌀 Абсурдный мем', '🎄 Новогодняя', '👨‍🏫 Для тренера / учителя']
    assert all(label in [button.text for button in buttons] for label in expected)
    assert next(button for button in buttons if button.text == expected[-1]).payload == 'audio_video_teacher'


async def test_teacher_video_failure_is_visible_and_preserves_inputs_for_retry(credit_db, caplog):
    writer = AsyncMock(side_effect=RuntimeError('LLM unavailable'))
    router = create_router(lyric_writer=writer)
    context = await ready_context()
    await prepare_teacher_video(router, context, 'upsell_video')
    await dispatch_callback(router, callback_event('audio_video_teacher'), context)
    event = message_event('Ирина Петровна, математика')
    await call_handler(router, 'echo', event, context)
    assert await context.get_state() == PosterStates.waiting_for_video_teacher_details
    assert (await context.get_data())['video_amount_kopecks'] == 24900
    assert (await context.get_data())['last_image_url'] == 'https://cdn.example/card.png'
    assert any(record.exc_info for record in caplog.records)
    assert 'Не удалось создать текст песни' in event.message.answer.await_args.args[0]
    assert 'payform' not in str(event.message.answer.await_args_list)


async def test_teacher_and_newyear_video_interest_counters_exclude_admin(credit_db):
    router = create_router(admin_user_id=999)
    for actor in (456, 999):
        context = await ready_context()
        async def click(payload):
            event = callback_event(payload)
            event.get_ids = lambda: (123, actor)
            await dispatch_callback(router, event, context)
        await click('audio_gift_teacher')
        await context.update_data(mode='mode_ny', last_image_url='https://cdn.example/card.png')
        await click('upsell_video')
        await click('audio_video_teacher')
        await click('upsell_video')
        await click('video_song:audio_fun_ny')
    counters = await credit_db.get_all_counters()
    for name in ('audio_scen_teacher_gift', 'audio_scen_teacher_video', 'audio_scen_ny_video'):
        assert counters[name] == 2
        assert (await credit_db.get_all_counters(admin_id=999))[name] == 1
    event = message_event('/stats')
    event.get_ids = lambda: (123, 999)
    await call_handler(router, 'stats', event)
    text = event.message.answer.await_args.args[0]
    gift = text.split('Подарочный формат):\n', 1)[1].split('Интерес к Аудиопесням (для видео)', 1)[0]
    video = text.split('Интерес к Аудиопесням (для видео):\n', 1)[1].split('Интерес к Мульт-услугам', 1)[0]
    assert '- 👨‍🏫 Для тренера / учителя: 1' in gift
    assert '- 👨‍🏫 Для тренера / учителя: 1' in video and '- 🎄 Новогодняя: 1' in video


async def test_switching_video_teacher_to_newyear_clears_teacher_lyrics():
    router = create_router(lyric_writer=AsyncMock(return_value=TEACHER_TEST_LYRICS))
    context = await ready_context()
    await prepare_teacher_video(router, context, 'upsell_video')
    await dispatch_callback(router, callback_event('audio_video_teacher'), context)
    await call_handler(router, 'echo', message_event('Ирина Петровна, математика'), context)
    assert (await context.get_data())['video_song_lyrics']
    await dispatch_callback(router, callback_event('video_song:audio_fun_ny'), context)
    data = await context.get_data()
    assert data['video_song_scenario'] == 'audio_fun_ny'
    assert data['video_song_lyrics'] is None and data['video_teacher_details'] is None
    assert data['video_amount_kopecks'] == 24900 and data['last_image_url'] == 'https://cdn.example/card.png'


async def test_video_teacher_real_dispatcher_routes_details_and_cancel_before_state_handler(credit_db, monkeypatch, event_replies):
    from maxapi import Dispatcher
    from maxapi.context.isolation import SimpleEventIsolation
    from maxapi.types import MessageCallback, MessageCreated, User
    monkeypatch.setattr(handlers, '_reply', event_replies)
    writer = AsyncMock(return_value=TEACHER_TEST_LYRICS)
    router = create_router(lyric_writer=writer)
    dp = Dispatcher(storage=MemoryContext, event_isolation=SimpleEventIsolation())
    dp.include_routers(router)
    bot = SimpleNamespace(me=User(user_id=999, first_name='Bot', is_bot=True),
                          send_message=AsyncMock(return_value=object()), send_callback=AsyncMock())
    user = {'user_id': 456, 'first_name': 'Test', 'is_bot': False}
    def message(text=None, sender=None):
        return {'sender': sender or user, 'recipient': {'chat_id': 123, 'chat_type': 'dialog'},
                'timestamp': 1, 'body': {'mid': 'teacher-test', 'seq': 1, 'text': text}}
    async def click(payload):
        await dp.handle(MessageCallback(timestamp=1, bot=bot, message=message(sender=bot.me.model_dump()),
            callback={'timestamp': 1, 'callback_id': 'teacher', 'payload': payload, 'user': user}))
    try:
        context = dp.fsm.get_context(chat_id=123, user_id=456)
        for text in ('/cancel', 'Ирина Петровна, математика'):
            await context.update_data(mode='mode_ny', last_image_url='https://cdn.example/card.png')
            await click('upsell_animate')
            await click('audio_video_teacher')
            assert await context.get_state() == PosterStates.waiting_for_video_teacher_details
            await dp.handle(MessageCreated(timestamp=2, bot=bot, message=message(text)))
            if text == '/cancel':
                assert await context.get_data() == {}
                writer.assert_not_awaited()
            else:
                assert await context.get_state() == PosterStates.waiting_for_video_payment
                assert (await context.get_data())['video_song_lyrics'] == TEACHER_TEST_LYRICS
                assert '199 ₽' in bot.send_message.await_args.kwargs['text']
    finally:
        await dp.shutdown()


@pytest.mark.parametrize('action', ['cancel', 'reopen'])
async def test_late_teacher_lyrics_do_not_overwrite_cancelled_or_reopened_menu(action):
    import asyncio
    entered, release = asyncio.Event(), asyncio.Event()
    async def writer(notes, tags, title):
        entered.set()
        await release.wait()
        return TEACHER_TEST_LYRICS
    router, context = create_router(lyric_writer=writer), await ready_context()
    await prepare_teacher_video(router, context, 'upsell_video')
    await dispatch_callback(router, callback_event('audio_video_teacher'), context)
    event = message_event('Ирина Петровна, математика')
    task = asyncio.create_task(call_handler(router, 'echo', event, context))
    try:
        await entered.wait()
        if action == 'cancel':
            await call_handler(router, 'cancel', message_event('/cancel'), context)
        else:
            await dispatch_callback(router, callback_event('upsell_video'), context)
    finally:
        release.set()
        await task
    assert not any(call.args and TEACHER_TEST_LYRICS in call.args[0] for call in event.message.answer.await_args_list)
    assert await context.get_state() == (PosterStates.waiting_for_mode if action == 'cancel' else PosterStates.waiting_for_video_song)


@pytest.mark.parametrize('mode,field,info,question,event_name', [
    ('mode_teacher', 'teacher_info', 'Ирина Петровна, математика', 'Напишите имя учителя и его предмет (например: «Ирина Петровна, математика»).', 'gen_teacher'),
    ('mode_coach', 'coach_info', 'Сергей Иванович, футбол', 'Напишите имя тренера и вид спорта (например: «Сергей Иванович, футбол»).', 'gen_coach'),
])
async def test_role_cards_collect_context_before_photo_then_generate_and_count(credit_db, mode, field, info, question, event_name):
    await credit_db.add_credits(456, 2)
    card = AsyncMock(return_value=['https://cdn.example/role-card.png'])
    router, context = create_router(postcard_generator=card), await ready_context()
    selected = callback_event(mode)
    await dispatch_callback(router, selected, context)
    assert selected.send.await_args.args[0] == question
    assert await context.get_state() == PosterStates.waiting_for_card_role_info
    await call_handler(router, 'echo', message_event(info), context)
    assert (await context.get_data())[field] == info
    assert await context.get_state() == PosterStates.waiting_for_photo
    await call_handler(router, 'collect_photo', photo_event(), context)
    assert await context.get_state() == PosterStates.waiting_for_card_text
    card.assert_not_awaited()
    text = message_event('Спасибо за Ваш труд!')
    await call_handler(router, 'echo', text, context)
    card.assert_awaited_once_with('https://photos.example.com/source.png', mode, 'Спасибо за Ваш труд!', theme_info=info)
    assert text.bot.send_message.await_args.kwargs['text'] == 'Ваша открытка готова!'
    assert (await credit_db.get_user(456))['balance'] == 1
    assert (await credit_db.get_all_counters())[event_name] == 1
    stats = message_event('/stats')
    await call_handler(create_router(admin_user_id=456), 'stats', stats)
    # Админский снимок исключает собственные тестовые генерации.
    assert ('- Учителям: 0' if mode == 'mode_teacher' else '- Тренерам: 0') in stats.message.answer.await_args.args[0]


@pytest.mark.parametrize('mode,field,info,event_name', [
    ('mode_teacher', 'teacher_info', 'Ирина Петровна, химия', 'gen_teacher'),
    ('mode_coach', 'coach_info', 'Сергей Иванович, плавание', 'gen_coach'),
])
async def test_video_role_card_generates_marked_preview_before_music_without_spending_card_credit(credit_db, mode, field, info, event_name):
    await credit_db.grant_free_trial(456)
    card = AsyncMock(return_value=['https://cdn.example/preview.png'])
    router, context = create_router(postcard_generator=card), await ready_context()
    await dispatch_callback(router, callback_event('cat_video'), context)
    await dispatch_callback(router, callback_event('video_style:' + mode), context)
    assert await context.get_state() == PosterStates.waiting_for_card_role_info
    await call_handler(router, 'echo', message_event(info), context)
    await call_handler(router, 'collect_photo', photo_event(), context)
    text = message_event('Спасибо!')
    await call_handler(router, 'echo', text, context)
    card.assert_awaited_once_with('https://photos.example.com/source.png', mode, 'Спасибо!', theme_info=info, preview=True)
    replies = text.message.answer.await_args_list
    assert any(call.args and call.args[0] == 'Превью открытки для видео (PREVIEW):' for call in replies)
    assert replies[-1].args[0] == '🎵 Выберите сценарий песни для видео-поздравления:'
    assert await context.get_state() == PosterStates.waiting_for_video_song
    data = await context.get_data()
    assert data[field] == info and data['video_preview_url'] == 'https://cdn.example/preview.png'
    assert data['video_amount_kopecks'] == 29900
    assert (await credit_db.get_user(456))['balance'] == 1
    assert (await credit_db.get_all_counters())[event_name] == 1
    handlers.send_offline_conversion.assert_awaited_once_with(456, goal_id='bot_generation')
    await dispatch_callback(router, callback_event('video_song:audio_fun_ny'), context)
    assert await context.get_state() == PosterStates.waiting_for_video_payment
    assert (await context.get_data())[field] == info
    handlers.generate_song.assert_not_awaited()


async def test_role_video_preview_retry_keeps_image_and_counts_once(credit_db, max_image_upload):
    card = AsyncMock(return_value=['https://cdn.example/preview.png'])
    router, context = create_router(postcard_generator=card), await ready_context()
    await dispatch_callback(router, callback_event('cat_video'), context)
    await dispatch_callback(router, callback_event('video_style:mode_teacher'), context)
    await call_handler(router, 'echo', message_event('Ирина Петровна, математика'), context)
    await call_handler(router, 'collect_photo', photo_event(), context)
    max_image_upload.side_effect = [RuntimeError('MAX upload failed'), AttachmentUpload(type='image', payload={'token': 'retry'})]
    await call_handler(router, 'echo', message_event('Спасибо!'), context)
    assert await context.get_state() == PosterStates.waiting_for_card_text
    assert (await context.get_data())['video_preview_url']
    await call_handler(router, 'echo', message_event('Спасибо!'), context)
    assert await context.get_state() == PosterStates.waiting_for_video_song
    card.assert_awaited_once()
    assert (await credit_db.get_all_counters())['gen_teacher'] == 1
    handlers.send_offline_conversion.assert_awaited_once()


@pytest.mark.parametrize('video', [False, True])
@pytest.mark.parametrize('info', ['', ' ', 'а'*501])
async def test_missing_or_oversized_role_info_never_generates(video, info):
    card = AsyncMock()
    router, context = create_router(postcard_generator=card), await ready_context()
    if video:
        await dispatch_callback(router, callback_event('cat_video'), context)
    await dispatch_callback(router, callback_event('video_style:mode_coach' if video else 'mode_coach'), context)
    await call_handler(router, 'echo', message_event(info), context)
    assert await context.get_state() == PosterStates.waiting_for_card_role_info
    assert 'coach_info' not in await context.get_data()
    card.assert_not_awaited()


async def test_role_preview_generation_failure_does_not_increment_or_lose_context(credit_db):
    card = AsyncMock(side_effect=RuntimeError('OpenAI failed'))
    router, context = create_router(postcard_generator=card), await ready_context()
    await dispatch_callback(router, callback_event('cat_video'), context)
    await dispatch_callback(router, callback_event('video_style:mode_coach'), context)
    await call_handler(router, 'echo', message_event('Сергей, футбол'), context)
    await call_handler(router, 'collect_photo', photo_event(), context)
    await call_handler(router, 'echo', message_event('Спасибо!'), context)
    data = await context.get_data()
    assert data['coach_info'] == 'Сергей, футбол' and data['photo']['url']
    assert await context.get_state() == PosterStates.waiting_for_card_text
    assert (await credit_db.get_all_counters()).get('gen_coach', 0) == 0
    handlers.send_offline_conversion.assert_not_awaited()


async def test_cancel_during_role_preview_drops_late_result(credit_db):
    import asyncio
    entered, release = asyncio.Event(), asyncio.Event()

    async def generate(*args, **kwargs):
        entered.set()
        await release.wait()
        return ['https://cdn.example/late-preview.png']

    router, context = create_router(postcard_generator=generate), await ready_context()
    await dispatch_callback(router, callback_event('cat_video'), context)
    await dispatch_callback(router, callback_event('video_style:mode_teacher'), context)
    await call_handler(router, 'echo', message_event('Ирина, математика'), context)
    await call_handler(router, 'collect_photo', photo_event(), context)
    text = message_event('Спасибо!')
    task = asyncio.create_task(call_handler(router, 'echo', text, context))
    try:
        await asyncio.wait_for(entered.wait(), timeout=5)
        await call_handler(router, 'cancel', message_event('/cancel'), context)
    finally:
        release.set()
        await task
    assert await context.get_state() == PosterStates.waiting_for_mode
    assert not (await context.get_data()).get('video_preview_url')
    assert (await credit_db.get_all_counters()).get('gen_teacher', 0) == 0
    handlers.send_offline_conversion.assert_not_awaited()
    assert not any(call.args and 'PREVIEW' in call.args[0] for call in text.message.answer.await_args_list)


@pytest.fixture(autouse=True)
def piapi_animation(monkeypatch):
    animator = AsyncMock(return_value='https://video.example/animated.mp4')
    monkeypatch.setattr(handlers, 'generate_video_from_avatar', animator)
    return animator


@pytest.mark.parametrize('entry', ['cat_cartoon', 'cat_cartoon_long'])
async def test_default_cartoon_generator_uses_real_piapi_chain(credit_db, monkeypatch, entry):
    import json
    import httpx
    import respx
    import video_service
    monkeypatch.setattr(handlers, 'generate_video_from_avatar', video_service.generate_video_from_avatar)
    monkeypatch.setattr(video_service.Config, 'from_env', lambda: SimpleNamespace(piapi_key='piapi-test-key'))
    monkeypatch.setattr(video_service.asyncio, 'sleep', AsyncMock())
    router = create_router(admin_user_id=456, avatar_generator=AsyncMock(return_value='https://cdn.example/avatar.png'))
    context = await ready_context()
    await start_cartoon(router, context, entry)
    await call_handler(router, 'collect_cartoon_photo', cartoon_photo(), context)
    await dispatch_callback(router, callback_event('avatar_ok_1'), context)
    event = callback_event('cartoon_song:audio_fun_ny')
    with respx.mock:
        post = respx.post(video_service.TASK_URL).respond(200, json={'data': {'task_id': 'cartoon-real'}})
        get = respx.get(video_service.TASK_URL + '/cartoon-real').respond(200, json={'data': {'status': 'completed', 'output': {'video': 'https://video.example/kling.mp4'}}})
        await dispatch_callback(router, event, context)
        assert post.call_count == get.call_count == 1
        payload = json.loads(post.calls[0].request.content)
        assert payload['input'] == {'image_url': 'https://cdn.example/avatar.png',
            'prompt': 'The character is smiling and blinking naturally', 'version': '3.0-turbo', 'mode': 'std', 'duration': 5, 'enable_audio': False}
        assert post.calls[0].request.headers['X-API-Key'] == get.calls[0].request.headers['X-API-Key'] == 'piapi-test-key'
    assert 'https://video.example/kling.mp4' in event.send.await_args.args[0]
    button = event.send.await_args.kwargs['attachments'][0].payload.buttons[0][0]
    assert button.url == 'https://video.example/kling.mp4'
    assert 'Тестовая' not in event.send.await_args.args[0]
    assert await context.get_state() == PosterStates.waiting_for_mode


@pytest.mark.parametrize('entry', ['upsell_animate', 'upsell_video'])
async def test_default_card_upsell_uses_real_piapi_chain(credit_db, monkeypatch, entry):
    import json
    import respx
    import video_service
    monkeypatch.setattr(handlers, 'generate_video_from_avatar', video_service.generate_video_from_avatar)
    monkeypatch.setattr(video_service.Config, 'from_env', lambda: SimpleNamespace(piapi_key='piapi-test-key'))
    monkeypatch.setattr(video_service.asyncio, 'sleep', AsyncMock())
    card = AsyncMock()
    router, context = create_router(admin_user_id=456, postcard_generator=card), await ready_context()
    await context.update_data(mode='mode_ny', last_image_url='https://cdn.example/ready-card.png')
    await dispatch_callback(router, callback_event(entry), context)
    event = callback_event('video_song:audio_fun_ny')
    with respx.mock:
        post = respx.post(video_service.TASK_URL).respond(200, json={'data': {'task_id': 'card-real'}})
        respx.get(video_service.TASK_URL + '/card-real').respond(200, json={'data': {'status': 'completed', 'output': {'video': 'https://video.example/card.mp4'}}})
        await dispatch_callback(router, event, context)
        assert json.loads(post.calls[0].request.content)['input']['image_url'] == 'https://cdn.example/ready-card.png'
    card.assert_not_awaited()
    assert 'https://video.example/card.mp4' in event.send.await_args.args[0]
    assert (await context.get_data())['video_complete']


@pytest.mark.parametrize('mode', ['mode_ny', 'mode_teacher'])
async def test_direct_video_generates_clean_postcard_before_piapi(credit_db, piapi_animation, mode):
    card = AsyncMock(return_value=['https://cdn.example/clean-card.png'])
    router, context = create_router(admin_user_id=456, postcard_generator=card), await ready_context()
    await context.update_data(mode=mode, photo={'url': 'https://cdn.example/source.png'}, card_text='Спасибо!',
        video_preview_url='https://cdn.example/watermarked.png', video_product='direct', video_flow=True,
        teacher_info='Ирина Петровна, математика', video_amount_kopecks=29900)
    await context.set_state(PosterStates.waiting_for_video_song)
    await dispatch_callback(router, callback_event('video_song:audio_fun_ny'), context)
    kwargs = {'theme_info': 'Ирина Петровна, математика'} if mode == 'mode_teacher' else {}
    card.assert_awaited_once_with('https://cdn.example/source.png', mode, 'Спасибо!', **kwargs)
    piapi_animation.assert_awaited_once_with('https://cdn.example/clean-card.png')
    assert (await context.get_data())['video_source_image'] == 'https://cdn.example/clean-card.png'


async def test_card_piapi_failure_logs_traceback_and_reuses_clean_image(credit_db, piapi_animation, caplog):
    piapi_animation.side_effect = [RuntimeError('PiAPI unavailable'), 'https://video.example/retry.mp4']
    card = AsyncMock(return_value=['https://cdn.example/clean-card.png'])
    router, context = create_router(admin_user_id=456, postcard_generator=card), await ready_context()
    await context.update_data(mode='mode_ny', photo={'url': 'https://cdn.example/source.png'}, card_text='Спасибо!',
        video_product='direct', video_flow=True, video_amount_kopecks=29900)
    await context.set_state(PosterStates.waiting_for_video_song)
    await dispatch_callback(router, callback_event('video_song:audio_fun_ny'), context)
    assert await context.get_state() == PosterStates.waiting_for_video_song
    assert any(r.exc_info and 'Ошибка генерации видео PiAPI' in r.message for r in caplog.records)
    await dispatch_callback(router, callback_event('video_song:audio_fun_ny'), context)
    card.assert_awaited_once()
    assert (await context.get_data())['video_result_url'] == 'https://video.example/retry.mp4'


async def test_regular_user_does_not_start_real_piapi_without_payment(credit_db, piapi_animation):
    router, context = create_router(admin_user_id=999), await ready_context()
    await context.update_data(mode='mode_ny', last_image_url='https://cdn.example/card.png')
    await dispatch_callback(router, callback_event('upsell_animate'), context)
    await dispatch_callback(router, callback_event('video_song:audio_fun_ny'), context)
    piapi_animation.assert_not_awaited()
    assert await context.get_state() == PosterStates.waiting_for_video_payment


async def test_piapi_failure_is_not_replaced_with_test_link(credit_db, piapi_animation):
    piapi_animation.side_effect = RuntimeError('provider failed')
    router = create_router(admin_user_id=456, avatar_generator=AsyncMock(return_value='https://cdn.example/avatar.png'))
    context = await ready_context()
    await start_cartoon(router, context)
    await call_handler(router, 'collect_cartoon_photo', cartoon_photo(), context)
    await dispatch_callback(router, callback_event('avatar_ok_1'), context)
    event = callback_event('cartoon_song:audio_fun_ny')
    await dispatch_callback(router, event, context)
    assert await context.get_state() == PosterStates.waiting_for_cartoon_song
    assert 'Не удалось создать видео' in event.send.await_args.args[0]
    assert 'example.com/cartoon-test-video.mp4' not in str(event.send.await_args_list)


async def test_cancel_during_piapi_does_not_restore_old_card_flow(credit_db, piapi_animation):
    import asyncio
    entered, release = asyncio.Event(), asyncio.Event()
    async def animate(*args):
        entered.set()
        await release.wait()
        return 'https://video.example/late.mp4'
    piapi_animation.side_effect = animate
    router, context = create_router(admin_user_id=456), await ready_context()
    await context.update_data(mode='mode_ny', last_image_url='https://cdn.example/card.png')
    await dispatch_callback(router, callback_event('upsell_animate'), context)
    event = callback_event('video_song:audio_fun_ny')
    task = asyncio.create_task(dispatch_callback(router, event, context))
    try:
        await asyncio.wait_for(entered.wait(), timeout=5)
        await call_handler(router, 'cancel', message_event('/cancel'), context)
    finally:
        release.set()
        await task
    assert await context.get_state() == PosterStates.waiting_for_mode
    assert not (await context.get_data()).get('video_source_image')
    assert 'https://video.example/late.mp4' not in str(event.send.await_args_list)


@pytest.mark.parametrize('result', ['https://example.com/cartoon-test-video.mp4', 'Это заглушка; видеосервис будет подключён позже.'])
async def test_cartoon_placeholder_is_persisted_before_promising_later_delivery(credit_db, result):
    video = AsyncMock(return_value=result)
    router = create_router(admin_user_id=456, avatar_generator=AsyncMock(return_value='https://cdn.example/avatar.png'), cartoon_video_generator=video)
    context = await ready_context()
    await start_cartoon(router, context)
    await call_handler(router, 'collect_cartoon_photo', cartoon_photo(), context)
    await dispatch_callback(router, callback_event('avatar_ok_1'), context)
    event = callback_event('cartoon_song:audio_fun_ny')
    await dispatch_callback(router, event, context)
    jobs = await credit_db.get_all_pending_videos()
    assert len(jobs) == 1
    assert jobs[0]['avatar_url'] == 'https://cdn.example/avatar.png' and jobs[0]['user_id'] == 456
    assert jobs[0]['song_url'] == video.await_args.args[1]
    assert event.send.await_args.args[0] == handlers.PENDING_VIDEO_MESSAGE
    assert await context.get_state() == PosterStates.waiting_for_mode
    assert 'Скачать видео' not in str(event.send.await_args_list)


async def test_card_placeholder_preserves_image_and_song_in_queue(credit_db):
    router = create_router(admin_user_id=456, video_generator=AsyncMock(return_value='https://example.com/test-video.mp4'))
    context = await ready_context()
    await context.update_data(mode='mode_ny', last_image_url='https://cdn.example/card.png', chosen_song_url='https://audio.example/song.mp3')
    await dispatch_callback(router, callback_event('upsell_animate'), context)
    event = callback_event('video_song:audio_fun_ny')
    await dispatch_callback(router, event, context)
    jobs = await credit_db.get_all_pending_videos()
    assert len(jobs) == 1 and jobs[0]['avatar_url'] == 'https://cdn.example/card.png'
    assert jobs[0]['song_url'] == 'https://audio.example/song.mp3'
    assert event.send.await_args.args[0] == handlers.PENDING_VIDEO_MESSAGE


@pytest.mark.parametrize('admin_id,actor', [(None, 456), (999, 456)])
async def test_process_pending_videos_is_admin_only(credit_db, admin_id, actor):
    generate = AsyncMock()
    event = message_event('/process_pending_videos')
    event.get_ids = lambda: (123, actor)
    await credit_db.save_pending_video(123, 'https://images.example/a.png', 'https://audio.example/a.mp3')
    await call_handler(create_router(admin_user_id=admin_id, cartoon_video_generator=generate), 'process_pending_videos', event)
    generate.assert_not_awaited()
    event.bot.send_message.assert_not_awaited()
    event.message.answer.assert_not_awaited()


async def test_process_pending_videos_continues_after_failure_and_marks_only_delivered(credit_db, caplog):
    first = await credit_db.save_pending_video(123, 'https://images.example/a.png', 'https://audio.example/a.mp3')
    await credit_db.save_pending_video(789, 'https://images.example/b.png', 'https://audio.example/b.mp3')
    generate = AsyncMock(side_effect=[RuntimeError('PiAPI unavailable'), 'https://video.example/b.mp4'])
    event = message_event('/process_pending_videos')
    await call_handler(create_router(admin_user_id=456, cartoon_video_generator=generate), 'process_pending_videos', event)
    assert generate.await_args_list == [call('https://images.example/a.png', 'https://audio.example/a.mp3'), call('https://images.example/b.png', 'https://audio.example/b.mp3')]
    event.bot.send_message.assert_awaited_once()
    assert event.bot.send_message.await_args.kwargs['user_id'] == 789
    assert [job['id'] for job in await credit_db.get_all_pending_videos()] == [first]
    assert 'Успешно: 1, Ошибок: 1' in event.message.answer.await_args.args[0]
    assert any(record.exc_info and 'Ошибка отложенного видео' in record.message for record in caplog.records)


async def test_pending_video_failed_delivery_reuses_result_after_restart(credit_db):
    await credit_db.save_pending_video(123, 'https://images.example/a.png', 'https://audio.example/a.mp3')
    generate = AsyncMock(return_value='https://video.example/a.mp4')
    event = message_event('/process_pending_videos')
    event.bot.send_message.side_effect = RuntimeError('MAX unavailable')
    await call_handler(create_router(admin_user_id=456, cartoon_video_generator=generate), 'process_pending_videos', event)
    assert (await credit_db.get_all_pending_videos())[0]['result_url'] == 'https://video.example/a.mp4'
    await credit_db.init_db()
    retry = message_event('/process_pending_videos')
    await call_handler(create_router(admin_user_id=456, cartoon_video_generator=generate), 'process_pending_videos', retry)
    generate.assert_awaited_once()
    assert await credit_db.get_all_pending_videos() == []
    assert 'https://video.example/a.mp4' in retry.bot.send_message.await_args.kwargs['text']


async def test_pending_video_placeholder_is_never_completed_or_sent(credit_db):
    await credit_db.save_pending_video(123, 'https://images.example/a.png', 'https://audio.example/a.mp3')
    event = message_event('/process_pending_videos')
    await call_handler(create_router(admin_user_id=456, cartoon_video_generator=AsyncMock(return_value='https://example.com/test.mp4')), 'process_pending_videos', event)
    event.bot.send_message.assert_not_awaited()
    assert len(await credit_db.get_all_pending_videos()) == 1


async def test_process_pending_videos_empty_queue_reports_zero(credit_db):
    event = message_event('/process_pending_videos')
    await call_handler(create_router(admin_user_id=456), 'process_pending_videos', event)
    assert 'Успешно: 0, Ошибок: 0' in event.message.answer.await_args.args[0]
    event.bot.send_message.assert_not_awaited()


async def test_process_pending_video_uses_real_piapi_adapter_without_song_as_prompt(credit_db, monkeypatch, piapi_animation):
    await credit_db.save_pending_video(123, 'https://images.example/a.png', 'https://audio.example/a.mp3')
    event = message_event('/process_pending_videos')
    await call_handler(create_router(admin_user_id=456), 'process_pending_videos', event)
    piapi_animation.assert_awaited_once_with('https://images.example/a.png')
    assert await credit_db.get_all_pending_videos() == []


async def test_process_pending_videos_serializes_repeated_admin_commands(credit_db):
    import asyncio
    entered, release = asyncio.Event(), asyncio.Event()
    async def generate(*args):
        entered.set()
        await release.wait()
        return 'https://video.example/a.mp4'
    await credit_db.save_pending_video(123, 'https://images.example/a.png', 'https://audio.example/a.mp3')
    router = create_router(admin_user_id=456, cartoon_video_generator=generate)
    first, second = message_event('/process_pending_videos'), message_event('/process_pending_videos')
    task = asyncio.create_task(call_handler(router, 'process_pending_videos', first))
    try:
        await asyncio.wait_for(entered.wait(), timeout=5)
        await call_handler(router, 'process_pending_videos', second)
        assert 'уже обрабатываются' in second.message.answer.await_args.args[0]
    finally:
        release.set()
        await task
    first.bot.send_message.assert_awaited_once()
    second.bot.send_message.assert_not_awaited()


async def test_cartoon_queue_preserves_existing_song_url(credit_db):
    router = create_router(admin_user_id=456, avatar_generator=AsyncMock(return_value='https://cdn.example/avatar.png'),
                           cartoon_video_generator=AsyncMock(return_value='https://example.com/test.mp4'))
    context = await ready_context()
    await start_cartoon(router, context)
    await call_handler(router, 'collect_cartoon_photo', cartoon_photo(), context)
    await dispatch_callback(router, callback_event('avatar_ok_1'), context)
    await context.update_data(chosen_song='audio_fun_ny', chosen_song_url='https://audio.example/real-track.mp3')
    await dispatch_callback(router, callback_event('cartoon_song:audio_fun_ny'), context)
    assert (await credit_db.get_all_pending_videos())[0]['song_url'] == 'https://audio.example/real-track.mp3'


async def test_failed_queue_save_never_promises_delivery(credit_db, monkeypatch):
    monkeypatch.setattr(handlers, 'save_pending_video', AsyncMock(side_effect=RuntimeError('DB unavailable')))
    router = create_router(admin_user_id=456, video_generator=AsyncMock(return_value='Это заглушка'))
    context = await ready_context()
    await context.update_data(mode='mode_ny', last_image_url='https://cdn.example/card.png')
    await dispatch_callback(router, callback_event('upsell_animate'), context)
    event = callback_event('video_song:audio_fun_ny')
    await dispatch_callback(router, event, context)
    assert handlers.PENDING_VIDEO_MESSAGE not in str(event.send.await_args_list)
    assert await context.get_state() == PosterStates.waiting_for_video_song


async def test_pending_video_none_delivery_confirmation_keeps_job_pending(credit_db):
    await credit_db.save_pending_video(123, 'https://images.example/a.png', 'https://audio.example/a.mp3')
    event = message_event('/process_pending_videos')
    event.bot.send_message.return_value = None
    await call_handler(create_router(admin_user_id=456), 'process_pending_videos', event)
    assert len(await credit_db.get_all_pending_videos()) == 1
    assert 'Успешно: 0, Ошибок: 1' in event.message.answer.await_args.args[0]


@pytest.mark.parametrize('kind', ['card', 'cartoon'])
async def test_video_result_delivers_merged_file_and_real_ttapi_song(credit_db, monkeypatch, piapi_animation, kind):
    merger = AsyncMock(return_value='https://cdn.example/final-with-vocals.mp4')
    monkeypatch.setattr(handlers, 'merge_video_and_audio', merger)
    router = create_router(admin_user_id=456, avatar_generator=AsyncMock(return_value='https://cdn.example/avatar.png'))
    context = await ready_context()
    if kind == 'cartoon':
        await start_cartoon(router, context)
        await call_handler(router, 'collect_cartoon_photo', cartoon_photo(), context)
        await dispatch_callback(router, callback_event('avatar_ok_1'), context)
        event = callback_event('cartoon_song:audio_fun_ny')
        image_url = 'https://cdn.example/avatar.png'
    else:
        await context.update_data(mode='mode_ny', last_image_url='https://cdn.example/card.png', card_text='С Новым годом, Аня!')
        await dispatch_callback(router, callback_event('upsell_video'), context)
        event = callback_event('video_song:audio_fun_ny')
        image_url = 'https://cdn.example/card.png'
    await dispatch_callback(router, event, context)
    handlers.generate_song.assert_awaited_once()
    assert 'Новогодняя' in handlers.generate_song.await_args.args[2]
    piapi_animation.assert_awaited_once_with(image_url)
    merger.assert_awaited_once_with('https://video.example/animated.mp4', 'https://audio.example/1.mp3')
    assert 'https://cdn.example/final-with-vocals.mp4' in event.send.await_args.args[0]
    assert event.send.await_args.kwargs['attachments'][0].payload.buttons[0][0].url == 'https://cdn.example/final-with-vocals.mp4'
    assert (await context.get_data())['video_song_url'] == 'https://audio.example/1.mp3'


async def test_video_merge_retry_reuses_song_job_and_kling_output(credit_db, monkeypatch, piapi_animation):
    merger = AsyncMock(side_effect=[RuntimeError('ffmpeg failed'), 'https://cdn.example/final.mp4'])
    monkeypatch.setattr(handlers, 'merge_video_and_audio', merger)
    router, context = create_router(admin_user_id=456), await ready_context()
    await context.update_data(mode='mode_ny', last_image_url='https://cdn.example/card.png')
    await dispatch_callback(router, callback_event('upsell_video'), context)
    await dispatch_callback(router, callback_event('video_song:audio_fun_ny'), context)
    assert await context.get_state() == PosterStates.waiting_for_video_song
    await dispatch_callback(router, callback_event('video_song:audio_fun_ny'), context)
    piapi_animation.assert_awaited_once()
    handlers.generate_song.assert_awaited_once()
    assert merger.await_count == 2
    assert (await context.get_data())['video_result_url'] == 'https://cdn.example/final.mp4'


async def test_pending_processor_delivers_merged_result(credit_db, monkeypatch, piapi_animation):
    await credit_db.save_pending_video(123, 'https://images.example/avatar.png', 'https://audio.example/song.mp3')
    merger = AsyncMock(return_value='https://cdn.example/pending-merged.mp4')
    monkeypatch.setattr(handlers, 'merge_video_and_audio', merger)
    event = message_event('/process_pending_videos')
    await call_handler(create_router(admin_user_id=456), 'process_pending_videos', event)
    merger.assert_awaited_once_with('https://video.example/animated.mp4', 'https://audio.example/song.mp3')
    assert 'https://cdn.example/pending-merged.mp4' in event.bot.send_message.await_args.kwargs['text']
    assert await credit_db.get_all_pending_videos() == []


async def test_switching_video_song_cannot_reuse_old_job_after_submit_failure(credit_db, monkeypatch):
    submit = AsyncMock(side_effect=['ny-job', RuntimeError('submit failed'), 'cat-job'])
    waiter = AsyncMock(return_value='https://audio.example/generated.mp3')
    router, context = create_router(admin_user_id=456, song_generator=submit, song_waiter=waiter), await ready_context()
    await context.update_data(mode='mode_ny', last_image_url='https://cdn.example/card.png')
    await dispatch_callback(router, callback_event('upsell_video'), context)
    await dispatch_callback(router, callback_event('video_song:audio_fun_ny'), context)
    await dispatch_callback(router, callback_event('upsell_video'), context)
    await dispatch_callback(router, callback_event('video_song:audio_fun_cat'), context)
    assert (await context.get_data())['video_song_job_id'] is None
    await dispatch_callback(router, callback_event('video_song:audio_fun_cat'), context)
    assert submit.await_count == 3
    assert waiter.await_args_list == [call('ny-job'), call('cat-job')]


async def test_old_short_merge_cache_is_rebuilt_without_new_kling_or_ttapi_request(credit_db, monkeypatch, piapi_animation):
    merger = AsyncMock(side_effect=['https://cdn.example/first.mp4', 'https://cdn.example/full-song.mp4'])
    monkeypatch.setattr(handlers, 'merge_video_and_audio', merger)
    router, context = create_router(admin_user_id=456), await ready_context()
    await context.update_data(mode='mode_ny', last_image_url='https://cdn.example/card.png')
    await dispatch_callback(router, callback_event('upsell_video'), context)
    await dispatch_callback(router, callback_event('video_song:audio_fun_ny'), context)
    data = await context.get_data()
    await context.update_data(video_merged_source=[data['video_raw_url'], data['video_song_url']],
                              video_merged_url='https://cdn.example/old-five-seconds.mp4')
    await dispatch_callback(router, callback_event('upsell_video'), context)
    event = callback_event('video_song:audio_fun_ny')
    await dispatch_callback(router, event, context)
    assert merger.await_count == 2
    piapi_animation.assert_awaited_once()
    handlers.generate_song.assert_awaited_once()
    assert 'https://cdn.example/full-song.mp4' in event.send.await_args.args[0]

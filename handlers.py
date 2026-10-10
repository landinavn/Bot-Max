"""Меню и FSM: фото → сфера жизни → генерация плаката."""

import asyncio
import logging
from collections.abc import Awaitable, Callable
from uuid import uuid4
from weakref import WeakValueDictionary

from maxapi import F, Router
from maxapi.context import MemoryContext
from maxapi.context.state_machine import State, StatesGroup
from maxapi.filters.command import Command, CommandStart
from maxapi.types import (
    CallbackButton,
    LinkButton,
    MessageCallback,
    MessageCreated,
    PhotoAttachmentPayload,
)
from maxapi.utils.inline_keyboard import InlineKeyboardBuilder

from database import deduct_credit, get_user, grant_free_trial
from image_service import (
    GenerationLimitError,
    GenerationNotConfigured,
    GenerationTimeoutError,
    UnsupportedPhotoFormatError,
    generate_vision_boards,
    image_from_url,
)

logger = logging.getLogger(__name__)
PosterGenerator = Callable[[str, str], Awaitable[list[str]]]
WELCOME_TEXT = (
    "Привет! Я бот для создания плакатов. "
    "Выберите действие: создать плакат, посмотреть примеры или получить помощь."
)
PACKAGES_TEXT = (
    "💎 Доступные пакеты:\n"
    "5 генераций — 299 ₽\n"
    "15 генераций — 699 ₽\n\n"
    "Оплата временно недоступна. Скоро!"
)
PACKAGE_OFFER_TEXT = "Понравился результат? Хотите создать ещё?\n\n" + PACKAGES_TEXT


def packages_keyboard():
    return (
        InlineKeyboardBuilder()
        .row(CallbackButton(text="Купить 5 генераций", payload="buy_5"))
        .row(CallbackButton(text="Купить 15 генераций", payload="buy_15"))
        .as_markup()
    )


class PosterStates(StatesGroup):
    waiting_for_photo = State()
    waiting_for_sphere = State()
    generating = State()


def main_keyboard():
    return (
        InlineKeyboardBuilder()
        .row(CallbackButton(text="Создать плакат", payload="create_poster"))
        .row(
            CallbackButton(text="Примеры", payload="examples"),
            CallbackButton(text="Помощь", payload="help"),
        )
        .as_markup()
    )


def is_bot_message(event: MessageCreated) -> bool:
    sender = event.message.sender
    return sender is not None and sender.is_bot


def result_keyboard(url: str, batch_id: str, index: int):
    return InlineKeyboardBuilder().row(
        LinkButton(text="Скачать", url=url),
        CallbackButton(text="Ещё", payload=f"more:{batch_id}"),
        CallbackButton(text="Поделиться", payload=f"share:{batch_id}:{index}"),
    ).as_markup()


def create_router(generator: PosterGenerator = generate_vision_boards) -> Router:
    router = Router()
    # FSM изолирован по чату, а баланс общий для всех чатов пользователя.
    generation_locks: WeakValueDictionary[int, asyncio.Lock] = WeakValueDictionary()

    async def generate_and_send(
        event: MessageCreated | MessageCallback,
        context: MemoryContext,
        *,
        new_batch: bool = False,
    ) -> None:
        credit_user_id = event.get_ids()[1]
        if credit_user_id is None:
            if event.message is not None:
                await event.message.answer("Не удалось определить пользователя. Отправьте /start.")
            return
        lock = generation_locks.setdefault(credit_user_id, asyncio.Lock())
        async with lock:
            await generate_with_credits(
                event, context, credit_user_id=credit_user_id, new_batch=new_batch
            )

    async def generate_with_credits(
        event: MessageCreated | MessageCallback,
        context: MemoryContext,
        *,
        credit_user_id: int,
        new_batch: bool = False,
    ) -> None:
        message = event.message
        if message is None:
            return
        data = await context.get_data()
        photo = data.get("photo")
        sphere = data.get("sphere")
        if not isinstance(photo, dict) or not photo.get("url") or not sphere:
            await context.clear()
            await context.set_state(PosterStates.waiting_for_photo)
            await message.answer("Начнём заново: отправьте одно фото.")
            return
        await context.set_state(PosterStates.generating)
        try:
            bot = event.bot
            if bot is None:
                raise RuntimeError("Бот не привязан к событию.")
            chat_id = message.recipient.chat_id
            user_id = None
            if chat_id is None:
                actor = event.callback.user if isinstance(event, MessageCallback) else message.sender
                if message.recipient.chat_type != "dialog" or actor is None:
                    raise RuntimeError("Не определён получатель плаката.")
                user_id = actor.user_id

            urls = data.get("variants")
            if new_batch:
                data = await context.update_data(new_batch_requested=True)
            if data.get("new_batch_requested") or not urls:
                user = await get_user(credit_user_id)
                if user is None or user["balance"] < 1:
                    await context.set_state(PosterStates.waiting_for_sphere)
                    await message.answer("У вас закончились кредиты. Купите пакет: /buy")
                    return
                await message.answer("Создаю плакат…")
                urls = await generator(photo["url"], sphere)
                if len(urls) != 1 or not all(isinstance(url, str) and url.startswith("https://") for url in urls):
                    raise RuntimeError("Генератор должен вернуть один HTTPS URL.")
                if not await deduct_credit(credit_user_id):
                    raise RuntimeError("Не удалось списать кредит после генерации.")
                user = await get_user(credit_user_id)
                data = await context.update_data(
                    variants=urls, batch_id=uuid4().hex[:12], sent_count=0,
                    new_batch_requested=False,
                    package_offer_pending=user is not None and user["balance"] == 0,
                )
            else:
                await message.answer("Отправляю готовый плакат…")
            batch_id = data["batch_id"]
            for index in range(data.get("sent_count", 0), 1):
                image = image_from_url(urls[index])
                keyboard = result_keyboard(urls[index], batch_id, index)
                logger.info("Запрос MAX API: отправка плаката.")
                sent = await bot.send_message(
                    chat_id=chat_id,
                    user_id=user_id,
                    text="Ваш плакат готов!",
                    attachments=[image, keyboard],
                )
                if sent is None:
                    raise RuntimeError("MAX не подтвердил отправку плаката.")
                await context.update_data(sent_count=index + 1)
            user = await get_user(credit_user_id)
            await message.answer(f"Осталось кредитов: {user['balance'] if user else 0}.")
            if data.get("package_offer_pending"):
                await message.answer(
                    PACKAGE_OFFER_TEXT, attachments=[packages_keyboard()]
                )
                await context.update_data(package_offer_pending=False)
        except GenerationTimeoutError:
            logger.error("Таймаут API при создании или сохранении плаката.")
            await context.set_state(PosterStates.waiting_for_sphere)
            await message.answer(
                "Сервис не ответил вовремя. Фото и сфера сохранены. "
                "Повторите сферу жизни позже или отправьте /cancel."
            )
            return
        except UnsupportedPhotoFormatError:
            logger.error("Неподдерживаемый формат фото или повреждённое изображение.")
            await context.update_data(
                variants=[], sent_count=0, batch_id=None, new_batch_requested=False
            )
            await context.set_state(PosterStates.waiting_for_photo)
            await message.answer(
                "Формат фото не поддерживается или изображение повреждено. "
                "Отправьте новое фото в формате PNG, JPEG или WebP."
            )
            return
        except GenerationLimitError as exc:
            logger.error(
                "Лимит генераций: %s.",
                "квота исчерпана" if exc.quota_exhausted else "слишком много запросов",
            )
            await context.set_state(PosterStates.waiting_for_sphere)
            if exc.quota_exhausted:
                text = (
                    "Лимит генераций исчерпан. Попробуйте позже или обратитесь "
                    "к администратору бота. Фото и сфера сохранены."
                )
            else:
                wait = f"Подождите {exc.retry_after} секунд" if exc.retry_after else "Подождите немного"
                text = (
                    f"Слишком много запросов генерации. {wait} и повторите сферу жизни. "
                    "Фото сохранено."
                )
            await message.answer(text)
            return
        except GenerationNotConfigured:
            logger.error("Не настроен API генерации или хранилище изображений.")
            await context.set_state(PosterStates.waiting_for_sphere)
            await message.answer(
                "Фото и сфера сохранены, но сервис генерации пока не настроен. "
                "После настройки повторите сферу жизни или /cancel."
            )
            return
        except Exception as exc:
            logger.error("Не удалось создать или отправить плакат (%s).", type(exc).__name__)
            await context.set_state(PosterStates.waiting_for_sphere)
            await message.answer(
                "Не удалось завершить создание или отправку плаката. "
                "Повторите сферу жизни или /cancel."
            )
            return
        # Данные нужны кнопкам «Ещё» и «Поделиться»; завершаем только состояние.
        await context.set_state(None)

    @router.message_created(CommandStart())
    async def start(event: MessageCreated, context: MemoryContext) -> None:
        if is_bot_message(event):
            return
        user_id = event.get_ids()[1]
        if user_id is None:
            await event.message.answer("Не удалось определить пользователя.")
            return
        trial_granted = await grant_free_trial(user_id)
        await context.clear()
        await event.message.answer(text=WELCOME_TEXT, attachments=[main_keyboard()])
        if trial_granted:
            await event.message.answer("Вам начислен 1 бесплатный кредит!")

    @router.message_created(Command("buy"))
    async def buy(event: MessageCreated) -> None:
        if is_bot_message(event):
            return
        await event.message.answer(
            PACKAGES_TEXT + "\n1 кредит — одно сгенерированное изображение.",
            attachments=[packages_keyboard()],
        )

    @router.message_callback(F.callback.payload.in_({"buy_5", "buy_15"}))
    async def buy_callback(event: MessageCallback) -> None:
        await event.ack(notification="Оплата временно недоступна. Скоро!")
        if event.message is not None:
            await event.send(PACKAGES_TEXT, attachments=[packages_keyboard()])

    @router.message_created(Command("cancel"))
    async def cancel(event: MessageCreated, context: MemoryContext) -> None:
        if is_bot_message(event):
            return
        await context.clear()
        await event.message.answer("Сбор данных отменён.", attachments=[main_keyboard()])

    @router.message_callback(F.callback.payload == "create_poster")
    async def begin_poster(event: MessageCallback, context: MemoryContext) -> None:
        if event.message is None:
            await event.ack(notification="Меню недоступно. Отправьте /start.")
            return
        if await context.get_state() == PosterStates.generating:
            await event.ack(notification="Плакат уже создаётся. Пожалуйста, подождите.")
            return
        await event.ack(notification="Загружаю…")
        await context.clear()
        await context.set_state(PosterStates.waiting_for_photo)
        await event.send("Отправьте одно фото для плаката. /cancel — отмена.")

    @router.message_callback(F.callback.payload.in_({"examples", "help"}))
    async def menu_callback(event: MessageCallback, context: MemoryContext) -> None:
        if event.message is None:
            await event.ack(notification="Меню недоступно. Отправьте /start.")
            return
        await event.ack(notification="Открываю…")

        if event.callback.payload == "examples":
            await event.send(
                "Примеры идей: ваше фото в образе успешного предпринимателя, "
                "спортсмена или путешественника. Сферы жизни: карьера, здоровье, путешествия."
            )
        else:
            await event.send(
                "Нажмите «Создать плакат», отправьте одно фото, затем укажите сферу жизни: "
                "например, карьера, семья, здоровье или путешествия.\n"
                "Бот создаст один плакат с кнопками «Скачать», «Ещё» и «Поделиться».\n"
                "/cancel — отменить сбор данных, /start — вернуться в меню."
            )

    @router.message_created(PosterStates.waiting_for_photo)
    async def collect_photo(event: MessageCreated, context: MemoryContext) -> None:
        if is_bot_message(event):
            return
        body = event.message.body
        attachments = (body.attachments or []) if body else []
        photos = [
            attachment
            for attachment in attachments
            if attachment.type == "image"
            and isinstance(attachment.payload, PhotoAttachmentPayload)
        ]
        if len(photos) != 1 or not photos[0].payload.url:
            await event.message.answer("Нужно одно фото, отправленное как изображение. /cancel — отмена.")
            return
        # Сохраняем метаданные фото, а не скачиваем его до выбора сферы.
        await context.update_data(photo=photos[0].payload.model_dump())
        await context.set_state(PosterStates.waiting_for_sphere)
        await event.message.answer(
            "Фото получено! Укажите сферу жизни (до 200 символов): "
            "карьера, семья, здоровье, путешествия или другая."
        )

    @router.message_created(PosterStates.waiting_for_sphere)
    async def collect_sphere(event: MessageCreated, context: MemoryContext) -> None:
        if is_bot_message(event):
            return
        body = event.message.body
        sphere = (body.text or "").strip() if body else ""
        if not sphere or len(sphere) > 200 or sphere.startswith("/"):
            await event.message.answer("Введите сферу жизни текстом от 1 до 200 символов. /cancel — отмена.")
            return
        data = await context.get_data()
        if data.get("sphere") != sphere:
            await context.update_data(
                variants=[], sent_count=0, batch_id=None, new_batch_requested=False
            )
        await context.update_data(sphere=sphere)
        await generate_and_send(event, context)

    @router.message_callback(
        F.callback.payload.startswith("more:") | F.callback.payload.startswith("share:")
    )
    async def result_callback(event: MessageCallback, context: MemoryContext) -> None:
        if event.message is None:
            await event.ack(notification="Сообщение удалено. Отправьте /start.")
            return
        parts = (event.callback.payload or "").split(":")
        data = await context.get_data()
        if len(parts) < 2 or parts[1] != data.get("batch_id"):
            await event.ack(notification="Эта кнопка устарела. Используйте последнее меню.")
            return
        if parts[0] == "share":
            try:
                index = int(parts[2]) if len(parts) == 3 else -1
                if index != 0:
                    raise ValueError("Invalid index")
                url = data["variants"][index]
            except (ValueError, KeyError, IndexError, TypeError):
                await event.ack(notification="Вариант недоступен. Отправьте /start.")
                return
            await event.ack(notification="Готовлю ссылку…")
            await event.send(f"Скопируйте и отправьте эту ссылку:\n{url}")
        elif len(parts) == 2:
            if await context.get_state() == PosterStates.generating:
                await event.ack(notification="Плакат уже создаётся. Подождите.")
                return
            await event.ack(notification="Создаю плакат…")
            await generate_and_send(
                event, context, new_batch=data.get("sent_count", 0) == 1
            )
        else:
            await event.ack(notification="Кнопка недоступна.")

    @router.message_created(PosterStates.generating)
    async def generating(event: MessageCreated) -> None:
        if not is_bot_message(event):
            await event.message.answer("Плакат уже создаётся. Пожалуйста, подождите.")

    @router.message_created()
    async def echo(event: MessageCreated) -> None:
        if is_bot_message(event):
            return
        body = event.message.body
        if body is not None and body.text is not None and body.text != "":
            await event.message.answer(text=body.text)

    return router

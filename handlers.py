"""Меню и FSM: фото → сфера жизни → генерация плаката."""

import logging
from collections.abc import Awaitable, Callable
from uuid import uuid4

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

    async def generate_and_send(
        event: MessageCreated | MessageCallback,
        context: MemoryContext,
        *,
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
                await message.answer("Создаю три варианта плаката…")
                urls = await generator(photo["url"], sphere)
                if len(urls) != 3 or not all(isinstance(url, str) and url.startswith("https://") for url in urls):
                    raise RuntimeError("Генератор должен вернуть три HTTPS URL.")
                data = await context.update_data(
                    variants=urls, batch_id=uuid4().hex[:12], sent_count=0,
                    new_batch_requested=False,
                )
            else:
                await message.answer("Отправляю готовые варианты…")
            batch_id = data["batch_id"]
            for index in range(data.get("sent_count", 0), 3):
                image = image_from_url(urls[index])
                keyboard = result_keyboard(urls[index], batch_id, index)
                logger.info("Запрос MAX API: отправка варианта %d из 3.", index + 1)
                sent = await bot.send_message(
                    chat_id=chat_id,
                    user_id=user_id,
                    text=f"Вариант {index + 1} из 3",
                    attachments=[image, keyboard],
                )
                if sent is None:
                    raise RuntimeError("MAX не подтвердил отправку плаката.")
                await context.update_data(sent_count=index + 1)
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
            logger.error("Не удалось создать или отправить варианты плаката (%s).", type(exc).__name__)
            await context.set_state(PosterStates.waiting_for_sphere)
            await message.answer(
                "Не удалось завершить создание или отправку вариантов. "
                "Повторите сферу жизни или /cancel."
            )
            return
        # Данные нужны кнопкам «Ещё» и «Поделиться»; завершаем только состояние.
        await context.set_state(None)

    @router.message_created(CommandStart())
    async def start(event: MessageCreated, context: MemoryContext) -> None:
        if is_bot_message(event):
            return
        await context.clear()
        await event.message.answer(text=WELCOME_TEXT, attachments=[main_keyboard()])

    @router.message_created(Command("cancel"))
    async def cancel(event: MessageCreated, context: MemoryContext) -> None:
        if is_bot_message(event):
            return
        await context.clear()
        await event.message.answer("Сбор данных отменён.", attachments=[main_keyboard()])

    @router.message_callback(F.callback.payload.in_({"create_poster", "examples", "help"}))
    async def menu_callback(event: MessageCallback, context: MemoryContext) -> None:
        if event.message is None:
            await event.ack(notification="Меню недоступно. Отправьте /start.")
            return
        await event.ack()

        if event.callback.payload == "create_poster":
            if await context.get_state() == PosterStates.generating:
                await event.send("Плакат уже создаётся. Пожалуйста, подождите.")
                return
            await context.clear()
            await context.set_state(PosterStates.waiting_for_photo)
            await event.send("Отправьте одно фото для плаката. /cancel — отмена.")
        elif event.callback.payload == "examples":
            await event.send(
                "Примеры идей: ваше фото в образе успешного предпринимателя, "
                "спортсмена или путешественника. Сферы жизни: карьера, здоровье, путешествия."
            )
        else:
            await event.send(
                "Нажмите «Создать плакат», отправьте одно фото, затем укажите сферу жизни: "
                "например, карьера, семья, здоровье или путешествия.\n"
                "Бот предложит три варианта с кнопками «Скачать», «Ещё» и «Поделиться».\n"
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
                if not 0 <= index < 3:
                    raise ValueError("Invalid index")
                url = data["variants"][index]
            except (ValueError, KeyError, IndexError, TypeError):
                await event.ack(notification="Вариант недоступен. Отправьте /start.")
                return
            await event.ack()
            await event.send(f"Скопируйте и отправьте эту ссылку:\n{url}")
        elif len(parts) == 2:
            if await context.get_state() == PosterStates.generating:
                await event.ack(notification="Варианты уже создаются. Подождите.")
                return
            await event.ack()
            await generate_and_send(
                event, context, new_batch=data.get("sent_count", 0) == 3
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

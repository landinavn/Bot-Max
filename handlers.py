"""Меню и FSM: режим → фото → мечта или готовый праздничный шаблон."""

import asyncio
import logging
from collections.abc import Awaitable, Callable
from html import escape
from pathlib import Path
from urllib.parse import urlsplit, unquote, urlencode
from uuid import uuid4
from weakref import WeakValueDictionary

from maxapi import F, Router
from maxapi.context import MemoryContext
from maxapi.context.state_machine import State, StatesGroup
from maxapi.filters.command import Command, CommandStart
from maxapi.enums.parse_mode import ParseMode
from maxapi.enums.upload_type import UploadType
from maxapi.types import (
    BotStarted,
    CallbackButton,
    InputMedia,
    LinkButton,
    MessageCallback,
    MessageCreated,
    PhotoAttachmentPayload,
)
from maxapi.utils.inline_keyboard import InlineKeyboardBuilder

from config import DEFAULT_BOT_LINK, validate_bot_link

from database import (
    deduct_credit, get_user, grant_free_trial, get_global_stats, increment_generations,
    record_first_start, get_source_stats, register_shared_image, get_shared_image,
)
from prodamus import PACKAGES, ProdamusPayments
from max_client import log_max_error
from image_service import (
    GenerationLimitError,
    GenerationNotConfigured,
    GenerationTimeoutError,
    UnsupportedPhotoFormatError,
    generate_vision_boards,
    generate_postcards,
    POSTCARD_PROMPTS,
    MAX_CARD_TEXT_LENGTH,
    upload_image_for_max,
)

logger = logging.getLogger(__name__)


def _recipient(event: MessageCreated | MessageCallback | BotStarted) -> dict[str, int]:
    if isinstance(event, BotStarted):
        return {"user_id": event.user.user_id}
    if event.message is None:
        raise RuntimeError("Не определён получатель сообщения.")
    if event.message.recipient.chat_type == "dialog":
        user_id = event.get_ids()[1]
        if user_id is None:
            raise RuntimeError("Не определён user_id получателя.")
        return {"user_id": user_id}
    chat_id = event.message.recipient.chat_id
    if chat_id is None:
        raise RuntimeError("Не определён chat_id группы.")
    return {"chat_id": chat_id}


async def _reply(event: MessageCreated | MessageCallback | BotStarted, text=None, **kwargs):
    if event.bot is None:
        raise RuntimeError("Бот не привязан к событию.")
    try:
        return await event.bot.send_message(**_recipient(event), text=text, **kwargs)
    except Exception as exc:
        await log_max_error(exc, logger)
        raise


PosterGenerator = Callable[[str, str], Awaitable[list[str]]]
WELCOME_TEXT = (
    "Привет! Я создаю волшебные открытки и карты желаний с твоим лицом. "
    "Выбери, что хочешь создать:"
)
MODE_BUTTONS = (
    ("🖼 Карта желаний", "mode_vision"),
    ("🎄 Открытка: Новый год", "mode_ny"),
    ("👨‍👩‍👧‍👦 Семейный Новый год", "mode_ny_family"),
    ("🎂 День рождения (Молодым)", "mode_bday_young"),
    ("🎂 День рождения (Старшим)", "mode_bday_old"),
    ("🌹 Открытка: 8 Марта", "mode_8march"),
    ("☀️ Открытка: С Добрым утром", "mode_goodmorning"),
    ("🎨 Современная открытка", "mode_modern_card"),
)


def postcard_retry_keyboard():
    return InlineKeyboardBuilder().row(
        CallbackButton(text="Повторить отправку / генерацию", payload="postcard_retry")
    ).as_markup()


CARD_TEXT_PROMPT = (
    "✍️ Теперь напиши текст поздравления и имя (например: «С днём рождения, Аня! Будь счастлива!»). "
    "Этот текст появится на открытке."
)
SHARED_WELCOME = (
    "Привет! Твой друг поделился с тобой этой открыткой. Хочешь создать такую же с твоим лицом? "
    "Нажми кнопку ниже!"
)
CHANNEL_TEXT = "✨ Хочешь узнать секреты исполнения желаний? Присоединяйся к нашему каналу!"


def create_own_keyboard():
    return InlineKeyboardBuilder().row(CallbackButton(text="Создать свою", payload="open_main_menu")).as_markup()


PACKAGES_TEXT = (
    "💎 Доступные пакеты:\n"
    "• 1 генерация (открытка) — 99 ₽\n"
    "• 5 генераций — 299 ₽\n"
    "• 15 генераций — 799 ₽"
)
PAYMENT_PACKAGES_TEXT = PACKAGES_TEXT.split("\n\n", 1)[0]
NO_CREDITS_TEXT = "У вас закончились кредиты. Пожалуйста, выберите пакет для продолжения:"
EXAMPLES_DIR = Path(__file__).resolve().parent / "examples"
START_EXAMPLE_FILES = (
    "i (1).webp",
    "i (18).webp",
    "i (17).webp",
    "i (19).webp",
    "i (16).webp",
    "i (14).webp",
)
EXAMPLE_TEXT = (
    "📸 Можно создать карту желаний или открытку по шаблону: Новый год (личный или семейный), День рождения (молодым или старшим), 8 Марта, Доброе утро, Современная открытка.\n"
    "Ниже пример превращения фото в карту желаний — до и после.\n\n"
    "Промпт: «Машина тесла с бордовым салоном, я за рулем»"
)
HELP_TEXT = (
    "🆘 Помощь\n\n"
    "Я создаю карты желаний и праздничные открытки с твоим фото. "
    "Вот как это работает:\n\n"
    "1. Выбери «Карта желаний» или открытку: Новый год (личный или семейный), День рождения (молодым или старшим), 8 Марта, Доброе утро, Современная открытка.\n"
    "2. Отправь своё фото (селфи).\n"
    "3. Для открытки напиши поздравление и имя после фото (до 500 символов). "
    "Для карты желаний опиши свою мечту или желание текстом — например, "
    "«Машина тесла с бордовым салоном, я за рулем» или «Я на пляже на Мальдивах».\n"
    "4. Получи готовый плакат с тобой в мечте!\n\n"
    "💎 Стоимость:\n"
    "• 1 бесплатная генерация — для новых пользователей.\n"
    "• 1 генерация (открытка) — 99 ₽.\n"
    "• Пакет 5 генераций — 299 ₽.\n"
    "• Пакет 15 генераций — 799 ₽.\n\n"
    "❓ Частые вопросы:"
)
HELP_ANSWERS = {
    "help_payment": "Выберите пакет для оплаты.",
    "help_generation": (
        "Попробуйте отправить другое фото или повторите позже. "
        'Если ошибка повторяется, напишите в поддержку: '
        '<a href="https://max.ru/u/f9LHodD0cOL0i7XGM4uyyUo-bFy4nt4Hqc-XMNmvGygT83X2wUkr6O7DRXo">'
        'Связаться с поддержкой</a>'
    ),
    "help_support": (
        'Напишите нам: <a href="https://max.ru/u/f9LHodD0cOL0i7XGM4uyyUo-bFy4nt4Hqc-XMNmvGygT83X2wUkr6O7DRXo">'
        'Связаться с поддержкой</a>'
    ),
}


def help_keyboard():
    return (
        InlineKeyboardBuilder()
        .row(CallbackButton(text="Как оплатить?", payload="help_payment"))
        .row(CallbackButton(text="Не работает генерация", payload="help_generation"))
        .row(CallbackButton(text="Связаться с поддержкой", payload="help_support"))
        .as_markup()
    )


def packages_keyboard():
    return (
        InlineKeyboardBuilder()
        .row(CallbackButton(text="Купить 1 генерацию — 99 ₽", payload="buy_1"))
        .row(CallbackButton(text="Купить 5 генераций — 299 ₽", payload="buy_5"))
        .row(CallbackButton(text="Купить 15 генераций — 799 ₽", payload="buy_15"))
        .as_markup()
    )


class PosterStates(StatesGroup):
    waiting_for_mode = State()
    waiting_for_photo = State()
    waiting_for_prompt = State()
    waiting_for_card_text = State()
    # Принимать описание и из состояния, сохранённого прежней версией.
    waiting_for_sphere = State()
    generating = State()


def main_keyboard():
    keyboard = InlineKeyboardBuilder()
    for text, payload in MODE_BUTTONS:
        keyboard.row(CallbackButton(text=text, payload=payload))
    return (
        keyboard
        .row(
            CallbackButton(text="Примеры", payload="examples"),
            CallbackButton(text="Помощь", payload="help"),
        )
        .as_markup()
    )


def is_bot_message(event: MessageCreated) -> bool:
    sender = event.message.sender
    return sender is not None and sender.is_bot


def result_keyboard(url: str):
    return InlineKeyboardBuilder().row(
        LinkButton(text="Скачать", url=url),
        CallbackButton(text="Ещё", payload="more"),
    ).as_markup()


def create_router(
    generator: PosterGenerator = generate_vision_boards,
    *, payments: ProdamusPayments | None = None,
    admin_user_id: int | None = None,
    postcard_generator: Callable[[str, str, str], Awaitable[list[str]]] = generate_postcards,
    bot_link: str = DEFAULT_BOT_LINK,
) -> Router:
    bot_link = validate_bot_link(bot_link)
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
                await _reply(event, "Не удалось определить пользователя. Отправьте /start.")
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
        is_admin = admin_user_id is not None and credit_user_id == admin_user_id
        data = await context.get_data()
        photo = data.get("photo")
        sphere = data.get("sphere")
        is_postcard = data.get("mode") in POSTCARD_PROMPTS
        retry_state = PosterStates.waiting_for_card_text if is_postcard else PosterStates.waiting_for_prompt

        async def problem(text):
            if is_postcard:
                await _reply(event, "Не удалось завершить создание или отправку открытки. "
                             "Фото и текст сохранены. Нажмите повторить, отправьте другой текст или /cancel.",
                             attachments=[postcard_retry_keyboard()])
            else:
                await _reply(event, text)

        if not isinstance(photo, dict) or not photo.get("url") or not sphere:
            mode = data.get("mode", "mode_vision")
            await context.clear()
            await context.update_data(mode=mode)
            await context.set_state(PosterStates.waiting_for_photo)
            await _reply(event, "Начнём заново: отправьте одно фото.")
            return
        if is_postcard and not data.get("card_text"):
            await context.set_state(PosterStates.waiting_for_card_text)
            await _reply(event, CARD_TEXT_PROMPT)
            return
        await context.set_state(PosterStates.generating)
        try:
            bot = event.bot
            if bot is None:
                raise RuntimeError("Бот не привязан к событию.")
            recipient = _recipient(event)

            urls = data.get("variants")
            if new_batch:
                data = await context.update_data(new_batch_requested=True)
            if data.get("new_batch_requested") or not urls:
                if not is_admin:
                    user = await get_user(credit_user_id)
                    if user is None or user["balance"] < 1:
                        await context.set_state(retry_state)
                        await _reply(event, NO_CREDITS_TEXT, attachments=[packages_keyboard()] + ([postcard_retry_keyboard()] if is_postcard else []))
                        return
                await _reply(event, "Создаю открытку…" if is_postcard else "Создаю плакат…")
                if is_postcard:
                    urls = await postcard_generator(photo["url"], data["mode"], data["card_text"])
                else:
                    urls = await generator(photo["url"], sphere)
                if len(urls) != 1 or not all(isinstance(url, str) and url.startswith("https://") for url in urls):
                    raise RuntimeError("Генератор должен вернуть один HTTPS URL.")
                user = None
                if is_admin:
                    await increment_generations(credit_user_id)
                    logger.info("Пропущено списание кредитов для администратора %s", credit_user_id)
                else:
                    if not await deduct_credit(credit_user_id, count_generation=True):
                        raise RuntimeError("Не удалось списать кредит после генерации.")
                    user = await get_user(credit_user_id)
                data = await context.update_data(
                    variants=urls, batch_id=uuid4().hex[:12], sent_count=0,
                    new_batch_requested=False, share_prompt_sent=False, channel_invite_sent=False,
                    package_offer_pending=not is_admin and user is not None and user["balance"] == 0,
                )
            else:
                await _reply(event, "Отправляю готовый плакат…")
            for index in range(data.get("sent_count", 0), 1):
                keyboard = result_keyboard(urls[index])
                try:
                    image = await upload_image_for_max(bot, urls[index])
                    logger.info("Запрос MAX API: отправка плаката с media token.")
                    sent = await bot.send_message(
                        **recipient,
                        text="Ваша открытка готов!" if is_postcard else "Ваш плакат готов!",
                        attachments=[image, keyboard],
                    )
                except Exception as exc:
                    await log_max_error(exc, logger)
                    raise
                if sent is None:
                    raise RuntimeError("MAX не подтвердил отправку плаката.")
                await context.update_data(
                    sent_count=index + 1, last_image_url=urls[index],
                    last_image_batch_id=data["batch_id"],
                )
            data = await context.get_data()
            if not data.get("share_prompt_sent"):
                token = await register_shared_image(urls[0])
                share_url = bot_link + "?" + urlencode({"start": "share_" + token})
                keyboard = InlineKeyboardBuilder().row(LinkButton(text="🔗 Поделиться", url=share_url)).as_markup()
                await _reply(event, "👇 Хочешь поделиться с друзьями?", attachments=[keyboard])
                await context.update_data(share_prompt_sent=True)
            if not is_postcard and not data.get("channel_invite_sent"):
                keyboard = InlineKeyboardBuilder().row(
                    LinkButton(text="Секреты исполнения желаний", url="https://max.ru/se14596751_biz")
                ).as_markup()
                await _reply(event, CHANNEL_TEXT, attachments=[keyboard])
                await context.update_data(channel_invite_sent=True)
            if not is_admin:
                user = await get_user(credit_user_id)
                await _reply(event, f"Осталось кредитов: {user['balance'] if user else 0}.")
                if data.get("package_offer_pending"):
                    await _reply(event,
                        NO_CREDITS_TEXT,
                        attachments=[packages_keyboard()],
                    )
                    await context.update_data(package_offer_pending=False)
        except GenerationTimeoutError:
            logger.error("Таймаут API при создании или сохранении плаката.")
            await context.set_state(retry_state)
            await problem(
                "Сервис не ответил вовремя. Фото и описание мечты сохранены. "
                "Повторите описание мечты позже или отправьте /cancel."
            )
            return
        except UnsupportedPhotoFormatError:
            logger.error("Неподдерживаемый формат фото или повреждённое изображение.")
            await context.update_data(
                variants=[], sent_count=0, batch_id=None, new_batch_requested=False
            )
            await context.set_state(PosterStates.waiting_for_photo)
            await _reply(event,
                "Формат фото не поддерживается или изображение повреждено. "
                "Отправьте новое фото в формате PNG, JPEG или WebP."
            )
            return
        except GenerationLimitError as exc:
            logger.error(
                "Лимит генераций: %s.",
                "квота исчерпана" if exc.quota_exhausted else "слишком много запросов",
            )
            await context.set_state(retry_state)
            if exc.quota_exhausted:
                text = (
                    "Лимит генераций исчерпан. Попробуйте позже или обратитесь "
                    "к администратору бота. Фото и описание мечты сохранены."
                )
            else:
                wait = f"Подождите {exc.retry_after} секунд" if exc.retry_after else "Подождите немного"
                text = (
                    f"Слишком много запросов генерации. {wait} и повторите описание мечты. "
                    "Фото сохранено."
                )
            await problem(text)
            return
        except GenerationNotConfigured:
            logger.error("Не настроен API генерации или хранилище изображений.")
            await context.set_state(retry_state)
            await problem(
                "Фото и описание мечты сохранены, но сервис генерации пока не настроен. "
                "После настройки повторите описание мечты или /cancel."
            )
            return
        except Exception as exc:
            logger.error("Не удалось создать или отправить плакат (%s).", type(exc).__name__)
            await context.set_state(retry_state)
            await problem(
                "Не удалось завершить создание или отправку плаката. "
                "Повторите описание мечты или /cancel."
            )
            return
        # Данные нужны кнопкам «Ещё» и «Поделиться»; завершаем только состояние.
        await context.set_state(None)

    @router.message_created(Command("stats"))
    async def stats(event: MessageCreated) -> None:
        if is_bot_message(event):
            return
        if admin_user_id is None or event.get_ids()[1] != admin_user_id:
            return
        data = await get_global_stats()
        sources = await get_source_stats()
        source_text = "\n\n📍 Источники первого запуска (весь период):"
        for source in sources:
            amount = source["revenue_kopecks"]
            revenue = f"{amount // 100:,}".replace(",", " ") + f",{amount % 100:02d} ₽"
            source_text += (
                f"\n\n{source['label']}\n"
                f"👥 Новые пользователи: {source['new_users']}\n"
                f"🖼 Пользователи с генерацией: {source['users_with_generation']}\n"
                f"💳 Покупатели: {source['buyers']}\n"
                f"💰 Сумма оплат: {revenue}"
            )
            if source["source"] == "legacy":
                source_text += f"\nРанее зарегистрированы, дата и источник неизвестны: {source['total_users']}"
        await _reply(event,
            "📊 Статистика бота:\n\n"
            f"👥 Всего пользователей: {data['total_users']}\n"
            f"💳 Покупателей: {data['total_paid_users']}\n"
            f"🖼 Всего сгенерировано картинок: {data['total_generated']}\n"
            f"💰 Всего покупок: {data['total_purchases']}" + source_text
        )

    async def show_start(
        event: MessageCreated | MessageCallback | BotStarted, context: MemoryContext,
        *, payload: str | None = None, timestamp_ms: int | None = None,
    ) -> None:
        user_id = event.get_ids()[1]
        if user_id is None:
            await _reply(event, "Не удалось определить пользователя.")
            return
        await record_first_start(user_id, payload, timestamp_ms=timestamp_ms)
        trial_granted = await grant_free_trial(user_id)
        await context.clear()
        await context.set_state(PosterStates.waiting_for_mode)
        if isinstance(payload, str) and payload.startswith("share_"):
            image_url = await get_shared_image(unquote(payload.removeprefix("share_")))
            if image_url is not None:
                try:
                    image = await upload_image_for_max(event.bot, image_url)
                    await _reply(event, SHARED_WELCOME, attachments=[image, create_own_keyboard()])
                except Exception as exc:
                    await log_max_error(exc, logger)
                    await _reply(event, SHARED_WELCOME + "\nКартинку сейчас не удалось загрузить.",
                                 attachments=[create_own_keyboard()])
                if trial_granted:
                    await _reply(event, "Вам начислен 1 бесплатный кредит!")
                return
            await _reply(event, "Эта ссылка на картинку недоступна. Вы можете создать свою.")
        examples = []
        for filename in START_EXAMPLE_FILES:
            path = EXAMPLES_DIR / filename
            try:
                if not path.is_file():
                    raise FileNotFoundError(filename)
                image = await event.bot.upload_media(InputMedia(path=str(path), type=UploadType.IMAGE))
                if not isinstance(image.payload.token, str) or not image.payload.token.strip():
                    raise RuntimeError("MAX вернул пустой media token.")
                examples.append(image)
            except Exception as exc:
                logger.error("Не удалось загрузить пример %s: %s", filename, exc)
        await _reply(event, text=WELCOME_TEXT + ("\n\nПримеры работ: карта желаний и праздничные открытки." if examples else ""),
                     attachments=[*examples, main_keyboard()])
        if trial_granted:
            await _reply(event, "Вам начислен 1 бесплатный кредит!")

    @router.bot_started()
    async def bot_started(event: BotStarted, context: MemoryContext) -> None:
        if not event.user.is_bot:
            await show_start(event, context, payload=event.payload, timestamp_ms=event.timestamp)

    @router.message_created(CommandStart())
    async def start(event: MessageCreated, context: MemoryContext) -> None:
        if not is_bot_message(event):
            body = event.message.body
            parts = ((body.text or "").split(maxsplit=1) if body else [])
            await show_start(event, context, payload=parts[1].strip() if len(parts) > 1 else None)

    @router.message_callback(F.callback.payload == "open_main_menu")
    async def open_main_menu(event: MessageCallback, context: MemoryContext) -> None:
        if event.message is None:
            await event.ack(notification="Отправьте /start, чтобы открыть меню.")
            return
        if await context.get_state() == PosterStates.generating:
            await event.ack(notification="Изображение уже создаётся. Подождите.")
            return
        await event.ack(notification="Открываю меню…")
        await show_start(event, context)

    @router.message_created(Command("buy"))
    async def buy(event: MessageCreated) -> None:
        if is_bot_message(event):
            return
        await _reply(event,
            (PAYMENT_PACKAGES_TEXT if payments else PACKAGES_TEXT)
            + "\n1 кредит — одно сгенерированное изображение.",
            attachments=[packages_keyboard()],
        )

    @router.message_callback(F.callback.payload.in_({"buy_1", "buy_5", "buy_15"}))
    async def buy_callback(event: MessageCallback) -> None:
        if event.message is None:
            await event.ack(notification="Сообщение удалено. Отправьте /buy.")
            return
        await event.ack(notification="Создаю ссылку…")
        if payments is None:
            logger.error("Не настроен Prodamus для создания платёжной ссылки.")
            await _reply(event, "Не удалось создать ссылку на оплату. Обратитесь в поддержку.")
            return
        user_id = event.get_ids()[1]
        if user_id is None:
            await _reply(event, "Не удалось определить пользователя. Отправьте /start.")
            return
        credits = int(event.callback.payload.removeprefix("buy_"))
        try:
            link = await payments.create_payment_link(user_id, credits)
        except Exception as exc:
            logger.error("Не удалось создать ссылку Prodamus (%s).", type(exc).__name__)
            await _reply(event, "Не удалось создать ссылку на оплату. Попробуйте /buy позже.")
            return
        amount, title = PACKAGES[credits]
        keyboard = InlineKeyboardBuilder().row(
            LinkButton(text=f"Оплатить {amount // 100} ₽", url=link)
        ).as_markup()
        await _reply(event,
            f"Для оплаты пакета «{title} — {amount // 100} ₽» перейдите по ссылке:\n{link}",
            attachments=[keyboard],
        )

    @router.message_created(Command("cancel"))
    async def cancel(event: MessageCreated, context: MemoryContext) -> None:
        if is_bot_message(event):
            return
        await context.clear()
        await context.set_state(PosterStates.waiting_for_mode)
        await _reply(event, "Сбор данных отменён.", attachments=[main_keyboard()])

    @router.message_callback(F.callback.payload.in_({"create_poster", "mode_bday", *(payload for _, payload in MODE_BUTTONS)}))
    async def begin_poster(event: MessageCallback, context: MemoryContext) -> None:
        if event.message is None:
            await event.ack(notification="Меню недоступно. Отправьте /start.")
            return
        if await context.get_state() == PosterStates.generating:
            await event.ack(notification="Плакат уже создаётся. Пожалуйста, подождите.")
            return
        await event.ack(notification="Загружаю…")
        await context.clear()
        mode = "mode_vision" if event.callback.payload == "create_poster" else event.callback.payload
        if mode == "mode_bday":  # Кнопки из меню предыдущей версии.
            mode = "mode_bday_young"
        await context.update_data(mode=mode)
        await context.set_state(PosterStates.waiting_for_photo)
        if mode == "mode_ny_family":
            text = "Отправьте одно общее фото семьи, где видны лица всех участников. Затем напишите поздравление для открытки. /cancel — отмена."
        elif mode == "mode_vision":
            text = "Отправьте одно фото для плаката. /cancel — отмена."
        else:
            text = "Отправьте одно фото для открытки. Затем напишите поздравление для открытки. /cancel — отмена."
        await _reply(event, text)

    @router.message_callback(F.callback.payload.in_({"examples", "help"}))
    async def menu_callback(event: MessageCallback, context: MemoryContext) -> None:
        if event.message is None:
            await event.ack(notification="Меню недоступно. Отправьте /start.")
            return
        await event.ack(notification="Открываю…")

        if event.callback.payload == "examples":
            photos = (
                (EXAMPLES_DIR / "before.jpg", "Фото до:"),
                (EXAMPLES_DIR / "after.jpg", "Результат:"),
            )
            if not all(path.is_file() for path, _ in photos):
                logger.warning("Не найдены локальные фотографии примера.")
                await _reply(event, "Пример временно недоступен. Попробуйте позже.")
                return
            try:
                if event.bot is None:
                    raise RuntimeError("Бот не привязан к событию.")
                await _reply(event, EXAMPLE_TEXT)
                for path, caption in photos:
                    image = await event.bot.upload_media(
                        InputMedia(path=str(path), type=UploadType.IMAGE)
                    )
                    token = image.payload.token
                    if not isinstance(token, str) or not token.strip():
                        raise RuntimeError("MAX вернул пустой media token.")
                    sent = await _reply(event, caption, attachments=[image])
                    if sent is None:
                        raise RuntimeError("MAX не подтвердил отправку примера.")
            except Exception as exc:
                logger.error("Не удалось отправить пример (%s).", type(exc).__name__)
                await _reply(event, "Не удалось отправить пример. Попробуйте позже.")
        else:
            await _reply(event, HELP_TEXT, attachments=[help_keyboard()])

    @router.message_callback(F.callback.payload.in_(set(HELP_ANSWERS)))
    async def help_callback(event: MessageCallback) -> None:
        if event.message is None:
            await event.ack(notification="Меню недоступно. Отправьте /start.")
            return
        await event.ack(notification="Открываю…")
        if event.callback.payload == "help_payment":
            text = (
                "Выберите пакет ниже и нажмите «Оплатить» в следующем сообщении.\n\n"
                + PAYMENT_PACKAGES_TEXT
                if payments else HELP_ANSWERS["help_payment"]
            )
            await _reply(event, text, attachments=[packages_keyboard()])
        elif event.callback.payload in {"help_support", "help_generation"}:
            await _reply(event, HELP_ANSWERS[event.callback.payload], parse_mode=ParseMode.HTML)
        else:
            await _reply(event, HELP_ANSWERS[event.callback.payload])

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
            await _reply(event, "Нужно одно фото, отправленное как изображение. /cancel — отмена.")
            return
        data = await context.get_data()
        await context.update_data(photo=photos[0].payload.model_dump(), variants=[], sent_count=0,
                                  batch_id=None, new_batch_requested=False)
        if data.get("mode") in POSTCARD_PROMPTS:
            await context.update_data(sphere=data["mode"], card_text=None)
            await context.set_state(PosterStates.waiting_for_card_text)
            await _reply(event, CARD_TEXT_PROMPT)
            return
        await context.set_state(PosterStates.waiting_for_prompt)
        await _reply(event,
            "✅ Фото получено!\n\n"
            "Теперь опиши свою мечту или желание (до 200 символов). "
            "Например: «Машина тесла с бордовым салоном, я за рулем» "
            "или «Я на пляже на Мальдивах».\n\n"
            "/cancel — отмена"
        )

    @router.message_created(PosterStates.waiting_for_card_text)
    async def collect_card_text(event: MessageCreated, context: MemoryContext) -> None:
        if is_bot_message(event):
            return
        data = await context.get_data()
        if data.get("mode") not in POSTCARD_PROMPTS:
            await context.set_state(PosterStates.waiting_for_mode)
            await _reply(event, "Выберите открытку в меню.", attachments=[main_keyboard()])
            return
        body = event.message.body
        text = (body.text or "").strip() if body else ""
        if not text or len(text) > MAX_CARD_TEXT_LENGTH:
            await _reply(event, f"Напишите поздравление текстом — от 1 до {MAX_CARD_TEXT_LENGTH} символов.")
            return
        if data.get("card_text") != text:
            await context.update_data(variants=[], sent_count=0, batch_id=None, new_batch_requested=False)
        await context.update_data(card_text=text, sphere=data["mode"])
        await generate_and_send(event, context)

    @router.message_created(PosterStates.waiting_for_sphere)
    @router.message_created(PosterStates.waiting_for_prompt)
    async def collect_sphere(event: MessageCreated, context: MemoryContext) -> None:
        if is_bot_message(event):
            return
        if (await context.get_data()).get("mode") in POSTCARD_PROMPTS:
            await context.set_state(PosterStates.waiting_for_card_text)
            await _reply(event, CARD_TEXT_PROMPT)
            return
        body = event.message.body
        sphere = (body.text or "").strip() if body else ""
        if len(sphere) > 200:
            await _reply(event, "Слишком длинное описание. Пожалуйста, уложись в 200 символов")
            return
        if not sphere:
            await _reply(event, "Напиши свою мечту или желание текстом. /cancel — отмена")
            return
        data = await context.get_data()
        if data.get("sphere") != sphere:
            await context.update_data(
                variants=[], sent_count=0, batch_id=None, new_batch_requested=False
            )
        await context.update_data(sphere=sphere)
        await generate_and_send(event, context)

    @router.message_callback(
        F.callback.payload.in_({"more", "share"})
        | F.callback.payload.startswith("more:")
        | F.callback.payload.startswith("share:")
    )
    async def result_callback(event: MessageCallback, context: MemoryContext) -> None:
        if event.message is None:
            await event.ack(notification="Сообщение удалено. Отправьте /start.")
            return
        parts = (event.callback.payload or "").split(":")
        action = parts[0]
        data = await context.get_data()
        if action == "more":
            if len(parts) not in {1, 2}:
                await event.ack(notification="Кнопка недоступна.")
                return
            if await context.get_state() == PosterStates.generating:
                await event.ack(notification="Плакат уже создаётся. Подождите.")
                return
            is_postcard = data.get("mode") in POSTCARD_PROMPTS
            await event.ack(notification="Жду текст новой открытки." if is_postcard else "Жду новое описание мечты.")
            photo = data.get("photo")
            if not isinstance(photo, dict) or not isinstance(photo.get("url"), str) or not photo["url"]:
                mode = data.get("mode", "mode_vision")
                await context.clear()
                await context.update_data(mode=mode)
                await context.set_state(PosterStates.waiting_for_photo)
                await _reply(event, "Фото не сохранилось. Пожалуйста, загрузите фото заново.")
                return
            if is_postcard:
                await context.update_data(card_text=None, new_batch_requested=True)
                await context.set_state(PosterStates.waiting_for_card_text)
                await _reply(event, CARD_TEXT_PROMPT)
                return
            variants = data.get("variants") or []
            await context.update_data(
                sphere=None, new_batch_requested=True, package_offer_pending=False,
                last_image_url=data.get("last_image_url") or (variants[0] if variants else None),
                last_image_batch_id=data.get("last_image_batch_id") or data.get("batch_id"),
            )
            await context.set_state(PosterStates.waiting_for_prompt)
            await _reply(event, "Опишите новую мечту или желание (до 200 символов). Ваше фото сохранено. /cancel — отмена.")
            return

        # Поддержать и новые кнопки share, и уже отправленные share:<batch>:0.
        if len(parts) != 1:
            batch_id = data.get("last_image_batch_id") or data.get("batch_id")
            if len(parts) != 3 or parts[1] != batch_id or parts[2] != "0":
                await event.ack(notification="Эта кнопка устарела. Используйте последнее меню.")
                return
        variants = data.get("variants") or []
        url = data.get("last_image_url") or (variants[0] if variants else None)
        # Кнопка «Скачать» хранит ссылку именно на изображение этого сообщения.
        body = getattr(event.message, "body", None)
        for attachment in getattr(body, "attachments", None) or []:
            payload = getattr(attachment, "payload", None)
            for row in getattr(payload, "buttons", None) or []:
                for button in row:
                    if getattr(button, "text", None) == "Скачать" and getattr(button, "url", None):
                        url = button.url
        try:
            parsed = urlsplit(url) if isinstance(url, str) else None
            valid = parsed is not None and parsed.scheme == "https" and bool(parsed.hostname)
        except ValueError:
            valid = False
        if not valid:
            await event.ack(notification="Ссылка недоступна.")
            await _reply(event, "Ссылка на картинку не сохранилась. Создайте изображение заново.")
            return
        await event.ack(notification="Готовлю ссылку…")
        safe_url = escape(url, quote=True)
        await _reply(
            event,
            f'Вы можете скачать картинку или поделиться ссылкой:\n<a href="{safe_url}">{safe_url}</a>',
            parse_mode=ParseMode.HTML,
        )

    @router.message_callback(F.callback.payload == "postcard_retry")
    async def retry_postcard(event: MessageCallback, context: MemoryContext) -> None:
        if event.message is None or (await context.get_data()).get("mode") not in POSTCARD_PROMPTS:
            await event.ack(notification="Выберите открытку в /start.")
            return
        if await context.get_state() not in (PosterStates.waiting_for_mode, PosterStates.waiting_for_card_text):
            await event.ack(notification="Повтор недоступен. Используйте последнее меню.")
            return
        await event.ack(notification="Повторяю…")
        await generate_and_send(event, context)

    @router.message_created(PosterStates.waiting_for_mode)
    async def choose_mode(event: MessageCreated) -> None:
        if not is_bot_message(event):
            await _reply(event, "Выберите карту желаний или открытку кнопкой.", attachments=[main_keyboard()])

    @router.message_created(PosterStates.generating)
    async def generating(event: MessageCreated) -> None:
        if not is_bot_message(event):
            await _reply(event, "Плакат уже создаётся. Пожалуйста, подождите.")

    @router.message_created()
    async def echo(event: MessageCreated) -> None:
        if is_bot_message(event):
            return
        body = event.message.body
        if body is not None and body.text is not None and body.text != "":
            await _reply(event, text=body.text)

    return router

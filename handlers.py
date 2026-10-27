"""Меню и FSM: режим → фото → мечта или готовый праздничный шаблон."""

import asyncio
import logging
from collections.abc import Awaitable, Callable
from pathlib import Path
from urllib.parse import unquote, urlencode
from uuid import uuid4
from weakref import WeakValueDictionary

from maxapi import F, Router
from maxapi.context import MemoryContext
from maxapi.context.state_machine import State, StatesGroup
from maxapi.filters.command import Command, CommandStart
from maxapi.filters.middleware import BaseMiddleware
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
    get_all_counters, increment_counter,
    get_advanced_stats, update_user_activity, mark_user_inactive, get_active_user_ids,
    is_promo_active, PromoUnavailableError, get_users_by_activity, start_broadcast_promo,
    get_users_for_reminder,
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
    fix_text_with_ai,
    POSTCARD_PROMPTS,
    MAX_CARD_TEXT_LENGTH,
    upload_image_for_max,
)

logger = logging.getLogger(__name__)


class UserActivityMiddleware(BaseMiddleware):
    async def __call__(self, handler, event_object, data):
        actor = None
        if isinstance(event_object, BotStarted):
            actor = event_object.user
        elif isinstance(event_object, MessageCallback):
            actor = event_object.callback.user
        elif isinstance(event_object, MessageCreated):
            actor = event_object.message.sender
        if actor is not None and not actor.is_bot:
            await update_user_activity(actor.user_id, timestamp_ms=event_object.timestamp)
        return await handler(event_object, data)


def max_error_status(exc: Exception) -> int | None:
    response = getattr(exc, "response", None)
    for value in (getattr(exc, "code", None), getattr(exc, "status", None),
                  getattr(response, "status_code", None), getattr(response, "status", None)):
        if isinstance(value, int):
            return value
    return 403 if type(exc).__name__ == "Forbidden" else None

GENERATION_EVENTS = {
    "mode_vision": "gen_vision",
    "mode_ny": "gen_ny",
    "mode_ny_family": "gen_ny_family",
    "mode_bday_young": "gen_bday_young",
    "mode_bday_old": "gen_bday_old",
    "mode_8march": "gen_8march",
    "mode_goodmorning": "gen_goodmorning",
    "mode_modern_card": "gen_scrapbook",
    "mode_ny_cozy": "gen_ny_cozy",
    "mode_ny_fairytale": "gen_ny_fairytale",
    "mode_ny_elegant": "gen_ny_elegant",
    "mode_bday_old_elegant": "gen_bday_old_elegant",
    "mode_halloween": "gen_halloween",
}


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
    "Привет! Я твой личный нейро-художник 🎨\n"
    "\n"
    "Я создаю волшебные карты желаний и персональные открытки с твоим лицом или лицом твоего "
    "близкого человека. \n"
    "\n"
    "🎁 Ты можешь добавить любое пожелание, а мы красиво его оформим.\n"
    "\n"
    "🎨 Первая генерация — бесплатно!\n"
    "\n"
    "👇 Выбери, что хочешь создать:"
)
MODE_BUTTONS = (
    ("🎄 Открытка: Новый год", "mode_ny"),
    ("👨‍👩‍👧‍👦 Семейный Новый год", "mode_ny_family"),
    ("🕯 Уютный Новый год", "mode_ny_cozy"),
    ("❄️ Зимняя сказка", "mode_ny_fairytale"),
    ("🥂 Элегантный Новый год", "mode_ny_elegant"),
    ("🖼 Карта желаний", "mode_vision"),
    ("🎂 День рождения (Молодым)", "mode_bday_young"),
    ("🎂 День рождения (Старшим)", "mode_bday_old"),
    ("🎂 День рождения 2 (Старшим)", "mode_bday_old_elegant"),
    ("🎃 Хэллоуин", "mode_halloween"),
    ("☀️ Открытка: С Добрым утром", "mode_goodmorning"),
    ("🎨 Скрапбукинг", "mode_modern_card"),
)


def postcard_retry_keyboard():
    return InlineKeyboardBuilder().row(
        CallbackButton(text="Повторить отправку / генерацию", payload="postcard_retry")
    ).as_markup()


PHOTO_GUIDANCE = (
    "📸 Отправьте четкое фото, где хорошо видно лицо (анфас, при дневном свете). "
    "Вы можете загрузить как одного человека, так и целую семью! "
    "Без темных очков и сильных теней — так нейросеть сохранит максимальное сходство!"
)


def photo_request(text: str) -> str:
    return text + "\n\n" + PHOTO_GUIDANCE


CARD_TEXT_PROMPT = (
    "✍️ Отлично! Теперь напиши текст поздравления и имя (например: «С днём рождения, Аня! Будь счастлива!»).\n\n"
    "Этот текст появится на открытке.\n\n"
    "Вы можете использовать не только буквы, но и эмодзи (😊, 🎉, ❤️).\n\n"
    "💡 Примеры для вдохновения:\n"
    "• «С днём рождения, [Имя]! Желаю моря счастья, крепкого здоровья и исполнения самых заветных желаний!»\n"
    "• «С праздником, дорогая [Имя]! Пусть в жизни всегда будет место для чудес, а рядом — самые близкие люди.»\n"
    "• «С Новым годом, [Имя]! Пусть новый год принесёт только лучшее, а все мечты обязательно сбудутся!»\n\n"
    "Напиши свой текст (до 200 символов) или /cancel для отмены:"
)
SHARED_WELCOME = (
    "Привет! Твой друг поделился с тобой этой открыткой. Хочешь создать такую же с твоим лицом?"
)
CHANNEL_TEXT = (
    "✨ Понравилась твоя карта желаний?\n\n"
    "Присоединяйся к нашему каналу «Жизнь мечты | Как изменить свою жизнь». "
    "Там мы делимся вдохновением, секретами исполнения желаний и новыми шаблонами!"
)


def create_own_keyboard():
    return InlineKeyboardBuilder().row(CallbackButton(text="Создать свою", payload="open_main_menu")).as_markup()


PACKAGES_TEXT = (
    "💎 Доступные пакеты генераций:\n"
    "(Кредиты подходят для создания любых Карт желаний и Открыток. Они не сгорают!)\n"
    "\n"
    "• 1 генерация — 99 ₽\n"
    "• 3 генерации — 199 ₽\n"
    "• 5 генераций — 299 ₽\n"
    "• 15 генераций — 799 ₽\n"
    "\n"
    "👇 Выбери пакет для покупки:"
)
PAYMENT_PACKAGES_TEXT = PACKAGES_TEXT
NO_CREDITS_TEXT = "У вас закончились кредиты. Пожалуйста, выберите пакет для продолжения:"
EXAMPLES_DIR = Path(__file__).resolve().parent / "examples"
START_EXAMPLE_FILES = (
    r"i (1).webp",
    r"i (29).webp",
    r"i (28).webp",
    r"i (27).webp",
    r"i (26).webp",
    r"i (21).webp",
)

CATEGORY_BUTTONS = (
    ("🖼 Карта Желаний", "cat_vision"),
    ("🎁 Открытки", "cat_cards"),
    ("📸 Фотосессия", "cat_photo"),
    ("🎵 Аудиопесни", "cat_audio"),
)

# Файлы, текст и кнопки каждого экрана. mode_* остаются текущими входами в FSM.
CATEGORY_MENUS = {
    "cat_vision": (
        (r"i (1).webp",),
        (
            "Привет! Здесь ты можешь создать вдохновляющую карту желаний с твоим лицом. \n"
            "Всё просто: добавь своё фото и напиши описание своей мечты. Нейросеть нарисует твоё "
            "будущее! ✨\n"
            "\n"
            "🎨 Первая карта — бесплатно!"
        ),
        (
            ("Создать", "mode_vision"),
            ("⬅️ Назад", "back_to_main"),
        ),
    ),
    "cat_cards": (
        (r"i (28).webp", r"i (22).webp", r"i (25).webp", r"i (29).webp", r"i (27).webp", r"i (21).webp"),
        (
            "Привет! Я создаю волшебные открытки с твоим лицом или лицом близкого человека для любого "
            "праздника.\n"
            "✍️ На открытках ты можешь добавить любое пожелание и поздравление.\n"
            "\n"
            "🎨 Первая открытка — бесплатно!\n"
            "\n"
            "👇 Выбери, что хочешь создать:"
        ),
        (
            ("🎄 Новый год", "sub_ny"),
            ("🎂 День рождения", "sub_bday"),
            ("🎃 Хэллоуин", "sub_halloween"),
            ("☀️ Доброе утро", "sub_goodmorning"),
            ("⬅️ Назад", "back_to_main"),
        ),
    ),
    "sub_ny": (
        (r"i (28).webp", r"i (22).webp", r"i (23).webp", r"i (24).webp", r"i (25).webp"),
        (
            "🎄 Новый год\n"
            "\n"
            "Выбери шаблон открытки:"
        ),
        (
            ("Классический НГ", "mode_ny"),
            ("Семейный НГ", "mode_ny_family"),
            ("Уютный НГ", "mode_ny_cozy"),
            ("Зимняя сказка", "mode_ny_fairytale"),
            ("Элегантный НГ", "mode_ny_elegant"),
            ("⬅️ Назад", "back_to_cards"),
        ),
    ),
    "sub_bday": (
        (r"i (29).webp", r"i (27).webp", r"i (21).webp"),
        (
            "🎂 День рождения\n"
            "\n"
            "Выбери шаблон открытки:"
        ),
        (
            ("ДР Молодым", "mode_bday_young"),
            ("ДР Старшим", "mode_bday_old"),
            ("ДР Старшим 2", "mode_bday_old_elegant"),
            ("Скрапбукинг", "mode_modern_card"),
            ("⬅️ Назад", "back_to_cards"),
        ),
    ),
    "sub_halloween": (
        (r"i (26).webp",),
        (
            "🎃 Хэллоуин\n"
            "\n"
            "Создай уютную праздничную открытку с твоим лицом!"
        ),
        (
            ("Создать Хэллоуин", "mode_halloween"),
            ("⬅️ Назад", "back_to_cards"),
        ),
    ),
    "sub_goodmorning": (
        (r"i (20).webp",),
        (
            "☀️ Доброе утро\n"
            "\n"
            "Создай открытку с тёплым утренним пожеланием!"
        ),
        (
            ("Создать Доброе утро", "mode_goodmorning"),
            ("⬅️ Назад", "back_to_cards"),
        ),
    ),
    "cat_photo": (
        (),
        (
            "📸 Раздел «Фотосессия» уже совсем скоро заработает!\n\n"
            "Мы вовсю готовим для вас потрясающие образы. Но вы уже можете посмотреть стили и тарифы, "
            "которые будут доступны.\n\n"
            "Выбери стиль, который тебе интересен:"
        ),
        (
            ("🏙 Студия", "photo_style_studio"),
            ("🌳 Прогулка", "photo_style_walk"),
            ("🎄 Новогодняя", "photo_style_ny"),
            ("⬅️ Назад", "back_to_main"),
        ),
    ),
    "cat_audio": (
        (),
        (
            "🎵 Раздел в разработке!\n"
            "Совсем скоро ты сможешь заказать персональную песню с твоим именем. Это будет хит! 🎶"
        ),
        (
            ("⬅️ Назад", "back_to_main"),
        ),
    ),
}


PHOTO_STYLES = {
    "photo_style_studio": "Студия",
    "photo_style_walk": "Прогулка",
    "photo_style_ny": "Новогодняя",
}
PHOTO_CLICK_EVENTS = {
    "photo_style_studio": "click_photo_studio",
    "photo_style_walk": "click_photo_walk",
    "photo_style_ny": "click_photo_ny",
    "photo_tariff_590": "click_tariff_590",
    "photo_tariff_990": "click_tariff_990",
    "photo_tariff_1490": "click_tariff_1490",
}
PHOTO_TARIFF_TEXT = (
    "Вы выбрали стиль: {style_name}.\n\n"
    "Теперь выберите тариф, который вам подходит:\n\n"
    "🔹 «Попробовать» — 590 ₽\n"
    "Вы получите 3 профессиональных фото в одном стиле. Отличный вариант, чтобы оценить качество!\n\n"
    "🔹 «Фотосессия» — 990 ₽\n"
    "Вы получите 5 фото в одном стиле. Полноценная мини-сессия для вашего портфолио или соцсетей.\n\n"
    "🔹 «Премиум» — 1490 ₽\n"
    "7 фото (5 в одном стиле и 2 в другом). Снимки в максимальном разрешении 4K — готовы к печати "
    "в большом формате! Вы сможете загрузить два разных фото для разных образов."
)
PHOTO_TARIFF_BUTTONS = (
    ("Тариф «Попробовать» (590 ₽)", "photo_tariff_590"),
    ("Тариф «Фотосессия» (990 ₽)", "photo_tariff_990"),
    ("Тариф «Премиум» (1490 ₽)", "photo_tariff_1490"),
    ("⬅️ Назад", "cat_photo"),
)
PHOTO_TARIFF_SOON_TEXT = (
    "🚀 Отличный выбор! Этот тариф скоро будет доступен. Мы уже заканчиваем настройку фотосессий, "
    "следите за обновлениями!"
)


PHOTO_NY_TRIAL_TEMPLATES = {
    "photo_temp_fam3": ("👨‍👩‍👧 Семейная (3 чел)", "fam3"),
    "photo_temp_fam4": ("👨‍👩‍👧‍👦 Семейная (4+ чел)", "fam4"),
    "photo_temp_toddlers": ("👶 Семейная с малышами", "toddlers"),
    "photo_temp_mom_daughter": ("👩‍👧 Мама + дочка", "mom_daughter"),
    "photo_temp_rabbit": ("🐰 С кроликом", "rabbit"),
}
# TODO: Заменить на реальную ссылку Prodamus для 590 руб
PHOTO_TRIAL_TEST_PAYMENT_URL = "https://fotobot.payform.ru/"
PHOTO_TRIAL_CHECKOUT_TEXT = (
    "Отличный выбор! Сейчас я сформирую ссылку на оплату.\n\n"
    "Стоимость: 590 ₽ (3 фото).\n\n"
    "После оплаты я попрошу вас загрузить фото, и мы начнем!\n\n"
    "⚠️ Это тестовая ссылка. Оплата и обработка заказа фотосессии пока не подключены."
)


BROADCAST_EXAMPLE_FILES = (
    r"i (28).webp",
    r"i (25).webp",
    r"i (29).webp",
    r"i (26).webp",
)


def is_attachment_count_limit(exc: Exception) -> bool:
    if max_error_status(exc) not in {400, 413, 422}:
        return False
    details = str(getattr(exc, "raw", "") or exc).lower()
    return (
        any(word in details for word in ("attachment", "media", "вложен"))
        and any(word in details for word in ("too many", "too.many", "maximum", "max ", "limit", "не более", "количеств"))
    )


async def send_start_album(event, attachments) -> None:
    try:
        await _reply(event, text="Примеры работ:", attachments=attachments)
    except Exception as exc:
        if len(attachments) <= 10 or not is_attachment_count_limit(exc):
            raise
        logger.info("MAX отклонил число вложений; отправляю примеры альбомами по 10.")
        for offset in range(0, len(attachments), 10):
            await _reply(event, text="Примеры работ:", attachments=attachments[offset:offset + 10])


EXAMPLE_TEXT = (
    "📸 Можно создать карту желаний или открытку по шаблону: Новый год (личный или семейный), День рождения (молодым или старшим), уютный Новый год, Зимняя сказка, элегантный Новый год, День рождения 2 (Старшим), Хэллоуин, Доброе утро, Скрапбукинг.\n"
    "Ниже пример превращения фото в карту желаний — до и после.\n\n"
    "Промпт: «Машина тесла с бордовым салоном, я за рулем»"
)
HELP_TEXT = (
    "🆘 Помощь\n\nКак это работает:\n"
    "1. Выбираете шаблон (открытка или карта желаний).\n"
    "2. Загружаете четкое фото.\n"
    "3. Пишете текст поздравления (своими словами), а для карты желаний — описание мечты.\n"
    "4. Получаете готовый подарок примерно за 1 минуту!\n\n"
    "💎 Что такое кредиты (генерации):\n"
    "Это ваши попытки. Они НЕ сгорают! Купите пакет сейчас, а используйте, когда будет удобно "
    "(например, перед Новым годом).\n\n"
    "🔒 Конфиденциальность:\n"
    "Ваши фото используются для создания изображения и обрабатываются сервисом OpenAI. "
    "Готовая картинка сохраняется в облачном хранилище и доступна по ссылке.\n\n"
    "💬 Остались вопросы? Нажмите «Связаться с поддержкой» ниже."
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
        .row(CallbackButton(text="🔄 Главное меню", payload="open_main_menu"))
        .as_markup()
    )


PROMO_TEXT = (
    "🎁 Специальное предложение!\nЗабери 3 генерации за 149 ₽ (вместо 199 ₽).\n"
    "⏳ Цена действует 24 часа, но купленные генерации можно использовать в любое время и на любые картинки!\n\n"
    "🔥 Это предложение сейчас популярно — к нам присоединились более 60 человек!"
)


def packages_keyboard(*, promo: bool = False):
    builder = InlineKeyboardBuilder()
    if promo:
        builder.row(CallbackButton(text="Купить 3 генерации за 149 ₽", payload="buy_3_promo"))
    for credits, label in ((1, "1 генерацию"), (3, "3 генерации"), (5, "5 генераций"), (15, "15 генераций")):
        builder.row(CallbackButton(text=f"Купить {label} — {PACKAGES[credits][0] // 100} ₽", payload=f"buy_{credits}"))
    return builder.as_markup()


async def purchase_keyboard(user_id: int | None):
    return packages_keyboard(promo=user_id is not None and await is_promo_active(user_id))


class PosterStates(StatesGroup):
    waiting_for_mode = State()
    waiting_for_photo = State()
    waiting_for_prompt = State()
    waiting_for_card_text = State()
    # Принимать описание и из состояния, сохранённого прежней версией.
    waiting_for_sphere = State()
    generating = State()


def navigation_keyboard(buttons, *, footer=()):
    keyboard = InlineKeyboardBuilder()
    for text, payload in buttons:
        keyboard.row(CallbackButton(text=text, payload=payload))
    if footer:
        keyboard.row(*(CallbackButton(text=text, payload=payload) for text, payload in footer))
    return keyboard.as_markup()


def main_keyboard():
    return navigation_keyboard(CATEGORY_BUTTONS, footer=(
        ("🖼 Примеры", "show_examples"), ("🆘 Помощь", "show_help"),
    ))


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
    router.register_outer_middleware(UserActivityMiddleware())
    broadcast_lock = asyncio.Lock()
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
        mode = data.get("mode", "mode_vision")
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
            await _reply(event, photo_request("Начнём заново: отправьте одно фото."))
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
                        await _reply(event, NO_CREDITS_TEXT, attachments=[await purchase_keyboard(credit_user_id)] + ([postcard_retry_keyboard()] if is_postcard else []))
                        return
                await _reply(event, "⏳ Генерирую..." if is_postcard else "Создаю плакат…")
                if is_postcard:
                    text = data["card_text"]
                    fixed_text = data.get("fixed_card_text")
                    if data.get("fixed_card_text_source") != text or not fixed_text:
                        fixed_text = await fix_text_with_ai(text)
                        logger.info("Исходный текст: %s | Исправленный: %s", text, fixed_text)
                        data = await context.update_data(
                            fixed_card_text=fixed_text, fixed_card_text_source=text,
                        )
                    urls = await postcard_generator(photo["url"], data["mode"], fixed_text)
                else:
                    urls = await generator(photo["url"], sphere)
                if len(urls) != 1 or not all(isinstance(url, str) and url.startswith("https://") for url in urls):
                    raise RuntimeError("Генератор должен вернуть один HTTPS URL.")
                user = None
                generation_event = GENERATION_EVENTS[mode]
                if is_admin:
                    await increment_generations(credit_user_id, event_name=generation_event)
                    logger.info("Пропущено списание кредитов для администратора %s", credit_user_id)
                else:
                    if not await deduct_credit(credit_user_id, count_generation=True, generation_event=generation_event):
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
                        text="Ваша открытка готова!" if is_postcard else "Ваш плакат готов!",
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
            if mode == "mode_vision" and not data.get("channel_invite_sent"):
                keyboard = InlineKeyboardBuilder().row(
                    LinkButton(text="Перейти в канал", url="https://max.ru/se14596751_biz")
                ).as_markup()
                await _reply(event, CHANNEL_TEXT, attachments=[keyboard])
                await context.update_data(channel_invite_sent=True)
            if not data.get("share_prompt_sent"):
                token = await register_shared_image(urls[0])
                share_url = bot_link + "?" + urlencode({"start": "share_" + token})
                keyboard = InlineKeyboardBuilder().row(LinkButton(text="🔗 Поделиться", url=share_url)).as_markup()
                await _reply(event, "👇 Хочешь поделиться с друзьями?", attachments=[keyboard])
                await context.update_data(share_prompt_sent=True)
            if not is_admin:
                user = await get_user(credit_user_id)
                await _reply(event, f"Осталось кредитов: {user['balance'] if user else 0}.")
                if data.get("package_offer_pending"):
                    await _reply(event,
                        PROMO_TEXT if await is_promo_active(credit_user_id) else NO_CREDITS_TEXT,
                        attachments=[await purchase_keyboard(credit_user_id)],
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
                "Отправьте новое фото в формате PNG, JPEG или WebP." + "\n\n" + PHOTO_GUIDANCE
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
        # Сохраняем режим для «Ещё» и кэш результата для повторной отправки.
        await context.set_state(None)

    @router.message_created(Command("stats"))
    async def stats(event: MessageCreated) -> None:
        if is_bot_message(event):
            return
        if admin_user_id is None or event.get_ids()[1] != admin_user_id:
            return
        data = await get_global_stats(admin_id=admin_user_id)
        activity = await get_advanced_stats(admin_id=admin_user_id)
        counters = await get_all_counters(admin_id=admin_user_id)
        sources = await get_source_stats(admin_id=admin_user_id)
        admin = await get_user(admin_user_id)
        admin_text = (
            "\n\n🛠 Тестовая активность (Админ):\n"
            f"- Сгенерировано: {admin['total_generations'] if admin else 0}\n"
            f"- Покупок: {admin['total_purchases'] if admin else 0}"
        )
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
            f"👥 Всего запускали: {activity['total_all']}\n"
            f"🟢 Доступны для сообщений: {activity['total_active']}\n"
            f"🔴 Заблокировали/Удалили: {activity['total_blocked']}\n"
            f"🔄 Вернулись после 1-го дня: {activity['total_returned']}\n\n"
            f"👥 Всего пользователей: {data['total_users']}\n"
            f"💳 Покупателей: {data['total_paid_users']}\n"
            "\n🛍 Проданные пакеты:\n"
            f"- 1 генерация (99 ₽): {counters.get('buy_1', 0)}\n"
            f"- 3 генерации (199 ₽; по акции 149 ₽): {counters.get('buy_3', 0)}\n"
            f"- Из них по акции: {counters.get('buy_3_promo', 0)}\n"
            f"- 5 генераций (299 ₽): {counters.get('buy_5', 0)}\n"
            f"- 15 генераций (799 ₽): {counters.get('buy_15', 0)}\n"
            "\n🖼 Сгенерировано картинок:\n"
            f"- Карта желаний: {counters.get('gen_vision', 0)}\n"
            f"- Новый год: {counters.get('gen_ny', 0)}\n"
            f"- Семейный НГ: {counters.get('gen_ny_family', 0)}\n"
            f"- ДР (Молодым): {counters.get('gen_bday_young', 0)}\n"
            f"- ДР (Старшим): {counters.get('gen_bday_old', 0)}\n"
            f"- Доброе утро: {counters.get('gen_goodmorning', 0)}\n"
            f"- Уютный НГ: {counters.get('gen_ny_cozy', 0)}\n"
            f"- Зимняя сказка: {counters.get('gen_ny_fairytale', 0)}\n"
            f"- Элегантный НГ: {counters.get('gen_ny_elegant', 0)}\n"
            f"- ДР Старшим 2: {counters.get('gen_bday_old_elegant', 0)}\n"
            f"- Хэллоуин: {counters.get('gen_halloween', 0)}\n"
            f"- Скрапбукинг: {counters.get('gen_scrapbook', 0)}\n\n"
            f"Без разбивки по шаблонам (раньше): {counters.get('gen_unclassified', 0)}\n"
            f"🖼 Всего сгенерировано картинок: {data['total_generated']}\n"
            "\n🖱 Интерес к новым разделам:\n"
            f"- Фотосессия (клики): {counters.get('click_photo', 0)}\n"
            f"- Аудиопесни (клики): {counters.get('click_audio', 0)}\n\n"
            "📸 Интерес к Фотосессиям (клики):\n"
            f"- Стиль: Студия: {counters.get('click_photo_studio', 0)}\n"
            f"- Стиль: Прогулка: {counters.get('click_photo_walk', 0)}\n"
            f"- Стиль: Новогодняя: {counters.get('click_photo_ny', 0)}\n\n"
            f"- Тариф «Попробовать» (590): {counters.get('click_tariff_590', 0)}\n"
            f"- Тариф «Фотосессия» (990): {counters.get('click_tariff_990', 0)}\n"
            f"- Тариф «Премиум» (1490): {counters.get('click_tariff_1490', 0)}\n\n"
            f"💰 Всего покупок: {data['total_purchases']}" + source_text + admin_text
        )

    @router.message_created(Command("broadcast"))
    async def broadcast(event: MessageCreated) -> None:
        if is_bot_message(event) or admin_user_id is None or event.get_ids()[1] != admin_user_id:
            return
        parts = (event.message.body.text or "").split(maxsplit=1)
        if len(parts) != 2 or not parts[1].strip() or len(parts[1]) > 4000:
            await _reply(event, "Используйте /broadcast текст сообщения (до 4000 символов).")
            return
        if broadcast_lock.locked():
            await _reply(event, "Рассылка уже выполняется. Дождитесь завершения.")
            return
        async with broadcast_lock:
            recipients = await get_active_user_ids()
            sent = blocked = failed = 0
            stopped = False
            for user_id in recipients:
                # Пользователь мог стать недоступен уже после получения списка.
                user = await get_user(user_id)
                if user is None or not user["is_active"]:
                    continue
                try:
                    await event.bot.send_message(user_id=user_id, text=parts[1])
                    sent += 1
                except Exception as exc:
                    await log_max_error(exc, logger)
                    status = max_error_status(exc)
                    if status == 403:
                        await mark_user_inactive(user_id)
                        blocked += 1
                    else:
                        failed += 1
                        if status in {401, 429}:
                            stopped = True
                            break
                await asyncio.sleep(0.1)
            title = "Рассылка остановлена" if stopped else "Рассылка завершена"
            await _reply(event, f"{title}. Отправлено: {sent}. "
                         f"Недоступны (403): {blocked}. Ошибок: {failed}.")

    async def segmented_broadcast(event: MessageCreated, *, has_generated: bool) -> None:
        if is_bot_message(event) or admin_user_id is None or event.get_ids()[1] != admin_user_id:
            return
        if broadcast_lock.locked():
            await _reply(event, "Рассылка уже выполняется. Дождитесь завершения.")
            return
        async with broadcast_lock:
            recipients = await get_users_by_activity(has_generated=has_generated)
            segment = "сгенерировавших" if has_generated else "новых"
            await _reply(event, f"🚀 Начинаю рассылку для {len(recipients)} {segment} пользователей...")
            if not recipients:
                await _reply(event, "✅ Рассылка завершена! Успешно: 0, Ошибок: 0.")
                return
            # Полный альбом обязателен. Загружаем один раз и переиспользуем токены.
            examples = []
            try:
                for filename in BROADCAST_EXAMPLE_FILES:
                    path = EXAMPLES_DIR / filename
                    if not path.is_file():
                        raise FileNotFoundError(filename)
                    image = await event.bot.upload_media(InputMedia(path=str(path), type=UploadType.IMAGE))
                    if not isinstance(image.payload.token, str) or not image.payload.token.strip():
                        raise RuntimeError("MAX вернул пустой media token.")
                    examples.append(image)
            except Exception as exc:
                await log_max_error(exc, logger)
                await _reply(event, "Рассылка отменена: не удалось загрузить все 4 примера. Проверьте examples и логи.")
                return
            keyboard = InlineKeyboardBuilder()
            keyboard.row(CallbackButton(
                text="Купить 3 генерации за 149 ₽" if has_generated else "Попробовать бесплатно",
                payload="buy_promo_149" if has_generated else "open_main_menu",
            ))
            attachments = [*examples, keyboard.as_markup()]
            text = (
                "🎁 Мы обновили наш бот! Теперь доступно 5 новогодних открыток, Хэллоуин и элегантный День рождения!\n\n"
                "А для вас действует специальное предложение: **3 генерации за 149 ₽** (вместо 199 ₽).\n\n"
                "⏳ Цена действует 24 часа, но купленные генерации можно использовать в любое время и на любые картинки!\n\n"
                "Посмотрите новые примеры ниже 👇"
            ) if has_generated else (
                "✨ У нас появились новые волшебные открытки! Теперь можно создать Новогоднюю сказку, Хэллоуин или элегантное поздравление с вашим лицом.\n\n"
                "🎨 Первая открытка — бесплатно!\n\nПосмотрите новые примеры ниже 👇"
            )
            campaign_id = uuid4().hex
            success_count = failed_count = 0
            for user_id in recipients:
                try:
                    user = await get_user(user_id)
                    if user is None or not user["is_active"] or (user["total_generations"] > 0) != has_generated:
                        continue
                    if has_generated:
                        await start_broadcast_promo(user_id, campaign_id)
                    # Четыре фото и кнопка отправляются одним сообщением.
                    await event.bot.send_message(
                        user_id=user_id, text=text, attachments=attachments, parse_mode=ParseMode.MARKDOWN,
                    )
                    success_count += 1
                except Exception as exc:
                    failed_count += 1
                    logger.error(f"Ошибка отправки пользователю {user_id}: {exc}")
                    await log_max_error(exc, logger)
                    if max_error_status(exc) == 403:
                        try:
                            await mark_user_inactive(user_id)
                        except Exception as marking_error:
                            logger.error("Не удалось отметить пользователя %s недоступным: %s", user_id, marking_error)
                await asyncio.sleep(0.5)
            await _reply(event, f"✅ Рассылка завершена! Успешно: {success_count}, Ошибок: {failed_count}.")

    @router.message_created(Command("broadcast_gen"))
    async def broadcast_gen(event: MessageCreated) -> None:
        await segmented_broadcast(event, has_generated=True)

    @router.message_created(Command("broadcast_nogen"))
    async def broadcast_nogen(event: MessageCreated) -> None:
        await segmented_broadcast(event, has_generated=False)

    @router.message_created(Command("broadcast_reminder"))
    async def broadcast_reminder(event: MessageCreated) -> None:
        if is_bot_message(event) or admin_user_id is None or event.get_ids()[1] != admin_user_id:
            return
        if broadcast_lock.locked():
            await _reply(event, "Рассылка уже выполняется. Дождитесь завершения.")
            return
        async with broadcast_lock:
            recipients = await get_users_for_reminder()
            await _reply(event, f"⏳ Начинаю рассылку напоминаний для {len(recipients)} пользователей...")
            keyboard = InlineKeyboardBuilder().row(CallbackButton(
                text="Купить 3 генерации за 149 ₽", payload="buy_promo_149",
            )).as_markup()
            text = (
                "⏳ Напоминаем! Специальное предложение (3 генерации за 149 ₽) сгорит через 2-3 часа!\n\n"
                "Не упустите шанс создать красивые открытки для себя и близких. "
                "Купленные генерации останутся с вами навсегда, вы сможете использовать их, когда захотите."
            )
            success = failed = 0
            for user_id in recipients:
                try:
                    user = await get_user(user_id)
                    # Пока шла рассылка, скидку могли использовать или пользователь мог заблокировать бота.
                    if user is None or not user["is_active"] or not await is_promo_active(user_id):
                        continue
                    await event.bot.send_message(user_id=user_id, text=text, attachments=[keyboard])
                    success += 1
                except Exception as exc:
                    failed += 1
                    logger.error(f"Ошибка отправки пользователю {user_id}: {exc}")
                    await log_max_error(exc, logger)
                    if max_error_status(exc) == 403:
                        try:
                            await mark_user_inactive(user_id)
                        except Exception as marking_error:
                            logger.error("Не удалось отметить пользователя %s недоступным: %s", user_id, marking_error)
                await asyncio.sleep(0.5)
            await _reply(event, f"✅ Напоминания отправлены! Успешно: {success}, Ошибок: {failed}")

    async def send_menu_examples(event, filenames) -> bool:
        examples = []
        for filename in filenames:
            try:
                path = EXAMPLES_DIR / filename
                if not path.is_file():
                    raise FileNotFoundError(filename)
                image = await event.bot.upload_media(InputMedia(path=str(path), type=UploadType.IMAGE))
                if not isinstance(image.payload.token, str) or not image.payload.token.strip():
                    raise RuntimeError("MAX вернул пустой media token.")
                examples.append(image)
            except Exception as exc:
                logger.error("Не удалось загрузить пример %s: %s", filename, exc)
        if examples:
            try:
                await send_start_album(event, examples)
                return True
            except Exception as exc:
                logger.error("Не удалось отправить альбом примеров: %s", exc)
        return False

    async def send_welcome_message(
        event: MessageCreated | MessageCallback | BotStarted, context: MemoryContext,
        *, payload: str | None = None, timestamp_ms: int | None = None,
    ) -> None:
        user_id = event.user.user_id if isinstance(event, BotStarted) else event.get_ids()[1]
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
        await send_menu_examples(event, START_EXAMPLE_FILES)
        await _reply(event, text=WELCOME_TEXT, attachments=[main_keyboard()])
        if trial_granted:
            await _reply(event, "Вам начислен 1 бесплатный кредит!")

    @router.bot_started()
    async def bot_started(event: BotStarted, context: MemoryContext) -> None:
        if not event.user.is_bot:
            logger.info("Получено bot_started: отправляю приветствие пользователю %s", event.user.user_id)
            await send_welcome_message(event, context, payload=event.payload, timestamp_ms=event.timestamp)

    @router.message_created(CommandStart())
    async def start(event: MessageCreated, context: MemoryContext) -> None:
        if not is_bot_message(event):
            body = event.message.body
            parts = ((body.text or "").split(maxsplit=1) if body else [])
            await send_welcome_message(event, context, payload=parts[1].strip() if len(parts) > 1 else None)

    @router.message_callback(F.callback.payload.in_({"open_main_menu", "back_to_main"}))
    async def open_main_menu(event: MessageCallback, context: MemoryContext) -> None:
        if event.message is None:
            await event.ack(notification="Отправьте /start, чтобы открыть меню.")
            return
        await event.ack(notification="Открываю меню…")
        await send_welcome_message(event, context)

    @router.message_callback(F.callback.payload.in_({*CATEGORY_MENUS, "back_to_cards"}))
    async def category_menu(event: MessageCallback, context: MemoryContext) -> None:
        if event.message is None:
            await event.ack(notification="Меню недоступно. Отправьте /start.")
            return
        await event.ack(notification="Открываю…")
        await context.clear()
        await context.set_state(PosterStates.waiting_for_mode)
        screen = "cat_cards" if event.callback.payload == "back_to_cards" else event.callback.payload
        filenames, text, buttons = CATEGORY_MENUS[screen]
        interest_event = {"cat_photo": "click_photo", "cat_audio": "click_audio"}.get(screen)
        user_id = event.get_ids()[1]
        if interest_event is not None and user_id is not None:
            await increment_counter(interest_event, user_id=user_id)
        await send_menu_examples(event, filenames)
        await _reply(event, text=text, attachments=[navigation_keyboard(buttons)])

    async def show_photo_tariffs(event: MessageCallback, context: MemoryContext, style: str) -> None:
        await context.clear()
        await context.set_state(PosterStates.waiting_for_mode)
        await context.update_data(photo_style=style)
        await _reply(
            event, PHOTO_TARIFF_TEXT.format(style_name=PHOTO_STYLES[style]),
            attachments=[navigation_keyboard(PHOTO_TARIFF_BUTTONS)],
        )

    @router.message_callback(F.callback.payload.in_(set(PHOTO_STYLES)))
    async def photo_style_menu(event: MessageCallback, context: MemoryContext) -> None:
        if event.message is None:
            await event.ack(notification="Меню недоступно. Отправьте /start.")
            return
        await event.ack(notification="Показываю тарифы…")
        user_id = event.get_ids()[1]
        if user_id is not None:
            await increment_counter(PHOTO_CLICK_EVENTS[event.callback.payload], user_id=user_id)
        await show_photo_tariffs(event, context, event.callback.payload)

    @router.message_callback(F.callback.payload.in_({"photo_tariff_590", "photo_tariff_990", "photo_tariff_1490"}))
    async def photo_tariff_soon(event: MessageCallback, context: MemoryContext) -> None:
        if event.message is None:
            await event.ack(notification="Меню недоступно. Отправьте /start.")
            return
        data = await context.get_data()
        ny_trial = data.get("photo_style") == "photo_style_ny" and event.callback.payload == "photo_tariff_590"
        await event.ack(notification="Выберите сюжет…" if ny_trial else "Скоро будет доступно…")
        user_id = event.get_ids()[1]
        if user_id is not None:
            await increment_counter(PHOTO_CLICK_EVENTS[event.callback.payload], user_id=user_id)
        if ny_trial:
            await context.clear()
            await context.set_state(PosterStates.waiting_for_mode)
            await context.update_data(photo_style="photo_style_ny", photo_tariff="photo_tariff_590")
            buttons = tuple((label, payload) for payload, (label, _) in PHOTO_NY_TRIAL_TEMPLATES.items())
            await _reply(
                event, "Отличный выбор! Выберите сюжет для вашей новогодней фотосессии (3 фото):",
                attachments=[navigation_keyboard((*buttons, ("⬅️ Назад", "photo_tariff_back")))],
            )
            return
        await _reply(
            event, PHOTO_TARIFF_SOON_TEXT,
            attachments=[navigation_keyboard((("⬅️ Назад", "cat_photo"),))],
        )

    @router.message_callback(F.callback.payload == "photo_tariff_back")
    async def photo_tariff_back(event: MessageCallback, context: MemoryContext) -> None:
        if event.message is None:
            await event.ack(notification="Меню недоступно. Отправьте /start.")
            return
        await event.ack(notification="Показываю тарифы…")
        style = (await context.get_data()).get("photo_style")
        if style in PHOTO_STYLES:
            await show_photo_tariffs(event, context, style)
        else:
            await context.clear()
            await context.set_state(PosterStates.waiting_for_mode)
            _, text, buttons = CATEGORY_MENUS["cat_photo"]
            await _reply(event, text=text, attachments=[navigation_keyboard(buttons)])

    @router.message_callback(F.callback.payload.in_(set(PHOTO_NY_TRIAL_TEMPLATES)))
    async def photo_template_checkout(event: MessageCallback, context: MemoryContext) -> None:
        if event.message is None:
            await event.ack(notification="Меню недоступно. Отправьте /start.")
            return
        await event.ack(notification="Выбираю сюжет…")
        data = await context.get_data()
        if data.get("photo_style") != "photo_style_ny" or data.get("photo_tariff") != "photo_tariff_590":
            await _reply(
                event, "Выберите стиль и тариф фотосессии заново.",
                attachments=[navigation_keyboard((("📸 Фотосессия", "cat_photo"),))],
            )
            return
        template = PHOTO_NY_TRIAL_TEMPLATES[event.callback.payload][1]
        await context.update_data(
            template=template, photo_product_name="Фотосессия Попробовать", photo_amount_kopecks=59000,
        )
        # create_payment_link и webhook принимают только PACKAGES с кредитами.
        # Не выдаём тестовую ссылку за оплачиваемый заказ и не создаём заказ пакета кредитов.
        keyboard = InlineKeyboardBuilder().row(
            LinkButton(text="Оплатить 590 ₽", url=PHOTO_TRIAL_TEST_PAYMENT_URL)
        ).row(CallbackButton(text="⬅️ Назад", payload="photo_tariff_590")).as_markup()
        await _reply(event, PHOTO_TRIAL_CHECKOUT_TEXT, attachments=[keyboard])

    @router.message_callback(F.callback.payload.in_({"show_examples", "show_help"}))
    async def utility_menu(event: MessageCallback) -> None:
        if event.message is None:
            await event.ack(notification="Меню недоступно. Отправьте /start.")
            return
        await event.ack(notification="Открываю…")
        back = ("⬅️ Назад", "back_to_main")
        if event.callback.payload == "show_examples":
            sent = await send_menu_examples(event, START_EXAMPLE_FILES)
            await _reply(
                event,
                "🖼 Карты желаний и открытки с твоим лицом. Вернуться к выбору можно кнопкой ниже."
                if sent else "Примеры временно недоступны. Попробуйте позже.",
                attachments=[navigation_keyboard((back,))],
            )
        else:
            await _reply(
                event, HELP_TEXT + "\n\n" + HELP_ANSWERS["help_support"],
                parse_mode=ParseMode.HTML, attachments=[navigation_keyboard((back,))],
            )

    @router.message_created(Command("buy"))
    async def buy(event: MessageCreated) -> None:
        if is_bot_message(event):
            return
        await _reply(event,
            PACKAGES_TEXT,
            attachments=[await purchase_keyboard(event.get_ids()[1])],
        )

    @router.message_callback(F.callback.payload.in_({"buy_1", "buy_3", "buy_3_promo", "buy_promo_149", "buy_5", "buy_15"}))
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
        promo_requested = event.callback.payload in {"buy_3_promo", "buy_promo_149"}
        credits = 3 if promo_requested else int(event.callback.payload.removeprefix("buy_"))
        promo = promo_requested and await is_promo_active(user_id)
        amount, title = PACKAGES[credits]
        try:
            if promo:
                try:
                    link = await payments.create_payment_link(user_id, credits, promo=True)
                    amount = 14900
                except PromoUnavailableError:
                    promo = False
                    link = await payments.create_payment_link(user_id, credits)
            else:
                link = await payments.create_payment_link(user_id, credits)
        except Exception as exc:
            logger.error("Не удалось создать ссылку Prodamus (%s).", type(exc).__name__)
            await _reply(event, "Не удалось создать ссылку на оплату. Попробуйте /buy позже.")
            return
        keyboard = InlineKeyboardBuilder().row(
            LinkButton(text=f"Оплатить {amount // 100} ₽", url=link)
        ).as_markup()
        notice = "Скидка больше недоступна. Предлагаю обычный тариф.\n" if promo_requested and not promo else ""
        await _reply(event,
            notice + f"Для оплаты пакета «{title} — {amount // 100} ₽» перейдите по ссылке:\n{link}",
            attachments=[keyboard],
        )

    @router.message_created(Command("cancel"))
    async def cancel(event: MessageCreated, context: MemoryContext) -> None:
        if is_bot_message(event):
            return
        await context.clear()
        await context.set_state(PosterStates.waiting_for_mode)
        await _reply(event, "Сбор данных отменён.", attachments=[main_keyboard()])

    @router.message_callback(F.callback.payload.in_({"create_poster", "mode_bday", "mode_8march", *(payload for _, payload in MODE_BUTTONS)}))
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
        await _reply(event, photo_request(text))

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
            await _reply(event, text, attachments=[await purchase_keyboard(event.get_ids()[1])])
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
            await _reply(event, photo_request("Нужно одно фото, отправленное как изображение. /cancel — отмена."))
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
        photo = data.get("photo")
        if not isinstance(photo, dict) or not isinstance(photo.get("url"), str) or not photo["url"]:
            await context.set_state(PosterStates.waiting_for_photo)
            await _reply(event, photo_request("Фото не сохранилось. Пожалуйста, загрузите фото заново."))
            return
        body = event.message.body
        text = (body.text or "").strip() if body else ""
        if not text or len(text) > MAX_CARD_TEXT_LENGTH:
            await _reply(event, f"Напишите поздравление текстом — от 1 до {MAX_CARD_TEXT_LENGTH} символов.")
            return
        if data.get("card_text") != text:
            await context.update_data(
                variants=[], sent_count=0, batch_id=None, new_batch_requested=False,
                fixed_card_text=None, fixed_card_text_source=None,
            )
        await context.update_data(card_text=text, sphere=data["mode"])
        logger.info("Начинаю генерацию открытки с текстом: %s", text)
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
        (F.callback.payload == "more") | F.callback.payload.startswith("more:")
    )
    async def result_callback(event: MessageCallback, context: MemoryContext) -> None:
        if event.message is None:
            await event.ack(notification="Сообщение удалено. Отправьте /start.")
            return
        parts = (event.callback.payload or "").split(":")
        if len(parts) not in {1, 2}:
            await event.ack(notification="Кнопка недоступна.")
            return
        if await context.get_state() == PosterStates.generating:
            await event.ack(notification="Плакат уже создаётся. Подождите.")
            return
        mode = (await context.get_data()).get("mode", "mode_vision")
        if mode not in GENERATION_EVENTS:
            mode = "mode_vision"
        await event.ack(notification="Жду новое фото.")
        await context.clear()
        await context.update_data(mode=mode)
        await context.set_state(PosterStates.waiting_for_photo)
        await _reply(event, photo_request("Отправьте новое общее фото семьи. /cancel — отмена." if mode == "mode_ny_family"
                     else "Отправьте новое фото. /cancel — отмена."))

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
    async def echo(event: MessageCreated, context: MemoryContext) -> None:
        if is_bot_message(event):
            return
        state = await context.get_state()
        if state == PosterStates.waiting_for_card_text:
            # MAX останавливает поиск на первом подходящем обработчике:
            # простой return не передаст событие следующему хендлеру.
            logger.info("FSM: передаю текст из общего обработчика в waiting_for_card_text.")
            await collect_card_text(event, context)
            return
        if state is not None:
            logger.info("Общий обработчик пропустил сообщение в состоянии %s.", state)
            return
        body = event.message.body
        if body is not None and body.text is not None and body.text != "":
            await _reply(event, text=body.text)

    return router

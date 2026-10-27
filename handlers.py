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
    record_first_start, get_source_stats, get_stats_by_source, register_shared_image, get_shared_image,
    get_all_counters, increment_counter,
    get_advanced_stats, update_user_activity, mark_user_inactive, get_active_user_ids,
    is_promo_active, PromoUnavailableError, get_users_by_activity, start_broadcast_promo,
    get_users_for_reminder, get_audio_order,
)
from prodamus import PACKAGES, ProdamusPayments
from audio_service import generate_song, wait_for_song, song_tracks, build_song_request, AudioNotConfigured, SongFailed
from max_client import log_max_error
from image_service import (
    GenerationLimitError,
    GenerationNotConfigured,
    GenerationTimeoutError,
    UnsupportedPhotoFormatError,
    generate_vision_boards,
    generate_postcards,
    generate_photosession_frame, NY_FAMILY_THREE_PROMPTS, NY_FAMILY_PHOTOSESSION_PROMPTS,
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

PHOTO_MAIN_EXAMPLE_FILES = (
    r"photo_main_1.webp", r"photo_main_2.webp", r"photo_main_3.webp",
)
PHOTO_FAMILY_EXAMPLE_FILES = (
    r"photo_fam_1.webp", r"photo_fam_2.webp", r"photo_fam_3.webp",
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
        PHOTO_MAIN_EXAMPLE_FILES,
        (
            "📸 Ура! Новогодняя фотосессия уже доступна! 🎄\n\n"
            "Мы вовсю готовим для вас потрясающие образы, и вы уже можете попробовать наши новинки.\n\n"
            "В тарифе «Попробовать» доступны три варианта семейных фотосессий:\n"
            "• Семейная 3+ чел\n"
            "• Семейная 4+ чел\n"
            "• Семейная с малышами\n\n"
            "👇 Выбери стиль, который тебе интересен:"
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
            "🎵 Музыкальная мастерская!\n\n"
            "Мы создаем персональные песни с вашим именем или поздравлением. Выберите формат:\n\n"
            "🔥 «Песня по фану» (199 ₽)\n"
            "2 варианта на выбор. Без превью — сразу после оплаты. "
            "Если не понравится — 1 бесплатная перегенерация (ещё 2 варианта). "
            "Идеально, чтобы удивить друга или коллегу.\n\n"
            "🎁 «Песня в подарок» (490 ₽)\n"
            "Полный трек 2–3 минуты + минусовка + текст. "
            "Сначала послушайте превью с защитой (10–15 сек), чтобы понять стиль. "
            "После оплаты — чистый звук без водяных знаков и голосового сопровождения. "
            "Если что-то не так — бесплатно перегенерируем."
        ),
        (
            ("🔥 Песня по фану (199 ₽)", "audio_tariff_fun"),
            ("🎁 Песня в подарок (490 ₽)", "audio_tariff_gift"),
            ("⬅️ Назад", "back_to_main"),
        ),
    ),
}


AUDIO_CLICK_EVENTS = {
    "audio_tariff_fun": "click_audio_fun",
    "audio_tariff_gift": "click_audio_gift",
}

AUDIO_FUN_SCENARIOS = {
    "audio_fun_boss": ("🎤 Начальник года", "Напиши имя начальника и одну рабочую шутку или привычку (например: «Сергей, любит кофе»)"),
    "audio_fun_friend": ("💖 Лучшая подруга", "Напиши имя подруги и одно общее воспоминание или фразу (например: «Аня, любим вино и сериалы»)"),
    "audio_fun_cat": ("🐱 Гимн кота", "Напиши кличку кота и его привычку (например: «Барсик, орёт в 5 утра»)"),
    "audio_fun_student": ("🎓 Вечный студент", "Напиши имя друга и его специальность или фразу (например: «Иван, сдаёт сессию»)"),
    "audio_fun_universal": ("⏰ Поздравление на бегу", "Напиши имя и повод (например: «Маша, с днём рождения»)"),
    "audio_fun_meme": ("🌀 Абсурдный мем", "Напиши имя (если нужно) и выбери тему: Ода холодильнику / Песня про забытые ключи / Гимн пятнице"),
}

AUDIO_FUN_MENU_TEXT = (
    "🔥 Выбери сценарий для песни (30-40 секунд, 2 варианта на выбор). "
    "После выбора я попрошу пару деталей, и мы создадим хит!"
)

AUDIO_FUN_PAYMENT_TEXT = (
    "🎵 Ваша песня почти готова!\n\n"
    "Мы сгенерируем 2 варианта специально под ваш сценарий. Вы сможете выбрать лучший и скачать его в MP3.\n\n"
    "Что если не понравится? Мы бесплатно перегенерируем песню 1 раз — без вопросов и доплат. "
    "Просто нажмите «Не то, ещё раз» после прослушивания.\n\n"
    "199 ₽ — и через минуту у вас будет два трека на выбор."
)

AUDIO_FUN_SUCCESS_TEXT = (
    "🎧 Ваши треки готовы!\n\n"
    "Вариант 1: Трек 1 — (стиль)\n"
    "Вариант 2: Трек 2 — (стиль)\n\n"
    "Прослушайте оба и выберите лучший. Если ни один не подошёл — нажмите «Не то, ещё раз», "
    "и мы бесплатно сделаем ещё одну генерацию (2 новых варианта).\n\n"
    "Скачать понравившийся: нажмите на кнопку под треком."
)

AUDIO_GIFT_SUCCESS_TEXT = (
    "🎉 Ваша песня готова!\n\n"
    "Вот ваш полный трек: [ссылка на MP3]\n"
    "Минусовка: [ссылка]\n"
    "Текст песни: [ссылка]\n\n"
    "Если что-то пошло не так (файл не открывается, качество не устроило) — напишите в поддержку, "
    "мы перегенерируем бесплатно.\n\n"
    "Спасибо, что выбрали нас! Пусть подарок запомнится."
)

AUDIO_GIFT_TEXT = (
    "🎁 Песня в подарок — трек, который запомнится\n\n"
    "Полноценная песня 2–3 минуты с именем и деталями. После оплаты вы получите MP3, минусовку и текст. "
    "Сначала послушайте превью (10–15 секунд), чтобы понять стиль. "
    "Если что-то пойдёт не так — бесплатно перегенерируем."
)

AUDIO_GIFT_SCENARIOS = {
    "gift_scen_bday": ("🎂 День рождения", "Как зовут виновника торжества? Напишите 2-3 факта или привычки через запятую (например: любит горы, всегда поддерживает, обожает кофе)."),
    "gift_scen_anniv": ("💍 Годовщина / Свадьба", "Напишите ваши имена, сколько лет вместе и одну историю или деталь (например: Аня и Ваня, 5 лет, познакомились в универе)."),
    "gift_scen_parents": ("👵 Для родителей", "Как зовут маму или папу? Какой повод (юбилей, день рождения, просто так)? Напишите одно воспоминание или качество (например: всегда поддерживала, вкусные пироги)."),
    "gift_scen_child": ("🧸 Для ребёнка", "Как зовут ребёнка, сколько ему лет и какое у него увлечение или мечта? (например: Маша, 7 лет, любит рисовать)."),
    "gift_scen_friend": ("🤝 Для друга / подруги", "Как зовут друга/подругу? Напишите одну общую историю или шутку (например: всегда выручает, познакомились в универе)."),
    "gift_scen_custom": ("🎯 Свой вариант", "Напишите имя, повод (любой) и 1-2 факта о человеке."),
}

AUDIO_GIFT_SCENARIO_TEXT = (
    "Для кого песня?\n\n"
    "🎂 День рождения — персональное поздравление.\n"
    "💍 Годовщина / Свадьба — ваша история любви.\n"
    "👵 Для родителей — воспоминания и благодарность.\n"
    "🧸 Для ребёнка — увлечения и мечты.\n"
    "🤝 Для друга / подруги — общие истории и шутки.\n"
    "🎯 Свой вариант — любой человек и повод."
)

AUDIO_GIFT_PREVIEW_TEXT = (
    "🎁 Ваша песня в подарок почти готова!\n\n"
    "Послушайте фрагмент (10–15 секунд), чтобы понять стиль и настроение. "
    "Обратите внимание: это превью с защитой — в полной версии будет чистый звук без голосового сопровождения.\n\n"
    "Что вы получите после оплаты:\n"
    "• Полный трек (2–3 минуты) в MP3 — чистый звук, без водяных знаков\n"
    "• Минусовку (инструментал) — если захотите спеть сами\n"
    "• Текст песни — чтобы сохранить на память\n\n"
    "490 ₽ — и подарок готов. Если после прослушивания полной версии что-то пойдёт не так — "
    "мы бесплатно перегенерируем."
)


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
    "photo_temp_fam4": ("👨‍👩‍👧‍👦 Семейная 3+ чел", "fam4"),
    "photo_temp_toddlers": ("👶 Семейная с малышами", "toddlers"),
    "photo_temp_mom_daughter": ("👩‍👧 Мама + дочка", "mom_daughter"),
    "photo_temp_rabbit": ("🐰 С кроликом", "rabbit"),
}
PHOTO_TRIAL_PAYMENT_URL = "https://link.payform.ru/?paymentLinkId=72139edd-76b7-4dcf-b7e4-a34f6bd9c625"
PHOTO_TRIAL_CHECKOUT_TEXT = (
    "Вы выбрали тариф «Попробовать» (3 фото) за 590 ₽.\n\n"
    "Для продолжения, пожалуйста, оплатите заказ по ссылке ниже. После оплаты обязательно вернитесь в бота!"
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
    waiting_for_photosession_photo = State()
    waiting_for_prompt = State()
    waiting_for_card_text = State()
    waiting_for_song_details = State()
    waiting_for_audio_fun_payment = State()
    waiting_for_audio_test_result = State()
    waiting_for_audio_generation = State()
    waiting_for_audio_result = State()
    waiting_for_gift_bday = State()
    waiting_for_gift_anniv = State()
    waiting_for_gift_parents = State()
    waiting_for_gift_child = State()
    waiting_for_gift_friend = State()
    waiting_for_gift_custom = State()
    waiting_for_gift_preview = State()
    # Принимать описание и из состояния, сохранённого прежней версией.
    waiting_for_sphere = State()
    generating = State()


GIFT_SCENARIO_STATES = {
    "gift_scen_bday": PosterStates.waiting_for_gift_bday,
    "gift_scen_anniv": PosterStates.waiting_for_gift_anniv,
    "gift_scen_parents": PosterStates.waiting_for_gift_parents,
    "gift_scen_child": PosterStates.waiting_for_gift_child,
    "gift_scen_friend": PosterStates.waiting_for_gift_friend,
    "gift_scen_custom": PosterStates.waiting_for_gift_custom,
}


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


def photo_trial_payment_keyboard():
    return InlineKeyboardBuilder().row(
        LinkButton(text="Оплатить 590 ₽", url=PHOTO_TRIAL_PAYMENT_URL)
    ).row(CallbackButton(text="Выбрать сюжет", payload="photo_choose_template")).row(
        CallbackButton(text="⬅️ Назад", payload="photo_tariff_back")
    ).as_markup()


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
    photosession_generator: Callable[[str, str], Awaitable[str]] = generate_photosession_frame,
    song_generator: Callable[[str, str, str], Awaitable[str]] = generate_song,
    song_waiter: Callable[[str], Awaitable[dict]] = wait_for_song,
) -> Router:
    bot_link = validate_bot_link(bot_link)
    router = Router()
    router.register_outer_middleware(UserActivityMiddleware())
    broadcast_lock = asyncio.Lock()
    # FSM изолирован по чату, а баланс общий для всех чатов пользователя.
    generation_locks: WeakValueDictionary[int, asyncio.Lock] = WeakValueDictionary()

    def family_photosession_selected(data):
        return (data.get("photo_style") == "photo_style_ny"
                and data.get("photo_tariff") == "photo_tariff_590"
                and data.get("template") in NY_FAMILY_PHOTOSESSION_PROMPTS)

    async def run_family_photosession(event, context: MemoryContext) -> None:
        user_id = event.get_ids()[1]
        if user_id is None:
            await _reply(event, "Не удалось определить пользователя. Отправьте /start.")
            return
        if admin_user_id is None or user_id != admin_user_id:
            await context.set_state(PosterStates.waiting_for_mode)
            await _reply(event, PHOTO_TRIAL_CHECKOUT_TEXT, attachments=[photo_trial_payment_keyboard()])
            return
        lock = generation_locks.setdefault(user_id, asyncio.Lock())
        if lock.locked():
            await _reply(event, "Фотографии уже создаются. Пожалуйста, подождите.")
            return
        async with lock:
            data = await context.get_data()
            if not family_photosession_selected(data):
                await _reply(event, "Выберите новогоднюю фотосессию, тариф 590 ₽ и семейный сюжет.")
                return
            photo = data.get("photo")
            if not isinstance(photo, dict) or not photo.get("url"):
                await context.set_state(PosterStates.waiting_for_photosession_photo)
                await _reply(event, photo_request(
                    "Отправьте одно общее фото, где хорошо видны лица всех трёх членов семьи. /cancel — отмена."
                    if data["template"] == "fam3" else
                    "Отправьте одно общее фото семьи с малышом или малышами, где хорошо видны лица всех участников. /cancel — отмена."
                    if data["template"] == "toddlers" else
                    "Отправьте одно общее фото семьи из 3 и более человек, где хорошо видны лица детей и родителей. /cancel — отмена."
                ))
                return
            if data.get("photosession_complete"):
                await _reply(event, "Все 3 фотографии уже отправлены.")
                return
            session_id = data.get("photosession_session_id") or uuid4().hex
            await context.update_data(photosession_session_id=session_id)
            await context.set_state(PosterStates.generating)
            try:
                urls = list(data.get("photosession_urls", []))
                sent_count = data.get("photosession_sent_count", 0)
                reference_url = photo["url"]
                for index, prompt in enumerate(NY_FAMILY_PHOTOSESSION_PROMPTS[data["template"]]):
                    if (await context.get_data()).get("photosession_session_id") != session_id:
                        return
                    if index >= len(urls):
                        await _reply(event, f"Генерирую кадр {index + 1} из 3... ⏳ Это может занять 3-5 минут. Пожалуйста, не закрывайте чат!")
                        url = await photosession_generator(reference_url, prompt)
                        if not isinstance(url, str) or not url.startswith("https://"):
                            raise RuntimeError("Генератор не вернул HTTPS-ссылку кадра.")
                        if (await context.get_data()).get("photosession_session_id") != session_id:
                            return
                        urls.append(url)
                        await context.update_data(photosession_urls=list(urls))
                    if index >= sent_count:
                        image = await upload_image_for_max(event.bot, urls[index])
                        sent = await event.bot.send_message(
                            **_recipient(event), text=f"Фотография {index + 1} из 3", attachments=[image],
                        )
                        if sent is None:
                            raise RuntimeError("MAX не подтвердил отправку фотографии.")
                        if (await context.get_data()).get("photosession_session_id") != session_id:
                            return
                        await context.update_data(photosession_sent_count=index + 1)
                await context.update_data(photosession_complete=True)
                await context.set_state(PosterStates.waiting_for_mode)
                await _reply(event, "Готово! Все 3 фотографии отправлены.", attachments=[main_keyboard()])
            except Exception as exc:
                logger.error("Ошибка новогодней фотосессии для %s: %s", user_id, exc, exc_info=True)
                await log_max_error(exc, logger)
                if (await context.get_data()).get("photosession_session_id") != session_id:
                    return
                await context.set_state(PosterStates.waiting_for_mode)
                await _reply(
                    event, "Не удалось завершить фотосессию. Готовые кадры сохранены. Нажмите «Продолжить», чтобы повторить с места остановки.",
                    attachments=[navigation_keyboard((("Продолжить", f"photo_temp_{data['template']}"), ("⬅️ Назад", "cat_photo")))],
                )

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
        yandex = await get_stats_by_source("yandex_newyear", admin_id=admin_user_id)
        campaign_text = (
            "\n\n📍 Статистика по кампании «Yandex New Year»:\n"
            f"👥 Новые пользователи: {yandex['total_users']}\n"
            f"🖼 Сгенерировали: {yandex['total_generated']}\n"
            f"💳 Покупатели: {yandex['total_buyers']}\n"
            f"💰 Оплат: {yandex['total_revenue']:.2f} ₽".replace(".", ",")
        )
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
            "\n📸 Выбор шаблонов Фотосессий:\n"
            f"- Семейная 3+: {counters.get('select_photo_fam3', 0)}\n"
            f"- Семейная 4+: {counters.get('select_photo_fam4', 0)}\n"
            f"- Семейная с малышами: {counters.get('select_photo_toddlers', 0)}\n"
            "\n🎵 Активность Аудиопесен:\n"
            f"- Нажали «Не то, ещё раз» (регенерация): {counters.get('audio_regenerate_click', 0)}\n"
            f"- Скачали MP3: {counters.get('audio_download_mp3', 0)}\n"
            f"- Скачали минусовку: {counters.get('audio_download_inst', 0)}\n"
            "\n🖱 Интерес к новым разделам:\n"
            f"- Фотосессия (клики): {counters.get('click_photo', 0)}\n"
            f"- Аудиопесни (клики): {counters.get('click_audio', 0)}\n"
            f"- Песня по фану (199 ₽): {counters.get('click_audio_fun', 0)}\n"
            f"- Песня в подарок (490 ₽): {counters.get('click_audio_gift', 0)}\n\n"
            "🎵 Интерес к Аудиопесням (сценарии):\n"
            + "\n".join(
                f"- {label}: {counters.get('audio_scen_' + payload.removeprefix('audio_fun_'), 0)}"
                for payload, (label, _) in AUDIO_FUN_SCENARIOS.items()
            ) + "\n\n"
            "🎁 Интерес к Аудиопесням (Подарочный формат):\n"
            + "\n".join(
                f"- {label}: {counters.get('audio_gift_' + payload.removeprefix('gift_scen_'), 0)}"
                for payload, (label, _) in AUDIO_GIFT_SCENARIOS.items()
            ) + "\n\n"
            "📸 Интерес к Фотосессиям (клики):\n"
            f"- Стиль: Студия: {counters.get('click_photo_studio', 0)}\n"
            f"- Стиль: Прогулка: {counters.get('click_photo_walk', 0)}\n"
            f"- Стиль: Новогодняя: {counters.get('click_photo_ny', 0)}\n\n"
            f"- Тариф «Попробовать» (590): {counters.get('click_tariff_590', 0)}\n"
            f"- Тариф «Фотосессия» (990): {counters.get('click_tariff_990', 0)}\n"
            f"- Тариф «Премиум» (1490): {counters.get('click_tariff_1490', 0)}\n\n"
            f"💰 Всего покупок: {data['total_purchases']}" + source_text + admin_text + campaign_text
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

    @router.message_callback(F.callback.payload.in_(set(AUDIO_CLICK_EVENTS)))
    async def audio_tariff_soon(event: MessageCallback, context: MemoryContext) -> None:
        if event.message is None:
            await event.ack(notification="Меню недоступно. Отправьте /start.")
            return
        await event.ack(notification="Показываю тариф…")
        user_id = event.get_ids()[1]
        if user_id is not None:
            await increment_counter(AUDIO_CLICK_EVENTS[event.callback.payload], user_id=user_id)
        await context.clear()
        await context.set_state(PosterStates.waiting_for_mode)
        if event.callback.payload == "audio_tariff_fun":
            buttons = [(label, payload) for payload, (label, _) in AUDIO_FUN_SCENARIOS.items()]
            await _reply(event, AUDIO_FUN_MENU_TEXT,
                         attachments=[navigation_keyboard((*buttons, ("⬅️ Назад", "cat_audio")))])
            return
        await _reply(
            event,
            AUDIO_GIFT_TEXT,
            attachments=[navigation_keyboard((("Выбрать сценарий", "gift_choose_scenario"),
                                              ("⬅️ Назад", "cat_audio")))],
        )

    @router.message_callback(F.callback.payload.in_(set(AUDIO_FUN_SCENARIOS)))
    async def audio_fun_scenario(event: MessageCallback, context: MemoryContext) -> None:
        if event.message is None:
            await event.ack(notification="Меню недоступно. Отправьте /start.")
            return
        await event.ack(notification="Жду детали песни…")
        scenario = event.callback.payload.removeprefix("audio_fun_")
        user_id = event.get_ids()[1]
        if user_id is not None:
            await increment_counter("audio_scen_" + scenario, user_id=user_id)
        await context.clear()
        await context.update_data(song_scenario=scenario)
        await context.set_state(PosterStates.waiting_for_song_details)
        await _reply(event, AUDIO_FUN_SCENARIOS[event.callback.payload][1],
                     attachments=[navigation_keyboard((("⬅️ Назад", "audio_tariff_fun"),))])

    @router.message_callback(F.callback.payload == "gift_choose_scenario")
    async def gift_choose_scenario(event: MessageCallback, context: MemoryContext) -> None:
        if event.message is None:
            await event.ack(notification="Меню недоступно. Отправьте /start.")
            return
        await event.ack(notification="Выберите сценарий…")
        await context.clear()
        await context.set_state(PosterStates.waiting_for_mode)
        buttons = [(label, payload) for payload, (label, _) in AUDIO_GIFT_SCENARIOS.items()]
        await _reply(event, AUDIO_GIFT_SCENARIO_TEXT,
                     attachments=[navigation_keyboard((*buttons, ("⬅️ Назад", "audio_tariff_gift")))])

    @router.message_callback(F.callback.payload.in_(set(AUDIO_GIFT_SCENARIOS)))
    async def gift_scenario(event: MessageCallback, context: MemoryContext) -> None:
        if event.message is None:
            await event.ack(notification="Меню недоступно. Отправьте /start.")
            return
        await event.ack(notification="Жду детали песни…")
        payload = event.callback.payload
        scenario = payload.removeprefix("gift_scen_")
        user_id = event.get_ids()[1]
        if user_id is not None:
            await increment_counter("audio_gift_" + scenario, user_id=user_id)
        await context.clear()
        await context.update_data(gift_scenario=scenario)
        await context.set_state(GIFT_SCENARIO_STATES[payload])
        await _reply(event, AUDIO_GIFT_SCENARIOS[payload][1],
                     attachments=[navigation_keyboard((("⬅️ Назад", "gift_choose_scenario"),))])

    async def send_song_result(event, tracks: list[dict], audio_format: str) -> None:
        text = "🎧 Ваши треки готовы!" if len(tracks) > 1 else "🎧 Ваша песня готова!"
        buttons = []
        for index, track in enumerate(tracks, 1):
            text += f"\n\nВариант {index}: {track['title']}\n{track['audioUrl']}"
            payload = f"audio_download_fun_{index}" if audio_format == "fun" else f"audio_download_gift_{index}"
            buttons.append((f"Скачать вариант {index}", payload))
        buttons.append(("⬅️ Назад", "cat_audio"))
        await _reply(event, text, attachments=[navigation_keyboard(buttons)])

    async def prepare_audio_checkout(context, user_id, audio_format, data):
        order_id = data.get('audio_checkout_id') or f'audio-{user_id}-{uuid4().hex}'
        await context.update_data(audio_checkout_id=order_id)
        existing_order = await get_audio_order(order_id)
        if existing_order and existing_order['status'] == 'paid':
            return order_id, None
        link = data.get('audio_checkout_link')
        if not link:
            scenario = data['song_scenario' if audio_format == 'fun' else 'gift_scenario']
            details = data['song_details' if audio_format == 'fun' else 'gift_details']
            scenarios = AUDIO_FUN_SCENARIOS if audio_format == 'fun' else AUDIO_GIFT_SCENARIOS
            scenario_key = ('audio_fun_' if audio_format == 'fun' else 'gift_scen_') + scenario
            prompt, tags, title = build_song_request(details, scenarios[scenario_key][0], audio_format, data.get('song_style'))
            link = await payments.create_audio_payment_link(user_id, audio_format, scenario, details, data.get('song_style'), prompt, tags, title, order_id=order_id)
            if not isinstance(link, str) or not link.startswith('https://'):
                raise ValueError('Не получена ссылка Prodamus.')
            if (await context.get_data()).get('audio_checkout_id') == order_id:
                await context.update_data(audio_checkout_link=link)
        return order_id, link

    async def audio_payment_keyboard(event, context, audio_format, buttons):
        fallback = navigation_keyboard(buttons)
        user_id = event.get_ids()[1]
        if user_id is None or user_id == admin_user_id or payments is None:
            return fallback
        lock = generation_locks.setdefault(user_id, asyncio.Lock())
        if lock.locked():
            return fallback
        try:
            async with lock:
                order_id, link = await prepare_audio_checkout(context, user_id, audio_format, await context.get_data())
                if link is None or (await context.get_data()).get('audio_checkout_id') != order_id:
                    return fallback
                keyboard = InlineKeyboardBuilder()
                pay_payload = 'audio_fun_payment_soon' if audio_format == 'fun' else 'gift_payment_soon'
                for text, payload in buttons:
                    keyboard.row(LinkButton(text=text, url=link) if payload == pay_payload else CallbackButton(text=text, payload=payload))
                return keyboard.as_markup()
        except Exception as exc:
            logger.error('Не удалось подготовить кнопку оплаты аудиопесни (%s).', type(exc).__name__)
            return fallback  # Старый callback позволит повторить получение ссылки.

    async def run_song_generation(event: MessageCallback, context: MemoryContext, audio_format: str) -> None:
        if event.message is None:
            await event.ack(notification="Меню недоступно. Отправьте /start.")
            return
        user_id = event.get_ids()[1]
        if user_id is None:
            await event.ack(notification="Не удалось определить пользователя.")
            return
        if admin_user_id is None or user_id != admin_user_id:
            if payments is None:
                await event.ack(notification="Оплата пока недоступна.")
                await _reply(event, "💳 Оплата аудиопесен пока недоступна. Мы скоро подключим оплату — следите за обновлениями!",
                             attachments=[navigation_keyboard((("⬅️ Назад", "audio_tariff_fun" if audio_format == "fun" else "gift_choose_scenario"),))])
                return
            checkout_lock = generation_locks.setdefault(user_id, asyncio.Lock())
            if checkout_lock.locked():
                await event.ack(notification='Ссылка уже формируется. Пожалуйста, подождите.')
                return
            async with checkout_lock:
                await event.ack(notification='Готовлю ссылку на оплату…')
                data = await context.get_data()
                expected = PosterStates.waiting_for_audio_fun_payment if audio_format == 'fun' else PosterStates.waiting_for_gift_preview
                scenario = data.get('song_scenario' if audio_format == 'fun' else 'gift_scenario')
                details = data.get('song_details' if audio_format == 'fun' else 'gift_details')
                scenarios = AUDIO_FUN_SCENARIOS if audio_format == 'fun' else AUDIO_GIFT_SCENARIOS
                scenario_key = ('audio_fun_' if audio_format == 'fun' else 'gift_scen_') + str(scenario)
                if await context.get_state() != expected or scenario_key not in scenarios or not isinstance(details, str) or not details.strip():
                    await _reply(event, 'Выберите сценарий и заполните детали песни заново.')
                    return
                try:
                    order_id, link = await prepare_audio_checkout(context, user_id, audio_format, data)
                    if link is None:
                        await _reply(event, 'Оплата уже получена. Я пришлю песню по этому заказу; повторно платить не нужно.')
                        return
                    if (await context.get_data()).get('audio_checkout_id') != order_id:
                        return
                    label = 'Оплатить 199 ₽ и получить 2 варианта' if audio_format == 'fun' else 'Оплатить 490 ₽ и получить полный трек'
                    keyboard = InlineKeyboardBuilder().row(LinkButton(text=label, url=link)).row(
                        CallbackButton(text='⬅️ Назад', payload='audio_tariff_fun' if audio_format == 'fun' else 'gift_choose_scenario')).as_markup()
                    await _reply(event, f'💳 Для оплаты перейдите по ссылке ниже. После оплаты я автоматически пришлю песню.\nЗаказ: {order_id}', attachments=[keyboard])
                except Exception as exc:
                    logger.error('Не удалось создать аудиозаказ (%s).', type(exc).__name__)
                    await _reply(event, 'Не удалось сформировать ссылку на оплату. Попробуйте позже; повторно оплачивать ранее оплаченный заказ не нужно.')
            return
        lock = generation_locks.setdefault(user_id, asyncio.Lock())
        if lock.locked():
            await event.ack(notification="Генерация уже идёт. Пожалуйста, подождите.")
            return
        async with lock:
            data = await context.get_data()
            state = await context.get_state()
            if data.get("audio_format") == audio_format and data.get("audio_tracks"):
                await event.ack(notification="Показываю готовые треки…")
                await send_song_result(event, data["audio_tracks"], audio_format)
                return
            expected = PosterStates.waiting_for_audio_fun_payment if audio_format == "fun" else PosterStates.waiting_for_gift_preview
            scenario = data.get("song_scenario" if audio_format == "fun" else "gift_scenario")
            details = data.get("song_details" if audio_format == "fun" else "gift_details")
            scenarios = AUDIO_FUN_SCENARIOS if audio_format == "fun" else AUDIO_GIFT_SCENARIOS
            prefix = "audio_fun_" if audio_format == "fun" else "gift_scen_"
            if state != expected or prefix + str(scenario) not in scenarios or not isinstance(details, str) or not details.strip():
                await event.ack(notification="Выберите сценарий и заполните детали песни заново.")
                return
            if data.get("audio_submit_started") and not data.get("audio_job_id"):
                await event.ack(notification="Статус отправки неизвестен. Не запускаем повторную генерацию.")
                await _reply(event, "Не удалось подтвердить отправку в TTAPI. Обратитесь в поддержку перед новым запуском.",
                             attachments=[navigation_keyboard((("Написать в поддержку", "help_support"),))])
                return
            await event.ack(notification="Генерирую песню…")
            session = data.get("audio_session_id") or uuid4().hex
            await context.update_data(audio_session_id=session, audio_format=audio_format)
            await context.set_state(PosterStates.waiting_for_audio_generation)

            async def session_current():
                return (await context.get_data()).get("audio_session_id") == session

            try:
                if not data.get("audio_job_id"):
                    await _reply(event, "👑 Режим администратора: Оплата пропущена. Запускаю генерацию...")
                    if not await session_current():
                        return
                await _reply(event, "🎵 Генерирую ваши треки... Это займет около 1-2 минут.")
                if not await session_current():
                    return
                job_id = data.get("audio_job_id")
                if not job_id:
                    prompt, tags, title = build_song_request(details, scenarios[prefix + scenario][0], audio_format, data.get('song_style'))
                    await context.update_data(audio_submit_started=True)
                    job_id = await song_generator(prompt, tags, title)
                    if not await session_current():
                        return
                    await context.update_data(audio_job_id=job_id)
                result = await song_waiter(job_id)
                if not await session_current():
                    return
                tracks = song_tracks(result)
                await context.update_data(audio_tracks=tracks)
                await context.set_state(PosterStates.waiting_for_audio_result)
                await send_song_result(event, tracks, audio_format)
            except AudioNotConfigured:
                logger.warning("Генерация музыки не настроена: задайте TTAPI_KEY.")
                if await session_current():
                    await context.update_data(audio_submit_started=False)
                    await _reply(event, "Генерация музыки временно недоступна. Попробуйте позже.")
            except SongFailed:
                if await session_current():
                    await _reply(event, "TTAPI сообщил об ошибке генерации песни. Обратитесь в поддержку.")
            except TimeoutError:
                if await session_current():
                    await _reply(event, "Песня ещё не готова. Нажмите ту же кнопку ещё раз: проверю уже созданную задачу без новой генерации.")
            except Exception as exc:
                logger.error("Ошибка аудиогенерации (%s).", type(exc).__name__)
                if await session_current():
                    await _reply(event, "Не удалось получить или отправить песню. Повторите проверку той же кнопкой. Если задача не была подтверждена, обратитесь в поддержку.")
            finally:
                if await session_current() and await context.get_state() == PosterStates.waiting_for_audio_generation:
                    await context.set_state(expected)

    @router.message_callback(F.callback.payload == "gift_payment_soon")
    async def gift_payment_soon(event: MessageCallback, context: MemoryContext) -> None:
        await run_song_generation(event, context, "gift")

    @router.message_callback(F.callback.payload == "audio_fun_payment_soon")
    async def audio_fun_payment_soon(event: MessageCallback, context: MemoryContext) -> None:
        await run_song_generation(event, context, "fun")

    @router.message_callback(F.callback.payload.startswith('audio_paid_download:'))
    async def paid_audio_download(event: MessageCallback) -> None:
        if event.message is None:
            await event.ack(notification='Меню недоступно. Отправьте /start.')
            return
        parts = event.callback.payload.split(':')
        order = await get_audio_order(parts[1]) if len(parts) == 3 else None
        if (not order or order['user_id'] != event.get_ids()[1] or order['status'] != 'paid'
                or parts[2] not in {'0', '1'} or not order['tracks_json']):
            await event.ack(notification='Трек недоступен.')
            return
        import json
        tracks = json.loads(order['tracks_json'])
        index = int(parts[2])
        if index >= len(tracks):
            await event.ack(notification='Трек недоступен.')
            return
        await increment_counter('audio_download_mp3', user_id=order['user_id'])
        await event.ack(notification='Ссылка на MP3 готова.')
        url = tracks[index]['audioUrl']
        await _reply(event, url, attachments=[InlineKeyboardBuilder().row(LinkButton(text='Скачать MP3', url=url)).as_markup()])

    @router.message_callback(F.callback.payload.in_({"audio_gift_preview", "audio_download_fun_1", "audio_download_fun_2", "audio_download_gift_1", "audio_download_gift_2",
                                                     "audio_download_gift_mp3", "audio_download_gift_instrumental"}))
    async def audio_file_placeholder(event: MessageCallback, context: MemoryContext) -> None:
        if event.message is None:
            await event.ack(notification="Меню недоступно. Отправьте /start.")
            return
        counter = {
            "audio_download_fun_1": "audio_download_mp3",
            "audio_download_fun_2": "audio_download_mp3",
            "audio_download_gift_mp3": "audio_download_mp3",
            "audio_download_gift_1": "audio_download_mp3",
            "audio_download_gift_2": "audio_download_mp3",
            "audio_download_gift_instrumental": "audio_download_inst",
        }.get(event.callback.payload)
        user_id = event.get_ids()[1]
        if counter is not None and user_id is not None:
            # Пока файлы не подключены, учитываем нажатие кнопки скачивания.
            await increment_counter(counter, user_id=user_id)
        data = await context.get_data()
        tracks = data.get("audio_tracks", [])
        index = {"audio_download_fun_1": ("fun", 0), "audio_download_fun_2": ("fun", 1),
                 "audio_download_gift_mp3": ("gift", 0), "audio_download_gift_1": ("gift", 0),
                 "audio_download_gift_2": ("gift", 1)}.get(event.callback.payload)
        if index and data.get("audio_format") == index[0] and len(tracks) > index[1]:
            url = tracks[index[1]]["audioUrl"]
            await event.ack(notification="Ссылка на MP3 готова.")
            await _reply(event, url, attachments=[InlineKeyboardBuilder().row(LinkButton(text="Скачать MP3", url=url)).as_markup()])
            return
        await event.ack(notification="Аудио скоро появится")
        await _reply(event, "Аудио скоро появится")

    def audio_fun_result_keyboard(*, can_regenerate: bool):
        buttons = [("Скачать вариант 1", "audio_download_fun_1"), ("Скачать вариант 2", "audio_download_fun_2")]
        if can_regenerate:
            buttons.append(("Не то, ещё раз (бесплатно)", "audio_regenerate"))
        return navigation_keyboard(buttons)

    @router.message_created(Command("test_audio_fun_success"))
    async def test_audio_fun_success(event: MessageCreated, context: MemoryContext) -> None:
        if is_bot_message(event) or admin_user_id is None or event.get_ids()[1] != admin_user_id:
            return
        await context.clear()
        await context.update_data(audio_test_format="fun", audio_regeneration_used=False)
        await context.set_state(PosterStates.waiting_for_audio_test_result)
        await _reply(event, AUDIO_FUN_SUCCESS_TEXT + "\n\n🧪 Тестовый экран: файлы ещё не созданы.",
                     attachments=[audio_fun_result_keyboard(can_regenerate=True)])

    @router.message_created(Command("test_audio_gift_success"))
    async def test_audio_gift_success(event: MessageCreated, context: MemoryContext) -> None:
        if is_bot_message(event) or admin_user_id is None or event.get_ids()[1] != admin_user_id:
            return
        await context.clear()
        await context.update_data(audio_test_format="gift")
        await context.set_state(PosterStates.waiting_for_audio_test_result)
        await _reply(event, AUDIO_GIFT_SUCCESS_TEXT + "\n\n🧪 Тестовый экран: файлы ещё не созданы.",
                     attachments=[navigation_keyboard((("Скачать MP3", "audio_download_gift_mp3"),
                                                       ("Скачать минусовку", "audio_download_gift_instrumental"),
                                                       ("Написать в поддержку", "help_support")))])

    @router.message_callback(F.callback.payload == "audio_regenerate")
    async def audio_regenerate(event: MessageCallback, context: MemoryContext) -> None:
        if event.message is None:
            await event.ack(notification="Меню недоступно. Отправьте /start.")
            return
        user_id = event.get_ids()[1]
        if user_id is not None:
            await increment_counter("audio_regenerate_click", user_id=user_id)
        data = await context.get_data()
        if await context.get_state() != PosterStates.waiting_for_audio_test_result or data.get("audio_test_format") != "fun":
            await event.ack(notification="Треки ещё не созданы. Выберите формат в меню аудиопесен.")
            return
        if data.get("audio_regeneration_used"):
            await event.ack(notification="Бесплатная перегенерация уже использована.")
            return
        await context.update_data(audio_regeneration_used=True)
        await event.ack(notification="Бесплатная перегенерация: тестовый режим.")
        await _reply(event, "🎧 Ещё 2 варианта песни — тестовый экран. Аудио скоро появится.\n\n"
                     "Бесплатная перегенерация использована.",
                     attachments=[audio_fun_result_keyboard(can_regenerate=False)])

    async def show_photo_tariffs(event: MessageCallback, context: MemoryContext, style: str) -> None:
        await context.clear()
        await context.set_state(PosterStates.waiting_for_mode)
        await context.update_data(photo_style=style)
        if style == "photo_style_ny":
            await send_menu_examples(event, PHOTO_FAMILY_EXAMPLE_FILES)
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

    async def show_photo_template_options(event: MessageCallback, *, with_intro: bool = True) -> None:
        if with_intro:
            await _reply(event, "Выберите сюжет новогодней фотосессии (3 фото):")
        await send_menu_examples(event, PHOTO_FAMILY_EXAMPLE_FILES)
        buttons = tuple((label, payload) for payload, (label, _) in PHOTO_NY_TRIAL_TEMPLATES.items())
        await _reply(
            event, "👇 Выберите подходящий сюжет:",
            attachments=[navigation_keyboard((*buttons, ("⬅️ Назад", "photo_tariff_back")))],
        )

    @router.message_callback(F.callback.payload.in_({"photo_tariff_590", "photo_tariff_990", "photo_tariff_1490"}))
    async def photo_tariff_soon(event: MessageCallback, context: MemoryContext) -> None:
        if event.message is None:
            await event.ack(notification="Меню недоступно. Отправьте /start.")
            return
        data = await context.get_data()
        ny_trial = data.get("photo_style") == "photo_style_ny" and event.callback.payload == "photo_tariff_590"
        await event.ack(notification="Показываю ссылку…" if ny_trial else "Скоро будет доступно…")
        user_id = event.get_ids()[1]
        if user_id is not None:
            await increment_counter(PHOTO_CLICK_EVENTS[event.callback.payload], user_id=user_id)
        if ny_trial:
            await context.clear()
            await context.set_state(PosterStates.waiting_for_mode)
            await context.update_data(
                photo_style="photo_style_ny", photo_tariff="photo_tariff_590", tariff="trial_590",
                photo_product_name="Фотосессия Попробовать", photo_amount_kopecks=59000,
            )
            await _reply(event, PHOTO_TRIAL_CHECKOUT_TEXT, attachments=[photo_trial_payment_keyboard()])
            await show_photo_template_options(event, with_intro=False)
            return
        await _reply(
            event, PHOTO_TARIFF_SOON_TEXT,
            attachments=[navigation_keyboard((("⬅️ Назад", "cat_photo"),))],
        )

    @router.message_callback(F.callback.payload == "photo_choose_template")
    async def photo_choose_template(event: MessageCallback, context: MemoryContext) -> None:
        if event.message is None:
            await event.ack(notification="Меню недоступно. Отправьте /start.")
            return
        await event.ack(notification="Выберите сюжет…")
        data = await context.get_data()
        if data.get("photo_style") != "photo_style_ny" or data.get("photo_tariff") != "photo_tariff_590":
            await _reply(event, "Выберите стиль и тариф фотосессии заново.", attachments=[navigation_keyboard((("Фотосессия", "cat_photo"),))])
            return
        await show_photo_template_options(event)

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
        if await context.get_state() == PosterStates.generating:
            await event.ack(notification="Фотографии уже создаются. Пожалуйста, подождите.")
            return
        await event.ack(notification="Выбираю сюжет…")
        data = await context.get_data()
        if PHOTO_NY_TRIAL_TEMPLATES[event.callback.payload][1] in NY_FAMILY_PHOTOSESSION_PROMPTS and data.get("photo_style") not in (None, "photo_style_ny"):
            await _reply(event, PHOTO_TARIFF_SOON_TEXT, attachments=[navigation_keyboard((("⬅️ Назад", "cat_photo"),))])
            return
        if data.get("photo_style") != "photo_style_ny" or data.get("photo_tariff") != "photo_tariff_590":
            await _reply(
                event, "Выберите стиль и тариф фотосессии заново.",
                attachments=[navigation_keyboard((("📸 Фотосессия", "cat_photo"),))],
            )
            return
        template = PHOTO_NY_TRIAL_TEMPLATES[event.callback.payload][1]
        if template in {"fam3", "fam4", "toddlers"}:
            user_id = event.get_ids()[1]
            if user_id is not None:
                await increment_counter("select_photo_" + template, user_id=user_id)
        await context.update_data(
            template=template, photo_product_name="Фотосессия Попробовать", photo_amount_kopecks=59000,
        )
        if template in NY_FAMILY_PHOTOSESSION_PROMPTS and admin_user_id is not None and event.get_ids()[1] == admin_user_id:
            if (data.get("template") == template and data.get("photosession_admin_bypass")
                    and data.get("photosession_session_id")):
                await run_family_photosession(event, context)
                return
            # Новый запуск администратора всегда запрашивает свежее общее фото.
            await context.clear()
            await context.update_data(
                photo_style="photo_style_ny", photo_tariff="photo_tariff_590", tariff="trial_590",
                template=template, photosession=True, photosession_admin_bypass=True,
                photo_product_name="Фотосессия Попробовать", photo_amount_kopecks=59000,
            )
            await context.set_state(PosterStates.waiting_for_photosession_photo)
            await _reply(
                event, "👑 Режим администратора: Оплата пропущена. Пожалуйста, загрузите общее фото семьи (3 человека) для генерации."
                if template == "fam3" else
                "👑 Режим администратора: Оплата пропущена. Пожалуйста, загрузите общее фото семьи с малышом или малышами для генерации."
                if template == "toddlers" else
                "👑 Режим администратора: Оплата пропущена. Пожалуйста, загрузите общее фото семьи (3 и более человек) для генерации."
            )
            return
        # Готовая ссылка Prodamus используется без создания заказа пакета кредитов.
        # Возврат и подтверждение оплаты фотосессии подключаются отдельно.
        await _reply(event, PHOTO_TRIAL_CHECKOUT_TEXT, attachments=[photo_trial_payment_keyboard()])

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

    @router.message_created(PosterStates.waiting_for_photosession_photo)
    async def collect_photosession_photo(event: MessageCreated, context: MemoryContext) -> None:
        if is_bot_message(event):
            return
        data = await context.get_data()
        if not family_photosession_selected(data):
            await context.clear()
            await context.set_state(PosterStates.waiting_for_mode)
            await _reply(event, "Выберите фотосессию заново.", attachments=[main_keyboard()])
            return
        body = event.message.body
        photos = [a for a in (getattr(body, "attachments", None) or []) if a.type == "image"
                  and isinstance(a.payload, PhotoAttachmentPayload) and a.payload.url] if body else []
        if len(photos) != 1:
            await _reply(event, photo_request(
                "Нужно одно общее фото семьи из трёх человек, отправленное как изображение."
                if data["template"] == "fam3" else
                "Нужно одно общее фото семьи с малышом или малышами, отправленное как изображение."
                if data["template"] == "toddlers" else
                "Нужно одно общее фото семьи из 3 и более человек, отправленное как изображение."
            ))
            return
        await context.update_data(photo=photos[0].payload.model_dump())
        await run_family_photosession(event, context)

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

    @router.message_created(PosterStates.waiting_for_gift_bday)
    @router.message_created(PosterStates.waiting_for_gift_anniv)
    @router.message_created(PosterStates.waiting_for_gift_parents)
    @router.message_created(PosterStates.waiting_for_gift_child)
    @router.message_created(PosterStates.waiting_for_gift_friend)
    @router.message_created(PosterStates.waiting_for_gift_custom)
    async def collect_gift_details(event: MessageCreated, context: MemoryContext) -> None:
        if is_bot_message(event):
            return
        data = await context.get_data()
        payload = "gift_scen_" + str(data.get("gift_scenario", ""))
        if payload not in GIFT_SCENARIO_STATES or await context.get_state() != GIFT_SCENARIO_STATES[payload]:
            await context.clear()
            await context.set_state(PosterStates.waiting_for_mode)
            await _reply(event, "Выберите сценарий подарочной песни заново.",
                         attachments=[navigation_keyboard((("Выбрать сценарий", "gift_choose_scenario"),))])
            return
        body = event.message.body
        details = (body.text or "").strip() if body else ""
        if not details:
            await _reply(event, "Напишите детали песни текстом. /cancel — отмена.")
            return
        await context.update_data(gift_details=details)
        await context.set_state(PosterStates.waiting_for_gift_preview)
        await _reply(event, AUDIO_GIFT_PREVIEW_TEXT, attachments=[await audio_payment_keyboard(event, context, "gift", (
            ("▶️ Послушать превью", "audio_gift_preview"),
            ("Оплатить 490 ₽ и получить полный трек", "gift_payment_soon"),
            ("⬅️ Назад", "gift_choose_scenario"),
        ))])

    @router.message_created(PosterStates.waiting_for_gift_preview)
    async def gift_preview_message(event: MessageCreated) -> None:
        if not is_bot_message(event):
            await _reply(event, "Это тестовое превью. Чтобы изменить детали, выберите сценарий заново.",
                         attachments=[navigation_keyboard((("⬅️ Назад", "gift_choose_scenario"),))])

    @router.message_created(PosterStates.waiting_for_song_details)
    async def collect_song_details(event: MessageCreated, context: MemoryContext) -> None:
        if is_bot_message(event):
            return
        data = await context.get_data()
        if "audio_fun_" + str(data.get("song_scenario", "")) not in AUDIO_FUN_SCENARIOS:
            await context.clear()
            await context.set_state(PosterStates.waiting_for_mode)
            await _reply(event, "Выбери сценарий песни заново.",
                         attachments=[navigation_keyboard((("🔥 Песня по фану", "audio_tariff_fun"),))])
            return
        body = event.message.body
        details = (body.text or "").strip() if body else ""
        if not details:
            await _reply(event, "Напиши имя и детали песни текстом. /cancel — отмена.")
            return
        await context.update_data(song_details=details)
        await context.set_state(PosterStates.waiting_for_audio_fun_payment)
        await _reply(event, AUDIO_FUN_PAYMENT_TEXT, attachments=[await audio_payment_keyboard(event, context, "fun", (
            ("Оплатить 199 ₽ и получить 2 варианта", "audio_fun_payment_soon"),
            ("⬅️ Назад", "audio_tariff_fun"),
        ))])

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
        if state in GIFT_SCENARIO_STATES.values():
            await collect_gift_details(event, context)
            return
        if state == PosterStates.waiting_for_gift_preview:
            await gift_preview_message(event)
            return
        if state == PosterStates.waiting_for_song_details:
            await collect_song_details(event, context)
            return
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

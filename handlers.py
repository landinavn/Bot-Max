"""Меню и FSM: режим → фото → мечта или готовый праздничный шаблон."""

import asyncio
import logging
import json
from collections.abc import Awaitable, Callable
from pathlib import Path
from urllib.parse import unquote, urlencode, urlsplit
from uuid import uuid4
from time import time_ns
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
from video_service import build_video_prompt, generate_video_from_avatar, CARTOON_VIDEO_PROMPTS, create_video_task_with_retry as create_video_task, poll_video_task
from media_service import VIDEO_MERGE_POLICY, MUSICAL_VIDEO_POLICY, MUSICAL_VIDEO_WAIT_MESSAGE, merge_video_and_audio, create_musical_video, upload_video_for_max, upload_audio_for_max
from audio_service import create_audio_preview, song_audio_url, TEACHER_MUSIC_TAGS
from metrika_service import extract_yclid, send_offline_conversion

from database import (
    create_cartoon_order, get_cartoon_order, latest_paid_cartoon, finish_cartoon_order, update_cartoon_order,
    create_musical_order, get_musical_order,
    deduct_credit, get_user, grant_free_trial, get_global_stats, increment_generations,
    record_first_start, save_yclid, get_source_stats, get_stats_by_source, register_shared_image, get_shared_image,
    get_all_counters, increment_counter,
    get_advanced_stats, update_user_activity, mark_user_inactive, get_active_user_ids,
    is_promo_active, PromoUnavailableError, get_users_by_activity, start_broadcast_promo,
    get_users_for_reminder, get_audio_order, create_audio_order, find_gift_preview_order,
    update_audio_order, grant_audio_admin_access, request_audio_regeneration,
    check_song_promo, song_checkout_order, latest_gift_preview, claim_audio_order,
    save_pending_video, get_all_pending_videos, mark_video_completed, save_pending_video_result,
)
from prodamus import PACKAGES, ProdamusPayments
from audio_service import generate_song, wait_for_song, wait_for_song_result, make_protected_preview, TEACHER_SCENARIO_TITLE, song_tracks, build_song_request, write_song_lyrics, validate_song_arguments, InvalidSongArguments, AudioNotConfigured, SongFailed
from audio_orders import PaidAudioWorker, SUPPORT_URL
from max_client import log_max_error
from video_delivery import send_video_share
from image_service import (
    count_people_in_photo, create_cartoon_avatar_preview, CARTOON_IMAGE_PROMPTS,
    GenerationLimitError,
    GenerationNotConfigured,
    GenerationTimeoutError,
    UnsupportedPhotoFormatError,
    generate_vision_boards,
    generate_postcards,
    generate_cartoon_avatar, generate_photosession_frame, NY_FAMILY_THREE_PROMPTS, NY_FAMILY_PHOTOSESSION_PROMPTS,
    fix_text_with_ai,
    POSTCARD_PROMPTS,
    MAX_CARD_TEXT_LENGTH, MAX_THEME_INFO_LENGTH,
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
    "mode_teacher": "gen_teacher",
    "mode_coach": "gen_coach",
}

COACH_DETAILS_PROMPT = 'Напишите имя тренера, лучший тренер по .... (например: «Иван, лучший тренер по футболу»).'
ROLE_CARD_FIELDS = {'mode_teacher': 'teacher_info', 'mode_coach': 'coach_info'}
ROLE_CARD_QUESTIONS = {
    'mode_teacher': 'Напишите имя учителя и его предмет (например: «Ирина Петровна, математика»).',
    'mode_coach': COACH_DETAILS_PROMPT,
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
BROADCAST_UPDATE_TEXT = (
    '🎉 У нас большое обновление! Твой нейро-художник научился ещё большему! 🎨✨\n\n'
    'Теперь в боте можно создавать:\n'
    '🎵 Персональные песни — трогательную песню в подарок или весёлую песню по фану.\n'
    '💌 Музыкальные открытки — поздравления, которые не только приятно смотреть, но и слушать!\n'
    '🎄 Новогодние нейрофотосессии — волшебные праздничные фотографии с твоим лицом.\n'
    '🎬 Ожившие открытки — поздравления в формате видео.\n\n'
    'А скоро появятся мульт-открытки, настоящие мульт-поздравления и медитации! ✨\n\n'
    '🏆 Кстати, 30 октября — День тренера! Успей создать необычный музыкальный подарок любимому тренеру!\n\n'
    '👇 Нажми кнопку ниже, чтобы открыть обновлённое меню и посмотреть все новинки!\n\n'
    'Спасибо, что ты с нами! ❤️'
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
    ("👨‍🏫 Учителям", "mode_teacher"),
    ("🏆 Тренерам", "mode_coach"),
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


VIDEO_SONG_CUSTOM_TEXT_PROMPT = (
    'Отличный выбор! Теперь напишите имя и фразу для припева '
    '(например: «Иван, лучший тренер по футболу!» или «Маша, с днём рождения!»). '
    'Эта фраза обязательно прозвучит в песне.'
)


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

SONG_PROMO_EXPIRED_TEXT = 'К сожалению, 48 часов уже прошли. Вы можете приобрести песню по обычной цене 490 ₽.'

CATEGORY_BUTTONS = (
    ("🎵 Музыкальная открытка — Новинка 🔥", "cat_video"),
    ("🎬 Мульт-открытка (10-15 сек) — Новинка 🔥", "cat_cartoon"),
    ("🎬 Мульт-поздравление (30 сек) — Новинка 🔥", "cat_cartoon_long"),
    ("📸 Нейрофотосессия", "cat_photo"),
    ("🎵 Аудиопесни", "cat_audio"),
    ("🎁 Открытки", "cat_cards"),
    ("🔮 Карта Желаний", "cat_vision"),
    ("🧘 Медитации", "cat_meditation"),
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
            ("👨‍🏫 Учителям", "mode_teacher"),
            ("🏆 Тренерам", "mode_coach"),
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
            ("🎄 Новогодняя", "photo_style_ny"),
            ("🏙 Студия", "photo_style_studio"),
            ("🌳 Прогулка", "photo_style_walk"),
            ("⬅️ Назад", "back_to_main"),
        ),
    ),
    "cat_meditation": (
        (),
        "🧘‍♀️ Медитации на исполнение желаний\n\n"
        "Этот раздел уже в разработке! Скоро вы сможете слушать аудио-медитации и настраиваться на исполнение мечт. Следите за обновлениями! 🚀",
        (("⬅️ Назад", "back_to_main"),),
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
            "После оплаты — чистый звук без водяных знаков и звуковой метки. "
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
    'audio_fun_boss': ('🎤 Начальник года', 'Напишите имя начальника и фразу для припева (например: «Сергей, лучший босс!»).'),
    'audio_fun_friend': ('💖 Лучшая подруга', 'Напишите имя подруги и фразу для припева (например: «Аня, ты моя сестра!»).'),
    'audio_fun_cat': ('🐱 Гимн кота', 'Напишите кличку кота и фразу для припева (например: «Барсик, самый крутой кот!»).'),
    'audio_fun_student': ('🎓 Вечный студент', 'Напишите имя друга и фразу для припева (например: «Иван, сдай сессию!»).'),
    'audio_fun_universal': ('⏰ Поздравление на бегу', 'Напишите имя и фразу для припева (например: «Маша, с днём рождения!»).'),
    'audio_fun_meme': ('🌀 Абсурдный мем', 'Напишите тему мема и фразу для припева (например: «Холодильник, ты мой герой!»).'),
    'audio_fun_ny': ('🎄 Новогодняя', 'Напишите имя и фразу для припева (например: «С Новым годом, друзья!»).'),
}

AUDIO_FUN_MENU_TEXT = (
    "🔥 Выбери сценарий для песни (30-40 секунд, 2 варианта на выбор). "
    "После выбора я попрошу имя и фразу для припева, и мы создадим хит!"
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

TEACHER_DETAILS_PROMPT = 'Напишите имя тренера/учителя и фразу для припева (например: «Иван, лучший тренер по футболу»).'

AUDIO_GIFT_SCENARIOS = {
    'gift_scen_bday': ('🎂 День рождения', 'Напишите имя и фразу для припева (например: «Маша, будь счастлива!»).'),
    'gift_scen_anniv': ('💍 Годовщина / Свадьба', 'Напишите имена и фразу для припева (например: «Аня и Ваня, навсегда!»).'),
    'gift_scen_parents': ('👵 Для родителей', 'Напишите имя и фразу для припева (например: «Мама, спасибо за всё!»).'),
    'gift_scen_child': ('🧸 Для ребёнка', 'Напишите имя и фразу для припева (например: «Саша, расти большой!»).'),
    'gift_scen_friend': ('🤝 Для друга / подруги', 'Напишите имя и фразу для припева (например: «Игорь, ты лучший друг!»).'),
    'gift_scen_custom': ('🎯 Свой вариант', 'Напишите имя, повод и фразу для припева.'),
    'audio_gift_ny': ('🎄 Новогодняя', 'Напишите имя и фразу для припева (например: «С Новым годом, друзья!»).'),
    "audio_gift_teacher": (TEACHER_SCENARIO_TITLE, TEACHER_DETAILS_PROMPT),
}

AUDIO_GIFT_SCENARIO_TEXT = (
    "Для кого песня?\n\n"
    "🎂 День рождения — персональное поздравление.\n"
    "💍 Годовщина / Свадьба — ваша история любви.\n"
    "👵 Для родителей — воспоминания и благодарность.\n"
    "🧸 Для ребёнка — увлечения и мечты.\n"
    "🤝 Для друга / подруги — общие истории и шутки.\n"
    "🎯 Свой вариант — любой человек и повод.\n"
    "🎄 Новогодняя — семейные воспоминания и тёплые пожелания.\n"
    "👨‍🏫 Для тренера / учителя — энергичный гимн, мотивация и благодарность."
)



def gift_scenario_name(payload):
    return {'audio_gift_ny': 'ny', 'audio_gift_teacher': 'teacher'}.get(payload, payload.removeprefix('gift_scen_'))


def audio_scenario_key(scenario, audio_format):
    if audio_format == 'gift' and scenario in {'ny', 'teacher'}:
        return 'audio_gift_' + scenario
    return ('audio_fun_' if audio_format == 'fun' else 'gift_scen_') + str(scenario)


AUDIO_FUN_COUNTERS = {key: 'audio_scen_' + key.removeprefix('audio_fun_') for key in AUDIO_FUN_SCENARIOS}
AUDIO_FUN_COUNTERS['audio_fun_ny'] = 'audio_scen_ny_fun'
AUDIO_GIFT_COUNTERS = {key: 'audio_gift_' + gift_scenario_name(key) for key in AUDIO_GIFT_SCENARIOS}
AUDIO_GIFT_COUNTERS['audio_gift_ny'] = 'audio_scen_ny_gift'
AUDIO_GIFT_COUNTERS['audio_gift_teacher'] = 'audio_scen_teacher_gift'

VIDEO_SHORT_SCENARIOS = {**AUDIO_FUN_SCENARIOS, 'audio_video_teacher': (TEACHER_SCENARIO_TITLE, TEACHER_DETAILS_PROMPT)}
VIDEO_SONG_SCENARIOS = {**VIDEO_SHORT_SCENARIOS, **{key: value for key, value in AUDIO_GIFT_SCENARIOS.items() if key != 'audio_gift_teacher'}}
VIDEO_CARD_SCREENS = {'cat_cards', 'sub_ny', 'sub_bday', 'sub_halloween', 'sub_goodmorning'}
VIDEO_PAYMENT_PLACEHOLDER_URL = 'https://link.payform.ru/'  # TODO: Заменить на реальную ссылку
VIDEO_UPSELL_TEXT = (
    'Ваша открытка готова! 🎄\n\n'
    '✨ А хотите сделать её настоящим музыкальным подарком? Мы можем наложить на вашу открытку короткую персональную песню (30-40 секунд) и получится красивая музыкальная открытка, которую можно отправить в мессенджере.'
)



CARTOON_PRODUCTS = {
    'cat_cartoon': ('short', 59000, '🎬 Создаем короткий мультфильм с вашим лицом! Выберите стиль:', '5-10'),
    'cat_cartoon_long': ('long', 129000, '🎬 Создаем полноценное мульт-поздравление с сюжетом! Выберите стиль:', '15-20'),
}
CARTOON_AVATAR_EVENTS = {'avatar_ok_1': 'avatar_ok_first', 'avatar_bad_1': 'avatar_bad_first',
                       'avatar_ok_2': 'avatar_ok_second', 'avatar_bad_2': 'avatar_bad_second'}
CARTOON_THEMES = {
    'theme_ny': '🎄 Новый год', 'theme_bday': '🎂 День рождения',
    'theme_bday_kid': '👶 День рождения малыша', 'theme_valentine': '💖 14 февраля',
    'theme_feb23': '🛡️ 23 февраля', 'theme_mar8': '🌷 8 марта',
    'theme_mother': '🤱 День матери', 'theme_father': '👨‍👧 День отца',
}
CARTOON_PHOTO_PROMPT = '📸 Отправьте четкое фото (анфас, хорошее освещение). Мы нарисуем вашего персонажа!'
CARTOON_SONG_PHRASE_PROMPT = 'Теперь напишите имя и фразу для припева (например: «Маша, с днём рождения!»).'
CARTOON_COMING_SOON = '🎬 Раздел «Мульт-открытка» уже совсем скоро заработает! Мы заканчиваем настройку оплаты, и в ближайшее время вы сможете создавать настоящие мультфильмы с вашим лицом. Следите за обновлениями! 🚀'
CARTOON_VIDEO_WAIT = '🎬 Запускаю магию! Генерация видео займет от 3 до 5 минут. Пожалуйста, не закрывайте чат.'


CARTOON_TEST_SONG_URL = 'https://example.com/cartoon-test-song.mp3'  # TODO: Подключить выбранную песню.


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
    waiting_for_card_role_info = State()
    waiting_for_photosession_photo = State()
    waiting_for_cartoon_style = State()
    waiting_for_avatar_photo = State()
    waiting_for_avatar_review = State()
    waiting_for_cartoon_song = State()
    waiting_for_cartoon_song_phrase = State()
    waiting_for_cartoon_music_review = State()
    waiting_for_cartoon_payment = State()
    cartoon_generating = State()
    cartoon_manual_review = State()
    waiting_for_prompt = State()
    waiting_for_card_text = State()
    waiting_for_video_song = State()
    waiting_for_video_song_phrase = State()
    waiting_for_video_payment = State()
    waiting_for_video_teacher_details = State()
    generating_video_teacher = State()
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
    waiting_for_gift_ny = State()
    waiting_for_gift_teacher = State()
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
    "audio_gift_ny": PosterStates.waiting_for_gift_ny,
    "audio_gift_teacher": PosterStates.waiting_for_gift_teacher,
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


async def generate_video(avatar_url: str, song_url: str, *, mode: str = '') -> str:
    """Оживить аватар и склеить результат с уже готовой песней из очереди."""
    if not song_url or is_video_placeholder(song_url):
        raise ValueError('Для видео нужна готовая песня, а не тестовая ссылка.')
    video_url = await generate_video_from_avatar(avatar_url, mode=mode)
    if is_video_placeholder(video_url):
        return video_url
    return await merge_video_and_audio(video_url, song_url)


VIDEO_GENERATION_MESSAGE = (
    '🎬 Запускаю генерацию видео с вашим аватаром и песней.\n\n'
    '⏳ Это может занять от 5 до 15 минут (в зависимости от очереди на сервере). '
    'Пожалуйста, не закрывайте чат — я пришлю видео, как только оно будет готово!'
)

PENDING_VIDEO_MESSAGE = (
    '🎬 Видео-сервис временно настраивается. Мы сохранили ваш аватар и песню! '
    'Как только сервис заработает, мы автоматически создадим и пришлем вам видео. '
    'Вам не нужно ничего делать заново.'
)


def video_cache(data):
    keys = ('video_raw_source', 'video_raw_url', 'video_song_source', 'video_song_url',
            'video_song_job_source', 'video_song_job_id', 'video_song_lyrics',
            'video_merged_source', 'video_merged_url', 'audio_url', 'video_people_source', 'video_people_count',
            'video_song_preview_source', 'video_song_preview_url')
    return {key: data[key] for key in keys if key in data}


def is_video_placeholder(result) -> bool:
    if not isinstance(result, str):
        return False
    lower = result.lower()
    if any(marker in lower for marker in ('заглушка', 'видеосервис будет подключ', 'тестовая генерация')):
        return True
    try:
        host = urlsplit(result).hostname or ''
    except ValueError:
        return False
    return host == 'example.com' or host.endswith('.example.com')


def create_router(
    generator: PosterGenerator = generate_vision_boards,
    *, payments: ProdamusPayments | None = None,
    admin_user_id: int | None = None,
    postcard_generator: Callable[[str, str, str], Awaitable[list[str]]] = generate_postcards,
    bot_link: str = DEFAULT_BOT_LINK,
    photosession_generator: Callable[[str, str], Awaitable[str]] = generate_photosession_frame,
    song_generator: Callable[[str, str, str], Awaitable[str]] | None = None,
    song_waiter: Callable[[str], Awaitable[dict | str]] | None = None,
    lyric_writer=None,
    video_generator: Callable[[dict], Awaitable[str]] | None = None,
    avatar_generator: Callable[[str, str], Awaitable[str]] | None = None,
    cartoon_video_generator: Callable[[str, str], Awaitable[str]] | None = None,
) -> Router:
    default_card_video = video_generator is None
    avatar_generator = avatar_generator or generate_cartoon_avatar
    default_cartoon_video = cartoon_video_generator is None
    cartoon_video_generator = cartoon_video_generator or generate_video

    async def prepare_video_song(data, scenario):
        custom_song = data.get('video_flow') and data.get('video_product') == 'direct'
        details = (data.get('song_custom_text') if custom_song else
                   data.get('video_song_phrase') or data.get('video_teacher_details') or data.get('song_details') or data.get('card_text')
                   or data.get('teacher_info') or data.get('coach_info') or 'Пожелания счастья, радости и исполнения мечты')
        if not isinstance(details, str) or not details.strip():
            raise ValueError('Не сохранена фраза для припева. Выберите песню и введите текст заново.')
        source = [scenario, details, 'custom_text'] if custom_song else [scenario, details]
        if data.get('video_song_source') == source and data.get('video_song_url'):
            return data['video_song_url']
        existing = data.get('chosen_song_url') if data.get('chosen_song') == scenario else None
        if existing and not custom_song and not is_video_placeholder(existing):
            data.update(video_song_source=source, video_song_url=existing)
            return existing
        if data.get('video_song_job_source') != source or not data.get('video_song_job_id'):
            label = CARTOON_THEMES[data['cartoon_theme']] if data.get('cartoon_theme') else VIDEO_SONG_SCENARIOS[scenario][0]
            notes, tags, title = build_song_request(details, label, 'fun',
                TEACHER_MUSIC_TAGS if data.get('mode') == 'mode_coach' else None)
            lyrics = data.get('video_song_lyrics') if scenario == 'audio_video_teacher' and not custom_song else None
            if not lyrics:
                lyrics = await lyric_writer(notes, tags, title)
            validate_song_arguments(lyrics, tags, title)
            data.update(video_song_job_source=source, video_song_lyrics=lyrics, video_song_job_id=None)
            data['video_song_job_id'] = await song_generator(lyrics, tags, title)
        result = await song_waiter(data['video_song_job_id'])
        audio_url = song_audio_url(result)
        if is_video_placeholder(audio_url):
            raise ValueError('TTAPI вернул тестовую ссылку вместо песни.')
        data.update(video_song_source=source, video_song_url=audio_url)
        return audio_url

    async def render_video_with_song(data, image_url, scenario):
        audio_url = await prepare_video_song(data, scenario)
        mode = (data.get('cartoon_mode') if data.get('cartoon_flow') else data.get('mode')) or ''
        people_count = data.get('video_people_count') if data.get('video_people_source') == image_url else None
        if not isinstance(people_count, int) or isinstance(people_count, bool) or people_count < 0:
            people_count = await count_people_in_photo(image_url)
            data.update(video_people_source=image_url, video_people_count=people_count)
        source = [image_url, mode, people_count, build_video_prompt(mode, people_count)]
        raw = data.get('video_raw_url') if data.get('video_raw_source') == source else None
        raw = raw or await generate_video_from_avatar(image_url, mode=mode, people_count=people_count)
        if is_video_placeholder(raw):
            return raw
        data.update(video_raw_source=source, video_raw_url=raw)
        key = [VIDEO_MERGE_POLICY, raw, audio_url]
        if data.get('video_merged_source') != key or not data.get('video_merged_url'):
            data['video_merged_url'] = await merge_video_and_audio(raw, audio_url)
            data['video_merged_source'] = key
        return data['video_merged_url']

    async def prepare_card_video_image(data: dict) -> str:
        image_url = data.get('video_source_image')
        if not image_url:
            mode = data.get('mode')
            photo = data.get('photo') or {}
            if mode not in POSTCARD_PROMPTS or not photo.get('url') or not data.get('card_text'):
                raise ValueError('Не сохранены фото, шаблон или поздравление для видео.')
            fixed_text = data.get('fixed_card_text')
            if data.get('fixed_card_text_source') != data['card_text'] or not fixed_text:
                fixed_text = await fix_text_with_ai(data['card_text'])
            kwargs = {'theme_info': data[ROLE_CARD_FIELDS[mode]]} if mode in ROLE_CARD_FIELDS else {}
            # Превью содержит PREVIEW: для готового видео создаём чистую открытку.
            urls = await postcard_generator(photo['url'], mode, fixed_text, **kwargs)
            if len(urls) != 1 or not isinstance(urls[0], str) or not urls[0].startswith('https://'):
                raise ValueError('Генератор открытки не вернул HTTPS-ссылку.')
            image_url = urls[0]
            data['video_source_image'] = image_url
        return image_url

    async def generate_card_video(data: dict, *, progress=None) -> str:
        image_url = await prepare_card_video_image(data)
        audio_url = await prepare_video_song(data, data['video_song_scenario'])
        key = [MUSICAL_VIDEO_POLICY, image_url, audio_url]
        if data.get('video_merged_source') != key or not data.get('video_merged_url'):
            if progress is not None:
                await progress()
            data['video_merged_url'] = await create_musical_video(image_url, audio_url)
            data['video_merged_source'] = key
        return data['video_merged_url']

    video_generator = video_generator or generate_card_video
    lyric_writer = lyric_writer or write_song_lyrics
    song_generator = song_generator or generate_song
    # Подарок возвращает URL; импульсный формат сохраняет оба варианта TTAPI.
    fun_song_waiter = song_waiter or wait_for_song_result
    song_waiter = song_waiter or wait_for_song
    bot_link = validate_bot_link(bot_link)
    router = Router()
    router.register_outer_middleware(UserActivityMiddleware())
    broadcast_lock = asyncio.Lock()
    pending_video_lock = asyncio.Lock()
    # FSM изолирован по чату, а баланс общий для всех чатов пользователя.
    generation_locks: WeakValueDictionary[int, asyncio.Lock] = WeakValueDictionary()
    preview_locks: WeakValueDictionary[int, asyncio.Lock] = WeakValueDictionary()
    cartoon_locks: WeakValueDictionary[int, asyncio.Lock] = WeakValueDictionary()
    video_text_locks: WeakValueDictionary[int, asyncio.Lock] = WeakValueDictionary()
    video_locks: WeakValueDictionary[int, asyncio.Lock] = WeakValueDictionary()

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
                        await send_offline_conversion(user_id, goal_id="bot_generation")
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

        if data.get('video_flow'):
            await _reply(event, 'Создание видео будет доступно после подключения оплаты. Выберите действие в меню.', attachments=[main_keyboard()])
            return

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
                    kwargs = {'theme_info': data[ROLE_CARD_FIELDS[mode]]} if mode in ROLE_CARD_FIELDS else {}
                    urls = await postcard_generator(photo["url"], data["mode"], fixed_text, **kwargs)
                    if mode in ROLE_CARD_FIELDS and (await context.get_data()).get('card_role_session') != data.get('card_role_session'):
                        return
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
                    video_upsell_sent=False,
                )
                await send_offline_conversion(credit_user_id, goal_id="bot_generation")
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
            if is_postcard and not data.get('video_upsell_sent'):
                keyboard = InlineKeyboardBuilder().row(
                    CallbackButton(text='🎵 Наложить песню на открытку — 149 ₽', payload='upsell_music')
                ).as_markup()
                await _reply(event, VIDEO_UPSELL_TEXT, attachments=[keyboard])
                await context.update_data(video_upsell_sent=True)
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

    @router.message_created(Command('process_pending_videos'))
    async def process_pending_videos(event: MessageCreated):
        if is_bot_message(event) or admin_user_id is None or event.get_ids()[1] != admin_user_id:
            return
        if pending_video_lock.locked():
            await _reply(event, 'Отложенные видео уже обрабатываются. Дождитесь завершения.')
            return
        async with pending_video_lock:
            jobs = await get_all_pending_videos()
            await _reply(event, f'🎬 Начинаю обработку отложенных видео: {len(jobs)}.')
            success = failed = 0
            for job in jobs:
                try:
                    result = job.get('result_url')
                    if not result:
                        if job.get('render_kind') == 'musical':
                            await event.bot.send_message(user_id=job['user_id'], text=MUSICAL_VIDEO_WAIT_MESSAGE)
                            result = await create_musical_video(job['avatar_url'], job['song_url'])
                        else:
                            # Старые и мульт-задачи сохраняют исходный адаптер Kling.
                            result = (await generate_video(job['avatar_url'], job['song_url'], mode=job.get('mode') or '')
                                      if default_cartoon_video else await cartoon_video_generator(job['avatar_url'], job['song_url']))
                        if is_video_placeholder(result) or not isinstance(result, str) or not result.startswith('https://'):
                            raise ValueError('Видеосервис пока не вернул готовое видео.')
                        await save_pending_video_result(job['id'], result)
                    attachments = [InlineKeyboardBuilder().row(LinkButton(text='⬇️ Скачать видео', url=result)).as_markup()]
                    attachments.append(await upload_video_for_max(event.bot, result))
                    sent = await event.bot.send_message(user_id=job['user_id'], text='🎬 Ваше видео готово: ' + result,
                        attachments=attachments)
                    if sent is None:
                        raise RuntimeError('MAX не подтвердил отправку видео.')
                    await mark_video_completed(job['id'])
                    success += 1
                    await send_video_share(event.bot, job['user_id'], bot_link=bot_link)
                except Exception:
                    failed += 1
                    logger.exception('Ошибка отложенного видео %s для пользователя %s:', job['id'], job['user_id'])
            await _reply(event, f'✅ Обработка видео завершена! Успешно: {success}, Ошибок: {failed}.')

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
        cartoon_labels = [
            *((key + '_click', label) for key, label in CARTOON_THEMES.items()),
            ('avatar_ok_1', 'Первый персонаж — да'), ('avatar_bad_1', 'Первый персонаж — нет'),
            ('avatar_ok_2', 'Второй персонаж — да'), ('avatar_bad_2', 'Второй персонаж — нет'),
            ('song_ok', 'Музыка одобрена'), ('song_bad', 'Изменить текст песни'),
            ('video_success', 'Видео выдано'), ('video_fail', 'Ошибки видео'),
        ]
        all_cartoon_counts = await get_all_counters()
        cartoon_counts = {key: max(0, all_cartoon_counts.get(key, 0) - counters.get(key, 0)) for key, _ in cartoon_labels}
        cartoon_test_text = (
            "\n\n🎬 Мульт-открытки — клиенты:\n"
            + "\n".join(f"- {label}: {counters.get(key, 0)}" for key, label in cartoon_labels)
            + f"\n- Покупок за 590 ₽: {counters.get('buy_cartoon_short', 0)}"
            + f"\n- Покупок за 1290 ₽: {counters.get('buy_cartoon_long', 0)}"
            + "\n\n🛠 Мульт-открытки — тесты администратора:\n"
            + "\n".join(f"- {label}: {cartoon_counts.get(key, 0)}" for key, label in cartoon_labels)
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
            f"- Скрапбукинг: {counters.get('gen_scrapbook', 0)}\n"
            f"- Учителям: {counters.get('gen_teacher', 0)}\n"
            f"- Тренерам: {counters.get('gen_coach', 0)}\n\n"
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
            f"- Песня в подарок (490 ₽): {counters.get('click_audio_gift', 0)}\n"
            f"- Медитации (клики): {counters.get('click_meditation', 0)}\n"
            f"- Видео — вход в меню: {counters.get('click_video_menu', 0)}\n"
            f"- Старая кнопка анимации (клики): {counters.get('click_video_animate_199', 0)}\n"
            f"- Старая кнопка видео (клики): {counters.get('click_video_full_249', 0)}\n"
            f"- Музыкальный апселл за 149 ₽ (клики): {counters.get('click_music_upsell_149', 0)}\n\n"
            "🎵 Интерес к Аудиопесням (сценарии):\n"
            + "\n".join(
                f"- {label}: {counters.get(AUDIO_FUN_COUNTERS[payload], 0)}"
                for payload, (label, _) in AUDIO_FUN_SCENARIOS.items()
            ) + "\n\n"
            "🎁 Интерес к Аудиопесням (Подарочный формат):\n"
            + "\n".join(
                f"- {label}: {counters.get(AUDIO_GIFT_COUNTERS[payload], 0)}"
                for payload, (label, _) in AUDIO_GIFT_SCENARIOS.items()
            ) + "\n\n"
            "🎵 Интерес к Аудиопесням (для видео):\n"
            f"- 👨‍🏫 Для тренера / учителя: {counters.get('audio_scen_teacher_video', 0)}\n"
            f"- 🎄 Новогодняя: {counters.get('audio_scen_ny_video', 0)}\n\n"
            "🎬 Интерес к Мульт-услугам:\n"
            f"- Мульт-открытка (клики): {counters.get('click_cartoon_short', 0)}\n"
            f"- Мульт-поздравление (клики): {counters.get('click_cartoon_long', 0)}\n"
            f"- Первый аватар — подошёл: {counters.get('avatar_ok_first', 0)}\n"
            f"- Первый аватар — не похож: {counters.get('avatar_bad_first', 0)}\n"
            f"- Второй аватар — подошёл: {counters.get('avatar_ok_second', 0)}\n"
            f"- Второй аватар — не подошёл: {counters.get('avatar_bad_second', 0)}\n\n"
            "📸 Интерес к Фотосессиям (клики):\n"
            f"- Стиль: Студия: {counters.get('click_photo_studio', 0)}\n"
            f"- Стиль: Прогулка: {counters.get('click_photo_walk', 0)}\n"
            f"- Стиль: Новогодняя: {counters.get('click_photo_ny', 0)}\n\n"
            f"- Тариф «Попробовать» (590): {counters.get('click_tariff_590', 0)}\n"
            f"- Тариф «Фотосессия» (990): {counters.get('click_tariff_990', 0)}\n"
            f"- Тариф «Премиум» (1490): {counters.get('click_tariff_1490', 0)}\n\n"
            f"💰 Всего покупок: {data['total_purchases']}" + source_text + admin_text + cartoon_test_text + campaign_text
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

    @router.message_created(Command('broadcast_update'))
    async def broadcast_update(event: MessageCreated) -> None:
        if is_bot_message(event) or admin_user_id is None or event.get_ids()[1] != admin_user_id:
            return
        if broadcast_lock.locked():
            await _reply(event, 'Рассылка уже выполняется. Дождитесь завершения.')
            return
        async with broadcast_lock:
            users = await get_active_user_ids()
            await _reply(event, f'🚀 Начинаю рассылку обновления для {len(users)} пользователей...')
            keyboard = navigation_keyboard((('🚀 Открыть меню', 'back_to_main'),))
            success_count = failed_count = 0
            for user_id in users:
                try:
                    user = await get_user(user_id)
                    if user is None or not user['is_active']:
                        continue
                    sent = await event.bot.send_message(user_id=user_id, text=BROADCAST_UPDATE_TEXT, attachments=[keyboard])
                    if sent is None:
                        raise RuntimeError('MAX не подтвердил отправку анонса.')
                    success_count += 1
                except Exception as exc:
                    failed_count += 1
                    logger.exception('Ошибка отправки обновления пользователю %s:', user_id)
                    if max_error_status(exc) == 403:
                        try:
                            await mark_user_inactive(user_id)
                        except Exception:
                            logger.exception('Не удалось отметить пользователя %s недоступным:', user_id)
                await asyncio.sleep(0.5)
            await _reply(event, f'✅ Рассылка завершена! Успешно: {success_count}, Ошибок: {failed_count}')

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
        await save_yclid(user_id, extract_yclid(payload))
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
            await send_welcome_message(event, context, payload=parts[1].strip() if len(parts) > 1 else getattr(event, "payload", None))

    @router.message_callback(F.callback.payload.in_({"open_main_menu", "back_to_main"}))
    async def open_main_menu(event: MessageCallback, context: MemoryContext) -> None:
        if event.message is None:
            await event.ack(notification="Отправьте /start, чтобы открыть меню.")
            return
        await event.ack(notification="Открываю меню…")
        await send_welcome_message(event, context)

    def cartoon_style_keyboard(screen='cat_cards'):
        return navigation_keyboard((*((label, key) for key, label in CARTOON_THEMES.items()), ('⬅️ Назад', 'back_to_main')))

    async def ensure_cartoon_access(event, context):
        user_id = event.get_ids()[1]
        if user_id is None:
            if hasattr(event, 'callback'):
                await event.ack(notification='Отправьте /start, чтобы открыть меню.')
            return False
        if admin_user_id is not None and user_id == admin_user_id:
            return True
        data = await context.get_data()
        order = await get_cartoon_order(data.get('cartoon_order_id', ''))
        if (order and order['status'] == 'paid' and order['user_id'] == user_id
                and order['theme'] == data.get('cartoon_theme') and order['tariff'] == data.get('cartoon_flow')):
            return True
        if hasattr(event, 'callback'):
            await event.ack(notification='Сначала оплатите выбранную мульт-открытку.')
            if event.message is None:
                return False
        await _reply(event, 'Сначала выберите тему и оплатите мульт-открытку.',
                     attachments=[navigation_keyboard((('🎬 Мульт-открытка', 'cat_cartoon'), ('⬅️ Назад', 'back_to_main')))])
        return False

    async def persist_cartoon_state(context):
        data = await context.get_data()
        order_id = data.get('cartoon_order_id')
        if order_id:
            await update_cartoon_order(order_id, fsm_data=json.dumps(data, ensure_ascii=False),
                                       fsm_state=str(await context.get_state()))

    async def activate_cartoon_order(context, order):
        saved = json.loads(order['fsm_data']) if order.get('fsm_data') else None
        await context.clear()
        if saved:
            await context.set_data(saved)
            state = order['fsm_state']
            if state == str(PosterStates.cartoon_generating):
                # Resume the same stage and cached task, rather than issuing another paid request.
                state = (PosterStates.waiting_for_cartoon_music_review if saved.get('cartoon_kling_task_id')
                         else PosterStates.waiting_for_cartoon_song_phrase if saved.get('chosen_song_phrase')
                         else PosterStates.waiting_for_avatar_photo)
            await context.set_state(state)
            return
        await context.update_data(cartoon_order_id=order['order_id'], cartoon_flow=order['tariff'],
            cartoon_amount_kopecks=order['amount_kopecks'], cartoon_session_id=uuid4().hex,
            cartoon_theme=order['theme'], cartoon_mode=order['theme'], avatar_attempt=1,
            avatar_urls={}, avatar_preview_urls={})
        await context.set_state(PosterStates.waiting_for_avatar_photo)
        await persist_cartoon_state(context)

    async def send_cartoon_checkout(event, context, theme, product):
        user_id = event.get_ids()[1]
        data = await context.get_data()
        order = await get_cartoon_order(data.get('cartoon_order_id', ''))
        if (not order or order['user_id'] != user_id or order['theme'] != theme or order['tariff'] != product[0]
                or (order['status'] == 'pending' and order['amount_kopecks'] != product[1])):
            order_id = f"cartoon_{user_id}_{theme}_{time_ns()}"
            await create_cartoon_order(order_id, user_id, theme, product[0])
            order = await get_cartoon_order(order_id)
        await context.clear()
        await context.update_data(cartoon_order_id=order['order_id'], cartoon_flow=product[0],
                                  cartoon_amount_kopecks=product[1], cartoon_theme=theme)
        await context.set_state(PosterStates.waiting_for_cartoon_payment)
        if order['status'] == 'paid':
            await activate_cartoon_order(context, order)
            await _reply(event, CARTOON_PHOTO_PROMPT if await context.get_state() == PosterStates.waiting_for_avatar_photo
                         else 'Оплата подтверждена. Продолжайте по последнему сообщению.')
            return
        if order['status'] != 'pending':
            await _reply(event, 'Этот заказ уже завершён. Выберите новую мульт-открытку.', attachments=[main_keyboard()])
            return
        try:
            if payments is None:
                raise RuntimeError('Не настроена интеграция Prodamus.')
            link = await payments.create_payment_link(user_id, order_id=order['order_id'], amount=product[1] // 100,
                                                     description=order['product_name'])
            text = ('🎬 Мульт-открытка (10-15 сек) стоит 590 ₽. Оплатите, чтобы начать создание.' if product[0] == 'short'
                    else '🎬 Мульт-поздравление (до 30 сек) стоит 1290 ₽. Оплатите, чтобы начать создание.')
            keyboard = InlineKeyboardBuilder().row(LinkButton(text=f"Оплатить {product[1] // 100} ₽", url=link)).row(
                CallbackButton(text='Проверить оплату / продолжить', payload='cartoon_resume:' + order['order_id'])).as_markup()
            await _reply(event, text, attachments=[keyboard])
        except Exception:
            logger.exception('Не удалось выставить счёт мульт-заказа %s:', order['order_id'])
            await _reply(event, 'Не удалось создать платёжную ссылку. Попробуйте ещё раз; фото понадобится только после оплаты.',
                         attachments=[navigation_keyboard((('Повторить оплату', theme), ('⬅️ Назад', 'back_to_main')))])

    @router.message_callback(F.callback.payload.startswith('cartoon_resume:'))
    async def resume_paid_cartoon(event: MessageCallback, context: MemoryContext):
        order_id = event.callback.payload.removeprefix('cartoon_resume:')
        order = await get_cartoon_order(order_id)
        if event.message is None or not order or order['user_id'] != event.get_ids()[1]:
            await event.ack(notification='Заказ недоступен.')
            return
        if order['status'] != 'paid':
            await event.ack(notification='Оплата ещё не подтверждена.' if order['status'] == 'pending' else 'Заказ уже завершён.')
            return
        data = await context.get_data()
        await event.ack(notification='Оплата подтверждена.')
        if data.get('cartoon_order_id') != order_id or await context.get_state() == PosterStates.waiting_for_cartoon_payment:
            await activate_cartoon_order(context, order)
            await _reply(event, CARTOON_PHOTO_PROMPT if await context.get_state() == PosterStates.waiting_for_avatar_photo
                         else 'Данные заказа восстановлены. Продолжайте по последнему сообщению.')
        else:
            await _reply(event, 'Продолжайте создание по последнему сообщению. Данные заказа сохранены.')

    async def resume_cartoon_photo(event, context):
        body = event.message.body
        if not any(item.type == 'image' for item in (getattr(body, 'attachments', None) or [])):
            return False
        data = await context.get_data()
        order = (await get_cartoon_order(data['cartoon_order_id']) if data.get('cartoon_order_id')
                 else await latest_paid_cartoon(event.get_ids()[1]))
        if order and order['status'] == 'paid' and order['user_id'] == event.get_ids()[1]:
            await activate_cartoon_order(context, order)
            if await context.get_state() == PosterStates.waiting_for_avatar_photo:
                await collect_cartoon_photo(event, context)
            else:
                await _reply(event, 'Данные заказа восстановлены. Продолжайте по последнему сообщению.')
            return True
        return False

    def cartoon_product(data):
        return next((value for value in CARTOON_PRODUCTS.values() if value[0] == data.get('cartoon_flow')), None)

    def avatar_keyboard(attempt, themed=False):
        if themed:
            return navigation_keyboard((('✅ Да, отлично!', f'avatar_ok_{attempt}'), ('❌ Нет, не похоже', f'avatar_bad_{attempt}')))
        labels = ('✅ Да, отлично!', '❌ Не похоже, попробуем еще') if attempt == 1 else ('✅ Да, этот подходит!', '❌ Нет, совсем не то')
        return navigation_keyboard(((labels[0], f'avatar_ok_{attempt}'), (labels[1], f'avatar_bad_{attempt}')))

    async def show_cartoon_songs(event, context):
        await context.set_state(PosterStates.waiting_for_cartoon_song)
        buttons = [(label, 'cartoon_song:' + key) for key, (label, _) in AUDIO_FUN_SCENARIOS.items()]
        await _reply(event, 'Теперь выберите песню для вашего мультфильма:',
                     attachments=[navigation_keyboard((*buttons, ('⬅️ Назад', 'back_to_main')))])

    @router.message_callback(F.callback.payload.in_(set(CARTOON_PRODUCTS)))
    async def cartoon_menu(event: MessageCallback, context: MemoryContext):
        user_id = event.get_ids()[1]
        if event.message is None or user_id is None:
            await event.ack(notification='Отправьте /start, чтобы открыть меню.')
            return
        await event.ack(notification='Выберите стиль…')
        flow, price, text, _ = CARTOON_PRODUCTS[event.callback.payload]
        await increment_counter('click_cartoon_short' if flow == 'short' else 'click_cartoon_long', user_id=user_id)
        await context.clear()
        await context.update_data(cartoon_flow=flow, cartoon_amount_kopecks=price, cartoon_session_id=uuid4().hex)
        await context.set_state(PosterStates.waiting_for_cartoon_style)
        await _reply(event, text, attachments=[cartoon_style_keyboard()])

    @router.message_callback(F.callback.payload.startswith('cartoon_style:') | F.callback.payload.in_(set(CARTOON_THEMES)))
    async def cartoon_style(event: MessageCallback, context: MemoryContext):
        user_id = event.get_ids()[1]
        if user_id is None:
            await event.ack(notification='Отправьте /start, чтобы открыть меню.')
            return
        data = await context.get_data()
        target = event.callback.payload.removeprefix('cartoon_style:')
        product = cartoon_product(data)
        if event.message is None or product is None or target not in VIDEO_CARD_SCREENS | set(CARTOON_THEMES) | (set(POSTCARD_PROMPTS) - set(ROLE_CARD_FIELDS)):
            await event.ack(notification='Откройте раздел мульт-открыток заново.')
            return
        if await context.get_state() == PosterStates.cartoon_generating:
            await event.ack(notification='Аватар уже создаётся, пожалуйста, подождите.')
            return
        if user_id != admin_user_id:
            if target not in CARTOON_THEMES:
                await event.ack(notification='Выберите одну из новых тем.')
                await _reply(event, product[2], attachments=[cartoon_style_keyboard()])
                return
            await event.ack(notification='Готовлю оплату…')
            await increment_counter(target + '_click', user_id=user_id)
            await send_cartoon_checkout(event, context, target, product)
            return
        await event.ack(notification='Выбираю стиль…')
        await context.clear()
        await context.update_data(cartoon_flow=product[0], cartoon_amount_kopecks=product[1], cartoon_session_id=uuid4().hex)
        if target in CARTOON_THEMES:
            await increment_counter(target + '_click', user_id=event.get_ids()[1])
            await context.update_data(cartoon_theme=target, cartoon_mode=target, avatar_attempt=1, avatar_urls={}, avatar_preview_urls={})
            await context.set_state(PosterStates.waiting_for_avatar_photo)
            await _reply(event, CARTOON_PHOTO_PROMPT)
        elif target in POSTCARD_PROMPTS:
            await context.update_data(cartoon_mode=target, avatar_attempt=1, avatar_urls={})
            await context.set_state(PosterStates.waiting_for_avatar_photo)
            await _reply(event, photo_request('Отправьте фото для первого мульт-аватара. /cancel — отмена.'))
        else:
            await context.set_state(PosterStates.waiting_for_cartoon_style)
            filenames, text, _ = CATEGORY_MENUS[target]
            if target == 'cat_cards':
                text = product[2]
            await send_menu_examples(event, filenames)
            await _reply(event, text, attachments=[cartoon_style_keyboard(target)])

    async def collect_cartoon_photo(event: MessageCreated, context: MemoryContext):
        if is_bot_message(event) or not await ensure_cartoon_access(event, context):
            return
        user_id = event.get_ids()[1]
        if is_bot_message(event) or user_id is None:
            return
        lock = cartoon_locks.setdefault(user_id, asyncio.Lock())
        if lock.locked():
            await _reply(event, 'Аватар уже создаётся. Пожалуйста, подождите.')
            return
        async with lock:
            data = await context.get_data()
            if (await context.get_state() != PosterStates.waiting_for_avatar_photo or not cartoon_product(data)
                    or data.get('cartoon_mode') not in set(POSTCARD_PROMPTS) | set(CARTOON_THEMES) or data.get('avatar_attempt') not in (1, 2)):
                await _reply(event, 'Выберите мульт-открытку заново.', attachments=[main_keyboard()])
                return
            body = event.message.body
            photos = [item for item in (getattr(body, 'attachments', None) or [])
                      if item.type == 'image' and isinstance(item.payload, PhotoAttachmentPayload) and item.payload.url]
            if len(photos) != 1:
                await _reply(event, photo_request('Нужно одно фото, отправленное как изображение.'))
                return
            photo_url = photos[0].payload.url
            attempt = data['avatar_attempt']
            if attempt == 2 and photo_url == data.get('avatar_first_photo_url'):
                await _reply(event, 'Пожалуйста, отправьте другое фото для второго аватара.')
                return
            session = data['cartoon_session_id']
            await context.set_state(PosterStates.cartoon_generating)
            try:
                urls = dict(data.get('avatar_urls', {}))
                url = urls.get(str(attempt))
                if not url:
                    await _reply(event, '🎨 Рисую вашего персонажа... Это займет около минуты.' if data.get('cartoon_theme') else f'🎨 Создаю мульт-аватар {attempt} из 2... Пожалуйста, подождите.')
                    url = await avatar_generator(photo_url, data['cartoon_mode'])
                    if not isinstance(url, str) or not url.startswith('https://'):
                        raise ValueError('Генератор не вернул HTTPS-ссылку аватара.')
                    if (await context.get_data()).get('cartoon_session_id') != session:
                        return
                    urls[str(attempt)] = url
                    await context.update_data(avatar_urls=urls, **({'avatar_first_photo_url': photo_url} if attempt == 1 else {}))
                    await send_offline_conversion(user_id, goal_id='bot_generation')
                display_url = url
                if data.get('cartoon_theme'):
                    previews = dict(data.get('avatar_preview_urls', {}))
                    display_url = previews.get(str(attempt))
                    if not display_url:
                        display_url = await create_cartoon_avatar_preview(url)
                        if (await context.get_data()).get('cartoon_session_id') != session:
                            return
                        previews[str(attempt)] = display_url
                    await context.update_data(avatar_preview_urls=previews, original_image_url=url, watermarked_image_url=display_url)
                image = await upload_image_for_max(event.bot, display_url)
                if (await context.get_data()).get('cartoon_session_id') != session:
                    return
                sent = await _reply(event, 'Вот ваш персонаж! Вам нравится?' if data.get('cartoon_theme') else 'Ваш мульт-аватар готов! Подходит?',
                    attachments=[image, avatar_keyboard(attempt, themed=bool(data.get('cartoon_theme')))])
                if sent is None:
                    raise RuntimeError('MAX не подтвердил отправку аватара.')
                if (await context.get_data()).get('cartoon_session_id') == session:
                    await context.set_state(PosterStates.waiting_for_avatar_review)
            except Exception:
                logger.exception('Ошибка генерации или отправки мульт-аватара:')
                if (await context.get_data()).get('cartoon_session_id') == session:
                    await context.set_state(PosterStates.waiting_for_avatar_photo)
                    await _reply(event, 'Не удалось создать или отправить аватар. Отправьте фото ещё раз; готовый аватар будет отправлен повторно.')
            finally:
                await persist_cartoon_state(context)

    async def handoff_cartoon(event, context, user_id):
        data = await context.get_data()
        if not data.get('cartoon_handoff_sent'):
            try:
                if admin_user_id is None:
                    raise RuntimeError('Не задан ADMIN_USER_ID для передачи художнику.')
                sent = await event.bot.send_message(user_id=admin_user_id,
                    text=f'🎨 Нужен художник для мульт-открытки. ID клиента: {user_id}. Формат: {data["cartoon_flow"]}. Второй аватар не подошёл.')
                if sent is None:
                    raise RuntimeError('MAX не подтвердил уведомление художника.')
                await context.update_data(cartoon_handoff_sent=True)
            except Exception:
                logger.exception('Не удалось передать мульт-открытку художнику:')
                await _reply(event, 'Не удалось отправить заявку художнику. Повторите передачу или обратитесь в поддержку.',
                             attachments=[navigation_keyboard((('Повторить передачу', 'cartoon_retry_handoff'), ('🆘 Помощь', 'show_help')))])
                return
        await _reply(event, 'Понимаем вас. Передаем заказ нашему художнику для ручной обработки. Он свяжется с вами в течение часа.' if data.get('cartoon_theme') else
                     'Понимаем вас. Передаем заказ нашему художнику, он свяжется с вами в течение часа.')
        if data.get('cartoon_order_id'):
            await finish_cartoon_order(data['cartoon_order_id'], user_id, manual=True)
        if data.get('cartoon_theme'):
            await context.clear()
            await context.set_state(None)

    @router.message_callback(F.callback.payload.in_(set(CARTOON_AVATAR_EVENTS)))
    async def cartoon_avatar_review(event: MessageCallback, context: MemoryContext):
        if not await ensure_cartoon_access(event, context):
            return
        user_id = event.get_ids()[1]
        if event.message is None or user_id is None:
            await event.ack(notification='Откройте раздел мульт-открыток заново.')
            return
        lock = cartoon_locks.setdefault(user_id, asyncio.Lock())
        async with lock:
            data = await context.get_data()
            attempt = int(event.callback.payload[-1])
            url = data.get('avatar_urls', {}).get(str(attempt))
            if not cartoon_product(data) or data.get('avatar_attempt') != attempt or not url or await context.get_state() != PosterStates.waiting_for_avatar_review:
                await event.ack(notification='Эта кнопка уже недоступна. Используйте кнопки под текущим аватаром.')
                return
            await event.ack(notification='Выбор сохранён.')
            await increment_counter(CARTOON_AVATAR_EVENTS[event.callback.payload], user_id=user_id)
            if data.get('cartoon_theme'):
                await increment_counter(event.callback.payload, user_id=user_id)
            if event.callback.payload.startswith('avatar_ok_'):
                await context.update_data(chosen_avatar=url)
                if data.get('cartoon_theme'):
                    await context.update_data(original_image_url=url, chosen_song='audio_fun_ny' if data['cartoon_theme'] == 'theme_ny' else 'audio_fun_universal')
                    await context.set_state(PosterStates.waiting_for_cartoon_song_phrase)
                    await _reply(event, CARTOON_SONG_PHRASE_PROMPT)
                else:
                    await show_cartoon_songs(event, context)
            elif attempt == 1:
                await context.update_data(avatar_attempt=2)
                await context.set_state(PosterStates.waiting_for_avatar_photo)
                await _reply(event, 'Хорошо! Отправьте другое фото (или это же, но более четкое).' if data.get('cartoon_theme') else photo_request('Пришлите другое фото — попробуем создать второй мульт-аватар.'))
            else:
                await context.set_state(PosterStates.cartoon_manual_review)
                await handoff_cartoon(event, context, user_id)
            await persist_cartoon_state(context)

    @router.message_callback(F.callback.payload == 'cartoon_retry_handoff')
    async def cartoon_retry_handoff(event: MessageCallback, context: MemoryContext):
        if not await ensure_cartoon_access(event, context):
            return
        user_id = event.get_ids()[1]
        if event.message is None or user_id is None:
            await event.ack(notification='Заявка недоступна.')
            return
        async with cartoon_locks.setdefault(user_id, asyncio.Lock()):
            data = await context.get_data()
            if not cartoon_product(data) or await context.get_state() != PosterStates.cartoon_manual_review:
                await event.ack(notification='Заявка недоступна.')
                return
            await event.ack(notification='Отправляю заявку…')
            await handoff_cartoon(event, context, user_id)

    @router.message_callback(F.callback.payload.startswith('cartoon_song:'))
    async def cartoon_song(event: MessageCallback, context: MemoryContext):
        if not await ensure_cartoon_access(event, context):
            return
        user_id = event.get_ids()[1]
        if user_id is None:
            await event.ack(notification='Откройте раздел мульт-открыток заново.')
            return
        lock = cartoon_locks.setdefault(user_id, asyncio.Lock())
        if lock.locked():
            await event.ack(notification='Заказ уже обрабатывается. Подождите.')
            return
        async with lock:
            await select_cartoon_song(event, context)

    async def select_cartoon_song(event, context):
        data = await context.get_data()
        product = cartoon_product(data)
        song = event.callback.payload.removeprefix('cartoon_song:')
        if (event.message is None or not product or song not in AUDIO_FUN_SCENARIOS or not data.get('chosen_avatar')
                or await context.get_state() not in (PosterStates.waiting_for_cartoon_song, PosterStates.waiting_for_cartoon_payment)):
            await event.ack(notification='Сначала выберите и одобрите аватар.')
            return
        await event.ack(notification='Песня выбрана.')
        if data.get('chosen_song') != song:
            await context.update_data(chosen_song_url=None, chosen_song_phrase=None)
        await context.update_data(chosen_song=song, video_song_phrase=None)
        await context.set_state(PosterStates.waiting_for_cartoon_song_phrase)
        await _reply(event, AUDIO_FUN_SCENARIOS[song][1])

    async def render_selected_cartoon(event, context):
        data = await context.get_data()
        product = cartoon_product(data)
        song = data['chosen_song']
        song_url = data.get('chosen_song_url') or CARTOON_TEST_SONG_URL + '?' + urlencode({'scenario': song})
        if admin_user_id is not None and event.get_ids()[1] == admin_user_id:
            await context.set_state(PosterStates.cartoon_generating)
            await _reply(event, VIDEO_GENERATION_MESSAGE)
            try:
                data.update(chosen_song=song, chosen_song_url=song_url)
                result = (await render_video_with_song(data, data['chosen_avatar'], song) if default_cartoon_video else
                          await cartoon_video_generator(data['chosen_avatar'], song_url))
                if (await context.get_data()).get('cartoon_session_id') != data.get('cartoon_session_id'):
                    return
                if is_video_placeholder(result):
                    await save_pending_video(event.get_ids()[1], data['chosen_avatar'], data.get('video_song_url') or song_url,
                                             mode=data.get('cartoon_mode') or '')
                    await _reply(event, PENDING_VIDEO_MESSAGE, attachments=[main_keyboard()])
                    await context.set_state(PosterStates.waiting_for_mode)
                    return
                if not isinstance(result, str) or not result.startswith('https://'):
                    raise ValueError('PiAPI не вернул HTTPS-ссылку на видео.')
                video = await upload_video_for_max(event.bot, result)
                sent = await _reply(event, '🎬 Ваше видео с песней готово: ' + result,
                             attachments=[InlineKeyboardBuilder().row(LinkButton(text='⬇️ Скачать видео', url=result)).row(
                                 CallbackButton(text='⬅️ Главное меню', payload='back_to_main')).as_markup(), video])
                if sent is None:
                    raise RuntimeError('MAX не подтвердил отправку мульт-видео.')
                await context.set_state(PosterStates.waiting_for_mode)
                await send_video_share(event.bot, event.get_ids()[1], bot_link=bot_link)
            except Exception:
                logger.exception('Ошибка генерации мульт-видео PiAPI:')
                if (await context.get_data()).get('cartoon_session_id') != data.get('cartoon_session_id'):
                    return
                await context.set_state(PosterStates.waiting_for_cartoon_song)
                await _reply(event, 'Не удалось создать видео. Подробности записаны в журнал. Выберите песню ещё раз.')
            finally:
                if (await context.get_data()).get('cartoon_session_id') == data.get('cartoon_session_id'):
                    await context.update_data(**video_cache(data))
            return
        await context.set_state(PosterStates.waiting_for_cartoon_payment)
        price = product[1] // 100
        text = f'Отлично! Стоимость — {price} ₽. Это займет около {product[3]} минут.'
        await _reply(event, text, attachments=[InlineKeyboardBuilder().row(
            LinkButton(text=f'Оплатить {price} ₽', url=VIDEO_PAYMENT_PLACEHOLDER_URL)).row(
            CallbackButton(text='⬅️ Назад', payload='back_to_main')).as_markup()])

    def cartoon_music_keyboard(token):
        return navigation_keyboard((('✅ Да, создать видео!', 'cartoon_music_ok:' + token),
                                    ('🔄 Нет, изменить текст песни', 'cartoon_music_bad:' + token)))

    async def prepare_cartoon_music_preview(event, context):
        data = dict(await context.get_data())
        session = data.get('cartoon_session_id')
        await context.set_state(PosterStates.cartoon_generating)
        try:
            await _reply(event, '🎵 Создаю песню... Подождите немного.')
            audio_url = await prepare_video_song(data, data['chosen_song'])
            if (await context.get_data()).get('cartoon_session_id') != session:
                return
            if data.get('video_song_preview_source') != audio_url or not data.get('video_song_preview_url'):
                preview = await create_audio_preview(audio_url)
                data.update(video_song_preview_source=audio_url, video_song_preview_url=preview)
            if (await context.get_data()).get('cartoon_session_id') != session:
                return
            await context.update_data(**video_cache(data))
            audio = await upload_audio_for_max(event.bot, data['video_song_preview_url'])
            if (await context.get_data()).get('cartoon_session_id') != session:
                return
            token = uuid4().hex
            sent = await _reply(event, '🎧 Вот фрагмент вашей песни. Всё нравится?', attachments=[audio, cartoon_music_keyboard(token)])
            if sent is None:
                raise RuntimeError('MAX не подтвердил отправку превью песни.')
            if (await context.get_data()).get('cartoon_session_id') == session:
                await context.update_data(cartoon_preview_token=token)
                await context.set_state(PosterStates.waiting_for_cartoon_music_review)
        except Exception:
            logger.exception('Ошибка подготовки песни для мульт-открытки:')
            if (await context.get_data()).get('cartoon_session_id') == session:
                await context.set_state(PosterStates.waiting_for_cartoon_song_phrase)
                await _reply(event, 'Не удалось подготовить аудио-превью. Отправьте фразу ещё раз или /cancel.')
        finally:
            if (await context.get_data()).get('cartoon_session_id') == session:
                await context.update_data(**video_cache(data))
                await persist_cartoon_state(context)

    @router.message_callback(F.callback.payload.startswith('cartoon_music_ok:') | F.callback.payload.startswith('cartoon_music_bad:'))
    async def cartoon_music_review(event: MessageCallback, context: MemoryContext):
        if not await ensure_cartoon_access(event, context):
            return
        user_id = event.get_ids()[1]
        lock = cartoon_locks.setdefault(user_id, asyncio.Lock())
        if lock.locked():
            await event.ack(notification='Мульт-открытка уже создаётся. Подождите.')
            return
        async with lock:
            data = dict(await context.get_data())
            action, token = event.callback.payload.split(':', 1)
            if (event.message is None or not data.get('cartoon_theme') or not token or token != data.get('cartoon_preview_token')
                    or await context.get_state() != PosterStates.waiting_for_cartoon_music_review):
                await event.ack(notification='Используйте кнопки под текущим фрагментом песни.')
                return
            await event.ack(notification='Выбор сохранён.')
            if action == 'cartoon_music_bad':
                await increment_counter('song_bad', user_id=user_id)
                await context.update_data(cartoon_preview_token=None, chosen_song_url=None)
                await context.set_state(PosterStates.waiting_for_cartoon_song_phrase)
                await _reply(event, CARTOON_SONG_PHRASE_PROMPT)
                await persist_cartoon_state(context)
                return
            await increment_counter('song_ok', user_id=user_id)
            session = data['cartoon_session_id']
            await context.set_state(PosterStates.cartoon_generating)
            try:
                await _reply(event, CARTOON_VIDEO_WAIT)
                original = data['original_image_url']
                full_song = data['video_song_url']
                prompt = CARTOON_VIDEO_PROMPTS[data['cartoon_theme']]
                source = [original, prompt]
                logger.info('Промпт для Kling (%s): %s', data['cartoon_theme'], prompt)
                if data.get('cartoon_kling_source') != source:
                    data.update(cartoon_kling_source=source, cartoon_kling_task_id=None, cartoon_kling_video_url=None)
                if not data.get('cartoon_kling_task_id'):
                    task_id = await create_video_task(original, prompt)
                    if (await context.get_data()).get('cartoon_session_id') != session:
                        return
                    data['cartoon_kling_task_id'] = task_id
                    await context.update_data(**{key:value for key,value in data.items() if key.startswith('cartoon_kling_')})
                    await persist_cartoon_state(context)
                if not data.get('cartoon_kling_video_url'):
                    data['cartoon_kling_video_url'] = await poll_video_task(data['cartoon_kling_task_id'], timeout=1200)
                if (await context.get_data()).get('cartoon_session_id') != session:
                    return
                key = [data['cartoon_kling_video_url'], full_song, VIDEO_MERGE_POLICY]
                if data.get('video_merged_source') != key or not data.get('video_merged_url'):
                    data['video_merged_url'] = await create_musical_video(original, full_song, animated_video_url=data['cartoon_kling_video_url'])
                    data['video_merged_source'] = key
                if (await context.get_data()).get('cartoon_session_id') != session:
                    return
                await context.update_data(**video_cache(data))
                video = await upload_video_for_max(event.bot, data['video_merged_url'])
                if (await context.get_data()).get('cartoon_session_id') != session:
                    return
                sent = await _reply(event, '🎬 Ваша мульт-открытка готова!', attachments=[video,
                    InlineKeyboardBuilder().row(LinkButton(text='⬇️ Скачать MP4', url=data['video_merged_url'])).row(
                        CallbackButton(text='⬅️ Главное меню', payload='back_to_main')).as_markup()])
                if sent is None:
                    raise RuntimeError('MAX не подтвердил отправку мульт-открытки.')
                if (await context.get_data()).get('cartoon_session_id') == session:
                    if data.get('cartoon_order_id'):
                        await finish_cartoon_order(data['cartoon_order_id'], user_id)
                    await increment_counter('video_success', user_id=user_id)
                    await context.set_state(PosterStates.waiting_for_mode)
                    await send_video_share(event.bot, user_id, bot_link=bot_link)
            except Exception:
                logger.exception('Ошибка генерации тематической мульт-открытки:')
                if (await context.get_data()).get('cartoon_session_id') == session:
                    await increment_counter('video_fail', user_id=user_id)
                    await context.set_state(PosterStates.waiting_for_cartoon_music_review)
                    await _reply(event, 'Не удалось создать или отправить видео. Данные сохранены; повторите подтверждение или /cancel.',
                                 attachments=[cartoon_music_keyboard(token)])
            finally:
                if (await context.get_data()).get('cartoon_session_id') == session:
                    await context.update_data(**video_cache(data), **{key:value for key,value in data.items() if key.startswith('cartoon_kling_')})
                    await persist_cartoon_state(context)

    async def collect_card_role_info(event: MessageCreated, context: MemoryContext):
        if is_bot_message(event):
            return
        data = await context.get_data()
        mode = data.get('mode')
        if mode not in ROLE_CARD_FIELDS:
            await context.clear()
            await context.set_state(PosterStates.waiting_for_mode)
            await _reply(event, 'Выберите шаблон открытки заново.', attachments=[main_keyboard()])
            return
        body = event.message.body
        text = (body.text or '').strip() if body else ''
        if not text or len(text) > MAX_THEME_INFO_LENGTH:
            await _reply(event, ROLE_CARD_QUESTIONS[mode] + f' До {MAX_THEME_INFO_LENGTH} символов. /cancel — отмена.')
            return
        await context.update_data(**{ROLE_CARD_FIELDS[mode]: text})
        await context.set_state(PosterStates.waiting_for_photo)
        await _reply(event, photo_request('Теперь загрузите фото для открытки. Затем напишите поздравление. /cancel — отмена.'))

    async def generate_role_video_preview(event, context):
        user_id = event.get_ids()[1]
        if user_id is None:
            return
        lock = generation_locks.setdefault(user_id, asyncio.Lock())
        if lock.locked():
            await _reply(event, 'Превью уже создаётся. Пожалуйста, подождите.')
            return
        async with lock:
            data = await context.get_data()
            mode = data['mode']
            session = data.get('card_role_session')
            info = data[ROLE_CARD_FIELDS[mode]]
            key = [mode, data['photo']['url'], info, data['card_text']]
            await context.set_state(PosterStates.generating)
            try:
                if data.get('video_preview_key') != key or not data.get('video_preview_url'):
                    await _reply(event, '🎨 Создаю превью открытки для видео...')
                    fixed = data.get('fixed_card_text') if data.get('fixed_card_text_source') == data['card_text'] else None
                    fixed = fixed or await fix_text_with_ai(data['card_text'])
                    if (await context.get_data()).get('card_role_session') != session:
                        return
                    urls = await postcard_generator(data['photo']['url'], mode, fixed, theme_info=info, preview=True)
                    if len(urls) != 1 or not isinstance(urls[0], str) or not urls[0].startswith('https://'):
                        raise ValueError('Генератор не вернул HTTPS-ссылку превью.')
                    if (await context.get_data()).get('card_role_session') != session:
                        return
                    await context.update_data(video_preview_key=key, video_preview_url=urls[0], video_preview_counted=False,
                                              fixed_card_text=fixed, fixed_card_text_source=data['card_text'])
                    data = await context.get_data()
                if not data.get('video_preview_counted'):
                    await increment_generations(user_id, event_name=GENERATION_EVENTS[mode])
                    await context.update_data(video_preview_counted=True)
                    await send_offline_conversion(user_id, goal_id='bot_generation')
                image = await upload_image_for_max(event.bot, data['video_preview_url'])
                if (await context.get_data()).get('card_role_session') != session:
                    return
                sent = await _reply(event, 'Превью открытки для видео (PREVIEW):', attachments=[image])
                if sent is None:
                    raise RuntimeError('MAX не подтвердил отправку превью.')
                await show_video_songs(event, context)
            except Exception:
                logger.exception('Ошибка создания или отправки предметного превью для видео:')
                if (await context.get_data()).get('card_role_session') == session:
                    await context.set_state(PosterStates.waiting_for_card_text)
                    await _reply(event, 'Не удалось создать или отправить превью. Отправьте поздравление ещё раз или /cancel; готовое превью будет использовано повторно.')

    def video_style_keyboard(screen):
        buttons = []
        for label, payload in CATEGORY_MENUS[screen][2]:
            target = ('back_to_main' if payload == 'back_to_main' else
                      'video_style:cat_cards' if payload == 'back_to_cards' else 'video_style:' + payload)
            buttons.append((label, target))
        return navigation_keyboard(buttons)

    async def show_video_songs(event, context):
        await context.update_data(video_teacher_session_id=None, video_audio_delivery_session=None)
        await context.set_state(PosterStates.waiting_for_video_song)
        data = await context.get_data()
        scenarios = VIDEO_SHORT_SCENARIOS if data.get('video_product') in {'animate', 'upsell'} else VIDEO_SONG_SCENARIOS
        buttons = [(label, key if key == 'audio_video_teacher' else 'video_song:' + key) for key, (label, _) in scenarios.items()]
        await _reply(event, '🎵 Выберите сценарий песни для видео-поздравления:',
                     attachments=[navigation_keyboard((*buttons, ('⬅️ Назад', 'cat_video')))])

    @router.message_callback(F.callback.payload == 'cat_video')
    async def video_menu(event: MessageCallback, context: MemoryContext):
        if event.message is None:
            await event.ack(notification='Отправьте /start, чтобы открыть меню.')
            return
        await event.ack(notification='Выберите стиль…')
        user_id = event.get_ids()[1]
        if user_id is not None:
            await increment_counter('click_video_menu', user_id=user_id)
        await context.clear()
        await context.update_data(video_flow=True, video_amount_kopecks=29900, video_product='direct')
        await context.set_state(PosterStates.waiting_for_mode)
        await _reply(event, '🎵 Создаем музыкальную открытку! Выберите стиль открытки:',
                     attachments=[video_style_keyboard('cat_cards')])

    @router.message_callback(F.callback.payload.startswith('video_style:'))
    async def video_style(event: MessageCallback, context: MemoryContext):
        if event.message is None:
            await event.ack(notification='Отправьте /start, чтобы открыть меню.')
            return
        data = await context.get_data()
        target = event.callback.payload.removeprefix('video_style:')
        if not data.get('video_flow') or target not in VIDEO_CARD_SCREENS | set(POSTCARD_PROMPTS):
            await event.ack(notification='Выберите видео заново.')
            await _reply(event, 'Откройте раздел видео заново.', attachments=[main_keyboard()])
            return
        await event.ack(notification='Выбираю стиль…')
        if target in POSTCARD_PROMPTS:
            await start_mode_photo(event, context, target, video=True)
        else:
            await context.clear()
            await context.update_data(video_flow=True, video_amount_kopecks=29900, video_product='direct')
            await context.set_state(PosterStates.waiting_for_mode)
            filenames, text, _ = CATEGORY_MENUS[target]
            if target == 'cat_cards':
                text = '🎵 Создаем музыкальную открытку! Выберите стиль открытки:'
            await send_menu_examples(event, filenames)
            await _reply(event, text, attachments=[video_style_keyboard(target)])

    @router.message_callback(F.callback.payload.in_({'upsell_music', 'upsell_animate', 'upsell_video'}))
    async def video_upsell(event: MessageCallback, context: MemoryContext):
        if event.message is None:
            await event.ack(notification='Отправьте /start, чтобы открыть меню.')
            return
        await event.ack(notification='Открываю…')
        user_id = event.get_ids()[1]
        animate = event.callback.payload == 'upsell_animate'
        if user_id is not None:
            await increment_counter('click_music_upsell_149', user_id=user_id)
            if event.callback.payload != 'upsell_music':
                await increment_counter('click_video_animate_199' if animate else 'click_video_full_249', user_id=user_id)
        data = await context.get_data()
        image_url = data.get('last_image_url')
        if data.get('mode') not in POSTCARD_PROMPTS or not isinstance(image_url, str) or not image_url.startswith('https://'):
            await _reply(event, 'Сначала создайте открытку, затем выберите её анимацию или видео.', attachments=[main_keyboard()])
            return
        await context.update_data(video_flow=True, video_source_image=image_url,
                                  video_amount_kopecks=14900, video_product='upsell')
        await show_video_songs(event, context)

    @router.message_callback(F.callback.payload.startswith('video_song:') | (F.callback.payload == 'audio_video_teacher'))
    async def video_song(event: MessageCallback, context: MemoryContext):
        if event.message is None:
            await event.ack(notification='Отправьте /start, чтобы открыть меню.')
            return
        data = await context.get_data()
        scenario = event.callback.payload.removeprefix('video_song:')
        scenarios = VIDEO_SHORT_SCENARIOS if data.get('video_product') in {'animate', 'upsell'} else VIDEO_SONG_SCENARIOS
        expected_price = {'direct': 29900, 'upsell': 14900, 'animate': 14900}.get(data.get('video_product'))
        if (not data.get('video_flow') or scenario not in scenarios or expected_price is None
                or data.get('video_amount_kopecks') != expected_price
                or await context.get_state() not in (PosterStates.waiting_for_video_song, PosterStates.waiting_for_video_payment)):
            await event.ack(notification='Выберите видео и заполните данные заново.')
            return
        user_id = event.get_ids()[1]
        if data.get('video_product') == 'direct':
            await event.ack(notification='Жду фразу для припева…')
            if scenario == 'audio_video_teacher' and user_id is not None:
                await increment_counter('audio_scen_teacher_video', user_id=user_id)
            elif scenario in {'audio_fun_ny', 'audio_gift_ny'} and user_id is not None:
                await increment_counter('audio_scen_ny_video', user_id=user_id)
            await context.update_data(video_song_scenario=scenario, song_custom_text=None, video_song_phrase=None,
                                      video_teacher_details=None, video_song_lyrics=None, video_teacher_session_id=None,
                                      video_teacher_lyrics_source=None, video_song_tags=None, video_song_title=None)
            await context.set_state(PosterStates.waiting_for_video_song_phrase)
            await _reply(event, VIDEO_SONG_CUSTOM_TEXT_PROMPT)
            return
        if scenario == 'audio_video_teacher':
            await event.ack(notification='Жду данные учителя или тренера…')
            if user_id is not None:
                await increment_counter('audio_scen_teacher_video', user_id=user_id)
            await context.update_data(video_song_scenario=scenario, video_teacher_session_id=uuid4().hex,
                                      video_teacher_details=None, video_song_lyrics=None, video_teacher_lyrics_source=None,
                                      video_song_tags=None, video_song_title=None, video_song_phrase=None)
            await context.set_state(PosterStates.waiting_for_video_teacher_details)
            await _reply(event, TEACHER_DETAILS_PROMPT)
            return
        await event.ack(notification='Жду фразу для припева…')
        if scenario in {'audio_fun_ny', 'audio_gift_ny'} and user_id is not None:
            await increment_counter('audio_scen_ny_video', user_id=user_id)
        await context.update_data(video_song_scenario=scenario, video_song_lyrics=None, video_song_tags=None,
                                  video_song_title=None, video_teacher_details=None, video_teacher_lyrics_source=None,
                                  video_teacher_session_id=None, video_song_phrase=None)
        await context.set_state(PosterStates.waiting_for_video_song_phrase)
        await _reply(event, scenarios[scenario][1])

    async def collect_video_song_phrase(event: MessageCreated, context: MemoryContext):
        if is_bot_message(event):
            return
        state = await context.get_state()
        cartoon = state == PosterStates.waiting_for_cartoon_song_phrase
        if cartoon and not await ensure_cartoon_access(event, context):
            return
        if state not in (PosterStates.waiting_for_cartoon_song_phrase, PosterStates.waiting_for_video_song_phrase):
            return
        user_id = event.get_ids()[1]
        if user_id is None:
            return
        lock = (cartoon_locks if cartoon else video_text_locks).setdefault(user_id, asyncio.Lock())
        if lock.locked():
            await _reply(event, 'Песня уже создаётся. Пожалуйста, подождите.')
            return
        async with lock:
            data = await context.get_data()
            scenario = data.get('chosen_song' if cartoon else 'video_song_scenario')
            scenarios = AUDIO_FUN_SCENARIOS if cartoon else VIDEO_SONG_SCENARIOS
            if scenario not in scenarios:
                await _reply(event, 'Выберите сценарий песни заново.', attachments=[main_keyboard()])
                return
            body = event.message.body
            phrase = (body.text or '').strip() if body else ''
            if not phrase or len(phrase) > 500:
                await _reply(event, (CARTOON_SONG_PHRASE_PROMPT if cartoon and data.get('cartoon_theme') else
                                     VIDEO_SONG_CUSTOM_TEXT_PROMPT if not cartoon and data.get('video_product') == 'direct'
                                     else scenarios[scenario][1]) + ' До 500 символов. /cancel — отмена.')
                return
            changes = {'video_song_phrase': phrase}
            if not cartoon and data.get('video_product') == 'direct':
                changes['song_custom_text'] = phrase
            if cartoon:
                if data.get('chosen_song_phrase') != phrase:
                    changes['chosen_song_url'] = None
                changes['chosen_song_phrase'] = phrase
            await context.update_data(**changes)
            if cartoon:
                if data.get('cartoon_theme'):
                    await prepare_cartoon_music_preview(event, context)
                else:
                    await render_selected_cartoon(event, context)
            else:
                await finish_video_selection(event, context)

    async def collect_video_teacher_details(event: MessageCreated, context: MemoryContext):
        user_id = event.get_ids()[1]
        if is_bot_message(event) or user_id is None:
            return
        lock = video_text_locks.setdefault(user_id, asyncio.Lock())
        if lock.locked():
            await _reply(event, 'Текст песни уже создаётся. Пожалуйста, подождите.')
            return
        async with lock:
            data = await context.get_data()
            session = data.get('video_teacher_session_id')
            if not session or data.get('video_song_scenario') != 'audio_video_teacher' or await context.get_state() != PosterStates.waiting_for_video_teacher_details:
                await _reply(event, 'Выберите сценарий для видео заново.', attachments=[main_keyboard()])
                return
            body = event.message.body
            details = (body.text or '').strip() if body else ''
            if not details:
                await _reply(event, TEACHER_DETAILS_PROMPT)
                return
            try:
                prompt, tags, title = build_song_request(details, TEACHER_SCENARIO_TITLE, 'fun')
                await context.set_state(PosterStates.generating_video_teacher)
                await _reply(event, '🎵 Создаю песню-благодарность на «Вы» для видео...')
                lyrics = data.get('video_song_lyrics') if data.get('video_teacher_lyrics_source') == details else None
                lyrics = lyrics or await lyric_writer(prompt, tags, title)
                validate_song_arguments(lyrics, tags, title)
                if (await context.get_data()).get('video_teacher_session_id') != session:
                    return
                await context.update_data(video_teacher_details=details, video_song_phrase=details, video_teacher_lyrics_source=details,
                                          video_song_lyrics=lyrics, video_song_tags=tags, video_song_title=title)
                await finish_video_selection(event, context)
            except Exception:
                logger.exception('Ошибка генерации текста благодарности для видео:')
                if (await context.get_data()).get('video_teacher_session_id') == session:
                    await context.set_state(PosterStates.waiting_for_video_teacher_details)
                    await _reply(event, 'Не удалось создать текст песни. Отправьте данные ещё раз или /cancel для отмены.')

    async def finish_video_selection(event, context):
        user_id = event.get_ids()[1]
        if user_id is None:
            return
        lock = video_locks.setdefault(user_id, asyncio.Lock())
        if lock.locked():
            await _reply(event, 'Видео уже создаётся. Пожалуйста, подождите.')
            return
        async with lock:
            data = await context.get_data()
            if data.get('video_product') == 'direct' or data.get('video_song_scenario') == 'audio_video_teacher':
                if not await deliver_video_song_audio(event, context):
                    return
            await run_selected_video(event, context)

    async def deliver_video_song_audio(event, context):
        token = uuid4().hex
        await context.update_data(video_audio_delivery_session=token)
        await context.set_state(PosterStates.generating)
        data = dict(await context.get_data())
        scenario = data['video_song_scenario']
        try:
            audio_url = await prepare_video_song(data, scenario)
            if (await context.get_data()).get('video_audio_delivery_session') != token:
                return False
            data['audio_url'] = audio_url
            # Сохранить ссылку до ответа MAX и перехода к склейке/оплате.
            await context.update_data(**video_cache(data))
            listen_url = audio_url
            direct = data.get('video_product') == 'direct'
            if direct:
                if data.get('video_song_preview_source') != audio_url or not data.get('video_song_preview_url'):
                    preview_url = await create_audio_preview(audio_url)
                    if (await context.get_data()).get('video_audio_delivery_session') != token:
                        return False
                    data.update(video_song_preview_source=audio_url, video_song_preview_url=preview_url)
                    await context.update_data(**video_cache(data))
                listen_url = data['video_song_preview_url']
            if data.get('video_song_delivered_url') != listen_url:
                text = ('🎧 Вот короткий фрагмент вашей песни. Если всё нравится, переходите к созданию видео!' if direct else
                        '🎧 Ваша песня готова! Прослушайте фрагмент и переходите к созданию видео.')
                sent = await _reply(event, text,
                    attachments=[InlineKeyboardBuilder().row(LinkButton(
                        text='🎵 Прослушать фрагмент (MP3)' if direct else '🎵 Прослушать песню (MP3)', url=listen_url)).as_markup()])
                if sent is None:
                    raise RuntimeError('MAX не подтвердил отправку аудиоссылки.')
                if (await context.get_data()).get('video_audio_delivery_session') != token:
                    return False
                await context.update_data(video_song_delivered_url=listen_url)
            return True
        except Exception:
            logger.exception('Ошибка получения или отправки аудио для видео:')
            if (await context.get_data()).get('video_audio_delivery_session') == token:
                await context.set_state(PosterStates.waiting_for_video_song)
                await _reply(event, 'Не удалось получить или отправить аудио. Данные сохранены. Повторите отправку песни или /cancel.',
                    attachments=[navigation_keyboard((('Повторить отправку песни', 'retry_video_song_audio'),))])
            return False
        finally:
            if (await context.get_data()).get('video_audio_delivery_session') == token:
                await context.update_data(**video_cache(data))

    @router.message_callback(F.callback.payload == 'retry_video_song_audio')
    async def retry_video_song_audio(event: MessageCallback, context: MemoryContext):
        data = await context.get_data()
        if (event.message is None or not data.get('video_flow') or data.get('video_song_scenario') not in VIDEO_SONG_SCENARIOS
                or await context.get_state() != PosterStates.waiting_for_video_song):
            await event.ack(notification='Откройте раздел видео заново.')
            return
        await event.ack(notification='Повторяю отправку аудио…')
        await finish_video_selection(event, context)

    async def run_selected_video(event, context):
        data = await context.get_data()
        scenario = data['video_song_scenario']
        scenarios = VIDEO_SHORT_SCENARIOS if data.get('video_product') in {'animate', 'upsell'} else VIDEO_SONG_SCENARIOS
        await context.set_state(PosterStates.waiting_for_video_payment)
        if admin_user_id is not None and event.get_ids()[1] == admin_user_id:
            await context.set_state(PosterStates.generating)
            generation_id = uuid4().hex
            await context.update_data(video_generation_id=generation_id)
            video_data = dict(await context.get_data())
            try:
                if default_card_video:
                    result = await generate_card_video(video_data, progress=lambda: _reply(event, MUSICAL_VIDEO_WAIT_MESSAGE))
                else:
                    await _reply(event, MUSICAL_VIDEO_WAIT_MESSAGE)
                    result = await video_generator(video_data)
                if (await context.get_data()).get('video_generation_id') != generation_id:
                    return
                if is_video_placeholder(result):
                    image_url = (video_data.get('video_source_image') or video_data.get('chosen_avatar')
                                 or (video_data.get('photo') or {}).get('url'))
                    song_url = video_data.get('chosen_song_url') or video_data.get('video_song_url') or ''
                    await save_pending_video(event.get_ids()[1], image_url, song_url, mode=video_data.get('mode') or '', render_kind='musical')
                    await _reply(event, PENDING_VIDEO_MESSAGE, attachments=[main_keyboard()])
                    await context.set_state(PosterStates.waiting_for_mode)
                    return
                if not isinstance(result, str) or not result.startswith('https://'):
                    raise ValueError('Генератор видео не вернул HTTPS-ссылку.')
                video_attachment = await upload_video_for_max(event.bot, result)
                if (await context.get_data()).get('video_generation_id') != generation_id:
                    return
                sent = await _reply(event, '🎵 Ваша музыкальная открытка готова: ' + result, attachments=[
                    InlineKeyboardBuilder().row(LinkButton(text='⬇️ Скачать видео', url=result)).row(
                        CallbackButton(text='⬅️ Главное меню', payload='back_to_main')).as_markup(), video_attachment])
                if sent is None:
                    raise RuntimeError('MAX не подтвердил отправку музыкальной открытки.')
                await context.update_data(video_complete=True, video_result_url=result)
                await context.set_state(PosterStates.waiting_for_mode)
                await send_video_share(event.bot, event.get_ids()[1], bot_link=bot_link)
            except Exception:
                logger.exception('Ошибка создания музыкальной открытки:')
                if (await context.get_data()).get('video_generation_id') != generation_id:
                    return
                await context.set_state(PosterStates.waiting_for_video_song)
                await _reply(event, 'Не удалось создать видео. Подробности записаны в журнал. Выберите песню ещё раз.')
            finally:
                if video_data.get('video_source_image') and (await context.get_data()).get('video_generation_id') == generation_id:
                    await context.update_data(video_source_image=video_data['video_source_image'])
                if (await context.get_data()).get('video_generation_id') == generation_id:
                    await context.update_data(**video_cache(video_data))
            return
        price = data['video_amount_kopecks'] // 100
        text = (f'Стоимость музыкальной открытки — {price} ₽. '
                'Вы получите готовый MP4-файл с вашей открыткой и персональной песней, который можно отправить в мессенджере.')
        if payments is None:
            await _reply(event, text + '\n\nОплата временно недоступна. Попробуйте позже.',
                attachments=[navigation_keyboard(((f'Повторить оплату {price} ₽', 'retry_music_checkout'), ('⬅️ Назад', 'back_to_main')))])
            return
        token = uuid4().hex
        await context.update_data(music_checkout_session=token)
        await context.set_state(PosterStates.generating)
        checkout = dict(await context.get_data())
        try:
            image_url = await prepare_card_video_image(checkout)
            if (await context.get_data()).get('music_checkout_session') != token:
                return
            audio_url = await prepare_video_song(checkout, checkout['video_song_scenario'])
            tariff = 'direct' if checkout['video_product'] == 'direct' else 'upsell'
            key = [image_url, audio_url, tariff]
            order_id = checkout.get('music_checkout_id') if checkout.get('music_checkout_source') == key else None
            order = await get_musical_order(order_id) if order_id else None
            if (await context.get_data()).get('music_checkout_session') != token:
                return
            if order and order['status'] == 'paid':
                keyboard = InlineKeyboardBuilder().row(CallbackButton(text='⬅️ Главное меню', payload='back_to_main'))
                if order['result_url']:
                    keyboard.row(LinkButton(text='⬇️ Скачать MP4', url=order['result_url']))
                await _reply(event, 'Этот заказ уже оплачен. Музыкальная открытка будет отправлена автоматически.', attachments=[keyboard.as_markup()])
                return
            if not order:
                order_id = f'music_{event.get_ids()[1]}_{uuid4().hex}'
                await create_musical_order(order_id, event.get_ids()[1], image_url, audio_url, tariff)
                order = await get_musical_order(order_id)
            if (await context.get_data()).get('music_checkout_session') != token:
                return
            await context.update_data(music_checkout_id=order_id, music_checkout_source=key)
            link = await payments.create_musical_payment_link(order_id=order_id, amount=price, description=order['product_name'])
            if (await context.get_data()).get('music_checkout_session') != token:
                return
            sent = await _reply(event, text, attachments=[InlineKeyboardBuilder().row(
                LinkButton(text=f'Оплатить {price} ₽', url=link)).row(
                CallbackButton(text='⬅️ Назад', payload='back_to_main')).as_markup()])
            if sent is None:
                raise RuntimeError('MAX не подтвердил отправку платёжной ссылки.')
        except Exception:
            logger.exception('Ошибка подготовки оплаты музыкальной открытки:')
            if (await context.get_data()).get('music_checkout_session') == token:
                await _reply(event, 'Не удалось создать ссылку на оплату. Данные сохранены, попробуйте ещё раз.',
                    attachments=[navigation_keyboard((('Повторить оплату', 'retry_music_checkout'),))])
        finally:
            if (await context.get_data()).get('music_checkout_session') == token:
                if checkout.get('video_source_image'):
                    await context.update_data(video_source_image=checkout['video_source_image'])
                await context.update_data(**video_cache(checkout))
                await context.set_state(PosterStates.waiting_for_video_payment)

    @router.message_callback(F.callback.payload == 'retry_music_checkout')
    async def retry_music_checkout(event: MessageCallback, context: MemoryContext):
        data = await context.get_data()
        if (event.message is None or not data.get('video_flow') or not data.get('video_song_scenario')
                or await context.get_state() != PosterStates.waiting_for_video_payment):
            await event.ack(notification='Откройте музыкальные открытки заново.')
            return
        await event.ack(notification='Готовлю оплату…')
        lock = video_locks.setdefault(event.get_ids()[1], asyncio.Lock())
        if lock.locked():
            return
        async with lock:
            await run_selected_video(event, context)

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
        interest_event = {"cat_photo": "click_photo", "cat_audio": "click_audio",
                          "cat_meditation": "click_meditation"}.get(screen)
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
            await increment_counter(AUDIO_FUN_COUNTERS[event.callback.payload], user_id=user_id)
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
        scenario = gift_scenario_name(payload)
        user_id = event.get_ids()[1]
        if user_id is not None:
            await increment_counter(AUDIO_GIFT_COUNTERS[payload], user_id=user_id)
        await context.clear()
        await context.update_data(gift_scenario=scenario)
        await context.set_state(GIFT_SCENARIO_STATES[payload])
        await _reply(event, AUDIO_GIFT_SCENARIOS[payload][1],
                     attachments=[navigation_keyboard((("⬅️ Назад", "gift_choose_scenario"),))])

    async def send_song_result(event, tracks: list[dict], audio_format: str, *, can_regenerate=True) -> None:
        text = "🎧 Ваши треки готовы!" if len(tracks) > 1 else "🎧 Ваша песня готова!"
        buttons = []
        for index, track in enumerate(tracks, 1):
            text += f"\n\nВариант {index}: {track['title']}\n{track['audioUrl']}"
            payload = f"audio_download_fun_{index}" if audio_format == "fun" else f"audio_download_gift_{index}"
            buttons.append((f"Скачать вариант {index}", payload))
        if can_regenerate:
            buttons.append(('🔄 Не то, ещё раз (бесплатно)', 'audio_regenerate'))
        buttons.append(('🆘 Написать в поддержку', 'help_support'))
        buttons.append(("⬅️ Назад", "cat_audio"))
        keyboard = InlineKeyboardBuilder()
        for label, payload in buttons:
            keyboard.row(LinkButton(text=label, url=SUPPORT_URL) if payload == 'help_support' else CallbackButton(text=label, payload=payload))
        await _reply(event, text, attachments=[keyboard.as_markup()])

    async def generate_gift_preview(event, context):
        user_id = event.get_ids()[1]
        if user_id is None:
            return
        lock = preview_locks.setdefault(user_id, asyncio.Lock())
        claimed = False
        order_id = None
        try:
            async with lock:
                data = await context.get_data()
                scenario, details = data.get('gift_scenario'), data.get('gift_details')
                key = audio_scenario_key(scenario, 'gift')
                if key not in AUDIO_GIFT_SCENARIOS or not isinstance(details, str) or not details.strip():
                    raise InvalidSongArguments('Выберите сценарий и заполните детали песни заново.')
                order = await find_gift_preview_order(user_id, scenario, details, data.get('song_style'))
                if not order:
                    prompt, tags, title = build_song_request(details, AUDIO_GIFT_SCENARIOS[key][0], 'gift', data.get('song_style'))
                    validate_song_arguments(prompt, tags, title)
                    order_id = f'audio-{user_id}-{uuid4().hex}'
                    await create_audio_order(order_id, user_id, 'gift', scenario, details, data.get('song_style'), prompt, tags, title)
                    order = await get_audio_order(order_id)
                order_id = order['order_id']
                await context.update_data(audio_checkout_id=order_id)
                if order['preview_url']:
                    await PaidAudioWorker(event.bot, payments=payments, admin_user_id=admin_user_id).deliver_preview(order)
                    return
                # Превью не публикуется в фоновую очередь. Этот обработчик ждёт результат сам.
                claimed = await claim_audio_order(order_id, inline_preview=True)
                if not claimed:
                    await _reply(event, 'Этот заказ уже обрабатывается. Дождитесь результата, повторно оплачивать не нужно.')
                    return
                await _reply(event, '🎵 Создаю песню и фрагмент для прослушивания. Пришлю превью, как только оно будет готово. Оплачивать пока ничего не нужно.')
                tracks = json.loads(order['tracks_json']) if order['tracks_json'] else None
                if not tracks:
                    job_id = order['job_id']
                    if not job_id:
                        if order['submit_started']:
                            raise AudioNotConfigured('Отправка в TTAPI не подтверждена. Обратитесь в поддержку; не создавайте повторный заказ.')
                        prompt, tags, title = order['prompt'], order['tags'], order['title']
                        validate_song_arguments(prompt, tags, title)
                        logger.info('Готовлю текст песни для пользователя %s', user_id)
                        prompt = order['lyrics'] or await lyric_writer(prompt, tags, title)
                        validate_song_arguments(prompt, tags, title)
                        await update_audio_order(order_id, lyrics=prompt, submit_started=1)
                        logger.info('Запускаю audio_service.generate_song для пользователя %s', user_id)
                        job_id = await song_generator(prompt, tags, title)
                        await update_audio_order(order_id, job_id=job_id)
                    audio_url = await song_waiter(job_id)
                    tracks = song_tracks(audio_url)
                    await update_audio_order(order_id, tracks_json=json.dumps(tracks, ensure_ascii=False))
                preview_url = await make_protected_preview(tracks[0]['audioUrl'])
                if preview_url in {track['audioUrl'] for track in tracks}:
                    raise ValueError('Превью не должно быть полным треком.')
                await update_audio_order(order_id, preview_url=preview_url, error=None)
                order = await get_audio_order(order_id)
                await PaidAudioWorker(event.bot, payments=payments, admin_user_id=admin_user_id).deliver_preview(order)
                await update_audio_order(order_id, preview_delivered=1)
        except Exception as exc:
            logger.exception('ОШИБКА ГЕНЕРАЦИИ ПЕСНИ: %s', exc)
            data = await context.get_data()
            scenario_state = GIFT_SCENARIO_STATES.get(audio_scenario_key(data.get('gift_scenario'), 'gift'))
            if scenario_state is not None:
                await context.set_state(scenario_state)
            await _reply(event, f'❌ Ошибка: {exc}')
        finally:
            if claimed:
                try:
                    await update_audio_order(order_id, running=0)
                except Exception as exc:
                    logger.exception('ОШИБКА ГЕНЕРАЦИИ ПЕСНИ: %s', exc)
                    await _reply(event, f'❌ Ошибка сохранения заказа: {exc}')

    async def prepare_audio_checkout(context, user_id, audio_format, data):
        order_id = data.get('audio_checkout_id') or f'audio-{user_id}-{uuid4().hex}'
        await context.update_data(audio_checkout_id=order_id)
        existing_order = await get_audio_order(order_id)
        if existing_order and existing_order['status'] == 'paid':
            return order_id, None
        if audio_format == 'gift':
            if not existing_order or not existing_order['preview_url']:
                raise ValueError('Сначала необходимо создать превью.')
            existing_order = await song_checkout_order(order_id)
            if existing_order['order_id'] != order_id:
                order_id = existing_order['order_id']
                await context.update_data(audio_checkout_id=order_id, audio_checkout_link=None)
                data = await context.get_data()
            if existing_order['status'] == 'paid' or existing_order['free_access']:
                return order_id, None
        link = data.get('audio_checkout_link') or (existing_order['payment_link'] if existing_order else None)
        if not link:
            scenario = data['song_scenario' if audio_format == 'fun' else 'gift_scenario']
            details = data['song_details' if audio_format == 'fun' else 'gift_details']
            scenarios = AUDIO_FUN_SCENARIOS if audio_format == 'fun' else AUDIO_GIFT_SCENARIOS
            scenario_key = audio_scenario_key(scenario, audio_format)
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
            if audio_format == 'gift':
                data = await context.get_data()
                order = await get_audio_order(data.get('audio_checkout_id', ''))
                if order and order['status'] == 'paid' and order['user_id'] == user_id:
                    await event.ack(notification='Оплата уже получена.')
                    await _reply(event, 'Оплата уже получена. Повторно платить не нужно.')
                    return
                if not order or not order['preview_url']:
                    await event.ack(notification='Сначала создаю превью…')
                    await generate_gift_preview(event, context)
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
                scenario_key = audio_scenario_key(scenario, audio_format)
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
                    order = await get_audio_order(order_id)
                    label = 'Оплатить 199 ₽ и получить 2 варианта' if audio_format == 'fun' else f"Оплатить {order['amount_kopecks'] // 100} ₽ и получить полный трек"
                    keyboard = InlineKeyboardBuilder().row(LinkButton(text=label, url=link)).row(
                        CallbackButton(text='⬅️ Назад', payload='audio_tariff_fun' if audio_format == 'fun' else 'gift_choose_scenario')).as_markup()
                    await _reply(event, f'💳 Для оплаты перейдите по ссылке ниже. После оплаты я автоматически пришлю песню.\nЗаказ: {order_id}', attachments=[keyboard])
                except Exception as exc:
                    logger.error('Не удалось создать аудиозаказ (%s).', type(exc).__name__)
                    await _reply(event, 'Не удалось сформировать ссылку на оплату. Попробуйте позже; повторно оплачивать ранее оплаченный заказ не нужно.')
            return
        if audio_format == 'gift':
            data = await context.get_data()
            order = await get_audio_order(data.get('audio_checkout_id', ''))
            if order and order['user_id'] == user_id:
                await grant_audio_admin_access(order['order_id'], user_id)
                await event.ack(notification='Оплата пропущена.')
                await _reply(event, '👑 Режим администратора: Оплата пропущена. Пришлю полный трек по этому заказу.')
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
                await send_song_result(event, data["audio_tracks"], audio_format, can_regenerate=not data.get('audio_regeneration_used'))
                return
            expected = PosterStates.waiting_for_audio_fun_payment if audio_format == "fun" else PosterStates.waiting_for_gift_preview
            scenario = data.get("song_scenario" if audio_format == "fun" else "gift_scenario")
            details = data.get("song_details" if audio_format == "fun" else "gift_details")
            scenarios = AUDIO_FUN_SCENARIOS if audio_format == "fun" else AUDIO_GIFT_SCENARIOS
            scenario_key = audio_scenario_key(scenario, audio_format)
            if state != expected or scenario_key not in scenarios or not isinstance(details, str) or not details.strip():
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
                    prompt, tags, title = build_song_request(details, scenarios[scenario_key][0], audio_format, data.get('song_style'))
                    logger.info('Начинаю подготовку текста песни для пользователя %s.', user_id)
                    prompt = data.get('audio_lyrics') or await lyric_writer(prompt, tags, title)
                    validate_song_arguments(prompt, tags, title)
                    await context.update_data(audio_lyrics=prompt)
                    await context.update_data(audio_submit_started=True)
                    logger.info("Запускаю audio_service.generate_song для пользователя %s", user_id)
                    job_id = await song_generator(prompt, tags, title)
                    if not await session_current():
                        return
                    await context.update_data(audio_job_id=job_id)
                result = await (fun_song_waiter if audio_format == 'fun' else song_waiter)(job_id)
                if not await session_current():
                    return
                tracks = song_tracks(result)
                await context.update_data(audio_tracks=tracks)
                await context.set_state(PosterStates.waiting_for_audio_result)
                await send_song_result(event, tracks, audio_format, can_regenerate=not data.get('audio_regeneration_used'))
            except InvalidSongArguments as exc:
                logger.exception("ОШИБКА ГЕНЕРАЦИИ ПЕСНИ: %s", exc)
                if await session_current():
                    await context.update_data(audio_submit_started=False)
                    await _reply(event, str(exc))
            except AudioNotConfigured:
                logger.exception("Не настроена генерация песни:")
                if await session_current():
                    await context.update_data(audio_submit_started=False)
                    await _reply(event, "Генерация музыки временно недоступна. Попробуйте позже.")
            except SongFailed:
                logger.exception("TTAPI сообщил об ошибке генерации песни:")
                if await session_current():
                    await _reply(event, "TTAPI сообщил об ошибке генерации песни. Обратитесь в поддержку.")
            except TimeoutError:
                if await session_current():
                    await _reply(event, "Песня ещё не готова. Нажмите ту же кнопку ещё раз: проверю уже созданную задачу без новой генерации.")
            except Exception as exc:
                logger.exception("ОШИБКА ГЕНЕРАЦИИ ПЕСНИ: %s", exc)
                if await session_current():
                    await _reply(event, f"❌ Ошибка: {exc}")
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
        if (not order or order['user_id'] != event.get_ids()[1] or not (order['status'] == 'paid' or order['free_access'])
                or parts[2] not in {'0', '1'} or not (order['tracks_json'] or order['previous_tracks_json'])):
            await event.ack(notification='Трек недоступен.')
            return
        import json
        tracks = json.loads(order['tracks_json'] or order['previous_tracks_json'])
        index = int(parts[2])
        if index >= len(tracks):
            await event.ack(notification='Трек недоступен.')
            return
        await increment_counter('audio_download_mp3', user_id=order['user_id'])
        await event.ack(notification='Ссылка на MP3 готова.')
        url = tracks[index]['audioUrl']
        keyboard = InlineKeyboardBuilder().row(LinkButton(text='Скачать MP3', url=url))
        PaidAudioWorker.result_actions(keyboard, order)
        await _reply(event, url, attachments=[keyboard.as_markup()])

    @router.message_callback(F.callback.payload == 'dismiss_upsell')
    async def dismiss_song_upsell(event: MessageCallback):
        await event.ack(notification='Хорошо!')
        await _reply(event, 'Хорошо! Если надумаете, мы всегда здесь 😊')

    async def send_gift_checkout_link(event, order, *, introductory_text=None):
        order = await song_checkout_order(order['order_id'])
        if order['status'] == 'paid' or order['free_access']:
            await _reply(event, 'Доступ к полной песне уже открыт. Повторно платить не нужно.')
            return
        if payments is None:
            await _reply(event, 'Оплата временно недоступна. Попробуйте позже.')
            return
        link = order['payment_link'] or await payments.create_audio_payment_link(
            order['user_id'], 'gift', order['scenario'], order['details'], order['style'],
            order['prompt'], order['tags'], order['title'], order_id=order['order_id'])
        label = f"Оплатить {order['amount_kopecks'] // 100} ₽ и получить полный трек"
        await _reply(event, introductory_text or 'Оплатите доступ к полной песне по ссылке ниже.',
                     attachments=[InlineKeyboardBuilder().row(LinkButton(text=label, url=link)).as_markup()])

    @router.message_callback(F.callback.payload == 'upsell_song_discount')
    async def upsell_song_discount(event: MessageCallback, context: MemoryContext):
        user_id = event.get_ids()[1]
        if event.message is None or user_id is None:
            await event.ack(notification='Отправьте /start, чтобы открыть меню.')
            return
        await event.ack(notification='Открываю подарок…')
        active = await check_song_promo(user_id)
        text = ('Отличный выбор! Переходите по ссылке для оплаты со скидкой:' if active else SONG_PROMO_EXPIRED_TEXT)
        order = await latest_gift_preview(user_id)
        if order:
            try:
                await send_gift_checkout_link(event, order, introductory_text=text)
            except Exception:
                logger.error('Не удалось подготовить оплату подарочной песни для %s.', user_id)
                await _reply(event, 'Не удалось подготовить оплату. Попробуйте эту кнопку позже.')
            return
        # Без сценария и деталей оплачивать нечего: сохраняем обычный путь с превью.
        await context.clear()
        await context.set_state(PosterStates.waiting_for_mode)
        buttons = [(label, payload) for payload, (label, _) in AUDIO_GIFT_SCENARIOS.items()]
        await _reply(event, ('Скидка FIRST: песня за 392 ₽! ' if active else text + '\n\n') +
                     'Сначала выберите сценарий и напишите детали песни. После превью я пришлю ссылку на оплату по действующей цене.',
                     attachments=[navigation_keyboard((*buttons, ('⬅️ Назад', 'cat_audio')))])

    @router.message_callback(F.callback.payload.startswith('audio_gift_checkout:'))
    async def gift_preview_checkout(event: MessageCallback):
        order = await get_audio_order(event.callback.payload.removeprefix('audio_gift_checkout:'))
        if event.message is None or not order or order['user_id'] != event.get_ids()[1] or not order['preview_url']:
            await event.ack(notification='Превью недоступно.')
            return
        if order['status']=='paid' or order['free_access']:
            await event.ack(notification='Доступ к полной песне уже открыт.')
            return
        if payments is None:
            await event.ack(notification='Оплата временно недоступна.')
            return
        await event.ack(notification='Готовлю ссылку на оплату…')
        try:
            await send_gift_checkout_link(event, order)
        except Exception:
            await _reply(event,'Не удалось подготовить оплату. Попробуйте эту кнопку позже.')

    @router.message_callback(F.callback.payload.startswith('audio_admin_full:'))
    async def admin_audio_full(event: MessageCallback):
        order_id = event.callback.payload.removeprefix('audio_admin_full:')
        order = await get_audio_order(order_id)
        if event.message is None or admin_user_id is None or event.get_ids()[1] != admin_user_id or not order or order['user_id'] != admin_user_id:
            await event.ack(notification='Доступ запрещён.')
            return
        await grant_audio_admin_access(order_id, admin_user_id)
        await event.ack(notification='Оплата пропущена.')
        await _reply(event, '👑 Режим администратора: Оплата пропущена. Пришлю полный трек по этому заказу.')

    @router.message_callback(F.callback.payload.startswith('audio_paid_lyrics:') | F.callback.payload.startswith('audio_paid_instrumental:'))
    async def paid_audio_extras(event: MessageCallback):
        order_id = event.callback.payload.split(':',1)[1]
        order = await get_audio_order(order_id)
        if event.message is None or not order or order['user_id'] != event.get_ids()[1] or not (order['status']=='paid' or order['free_access']):
            await event.ack(notification='Файл недоступен.')
            return
        await event.ack(notification='Открываю…')
        keyboard = PaidAudioWorker.result_actions(InlineKeyboardBuilder(), order).as_markup()
        if event.callback.payload.startswith('audio_paid_lyrics:'):
            lyrics = order['lyrics'] or order['prompt']
            for offset in range(0, len(lyrics), 3500):
                await _reply(event, lyrics[offset:offset+3500], attachments=[keyboard] if offset+3500 >= len(lyrics) else [])
        else:
            await _reply(event, 'Минусовка пока недоступна. Напишите в поддержку — поможем с вашим заказом.', attachments=[keyboard])

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
        if event.callback.payload == 'audio_gift_preview':
            order = await get_audio_order(data.get('audio_checkout_id',''))
            if order and order['user_id'] == user_id and order['preview_url']:
                await event.ack(notification='Превью готово.')
                await _reply(event, order['preview_url'], attachments=[InlineKeyboardBuilder().row(LinkButton(text='▶️ Послушать превью', url=order['preview_url'])).as_markup()])
                return
        tracks = data.get("audio_tracks", [])
        index = {"audio_download_fun_1": ("fun", 0), "audio_download_fun_2": ("fun", 1),
                 "audio_download_gift_mp3": ("gift", 0), "audio_download_gift_1": ("gift", 0),
                 "audio_download_gift_2": ("gift", 1)}.get(event.callback.payload)
        if index and data.get("audio_format") == index[0] and len(tracks) > index[1]:
            url = tracks[index[1]]["audioUrl"]
            await event.ack(notification="Ссылка на MP3 готова.")
            keyboard = InlineKeyboardBuilder().row(LinkButton(text='Скачать MP3', url=url))
            if not data.get('audio_regeneration_used'):
                keyboard.row(CallbackButton(text='🔄 Не то, ещё раз (бесплатно)', payload='audio_regenerate'))
            keyboard.row(LinkButton(text='🆘 Написать в поддержку', url=SUPPORT_URL))
            await _reply(event, url, attachments=[keyboard.as_markup()])
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

    @router.message_callback((F.callback.payload == "audio_regenerate") | F.callback.payload.startswith('audio_regenerate:'))
    async def audio_regenerate(event: MessageCallback, context: MemoryContext) -> None:
        if event.message is None:
            await event.ack(notification="Меню недоступно. Отправьте /start.")
            return
        user_id = event.get_ids()[1]
        if user_id is not None:
            await increment_counter("audio_regenerate_click", user_id=user_id)
        data = await context.get_data()
        if event.callback.payload.startswith('audio_regenerate:'):
            order_id = event.callback.payload.removeprefix('audio_regenerate:')
            if user_id is None or not await request_audio_regeneration(order_id, user_id):
                await event.ack(notification='Перегенерация уже использована или заказ пока не готов.')
                return
            await event.ack(notification='Запускаю бесплатную перегенерацию.')
            await _reply(event, '🔄 Создаю новые варианты по вашим сохранённым данным. Повторно оплачивать не нужно.')
            return
        if (await context.get_state() == PosterStates.waiting_for_audio_result and data.get('audio_format') in {'fun','gift'}
                and admin_user_id is not None and user_id == admin_user_id):
            if data.get('audio_regeneration_used'):
                await event.ack(notification='Бесплатная перегенерация уже использована.')
                return
            kind = data['audio_format']
            await context.update_data(audio_regeneration_used=True, audio_job_id=None, audio_tracks=[], audio_submit_started=False, audio_session_id=uuid4().hex)
            await context.set_state(PosterStates.waiting_for_audio_fun_payment if kind=='fun' else PosterStates.waiting_for_gift_preview)
            await run_song_generation(event, context, kind)
            return
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

    # Команды /start, /buy и /cancel должны обрабатываться раньше ожидания фото.
    router.message_created(PosterStates.waiting_for_card_role_info)(collect_card_role_info)
    router.message_created(PosterStates.waiting_for_avatar_photo)(collect_cartoon_photo)
    router.message_created(PosterStates.waiting_for_video_teacher_details)(collect_video_teacher_details)
    router.message_created(PosterStates.waiting_for_video_song_phrase)(collect_video_song_phrase)
    router.message_created(PosterStates.waiting_for_cartoon_song_phrase)(collect_video_song_phrase)

    @router.message_callback(F.callback.payload.in_({"create_poster", "mode_bday", "mode_8march", *(payload for _, payload in MODE_BUTTONS)}))
    async def begin_poster(event: MessageCallback, context: MemoryContext) -> None:
        if event.message is None:
            await event.ack(notification="Меню недоступно. Отправьте /start.")
            return
        if await context.get_state() == PosterStates.generating:
            await event.ack(notification="Плакат уже создаётся. Пожалуйста, подождите.")
            return
        await event.ack(notification="Загружаю…")
        mode = "mode_vision" if event.callback.payload == "create_poster" else event.callback.payload
        if mode == "mode_bday":
            mode = "mode_bday_young"
        await start_mode_photo(event, context, mode)

    async def start_mode_photo(event, context, mode, *, video=False):
        await context.clear()
        await context.update_data(mode=mode)
        if video:
            await context.update_data(video_flow=True, video_amount_kopecks=29900, video_product='direct')
        if mode in ROLE_CARD_FIELDS:
            await context.update_data(card_role_session=uuid4().hex)
            if not video:
                await context.set_state(PosterStates.waiting_for_card_role_info)
                await _reply(event, ROLE_CARD_QUESTIONS[mode])
                return
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
        data = await context.get_data()
        if not data.get('video_flow') and data.get('mode') in ROLE_CARD_FIELDS and not data.get(ROLE_CARD_FIELDS[data['mode']]):
            await context.set_state(PosterStates.waiting_for_card_role_info)
            await _reply(event, ROLE_CARD_QUESTIONS[data['mode']])
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
    @router.message_created(PosterStates.waiting_for_gift_ny)
    @router.message_created(PosterStates.waiting_for_gift_teacher)
    async def collect_gift_details(event: MessageCreated, context: MemoryContext) -> None:
        if is_bot_message(event):
            return
        data = await context.get_data()
        payload = audio_scenario_key(data.get("gift_scenario", ""), "gift")
        if payload not in GIFT_SCENARIO_STATES or await context.get_state() != GIFT_SCENARIO_STATES[payload]:
            await context.clear()
            await context.set_state(PosterStates.waiting_for_mode)
            await _reply(event, "Выберите сценарий подарочной песни заново.",
                         attachments=[navigation_keyboard((("Выбрать сценарий", "gift_choose_scenario"),))])
            return
        body = event.message.body
        details = (body.text or "").strip() if body else ""
        if not details:
            await _reply(event, AUDIO_GIFT_SCENARIOS[payload][1])
            return
        await context.update_data(gift_details=details, gift_chorus_phrase=details)
        await context.set_state(PosterStates.waiting_for_gift_preview)
        await generate_gift_preview(event, context)

    @router.message_created(PosterStates.waiting_for_gift_preview)
    async def gift_preview_message(event: MessageCreated, context: MemoryContext) -> None:
        if not is_bot_message(event):
            data = await context.get_data()
            order = await get_audio_order(data.get('audio_checkout_id',''))
            if order and order['preview_url']:
                await PaidAudioWorker(event.bot, payments=payments, admin_user_id=admin_user_id).deliver_preview(order)
                return
            await _reply(event, "Песня и фрагмент создаются. Пришлю ссылку, когда превью будет готово. Чтобы изменить детали, выберите сценарий заново.",
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
            await _reply(event, AUDIO_FUN_SCENARIOS['audio_fun_' + data['song_scenario']][1])
            return
        await context.update_data(song_details=details, song_chorus_phrase=details)
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
        if not data.get('video_flow') and data['mode'] in ROLE_CARD_FIELDS and not data.get(ROLE_CARD_FIELDS[data['mode']]):
            await context.set_state(PosterStates.waiting_for_card_role_info)
            await _reply(event, ROLE_CARD_QUESTIONS[data['mode']])
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
        if data.get('video_flow'):
            if data['mode'] in ROLE_CARD_FIELDS:
                # Тематика берётся из уже введённого поздравления, без отдельного вопроса.
                await context.update_data(**{ROLE_CARD_FIELDS[data['mode']]: text})
                await generate_role_video_preview(event, context)
            else:
                await show_video_songs(event, context)
            return
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
        if mode in ROLE_CARD_FIELDS:
            await start_mode_photo(event, context, mode)
            return
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
    async def choose_mode(event: MessageCreated, context: MemoryContext) -> None:
        if not is_bot_message(event):
            if await resume_cartoon_photo(event, context):
                return
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
        if state is None or state == PosterStates.waiting_for_cartoon_payment:
            if await resume_cartoon_photo(event, context):
                return
        if state == PosterStates.waiting_for_card_role_info:
            await collect_card_role_info(event, context)
            return
        if state in (PosterStates.waiting_for_video_song_phrase, PosterStates.waiting_for_cartoon_song_phrase):
            await collect_video_song_phrase(event, context)
            return
        if state == PosterStates.waiting_for_video_teacher_details:
            await collect_video_teacher_details(event, context)
            return
        if state == PosterStates.generating_video_teacher:
            await _reply(event, 'Текст песни создаётся. Пожалуйста, подождите.')
            return
        if state == PosterStates.waiting_for_avatar_photo:
            await collect_cartoon_photo(event, context)
            return
        if state == PosterStates.cartoon_generating:
            await _reply(event, 'Персонаж, песня или видео создаётся. Пожалуйста, подождите.')
            return
        if state in (PosterStates.waiting_for_cartoon_style, PosterStates.waiting_for_avatar_review,
                     PosterStates.waiting_for_cartoon_song, PosterStates.waiting_for_cartoon_payment, PosterStates.waiting_for_cartoon_music_review):
            await _reply(event, 'Пожалуйста, выберите действие кнопкой под последним сообщением. /cancel — отмена.')
            return
        if state in GIFT_SCENARIO_STATES.values():
            await collect_gift_details(event, context)
            return
        if state == PosterStates.waiting_for_gift_preview:
            await gift_preview_message(event, context)
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

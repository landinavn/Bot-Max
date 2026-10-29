"""Приглашение поделиться ботом после успешной выдачи готового видео."""
import asyncio
import logging

from maxapi.types import LinkButton
from maxapi.utils.inline_keyboard import InlineKeyboardBuilder

from config import DEFAULT_BOT_LINK, validate_bot_link

logger = logging.getLogger(__name__)
VIDEO_SHARE_TEXT = (
    '👇 Понравился результат? Поделитесь ботом с друзьями, '
    'чтобы они тоже могли создать волшебный подарок!'
)


async def send_video_share(bot, user_id: int, *, bot_link: str = DEFAULT_BOT_LINK) -> bool:
    # Ошибка приглашения не должна превращать успешно выданный ролик в неудачный заказ.
    try:
        keyboard = InlineKeyboardBuilder().row(LinkButton(
            text='🔗 Поделиться ботом', url=validate_bot_link(bot_link))).as_markup()
        sent = await bot.send_message(user_id=user_id, text=VIDEO_SHARE_TEXT, attachments=[keyboard])
        if sent is None:
            raise RuntimeError('MAX не подтвердил отправку приглашения.')
        return True
    except asyncio.CancelledError:
        raise
    except Exception:
        logger.exception('Не удалось отправить приглашение поделиться ботом пользователю %s:', user_id)
        return False

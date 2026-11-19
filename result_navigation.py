"""Consistent navigation on completed product messages."""
from maxapi.types import CallbackButton
from maxapi.utils.inline_keyboard import InlineKeyboardBuilder

HOME_TEXT = '🏠 Главный экран'
HOME_PAYLOAD = 'back_to_main'


def home_button():
    return CallbackButton(text=HOME_TEXT, payload=HOME_PAYLOAD)


def result_keyboard(builder=None):
    """Keep product actions and append exactly one home row."""
    result = InlineKeyboardBuilder()
    if builder is not None:
        for row in builder.as_markup().payload.buttons:
            buttons = [button for button in row
                       if getattr(button, 'payload', None) not in (HOME_PAYLOAD, 'open_main_menu')]
            if buttons:
                result.row(*buttons)
    result.row(home_button())
    return result.as_markup()

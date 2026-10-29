from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from video_delivery import send_video_share, VIDEO_SHARE_TEXT


async def test_share_is_a_bot_link_in_a_separate_message():
    bot=SimpleNamespace(send_message=AsyncMock(return_value=object()))
    assert await send_video_share(bot,456,bot_link='https://max.ru/custom_bot') is True
    message=bot.send_message.await_args.kwargs
    assert message['text']==VIDEO_SHARE_TEXT and message['user_id']==456
    button=message['attachments'][0].payload.buttons[0][0]
    assert button.text=='🔗 Поделиться ботом' and button.url=='https://max.ru/custom_bot'
    assert not hasattr(button,'payload')


@pytest.mark.parametrize('failure',[RuntimeError('MAX unavailable'),None])
async def test_share_failure_is_logged_and_does_not_fail_delivery(failure,caplog):
    bot=SimpleNamespace(send_message=AsyncMock(return_value=None))
    if isinstance(failure,Exception):
        bot.send_message.side_effect=failure
    assert await send_video_share(bot,456) is False
    assert 'Не удалось отправить приглашение' in caplog.text

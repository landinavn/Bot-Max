"""Real MAX callback models: keyboard message belongs to bot, actor is callback.user."""
import logging
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from maxapi.types.updates.message_callback import MessageCallback

from multstudio.router import create_router
from multstudio import repository as repo
from test_handlers import dispatch_callback, ready_context, message_event, call_handler
from test_multstudio import studio


def event(payload, *, actor=456, actor_bot=False, deleted=False, bot=None):
    bot = bot or SimpleNamespace(send_message=AsyncMock(return_value=object()),
                                 send_callback=AsyncMock(return_value=object()))
    message = None if deleted else {
        'sender': {'user_id':499616409,'first_name':'Открытки и Карта желаний','is_bot':True},
        'recipient': {'chat_id':123,'chat_type':'dialog'},
        'timestamp':1800000000000,
        'body': {'mid':'mid-studio','seq':1,'text':'Мультстудия'},
    }
    return MessageCallback.model_validate({
        'timestamp':1800000000001,'bot':bot,'message':message,
        'callback': {'timestamp':1800000000001,'callback_id':'callback-studio', 'payload':payload,
                     'user':{'user_id':actor,'first_name':'Валентина','is_bot':actor_bot}},
    })


async def test_real_project_button_from_bot_message_acks_logs_and_opens_card(studio, caplog):
    project,settings,assets=studio
    await repo.budget(project,12500000)
    router=create_router(456,settings,assets)
    context=await ready_context()
    home=event('studio:home')
    await dispatch_callback(router,home,context)
    home.bot.send_callback.assert_awaited_once()
    buttons=home.bot.send_message.await_args.kwargs['attachments'][0].payload.buttons
    project_button=next(row[0] for row in buttons if 'Маленькие чудеса Ани' in row[0].text)
    assert project_button.payload=='studio:project:'+project
    pressed=event(project_button.payload)
    with caplog.at_level(logging.INFO,logger='multstudio.router'):
        await dispatch_callback(router,pressed,context)
    pressed.bot.send_callback.assert_awaited_once()
    sent=pressed.bot.send_message.await_args.kwargs
    assert sent['user_id']==456
    assert sent['text'].startswith('📁 Проект: Маленькие чудеса Ани\nБюджет: 12.500000$')
    assert len(sent['attachments'])==1
    buttons=[b for row in sent['attachments'][0].payload.buttons for b in row]
    assert [(b.text,b.payload) for b in buttons]==[
        ('💰 Установить бюджет','studio:budget:'+project),
        ('👥 Персонажи','studio:characters:'+project),
        ('🎬 Сцены','studio:scenes:'+project),('⬅️ Назад','studio:home')]
    assert 'Открываю проект '+project in caplog.text


async def test_existing_short_project_callback_still_opens_card(studio):
    project,settings,assets=studio
    pressed=event('studio:p:'+project)
    await dispatch_callback(create_router(456,settings,assets),pressed,await ready_context())
    assert '📁 Проект:' in pressed.bot.send_message.await_args.kwargs['text']
    pressed.bot.send_callback.assert_awaited_once()


@pytest.mark.parametrize('payload', ['characters','scenes','budget'])
async def test_project_navigation_buttons_dispatch_correctly(studio,payload):
    project,settings,assets=studio
    pressed=event('studio:'+payload+':'+project)
    await dispatch_callback(create_router(456,settings,assets),pressed,await ready_context())
    pressed.bot.send_callback.assert_awaited_once()
    sent=pressed.bot.send_message.await_args.kwargs
    assert sent['text']
    if payload=='characters':
        labels=[b.text for row in sent['attachments'][0].payload.buttons for b in row]
        assert all(name in labels for name in ('Аня','Дима','Маша'))
    elif payload=='scenes':
        assert 'Опять Новый год?!' in str(sent['attachments'])
        assert 'Задания и расходы' in str(sent['attachments'])
    else:
        assert 'USD' in sent['text']


@pytest.mark.parametrize('actor,actor_bot', [(999,False),(456,True)])
async def test_non_admin_or_bot_actor_is_rejected_despite_bot_message(studio,actor,actor_bot):
    project,settings,assets=studio
    pressed=event('studio:project:'+project,actor=actor,actor_bot=actor_bot)
    await dispatch_callback(create_router(456,settings,assets),pressed,await ready_context())
    pressed.bot.send_message.assert_not_awaited()
    pressed.bot.send_callback.assert_awaited_once()
    assert pressed.bot.send_callback.await_args.kwargs['notification']=='Раздел доступен только администратору.'


async def test_admin_bot_message_is_not_accepted_as_incoming_command(studio):
    _,settings,assets=studio
    incoming=message_event('/multstudio')
    incoming.message.sender=SimpleNamespace(is_bot=True)
    await call_handler(create_router(456,settings,assets),'studio_start',incoming,await ready_context())
    incoming.message.answer.assert_not_awaited()


async def test_missing_project_acknowledged_logged_and_reported(studio,caplog):
    _,settings,assets=studio
    pressed=event('studio:project:'+'f'*32)
    with caplog.at_level(logging.INFO,logger='multstudio.router'):
        await dispatch_callback(create_router(456,settings,assets),pressed,await ready_context())
    pressed.bot.send_callback.assert_awaited_once()
    assert 'Запись не найдена' in pressed.bot.send_message.await_args.kwargs['text']
    assert 'Открываю проект '+'f'*32 in caplog.text


async def test_project_of_other_admin_is_not_exposed(studio):
    project,settings,assets=studio
    pressed=event('studio:project:'+project,actor=999)
    await dispatch_callback(create_router(999,settings,assets),pressed,await ready_context())
    assert 'Нет доступа' in pressed.bot.send_message.await_args.kwargs['text']


async def test_deleted_keyboard_message_still_gets_ack(studio):
    project,settings,assets=studio
    pressed=event('studio:project:'+project,deleted=True)
    await dispatch_callback(create_router(456,settings,assets),pressed,await ready_context())
    pressed.bot.send_callback.assert_awaited_once()
    pressed.bot.send_message.assert_not_awaited()

"""The update campaign sends all examples and a working start button to active users."""
import asyncio
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock,call

import pytest
from maxapi.types import AttachmentUpload
from maxapi.exceptions import MaxApiError

import handlers
from handlers import create_router,PosterStates
from test_handlers import (message_event,callback_event,ready_context,call_handler,dispatch_callback,
    event_replies,isolated_example_directory,max_image_upload,text_correction)


@pytest.fixture(autouse=True)
def update_media(isolated_example_directory,monkeypatch):
    files=[*handlers.BROADCAST_UPDATE_PHOTOS,*(n for n,_ in handlers.BROADCAST_UPDATE_VIDEOS)]
    for name in files: (handlers.EXAMPLES_DIR/name).write_bytes(b'fixture media')
    monkeypatch.setattr(handlers.asyncio,'sleep',AsyncMock())


def event(command='/broadcast_update'):
    e=message_event(command)
    async def upload(media):
        return AttachmentUpload(type=media.type.value,payload={'token':Path(media.path).name})
    e.bot.upload_media=AsyncMock(side_effect=upload)
    return e


async def subscribers(db):
    for uid in (101,102,103): await db.add_credits(uid,1)
    await db.mark_user_inactive(102)


async def test_update_sends_four_photos_two_videos_and_start_for_every_subscriber(credit_db):
    await subscribers(credit_db)
    e=event();await call_handler(create_router(admin_user_id=456),'broadcast_update',e)
    assert [c.kwargs['user_id'] for c in e.bot.send_message.await_args_list]==[101]*3+[103]*3
    assert e.bot.upload_media.await_count==6  # Uploaded once for the whole audience.
    media=e.bot.upload_media.await_args_list
    assert [Path(c.args[0].path).name for c in media]==[
        'Леденец.webp','кролик1.webp','мама+дочка.webp','Женщина в красном.png',
        'cartoon_example.mp4','musical-card.mp4']
    assert [c.args[0].type.value for c in media]==['image']*4+['video']*2
    for start in (0,3):
        messages=e.bot.send_message.await_args_list[start:start+3]
        assert messages[0].kwargs['text']==handlers.BROADCAST_UPDATE_TEXT
        assert [a.type.value for a in messages[0].kwargs['attachments'][:-1]]==['image']*4
        assert [m.kwargs['attachments'][0].type.value for m in messages[1:]]==['video','video']
        for m in messages:
            button=m.kwargs['attachments'][-1].payload.buttons[0][0]
            assert (button.text,button.payload)==('🚀 Старт','open_main_menu')
    assert 'Успешно: 2, Ошибок: 0' in e.message.answer.await_args.args[0]
    assert handlers.asyncio.sleep.await_count==6
    text=handlers.BROADCAST_UPDATE_TEXT
    assert all(word in text for word in ['Музыкальные открытки','Мульт-открытки','фотосессии','Мама + дочка','Женщина в красном'])
    assert 'скоро появятся' not in text and '30 октября' not in text


@pytest.mark.parametrize('admin,actor,is_bot',[(None,456,False),(999,456,False),(456,None,False),(456,456,True)])
@pytest.mark.parametrize('name',['broadcast_update','broadcast_update_preview'])
async def test_update_is_admin_only_before_upload_or_send(credit_db,admin,actor,is_bot,name):
    await credit_db.add_credits(101,1)
    e=event('/'+name);e.get_ids=lambda:(123,actor);e.message.sender.is_bot=is_bot
    await call_handler(create_router(admin_user_id=admin),name,e)
    e.bot.upload_media.assert_not_awaited();e.bot.send_message.assert_not_awaited()
    e.message.answer.assert_not_awaited()


async def test_empty_audience_does_not_upload_media(credit_db):
    e=event();await call_handler(create_router(admin_user_id=456),'broadcast_update',e)
    e.bot.upload_media.assert_not_awaited();e.bot.send_message.assert_not_awaited()
    assert 'Успешно: 0, Ошибок: 0' in e.message.answer.await_args.args[0]


@pytest.mark.parametrize('failure',['missing_file','upload','empty_token'])
async def test_preflight_failure_sends_nothing_to_subscribers(credit_db,failure):
    await subscribers(credit_db);e=event()
    if failure=='missing_file':
        (handlers.EXAMPLES_DIR/'musical-card.mp4').unlink()
    elif failure=='upload':
        e.bot.upload_media.side_effect=[AttachmentUpload(type='image',payload={'token':'ok'})]*5+[RuntimeError('video upload failed')]
    else:
        e.bot.upload_media.side_effect=None
        e.bot.upload_media.return_value=SimpleNamespace(payload=SimpleNamespace(token=' '))
    await call_handler(create_router(admin_user_id=456),'broadcast_update',e)
    e.bot.send_message.assert_not_awaited()
    assert 'отменена' in e.message.answer.await_args.args[0]
    if failure=='missing_file':e.bot.upload_media.assert_not_awaited()


@pytest.mark.parametrize('status',[403,500])
async def test_recipient_failure_does_not_block_next_subscriber(credit_db,status):
    await subscribers(credit_db);e=event()
    e.bot.send_message.side_effect=[MaxApiError(code=status,raw='failed'),object(),object(),object()]
    await call_handler(create_router(admin_user_id=456),'broadcast_update',e)
    assert [c.kwargs['user_id'] for c in e.bot.send_message.await_args_list]==[101,103,103,103]
    assert (await credit_db.get_user(101))['is_active']==(0 if status==403 else 1)
    assert 'Успешно: 1, Ошибок: 1' in e.message.answer.await_args.args[0]


@pytest.mark.parametrize('status',[401,429])
async def test_auth_or_rate_error_stops_campaign(credit_db,status):
    await subscribers(credit_db);e=event()
    e.bot.send_message.side_effect=MaxApiError(code=status,raw='stop')
    await call_handler(create_router(admin_user_id=456),'broadcast_update',e)
    e.bot.send_message.assert_awaited_once()
    assert (await credit_db.get_user(101))['is_active']==1
    assert 'Рассылка остановлена' in e.message.answer.await_args.args[0]


@pytest.mark.parametrize('position',[0,1,2])
async def test_missing_confirmation_or_partial_delivery_is_not_counted_success(credit_db,position):
    await credit_db.add_credits(101,1);e=event()
    e.bot.send_message.side_effect=[object()]*position+[None]
    await call_handler(create_router(admin_user_id=456),'broadcast_update',e)
    assert e.bot.send_message.await_count==position+1
    assert 'Успешно: 0, Ошибок: 1' in e.message.answer.await_args.args[0]


async def test_blocked_status_update_failure_does_not_stop_campaign(credit_db,monkeypatch):
    await subscribers(credit_db);e=event()
    monkeypatch.setattr(handlers,'mark_user_inactive',AsyncMock(side_effect=RuntimeError('DB busy')))
    e.bot.send_message.side_effect=[MaxApiError(code=403,raw='blocked')]+[object()]*3
    await call_handler(create_router(admin_user_id=456),'broadcast_update',e)
    assert e.bot.send_message.await_count==4
    assert 'Успешно: 1, Ошибок: 1' in e.message.answer.await_args.args[0]


async def test_activity_is_rechecked_before_each_recipient(credit_db):
    await subscribers(credit_db);e=event()
    async def send(**kwargs):
        if kwargs['user_id']==101:await credit_db.mark_user_inactive(103)
        return object()
    e.bot.send_message.side_effect=send
    await call_handler(create_router(admin_user_id=456),'broadcast_update',e)
    assert [c.kwargs['user_id'] for c in e.bot.send_message.await_args_list]==[101]*3
    assert 'Успешно: 1, Ошибок: 0' in e.message.answer.await_args.args[0]


async def test_update_shares_lock_with_other_broadcasts(credit_db):
    await credit_db.add_credits(101,1)
    entered,release=asyncio.Event(),asyncio.Event()
    async def send(**kwargs):entered.set();await release.wait();return object()
    router=create_router(admin_user_id=456);first=event();first.bot.send_message.side_effect=send
    task=asyncio.create_task(call_handler(router,'broadcast_update',first))
    try:
        await asyncio.wait_for(entered.wait(),5)
        for name,command in [('broadcast_update','/broadcast_update'),('broadcast','/broadcast Привет')]:
            duplicate=event(command)
            await call_handler(router,name,duplicate)
            assert 'Рассылка уже выполняется' in duplicate.message.answer.await_args.args[0]
            duplicate.bot.upload_media.assert_not_awaited();duplicate.bot.send_message.assert_not_awaited()
    finally:
        release.set();await task


async def test_preview_only_sends_the_campaign_to_admin(credit_db):
    await subscribers(credit_db);e=event('/broadcast_update_preview')
    await call_handler(create_router(admin_user_id=456),'broadcast_update_preview',e)
    e.bot.send_message.assert_not_awaited()
    assert e.message.answer.await_count==3
    assert e.message.answer.await_args_list[0].args[0]==handlers.BROADCAST_UPDATE_TEXT
    assert [len(c.kwargs['attachments']) for c in e.message.answer.await_args_list]==[5,2,2]


async def test_start_button_reopens_current_menu_without_extra_free_credit(credit_db):
    await credit_db.grant_free_trial(456)
    before=(await credit_db.get_user(456))['balance']
    context=await ready_context();await context.update_data(photo={'url':'stale'},mode='old')
    await context.set_state(PosterStates.waiting_for_card_text)
    e=callback_event('open_main_menu')
    await dispatch_callback(create_router(),e,context)
    assert await context.get_data()=={}
    assert await context.get_state()==PosterStates.waiting_for_mode
    assert e.send.await_args.kwargs['attachments'][0].model_dump()==handlers.main_keyboard().model_dump()
    assert (await credit_db.get_user(456))['balance']==before


@pytest.mark.parametrize('command',['broadcast_update','broadcast_update_preview'])
async def test_real_dispatcher_routes_update_command(credit_db,monkeypatch,event_replies,command):
    from maxapi import Dispatcher
    from maxapi.context import MemoryContext
    from maxapi.context.isolation import SimpleEventIsolation
    from maxapi.types import MessageCreated,User
    monkeypatch.setattr(handlers,'_reply',event_replies)
    await credit_db.add_credits(101,1)
    sample=event()
    bot=SimpleNamespace(me=User(user_id=9999,first_name='Bot',is_bot=True),
                        send_message=sample.bot.send_message,upload_media=sample.bot.upload_media)
    dp=Dispatcher(storage=MemoryContext,event_isolation=SimpleEventIsolation())
    dp.include_routers(create_router(admin_user_id=456))
    incoming=MessageCreated(timestamp=1,bot=bot,message={
        'sender':{'user_id':456,'first_name':'Admin','is_bot':False},
        'recipient':{'chat_id':123,'chat_type':'dialog'},'timestamp':1,
        'body':{'mid':'campaign','seq':1,'text':'/'+command,'attachments':[]}})
    try:
        await dp.handle(incoming)
        if command=='broadcast_update':
            recipients=[c.kwargs['user_id'] for c in bot.send_message.await_args_list if c.kwargs.get('text')==handlers.BROADCAST_UPDATE_TEXT]
            assert recipients==[101,456]
            assert 'Успешно: 2, Ошибок: 0' in bot.send_message.await_args.kwargs['text']
        else:
            assert bot.send_message.await_count==3
            assert all(c.kwargs.get('user_id')!=101 for c in bot.send_message.await_args_list)
            assert all(c.kwargs.get('user_id')==456 for c in bot.send_message.await_args_list)
    finally:await dp.shutdown()

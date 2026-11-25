from unittest.mock import AsyncMock

import httpx
import pytest
from maxapi.types.attachments.attachment import Attachment

from multstudio import repository as repo
from multstudio.music import upload_music, MAX_BYTES
from multstudio.render import process, probe
from multstudio.router import create_router, StudioStates
from test_multstudio import studio
from test_multstudio_character_description import replies
from test_multstudio_project_menu import event
from test_handlers import dispatch_callback, ready_context, message_event, call_handler


async def episode(studio):
    return (await repo.children('episode',studio[0]))[0]


@pytest.mark.parametrize('kind',['audio','file','link'])
async def test_music_accepts_max_mp3_or_existing_link(studio,replies,monkeypatch,kind):
    ep=await episode(studio);ctx=await ready_context()
    router=create_router(456,studio[1],studio[2])
    pressed=event('studio:music:'+ep['id']);await dispatch_callback(router,pressed,ctx)
    assert 'MP3' in pressed.bot.send_message.await_args.kwargs['text']
    service=AsyncMock();monkeypatch.setattr('multstudio.music.upload_music',service)
    submitted=message_event('https://cdn.example/music.mp3' if kind=='link' else None)
    if kind!='link':submitted.message.body.attachments=[Attachment(type=kind,payload={'url':'https://cdn.example/music.mp3'})]
    await call_handler(router,'studio_input',submitted,ctx)
    assert service.await_args.args[0] is studio[2]
    assert service.await_args.args[1]['id']==ep['id']
    assert service.await_args.args[2]=='https://cdn.example/music.mp3'
    assert await ctx.get_state()!=StudioStates.input
    assert 'музыка сохранена' in str(submitted.message.answer.await_args)


async def test_bad_attachment_keeps_music_input_open(studio,replies,monkeypatch):
    ep=await episode(studio);ctx=await ready_context();router=create_router(456,studio[1],studio[2])
    await dispatch_callback(router,event('studio:music:'+ep['id']),ctx)
    service=AsyncMock();monkeypatch.setattr('multstudio.music.upload_music',service)
    submitted=message_event(None)
    submitted.message.body.attachments=[Attachment(type='image',payload={'url':'https://cdn.example/image.png','photo_id':1,'token':'t'})]
    await call_handler(router,'studio_input',submitted,ctx)
    service.assert_not_awaited()
    assert await ctx.get_state()==StudioStates.input


async def test_non_owner_cannot_open_music_upload(studio,replies):
    ep=await episode(studio);ctx=await ready_context();router=create_router(999,studio[1],studio[2])
    await dispatch_callback(router,event('studio:music:'+ep['id'],actor=999),ctx)
    assert await ctx.get_state()!=StudioStates.input


async def test_real_mp3_is_normalized_and_used_by_next_episode_snapshot(studio,monkeypatch):
    ep=await episode(studio);assets=studio[2];assets.root.mkdir(parents=True,exist_ok=True)
    original=assets.path('incoming.bin')
    await process('ffmpeg','-nostdin','-v','error','-y','-f','lavfi','-i',
                  'sine=frequency=330:duration=0.5','-f','mp3',original)
    download=AsyncMock(return_value=(original.name,assets.checksum(original)))
    monkeypatch.setattr(assets,'download',download)
    snap=await repo.snapshot('episode',ep['id'])
    key=await upload_music(assets,snap,'https://cdn.example/music.mp3')
    download.assert_awaited_once_with('https://cdn.example/music.mp3','bin',limit=MAX_BYTES)
    selected=(await repo.snapshot('episode',ep['id']))['assets']['music']
    assert selected['id']==key and selected['path']!=original.name
    info=await probe(assets.path(selected['path']))
    track=next(s for s in info['streams'] if s['codec_type']=='audio')
    assert track['codec_name']=='mp3' and track['sample_rate']=='48000' and track['channels']==2
    assert original.is_file()


@pytest.mark.parametrize('failure',['corrupt','wav','timeout'])
async def test_invalid_upload_does_not_replace_previous_music(studio,monkeypatch,failure):
    ep=await episode(studio);assets=studio[2];assets.root.mkdir(parents=True,exist_ok=True)
    old_path,checksum=await assets.write(b'old-music','mp3')
    snap=await repo.snapshot('episode',ep['id'])
    previous=await repo.add_asset(studio[0],'episode',ep['id'],'music',snap['context_hash'],old_path,checksum)
    bad=assets.path('bad.bin')
    if failure=='wav':
        await process('ffmpeg','-v','error','-y','-f','lavfi','-i','sine=duration=0.1','-f','wav',bad)
    else:bad.write_bytes(b'not an audio file')
    download=AsyncMock(return_value=(bad.name,'hash'))
    if failure=='timeout':download.side_effect=httpx.ReadTimeout('private URL must not be shown')
    monkeypatch.setattr(assets,'download',download)
    with pytest.raises(ValueError):await upload_music(assets,snap,'https://cdn.example/file.mp3')
    assert (await repo.snapshot('episode',ep['id']))['assets']['music']['id']==previous

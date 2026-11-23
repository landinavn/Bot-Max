"""Recovery must keep original images and reviewed words without calling paid generators."""
from datetime import datetime, timedelta, timezone
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest
from maxapi.types import AttachmentUpload

import cartoon_avatar_archive as archive
import database
import handlers
from test_handlers import callback_event,message_event,ready_context,dispatch_callback,call_handler,event_replies

URL='https://cdn.example/clean-mother.png'
WORDS='[Verse 1]\nТекст согласован\n[Chorus]\nСпасибо мама'


async def test_archive_survives_restart_filters_owner_and_deduplicates(credit_db):
    key=await archive.save(456,URL,'theme_mother')
    assert await archive.save(456,URL,'theme_mother')==key
    await archive.save(999,'https://cdn.example/other.png','theme_mother')
    await database.init_db()
    assert len(await archive.recent(456))==1
    assert (await archive.get(456,key))['image_url']==URL
    assert await archive.get(999,key) is None


async def test_clean_link_is_saved_before_old_state_is_cleared(credit_db):
    router=handlers.create_router(admin_user_id=456)
    ctx=await ready_context()
    await ctx.update_data(cartoon_flow='mother',cartoon_theme='theme_mother',chosen_avatar=URL,
        original_image_url=URL,watermarked_image_url='https://cdn.example/preview.png')
    await dispatch_callback(router,callback_event('theme_mother'),ctx)
    assert await ctx.get_state()==handlers.PosterStates.waiting_for_song_setup
    assert (await archive.recent(456))[0]['image_url']==URL


async def test_restore_keeps_reviewed_words_and_never_generates_picture_or_music(credit_db,monkeypatch):
    key=await archive.save(456,URL,'theme_mother')
    image=AsyncMock();music=AsyncMock();writer=AsyncMock()
    upload=AsyncMock(return_value=AttachmentUpload(type='image',payload={'token':'restored'}))
    monkeypatch.setattr(handlers,'upload_image_for_max',upload)
    music.return_value='restored-image-song-job'
    router=handlers.create_router(admin_user_id=456,avatar_generator=image,song_generator=music,lyric_writer=writer,
        song_waiter=AsyncMock(return_value={'data':{'musics':[{'audioUrl':'https://cdn.example/song.mp3'}]}}))
    ctx=await ready_context()
    await ctx.update_data(cartoon_flow='mother',cartoon_theme='theme_mother',
        song_review_approved=True,song_review_lyrics=WORDS,song_review_details='Ольга; спасибо',
        song_review_voice='female',song_review_tags='female vocals',song_review_format='gift',
        song_review_notes='Ольга; спасибо',song_review_title='Песня маме')
    await ctx.set_state(handlers.PosterStates.waiting_for_avatar_photo)
    await call_handler(router,'restore_cartoon_image',message_event('/restore_cartoon_image'),ctx)
    await dispatch_callback(router,callback_event('restore_cartoon_show:'+str(key)),ctx)
    upload.assert_awaited_once();assert upload.await_args.args[1]==URL
    await dispatch_callback(router,callback_event('restore_cartoon_use:'+str(key)),ctx)
    d=await ctx.get_data()
    assert d['chosen_avatar']==d['original_image_url']==URL
    assert d['song_review_lyrics']==WORDS and d['song_review_voice']=='female' and d['_song_seed']
    assert not d['card_song_precheckout']
    assert await ctx.get_state()==handlers.PosterStates.waiting_for_avatar_review
    assert d['avatar_urls']=={'1':URL}
    image.assert_not_awaited();music.assert_not_awaited();writer.assert_not_awaited()
    # Reusing the old confirmation is harmless after consuming it.
    await dispatch_callback(router,callback_event('restore_cartoon_use:'+str(key)),ctx)
    image.assert_not_awaited();music.assert_not_awaited()
    monkeypatch.setattr(handlers,'create_audio_preview',AsyncMock(return_value='https://cdn.example/preview.mp3'))
    monkeypatch.setattr(handlers,'upload_audio_for_max',AsyncMock(return_value=AttachmentUpload(type='audio',payload={'token':'audio'})))
    await dispatch_callback(router,callback_event('avatar_ok_1'),ctx)
    image.assert_not_awaited();writer.assert_not_awaited();music.assert_awaited_once()
    assert music.await_args.args[0]==WORDS
    assert (await ctx.get_data())['chosen_avatar']==URL


async def test_public_user_cannot_restore_or_import_an_admin_image(credit_db,monkeypatch):
    key=await archive.save(999,URL,'theme_mother')
    uploader=AsyncMock();monkeypatch.setattr(handlers,'upload_image_for_max',uploader)
    router=handlers.create_router(admin_user_id=999);ctx=await ready_context()
    await call_handler(router,'restore_cartoon_image',message_event('/restore_cartoon_image'),ctx)
    await dispatch_callback(router,callback_event('restore_cartoon_show:'+str(key)),ctx)
    await dispatch_callback(router,callback_event('restore_cartoon_use:'+str(key)),ctx)
    uploader.assert_not_awaited();assert not (await ctx.get_data()).get('chosen_avatar')


async def test_legacy_scan_shows_existing_files_without_generating_them(credit_db,monkeypatch):
    items=[{'url':'https://cdn.example/'+str(i)+'.png','date':'2026-11-23T18:00:00Z'} for i in range(5)]
    monkeypatch.setattr(archive,'legacy_objects',AsyncMock(return_value=(items,True)))
    monkeypatch.setattr(handlers.Config,'from_env',lambda:SimpleNamespace())
    upload=AsyncMock(return_value=AttachmentUpload(type='image',payload={'token':'candidate'}))
    monkeypatch.setattr(handlers,'upload_image_for_max',upload)
    gen=AsyncMock();router=handlers.create_router(admin_user_id=456,avatar_generator=gen)
    ctx=await ready_context();e=message_event('/restore_cartoon_image storage')
    await call_handler(router,'restore_cartoon_image',e,ctx)
    assert upload.await_count==3 and 'без PREVIEW' in str(e.message.answer.await_args_list)
    await dispatch_callback(router,callback_event('restore_cartoon_more'),ctx)
    assert upload.await_count==5
    assert not (await ctx.get_data()).get('chosen_avatar')
    gen.assert_not_awaited()


async def test_import_only_owned_orders_and_never_treat_preview_as_original(credit_db):
    for user in (456,999):
        key=await database.create_cartoon_order('cartoon_archive_'+str(user),user,'theme_mother','mother')
        await database.update_cartoon_order(key,fsm_data=json.dumps({'cartoon_theme':'theme_mother',
            'avatar_urls':{'1':URL if user==456 else 'https://cdn.example/other.png'},
            'watermarked_image_url':'https://cdn.example/preview.png'}))
    await archive.import_saved_orders(456)
    assert [x['image_url'] for x in await archive.recent(456)]==[URL]
    assert not archive.originals({'original_image_url':'https://cdn.example/preview.png','watermarked_image_url':'https://cdn.example/preview.png'})


@pytest.mark.parametrize('url',['http://store.example/b/vision-boards/'+'a'*32+'.png',
    'https://store.example/bad/vision-boards/'+'a'*32+'.png',
    'https://store.example/b/vision-boards/'+'a'*32+'.png?token=secret',
    'https://store.example/b/vision-boards/../other.png'])
def test_explicit_legacy_url_must_match_this_bots_storage(url):
    with pytest.raises(ValueError):archive.storage_key(SimpleNamespace(storage_bucket='b',storage_public_base_url='https://store.example/b'),url)


def test_storage_scan_sorts_dates_not_uuid_and_closes_client(monkeypatch):
    now=datetime.now(timezone.utc)
    client=Mock();client.list_objects_v2.side_effect=[
        {'Contents':[{'Key':'vision-boards/'+'f'*32+'.png','LastModified':now-timedelta(hours=1)}],
         'IsTruncated':True,'NextContinuationToken':'page2'},
        {'Contents':[{'Key':'vision-boards/'+'a'*32+'.png','LastModified':now}], 'IsTruncated':False}]
    monkeypatch.setattr('image_service._storage_client',lambda _:client)
    items,complete=archive._legacy_objects(SimpleNamespace(storage_bucket='b',storage_public_base_url='https://cdn.example'))
    assert complete and items[0]['url'].endswith('a'*32+'.png')
    assert client.list_objects_v2.call_args.kwargs['ContinuationToken']=='page2'
    client.close.assert_called_once()

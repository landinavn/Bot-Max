import asyncio
import json
from pathlib import Path
from unittest.mock import AsyncMock

import httpx
import pytest

import database
import handlers
import song_alignment as align
import mother_recovery as recovery
from mother_admin_sessions import save, latest
from test_handlers import ready_context, message_event, callback_event, call_handler, dispatch_callback, event_replies

IMAGE='https://cdn.example/clean.png'
AUDIO='https://cdn.example/full.mp3'
LYRICS='[Verse]\nВ окне родного дома свет не гаснет'


def saved():
    return {'cartoon_theme':'theme_mother','cartoon_session_id':'test-mother',
        'chosen_avatar':IMAGE,'original_image_url':IMAGE,'song_1_url':AUDIO,
        'selected_song_variant':1,'song_review_lyrics':LYRICS,'cartoon_video_failed':True,
        'cartoon_kling_story':{'alignment_submitting':align.alignment_source(AUDIO,LYRICS),'shots':[{} for _ in range(6)]}}


async def resolve(story, checkpoint=None, *, audio=AUDIO, lyrics=LYRICS):
    return await align.resolve_song_start(story,Path('song.mp3'),lyrics,audio,Path('.'),checkpoint or AsyncMock())


async def test_transient_failure_is_cleared_and_success_is_reused(monkeypatch):
    detector=AsyncMock(side_effect=[TimeoutError(),3.2]);monkeypatch.setattr(align,'detect_song_start',detector)
    story={};snapshots=[]
    async def checkpoint():snapshots.append(json.loads(json.dumps(story)))
    assert await resolve(story,checkpoint)==3.2
    assert story['alignment_attempts']==2 and not story['alignment_submitting'] and not story['alignment_error']
    assert any(s.get('alignment_error') and not s.get('alignment_submitting') for s in snapshots)
    assert await resolve(story)==3.2 and detector.await_count==2


async def test_legacy_stale_marker_can_complete_after_restore(monkeypatch):
    detector=AsyncMock(return_value=4.);monkeypatch.setattr(align,'detect_song_start',detector)
    story=saved()['cartoon_kling_story']
    assert await resolve(story)==4. and story['alignment_attempts']==2
    detector.assert_awaited_once()


async def test_attempt_budget_survives_cancellation_restart_and_is_bounded(monkeypatch):
    detector=AsyncMock(side_effect=asyncio.CancelledError());monkeypatch.setattr(align,'detect_song_start',detector)
    story={}
    with pytest.raises(asyncio.CancelledError):await resolve(story)
    assert story['alignment_submitting'] is None and story['alignment_attempts']==1
    restored=json.loads(json.dumps(story));detector.side_effect=TimeoutError()
    with pytest.raises(align.SongAlignmentError):await resolve(restored)
    with pytest.raises(align.SongAlignmentError,match='Две попытки'):await resolve(restored)
    assert detector.await_count==2


async def test_mismatch_and_configuration_errors_are_not_automatically_retried(monkeypatch):
    detector=AsyncMock(side_effect=ValueError('No matching words'));monkeypatch.setattr(align,'detect_song_start',detector)
    story={}
    with pytest.raises(align.SongAlignmentError):await resolve(story)
    detector.assert_awaited_once()
    assert not story['alignment_submitting']


async def test_changed_audio_or_text_has_separate_alignment_but_preserves_shots(monkeypatch):
    detector=AsyncMock(return_value=1.);monkeypatch.setattr(align,'detect_song_start',detector)
    story={'shots':[{'task_id':'paid-id'}]}
    for url,text in ((AUDIO,LYRICS),(AUDIO,LYRICS+'!'),(AUDIO+'?second',LYRICS+'!')):
        assert await resolve(story,audio=url,lyrics=text)==1.
    assert detector.await_count==3 and story['alignment_attempts']==1
    assert story['shots']==[{'task_id':'paid-id'}]


@pytest.mark.parametrize('status,expected',[(400,False),(401,False),(403,False),(408,True),(429,True),(500,True),(503,True)])
def test_provider_status_retry_classification(status,expected):
    from openai import APIStatusError
    error=APIStatusError('test',response=httpx.Response(status,request=httpx.Request('POST','https://api.example/')),body={})
    assert bool(align.retryable_alignment_error(error)) is expected


async def test_manual_paid_alignment_order_can_resume_without_losing_media(credit_db):
    key=await database.create_cartoon_order('cartoon_retry',456,'theme_mother','mother')
    await database.mark_cartoon_paid(key,'paid-cartoon')
    data=saved();data['cartoon_order_id']=key
    await database.update_cartoon_order(key,fsm_data=json.dumps(data),fsm_state=str(handlers.PosterStates.cartoon_manual_review))
    await database.finish_cartoon_order(key,456,manual=True)
    order=await database.get_cartoon_order(key)
    assert await recovery.recover_order(order,handlers.PosterStates.waiting_for_cartoon_song_phrase)
    result=await database.get_cartoon_order(key)
    assert result['status']=='paid' and result['provider_payment_id']=='paid-cartoon'
    restored=json.loads(result['fsm_data'])
    assert not restored['cartoon_video_failed'] and recovery.urls(restored)==(IMAGE,AUDIO)
    assert restored['cartoon_kling_story']==data['cartoon_kling_story']
    assert not await recovery.recover_order(order,handlers.PosterStates.waiting_for_cartoon_song_phrase)


@pytest.mark.parametrize('kind',['video_unknown','image_unknown','exhausted','already_aligned'])
def test_recovery_never_unlocks_unrelated_paid_tasks_or_exhausted_budget(kind):
    data=saved();story=data['cartoon_kling_story']
    if kind=='video_unknown':story['shots'][0]={'video_submitting':True}
    elif kind=='image_unknown':story['shots'][0]={'image_submitting':True}
    elif kind=='exhausted':story.update(alignment_attempt_source=align.alignment_source(AUDIO,LYRICS),alignment_attempts=2)
    else:story['alignment_source']=align.alignment_source(AUDIO,LYRICS)
    assert not recovery.alignment_only(data)


async def test_assets_survive_fsm_loss_and_are_admin_only_readonly(credit_db,monkeypatch):
    data=saved();await save(456,data,handlers.PosterStates.cartoon_manual_review)
    ctx=await ready_context();before=await ctx.get_data()
    router=handlers.create_router(admin_user_id=456)
    monkeypatch.setattr(handlers,'upload_image_for_max',AsyncMock(side_effect=RuntimeError('MAX down')))
    monkeypatch.setattr(handlers,'upload_audio_for_max',AsyncMock(side_effect=RuntimeError('MAX down')))
    e=message_event('/mother_assets')
    await call_handler(router,'mother_assets',e,ctx)
    text=str(e.message.answer.await_args_list)
    assert IMAGE in text and AUDIO in text and '⬇️ Песня' in text
    assert await ctx.get_data()==before
    assert (await latest(456))[0]==data
    denied=message_event('/mother_assets');denied.get_ids=lambda:(123,789)
    await call_handler(router,'mother_assets',denied,ctx)
    denied.message.answer.assert_not_awaited()
    handlers.generate_song.assert_not_awaited()


async def test_admin_can_export_specific_client_order_without_mutating_it(credit_db):
    key=await database.create_cartoon_order('cartoon_client',789,'theme_mother','mother')
    data=saved();await database.update_cartoon_order(key,fsm_data=json.dumps(data))
    before=await database.get_cartoon_order(key)
    label,got=await recovery.assets(456,key)
    assert label==key and recovery.urls(got)==(IMAGE,AUDIO)
    assert await database.get_cartoon_order(key)==before
    with pytest.raises(ValueError):await recovery.assets(456,'missing')


async def test_client_resume_replaces_failed_live_state_and_uses_retained_song(credit_db):
    key=await database.create_cartoon_order('cartoon_client_resume',456,'theme_mother','mother')
    await database.mark_cartoon_paid(key,'client-paid')
    data=saved()
    data.update(cartoon_order_id=key,cartoon_flow='short',cartoon_auto_pipeline=True,
        chosen_song='audio_fun_mother',chosen_song_phrase='Маме; спасибо',song_attempt=1,
        song_review_approved=True,song_review_details='Маме; спасибо',song_review_voice='female')
    await database.update_cartoon_order(key,fsm_data=json.dumps(data),fsm_state=str(handlers.PosterStates.cartoon_manual_review))
    await database.finish_cartoon_order(key,456,manual=True)
    ctx=await ready_context();await ctx.set_data(data);await ctx.set_state(handlers.PosterStates.cartoon_manual_review)
    video=AsyncMock(return_value='https://cdn.example/finished.mp4')
    router=handlers.create_router(admin_user_id=999,cartoon_video_generator=video)
    await dispatch_callback(router,callback_event('cartoon_resume:'+key),ctx)
    video.assert_awaited_once_with(IMAGE,AUDIO)
    handlers.generate_song.assert_not_awaited()
    assert (await database.get_cartoon_order(key))['status']=='completed'


async def test_admin_resume_clears_only_alignment_failure_and_keeps_song(credit_db,monkeypatch):
    data=saved();data.update(song_review_user=456,cartoon_flow='short',chosen_song='audio_fun_mother',
        chosen_song_phrase='Маме; спасибо',song_attempt=1,song_1_preview_url='https://cdn.example/preview.mp3',
        song_review_approved=True,song_review_details='Маме; спасибо',song_review_voice='female')
    await save(456,data,handlers.PosterStates.cartoon_manual_review)
    monkeypatch.setattr(handlers,'upload_audio_for_max',AsyncMock(return_value=object()))
    router=handlers.create_router(admin_user_id=456)
    ctx=await ready_context()
    await call_handler(router,'mother_resume',message_event('/mother_resume'),ctx)
    restored=await ctx.get_data()
    assert not restored['cartoon_video_failed']
    assert recovery.urls(restored)==(IMAGE,AUDIO)
    assert await ctx.get_state()==handlers.PosterStates.waiting_for_cartoon_music_review
    handlers.generate_song.assert_not_awaited()

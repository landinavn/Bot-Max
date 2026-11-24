import json
from unittest.mock import AsyncMock

import pytest

import handlers
import database
import mother_recovery as recovery
import mother_story
from mother_admin_sessions import save
from song_alignment import alignment_source
from test_handlers import ready_context,call_handler,dispatch_callback,message_event,callback_event,event_replies
from test_mothers_day import renderer

AUDIO='https://cdn.example/chosen-second.mp3'
IMAGE='https://cdn.example/mom.png'
TEXT='[Verse]\nВ окне родного дома свет не гаснет'


def snapshot():
    return {'cartoon_session_id':'resume-manual-13','cartoon_theme':'theme_mother','cartoon_flow':'mother',
        'chosen_avatar':IMAGE,'original_image_url':IMAGE,'selected_song_variant':2,
        'song_1_url':'https://cdn.example/first.mp3','song_2_url':AUDIO,
        'chosen_song':'audio_fun_mother','chosen_song_phrase':'Маме; спасибо',
        'song_review_approved':True,'song_review_details':'Маме; спасибо','song_review_lyrics':TEXT,
        'song_review_voice':'female','song_review_tags':'female vocals','song_review_title':'Маме',
        'song_review_user':456,'cartoon_video_failed':True,
        'cartoon_kling_story':{'source':[mother_story.POLICY,IMAGE],
            'shots':[{} for _ in range(6)],'alignment_source':alignment_source(AUDIO,TEXT),
            'audio_start':13.,'alignment_method':'admin','alignment_submitting':None}}


async def test_resume_keeps_second_song_and_manual_13_without_new_music_or_image(credit_db,monkeypatch):
    d=snapshot();await save(456,d,handlers.PosterStates.cartoon_manual_review)
    generator=AsyncMock();picture=AsyncMock();ctx=await ready_context()
    render=AsyncMock(return_value='https://cdn.example/ready.mp4')
    monkeypatch.setattr(mother_story,'generate',render)
    router=handlers.create_router(admin_user_id=456,song_generator=generator,avatar_generator=picture)
    e=message_event('/mother_resume');await call_handler(router,'mother_resume',e,ctx)
    assert 'первые 13 секунд' in str(e.message.answer.await_args_list)
    restored=await ctx.get_data()
    assert not restored['cartoon_video_failed'] and restored['selected_song_variant']==2
    assert recovery.saved_start(restored)==13.
    token=restored['cartoon_resume_token']
    render.assert_not_awaited()
    await dispatch_callback(router,callback_event('mother_continue_saved:'+token),ctx)
    render.assert_awaited_once()
    assert render.await_args.args[1:]==(IMAGE,AUDIO)
    assert render.await_args.args[0]['cartoon_kling_story']['audio_start']==13.
    await dispatch_callback(router,callback_event('mother_continue_saved:'+token),ctx)
    render.assert_awaited_once();generator.assert_not_awaited();picture.assert_not_awaited()


async def test_saved_selection_is_restored_only_if_not_explicitly_selected():
    d=snapshot();d['selected_song_variant']=None
    recovery.restore_start_selection(d)
    assert d['selected_song_variant']==2
    d['selected_song_variant']=1
    recovery.restore_start_selection(d)
    assert d['selected_song_variant']==1 and recovery.saved_start(d) is None


@pytest.mark.parametrize('change',['audio','text','unknown_image','known_video'])
async def test_resume_never_clears_unrelated_failure_or_applies_offset_to_other_source(credit_db,change):
    d=snapshot()
    if change=='audio':d['song_2_url']='https://cdn.example/different.mp3'
    elif change=='text':d['song_review_lyrics']='Совершенно другие слова песни'
    elif change=='unknown_image':d['cartoon_kling_story']['shots'][0]={'image_submitting':True}
    else:d['cartoon_kling_story']['shots'][0]={'task_id':'already-submitted-video'}
    assert not recovery.can_recover_start(d)
    await save(456,d,handlers.PosterStates.cartoon_manual_review)
    ctx=await ready_context();router=handlers.create_router(admin_user_id=456)
    e=message_event('/mother_resume');await call_handler(router,'mother_resume',e,ctx)
    assert 'блокировка ещё активна' in str(e.message.answer.await_args_list)
    assert (await ctx.get_data())['cartoon_video_failed']
    assert not (await ctx.get_data()).get('cartoon_resume_token')


async def test_paid_manual_order_keeps_payment_and_recovers_verified_offset(credit_db):
    key=await database.create_cartoon_order('cartoon_manual_ready',456,'theme_mother','mother')
    await database.mark_cartoon_paid(key,'payment-before-trim')
    d=snapshot();d['cartoon_order_id']=key;d['selected_song_variant']=None
    await database.update_cartoon_order(key,fsm_data=json.dumps(d))
    await database.finish_cartoon_order(key,456,manual=True)
    order=await database.get_cartoon_order(key)
    assert await recovery.recover_order(order,handlers.PosterStates.waiting_for_cartoon_music_review)
    order=await database.get_cartoon_order(key)
    assert order['status']=='paid' and order['provider_payment_id']=='payment-before-trim'
    recovered=json.loads(order['fsm_data'])
    assert recovered['selected_song_variant']==2 and recovery.saved_start(recovered)==13.


async def test_story_uses_exact_saved_13_without_another_transcription(renderer,monkeypatch):
    d=snapshot();d['cartoon_video_failed']=False
    detector=AsyncMock(side_effect=AssertionError('Do not transcribe this audio again'))
    monkeypatch.setattr('song_alignment.detect_song_start',detector)
    async def render(clips,audio,target,duration,**kwargs):
        assert kwargs['audio_start']==13. and duration==60.
        target.write_bytes(b'mp4')
    monkeypatch.setattr(mother_story,'render_local',AsyncMock(side_effect=render))
    assert await mother_story.generate(d,IMAGE,AUDIO,checkpoint=AsyncMock(),progress=AsyncMock())=='https://cdn.example/mother.mp4'
    detector.assert_not_awaited()

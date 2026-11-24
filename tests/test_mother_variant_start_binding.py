import json
from unittest.mock import AsyncMock

import pytest

import database
import handlers
import mother_recovery as recovery
from mother_admin_sessions import save,latest
from song_alignment import alignment_source
from test_handlers import ready_context,message_event,call_handler,event_replies

WORDS='[Verse]\nВ окне родного дома свет не гаснет'
ONE='https://cdn.example/one.mp3'
TWO='https://cdn.example/two.mp3'


def snapshot():
    return {'cartoon_session_id':'wrong-variant-offset','cartoon_theme':'theme_mother','song_review_user':456,
        'chosen_avatar':'https://cdn.example/mother.png','song_review_lyrics':WORDS,
        'song_1_url':ONE,'song_2_url':TWO,'selected_song_variant':2,'cartoon_video_failed':True,
        'cartoon_kling_story':{'alignment_source':alignment_source(ONE,WORDS),'audio_start':13.,
            'alignment_method':'admin','shots':[{} for _ in range(6)]}}


async def test_exact_reported_snapshot_can_bind_13_to_second_variant():
    d=snapshot()
    assert recovery.saved_start(d) is None
    recovery.manual_start(d,13,variant=2)
    assert recovery.saved_start(d)==13. and not d['cartoon_video_failed']
    assert d['video_song_url']==TWO and d['selected_song_variant']==2
    assert d['cartoon_kling_story']['alignment_manual_history'][0]['source']==alignment_source(ONE,WORDS)
    assert d['song_1_url']==ONE and d['song_2_url']==TWO


async def test_explicit_command_reports_variant_and_persists_without_generation(credit_db):
    d=snapshot();await save(456,d,handlers.PosterStates.cartoon_manual_review)
    ctx=await ready_context();router=handlers.create_router(admin_user_id=456)
    event=message_event('/mother_song_start 13 2')
    await call_handler(router,'mother_song_start',event,ctx)
    assert 'Вариант 2' in str(event.message.answer.await_args_list)
    stored=(await latest(456))[0]
    assert recovery.saved_start(stored)==13. and stored['selected_song_variant']==2
    handlers.generate_song.assert_not_awaited()


async def test_unselected_two_variants_never_default_silently_to_first(credit_db):
    d=snapshot();d['selected_song_variant']=None
    await save(456,d,handlers.PosterStates.cartoon_manual_review)
    ctx=await ready_context();router=handlers.create_router(admin_user_id=456)
    event=message_event('/mother_song_start 13')
    await call_handler(router,'mother_song_start',event,ctx)
    assert 'вариант явно' in str(event.message.answer.await_args_list)
    assert (await latest(456))[0]==d


@pytest.mark.parametrize('field',['image_submitting','image_url','video_submitting','task_id','video_url'])
def test_explicit_rebind_never_unlocks_any_started_visual_request(field):
    d=snapshot();d['cartoon_kling_story']['shots'][0]={field:'existing'}
    original=json.loads(json.dumps(d))
    with pytest.raises(ValueError):recovery.manual_start(d,13,variant=2)
    assert d==original


@pytest.mark.parametrize('kind',['words','unknown_audio','finished'])
def test_cannot_rebind_unrelated_source_or_completed_video(kind):
    d=snapshot()
    if kind=='words':d['song_review_lyrics']='Совершенно другая песня'
    elif kind=='unknown_audio':d['cartoon_kling_story']['alignment_source'][0]='https://cdn.example/unrelated.mp3'
    else:d['cartoon_kling_story']['result_url']='https://cdn.example/done.mp4'
    with pytest.raises(ValueError):recovery.manual_start(d,13,variant=2)


async def test_paid_manual_rebind_preserves_payment_and_purchase_count(credit_db):
    key=await database.create_cartoon_order('cartoon_explicit_variant',456,'theme_mother','mother')
    await database.mark_cartoon_paid(key,'explicit-variant-paid')
    d=snapshot();d['cartoon_order_id']=key
    await database.update_cartoon_order(key,fsm_data=json.dumps(d));await database.finish_cartoon_order(key,456,manual=True)
    assert await recovery.recover_order(await database.get_cartoon_order(key),handlers.PosterStates.waiting_for_cartoon_music_review,start=13,variant=2)
    order=await database.get_cartoon_order(key)
    assert order['status']=='paid' and order['provider_payment_id']=='explicit-variant-paid'
    assert recovery.saved_start(json.loads(order['fsm_data']))==13.
    assert (await database.get_user(456))['total_purchases']==1

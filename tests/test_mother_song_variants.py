import asyncio
import json
from unittest.mock import AsyncMock

import pytest

import mother_song_variants as variants
import handlers
import database
from test_handlers import ready_context,callback_event,dispatch_callback,event_replies

TEXT='[Verse]\nСпасибо, Ольга, за любовь и свет!\n[Chorus]\nТеплее мамы в мире нет!'


def data():
    return {'cartoon_theme':'theme_mother','chosen_song':'audio_fun_mother','chosen_song_phrase':'Ольга; спасибо',
        'song_review_details':'Ольга; спасибо','song_review_approved':True,'song_review_lyrics':TEXT,
        'song_review_tags':'pop, female solo vocals','song_review_title':'Маме',
        'song_review_voice':'female','chosen_avatar':'https://cdn.example/mom.png'}


async def prepare(d,generator=None,waiter=None):
    gen=generator or AsyncMock(side_effect=['warm-job','happy-job'])
    wait=waiter or AsyncMock(side_effect=lambda job:'https://cdn.example/'+job+'.mp3')
    await variants.prepare(d,generator=gen,waiter=wait,checkpoint=AsyncMock())
    return gen,wait


async def test_same_approved_words_and_voice_two_styles_and_no_duplicate_generation():
    d=data();gen,wait=await prepare(d)
    assert gen.await_count==2
    assert all(c.args[0]==TEXT and c.args[2]=='Маме' and 'female solo vocals' in c.args[1] for c in gen.await_args_list)
    assert 'relaxed flowing' in gen.await_args_list[0].args[1]
    assert 'slightly more cheerful' in gen.await_args_list[1].args[1]
    assert d['song_1_url']!=d['song_2_url']
    restored=json.loads(json.dumps(d));resubmit=AsyncMock();repoll=AsyncMock()
    await prepare(restored,resubmit,repoll)
    resubmit.assert_not_awaited();repoll.assert_not_awaited()
    assert restored['chosen_avatar']==d['chosen_avatar']


async def test_known_job_timeout_resumes_without_repeating_post():
    d=data();gen=AsyncMock(return_value='warm-job')
    with pytest.raises(TimeoutError):await prepare(d,gen,AsyncMock(side_effect=TimeoutError()))
    restored=json.loads(json.dumps(d));new=AsyncMock(return_value='happy-job')
    await prepare(restored,new)
    new.assert_awaited_once()
    assert restored['mother_song_variants']['items']['1']['job_id']=='warm-job'


async def test_unknown_second_post_preserves_first_song_and_never_posts_again():
    d=data();gen=AsyncMock(side_effect=['warm-job',TimeoutError()])
    with pytest.raises(variants.UnknownSongSubmission):await prepare(d,gen)
    assert d['song_1_url']=='https://cdn.example/warm-job.mp3'
    retry=AsyncMock()
    with pytest.raises(variants.UnknownSongSubmission):await prepare(json.loads(json.dumps(d)),retry)
    retry.assert_not_awaited()


async def test_cancelled_post_does_not_create_another_charge():
    d=data()
    with pytest.raises(asyncio.CancelledError):await prepare(d,AsyncMock(side_effect=asyncio.CancelledError()))
    retry=AsyncMock()
    with pytest.raises(variants.UnknownSongSubmission):await prepare(json.loads(json.dumps(d)),retry)
    retry.assert_not_awaited()


async def test_legacy_first_song_is_kept_and_only_cheerful_arrangement_is_created():
    d=data();d.update(song_1_url='https://cdn.example/old-full.mp3',video_song_lyrics=TEXT,
        video_song_source=['audio_fun_mother','Ольга; спасибо'])
    gen=AsyncMock(return_value='happy-job');await prepare(d,gen)
    gen.assert_awaited_once();assert 'slightly more cheerful' in gen.await_args.args[1]
    assert d['song_1_url']=='https://cdn.example/old-full.mp3'


async def test_legacy_unknown_submission_is_not_silently_replaced():
    d=data();d.update(video_song_lyrics=TEXT,video_song_source=None,
        video_song_job_source=['audio_fun_mother','Ольга; спасибо'],
        video_song_submitting_source=['audio_fun_mother','Ольга; спасибо'])
    gen=AsyncMock()
    with pytest.raises(variants.UnknownSongSubmission):await prepare(d,gen)
    gen.assert_not_awaited()


async def test_changed_words_archive_variants_without_losing_image():
    d=data();await prepare(d)
    old=json.loads(json.dumps(d['mother_song_variants']))
    d['song_review_lyrics']=TEXT.replace('Ольга','Маша')
    await prepare(d)
    assert d['mother_song_variants_archive']==[old]
    assert d['chosen_avatar']=='https://cdn.example/mom.png'


async def test_unapproved_text_never_calls_music_provider():
    d=data();d['song_review_approved']=False;gen=AsyncMock()
    with pytest.raises(ValueError):await prepare(d,gen)
    gen.assert_not_awaited()


async def test_retry_callback_cannot_bypass_payment(credit_db):
    key=await database.create_cartoon_order('cartoon_unpaid_variants',456,'theme_mother','mother')
    d=data();d.update(cartoon_order_id=key,cartoon_session_id='session',cartoon_flow='mother',cartoon_preview_token='token')
    ctx=await ready_context();await ctx.set_data(d);await ctx.set_state(handlers.PosterStates.waiting_for_cartoon_music_review)
    gen=AsyncMock()
    router=handlers.create_router(admin_user_id=999,song_generator=gen)
    await dispatch_callback(router,callback_event('mother_songs_retry:token'),ctx)
    gen.assert_not_awaited()


async def test_style_statistics_are_idempotent_and_separate_admin(credit_db):
    from mother_stats import record,text,sales
    assert await record(456,'mother_song_pick_2','paid-one:settings')
    assert not await record(456,'mother_song_pick_2','paid-one:settings')
    await record(999,'mother_song_pick_1','admin-test:settings')
    all_c=await database.get_all_counters()
    # Same filtering supplied by /stats to all Mother's Day events.
    client=lambda key:all_c.get(key,0)-(1 if key=='mother_song_pick_1' else 0)
    report=text(client,all_c,await sales(999))
    assert 'спокойное 0, чуть веселее 1' in report and 'спокойное исполнение: 1' in report


@pytest.mark.parametrize('unknown_post',[False,True])
async def test_available_first_song_can_be_selected_when_second_is_not_ready(credit_db,monkeypatch,unknown_post):
    from maxapi.types import AttachmentUpload
    key=await database.create_cartoon_order('cartoon_partial',456,'theme_mother','mother')
    await database.mark_cartoon_paid(key,'partial-payment')
    d=data();d.update(cartoon_order_id=key,cartoon_session_id='partial-session',cartoon_flow='mother',
        cartoon_preview_token='before',song_review_user=456,original_image_url=d['chosen_avatar'])
    ctx=await ready_context();await ctx.set_data(d);await ctx.set_state(handlers.PosterStates.waiting_for_cartoon_music_review)
    gen=AsyncMock(side_effect=['warm-job',TimeoutError() if unknown_post else 'happy-job'])
    waiter=AsyncMock(side_effect=['https://cdn.example/warm.mp3',TimeoutError()])
    video=AsyncMock(return_value='https://cdn.example/final.mp4')
    monkeypatch.setattr(handlers,'upload_audio_for_max',AsyncMock(return_value=AttachmentUpload(type='audio',payload={'token':'audio'})))
    router=handlers.create_router(admin_user_id=999,song_generator=gen,song_waiter=waiter,cartoon_video_generator=video)
    await dispatch_callback(router,callback_event('mother_songs_retry:before'),ctx)
    video.assert_not_awaited()
    restored=await ctx.get_data()
    assert restored['song_1_url']=='https://cdn.example/warm.mp3'
    if not unknown_post:assert restored['mother_song_variants']['items']['2']['job_id']=='happy-job'
    token=restored['cartoon_preview_token']
    for _ in range(2):await dispatch_callback(router,callback_event('cartoon_song_pick:1:'+token),ctx)
    assert (await database.get_all_counters())['mother_song_pick_1']==1
    await dispatch_callback(router,callback_event('confirm_video_2'),ctx)
    video.assert_awaited_once_with('https://cdn.example/mom.png','https://cdn.example/warm.mp3')
    assert (await database.get_cartoon_order(key))['status']=='completed' and gen.await_count==2

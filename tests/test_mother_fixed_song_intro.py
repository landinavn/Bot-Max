"""Fixed 13 s recording rule for both Mother cartoons; no real ASR/music/image/video requests."""
import copy
import json
from pathlib import Path
from unittest.mock import AsyncMock

import pytest

import database
import handlers
import media_service
import mother_recovery
import mother_song_timing as timing
import mother_story
import mother_storyboard as board
import mother_storyboard_legacy_v2 as previous
import song_alignment
from test_mothers_day import renderer

MASTER='https://cdn.example/mother.png'
AUDIO='https://cdn.example/approved-song.mp3'
LYRICS='В окне родного дома свет всё так же льётся,'


def stalled(theme):
    source=song_alignment.alignment_source(AUDIO,LYRICS)
    return {'cartoon_theme':theme,'chosen_avatar':MASTER,'original_image_url':MASTER,
        'selected_song_variant':1,'song_1_url':AUDIO,'song_review_lyrics':LYRICS,'cartoon_video_failed':True,
        'cartoon_kling_story':{'source':[previous.BIRTHDAY_POLICY if theme=='theme_mother_bday' else previous.POLICY,MASTER],
            'shot_seconds':10,'shots':[{} for _ in range(6)],
            'alignment_attempts':2,'alignment_attempt_source':source,
            'alignment_error':{'source':source,'type':'lyrics_mismatch'},
            'alignment_transcript':{'audio_url':AUDIO,'words':[{'word':'Он греет даже','start':4.9}]}}}


@pytest.mark.parametrize('theme',['theme_mother','theme_mother_bday'])
async def test_both_new_cartoon_products_use_13_without_recognizer(renderer,monkeypatch,theme):
    detector=AsyncMock(side_effect=AssertionError('ASR must not run'))
    monkeypatch.setattr(song_alignment,'detect_song_start',detector)
    data={'cartoon_theme':theme,'chosen_avatar':MASTER,'song_review_lyrics':LYRICS}
    images,submit,poll=renderer
    await mother_story.generate(data,MASTER,AUDIO,checkpoint=AsyncMock(),progress=AsyncMock())
    saved=data['cartoon_kling_story']
    assert saved['audio_start']==13. and saved['alignment_method']==timing.FIXED_INTRO_METHOD
    assert saved['alignment_source']==song_alignment.alignment_source(AUDIO,LYRICS)
    assert images.await_count==submit.await_count==6
    assert all(call.args[0]==MASTER and call.kwargs=={'image_size':'1536x1024'} for call in images.await_args_list)
    detector.assert_not_awaited()


@pytest.mark.parametrize('theme',['theme_mother','theme_mother_bday'])
async def test_cached_bad_transcript_and_exhausted_budget_do_not_block_old_unstarted_order(renderer,monkeypatch,theme):
    data=stalled(theme);old=copy.deepcopy(data['cartoon_kling_story'])
    detector=AsyncMock(side_effect=AssertionError('Do not repeat ASR'))
    monkeypatch.setattr(song_alignment,'detect_song_start',detector)
    await mother_story.generate(data,MASTER,AUDIO,checkpoint=AsyncMock(),progress=AsyncMock())
    saved=data['cartoon_kling_story']
    assert saved['source'][0]==board.storyboard(theme=='theme_mother_bday')[0]
    assert saved['audio_start']==13. and not saved['alignment_error'] and not saved['alignment_submitting']
    assert saved['alignment_transcript']==old['alignment_transcript'] and saved['alignment_attempts']==2
    detector.assert_not_awaited()


@pytest.mark.parametrize('theme',['theme_mother','theme_mother_bday'])
async def test_saved_manual_mark_for_exact_audio_is_not_overwritten(renderer,monkeypatch,theme):
    data=stalled(theme);s=data['cartoon_kling_story']
    s.update(alignment_source=song_alignment.alignment_source(AUDIO,LYRICS),audio_start=7.25,alignment_method='admin')
    async def render(clips,song,target,duration,**kwargs):
        assert duration==60. and kwargs['audio_start']==7.25;target.write_bytes(b'mp4')
    monkeypatch.setattr(mother_story,'render_local',AsyncMock(side_effect=render))
    detector=AsyncMock(side_effect=AssertionError('ASR must not run'));monkeypatch.setattr(song_alignment,'detect_song_start',detector)
    await mother_story.generate(data,MASTER,AUDIO,checkpoint=AsyncMock(),progress=AsyncMock())
    assert s['audio_start']==7.25 and s['alignment_method']=='admin'
    detector.assert_not_awaited()


@pytest.mark.parametrize('theme',['theme_mother','theme_mother_bday'])
async def test_short_audio_is_rejected_before_any_visual_generation(renderer,monkeypatch,theme):
    monkeypatch.setattr(media_service,'audio_duration',AsyncMock(return_value=72.9))
    data={'cartoon_theme':theme,'chosen_avatar':MASTER,'song_review_lyrics':LYRICS}
    with pytest.raises(ValueError,match='меньше минуты'):
        await mother_story.generate(data,MASTER,AUDIO,checkpoint=AsyncMock(),progress=AsyncMock())
    images,submit,poll=renderer;images.assert_not_awaited();submit.assert_not_awaited()


@pytest.mark.parametrize('theme',['theme_mother','theme_mother_bday'])
async def test_previously_blocked_paid_orders_resume_with_same_media_and_payment(credit_db,theme):
    tariff='mother_bday' if theme=='theme_mother_bday' else 'mother'
    key=await database.create_cartoon_order('cartoon_fixed_'+theme,123,theme,tariff)
    await database.mark_cartoon_paid(key,'synthetic-'+theme)
    data=stalled(theme);data['cartoon_order_id']=key
    await database.update_cartoon_order(key,fsm_data=json.dumps(data),fsm_state=str(handlers.PosterStates.cartoon_manual_review))
    await database.finish_cartoon_order(key,123,manual=True)
    old=await database.get_cartoon_order(key)
    assert await mother_recovery.recover_order(old,handlers.PosterStates.waiting_for_cartoon_song_phrase)
    new=await database.get_cartoon_order(key);saved=json.loads(new['fsm_data'])
    assert new['status']=='paid' and new['provider_payment_id']==old['provider_payment_id'] and new['amount_kopecks']==old['amount_kopecks']
    assert saved['song_1_url']==AUDIO and saved['chosen_avatar']==MASTER
    assert saved['cartoon_kling_story']['audio_start']==13. and not saved['cartoon_video_failed']


@pytest.mark.parametrize('kind',['unknown_image','known_image','unknown_video','known_video','result','other_theme','legacy_five_seconds'])
def test_automatic_fixed_recovery_does_not_clear_unrelated_or_paid_visual_work(kind):
    data=stalled('theme_mother');story=data['cartoon_kling_story']
    if kind=='unknown_image':story['shots'][0]={'image_submitting':True}
    if kind=='known_image':story['shots'][0]={'image_url':'https://cdn.example/paid.png'}
    if kind=='unknown_video':story['shots'][0]={'video_submitting':True}
    if kind=='known_video':story['shots'][0]={'task_id':'paid-existing'}
    if kind=='result':story['result_url']='https://cdn.example/result.mp4'
    if kind=='other_theme':data['cartoon_theme']='theme_ny'
    if kind=='legacy_five_seconds':story['shot_seconds']=5
    before=copy.deepcopy(data)
    assert not timing.recoverable_fixed_intro(data)
    assert data==before


async def test_non_mother_product_does_not_get_a_13_second_default(tmp_path,monkeypatch):
    assert timing.fixed_intro_for({'cartoon_theme':'theme_ny'}) is None
    detector=AsyncMock(return_value=3.25);monkeypatch.setattr(song_alignment,'detect_song_start',detector)
    result=await song_alignment.resolve_song_start({},Path('song.mp3'),LYRICS,AUDIO,tmp_path,AsyncMock())
    assert result==3.25;detector.assert_awaited_once()


def test_admin_can_override_the_fixed_rule_without_switching_song():
    data=stalled('theme_mother')
    song_alignment.apply_fixed_start(data['cartoon_kling_story'],LYRICS,AUDIO,13.)
    mother_recovery.manual_start(data,12.5,variant=1)
    assert data['cartoon_kling_story']['audio_start']==12.5
    assert data['cartoon_kling_story']['alignment_method']=='admin' and data['song_1_url']==AUDIO


@pytest.mark.parametrize('theme',['theme_mother','theme_mother_bday'])
async def test_already_paid_visuals_keep_policy_and_provider_id(renderer,theme):
    data=stalled(theme);s=data['cartoon_kling_story'];old_policy=s['source'][0]
    s['shots'][0]={'image_url':'https://cdn.example/paid-first.png','task_id':'existing-paid-task',
                   'video_url':'https://cdn.example/paid-first.mp4','end_frame_url':'https://cdn.example/paid-end.png'}
    await mother_story.generate(data,MASTER,AUDIO,checkpoint=AsyncMock(),progress=AsyncMock())
    images,submit,poll=renderer
    assert s['source'][0]==old_policy and s['shots'][0]['task_id']=='existing-paid-task'
    assert images.await_count==submit.await_count==5


def test_changed_recording_does_not_inherit_manual_mark_from_another_variant():
    story={'alignment_source':song_alignment.alignment_source(AUDIO,LYRICS),
           'alignment_method':'admin','audio_start':7.25}
    other='https://cdn.example/other-performance.mp3'
    assert song_alignment.apply_fixed_start(story,LYRICS,other,13.)==13.
    assert story['alignment_source']==song_alignment.alignment_source(other,LYRICS)
    assert story['alignment_method']==timing.FIXED_INTRO_METHOD


@pytest.mark.parametrize('theme',['theme_mother','theme_mother_bday'])
async def test_delivered_result_is_returned_without_new_provider_calls(renderer,monkeypatch,theme):
    data=stalled(theme);story=data['cartoon_kling_story']
    story.update(result_url='https://cdn.example/delivered.mp4',result_audio=AUDIO)
    detector=AsyncMock(side_effect=AssertionError('No ASR for delivered result'))
    monkeypatch.setattr(song_alignment,'detect_song_start',detector)
    assert await mother_story.generate(data,MASTER,AUDIO,checkpoint=AsyncMock(),progress=AsyncMock())=='https://cdn.example/delivered.mp4'
    images,submit,poll=renderer
    images.assert_not_awaited();submit.assert_not_awaited();poll.assert_not_awaited();detector.assert_not_awaited()


@pytest.mark.parametrize('method,old_start,new_start',[('transcript',2.,13.),('admin',7.25,7.25)])
def test_visual_revision_plan_and_actual_render_share_the_same_intro_rule(method,old_start,new_start):
    from mother_story_revision import prepare_revision
    from test_mother_character_consistency import completed
    data=completed();data['cartoon_kling_story'].update(alignment_method=method,audio_start=old_start)
    result=prepare_revision(data)
    assert result['cartoon_kling_story']['audio_start']==new_start
    assert result['cartoon_kling_story_archive'][-1]['cartoon_kling_story']['audio_start']==old_start
    assert result['song_1_url']==data['song_1_url']

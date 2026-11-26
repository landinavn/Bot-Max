"""Synthetic reference images/IDs only; no provider requests or real client records."""
import copy
import json
import sqlite3
from pathlib import Path
from unittest.mock import AsyncMock

import pytest

import mother_story as story
import mother_storyboard as current
import mother_storyboard_legacy_v2 as previous
from mother_story_revision import prepare_revision, prepare_order
from song_alignment import alignment_source
from test_mothers_day import renderer

MASTER='https://cdn.example/approved-mother.png'
AUDIO='https://cdn.example/song.mp3'
LYRICS='В окне родного дома свет всё так же льётся,'


def completed():
    return {'cartoon_order_id':'cartoon-synthetic','cartoon_theme':'theme_mother',
        'chosen_avatar':MASTER,'original_image_url':MASTER,'song_1_url':AUDIO,'video_song_url':AUDIO,
        'selected_song_variant':1,'song_attempt':1,'song_review_lyrics':LYRICS,'cartoon_auto_pipeline':True,
        'video_merged_url':'https://cdn.example/old-final.mp4',
        'cartoon_kling_story':{'source':[previous.POLICY,MASTER],'shot_seconds':10,
            'alignment_source':alignment_source(AUDIO,LYRICS),'audio_start':13.,'alignment_method':'admin',
            'result_url':'https://cdn.example/old-final.mp4','result_audio':AUDIO,
            'shots':[{'image_url':f'https://cdn.example/old-{i}.png','task_id':f'paid-old-{i}',
                'video_url':f'https://cdn.example/old-{i}.mp4','image_submitting':True,'video_submitting':True} for i in range(6)]}}


@pytest.mark.parametrize('birthday',[False,True])
def test_new_prompts_preserve_master_style_and_do_not_impose_grandmother_traits(birthday):
    policy,shots,base=current.storyboard(birthday)
    assert policy not in (previous.POLICY,previous.BIRTHDAY_POLICY)
    for i in range(6):
        for prompt in (current.image_prompt(i,birthday=birthday),current.video_prompt(i,birthday=birthday)):
            assert 'APPROVED character master' in prompt and 'ONLY visual identity authority' in prompt
            assert 'cream cardigan' not in prompt and 'pearl earrings' not in prompt and 'grey or natural hair' not in prompt
            assert 'exact hair color' in prompt and 'realism and facial proportions' in prompt
    assert len(shots)==6
    assert 'birthday cake' in current.image_prompt(2,birthday=True)


@pytest.mark.parametrize('birthday',[False,True])
async def test_old_six_shot_order_keeps_previous_policy_prompts_and_references(renderer,birthday):
    images,submit,poll=renderer;policy,_,_=previous.storyboard(birthday)
    data={'cartoon_theme':'theme_mother_bday' if birthday else 'theme_mother',
          'cartoon_kling_story':{'source':[policy,MASTER],'shot_seconds':10,'shots':[{} for _ in range(6)]}}
    await story.generate(data,MASTER,AUDIO,checkpoint=AsyncMock(),progress=AsyncMock())
    assert data['cartoon_kling_story']['source']==[policy,MASTER]
    assert images.await_args_list[0].args[1]==previous.image_prompt(0,birthday=birthday)
    assert images.await_args_list[1].kwargs['style_reference_urls']==('https://cdn.example/image-0.png','https://cdn.example/end-0.png')
    assert submit.await_count==6


async def test_new_story_rejects_conflicting_approved_master_before_provider_calls(renderer):
    images,submit,poll=renderer
    with pytest.raises(ValueError,match='утверждённый'):
        await story.generate({'chosen_avatar':MASTER},'https://cdn.example/wrong-person.png',AUDIO,checkpoint=AsyncMock(),progress=AsyncMock())
    images.assert_not_awaited();submit.assert_not_awaited()


def test_revision_archives_all_paid_assets_without_mutating_original_or_music():
    data=completed();before=copy.deepcopy(data)
    revised=prepare_revision(data)
    assert data==before
    for key in ('chosen_avatar','original_image_url','song_1_url','video_song_url','song_review_lyrics','selected_song_variant','cartoon_order_id'):
        assert revised[key]==before[key]
    assert 'video_merged_url' not in revised
    new=revised['cartoon_kling_story']
    assert new['source']==[current.POLICY,MASTER] and new['audio_start']==13
    assert new['alignment_source']==before['cartoon_kling_story']['alignment_source']
    assert new['shots']==[{} for _ in range(6)]
    archived=revised['cartoon_kling_story_archive'][-1]
    assert archived['cartoon_kling_story']==before['cartoon_kling_story']
    assert archived['video_merged_url']==before['video_merged_url']


@pytest.mark.parametrize('change',['unknown','wrong_audio','unapproved_master','no_result','invalid_start'])
def test_revision_rejects_unsafe_or_unrelated_changes(change):
    data=completed()
    if change=='unknown':data['cartoon_kling_story']['shots'][0].pop('task_id')
    if change=='wrong_audio':data['song_1_url']='https://cdn.example/another.mp3'
    if change=='unapproved_master':data['original_image_url']='https://cdn.example/wrong.png'
    if change=='no_result':data.pop('video_merged_url');data['cartoon_kling_story'].pop('result_url')
    if change=='invalid_start':data['cartoon_kling_story']['audio_start']=float('nan')
    with pytest.raises(ValueError):prepare_revision(data)


def test_cli_plan_readonly_prepare_backed_up_and_repeat_does_not_lose_old_ids(tmp_path):
    data=completed();path=tmp_path/'synthetic.sqlite3'
    with sqlite3.connect(path) as db:
        db.execute('CREATE TABLE cartoon_orders(order_id TEXT PRIMARY KEY,user_id INTEGER,status TEXT,paid_at TEXT,fsm_data TEXT,fsm_state TEXT,amount_kopecks INTEGER)')
        db.execute('INSERT INTO cartoon_orders VALUES(?,?,?,?,?,?,?)',('cartoon-synthetic',123,'completed','synthetic-paid',json.dumps(data),'done',59000))
    before=path.read_bytes();plan=prepare_order(path,'cartoon-synthetic')
    assert path.read_bytes()==before and not plan['prepared']
    result=prepare_order(path,'cartoon-synthetic',apply=True)
    assert result['prepared'] and Path(result['backup']).stat().st_mode&0o777==0o600
    with sqlite3.connect(path) as db:
        row=db.execute('SELECT status,paid_at,fsm_data,amount_kopecks FROM cartoon_orders').fetchone()
        revised=json.loads(row[2]);assert row[0]=='manual' and row[1]=='synthetic-paid' and row[3]==59000
    with sqlite3.connect(result['backup']) as db:
        assert json.loads(db.execute('SELECT fsm_data FROM cartoon_orders').fetchone()[0])==data
    with pytest.raises(ValueError):prepare_order(path,'cartoon-synthetic',apply=True)


async def test_repaired_order_keeps_13_seconds_and_cached_song_without_new_alignment(renderer,monkeypatch):
    data=prepare_revision(completed());images,submit,poll=renderer
    import media_service
    async def render(clips,audio,target,duration,**kwargs):
        assert kwargs['audio_start']==13. and duration==60.;target.write_bytes(b'mp4')
    monkeypatch.setattr(story,'render_local',AsyncMock(side_effect=render))
    detector=AsyncMock(side_effect=AssertionError('Paid alignment must not run again'))
    monkeypatch.setattr('song_alignment.detect_song_start',detector)
    await story.generate(data,MASTER,AUDIO,checkpoint=AsyncMock(),progress=AsyncMock())
    detector.assert_not_awaited()
    assert images.await_count==submit.await_count==6
    assert all(call.args[0]==MASTER and call.kwargs=={'image_size':'1536x1024'} for call in images.await_args_list)
    assert all(call.args[2]==data['cartoon_kling_story']['shots'][i]['video_prompt'] for i,call in enumerate(submit.await_args_list))


def test_concurrent_order_change_is_not_overwritten_during_revision(tmp_path,monkeypatch):
    import mother_story_revision as revision
    path=tmp_path/'concurrent.sqlite3';data=completed();real_connect=sqlite3.connect
    with real_connect(path) as db:
        db.execute('CREATE TABLE cartoon_orders(order_id TEXT PRIMARY KEY,user_id INTEGER,status TEXT,paid_at TEXT,fsm_data TEXT,fsm_state TEXT)')
        db.execute('INSERT INTO cartoon_orders VALUES(?,?,?,?,?,?)',('cartoon-synthetic',123,'completed','paid',json.dumps(data),'done'))
    changed={**data,'parallel_change':'retain-this'}
    class Concurrent(sqlite3.Connection):
        def backup(self,*args,**kwargs):
            result=super().backup(*args,**kwargs)
            with real_connect(path) as other:
                other.execute('UPDATE cartoon_orders SET fsm_data=?',(json.dumps(changed),))
            return result
    monkeypatch.setattr(revision.sqlite3,'connect',lambda *a,**k: real_connect(*a,**{**k,'factory':Concurrent}))
    with pytest.raises(ValueError,match='параллельно'):revision.prepare_order(path,'cartoon-synthetic',apply=True)
    with real_connect(path) as db:
        row=db.execute('SELECT status,fsm_data FROM cartoon_orders').fetchone()
    assert row[0]=='completed' and json.loads(row[1])==changed

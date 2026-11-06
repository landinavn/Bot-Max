import hashlib
from unittest.mock import AsyncMock

import pytest

from multstudio import repository as repo
from multstudio.render import Renderer, process, probe, duration
from multstudio.router import create_router
from multstudio.seed import seed
from test_multstudio import studio, ready_scene
from test_multstudio_av_sync import clip, tone, samples, rms, scene
from test_multstudio_project_menu import event
from test_handlers import dispatch_callback, ready_context


@pytest.mark.parametrize('old_schema',[True,False])
async def test_one_time_upgrade_sets_requested_timings_and_preserves_media_identity(studio,old_schema):
    project,_,_=studio
    _,episode=await ready_scene(studio)
    shots=await repo.children('scene',episode['id'])
    before=[await repo.snapshot('scene',s['id']) for s in shots]
    if old_schema:
        # Production schema/settings from PR #190: no trimming column yet.
        async with repo.connect() as db:
            await db.execute('DELETE FROM cartoon_editing_migrations')
            await db.execute('DROP TABLE cartoon_scene_editing')
            await db.execute('CREATE TABLE cartoon_scene_editing (scene_id TEXT PRIMARY KEY REFERENCES cartoon_scenes(id), speech_start REAL NOT NULL)')
            await db.execute('INSERT INTO cartoon_scene_editing VALUES (?,3)',(shots[0]['id'],))
            await db.execute('INSERT INTO cartoon_scene_editing VALUES (?,2)',(shots[2]['id'],))
            await db.commit()
    else:
        # PR #191 already applied; keep its 4-second trim and update only speech.
        async with repo.connect() as db:
            await db.execute("DELETE FROM cartoon_editing_migrations WHERE name IN ('anya-s1-e1-no-intro-20261107-v3','anya-s1-e1-delay-scenes-2-3-20261107-v4','anya-s1-e1-delay-scene-3-20261107-v5')")
            await db.commit()
        await repo.set_scene_speech_start(shots[0]['id'],456,1)
        await repo.set_scene_speech_start(shots[2]['id'],456,2)
        await repo.set_scene_speech_start(shots[3]['id'],456,0.5)
    await repo.init()
    expected={1:(0,0),2:(3,4),3:(5,0),4:(0,0),5:(1,0),6:(0,0),7:(0,0)}
    for shot in shots:
        settings=await repo.scene_editing(shot['id'])
        assert (settings['speech_start'],settings['video_start'])==expected[shot['number']]
    assert before==[await repo.snapshot('scene',s['id']) for s in shots]
    await repo.set_scene_speech_start(shots[0]['id'],456,1.5)
    await repo.set_scene_video_start(shots[1]['id'],456,3.5)
    await repo.init()
    await seed(456)
    assert (await repo.scene_editing(shots[0]['id']))['speech_start']==1.5
    assert (await repo.scene_editing(shots[1]['id']))['video_start']==3.5
    async with repo.connect() as db:
        assert len(await repo.rows(db,'SELECT * FROM cartoon_editing_migrations'))==4
    assert await repo.claim() is None


async def test_independent_timing_changes_preserve_the_other_setting_and_context(studio):
    _,_,_=studio
    shot,_=await ready_scene(studio)
    before=await repo.snapshot('scene',shot['id'])
    await repo.set_scene_video_start(shot['id'],456,'1,25')
    await repo.set_scene_speech_start(shot['id'],456,0.75)
    assert await repo.scene_editing(shot['id'])==dict(speech_start=0.75,video_start=1.25)
    await repo.set_scene_video_start(shot['id'],456,0.5)
    assert await repo.scene_editing(shot['id'])==dict(speech_start=0.75,video_start=0.5)
    assert before==await repo.snapshot('scene',shot['id'])
    with pytest.raises(ValueError,match='доступа'):
        await repo.set_scene_video_start(shot['id'],999,2)


async def test_real_four_second_video_cut_skips_early_frames_and_keeps_speech(studio):
    _,_,assets=studio; assets.root.mkdir(parents=True,exist_ok=True)
    # Red is the unwanted four-second introduction; blue is Dima's speaking shot.
    await process('ffmpeg','-nostdin','-v','error','-y','-f','lavfi','-i','color=c=red:s=64x96:r=30:d=4',
        '-f','lavfi','-i','color=c=blue:s=64x96:r=30:d=1',
        '-filter_complex','[0:v][1:v]concat=n=2:v=1:a=0[v]','-map','[v]',
        '-c:v','libx264','-threads','1',assets.path('dima.mp4'))
    await tone(assets.path('dima.m4a'),1.4)
    shot=scene('dima.mp4','dima.m4a',5)
    shot['editing']['video_start']=4
    checksum=hashlib.sha256(assets.path('dima.mp4').read_bytes()).hexdigest()
    result=await Renderer(assets).scene(shot)
    assert 1.55<=await duration(result)<=1.7
    pixel=await process('ffmpeg','-nostdin','-v','error','-ss','0.05','-i',result,
        '-frames:v','1','-vf','scale=1:1','-f','rawvideo','-pix_fmt','rgb24','-')
    assert pixel[2]>pixel[0]+100
    sound=await samples(result)
    assert rms(sound,0.04,0.2)>0.04 and rms(sound,1.15,1.3)>0.04
    assert hashlib.sha256(assets.path('dima.mp4').read_bytes()).hexdigest()==checksum
    info=await probe(result)
    assert all(abs(float(s['start_time']))<0.001 for s in info['streams'])


async def test_cut_past_end_is_rejected_without_touching_source(studio):
    _,_,assets=studio; assets.root.mkdir(parents=True,exist_ok=True)
    await clip(assets.path('short.mp4'),0.5)
    await tone(assets.path('voice.m4a'),0.2)
    shot=scene('short.mp4','voice.m4a',0.5)
    shot['editing']['video_start']=4
    before=assets.path('short.mp4').read_bytes()
    with pytest.raises(ValueError,match='за концом'):
        await Renderer(assets).scene(shot)
    assert assets.path('short.mp4').read_bytes()==before


async def test_other_projects_and_later_episodes_have_no_preset_cut(studio):
    project,_,_=studio
    episode=await repo.create('episode',{},project_id=project,title='Другая серия',number=2)
    shot=await repo.create('scene',{},episode_id=episode,number=5)
    assert await repo.scene_editing(shot)==dict(speech_start=0,video_start=0)
    project=await repo.create('project',{},owner_id=456,title='Другой проект',season=1)
    episode=await repo.create('episode',{},project_id=project,title='Другая серия',number=1)
    shot=await repo.create('scene',{},episode_id=episode,number=2)
    await repo.init()
    assert await repo.scene_editing(shot)==dict(speech_start=0,video_start=0)


async def test_trim_editor_menu_uses_separate_input_state(studio):
    project,settings,assets=studio
    _,episode=await ready_scene(studio)
    shot=(await repo.children('scene',episode['id']))[1]
    router=create_router(456,settings,assets)
    pressed=event('studio:s:'+shot['id'])
    await dispatch_callback(router,pressed,await ready_context())
    assert 'Начало исходного видео: 4 с' in pressed.bot.send_message.await_args.kwargs['text']
    context=await ready_context(); pressed=event('studio:video_start:'+shot['id'])
    await dispatch_callback(router,pressed,context)
    assert 'с 4 секунды' in pressed.bot.send_message.await_args.kwargs['text']
    assert (await context.get_data())['studio_input']['action']=='video_start'
    await dispatch_callback(router,event('studio:home'),context)
    assert await context.get_state() is None
    assert await repo.claim() is None


async def test_update_after_no_intro_keeps_cuts_and_other_scenes_and_runs_once(studio):
    _,_,_=studio
    _,episode=await ready_scene(studio)
    shots=await repo.children('scene',episode['id'])
    before=[await repo.snapshot('scene',s['id']) for s in shots]
    async with repo.connect() as db:
        await db.execute("DELETE FROM cartoon_editing_migrations WHERE name IN ('anya-s1-e1-delay-scenes-2-3-20261107-v4','anya-s1-e1-delay-scene-3-20261107-v5')")
        await db.commit()
    await repo.set_scene_speech_start(shots[1]['id'],456,0)
    await repo.set_scene_speech_start(shots[2]['id'],456,0)
    await repo.set_scene_video_start(shots[2]['id'],456,0.5)
    others={s['id']:await repo.scene_editing(s['id']) for s in shots if s['number'] not in (2,3)}
    await repo.init()
    assert await repo.scene_editing(shots[1]['id'])==dict(speech_start=3,video_start=4)
    assert await repo.scene_editing(shots[2]['id'])==dict(speech_start=5,video_start=0.5)
    assert others=={s['id']:await repo.scene_editing(s['id']) for s in shots if s['number'] not in (2,3)}
    assert before==[await repo.snapshot('scene',s['id']) for s in shots]
    await repo.set_scene_speech_start(shots[1]['id'],456,2.5)
    await repo.init()
    await seed(456)
    assert (await repo.scene_editing(shots[1]['id']))['speech_start']==2.5
    assert await repo.claim() is None


async def test_scene_three_moves_from_three_to_five_seconds_only_once(studio):
    _,_,_=studio
    _,episode=await ready_scene(studio)
    shots=await repo.children('scene',episode['id'])
    third=shots[2]
    before=[await repo.snapshot('scene',s['id']) for s in shots]
    async with repo.connect() as db:
        await db.execute("DELETE FROM cartoon_editing_migrations WHERE name='anya-s1-e1-delay-scene-3-20261107-v5'")
        await db.commit()
    await repo.set_scene_speech_start(third['id'],456,3)
    await repo.set_scene_video_start(third['id'],456,0.5)
    others={s['id']:await repo.scene_editing(s['id']) for s in shots if s['number']!=3}
    await repo.init()
    assert await repo.scene_editing(third['id'])==dict(speech_start=5,video_start=0.5)
    assert others=={s['id']:await repo.scene_editing(s['id']) for s in shots if s['number']!=3}
    assert before==[await repo.snapshot('scene',s['id']) for s in shots]
    await repo.set_scene_speech_start(third['id'],456,4.5)
    await repo.init()
    await seed(456)
    assert (await repo.scene_editing(third['id']))['speech_start']==4.5
    assert await repo.claim() is None

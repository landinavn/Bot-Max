import copy
import json
from dataclasses import replace
from types import SimpleNamespace
from unittest.mock import AsyncMock

import httpx
import pytest

from multstudio import repository as repo, rules
from multstudio.episode_two import prepare as second, prepare_jobs, speech_profiles, plan
from multstudio.episode_three import prepare, SCENES, TITLE
from multstudio.heygen_video import StudioHeyGen
from multstudio.render import Renderer, duration, process, probe
from multstudio.worker import Worker
from multstudio.seed import seed
from test_multstudio import studio, add_image


async def episode(studio):
    project,settings,assets=studio
    await second(project,456)
    key=await prepare(project,456)
    return key,await repo.children('scene',key)


async def ready(studio, number=1):
    key,scenes=await episode(studio)
    for char in await repo.children('character',studio[0]):
        await add_image(studio[2],await repo.snapshot('character',char['id']))
    # The apartment anchor is the final scene of episode two.
    anchor=scenes[0]['spec']['location_reference_scene_id']
    await add_image(studio[2],await repo.snapshot('scene',anchor))
    scene=scenes[number-1]
    snap=await repo.snapshot('scene',scene['id'])
    await add_image(studio[2],snap)
    return await repo.snapshot('scene',scene['id'])


async def attach_audio(studio,snap,seconds=3,profile_override=None):
    assets=studio[2];assets.root.mkdir(parents=True,exist_ok=True)
    path=assets.path('voice.mp3')
    await process('ffmpeg','-v','error','-y','-f','lavfi','-i',f'sine=frequency=440:duration={seconds}',path)
    snap['quote']={'speech_profiles':await speech_profiles(snap)}
    metadata=rules.speech_metadata(snap,await duration(path))
    if profile_override:metadata['voice_profile']=profile_override
    await repo.add_asset(snap['project_id'],'scene',snap['id'],'speech',snap['context_hash'],path.name,
                         assets.checksum(path),metadata=repo.encoded(metadata))
    return await repo.snapshot('scene',snap['id'])


async def test_ep3_idempotent_drafts_and_historical_media_unchanged(studio):
    project,settings,assets=studio
    await second(project,456)
    before=[await repo.snapshot('episode',e['id']) for e in await repo.children('episode',project)]
    chars=await repo.children('character',project)
    voices=[await repo.character_speech(c['id']) for c in chars]
    key,scenes=await episode(studio)
    await prepare_jobs(key,settings)
    assert await prepare(project,456)==key
    assert (await repo.get('episode',key))['title']==TITLE
    assert len(scenes)==9 and sum(s['spec']['duration'] for s in scenes)==45
    assert sum(bool(s['spec']['dialogue']) for s in scenes)==7
    assert all(rules.strict(s['spec']) for s in scenes)
    assert scenes[4]['spec']['new_location']
    assert scenes[0]['spec']['location_reference_scene_id']==scenes[5]['spec']['location_reference_scene_id']
    assert scenes[8]['spec']['characters']==[] and scenes[8]['spec']['speaker_id'] is None
    assert [s['spec']['dialogue'][0]['text'] if s['spec']['dialogue'] else '' for s in scenes]==[s[2] for s in SCENES]
    assert [await repo.snapshot('episode',x['id']) for x in before]==before
    assert await repo.children('character',project)==chars
    assert [await repo.character_speech(c['id']) for c in chars]==voices
    async with repo.connect() as db:
        jobs=await repo.rows(db,'SELECT * FROM cartoon_jobs WHERE target_id=? OR target_id IN (SELECT id FROM cartoon_scenes WHERE episode_id=?)',(key,key))
    assert len(jobs)==26 and all(j['status']=='draft' and j['approved_by'] is None for j in jobs)
    assert await repo.claim() is None
    budget=await plan(key,replace(settings,heygen_price=100000))
    assert budget['silent']==2 and budget['utterances']==7 and budget['videos']==9 and not budget['errors']
    assert 'heygen/avatar_iv' in budget['services']['video']['models']


async def test_new_dialogues_split_globally_and_cannot_remove_rules(studio):
    key,scenes=await episode(studio)
    cast=await repo.children('character',studio[0])
    lines=[dict(character_id=c['id'],text='Привет!') for c in cast]
    created=await repo.create_scene_sequence(dict(title='Диалог',visual='Квартира',characters=[c['id'] for c in cast],dialogue=lines,duration=9),episode_id=key,number=10)
    assert len(created)==3
    for scene_id,line in zip(created,lines):
        scene=await repo.get('scene',scene_id)
        assert scene['spec']['duration']==5 and scene['spec']['characters']==[line['character_id']]
        bad=dict(scene['spec']);bad.pop('production_rules')
        with pytest.raises(ValueError,match='удалить'):await repo.edit('scene',scene_id,bad)
    assert len(await repo.children('scene',key))==12


@pytest.mark.parametrize('seconds',[4.51,5,8,float('nan'),True,None])
def test_audio_gate(seconds):
    with pytest.raises(ValueError):rules.audio_seconds(seconds)


async def test_no_video_before_measured_audio_and_no_voice_substitution(studio):
    snap=await ready(studio)
    settings=replace(studio[1],heygen_price=100000)
    with pytest.raises(ValueError,match='озвучку'):await repo.quote('scene',snap['id'],'video',settings)
    snap=await attach_audio(studio,snap,seconds=4.8)
    with pytest.raises(ValueError,match='4,50'):await repo.quote('scene',snap['id'],'video',settings)
    snap=await attach_audio(studio,snap,seconds=3,profile_override={'voice':'other','speed':1})
    with pytest.raises(ValueError,match='Голос'):await repo.quote('scene',snap['id'],'video',settings)
    snap=await attach_audio(studio,snap,seconds=3)
    job=await repo.quote('scene',snap['id'],'video',settings)
    await repo.budget(studio[0],10000000)
    speaker=snap['spec']['speaker_id']
    profile=await repo.character_speech(speaker);profile['emotion']='strict' if profile['voice']=='masha' else 'evil'
    await repo.set_character_speech(speaker,456,profile)
    with pytest.raises(ValueError,match='голос'):await repo.confirm(job['id'],456)


async def test_heygen_contract_is_one_avatar_then_one_vertical_video():
    requests=[]
    def handler(request):
        requests.append(request)
        return httpx.Response(200,json={'data':{'avatar_item':{'id':'scene-look'}}} if request.url.path=='/v3/avatars' else {'data':{'video_id':'scene-video'}})
    adapter=StudioHeyGen('test-key',httpx.MockTransport(handler))
    assert await adapter.create_scene_avatar('https://example.org/frame.png','job-avatar')=='scene-look'
    assert await adapter.scene_video('scene-look','https://example.org/speech.mp3','job-video')=='scene-video'
    payload=json.loads(requests[1].content)
    assert payload['audio_url']=='https://example.org/speech.mp3' and payload['aspect_ratio']=='9:16'
    assert payload['engine']=={'type':'avatar_iv'} and 'voice_id' not in payload


async def test_avatar_phase_survives_restart_and_never_repeats_paid_post(studio):
    snap=await attach_audio(studio,await ready(studio))
    job=await repo.quote('scene',snap['id'],'video',replace(studio[1],heygen_price=100000))
    await repo.budget(studio[0],10000000);await repo.confirm(job['id'],456)
    adapter=SimpleNamespace(ready=lambda:None,create_scene_avatar=AsyncMock(return_value='look'),poll=AsyncMock(return_value={'avatar_id':'look'}),scene_video=AsyncMock(return_value='vid'))
    providers=SimpleNamespace(studio_heygen=adapter,preflight=lambda k:None)
    assets=studio[2];assets.asset_url=AsyncMock(return_value='https://example.org/media')
    worker=Worker(SimpleNamespace(),456,studio[1],providers,assets)
    await worker.execute(await repo.claim())
    assert (await repo.job(job['id']))['status']=='avatar_polling'
    await repo.update_job(job['id'],next_attempt=0)
    await worker.execute(await repo.claim())
    assert (await repo.job(job['id']))['status']=='video_preparing'
    worker=Worker(SimpleNamespace(),456,studio[1],providers,assets)
    await worker.execute(await repo.claim())
    assert (await repo.job(job['id']))['status']=='polling'
    adapter.create_scene_avatar.assert_awaited_once();adapter.scene_video.assert_awaited_once()
    assert (await repo.job(job['id']))['provider_id']=='vid'


async def make_clip(assets,seconds,audio_seconds=None,delay=0):
    from uuid import uuid4
    assets.root.mkdir(parents=True,exist_ok=True)
    path=assets.path(uuid4().hex+'.mp4')
    args=['ffmpeg','-v','error','-y','-f','lavfi','-i',f'color=c=blue:s=160x284:r=30:d={seconds}']
    if audio_seconds is not None:
        args+=['-f','lavfi','-i',f'sine=frequency=440:duration={audio_seconds}']
        args+=['-af',f'adelay={int(delay*1000)}:all=1,apad','-c:a','aac']
    await process(*args,'-t',str(seconds),'-c:v','libx264','-threads','1','-pix_fmt','yuv420p',path)
    return path


async def test_normalization_extends_only_silent_tail_and_rejects_truncated_voice(studio):
    snap=await attach_audio(studio,await ready(studio),seconds=3)
    snap['quote']={'speech_profiles':await speech_profiles(snap)}
    render=Renderer(studio[2])
    source=await make_clip(studio[2],4,audio_seconds=3)
    normalized=await render.normalize_video(snap,source)
    assert abs(await duration(normalized)-5)<.035
    source=await make_clip(studio[2],6,audio_seconds=5.6)
    with pytest.raises(ValueError,match='Обрезка слов запрещена'):await render.normalize_video(snap,source)
    source=await make_clip(studio[2],6,audio_seconds=3)
    assert abs(await duration(await render.normalize_video(snap,source))-5)<.035
    source=await make_clip(studio[2],5,audio_seconds=3,delay=.5)
    with pytest.raises(ValueError,match='сдвинул начало'):await render.normalize_video(snap,source)


async def test_strict_nine_scene_assembly_is_45s_and_ending_inside_last_block(studio):
    key,scenes=await episode(studio)
    source=await make_clip(studio[2],5)
    renderer=Renderer(studio[2])
    snaps=[]
    # Offline silent test shots exercise exact block length, SRT export and final card.
    for scene in scenes:
        spec=rules.normalize(dict(visual='Квартира',characters=[],dialogue=[],duration=5))
        snap=dict(target='scene',id=scene['id'],project_id=studio[0],number=scene['number'],context_hash='render-test',spec=spec,
                  assets={'video':{'kind':'video','path':source.name,'approved':1,'metadata':repo.encoded(dict(duration_seconds=5,image_id=None,speech_id=None))}},editing={})
        snaps.append(snap)
    result=await renderer.assemble(dict(spec={'ending':['Маленькие чудеса Ани','Продолжение следует…']},scenes=snaps,assets={}))
    assert abs(await duration(result)-45)<.035
    info=await probe(result)
    video=next(s for s in info['streams'] if s['codec_type']=='video')
    assert (video['width'],video['height'],video['r_frame_rate'])==(1080,1920,'30/1')
    assert next(s for s in info['streams'] if s['codec_type']=='audio')['codec_name']=='aac'
    assert any(studio[2].root.glob('*.srt'))


async def test_too_long_tts_is_saved_and_blocks_all_video_submissions(studio):
    snap=await ready(studio)
    settings=studio[1];assets=studio[2]
    assets.root.mkdir(parents=True,exist_ok=True)
    audio=assets.path('long-tts.mp3')
    await process('ffmpeg','-v','error','-y','-f','lavfi','-i','sine=duration=5',audio)
    job=await repo.quote('scene',snap['id'],'speech',settings)
    await repo.budget(studio[0],10000000);await repo.confirm(job['id'],456)
    speech=AsyncMock(return_value=audio.read_bytes())
    providers=SimpleNamespace(preflight=lambda kind:None,speech_line=speech)
    worker=Worker(SimpleNamespace(),456,settings,providers,assets)
    await worker.execute(await repo.claim())
    current=await repo.job(job['id'])
    assert current['status']=='manual' and 'Предложение:' in current['error']
    async with repo.connect() as db:
        assert await repo.one(db,"SELECT id FROM cartoon_assets WHERE job_id=? AND kind='speech_rejected'",(job['id'],))
        checked=await repo.one(db,'SELECT * FROM cartoon_scene_validation WHERE scene_id=?',(snap['id'],))
    assert checked['scene_validation_status']=='audio_too_long' and checked['audio_duration_seconds']>4.5
    with pytest.raises(ValueError,match='озвучку'):await repo.quote('scene',snap['id'],'video',replace(settings,heygen_price=100000))
    speech.assert_awaited_once()


async def test_safe_avatar_get_retry_preserves_stage(studio):
    snap=await attach_audio(studio,await ready(studio))
    job=await repo.quote('scene',snap['id'],'video',replace(studio[1],heygen_price=100000))
    await repo.update_job(job['id'],status='manual',provider_id='known-look',usage=repo.encoded({'provider':'heygen','stage':'avatar'}))
    await repo.retry_poll(job['id'],456)
    assert (await repo.job(job['id']))['status']=='avatar_polling'
    with pytest.raises(ValueError):await repo.retry_poll(job['id'],999)


def test_video_lineage_cannot_mix_new_image_or_voice():
    spec=rules.normalize(dict(characters=['a'],dialogue=[{'character_id':'a','text':'Привет!'}]))
    snap={'spec':spec,'assets':{'image':{'id':'img'},'speech':{'id':'mp3'},
          'video':{'approved':1,'metadata':repo.encoded({'duration_seconds':5,'image_id':'img','speech_id':'mp3'})}}}
    rules.validate_video_asset(snap)
    snap['assets']['speech']['id']='other'
    with pytest.raises(ValueError,match='другой озвучкой'):rules.validate_video_asset(snap)
    snap['assets']['speech']['id']='mp3';snap['assets']['image']['id']='other'
    with pytest.raises(ValueError,match='другому изображению'):rules.validate_video_asset(snap)


async def test_empty_final_scene_generates_single_location_frame_and_requires_anchor(studio):
    from multstudio.prompts import scene_image_prompt,validate_scene_image_references
    key,scenes=await episode(studio)
    snap=await repo.snapshot('scene',scenes[-1]['id'])
    with pytest.raises(ValueError,match='предыдущей сцены'):validate_scene_image_references(snap)
    snap['location_reference']={'approved':1}
    validate_scene_image_references(snap)
    prompt=scene_image_prompt(snap,'https://example.org/location.png')
    assert 'exactly 0 characters' in prompt and 'split screen' in prompt
    assert 'https://example.org/location.png' in prompt


async def test_line_edit_plain_text_preserves_speaker_and_never_submits(studio):
    from multstudio.router import create_router
    from test_multstudio_project_menu import event
    from test_handlers import dispatch_callback,ready_context,message_event,call_handler
    key,scenes=await episode(studio)
    router=create_router(456,studio[1],studio[2]);context=await ready_context()
    await dispatch_callback(router,event('studio:line_edit:'+scenes[0]['id']),context)
    await call_handler(router,'studio_input',message_event('Мам! Давай увидим твою мечту!'),context)
    row=await repo.get('scene',scenes[0]['id'])
    assert row['spec']['dialogue'][0]['text']=='Мам! Давай увидим твою мечту!'
    assert row['spec']['speaker_id']==scenes[0]['spec']['speaker_id'] and row['spec']['duration']==5
    assert await repo.claim() is None


async def test_shortening_dialogue_keeps_exact_still_but_not_old_audio(studio):
    before=await attach_audio(studio,await ready(studio))
    image=before['assets']['image'];old_audio=before['assets']['speech']
    await repo.update_scene_line(before['id'],456,'Мам! Давай увидим мечту!')
    after=await repo.snapshot('scene',before['id'])
    assert after['assets']['image']['id']==image['id'] and 'speech' not in after['assets'] and 'video' not in after['assets']
    assert studio[2].path(old_audio['path']).is_file()
    with pytest.raises(ValueError):await repo.update_scene_line(before['id'],999,'Нельзя')
    assert (await repo.quote('scene',before['id'],'speech',studio[1]))['status']=='quoted'

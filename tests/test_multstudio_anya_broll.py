import copy
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock

import httpx
import pytest

from multstudio import repository as repo
from multstudio.episode_three_revision import apply,MIGRATION,PAIR_TITLE,ROOM_TITLE
from multstudio.episode_three import prepare,SCENES
from multstudio.episode_two import prepare as second,prepare_jobs
from multstudio.heygen_video import StudioHeyGen
from multstudio.heygen_subjects import validate as validate_subjects
from multstudio.prompts import scene_image_prompt
from multstudio.worker import Worker
from multstudio.timeline import label
from multstudio.render import Renderer,duration,process
from multstudio.rules import normalize
from test_multstudio import studio,add_image
from test_multstudio_episode_three import episode,ready,attach_audio,make_clip


async def old_series(studio):
    key,scenes=await episode(studio)
    cast={c['name']:c['id'] for c in await repo.children('character',studio[0])}
    # Reproduce the deployed first version of shot 3, including its archived version.
    spec=copy.deepcopy(scenes[2]['spec'])
    spec.update(characters=[cast['Маша']],speaker_id=cast['Маша'],speech_speeds={cast['Маша']:1.1},
                visual='Крупный план Маши с телефоном.')
    spec.pop('framing',None);spec['dialogue'][0]['character_id']=cast['Маша']
    await repo.edit('scene',scenes[2]['id'],spec)
    return key,await repo.children('scene',key),cast


async def test_revision_keeps_every_other_snapshot_id_media_and_original_numbers(studio):
    project,settings,assets=studio
    key,scenes,cast=await old_series(studio)
    for s in scenes:
        snap=await repo.snapshot('scene',s['id'])
        await add_image(assets,snap)
        await repo.add_asset(project,'scene',s['id'],'video',snap['context_hash'],'saved.mp4','checksum',approved=1,metadata=repo.encoded({'duration_seconds':5}))
    before={s['id']:await repo.snapshot('scene',s['id']) for s in scenes}
    first_two=[await repo.snapshot('episode',e['id']) for e in await repo.children('episode',project) if e['number']<3]
    chars=await repo.children('character',project)
    voices={c['id']:await repo.character_speech(c['id']) for c in chars}
    await apply(project,456)
    current=await repo.children('scene',key)
    assert [label(s) for s in current]==['1','2','2а','3','4','5','6','6а','7','8','9']
    assert len(current)==11 and sum(s['spec']['duration'] for s in current)==55
    assert sum(bool(s['spec'].get('dialogue')) for s in current)==7
    for s in scenes:
        after=await repo.snapshot('scene',s['id'])
        assert after['number']==s['number']
        if s['number']!=3: assert after==before[s['id']]
    third=await repo.get('scene',scenes[2]['id'])
    assert third['spec']['speaker_id']==cast['Аня'] and third['spec']['characters']==[cast['Аня']]
    assert third['spec']['dialogue'][0]['text']==before[third['id']]['spec']['dialogue'][0]['text']
    assert third['spec']['speech_speeds']=={cast['Аня']:1.05} and third['spec']['framing']=='medium'
    assert not (await repo.snapshot('scene',third['id']))['assets']
    assert [await repo.snapshot('episode',e['id']) for e in await repo.children('episode',project) if e['number']<3]==first_two
    assert await repo.children('character',project)==chars
    assert {c['id']:await repo.character_speech(c['id']) for c in chars}==voices
    snap=await repo.snapshot('episode',key)
    assert [s['id'] for s in snap['scenes']]==[s['id'] for s in current]
    assert snap['spec']['target_seconds']==55 and snap['scenes'][-1]['number']==9
    async with repo.connect() as db:
        assert len(await repo.rows(db,"SELECT * FROM cartoon_assets WHERE kind='video'"))==9
    assert await repo.claim() is None


async def test_revision_is_once_only_and_preserves_subsequent_manual_edits(studio):
    key,scenes,cast=await old_series(studio)
    await apply(studio[0],456)
    third=await repo.get('scene',scenes[2]['id'])
    spec=dict(third['spec'],visual='Аня за столом, исправление администратора.')
    await repo.edit('scene',third['id'],spec)
    await apply(studio[0],456);await prepare(studio[0],456)
    assert (await repo.get('scene',third['id']))['spec']==spec
    assert len(await repo.children('scene',key))==11
    with pytest.raises(ValueError):await apply(studio[0],999)


async def test_inserts_have_no_speech_and_use_same_apartment_and_heygen_model(studio):
    key,_,cast=await old_series(studio)
    await apply(studio[0],456);await prepare_jobs(key,studio[1])
    shots=await repo.children('scene',key)
    inserts=[s for s in shots if s['number']>9]
    assert len(inserts)==2
    for shot in inserts:
        spec=shot['spec']
        assert spec['duration']==5 and spec['speaker_id'] is None and spec['dialogue']==[]
        assert spec['video_provider']=='heygen_video' and spec['location_reference_scene_id']==shots[0]['spec']['location_reference_scene_id']
        assert studio[1].quote('video',{'spec':spec})['model']=='heygen-video-1'
        async with repo.connect() as db:
            jobs=await repo.rows(db,'SELECT kind,status FROM cartoon_jobs WHERE target_id=?',(shot['id'],))
        assert {j['kind'] for j in jobs}=={'image','video'} and all(j['status']=='draft' for j in jobs)
    assert inserts[0]['spec']['characters']==[cast['Аня'],cast['Маша']]
    assert inserts[1]['spec']['characters']==[]
    assert await repo.claim() is None


async def test_medium_shot_prompt_is_explicit_and_only_anya_is_cast(studio):
    key,scenes,_=await old_series(studio)
    await apply(studio[0],456)
    snap=await repo.snapshot('scene',scenes[2]['id'])
    prompt=scene_image_prompt(snap)
    assert 'ONE waist-up MEDIUM SHOT' in prompt and 'complete hands holding the phone' in prompt
    assert 'Show exactly 1 characters' in prompt and 'mouth fully closed' in prompt
    validate_subjects(snap)


@pytest.mark.parametrize('spec',[{'description':'11 лет, дочь.'},{'age':17,'description':'Персонаж'},{'description':'12 years old'}])
def test_known_minor_is_not_sent_to_heygen_speaking_avatar(spec):
    with pytest.raises(ValueError,match='младше 18'):validate_subjects({'cast':[{'spec':spec}]})


async def test_replaced_scene_unsubmitted_quotes_are_cancelled_but_paid_jobs_retained(studio):
    key,scenes,_=await old_series(studio)
    # Image confirmation does not require adult-voice validation.
    for c in await repo.children('character',studio[0]):await add_image(studio[2],await repo.snapshot('character',c['id']))
    anchor=scenes[2]['spec']['location_reference_scene_id'];await add_image(studio[2],await repo.snapshot('scene',anchor))
    job=await repo.quote('scene',scenes[2]['id'],'image',studio[1]);await repo.confirm(job['id'],456)
    await apply(studio[0],456)
    assert (await repo.job(job['id']))['status']=='cancelled'
    assert await repo.claim() is None


async def test_silent_heygen_contract_has_image_duration_no_avatar_or_tts():
    requests=[]
    def handler(r):
        requests.append(r)
        return httpx.Response(202,json={'data':{'video_id':'motion-id','status':'pending'}}) if r.method=='POST' else httpx.Response(200,json={'data':{'status':'completed','video_url':'https://example.org/motion.mp4'}})
    provider=StudioHeyGen('test',httpx.MockTransport(handler))
    spec=normalize(dict(characters=['anya','masha'],dialogue=[],camera='Medium shot',motion='A gentle nod.'))
    assert await provider.silent_video('https://example.org/pair.png',spec,'key')=='motion-id'
    assert (await provider.poll_silent('motion-id'))['url']=='https://example.org/motion.mp4'
    body=json.loads(requests[0].content)
    assert requests[0].url.path=='/v3/models/videos' and requests[1].url.path=='/v3/models/videos/motion-id'
    assert body['mode']=='image_to_video' and body['model']=='heygen-video-1' and body['duration']==5
    assert body['image']['url']=='https://example.org/pair.png'
    assert not {'audio_url','script','voice_id','avatar_id','enable_audio'} & body.keys()
    assert 'No dialogue' in body['prompt']


async def test_scene_three_requires_new_anya_speech_and_old_masha_audio_is_not_reused(studio):
    key,scenes,cast=await old_series(studio)
    await apply(studio[0],456)
    for c in await repo.children('character',studio[0]):await add_image(studio[2],await repo.snapshot('character',c['id']))
    anchor=scenes[2]['spec']['location_reference_scene_id'];await add_image(studio[2],await repo.snapshot('scene',anchor))
    snap=await repo.snapshot('scene',scenes[2]['id']);await add_image(studio[2],snap)
    with pytest.raises(ValueError,match='озвучку'):await repo.quote('scene',snap['id'],'video',studio[1])
    j=await repo.quote('scene',snap['id'],'speech',studio[1])
    q=json.loads(j['snapshot'])['quote']
    assert q['speech_profiles'][cast['Аня']]['speed']==1.05
    assert q['speech_profiles'][cast['Аня']]['voice']==(await repo.character_speech(cast['Аня']))['voice']


async def test_new_silent_job_uses_heygen_model_without_speech_or_avatar(studio):
    key,scenes,_=await old_series(studio);await apply(studio[0],456)
    for c in await repo.children('character',studio[0]):await add_image(studio[2],await repo.snapshot('character',c['id']))
    anchor=scenes[2]['spec']['location_reference_scene_id'];await add_image(studio[2],await repo.snapshot('scene',anchor))
    pair=next(s for s in await repo.children('scene',key) if s['spec']['title']==PAIR_TITLE)
    snap=await repo.snapshot('scene',pair['id']);await add_image(studio[2],snap)
    job=await repo.quote('scene',pair['id'],'video',studio[1]);await repo.confirm(job['id'],456)
    adapter=SimpleNamespace(ready=lambda:None,silent_video=AsyncMock(return_value='motion'),scene_video=AsyncMock(),avatar_video=AsyncMock())
    providers=SimpleNamespace(studio_heygen=adapter,preflight=lambda k:None)
    assets=studio[2];assets.asset_url=AsyncMock(return_value='https://example.org/pair.png')
    worker=Worker(SimpleNamespace(),456,studio[1],providers,assets)
    await worker.execute(await repo.claim())
    assert (await repo.job(job['id']))['status']=='polling'
    adapter.silent_video.assert_awaited_once()
    adapter.scene_video.assert_not_awaited();adapter.avatar_video.assert_not_awaited()
    assert json.loads((await repo.job(job['id']))['usage'])['provider']=='heygen_video'


async def test_silent_output_discards_generated_provider_audio(studio):
    source=await make_clip(studio[2],5,audio_seconds=5)
    shot={'spec':normalize(dict(characters=[],dialogue=[])),'assets':{}}
    output=await Renderer(studio[2]).normalize_video(shot,source)
    assert abs(await duration(output)-5)<.035
    report=(await process('ffmpeg','-hide_banner','-i',output,'-af','silencedetect=noise=-60dB:d=4.9','-f','null','-',return_stderr=True)).decode(errors='replace')
    assert 'silence_start: 0' in report and 'silence_end: 5' in report


async def test_timeline_assembly_remains_55s_and_ending_stays_on_original_scene9(studio):
    key,_,_=await old_series(studio);await apply(studio[0],456)
    snap=await repo.snapshot('episode',key)
    clip=await make_clip(studio[2],5,audio_seconds=5)
    renderer=Renderer(studio[2]);renderer.scene=AsyncMock(return_value=clip)
    output=await renderer.assemble(snap)
    assert abs(await duration(output)-55)<.035
    calls=renderer.scene.await_args_list
    assert [c.args[0]['number'] for c in calls]==[1,2,10,3,4,5,6,11,7,8,9]
    assert all(c.args[1] is None for c in calls[:-1])
    assert calls[-1].args[1]==snap['spec']['ending']


async def test_policy_rejects_new_masha_speaking_video_before_queue(studio):
    key,scenes,cast=await old_series(studio)
    for c in await repo.children('character',studio[0]):await add_image(studio[2],await repo.snapshot('character',c['id']))
    anchor=scenes[2]['spec']['location_reference_scene_id'];await add_image(studio[2],await repo.snapshot('scene',anchor))
    shot=await repo.snapshot('scene',scenes[0]['id']);await add_image(studio[2],shot)
    shot=await attach_audio(studio,await repo.snapshot('scene',shot['id']))
    with pytest.raises(ValueError,match='младше 18'):await repo.quote('scene',shot['id'],'video',studio[1])
    assert await repo.claim() is None


async def test_existing_silent_scene_gets_heygen_on_new_quote_without_changing_its_spec(studio):
    spec=normalize(dict(characters=[],dialogue=[]));spec['video_provider']='kling'
    before=copy.deepcopy(spec)
    quote=studio[1].quote('video',{'spec':spec})
    assert quote['video_provider']=='heygen_video' and quote['model']=='heygen-video-1'
    assert spec==before

import json
from dataclasses import replace
from types import SimpleNamespace
from unittest.mock import AsyncMock
from urllib.parse import parse_qs

import httpx
import pytest
import respx

from multstudio import repository as repo
from multstudio.adapters import Providers, YandexTTSAdapter, yandex_tts
from multstudio.seed import seed
from multstudio.tts import speech_settings, normalize_speed, validate_speech_snapshot
from multstudio.router import create_router
from multstudio.worker import Worker
from test_multstudio import studio, ready_scene, add_image
from test_multstudio_project_menu import event
from test_handlers import dispatch_callback, ready_context

MP3=b'ID3-expressive-speech'


@pytest.mark.parametrize('name,old,emotion,speed',[
    ('Аня','neutral','good',1.05),('Дима','neutral','neutral',0.95),('Маша','good','good',1.1)])
def test_old_character_defaults_are_resolved_without_mutation(name,old,emotion,speed):
    spec=dict(voice='filipp' if name=='Дима' else 'alena',emotion=old,description='Сохранённое лицо')
    before=spec.copy()
    profile=speech_settings(spec,name)
    assert (profile['emotion'],profile['speed'])==(emotion,speed)
    assert spec==before


def test_explicit_custom_settings_are_kept():
    assert speech_settings({'voice':'alena','emotion':'neutral','speed':0.8},'Аня')=={
        'voice':'alena','emotion':'neutral','speed':0.8}
    assert speech_settings({'voice':'alena','emotion':'evil'},'Аня')['emotion']=='evil'


@pytest.mark.parametrize('speed',[0,0.09,3.1,-1,float('nan'),float('inf'),True,None,'bad'])
async def test_invalid_speed_cannot_reach_paid_request(speed):
    calls=[]
    def send(request):
        calls.append(request)
        return httpx.Response(200,content=MP3)
    with pytest.raises(ValueError,match='Скорость'):
        await YandexTTSAdapter('key',transport=httpx.MockTransport(send)).synthesize('Реплика','alena','good',speed)
    assert calls==[]


@pytest.mark.parametrize('speed',[0.1,0.95,1.05,1.1,3.0])
def test_supported_speed_range(speed):
    assert normalize_speed(speed)==speed


@pytest.mark.parametrize('voice,emotion,speed',[('alena','good',1.05),('filipp','neutral',0.95),('alena','good',1.1)])
async def test_emotion_and_speed_are_encoded_in_real_form(voice,emotion,speed):
    def send(request):
        assert request.headers['Authorization']=='Api-Key secret'
        assert not request.url.query
        assert parse_qs(request.content.decode())=={
            'text':['Живая русская речь'],'lang':['ru-RU'],'voice':[voice],
            'format':['mp3'],'emotion':[emotion],'speed':[str(speed)]}
        return httpx.Response(200,content=MP3)
    assert await YandexTTSAdapter('secret',transport=httpx.MockTransport(send)).synthesize(
        'Живая русская речь',voice,emotion,speed)==MP3


@respx.mock
async def test_public_function_passes_emotion_and_speed_without_folder(monkeypatch):
    monkeypatch.setenv('YANDEX_TTS_API_KEY','key')
    route=respx.post(YandexTTSAdapter.ENDPOINT).mock(return_value=httpx.Response(200,content=MP3))
    assert await yandex_tts('Привет!','alena','good',1.1)==MP3
    body=parse_qs(route.calls[0].request.content.decode())
    assert body['emotion']==['good'] and body['speed']==['1.1'] and 'folderId' not in body


async def setup_old_ready_episode(studio):
    project,settings,assets=studio
    scene,episode=await ready_scene(studio)
    characters=await repo.children('character',project)
    old_emotions=['neutral','neutral','good']
    for char,emotion in zip(characters,old_emotions):
        spec=dict(char['spec'],emotion=emotion)
        spec.pop('speed',None)
        await repo.edit('character',char['id'],spec)
        await add_image(assets,await repo.snapshot('character',char['id']))
    saved=[]
    for scene in await repo.children('scene',episode['id']):
        await add_image(assets,await repo.snapshot('scene',scene['id']))
        job=await repo.quote('scene',scene['id'],'video',settings)
        data=('saved-video-'+str(scene['number'])).encode()
        asset=await assets.retain(json.loads(job['snapshot']),'video',data=data,suffix='mp4',job_id=job['id'])
        await repo.update_job(job['id'],status='done',actual=27000,notified=1,
                              result_url='https://cdn.example/'+job['id']+'/video.mp4')
        snap=await repo.snapshot('scene',scene['id'])
        saved.append((scene,job,asset,data,snap))
    return characters,episode,saved


async def test_all_seven_ready_videos_and_refs_survive_seed_and_new_speech_quotes(studio):
    project,settings,assets=studio
    chars,episode,saved=await setup_old_ready_episode(studio)
    before_chars=await repo.children('character',project)
    assert await seed(456)==project
    expected={'Аня':('alena','good',1.05),'Дима':('filipp','neutral',0.95),'Маша':('masha','good',1.0)}
    for scene,job,asset_id,data,before in saved:
        if scene['spec'].get('dialogue'):
            speech=await repo.quote('scene',scene['id'],'speech',settings)
            profiles=json.loads(speech['snapshot'])['quote']['speech_profiles']
            for profile in profiles.values():
                assert (profile['voice'],profile['emotion'],profile['speed'])==expected[profile['name']]
        after=await repo.snapshot('scene',scene['id'])
        assert before['context_hash']==after['context_hash']
        assert before['assets']['image']['id']==after['assets']['image']['id']
        assert after['assets']['video']['id']==asset_id
        assert before['cast']==after['cast']
        item=await repo.job(job['id'])
        assert item['status']=='done' and item['actual']==27000 and item['notified']==1
        assert item['result_url']=='https://cdn.example/'+job['id']+'/video.mp4'
        result=await repo.job_result(job['id'])
        assert assets.path(result['path']).read_bytes()==data
    assert before_chars==await repo.children('character',project)
    assert await repo.claim() is None


@respx.mock
async def test_provider_executes_confirmed_profile_even_if_live_defaults_change(studio):
    _,settings,assets=studio
    chars,episode,saved=await setup_old_ready_episode(studio)
    scene=saved[0][0]
    job=await repo.quote('scene',scene['id'],'speech',settings)
    snap=json.loads(job['snapshot'])
    profile=snap['quote']['speech_profiles'][snap['spec']['dialogue'][0]['character_id']]
    # A copy of the raw character spec can differ; only the approved profile is sent.
    snap['cast'][0]['spec']['emotion']='evil'
    snap['cast'][0]['spec']['speed']=2.0
    adapter=Providers(replace(settings,tts_key='key'),SimpleNamespace(),assets)
    route=respx.post(YandexTTSAdapter.ENDPOINT).mock(return_value=httpx.Response(200,content=MP3))
    await adapter.speech_line(snap,snap['spec']['dialogue'][0])
    body=parse_qs(route.calls[0].request.content.decode())
    assert body['emotion']==[profile['emotion']] and body['speed']==[str(profile['speed'])]
    assert body['speed']==['1.05']


async def test_old_speech_job_without_profile_stops_before_paid_submission(studio,monkeypatch):
    project,settings,assets=studio
    scene,_=await ready_scene(studio)
    await add_image(assets,await repo.snapshot('scene',scene['id']))
    job=await repo.quote('scene',scene['id'],'speech',settings)
    snap=json.loads(job['snapshot'])
    snap['quote'].pop('speech_profiles')
    with pytest.raises(ValueError,match='эмоции и скорость'): validate_speech_snapshot(snap)
    await repo.budget(project,10000000)
    await repo.confirm(job['id'],456)
    async with repo.connect() as db:
        await db.execute('UPDATE cartoon_jobs SET snapshot=? WHERE id=?',(repo.encoded(snap),job['id']))
        await db.commit()
    providers=SimpleNamespace(preflight=lambda kind:None,speech_line=AsyncMock())
    begin=AsyncMock()
    monkeypatch.setattr(repo,'begin_submission',begin)
    await Worker(SimpleNamespace(),456,settings,providers,assets).execute(await repo.claim())
    begin.assert_not_awaited()
    providers.speech_line.assert_not_awaited()
    assert (await repo.job(job['id']))['status']=='preflight_failed'


async def test_speech_confirmation_displays_reviewable_profile(studio):
    project,settings,assets=studio
    scene,_=await ready_scene(studio)
    await add_image(assets,await repo.snapshot('scene',scene['id']))
    pressed=event('studio:make:speech:scene:'+scene['id'])
    await dispatch_callback(create_router(456,settings,assets),pressed,await ready_context())
    assert 'Аня: alena, emotion=good, speed=1.05' in pressed.bot.send_message.await_args.kwargs['text']
    assert await repo.claim() is None

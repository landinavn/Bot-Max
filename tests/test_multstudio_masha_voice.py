import base64
import json
from dataclasses import replace
from types import SimpleNamespace
from unittest.mock import AsyncMock

import httpx
import pytest
import respx

from multstudio import repository as repo
from multstudio.adapters import YandexTTSV3Adapter, YandexTTSAdapter, Providers, ProviderFailed
from multstudio.router import create_router
from multstudio.tts import MASHA_PROFILE, speech_settings, validate_speech_snapshot
from test_multstudio import studio, ready_scene, add_image
from test_multstudio_tts_emotions import setup_old_ready_episode
from test_multstudio_project_menu import event
from test_handlers import dispatch_callback, ready_context

MP3=b'ID3-teenage-voice'


def streamed(data=MP3):
    return '\n'.join(json.dumps({'result':{'audioChunk':{'data':base64.b64encode(chunk).decode()}}})
                     for chunk in (data[:4],data[4:]))


async def test_v3_exact_request_and_real_stream_decode(caplog):
    seen=[]
    def send(request):
        seen.append(request)
        assert str(request.url)==YandexTTSV3Adapter.ENDPOINT
        assert request.headers['Authorization']=='Api-Key secret'
        body=json.loads(request.content)
        assert body==dict(text='Мам, а ты себе что хочешь?',hints=[{'voice':'masha'},{'role':'good'},
            {'speed':'1.0'},{'pitchShift':'80.0'}],outputAudioSpec={'containerAudio':{'containerAudioType':'MP3'}})
        assert 'folderId' not in body and 'x-folder-id' not in request.headers
        return httpx.Response(200,text=streamed())
    adapter=YandexTTSV3Adapter('secret',transport=httpx.MockTransport(send))
    assert await adapter.synthesize('Мам, а ты себе что хочешь?',**dict(MASHA_PROFILE,emotion='good'))==MP3
    assert len(seen)==1 and 'secret' not in caplog.text


@pytest.mark.parametrize('body',[
    'not-json', json.dumps({'error':{'message':'bad request'}}),
    json.dumps({'result':{'audioChunk':{'data':'invalid!'}}}),
    json.dumps({'result':{'audioChunk':{'data':base64.b64encode(b'not-mp3').decode()}}}),
    json.dumps({'result':{'textChunk':{'text':'only-text'}}}),
])
async def test_bad_v3_response_never_retries(body):
    calls=[]
    def send(request):
        calls.append(request)
        return httpx.Response(200,text=body)
    with pytest.raises(ProviderFailed):
        await YandexTTSV3Adapter('key',transport=httpx.MockTransport(send)).synthesize('Реплика',**MASHA_PROFILE)
    assert len(calls)==1


@pytest.mark.parametrize('status',[401,429,500])
async def test_v3_http_errors_mask_secret_and_dialogue_without_retry(status,caplog):
    calls=[]
    def send(request):
        calls.append(request)
        return httpx.Response(status,text='Unknown api key private-key; dialogue: частная реплика')
    with pytest.raises(ProviderFailed,match=str(status)):
        await YandexTTSV3Adapter('private-key',transport=httpx.MockTransport(send)).synthesize('частная реплика',**MASHA_PROFILE)
    assert len(calls)==1 and 'Unknown api key' in caplog.text
    assert 'private-key' not in caplog.text and 'частная реплика' not in caplog.text


@pytest.mark.parametrize('value',[True,'bad',float('nan'),float('inf'),1001,-1001])
async def test_invalid_pitch_rejected_before_request(value):
    send=AsyncMock()
    with pytest.raises(ValueError,match='pitch_shift'):
        await YandexTTSV3Adapter('key',transport=httpx.MockTransport(send)).synthesize('Реплика','masha',pitch_shift=value)
    send.assert_not_awaited()


async def test_masha_cannot_be_sent_to_v1():
    send=AsyncMock()
    with pytest.raises(ValueError):
        await YandexTTSAdapter('key',transport=httpx.MockTransport(send)).synthesize('Реплика','masha','good')
    send.assert_not_awaited()


async def test_upgrade_preserves_all_media_hashes_and_seed_specs(studio):
    project,settings,assets=studio
    chars,episode,saved=await setup_old_ready_episode(studio)
    masha=next(c for c in chars if c['name']=='Маша')
    # Model the already installed bot before the new speech-profile table existed.
    async with repo.connect() as db:
        await db.execute('DELETE FROM cartoon_character_speech_profiles')
        await db.commit()
    before_chars=await repo.children('character',project)
    await repo.init()
    assert await repo.character_speech(masha['id'])==MASHA_PROFILE
    assert before_chars==await repo.children('character',project)
    for scene,job,asset_id,data,before in saved:
        after=await repo.snapshot('scene',scene['id'])
        assert after['context_hash']==before['context_hash']
        assert after['assets']['image']['id']==before['assets']['image']['id']
        assert after['assets']['video']['id']==asset_id
        result=await repo.job_result(job['id'])
        assert assets.path(result['path']).read_bytes()==data
        assert (await repo.job(job['id']))['actual']==27000
    assert await repo.claim() is None


async def test_custom_profile_is_persistent_and_does_not_edit_character(studio):
    project,_,_=studio
    masha=next(c for c in await repo.children('character',project) if c['name']=='Маша')
    custom=dict(voice='masha',emotion='friendly',speed=0.98,pitch_shift=50)
    await repo.set_character_speech(masha['id'],456,custom)
    await repo.init()
    from multstudio.seed import seed
    await seed(456)
    assert await repo.character_speech(masha['id'])==custom
    assert await repo.get('character',masha['id'])==masha
    with pytest.raises(ValueError,match='доступа'):
        await repo.set_character_speech(masha['id'],999,MASHA_PROFILE)
    assert await repo.character_speech(masha['id'])==custom


async def scene_five(studio):
    _,_,assets=studio
    _,episode=await ready_scene(studio)
    # Approve the previous room references for all seven scenes.
    for scene in await repo.children('scene',episode['id']):
        await add_image(assets,await repo.snapshot('scene',scene['id']))
    return (await repo.children('scene',episode['id']))[4]


@respx.mock
async def test_new_quote_freezes_v3_profile_and_provider_uses_it(studio):
    _,settings,assets=studio
    scene=await scene_five(studio)
    job=await repo.quote('scene',scene['id'],'speech',settings)
    snap=json.loads(job['snapshot'])
    assert snap['quote']['model']=='yandex-speechkit-v3'
    assert snap['quote']['estimate']==settings.speech_v3_price
    line=snap['spec']['dialogue'][0]
    assert snap['quote']['speech_profiles'][line['character_id']]['pitch_shift']==80.0
    await repo.set_character_speech(line['character_id'],456,dict(MASHA_PROFILE,pitch_shift=20))
    v3=respx.post(YandexTTSV3Adapter.ENDPOINT).mock(return_value=httpx.Response(200,text=streamed()))
    provider=Providers(replace(settings,tts_key='key'),SimpleNamespace(),assets)
    assert await provider.speech_line(snap,line)==MP3
    assert json.loads(v3.calls[0].request.content)['hints'][-1]=={'pitchShift':'80.0'}
    assert len(v3.calls)==1


async def test_v3_cost_is_required_and_rounds_each_utterance(studio):
    _,settings,_=studio
    scene=await scene_five(studio)
    with pytest.raises(ValueError,match='STUDIO_TTS_V3_250_CHARS_USD'):
        await repo.quote('scene',scene['id'],'speech',replace(settings,speech_v3_price=0))
    snap=await repo.snapshot('scene',scene['id'])
    char=snap['spec']['dialogue'][0]['character_id']
    snap['spec']['dialogue']=[dict(character_id=char,text='x'*251),dict(character_id=char,text='x')]
    assert settings.quote('speech',snap,speech_profiles={char:MASHA_PROFILE})['estimate']==3*settings.speech_v3_price
    assert await repo.claim() is None


async def test_masha_card_and_preset_do_not_queue_jobs(studio):
    project,settings,assets=studio
    masha=next(c for c in await repo.children('character',project) if c['name']=='Маша')
    pressed=event('studio:c:'+masha['id'])
    router=create_router(456,settings,assets)
    await dispatch_callback(router,pressed,await ready_context())
    assert 'SpeechKit v3: masha' in pressed.bot.send_message.await_args.kwargs['text']
    pressed=event('studio:masha_voice:'+masha['id'])
    await dispatch_callback(router,pressed,await ready_context())
    assert 'платная озвучка не запускалась' in pressed.bot.send_message.await_args.kwargs['text']
    assert await repo.get('character',masha['id'])==masha
    assert await repo.claim() is None


async def test_other_projects_and_adult_voices_are_unchanged(studio):
    project,_,_=studio
    other=await repo.create('project',{},owner_id=456,title='Другой мультфильм',season=1)
    char=await repo.create('character',dict(description='Другой персонаж',voice='alena',emotion='good'),
                           project_id=other,name='Маша')
    await repo.init()
    assert (await repo.character_speech(char))['voice']=='alena'
    for person in await repo.children('character',project):
        if person['name']!='Маша':
            assert (await repo.character_speech(person['id']))['voice']==person['spec']['voice']


async def test_approval_shows_masha_pitch_model_and_cost(studio):
    _,settings,assets=studio
    scene=await scene_five(studio)
    pressed=event('studio:make:speech:scene:'+scene['id'])
    await dispatch_callback(create_router(456,settings,assets),pressed,await ready_context())
    text=pressed.bot.send_message.await_args.kwargs['text']
    assert 'yandex-speechkit-v3' in text and 'masha' in text and 'pitch_shift=80.0' in text
    assert '0.025000 USD' in text
    assert await repo.claim() is None


async def test_long_v3_line_uses_documented_chunking_mode():
    def send(request):
        assert json.loads(request.content)['unsafeMode'] is True
        return httpx.Response(200,text=streamed())
    assert await YandexTTSV3Adapter('key',transport=httpx.MockTransport(send)).synthesize('x'*251,**MASHA_PROFILE)==MP3

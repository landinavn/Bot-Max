"""Official Fish contract, voice access, durable cache and the approved-video handoff."""
import json
import subprocess
from dataclasses import replace
from types import SimpleNamespace

import httpx
import pytest

import ded_moroz as dm
from fish_audio_tts import FishAudioAdapter,FishAudioError,DEFAULT_VOICE_ID,prepare_speech_text
from multstudio.assets import Assets
from test_ded_moroz import setup_dm,audio_order,step
from test_handlers import callback_event,ready_context


@pytest.fixture(scope='module')
def mp3_bytes(tmp_path_factory):
    path=tmp_path_factory.mktemp('fish-audio')/'voice.mp3'
    subprocess.run(['ffmpeg','-nostdin','-v','error','-f','lavfi','-i',
        'sine=frequency=500:duration=0.4','-c:a','libmp3lame',str(path)],check=True,capture_output=True)
    return path.read_bytes()


def voice_data(**values):
    return dict(_id=DEFAULT_VOICE_ID,type='tts',state='trained',visibility='public',**values)


def transport(mp3,requests,*,get_status=200,post_status=200,metadata=None):
    def handle(request):
        requests.append(request)
        if request.method=='GET':
            return httpx.Response(get_status,json=voice_data() if metadata is None else metadata)
        return httpx.Response(post_status,content=mp3,headers={'Content-Type':'audio/mpeg'})
    return httpx.MockTransport(handle)


@pytest.mark.parametrize('text,expected',[
    ('[warm] Я слышал, что ты ты любишь рисовать.','Я слышал, что ты любишь рисовать.'),
    ('ТЫ ты ты молодец, Машенька!','ТЫ молодец, Машенька!'),
    ('Что ты хочешь? Ты молодец!','Что ты хочешь? Ты молодец!'),
    ('[unknown] Здравствуй, [pause] Маша!','Здравствуй… Маша!'),
])
def test_russian_text_names_and_duplicate_ty(text,expected):
    assert prepare_speech_text(text)==expected
    assert prepare_speech_text(expected)==expected


async def test_exact_official_contract_and_saved_playable_mp3(tmp_path,mp3_bytes):
    calls=[];assets=Assets(tmp_path,None)
    adapter=FishAudioAdapter('secret-key',DEFAULT_VOICE_ID,assets,transport=transport(mp3_bytes,calls))
    key='a'*32;path=await adapter.generate_speech('[warm] Здравствуй, Маша! Ты ты молодец!',job_id=key)
    assert path==key+'/speech-v0.mp3' and assets.path(path).read_bytes()==mp3_bytes
    assert [(r.method,r.url.path) for r in calls]==[('GET','/model/'+DEFAULT_VOICE_ID),('POST','/v1/tts')]
    assert all(r.headers['Authorization']=='Bearer secret-key' for r in calls)
    assert calls[1].headers['model']=='s2.1-pro'
    assert calls[1].headers['Content-Type']=='application/json'
    body=json.loads(calls[1].content)
    assert body=={'text':'Здравствуй, Маша! Ты молодец!','reference_id':DEFAULT_VOICE_ID,
                 'format':'mp3','sample_rate':44100,'mp3_bitrate':128,'language':'ru'}
    manifest=json.loads(assets.path(key+'/speech-v0.fish.json').read_text())
    assert manifest['duration']>0 and 'secret-key' not in json.dumps(manifest)
    assert await adapter.generate_speech('[warm] Здравствуй, Маша! Ты ты молодец!',job_id=key)==path
    assert len(calls)==2  # Cache replay has no HTTP, even a voice lookup.
    subprocess.run(['ffmpeg','-nostdin','-v','error','-i',str(assets.path(path)),'-f','null','-'],check=True,capture_output=True)


@pytest.mark.parametrize('change',['text','voice','model','file'])
async def test_cache_never_silently_reuses_changed_settings_or_audio(tmp_path,mp3_bytes,change):
    calls=[];assets=Assets(tmp_path,None);key='b'*32
    adapter=FishAudioAdapter('secret',DEFAULT_VOICE_ID,assets,transport=transport(mp3_bytes,calls))
    await adapter.generate_speech('Машенька, с Новым годом!',job_id=key)
    text='Машенька, с Новым годом!'
    if change=='text':text='Ванечка, с Новым годом!'
    if change=='voice':adapter.voice_id='c'*32
    if change=='model':adapter.model='s2-pro'
    if change=='file':assets.path(key+'/speech-v0.mp3').write_bytes(b'changed')
    with pytest.raises(FishAudioError,match='проверки'):
        await adapter.generate_speech(text,job_id=key)
    assert len(calls)==2


@pytest.mark.parametrize('status,code',[(401,'authentication'),(403,'forbidden'),(404,'voice_unavailable'),(429,'rate_limit')])
async def test_unavailable_voice_blocks_paid_post(tmp_path,mp3_bytes,status,code,caplog):
    calls=[]
    adapter=FishAudioAdapter('very-secret',DEFAULT_VOICE_ID,Assets(tmp_path,None),
                             transport=transport(mp3_bytes,calls,get_status=status))
    with pytest.raises(FishAudioError) as error:await adapter.generate_speech('Привет')
    assert error.value.code==code and error.value.safe_to_retry
    assert len(calls)==1 and calls[0].method=='GET'
    assert 'very-secret' not in str(error.value) and 'very-secret' not in caplog.text


@pytest.mark.parametrize('metadata',[{'_id':DEFAULT_VOICE_ID,'type':'tts','state':'training'},
    {'_id':DEFAULT_VOICE_ID,'type':'tts','state':'trained','dmca_taken_down':True},
    {'_id':'f'*32,'type':'tts','state':'trained'}])
async def test_not_ready_or_wrong_voice_is_not_replaced(tmp_path,mp3_bytes,metadata):
    calls=[];adapter=FishAudioAdapter('key',DEFAULT_VOICE_ID,Assets(tmp_path,None),
        transport=transport(mp3_bytes,calls,metadata=metadata))
    with pytest.raises(FishAudioError):await adapter.generate_speech('Привет')
    assert len(calls)==1


@pytest.mark.parametrize('status,code',[(401,'authentication'),(402,'balance'),(403,'forbidden'),
    (404,'voice_unavailable'),(429,'rate_limit'),(500,'provider_error')])
async def test_synthesis_errors_are_bounded_no_fallback_no_retry(tmp_path,mp3_bytes,status,code):
    calls=[];adapter=FishAudioAdapter('secret',DEFAULT_VOICE_ID,Assets(tmp_path,None),
        transport=transport(mp3_bytes,calls,post_status=status))
    with pytest.raises(FishAudioError) as error:await adapter.generate_speech('Привет')
    assert error.value.code==code and error.value.status_code==status
    assert error.value.safe_to_retry==(status!=500)
    assert len(calls)==2 and len([r for r in calls if r.method=='POST'])==1
    assert not list(tmp_path.rglob('*.mp3'))


async def test_timeout_after_post_is_not_retried(tmp_path):
    calls=[]
    def handle(request):
        calls.append(request)
        if request.method=='GET':return httpx.Response(200,json=voice_data())
        raise httpx.ReadTimeout('private details')
    adapter=FishAudioAdapter('key',DEFAULT_VOICE_ID,Assets(tmp_path,None),transport=httpx.MockTransport(handle))
    with pytest.raises(FishAudioError) as error:await adapter.generate_speech('Привет')
    assert error.value.code=='timeout' and not error.value.safe_to_retry
    assert len(calls)==2 and 'private details' not in str(error.value)


async def test_empty_or_invalid_mp3_cannot_be_cached(tmp_path):
    calls=[];adapter=FishAudioAdapter('key',DEFAULT_VOICE_ID,Assets(tmp_path,None),transport=transport(b'',calls))
    with pytest.raises(FishAudioError):await adapter.generate_speech('Привет',job_id='d'*32)
    assert not list(tmp_path.rglob('*.fish.json'))


async def test_saved_fish_mp3_recovers_after_restart_and_is_used_for_video(credit_db,setup_dm,mp3_bytes):
    _,_,worker=setup_dm;row=await audio_order(worker);key=row['id'];calls=[]
    worker.tts=FishAudioAdapter('key',DEFAULT_VOICE_ID,worker.assets,transport=transport(mp3_bytes,calls))
    # Simulate successful HTTP/file save followed by process exit before SQLite commit.
    await dm.update(key,status='audio_submitting')
    path=await worker.tts.generate_speech(row['text'],job_id=key,version=row['version'])
    await worker.recover_interrupted()
    assert (await dm.get(key))['status']=='audio_publish'
    worker.assets.publish.side_effect=RuntimeError('S3')
    await worker.tick();assert (await dm.get(key))['status']=='audio_publish'
    url=f'https://storage.yandexcloud.net/vision-board-bot-images/studio/{key}/speech-v0.mp3'
    worker.assets.publish.side_effect=None;worker.assets.publish.return_value=url
    await dm.update(key,next_attempt=0);await worker.tick();await worker.tick()
    current=await dm.get(key);assert current['audio_url']==url and current['preview_sent']==1
    saved=await dm.read('SELECT * FROM ded_moroz_audio_versions WHERE order_id=?',(key,))
    assert saved['provider']=='fish_audio' and saved['voice_id']==DEFAULT_VOICE_ID
    assert saved['model']=='s2.1-pro' and saved['request_sha256']==worker.tts.fingerprint(row['text'])
    worker.heygen.create_video.assert_not_awaited()
    event=callback_event('dm_audio_approve');event.message.body=SimpleNamespace(mid='preview-current')
    await dm.callback(event,await ready_context(),456);await step(worker,key)
    worker.heygen.create_video.assert_awaited_once_with('look_santa',url,'dm-video-'+key)
    assert worker.assets.path(path).read_bytes()==mp3_bytes and len(calls)==2


async def test_known_auth_error_is_paused_and_explicitly_resumable(credit_db,setup_dm,mp3_bytes):
    _,_,worker=setup_dm;row=await audio_order(worker);calls=[]
    worker.tts=FishAudioAdapter('key',DEFAULT_VOICE_ID,worker.assets,transport=transport(mp3_bytes,calls,get_status=401))
    await worker.tick();await worker.tick()
    saved=await dm.get(row['id'])
    assert saved['status']=='manual' and saved['error']=='audio_queued:authentication'
    assert len(calls)==1 and 'неверный' in worker.bot.send_message.await_args.kwargs['text']
    assert await dm.resume(row['id'],456)=='audio_queued'


async def test_fish_model_and_voice_are_configurable_without_using_old_eleven_id(monkeypatch,setup_dm):
    config,_,_=setup_dm
    monkeypatch.setenv('FISH_AUDIO_API_KEY','test-key')
    monkeypatch.setenv('FISH_AUDIO_MODEL','s2-pro')
    monkeypatch.setenv('FISH_AUDIO_VOICE_ID',DEFAULT_VOICE_ID)
    monkeypatch.setenv('DED_MOROZ_VOICE_ID','old-id-that-must-not-be-used')
    settings=dm.Settings.from_config(config)
    assert settings.fish_model=='s2-pro' and settings.voice_id==DEFAULT_VOICE_ID
    assert 'test-key' not in repr(settings)


async def test_uncertain_paid_post_remains_manual_and_is_never_retried(credit_db,setup_dm):
    _,_,worker=setup_dm;row=await audio_order(worker);calls=[]
    def handle(request):
        calls.append(request)
        if request.method=='GET':return httpx.Response(200,json=voice_data())
        raise httpx.ReadTimeout('timeout')
    worker.tts=FishAudioAdapter('key',DEFAULT_VOICE_ID,worker.assets,transport=httpx.MockTransport(handle))
    await worker.tick();await worker.recover_interrupted();await worker.tick()
    saved=await dm.get(row['id'])
    assert saved['status']=='manual' and saved['error']=='audio_submitting:timeout'
    assert len(calls)==2
    with pytest.raises(ValueError,match='неизвестен'):await dm.resume(row['id'],456)

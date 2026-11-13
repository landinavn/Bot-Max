"""The user's ACTIVE Instant clone, durable speech URLs and approved MP3 delivery."""
import json
import subprocess
from types import SimpleNamespace
from unittest.mock import AsyncMock

import httpx
import pytest

import ded_moroz as dm
from heygen import HeyGenAdapter,HeyGenError
from heygen_tts import HeyGenTTSAdapter,HeyGenSpeechError,DEFAULT_VOICE_ID,prepare_speech_text
from multstudio.assets import Assets
from test_ded_moroz import setup_dm,audio_order,step
from test_handlers import callback_event,ready_context,dispatch_callback
from handlers import create_router


@pytest.fixture(scope='module')
def mp3_bytes(tmp_path_factory):
    path=tmp_path_factory.mktemp('heygen-clone')/'voice.mp3'
    subprocess.run(['ffmpeg','-nostdin','-v','error','-f','lavfi','-i','sine=frequency=400:duration=0.4',
                    '-c:a','libmp3lame',str(path)],check=True,capture_output=True)
    return path.read_bytes()


def make_adapter(assets,mp3,calls,*,get_status=200,post_status=200,metadata=None):
    def handle(request):
        calls.append(request)
        if request.method=='GET':
            return httpx.Response(get_status,json={'data':metadata or {
                'voice_id':DEFAULT_VOICE_ID,'name':'Дед мороз','status':'ACTIVE','mode':'instant','language':'ru'}})
        return httpx.Response(post_status,json={'data':{'audio_url':'https://cdn.example/clone.mp3'}})
    async def download(url,suffix,**kwargs):
        assert url=='https://cdn.example/clone.mp3'
        return await assets.write(mp3,suffix,job_id=kwargs['job_id'],name=kwargs['name'])
    assets.download=AsyncMock(side_effect=download)
    return HeyGenTTSAdapter('secret-key',DEFAULT_VOICE_ID,assets,transport=httpx.MockTransport(handle))


async def test_confirmed_instant_clone_russian_tags_and_mp3_contract(tmp_path,mp3_bytes):
    calls=[];assets=Assets(tmp_path,None);adapter=make_adapter(assets,mp3_bytes,calls);key='a'*32
    text='[warm] Охо-хо-хо! Здравствуй, Машенька! Ты ты молодец!'
    path=await adapter.generate_speech(text,job_id=key)
    assert [(r.method,r.url.path) for r in calls]==[('GET','/v3/models/audio/voices/'+DEFAULT_VOICE_ID),('POST','/v3/models/audio/tts')]
    body=json.loads(calls[1].content)
    assert body=={'model':'heygen-voice-1','voice_id':'3146577ad1e544c1a4658ded1090740c',
        'text':'[warm] Охо-хо-хо! Здравствуй, Машенька! Ты молодец!','language':'ru',
        'output_format':{'encoding':'mp3','sample_rate_hertz':44100,'bitrate_bps':192000}}
    assert all(r.headers['X-Api-Key']=='secret-key' for r in calls)
    assert assets.path(path).read_bytes()==mp3_bytes
    assert await adapter.generate_speech(text,job_id=key)==path and len(calls)==2
    assert 'secret-key' not in assets.path(key+'/speech-v0.heygen.json').read_text()


@pytest.mark.parametrize('change',['text','voice','model','file'])
async def test_cache_rejects_changed_request_or_corrupted_file(tmp_path,mp3_bytes,change):
    calls=[];assets=Assets(tmp_path,None);adapter=make_adapter(assets,mp3_bytes,calls);key='b'*32
    await adapter.generate_speech('Привет, Маша!',job_id=key)
    text='Привет, Маша!'
    if change=='text':text='Привет, Дима!'
    if change=='voice':
        adapter.voice_id='c'*32;adapter.client.voice_id=adapter.voice_id
    if change=='model':
        adapter.model='heygen-voice-1-turbo';adapter.client.model=adapter.model
    if change=='file':assets.path(key+'/speech-v0.mp3').write_bytes(b'broken')
    with pytest.raises(HeyGenSpeechError):await adapter.generate_speech(text,job_id=key)
    assert len(calls)==2


@pytest.mark.parametrize('status',[401,402,403,404,429])
async def test_voice_access_failure_prevents_paid_post(tmp_path,mp3_bytes,status):
    calls=[];adapter=make_adapter(Assets(tmp_path,None),mp3_bytes,calls,get_status=status)
    with pytest.raises(HeyGenSpeechError) as exc:await adapter.generate_speech('Привет')
    assert exc.value.safe_to_retry and len(calls)==1


@pytest.mark.parametrize('state,mode',[('PENDING','instant'),('FAILED','instant'),('ACTIVE','professional')])
async def test_voice_must_be_active_instant_without_substitution(tmp_path,mp3_bytes,state,mode):
    calls=[];adapter=make_adapter(Assets(tmp_path,None),mp3_bytes,calls,
        metadata={'voice_id':DEFAULT_VOICE_ID,'status':state,'mode':mode})
    with pytest.raises(HeyGenSpeechError):await adapter.generate_speech('Привет')
    assert len(calls)==1


@pytest.mark.parametrize('status',[400,401,402,403,404,429,500])
async def test_rejected_post_has_no_automatic_retry(tmp_path,mp3_bytes,status):
    calls=[];adapter=make_adapter(Assets(tmp_path,None),mp3_bytes,calls,post_status=status)
    with pytest.raises(HeyGenSpeechError) as exc:await adapter.generate_speech('Привет')
    assert exc.value.safe_to_retry==(status!=500) and len(calls)==2


async def test_saved_speech_url_survives_download_failure_without_another_post(tmp_path,mp3_bytes):
    calls=[];assets=Assets(tmp_path,None);adapter=make_adapter(assets,mp3_bytes,calls);key='d'*32
    download=assets.download.side_effect;assets.download.side_effect=httpx.ReadTimeout('download')
    with pytest.raises(HeyGenSpeechError) as exc:await adapter.generate_speech('Привет',job_id=key)
    assert exc.value.safe_to_retry and assets.path(key+'/speech-v0.heygen-source.json').is_file()
    assets.download.side_effect=download
    assert await adapter.generate_speech('Привет',job_id=key)==key+'/speech-v0.mp3'
    assert len(calls)==2


async def test_restart_restores_completed_clone_mp3_and_video_uses_approved_url(credit_db,setup_dm,mp3_bytes):
    _,_,worker=setup_dm;row=await audio_order(worker);key=row['id'];calls=[]
    worker.tts=make_adapter(worker.assets,mp3_bytes,calls)
    await dm.update(key,status='audio_submitting')
    path=await worker.tts.generate_speech(row['text'],job_id=key,version=0)
    await worker.recover_interrupted()
    assert (await dm.get(key))['status']=='audio_publish'
    for _ in range(2):await step(worker,key)
    saved=await dm.get(key)
    archive=await dm.read('SELECT * FROM ded_moroz_audio_versions WHERE order_id=?',(key,))
    assert archive['provider']=='heygen' and archive['voice_id']==DEFAULT_VOICE_ID
    assert archive['model']=='heygen-voice-1' and '[warm]' in archive['submitted_text']
    event=callback_event('dm_audio_approve');event.message.body=SimpleNamespace(mid='preview-current')
    await dm.callback(event,await ready_context(),456);await step(worker,key)
    worker.heygen.create_video.assert_awaited_once_with('look_santa',saved['audio_url'],'dm-video-'+key)
    assert len(calls)==2 and worker.assets.path(path).read_bytes()==mp3_bytes


async def test_manual_old_audio_order_can_be_explicitly_retried_once_without_reentering_photo(credit_db,setup_dm,monkeypatch):
    config,settings,worker=setup_dm;row=await audio_order(worker);key=row['id']
    await dm.update(key,status='manual',error='audio_submitting:HeyGenError')
    router=create_router(admin_user_id=456);context=await ready_context()
    opening=callback_event('cat_ded_moroz');await dispatch_callback(router,opening,context)
    button=opening.bot.send_message.await_args.kwargs['attachments'][0].payload.buttons[0][0]
    assert button.payload==f'dm_manual_regenerate:{key}:0'
    assert 'платную' in opening.bot.send_message.await_args.kwargs['text']
    worker.tts.generate_speech.assert_not_awaited()
    monkeypatch.setattr(dm.Config,'from_env',lambda:config)
    monkeypatch.setattr(dm.Settings,'from_config',lambda c:settings)
    event=callback_event(button.payload)
    await dispatch_callback(router,event,context);await dispatch_callback(router,event,context)
    saved=await dm.get(key)
    assert saved['status']=='audio_queued' and saved['version']==1 and saved['regeneration_attempts']==1
    assert saved['photo_path']==row['photo_path'] and saved['text']==row['text']
    worker.tts.generate_speech.assert_not_awaited()
    for _ in range(3):await step(worker,key)
    worker.tts.generate_speech.assert_awaited_once()


async def test_manual_retry_is_admin_owned_audio_only_and_requires_valid_config(credit_db,setup_dm):
    config,settings,worker=setup_dm;row=await audio_order(worker);key=row['id']
    await dm.update(key,status='manual',error='audio_submitting:HeyGenError',video_id='existing_video')
    assert not await dm.retry_manual_audio(key,456,0,admin=456,config=config,settings=settings)
    await dm.update(key,video_id=None)
    assert not await dm.retry_manual_audio(key,456,0,admin=None,config=config,settings=settings)
    assert not await dm.retry_manual_audio(key,999,0,admin=999,config=config,settings=settings)
    from dataclasses import replace
    with pytest.raises(ValueError,match='HEYGEN_API_KEY'):
        await dm.retry_manual_audio(key,456,0,admin=456,config=config,settings=replace(settings,heygen_key=None))
    assert (await dm.get(key))['status']=='manual'


async def test_full_tts_400_diagnostic_redacts_key(caplog):
    adapter=HeyGenAdapter('actual-key',httpx.MockTransport(lambda r:httpx.Response(400,text='error actual-key details')),
                         voice_id=DEFAULT_VOICE_ID)
    with pytest.raises(HeyGenError):await adapter.generate_speech('Привет')
    assert 'HeyGen TTS Error 400: error [redacted] details' in caplog.text
    assert 'actual-key' not in caplog.text

"""Real MP3 persistence and the ElevenLabs/HeyGen handoff, with HTTP/storage mocked."""
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock

import httpx
import pytest

from elevenlabs_tts import ElevenLabsAdapter, ElevenLabsError, clean_speech_text
from multstudio.assets import Assets
import ded_moroz as dm
from test_ded_moroz import setup_dm, audio_order, step
from test_handlers import callback_event, ready_context


@pytest.mark.parametrize('text,expected',[
    ('[warm] Здравствуй, [pause] Маша! [chuckles] С Новым годом!','Здравствуй… Маша! С Новым годом!'),
    ('Привет [unknown direction] мир!','Привет мир!'),
    ('Охо-хо-хо! [pause] До встречи!','Охо-хо-хо! До встречи!'),
    ('Аня [outer [inner] hidden] и Дима','Аня и Дима'),
    ('Привет! [unfinished direction','Привет!'),
    (' [warm] [chuckles] ','')])
def test_strip_all_bracketed_directions(text,expected):
    result=clean_speech_text(text)
    assert result==expected and '[' not in result and ']' not in result
    assert clean_speech_text(result)==result


async def test_binary_mp3_contract_and_immutable_local_save(tmp_path):
    requests=[];mp3=b'ID3\x04\x00\x00audio fixture'
    def handle(request):
        requests.append(request)
        return httpx.Response(200,content=mp3,headers={'Content-Type':'audio/mpeg'})
    assets=Assets(tmp_path,None)
    adapter=ElevenLabsAdapter('eleven-secret','eleven_voice',assets,httpx.MockTransport(handle))
    key='a'*32
    path=await adapter.generate_speech('[warm] Привет, [pause] Машенька!',job_id=key,version=2)
    assert path==key+'/speech-v2.mp3' and assets.path(path).read_bytes()==mp3
    r=requests[0]
    assert r.method=='POST' and r.url.host=='api.elevenlabs.io'
    assert r.url.path=='/v1/text-to-speech/eleven_voice'
    assert r.url.params['output_format']=='mp3_44100_128'
    assert r.headers['xi-api-key']=='eleven-secret' and r.headers['content-type']=='application/json'
    assert json.loads(r.content)=={'text':'Привет… Машенька!','model_id':'eleven_multilingual_v2',
        'voice_settings':{'stability':0.5,'similarity_boost':0.75}}
    with pytest.raises(ValueError,match='уже сохранён'):
        await adapter.generate_speech('Повтор',job_id=key,version=2)
    assert len(requests)==1


@pytest.mark.parametrize('status',[400,401,402,422,429,500])
async def test_http_error_never_retried_or_saved(tmp_path,status):
    requests=[]
    def handle(request):
        requests.append(request)
        return httpx.Response(status,json={'detail':'private provider response'})
    assets=Assets(tmp_path,None)
    adapter=ElevenLabsAdapter('eleven-secret','voice',assets,httpx.MockTransport(handle))
    with pytest.raises(ElevenLabsError,match=str(status)) as exc:
        await adapter.generate_speech('Привет',job_id='b'*32)
    assert len(requests)==1 and not list(tmp_path.rglob('*.mp3'))
    assert 'eleven-secret' not in str(exc.value) and 'private provider response' not in str(exc.value)


@pytest.mark.parametrize('content,content_type',[(b'','audio/mpeg'),(b'{"error":"oops"}','application/json')])
async def test_success_without_audio_not_saved(tmp_path,content,content_type):
    adapter=ElevenLabsAdapter('key','voice',Assets(tmp_path,None),httpx.MockTransport(
        lambda r:httpx.Response(200,content=content,headers={'Content-Type':content_type})))
    with pytest.raises(ElevenLabsError):await adapter.generate_speech('Привет')
    assert not list(tmp_path.rglob('*.mp3'))


async def test_tags_only_fail_before_http(tmp_path):
    requests=[]
    adapter=ElevenLabsAdapter('key','voice',Assets(tmp_path,None),httpx.MockTransport(lambda r:requests.append(r)))
    with pytest.raises(ValueError):await adapter.generate_speech('[warm] [chuckles]')
    assert not requests


async def test_elevenlabs_mp3_is_approved_then_used_by_avatar_iv_without_resynthesis(credit_db,setup_dm):
    config,settings,worker=setup_dm;row=await audio_order(worker);key=row['id'];requests=[]
    mp3=b'ID3\x04\x00\x00approved ElevenLabs audio'
    def handle(request):
        requests.append(request)
        return httpx.Response(200,content=mp3,headers={'Content-Type':'audio/mpeg'})
    worker.tts=ElevenLabsAdapter('eleven-key','santa_voice',worker.assets,httpx.MockTransport(handle))
    await step(worker,key)
    saved=await dm.get(key)
    assert saved['status']=='audio_publish' and worker.assets.path(saved['audio_path']).read_bytes()==mp3
    archive=await dm.read('SELECT * FROM ded_moroz_audio_versions WHERE order_id=?',(key,))
    assert archive['provider']=='elevenlabs' and archive['submitted_text']==json.loads(requests[0].content)['text']
    assert '[' not in archive['submitted_text'] and '[warm]' in archive['text']
    worker.assets.publish.side_effect=RuntimeError('S3 temporarily unavailable')
    await worker.tick();await worker.recover_interrupted()
    assert (await dm.get(key))['status']=='audio_publish' and len(requests)==1
    url=f'https://storage.yandexcloud.net/vision-board-bot-images/studio/{key}/speech-v0.mp3'
    worker.assets.publish.side_effect=None;worker.assets.publish.return_value=url
    await dm.update(key,next_attempt=0);await worker.tick();await worker.tick()
    saved=await dm.get(key)
    assert saved['audio_url']==url and saved['preview_sent']==1
    worker.heygen.create_video.assert_not_awaited()
    preview=worker.bot.send_message.await_args.kwargs
    assert preview['text']=='🎧 Прослушайте озвучку Деда Мороза. Всё нравится?'
    assert worker.bot.upload_media.await_args.args[0].type.value=='audio'
    event=callback_event('dm_audio_approve');event.message.body=SimpleNamespace(mid='preview-current')
    await dm.callback(event,await ready_context(),456)
    await step(worker,key)
    worker.heygen.create_video.assert_awaited_once_with('look_santa',url,'dm-video-'+key)
    assert (await dm.get(key))['approved_audio_path']==saved['audio_path']
    assert len(requests)==1 and worker.assets.path(saved['audio_path']).read_bytes()==mp3

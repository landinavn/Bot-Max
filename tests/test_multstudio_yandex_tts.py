import json
import logging
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock
from urllib.parse import parse_qs

import httpx
import pytest
import respx

from config import Config
from multstudio import repository as repo
from multstudio.adapters import Providers, ProviderFailed, YandexTTSAdapter, yandex_tts
from multstudio.router import validate_spec, create_router
from multstudio.seed import seed
from multstudio.settings import Settings
from multstudio.tts import MODEL, voice_settings
from multstudio.worker import Worker
from test_multstudio import studio, ready_scene, add_image
from test_multstudio_project_menu import event
from test_handlers import dispatch_callback, ready_context

ENDPOINT=YandexTTSAdapter.ENDPOINT
MP3=b'ID3-russian-audio'


@pytest.mark.parametrize('voice,emotion',[('alena','neutral'),('filipp','neutral'),('alena','good')])
async def test_speechkit_v1_exact_form_auth_binary_and_safe_log(caplog,voice,emotion):
    text='Мам, а ты себе что хочешь?'
    seen=[]
    def request(req):
        seen.append(req)
        assert str(req.url)==ENDPOINT
        assert req.method=='POST'
        assert req.headers['Authorization']=='Api-Key private-yandex-key'
        assert req.headers['Content-Type'].startswith('application/x-www-form-urlencoded')
        assert parse_qs(req.content.decode())=={
            'text':[text],'lang':['ru-RU'],'voice':[voice],'format':['mp3']}
        return httpx.Response(200,content=MP3,headers={'Content-Type':'audio/mpeg'})
    with caplog.at_level(logging.INFO,logger='multstudio.adapters'):
        result=await YandexTTSAdapter('private-yandex-key','folder-id',httpx.MockTransport(request)).synthesize(text,voice,emotion)
    assert result==MP3 and len(seen)==1
    assert f'Yandex TTS: озвучено {len(text)} символов, голос {voice}' in caplog.text
    assert text not in caplog.text and 'private-yandex-key' not in caplog.text


@respx.mock
async def test_public_yandex_tts_function_reads_only_yandex_environment(monkeypatch):
    monkeypatch.setenv('YANDEX_TTS_API_KEY','new-yandex-key')
    monkeypatch.setenv('YANDEX_TTS_FOLDER_ID','new-folder')
    route=respx.post(ENDPOINT).mock(return_value=httpx.Response(200,content=MP3))
    assert await yandex_tts('Привет!','alena')==MP3
    assert route.call_count==1
    assert route.calls[0].request.headers['Authorization']=='Api-Key new-yandex-key'
    assert parse_qs(route.calls[0].request.content.decode())=={
        'text':['Привет!'],'lang':['ru-RU'],'voice':['alena'],'format':['mp3']}


@pytest.mark.parametrize('folder',[None,'','wrong-old-folder'])
async def test_api_key_request_does_not_require_or_send_folder_or_emotion(folder):
    calls=[]
    def reply(request):
        calls.append(request)
        assert request.headers['Authorization']=='Api-Key api-key-only'
        assert not request.url.query
        assert parse_qs(request.content.decode())=={
            'text':['Русская речь'],'lang':['ru-RU'],'voice':['alena'],'format':['mp3']}
        return httpx.Response(200,content=MP3)
    result=await YandexTTSAdapter('api-key-only',folder,transport=httpx.MockTransport(reply)).synthesize(
        'Русская речь','alena','good')
    assert result==MP3 and len(calls)==1


@respx.mock
async def test_yandex_tts_works_without_folder_environment_or_parameter(monkeypatch):
    monkeypatch.setenv('YANDEX_TTS_API_KEY','api-key-only')
    monkeypatch.delenv('YANDEX_TTS_FOLDER_ID',raising=False)
    route=respx.post(ENDPOINT).mock(return_value=httpx.Response(200,content=MP3))
    assert await yandex_tts('Текст','filipp')==MP3
    assert parse_qs(route.calls[0].request.content.decode())=={
        'text':['Текст'],'lang':['ru-RU'],'voice':['filipp'],'format':['mp3']}


@pytest.mark.parametrize('status',[400,401,403,429,500])
async def test_provider_errors_never_retry_paid_post_or_expose_secrets(status,caplog):
    calls=[]
    def fail(req):
        calls.append(req)
        return httpx.Response(status,json={'error':'private-yandex-key Текст'})
    with pytest.raises(ProviderFailed) as error:
        await YandexTTSAdapter('private-yandex-key','folder',httpx.MockTransport(fail)).synthesize('Текст','alena')
    assert len(calls)==1
    assert str(status) in str(error.value)
    assert 'private' not in str(error.value)
    assert f'HTTP {status}' in caplog.text and 'private' not in caplog.text


async def test_timeout_is_not_automatically_resubmitted():
    calls=[]
    def fail(req):
        calls.append(req)
        raise httpx.ReadTimeout('private-yandex-key',request=req)
    with pytest.raises(ProviderFailed) as error:
        await YandexTTSAdapter('private-yandex-key','folder',httpx.MockTransport(fail)).synthesize('Текст','alena')
    assert len(calls)==1 and 'private-yandex-key' not in str(error.value)


@pytest.mark.parametrize('body',[b'',b'{"error":"not audio"}',b'<html>error</html>'])
async def test_empty_or_non_audio_success_is_not_saved_as_mp3(body):
    adapter=YandexTTSAdapter('key','folder',httpx.MockTransport(lambda req:httpx.Response(200,content=body)))
    with pytest.raises(ProviderFailed,match='MP3'): await adapter.synthesize('Текст','alena')


@pytest.mark.parametrize('text,voice,emotion',[('', 'alena','neutral'),('x'*5001,'alena','neutral'),
                                            ('Текст','dasha','neutral'),('Текст','alena','invalid')])
async def test_invalid_v1_input_stops_before_http(text,voice,emotion):
    request=Mock(side_effect=AssertionError('HTTP must not run'))
    with pytest.raises(ValueError):
        await YandexTTSAdapter('key','folder',httpx.MockTransport(request)).synthesize(text,voice,emotion)
    request.assert_not_called()


@pytest.mark.parametrize('key,folder,missing',[('', 'folder','YANDEX_TTS_API_KEY')])
async def test_missing_config_fails_without_http(key,folder,missing):
    request=Mock(side_effect=AssertionError('HTTP must not run'))
    with pytest.raises(ValueError,match=missing):
        await YandexTTSAdapter(key,folder,httpx.MockTransport(request)).synthesize('Текст','alena')
    request.assert_not_called()


def test_settings_no_longer_fall_back_to_openai_key(tmp_path,monkeypatch):
    monkeypatch.delenv('YANDEX_TTS_API_KEY',raising=False)
    monkeypatch.delenv('YANDEX_TTS_FOLDER_ID',raising=False)
    monkeypatch.setenv('STUDIO_TTS_API_KEY','old-openai-key')
    config=Config(token='fake',photo_path=tmp_path/'unused',image_api_key='image-openai-key')
    settings=Settings.from_config(config)
    assert settings.tts_key==settings.tts_folder_id==''
    monkeypatch.setenv('YANDEX_TTS_API_KEY',' yandex-key ')
    monkeypatch.setenv('YANDEX_TTS_FOLDER_ID',' folder-id ')
    settings=Settings.from_config(config)
    assert settings.tts_key=='yandex-key' and settings.tts_folder_id=='folder-id'
    assert 'yandex-key' not in repr(settings)


@pytest.mark.parametrize('status,body',[
    (401,'{"error_code":"UNAUTHENTICATED","error_message":"Unknown api key"}'),
    (403,'Folder not found'),
    (400,'Параметр folderId не найден\nПодробности ошибки без сокращений'),
])
async def test_full_yandex_error_body_is_logged_and_request_contract_remains_correct(status,body,caplog):
    calls=[]
    def reply(request):
        calls.append(request)
        assert request.headers['Authorization']=='Api-Key test-key'
        assert not request.url.query
        assert parse_qs(request.content.decode())=={
            'text':['Привет, мир!'],'lang':['ru-RU'],'voice':['alena'],'format':['mp3']}
        assert request.headers['content-type'].startswith('application/x-www-form-urlencoded')
        return httpx.Response(status,text=body)
    with pytest.raises(ProviderFailed):
        await YandexTTSAdapter('test-key','test-folder',httpx.MockTransport(reply)).synthesize('Привет, мир!','alena')
    assert len(calls)==1
    assert f'HTTP {status}; ответ Yandex: {body}' in caplog.text


async def test_error_response_is_not_truncated(caplog):
    body='Unknown api key\n'+('detail '*3000)+'END_OF_YANDEX_RESPONSE'
    transport=httpx.MockTransport(lambda request:httpx.Response(401,text=body))
    with pytest.raises(ProviderFailed):
        await YandexTTSAdapter('secret-key','folder',transport).synthesize('Русская реплика','alena')
    assert body in caplog.text


@pytest.mark.parametrize('encoded',[False,True])
async def test_error_response_masks_echoed_key_and_dialogue_including_json_escapes(encoded,caplog):
    key='private-key-123'
    text='Мам, а ты себе что хочешь?'
    response=json.dumps({'message':'Unknown api key','Authorization':'Api-Key '+key,
                         'text':text,'details':{'reason':'api key is invalid'}},ensure_ascii=not encoded)
    with pytest.raises(ProviderFailed) as error:
        await YandexTTSAdapter(key,'folder',httpx.MockTransport(lambda req:httpx.Response(401,text=response))).synthesize(text,'alena')
    assert 'Unknown api key' in caplog.text and 'api key is invalid' in caplog.text
    assert '[REDACTED]' in caplog.text
    assert key not in caplog.text and text not in caplog.text
    assert json.dumps(text,ensure_ascii=True)[1:-1] not in caplog.text
    assert 'Unknown api key' not in str(error.value)  # Body belongs in the server log only.


@pytest.mark.parametrize('old,expected',[('nova',('alena','neutral')),('onyx',('filipp','neutral')),('coral',('alena','good'))])
def test_legacy_character_voice_mapping_does_not_mutate_spec(old,expected):
    spec=dict(voice=old,description='Сохранённый персонаж',reference='approved-ref')
    before=spec.copy()
    assert voice_settings(spec)==expected
    assert spec==before


async def test_existing_legacy_character_refs_survive_seed_and_show_effective_yandex_voice(studio):
    project,settings,assets=studio
    scene,_=await ready_scene(studio)
    chars=await repo.children('character',project)
    for char,voice in zip(chars,['nova','onyx','coral']):
        spec=dict(char['spec'],voice=voice)
        spec.pop('emotion',None)
        await repo.edit('character',char['id'],spec)
        await add_image(assets,await repo.snapshot('character',char['id']))
    before=await repo.snapshot('scene',scene['id'])
    approved=await add_image(assets,before)
    await seed(456)
    after=await repo.snapshot('scene',scene['id'])
    assert before['context_hash']==after['context_hash']
    assert after['assets']['image']['id']==approved
    pressed=event('studio:c:'+chars[0]['id'])
    await dispatch_callback(create_router(456,settings,assets),pressed,await ready_context())
    assert 'SpeechKit v1: alena, emotion=neutral' in pressed.bot.send_message.await_args.kwargs['text']
    assert '"voice": "alena"' in pressed.bot.send_message.await_args.kwargs['text']


def test_new_yandex_voice_settings_validate_and_v3_dasha_is_rejected():
    assert validate_spec('character',{'description':'Маша','voice':'alena','emotion':'good'})['voice']=='alena'
    with pytest.raises(ValueError): validate_spec('character',{'description':'Маша','voice':'dasha'})


async def speech_job(studio):
    project,settings,assets=studio
    scene,_=await ready_scene(studio)
    await add_image(assets,await repo.snapshot('scene',scene['id']))
    job=await repo.quote('scene',scene['id'],'speech',settings)
    await repo.budget(project,10000000)
    await repo.confirm(job['id'],456)
    return await repo.job(job['id'])


async def test_old_openai_quote_stops_before_submission_and_releases_reservation(studio,monkeypatch):
    item=await speech_job(studio)
    snap=json.loads(item['snapshot'])
    snap['quote']['model']='gpt-4o-mini-tts'
    async with repo.connect() as db:
        await db.execute('UPDATE cartoon_jobs SET snapshot=? WHERE id=?',(repo.encoded(snap),item['id']))
        await db.commit()
    providers=SimpleNamespace(preflight=lambda kind:None,speech_line=AsyncMock())
    begin=AsyncMock()
    monkeypatch.setattr(repo,'begin_submission',begin)
    await Worker(SimpleNamespace(),456,studio[1],providers,studio[2]).execute(await repo.claim())
    providers.speech_line.assert_not_awaited()
    begin.assert_not_awaited()
    assert (await repo.job(item['id']))['status']=='preflight_failed'
    assert (await repo.budget(studio[0]))['committed']==0


@respx.mock
async def test_worker_uses_yandex_and_retains_same_s3_speech_path(studio,monkeypatch):
    project,settings,assets=studio
    settings=replace(settings,tts_key='yandex-key',tts_folder_id='folder-id')
    item=await speech_job((project,settings,assets))
    assert json.loads(item['snapshot'])['quote']['model']==MODEL
    config=Config(token='fake',photo_path=Path('unused'),image_api_key='must-not-use-openai')
    provider=Providers(settings,config,assets)
    provider.preflight=lambda kind:None
    openai=Mock(side_effect=AssertionError('OpenAI TTS must not be called'))
    monkeypatch.setattr('multstudio.adapters._image_api_client',openai)
    route=respx.post(ENDPOINT).mock(return_value=httpx.Response(200,content=MP3))
    assets.root.mkdir(parents=True,exist_ok=True)
    merged=assets.path('merged.mp3'); merged.write_bytes(MP3)
    assets.publish=AsyncMock(side_effect=lambda relative:'https://storage.yandexcloud.net/vision-board-bot-images/'+assets.object_key(relative))
    worker=Worker(SimpleNamespace(),456,settings,provider,assets)
    worker.renderer.speech=AsyncMock(return_value=merged)
    await worker.execute(await repo.claim())
    assert route.call_count==1
    assert parse_qs(route.calls[0].request.content.decode())['voice']==['alena']
    asset=await repo.job_result(item['id'])
    assert asset['path']==item['id']+'/speech.mp3'
    assert assets.object_key(asset['path'])==f"studio/{item['id']}/speech.mp3"
    assert (await repo.job(item['id']))['status']=='done'
    openai.assert_not_called()


@pytest.mark.parametrize('voice,expected',[('nova','alena'),('onyx','filipp'),('coral','alena')])
@respx.mock
async def test_provider_uses_saved_legacy_voice_with_speechkit(studio,voice,expected):
    _,settings,assets=studio
    provider=Providers(replace(settings,tts_key='key',tts_folder_id='folder'),SimpleNamespace(),assets)
    line=dict(character_id='c',text='Русская речь без акцента.')
    snap=dict(quote={'model':MODEL},cast=[{'id':'c','spec':{'voice':voice}}],spec={'dialogue':[line]})
    route=respx.post(ENDPOINT).mock(return_value=httpx.Response(200,content=MP3))
    assert await provider.speech_line(snap,line)==MP3
    assert parse_qs(route.calls[0].request.content.decode())['voice']==[expected]


def test_speech_preflight_needs_yandex_config_but_no_openai_key(tmp_path,monkeypatch):
    from multstudio.assets import Assets
    config=Config(token='fake',photo_path=tmp_path/'unused',storage_public_base_url='https://cdn.example')
    settings=Settings(root=tmp_path,tts_key='yandex-key',tts_folder_id='folder')
    monkeypatch.setattr('image_service._storage_client',lambda config:object())
    monkeypatch.setattr('multstudio.render.Renderer.ready',lambda self:None)
    provider=Providers(settings,config,Assets(tmp_path,config))
    provider.preflight('speech')
    with pytest.raises(ValueError,match='YANDEX_TTS_API_KEY'):
        Providers(replace(settings,tts_key=''),config,Assets(tmp_path,config)).preflight('speech')
    Providers(replace(settings,tts_folder_id=''),config,Assets(tmp_path,config)).preflight('speech')


@respx.mock
async def test_yandex_completion_automatically_delivers_audio_to_admin(studio,monkeypatch):
    project,settings,assets=studio
    settings=replace(settings,tts_key='key',tts_folder_id='folder')
    item=await speech_job((project,settings,assets))
    provider=Providers(settings,SimpleNamespace(),assets)
    provider.preflight=lambda kind:None
    respx.post(ENDPOINT).mock(return_value=httpx.Response(200,content=MP3))
    assets.root.mkdir(parents=True,exist_ok=True)
    merged=assets.path('merged-delivery.mp3'); merged.write_bytes(MP3)
    assets.publish=AsyncMock(side_effect=lambda relative:'https://storage.yandexcloud.net/vision-board-bot-images/'+assets.object_key(relative))
    from maxapi.types import AttachmentUpload
    upload=AsyncMock(return_value=AttachmentUpload(type='audio',payload={'token':'yandex-audio-in-max'}))
    monkeypatch.setattr('media_service.upload_audio_for_max',upload)
    admin=SimpleNamespace(send_message=AsyncMock(return_value=object()))
    worker=Worker(admin,456,settings,provider,assets)
    worker.renderer.speech=AsyncMock(return_value=merged)
    await worker.execute(await repo.claim())
    saved=await repo.job(item['id'])
    assert saved['status']=='done' and saved['notified']==1
    assert admin.send_message.await_args.kwargs['text']=='🎧 Озвучка готова'
    assert admin.send_message.await_args.kwargs['attachments'][0].type=='audio'
    upload.assert_awaited_once_with(admin,saved['result_url'])

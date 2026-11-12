"""Admin-only FSM, immutable versions, HeyGen contracts and restart-safe billing."""
import asyncio
import io
import json
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import httpx
import pytest
from PIL import Image
from maxapi.types import AttachmentUpload

import ded_moroz as dm
from config import Config
from handlers import create_router,PosterStates
from heygen import HeyGenAdapter,HeyGenError
from test_handlers import (message_event,callback_event,call_handler,dispatch_callback,ready_context,
                           isolated_example_directory,event_replies)


@pytest.fixture
def setup_dm(tmp_path):
    examples=tmp_path/'examples';(examples/'audio').mkdir(parents=True)
    for name in ('fire_overlay.mp4','audio/jingle_bells.mp3','audio/ny_music.mp3','ded_moroz_ref.png'):
        (examples/name).write_bytes(b'fixture')
    config=Config(token='test',photo_path=tmp_path/'unused',admin_user_id=456,
                  storage_bucket='bucket',storage_public_base_url='https://storage.example/bucket')
    settings=dm.Settings(tmp_path/'files',examples,heygen_key='secret-key',tts_key='tts-secret')
    bot=SimpleNamespace(send_message=AsyncMock(return_value=SimpleNamespace(body=SimpleNamespace(mid='preview-current'))),upload_media=AsyncMock(
        return_value=AttachmentUpload(type='audio',payload={'token':'audio'})))
    tts=SimpleNamespace(synthesize=AsyncMock(return_value=b'mp3'))
    heygen=SimpleNamespace(identifier=HeyGenAdapter.identifier,
        create_avatar=AsyncMock(return_value={'look_id':'look_santa','group_id':'group_santa'}),
        avatar_status=AsyncMock(return_value={'status':'completed'}),
        create_video=AsyncMock(return_value='video_santa'),
        video_status=AsyncMock(return_value={'status':'completed','video_url':'https://video.example/video.mp4'}))
    worker=dm.Worker(bot,456,config,settings,tts,heygen)
    worker.assets.publish=AsyncMock(side_effect=lambda p:'https://storage.example/bucket/'+str(p))
    return config,settings,worker


async def audio_order(worker=None):
    row=await dm.begin(456)
    facts=dict(name='Мишенька',fact1='любишь рисовать',fact2='научился плавать',fact3='добрый',task='помогать маме')
    await dm.update(row['id'],details=json.dumps(facts,ensure_ascii=False),text=dm.TEMPLATE.format(**facts),
                    photo_path=row['id']+'/child.png',photo_url='https://storage.example/child.png',status='audio_queued')
    if worker:
        photo=worker.assets.path(row['id']+'/child.png');photo.parent.mkdir(parents=True,exist_ok=True)
        Image.new('RGB',(16,16),'blue').save(photo)
    return await dm.get(row['id'])


async def step(worker,key):
    await worker.process(await dm.get(key))
    return await dm.get(key)


async def test_name_then_full_template_and_literal_phrases(credit_db):
    e=callback_event('cat_ded_moroz');context=await ready_context();router=create_router(admin_user_id=456)
    await dispatch_callback(router,e,context)
    e.send.assert_awaited_once_with(dm.GREETING)
    assert all(x in dm.BLANK_TEMPLATE for x in ('[ИМЯ]','[УВЛЕЧЕНИЕ]','[ДОСТИЖЕНИЕ]','[КАЧЕСТВО]','[ЗАДАНИЕ]'))
    assert await context.get_state()==PosterStates.ded_moroz_input
    values=['Машенька','любишь рисовать','научилась плавать','добрая','помогать маме']
    for n,value in enumerate(values):
        event=message_event(value)
        await call_handler(router,'ded_moroz_input',event,context)
        if n==0:
            assert [c.args[0] for c in event.message.answer.await_args_list]==[dm.EXPLANATION,dm.QUESTIONS[1][1]]
        elif n<4:
            event.message.answer.assert_awaited_once_with(dm.QUESTIONS[n+1][1])
        else:
            event.message.answer.assert_awaited_once_with(dm.PHOTO_PROMPT)
        assert 'SpeechKit' not in str(event.message.answer.await_args_list)
        assert 'Фразы будут вставлены целиком' not in str(event.message.answer.await_args_list)
    row=await dm.get((await context.get_data())['dm_order'])
    assert list(json.loads(row['details']).values())==values
    assert await context.get_state()==PosterStates.ded_moroz_photo
    assert 'любишь рисовать' in dm.QUESTIONS[1][1] and 'научился плавать' in dm.QUESTIONS[2][1]
    assert 'добрый' in dm.QUESTIONS[3][1] and 'помогать маме' in dm.QUESTIONS[4][1]


@pytest.mark.parametrize('admin',[None,999])
async def test_non_admin_only_placeholder_no_order(credit_db,admin):
    e=callback_event('cat_ded_moroz');context=await ready_context()
    await dispatch_callback(create_router(admin_user_id=admin),e,context)
    assert e.send.await_args.args[0]==dm.STUB
    assert e.send.await_args.kwargs['attachments'][0].payload.buttons[0][0].payload=='back_to_main'
    assert not await dm.read('SELECT * FROM ded_moroz_orders')


async def test_no_privileged_callbacks_or_inputs_for_other_user(credit_db):
    row=await audio_order();context=await ready_context()
    await context.update_data(dm_order=row['id']);await context.set_state(PosterStates.ded_moroz_edit)
    await dm.callback(callback_event('dm_audio_ok:'+row['id']+':0'),context,999)
    await dm.collect(message_event('attack'),context,999)
    assert (await dm.get(row['id']))['status']=='audio_queued'


async def test_photo_content_saved_normalized_to_local_alias_and_cloud(credit_db,setup_dm,monkeypatch):
    from multstudio.assets import Assets
    config,settings,worker=setup_dm
    row=await audio_order();await dm.update(row['id'],status='input')
    data=io.BytesIO();Image.new('RGB',(941,1672),'blue').save(data,format='JPEG')
    async def download(self,url,suffix,**kwargs):return await self.write(data.getvalue(),suffix,job_id=kwargs['job_id'],name=kwargs['name'])
    publish=AsyncMock(return_value='https://storage.example/child.png')
    monkeypatch.setattr(Assets,'download',download);monkeypatch.setattr(Assets,'publish',publish)
    e=message_event(None);e.message.body.attachments=[SimpleNamespace(type='file',payload=SimpleNamespace(url='https://max.example/photo'))]
    context=await ready_context();await context.update_data(dm_order=row['id']);await context.set_state(PosterStates.ded_moroz_photo)
    await dm.collect(e,context,456,config,settings)
    row=await dm.get(row['id'])
    assert row['status']=='audio_queued' and row['photo_url']=='https://storage.example/child.png'
    for path in (worker.assets.path(row['photo_path']),settings.root/'ded_moroz_456/child_photo.png'):
        with Image.open(path) as image:assert image.format=='PNG' and image.size==(941,1672)
    publish.assert_awaited_once();worker.tts.synthesize.assert_not_awaited()
    e.message.answer.assert_awaited_once_with(dm.AUDIO_GENERATING)
    assert 'Здравствуй, Мишенька!' in row['text'] and 'что ты любишь рисовать.' in row['text']


async def test_audio_does_not_require_video_assets_heygen_or_ffmpeg(credit_db,setup_dm,monkeypatch):
    from dataclasses import replace
    _,settings,worker=setup_dm;row=await audio_order(worker)
    for file in (*settings.files(),settings.examples/'fire_overlay.mp4',settings.examples/'ded_moroz_ref.png'):file.unlink()
    worker.settings=replace(settings,heygen_key=None)
    monkeypatch.setattr(dm.shutil,'which',lambda name:None)
    for _ in range(3):await step(worker,row['id'])
    assert (await dm.get(row['id']))['status']=='audio_ready'
    worker.tts.synthesize.assert_awaited_once()
    worker.heygen.create_avatar.assert_not_awaited();worker.heygen.create_video.assert_not_awaited()
    with pytest.raises(ValueError,match='HEYGEN_API_KEY'):
        worker.settings.ready(worker.config,stage='video')


async def test_audio_edit_once_stale_buttons_and_duplicate_approvals(credit_db,setup_dm):
    _,_,worker=setup_dm;row=await audio_order(worker);key=row['id']
    await step(worker,key);await step(worker,key);row=await step(worker,key)
    assert row['status']=='audio_ready' and row['preview_sent']==1
    worker.tts.synthesize.assert_awaited_once_with(row['text'],'filipp','good',0.95)
    assert len(dm.keyboard(row).payload.buttons)==2
    requests=await asyncio.gather(*(dm.request_edit(key,456,0) for _ in range(5)))
    assert sum(requests)==1
    assert await dm.revised(key,456,'Исправленный текст')
    assert not await dm.revised(key,456,'Повторная правка')
    await step(worker,key);await step(worker,key);row=await step(worker,key)
    assert row['version']==1 and row['edit_used']==1 and len(dm.keyboard(row).payload.buttons)==1
    assert row['audio_path'].endswith('speech-v1.mp3')
    assert worker.assets.path(key+'/speech-v0.mp3').read_bytes()==b'mp3'
    assert not await dm.transition(key,456,'audio_ready','video_queued',version=0)
    results=await asyncio.gather(*(dm.transition(key,456,'audio_ready','video_queued',version=1) for _ in range(5)))
    assert sum(results)==1
    await step(worker,key);row=await step(worker,key)
    assert row['status']=='download'
    worker.heygen.create_avatar.assert_awaited_once()
    worker.heygen.create_video.assert_awaited_once_with('look_santa',row['audio_url'],'dm-video-'+key)
    assert worker.tts.synthesize.await_count==2


async def test_avatar_reused_for_next_order_and_waits_until_ready(credit_db,setup_dm):
    _,_,worker=setup_dm
    worker.heygen.avatar_status.side_effect=[{'status':'processing'},{'status':'completed'}]
    assert await worker.avatar() is None
    assert await worker.avatar()=='look_santa'
    assert await worker.avatar()=='look_santa'
    worker.heygen.create_avatar.assert_awaited_once()
    saved=await dm.read('SELECT * FROM ded_moroz_avatar')
    assert saved['look_id']=='look_santa' and saved['group_id']=='group_santa' and saved['status']=='ready'


async def test_unknown_paid_post_and_restart_never_resubmit(credit_db,setup_dm):
    _,_,worker=setup_dm;row=await audio_order(worker)
    worker.tts.synthesize.side_effect=httpx.ReadTimeout('timeout')
    await worker.tick();await worker.tick()
    saved=await dm.get(row['id']);assert saved['status']=='manual'
    with pytest.raises(ValueError,match='неизвестен'):await dm.resume(row['id'],456)
    worker.tts.synthesize.assert_awaited_once()
    await dm.update(row['id'],status='video_submitting')
    await worker.recover_interrupted();await worker.tick()
    assert (await dm.get(row['id']))['status']=='manual'
    worker.heygen.create_video.assert_not_awaited()


async def test_audio_publish_retry_uses_saved_mp3(credit_db,setup_dm):
    _,_,worker=setup_dm;row=await audio_order(worker);await step(worker,row['id'])
    worker.assets.publish.side_effect=[RuntimeError('s3'), 'https://storage.example/voice.mp3']
    await worker.tick();assert (await dm.get(row['id']))['status']=='audio_publish'
    await dm.update(row['id'],next_attempt=0);await worker.tick()
    assert (await dm.get(row['id']))['status']=='audio_ready'
    worker.tts.synthesize.assert_awaited_once()


async def test_published_video_counter_is_atomic_and_delivery_retry_is_free(credit_db,setup_dm):
    _,_,worker=setup_dm;row=await audio_order(worker);key=row['id']
    output=worker.assets.path(key+'/greeting.mp4');output.parent.mkdir(parents=True,exist_ok=True);output.write_bytes(b'mp4')
    await dm.update(key,status='publish',output_path=key+'/greeting.mp4')
    await step(worker,key)
    assert (await dm.get(key))['status']=='deliver'
    worker.bot.send_message.side_effect=RuntimeError('MAX')
    await worker.tick();assert (await dm.get(key))['status']=='deliver'
    worker.bot.send_message.side_effect=None
    await dm.update(key,next_attempt=0);await worker.tick()
    assert (await dm.get(key))['status']=='done'
    assert (await credit_db.get_all_counters())['dm_video_created']==1
    worker.heygen.create_video.assert_not_awaited();worker.tts.synthesize.assert_not_awaited()
    # Explicit recovery/re-publication cannot count the same MP4 twice.
    await dm.update(key,status='publish');await step(worker,key);await step(worker,key)
    assert (await credit_db.get_all_counters())['dm_video_created']==1
    assert worker.bot.upload_media.await_args.args[0].type.value=='video'


async def test_poll_download_render_publish_full_pipeline(credit_db,setup_dm,monkeypatch):
    import video_service
    _,_,worker=setup_dm;row=await audio_order(worker);key=row['id']
    await dm.update(key,status='polling',video_id='video_santa',audio_path=key+'/speech.mp3')
    async def download(url,suffix,**kwargs):return await worker.assets.write(b'raw',suffix,job_id=key,name='heygen.mp4')
    worker.assets.download=AsyncMock(side_effect=download)
    async def render(video,photo,speech,examples,target,name):target.write_bytes(b'mp4');return target
    montage=AsyncMock(side_effect=render);monkeypatch.setattr(video_service,'render_ded_moroz',montage)
    for _ in range(5):await step(worker,key)
    assert (await dm.get(key))['status']=='done'
    worker.heygen.create_video.assert_not_awaited();montage.assert_awaited_once()
    assert montage.await_args.args[-1]=='Мишенька'


async def test_resume_with_known_video_id_only_polls(credit_db,setup_dm):
    _,_,worker=setup_dm;row=await audio_order(worker)
    await dm.update(row['id'],status='manual',video_id='video_known',error='video_submitting:interrupted_paid_submission')
    assert await dm.resume(row['id'],456)=='polling'
    await worker.tick();worker.heygen.video_status.assert_awaited_once_with('video_known')
    worker.heygen.create_video.assert_not_awaited()


async def test_heygen_v3_exact_payload_and_look_identifier():
    requests=[]
    def handle(request):
        requests.append(request)
        if request.method=='POST' and request.url.path=='/v3/avatars':
            return httpx.Response(200,json={'data':{'avatar_item':{'id':'look_santa'},'avatar_group':{'id':'group_santa'}}})
        if request.method=='POST':return httpx.Response(200,json={'data':{'video_id':'video_santa'}})
        return httpx.Response(200,json={'data':{'status':'completed','video_url':'https://cdn.example/video.mp4'}})
    adapter=HeyGenAdapter('test-secret',httpx.MockTransport(handle))
    assert await adapter.create_avatar('https://cdn.example/santa.png','avatar-key')=={'look_id':'look_santa','group_id':'group_santa'}
    assert await adapter.create_video('look_santa','https://cdn.example/voice.mp3','video-key')=='video_santa'
    await adapter.video_status('video_santa')
    assert json.loads(requests[0].content)=={'type':'photo','name':'Ded Moroz','file':{'type':'url','url':'https://cdn.example/santa.png'}}
    video=json.loads(requests[1].content)
    assert video=={'type':'avatar','avatar_id':'look_santa','audio_url':'https://cdn.example/voice.mp3',
                  'aspect_ratio':'16:9','resolution':'1080p','engine':{'type':'avatar_iv'},'expressiveness':'medium'}
    assert requests[0].headers['X-Api-Key']=='test-secret'
    assert requests[1].headers['Idempotency-Key']=='video-key'
    assert requests[2].url.path=='/v3/videos/video_santa'


@pytest.mark.parametrize('status',[401,402,429,500])
async def test_heygen_errors_not_retried_and_no_key_leak(status):
    seen=[]
    def handler(request):seen.append(request);return httpx.Response(status,text='sensitive provider details')
    adapter=HeyGenAdapter('private-key',httpx.MockTransport(handler))
    with pytest.raises(HeyGenError) as err:await adapter.create_video('look','https://cdn.example/audio.mp3','key')
    assert len(seen)==1 and 'private-key' not in str(err.value) and 'sensitive' not in str(err.value)


async def test_real_callbacks_route_edit_and_reject_stale_audio(credit_db,setup_dm):
    _,_,worker=setup_dm;row=await audio_order(worker);key=row['id']
    for _ in range(3):await step(worker,key)
    router=create_router(admin_user_id=456);context=await ready_context()
    e=callback_event('dm_audio_edit:'+key+':0')
    await dispatch_callback(router,e,context)
    assert await context.get_state()==PosterStates.ded_moroz_edit
    await call_handler(router,'ded_moroz_input',message_event('Новый текст'),context)
    assert (await dm.get(key))['version']==1
    stale=callback_event('dm_audio_ok:'+key+':0');await dispatch_callback(router,stale,context)
    assert (await dm.get(key))['status']=='audio_queued'
    for _ in range(3):await step(worker,key)
    await dispatch_callback(router,callback_event('dm_audio_ok:'+key+':1'),context)
    assert (await dm.get(key))['status']=='video_queued'
    assert await context.get_state() is None


async def test_real_dispatcher_keeps_question_sequence(credit_db,monkeypatch,event_replies):
    import handlers
    from maxapi import Dispatcher
    from maxapi.context import MemoryContext
    from maxapi.context.isolation import SimpleEventIsolation
    from maxapi.types import MessageCreated,User
    from test_multstudio_project_menu import event
    monkeypatch.setattr(handlers,'_reply',event_replies)
    bot=SimpleNamespace(me=User(user_id=9999,first_name='Bot',is_bot=True),
        send_message=AsyncMock(return_value=object()),send_callback=AsyncMock(return_value=object()))
    dp=Dispatcher(storage=MemoryContext,event_isolation=SimpleEventIsolation())
    dp.include_routers(create_router(admin_user_id=456))
    try:
        await dp.handle(event('cat_ded_moroz',bot=bot))
        for n,text in enumerate(['Ванечка','любишь рисовать','научился плавать','смелый','читать перед сном']):
            incoming=MessageCreated(timestamp=1800000000010+n,bot=bot,message={
                'sender':{'user_id':456,'first_name':'Admin','is_bot':False},
                'recipient':{'chat_id':123,'chat_type':'dialog'},'timestamp':1800000000010+n,
                'body':{'mid':'dm-'+str(n),'seq':n,'text':text,'attachments':[]}})
            await dp.handle(incoming)
        row=await dm.read('SELECT * FROM ded_moroz_orders WHERE user_id=456')
        assert len(json.loads(row['details']))==5 and row['status']=='input'
        assert bot.send_message.await_args.kwargs['text']==dm.PHOTO_PROMPT
    finally:await dp.shutdown()


async def test_admin_production_counter_visible_in_stats(credit_db):
    await credit_db.increment_counter('dm_video_created',user_id=456)
    e=message_event('/stats')
    await call_handler(create_router(admin_user_id=456),'stats',e)
    text='\n'.join(c.args[0] for c in e.message.answer.await_args_list)
    assert 'Готовых поздравлений Деда Мороза: 1' in text


async def test_long_edited_text_fits_max_preview_messages(credit_db,setup_dm):
    _,_,worker=setup_dm;row=await audio_order(worker)
    await dm.update(row['id'],text='а'*4400)
    for _ in range(3):await step(worker,row['id'])
    messages=worker.bot.send_message.await_args_list
    assert len(messages)==2 and all(len(m.kwargs['text'])<=3500 for m in messages)
    assert ''.join(m.kwargs['text'] for m in messages)=='а'*4400
    assert len(messages[-1].kwargs['attachments'])==2


async def test_exact_button_payloads_bound_to_saved_preview_message(credit_db,setup_dm):
    _,_,worker=setup_dm;row=await audio_order(worker);key=row['id']
    worker.bot.send_message.return_value=SimpleNamespace(body=SimpleNamespace(mid='old-preview'))
    for _ in range(3):await step(worker,key)
    row=await dm.get(key)
    assert [b.payload for r in dm.keyboard(row).payload.buttons for b in r]==['dm_audio_ok','dm_audio_edit']
    router=create_router(admin_user_id=456);context=await ready_context()
    e=callback_event('dm_audio_edit');e.message.body=SimpleNamespace(mid='old-preview')
    await dispatch_callback(router,e,context)
    await call_handler(router,'ded_moroz_input',message_event('Новый текст'),context)
    worker.bot.send_message.return_value=SimpleNamespace(body=SimpleNamespace(mid='new-preview'))
    for _ in range(3):await step(worker,key)
    stale=callback_event('dm_audio_ok');stale.message.body=SimpleNamespace(mid='old-preview')
    await dispatch_callback(router,stale,context)
    assert (await dm.get(key))['status']=='audio_ready'
    valid=callback_event('dm_audio_ok');valid.message.body=SimpleNamespace(mid='new-preview')
    await dispatch_callback(router,valid,context)
    assert (await dm.get(key))['status']=='video_queued'


async def test_native_maxapi_sended_message_binds_audio_button(credit_db,setup_dm):
    from maxapi.types import SendedMessage
    _,_,worker=setup_dm;row=await audio_order(worker);key=row['id']
    worker.bot.send_message.return_value=SendedMessage.model_validate({'message':{
        'sender':{'user_id':999,'first_name':'Bot','is_bot':True},
        'recipient':{'chat_id':123,'chat_type':'dialog'},'timestamp':1,
        'body':{'mid':'native-preview','seq':1,'text':'Preview','attachments':[]}}})
    for _ in range(3):await step(worker,key)
    assert (await dm.get(key))['preview_mid']=='native-preview'
    e=callback_event('dm_audio_ok');e.message.body=SimpleNamespace(mid='native-preview')
    await dispatch_callback(create_router(admin_user_id=456),e,await ready_context())
    assert (await dm.get(key))['status']=='video_queued'


async def test_max_processing_retry_reuses_same_video_token(credit_db,setup_dm):
    _,_,worker=setup_dm;row=await audio_order(worker);key=row['id']
    out=worker.assets.path(key+'/greeting.mp4');out.write_bytes(b'mp4')
    await dm.update(key,status='deliver',output_path=key+'/greeting.mp4')
    worker.bot.send_message.side_effect=RuntimeError('attachment.not.ready')
    await worker.tick();assert (await dm.get(key))['status']=='deliver'
    worker.bot.send_message.side_effect=None;await dm.update(key,next_attempt=0)
    await worker.tick()
    worker.bot.upload_media.assert_awaited_once()
    assert (await dm.get(key))['status']=='done' and worker.uploads=={}


async def test_video_preflight_ignores_absent_fire(credit_db,setup_dm):
    from dataclasses import replace
    config,settings,_=setup_dm
    (settings.examples/'fire_overlay.mp4').unlink()
    replace(settings,tts_key=None).ready(config,stage='video')
    (settings.examples/'audio/ny_music.mp3').unlink()
    settings.ready(config,stage='audio')
    with pytest.raises(ValueError,match='ny_music.mp3'):
        settings.ready(config,stage='video')


async def test_resume_after_name_replays_template_then_hobby(credit_db):
    row=await dm.begin(456);await dm.update(row['id'],details=json.dumps({'name':'Машенька'}))
    e=callback_event('cat_ded_moroz');context=await ready_context()
    await dispatch_callback(create_router(admin_user_id=456),e,context)
    assert [c.args[0] for c in e.send.await_args_list]==[dm.EXPLANATION,dm.QUESTIONS[1][1]]
    assert json.loads((await dm.get(row['id']))['details'])=={'name':'Машенька'}


async def test_resume_at_photo_does_not_repeat_old_intro(credit_db):
    row=await audio_order();await dm.update(row['id'],status='input')
    e=callback_event('cat_ded_moroz');context=await ready_context()
    await dispatch_callback(create_router(admin_user_id=456),e,context)
    e.send.assert_awaited_once_with(dm.PHOTO_PROMPT)
    assert await context.get_state()==PosterStates.ded_moroz_photo

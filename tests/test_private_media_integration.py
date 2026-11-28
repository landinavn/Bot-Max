import base64
import io
import json
from dataclasses import replace
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import httpx
from PIL import Image
import pytest

from test_private_media import private_env
import private_media as pm
from config import Config


def png():
    b=io.BytesIO();Image.new('RGB',(8,8),'green').save(b,format='PNG');return b.getvalue()


async def test_private_image_pipeline_has_no_public_put_or_anonymous_head(private_env,monkeypatch):
    import image_service as images
    c,s,a,store=private_env
    c=replace(c,image_api_key='synthetic-openai',image_model='synthetic-image-model')
    monkeypatch.setattr(Config,'from_env',classmethod(lambda cls:c))
    legacy=SimpleNamespace(put_object=Mock(side_effect=AssertionError('public upload forbidden')),close=Mock())
    monkeypatch.setattr(images,'_storage_client',lambda config:legacy)
    generator=AsyncMock(return_value=[png()]);monkeypatch.setattr(images,'_generate_image_bytes',generator)
    anonymous=Mock(side_effect=AssertionError('anonymous HEAD forbidden'))
    class Session:
        head=anonymous
        async def __aenter__(self):return self
        async def __aexit__(self,*args):pass
    monkeypatch.setattr(images.aiohttp,'ClientSession',lambda **kw:Session())
    with pm.media_scope(a):
        refs=await images._generate_and_store('https://synthetic.invalid/source.png','synthetic scene')
        assert len(refs)==1 and pm.is_private(refs[0])
        assert store.read(refs[0],a)[0]==png()
        attachment=SimpleNamespace(payload=SimpleNamespace(token='synthetic-token'))
        bot=SimpleNamespace(upload_media=AsyncMock(return_value=attachment))
        await images.upload_image_for_max(bot,refs[0])
        assert bot.upload_media.call_args.args[0].buffer==png()
    generator.assert_awaited_once();anonymous.assert_not_called();legacy.put_object.assert_not_called()
    assert s.puts==1


async def test_storage_access_failure_does_not_retry_paid_generation(private_env,monkeypatch):
    import image_service as images
    c,s,a,store=private_env
    c=replace(c,image_api_key='synthetic-openai',image_model='synthetic-model')
    monkeypatch.setattr(Config,'from_env',classmethod(lambda cls:c))
    monkeypatch.setattr(images,'_storage_client',lambda c:SimpleNamespace(close=Mock()))
    generator=AsyncMock(return_value=[png()]);monkeypatch.setattr(images,'_generate_image_bytes',generator)
    s.unavailable=True
    with pm.media_scope(a),pytest.raises(pm.MediaUnavailable):
        await images._generate_and_store('https://synthetic.invalid/source.png','synthetic scene')
    generator.assert_awaited_once();assert s.puts==0


async def test_openai_edit_receives_private_file_bytes_not_a_signed_url(private_env,monkeypatch):
    import image_service as images
    c,s,a,store=private_env
    c=replace(c,image_api_key='synthetic-openai',image_model='gpt-image-1')
    ref=store.put(png(),'image/png',a,operation_id='source')
    requests=[]
    async def handler(request):
        requests.append(request)
        assert request.method=='POST'
        assert png() in request.content
        assert b'private-media:' not in request.content and b'X-Amz-Signature' not in request.content
        return httpx.Response(200,json={'data':[{'b64_json':base64.b64encode(png()).decode()}]})
    original=httpx.AsyncClient
    class SyntheticClient(original):
        def __init__(self,**kw):super().__init__(**kw,transport=httpx.MockTransport(handler))
    monkeypatch.setattr(images.httpx,'AsyncClient',SyntheticClient)
    with pm.media_scope(a):result=await images._generate_image_bytes(ref,'synthetic scene',c,count=1)
    assert result==[png()] and len(requests)==1


async def test_foreign_private_input_never_reaches_openai(private_env,monkeypatch):
    import image_service as images
    c,s,a,store=private_env
    c=replace(c,image_api_key='synthetic-openai',image_model='gpt-image-1')
    ref=store.put(png(),'image/png',a,operation_id='source')
    requests=[]
    async def handler(request):
        requests.append(request)
        return httpx.Response(200,json={'data':[{'b64_json':base64.b64encode(png()).decode()}]})
    original=httpx.AsyncClient
    class SyntheticClient(original):
        def __init__(self,**kw):super().__init__(**kw,transport=httpx.MockTransport(handler))
    monkeypatch.setattr(images.httpx,'AsyncClient',SyntheticClient)
    before=s.heads
    with pm.media_scope(pm.Access(pm.Owner('max','2'))),pytest.raises(images.ImageGenerationError):
        await images._generate_image_bytes(ref,'synthetic scene',c,count=1)
    assert requests==[] and s.heads==before


async def test_private_refs_cannot_enter_legacy_public_share_registry(private_env,credit_db):
    c,s,a,store=private_env
    ref=store.put(png(),'image/png',a,operation_id='source')
    with pytest.raises(pm.MediaAccessDenied):await credit_db.register_shared_image(ref)
    with pytest.raises(pm.MediaAccessDenied):await credit_db.register_shared_image(store.presign(ref,a).url)
    token=await credit_db.register_shared_image('https://legacy.invalid/generated.png')
    assert await credit_db.get_shared_image(token)=='https://legacy.invalid/generated.png'


async def test_unpaid_full_song_owner_still_needs_the_existing_paid_entitlement(private_env,credit_db):
    c,s,a,store=private_env
    order='synthetic-audio-order'
    await credit_db.create_audio_order(order,1,'gift','mother','synthetic details',None,'lyrics','pop','title')
    service=pm.Access(a.owner,'service',order)
    ref=store.put(b'synthetic full track','audio/mpeg',service,operation_id='full',owner_access='order')
    pending=await pm.user_access(1)
    with pytest.raises(pm.MediaAccessDenied):store.read(ref,pending)
    with pytest.raises(pm.MediaAccessDenied):store.presign(ref,pending)
    with pytest.raises(pm.MediaAccessDenied):store.grant(ref,pending,pm.Owner('max','2'),expires_at=s.now+10)
    assert store.read(ref,service)[0]==b'synthetic full track'  # Internal preview work only.
    await credit_db.mark_audio_paid(order,'synthetic-payment')
    paid=await pm.user_access(1)
    assert store.read(ref,paid)[0]==b'synthetic full track'
    with pytest.raises(pm.MediaAccessDenied):store.read(ref,await pm.user_access(2))


async def test_url_only_providers_are_blocked_before_any_post_in_new_mode(private_env,monkeypatch):
    from multstudio.adapters import KlingAdapter
    from heygen import HeyGenAdapter
    from video_service import create_video_task
    monkeypatch.setenv('PRIVATE_MEDIA_ENABLED','true')
    posts=[]
    async def handler(request):posts.append(request);return httpx.Response(200,json={})
    kling=KlingAdapter('synthetic',transport=httpx.MockTransport(handler))
    heygen=HeyGenAdapter('synthetic',transport=httpx.MockTransport(handler))
    for invoke in (
        lambda:kling.submit('https://synthetic.invalid/image.png','scene',5),
        lambda:kling.avatar('https://synthetic.invalid/image.png','https://synthetic.invalid/audio.mp3','scene'),
        lambda:heygen.create_avatar('https://synthetic.invalid/image.png','synthetic-key'),
        lambda:heygen.create_video('synthetic-avatar','https://synthetic.invalid/audio.mp3','synthetic-key'),
        lambda:create_video_task('https://synthetic.invalid/image.png','scene'),
    ):
        with pytest.raises(pm.MediaProviderBlocked):await invoke()
    assert posts==[]


async def test_existing_provider_task_can_still_poll_without_creating_again(private_env,monkeypatch):
    from multstudio.adapters import KlingAdapter
    monkeypatch.setenv('PRIVATE_MEDIA_ENABLED','true')
    seen=[]
    async def handler(request):
        seen.append(request.method)
        return httpx.Response(200,json={'data':{'status':'completed','output':{'video':'https://synthetic.invalid/completed.mp4'}}})
    result=await KlingAdapter('synthetic',transport=httpx.MockTransport(handler)).poll('synthetic-existing-id')
    assert result['url']=='https://synthetic.invalid/completed.mp4' and seen==['GET']


async def test_max_middleware_uses_event_identity_and_resets_scope(private_env):
    middleware=pm.MediaScopeMiddleware()
    async def handler(event,data):
        assert pm.current_access().owner==pm.Owner('max','1')
        return 'synthetic result'
    event=SimpleNamespace(get_ids=lambda:(10,1),callback=SimpleNamespace(payload='forged-owner-2'))
    assert await middleware(handler,event,{})=='synthetic result'
    with pytest.raises(pm.MediaAccessDenied):pm.current_access()


async def test_accepted_photo_is_archived_without_persisting_max_url_or_token(private_env,monkeypatch):
    c,s,a,store=private_env
    monkeypatch.setenv('PRIVATE_MEDIA_ENABLED','true')
    calls=[]
    async def response(request):
        calls.append(request.method)
        return httpx.Response(200,content=png())
    original=httpx.AsyncClient
    class SyntheticClient(original):
        def __init__(self,**kw):super().__init__(**kw,transport=httpx.MockTransport(response))
    monkeypatch.setattr(httpx,'AsyncClient',SyntheticClient)
    payload=SimpleNamespace(url='https://max-synthetic.invalid/photo?token=synthetic-secret',
                            model_dump=lambda:{'url':'https://max-synthetic.invalid/photo?token=synthetic-secret','token':'synthetic-max-token'})
    with pm.media_scope(a):
        retained=await pm.retain_photo_payload(payload)
        assert set(retained)=={'url'} and pm.is_private(retained['url'])
        assert await pm.retain_image_url(retained['url'])==retained['url']
        assert store.read(retained['url'],a)[0]==png()
    assert calls==['GET'] and s.puts==1
    assert b'synthetic-secret' not in c.private_media_registry.read_bytes()


async def test_failed_photo_ingest_does_not_return_original_url(private_env,monkeypatch):
    c,s,a,store=private_env
    monkeypatch.setenv('PRIVATE_MEDIA_ENABLED','true')
    original=httpx.AsyncClient
    class SyntheticClient(original):
        def __init__(self,**kw):super().__init__(**kw,transport=httpx.MockTransport(lambda request:httpx.Response(403)))
    monkeypatch.setattr(httpx,'AsyncClient',SyntheticClient)
    with pm.media_scope(a),pytest.raises(pm.MediaUnavailable) as error:
        await pm.retain_image_url('https://max-synthetic.invalid/photo?token=synthetic-secret')
    assert 'synthetic-secret' not in str(error.value) and s.puts==0


async def test_private_download_callback_checks_owner_not_only_uuid(private_env,credit_db):
    import handlers
    from test_handlers import callback_event,call_handler,ready_context
    c,s,a,store=private_env
    owner=pm.Access(pm.Owner('max','456'))
    ref=store.put(png(),'image/png',owner,operation_id='callback')
    event=callback_event('private_media_download:'+ref[len(pm.PREFIX):])
    router=handlers.create_router(admin_user_id=99)
    await call_handler(router,'private_media_download',event,await ready_context())
    event.bot.upload_media.assert_awaited_once()
    assert event.bot.send_message.call_args.kwargs['attachments'][0].payload.token=='example-token'
    foreign=callback_event('private_media_download:'+ref[len(pm.PREFIX):])
    foreign.get_ids=lambda:(123,999)
    before=s.heads
    await call_handler(router,'private_media_download',foreign,await ready_context())
    foreign.bot.upload_media.assert_not_awaited()
    assert s.heads==before and 'недоступен' in foreign.bot.send_message.call_args.kwargs['text']


async def test_studio_audio_publish_is_private_and_never_calls_legacy_writer(private_env,tmp_path,monkeypatch):
    """Evidence for private-only audio integration: all writes are synthetic."""
    from multstudio.assets import Assets
    import image_service
    c,s,a,store=private_env
    legacy=Mock(side_effect=AssertionError('legacy storage must not be reached'))
    monkeypatch.setattr(image_service,'_storage_client',legacy)
    assets=Assets(tmp_path/'synthetic-studio',c)
    with pm.media_scope(a):
        path,_=await assets.write(b'synthetic audio bytes','mp3')
        reference=await assets.publish(path)
    assert pm.is_private(reference) and not reference.startswith('https:')
    legacy.assert_not_called()
    assert {bucket for bucket,key in s.objects}=={'synthetic-private'}
    assert store.read(reference,a)==(b'synthetic audio bytes','audio/mpeg')


@pytest.mark.parametrize('policy',[
    {'Statement':[{'Effect':'Allow','Principal':'*'}]},
    {'Statement':[{'Effect':'Allow','Principal':{'AWS':'*'}}]},
    {'Statement':[{'Effect':'Allow','Principal':{'Service':'cloudfront.amazonaws.com'}}]},
])
def test_public_or_unproven_bucket_policy_blocks_writes(private_env,policy):
    c,s,a,store=private_env;s.policy=policy
    with pytest.raises(pm.MediaUnavailable):store.put(png(),'image/png',a,operation_id='blocked')
    assert s.puts==0


def test_public_bucket_acl_blocks_write_without_changing_acl(private_env):
    c,s,a,store=private_env;s.public_acl=True
    with pytest.raises(pm.MediaUnavailable):store.put(png(),'image/png',a,operation_id='blocked')
    assert s.puts==0 and s.public_acl is True


async def test_readonly_private_dependency_blocks_public_derivatives_and_paid_posts(private_env,tmp_path,monkeypatch):
    import image_service,media_service,audio_service
    from multstudio.assets import Assets
    c,s,a,store=private_env
    ref=store.put(png(),'image/png',a,operation_id='readonly-source')
    readonly=replace(c,private_media_enabled=False)
    monkeypatch.setattr(Config,'from_env',classmethod(lambda cls:readonly))
    writer=Mock();post=AsyncMock()
    monkeypatch.setattr(audio_service,'_request',post)
    path=tmp_path/'derived.png';path.write_bytes(png())
    with pm.media_scope(a):
        assert (await pm.read_bytes(ref))[0]==png()
        for operation in (lambda:image_service._store_image(writer,readonly,path),
                          lambda:media_service._upload_video(writer,readonly,path)):
            with pytest.raises(pm.MediaError):operation()
        with pytest.raises(pm.MediaError):await Assets(tmp_path,readonly).publish(path.name)
        with pytest.raises(pm.MediaError):await audio_service.generate_song('synthetic lyrics','pop','synthetic')
        with pytest.raises(pm.MediaProviderBlocked):pm.provider_input('https://synthetic.invalid/old.png','Kling')
    writer.put_object.assert_not_called();post.assert_not_awaited()


async def test_private_group_updates_do_not_enter_handlers(private_env,monkeypatch):
    monkeypatch.setenv('PRIVATE_MEDIA_ENABLED','true')
    event=SimpleNamespace(get_ids=lambda:(123,456),bot=SimpleNamespace(send_message=AsyncMock()),
        message=SimpleNamespace(recipient=SimpleNamespace(chat_type='chat')))
    handler=AsyncMock()
    await pm.MediaScopeMiddleware()(handler,event,{})
    handler.assert_not_awaited()
    assert event.bot.send_message.call_args.kwargs['user_id']==456


def test_private_intent_survives_restart_before_result(private_env):
    c,s,a,store=private_env
    actor=pm.Access(a.owner,'service','synthetic-order')
    with pm.media_scope(actor):pm.generation_policy(c)
    restarted=pm.PrivateMedia(replace(c,private_media_enabled=False),client=s)
    assert restarted.private_order(actor.owner,actor.order_id)
    assert not s.objects
    with pm.media_scope(replace(actor,private_required=True)):
        with pytest.raises(pm.MediaError):pm.generation_policy(restarted.config)


async def test_song_context_retains_private_intent_with_write_flag_off(private_env,monkeypatch):
    c,s,a,store=private_env
    actor=pm.Access(a.owner,'service','synthetic-song-order')
    store.mark_private_order(actor)
    readonly=replace(c,private_media_enabled=False)
    monkeypatch.setattr(Config,'from_env',classmethod(lambda cls:readonly))
    monkeypatch.setenv('PRIVATE_MEDIA_ENABLED','false')
    @pm.song_context
    async def mutate(data):pm.generation_policy(readonly)
    with pm.media_scope(a):
        with pytest.raises(pm.MediaError):await mutate({'audio_checkout_id':actor.order_id})


@pytest.mark.parametrize('provider',['heygen-tts','elevenlabs','speechkit-v1','speechkit-v3','whisper'])
async def test_readonly_private_scope_blocks_direct_text_and_asr_posts(private_env,tmp_path,monkeypatch,provider):
    from heygen import HeyGenAdapter
    from elevenlabs_tts import ElevenLabsAdapter
    from multstudio.adapters import YandexTTSAdapter,YandexTTSV3Adapter
    import song_alignment
    c,s,a,store=private_env
    readonly=replace(c,private_media_enabled=False)
    monkeypatch.setattr(Config,'from_env',classmethod(lambda cls:readonly))
    http=Mock(side_effect=AssertionError('No HTTP request is allowed'))
    monkeypatch.setattr(httpx,'AsyncClient',http)
    operations={
        'heygen-tts':lambda:HeyGenAdapter('synthetic-key').generate_speech('synthetic'),
        'elevenlabs':lambda:ElevenLabsAdapter('synthetic-key','synthetic-voice',None).generate_speech('synthetic'),
        'speechkit-v1':lambda:YandexTTSAdapter('synthetic-key').synthesize('synthetic','filipp'),
        'speechkit-v3':lambda:YandexTTSV3Adapter('synthetic-key').synthesize('synthetic','filipp'),
        'whisper':lambda:song_alignment.detect_song_start(tmp_path/'unused.mp3','synthetic',tmp_path),
    }
    with pm.media_scope(pm.Access(a.owner,'service','synthetic-paused',private_required=True)):
        with pytest.raises(pm.MediaError):await operations[provider]()
    http.assert_not_called()


async def test_photo_worker_scope_restores_only_results_saved_in_its_order(private_env,monkeypatch):
    import database
    c,s,a,store=private_env
    ref=store.put(png(),'image/png',a,operation_id='result-from-user-session')
    other=pm.Access(pm.Owner('max','2'))
    foreign=store.put(png(),'image/png',other,operation_id='foreign-result')
    monkeypatch.setattr(database,'get_photo_order',AsyncMock(return_value={'user_id':1,'order_id':'synthetic-photo-order'}))
    lookup=AsyncMock(return_value=[{'image_url':ref}]);monkeypatch.setattr(database,'get_photo_results',lookup)
    @pm.order_scope('get_photo_order')
    async def deliver(bot,order_id,result):return await pm.read_bytes(result)
    assert (await deliver(None,'synthetic-photo-order',ref))[0]==png()
    lookup.assert_awaited_once_with('synthetic-photo-order')
    before=s.heads
    with pytest.raises(pm.MediaAccessDenied):await deliver(None,'synthetic-photo-order',foreign)
    assert s.heads==before

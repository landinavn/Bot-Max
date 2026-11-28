"""Security 1.1.1: synthetic owners, DBs and SDK clients; never real providers."""
import asyncio
from dataclasses import replace
import hashlib
import json
import os
from pathlib import Path
import sqlite3
from types import SimpleNamespace
from unittest.mock import AsyncMock,Mock

import pytest

from config import Config
import private_media as pm
import media_policy as policy
from test_private_media import private_env


def rollout(private_env,monkeypatch,products,**kwargs):
    c,s,a,store=private_env
    c=replace(c,private_media_enabled=False,private_media_products=frozenset(products),**kwargs)
    monkeypatch.setattr(Config,'from_env',classmethod(lambda cls:c))
    monkeypatch.setenv('PRIVATE_MEDIA_PRODUCTS',','.join(products))
    monkeypatch.setenv('PRIVATE_MEDIA_ENABLED','false')
    return c,s,a,store


@pytest.mark.parametrize('product',sorted(policy.PRODUCTS))
def test_each_product_can_be_selected_independently(private_env,monkeypatch,product):
    c,s,a,store=rollout(private_env,monkeypatch,{product})
    for other in policy.PRODUCTS:
        selected=policy.select_policy(c,a.owner,other,'synthetic-'+other)
        assert selected.mode==('private' if other==product else 'legacy')
    assert not s.objects


def test_policy_is_immutable_and_survives_flag_change_and_restart(private_env,monkeypatch):
    c,s,a,store=rollout(private_env,monkeypatch,{'postcards'})
    selected=policy.select_policy(c,a.owner,'postcards','synthetic-order')
    policy.PolicyStore(c).bind(selected)
    changed=replace(c,private_media_products=frozenset())
    assert policy.select_policy(changed,a.owner,'postcards','synthetic-order')==selected
    with policy.policy_scope(selected):assert policy.private_enabled(changed)
    with pytest.raises(pm.MediaAccessDenied):policy.PolicyStore(c).bind(replace(selected,mode='legacy'))
    assert policy.PolicyStore(c).load(pm.Owner('account','1'),'synthetic-order') is None
    assert policy.PolicyStore(c).load(a.owner,'synthetic-order')==selected


def test_admin_pilot_does_not_enable_other_users(private_env,monkeypatch):
    c,s,a,store=rollout(private_env,monkeypatch,{'postcards'},private_media_pilot_user_id=99)
    assert policy.select_policy(c,a.owner,'postcards','client').mode=='legacy'
    assert policy.select_policy(c,pm.Owner('max','99'),'postcards','pilot').mode=='private'
    assert policy.select_policy(c,pm.Owner('account','99'),'postcards','web').mode=='legacy'


@pytest.mark.parametrize('product',sorted(policy.PRODUCTS-policy.SUPPORTED))
async def test_unconfirmed_private_product_is_rejected_before_business_insert(private_env,credit_db,monkeypatch,product):
    c,s,a,store=rollout(private_env,monkeypatch,{product})
    called=AsyncMock()
    @policy.checkout_policy(product)
    async def create(user_id,order_id):await called()
    with pytest.raises(pm.MediaProviderBlocked):await create(1,'new-invoice')
    called.assert_not_awaited()
    assert not c.private_media_registry.exists() and s.puts==0


async def test_blocked_cartoon_creates_no_invoice_and_spends_no_credit(private_env,credit_db,monkeypatch):
    c,s,a,store=rollout(private_env,monkeypatch,{'cartoon'})
    await credit_db.add_credits(1,2)
    with pytest.raises(pm.MediaProviderBlocked):await credit_db.create_cartoon_order('cartoon_synthetic',1,'theme_ny','short')
    assert await credit_db.get_cartoon_order('cartoon_synthetic') is None
    selected=policy.select_policy(c,a.owner,'cartoon','synthetic-session')
    with policy.policy_scope(selected),pytest.raises(pm.MediaProviderBlocked):await credit_db.deduct_credit(1)
    assert (await credit_db.get_user(1))['balance']==2
    with sqlite3.connect(credit_db.DATABASE_PATH) as db:assert db.execute('SELECT COUNT(*) FROM payment_orders').fetchone()[0]==0


async def test_private_s3_unavailable_is_detected_before_debit(private_env,credit_db,monkeypatch):
    c,s,a,store=rollout(private_env,monkeypatch,{'postcards'})
    await credit_db.add_credits(1,2)
    s.public_acl=True
    chosen=policy.select_policy(c,a.owner,'postcards','private-credit')
    with policy.policy_scope(chosen),pytest.raises(pm.MediaUnavailable):await credit_db.deduct_credit(1)
    assert (await credit_db.get_user(1))['balance']==2


async def test_paid_legacy_order_keeps_working_when_its_product_flag_is_enabled(private_env,credit_db,monkeypatch):
    c,s,a,store=rollout(private_env,monkeypatch,set())
    key=await credit_db.create_cartoon_order('cartoon_old_synthetic',1,'theme_ny','short')
    order=await credit_db.get_cartoon_order(key)
    with sqlite3.connect(credit_db.DATABASE_PATH) as db:db.execute("UPDATE cartoon_orders SET status='paid' WHERE order_id=?",(order['order_id'],))
    c=replace(c,private_media_products=frozenset({'cartoon'}))
    monkeypatch.setattr(Config,'from_env',classmethod(lambda cls:c));monkeypatch.setenv('PRIVATE_MEDIA_PRODUCTS','cartoon')
    await policy.guard_invoice_order(order['order_id'],1)
    restored=await policy.restore_operation('cartoon',1,order['order_id'])
    assert restored.mode=='legacy'
    monkeypatch.setenv('PRIVATE_MEDIA_ENABLED','true')
    with policy.policy_scope(restored):assert pm.provider_input('https://synthetic.invalid/image.png','heygen')
    assert (await credit_db.get_cartoon_order(order['order_id']))['status']=='paid'
    assert s.puts==0


async def test_invoice_api_guard_precedes_http_and_existing_cached_links(private_env,credit_db,monkeypatch):
    import prodamus
    c,s,a,store=rollout(private_env,monkeypatch,set())
    key=await credit_db.create_cartoon_order('cartoon_invoice_synthetic',1,'theme_ny','short')
    row=await credit_db.get_cartoon_order(key)
    policy.PolicyStore(c).bind(policy.Policy('cartoon','private','max','1',row['order_id']))
    sender=prodamus.ProdamusPayments.__new__(prodamus.ProdamusPayments)
    http=Mock(side_effect=AssertionError('No invoice POST'))
    monkeypatch.setattr(prodamus.aiohttp,'ClientSession',http)
    with pytest.raises(pm.MediaProviderBlocked):await sender._request_payment_link({'order_id':row['order_id'],'_param_user_id':'1'})
    with pytest.raises(pm.MediaProviderBlocked):await sender.create_payment_link(1,order_id=row['order_id'],amount=390,description='synthetic')
    http.assert_not_called()


async def test_multstudio_confirmation_is_blocked_before_queue_mutation(private_env,monkeypatch):
    from multstudio import repository
    c,s,a,store=rollout(private_env,monkeypatch,{'multstudio'})
    policy.PolicyStore(c).bind(policy.Policy('multstudio','private','max','1','studio:synthetic-job'))
    monkeypatch.setattr(repository,'media_job_owner',AsyncMock(return_value={'owner_id':1,'id':'synthetic-job'}))
    connection=Mock(side_effect=AssertionError('No studio queue write'))
    monkeypatch.setattr(repository,'connect',connection)
    with pytest.raises(pm.MediaProviderBlocked):await repository.confirm('synthetic-job',1)
    connection.assert_not_called()


async def test_ded_moroz_checkout_is_blocked_before_invoice_row(private_env,monkeypatch):
    import ded_moroz_sales
    c,s,a,store=rollout(private_env,monkeypatch,{'ded_moroz'})
    connect=Mock(side_effect=AssertionError('No invoice row'))
    monkeypatch.setattr(ded_moroz_sales.aiosqlite,'connect',connect)
    with pytest.raises(pm.MediaProviderBlocked):await ded_moroz_sales.checkout(1)
    connect.assert_not_called()


async def test_musical_policy_is_inherited_by_child_image_and_song(private_env,monkeypatch):
    c,s,a,store=rollout(private_env,monkeypatch,{'musical_card'})
    parent=await policy.bind_operation('musical_card',1,'music_synthetic')
    with policy.policy_scope(parent):
        assert policy.private_enabled(c)
        assert policy.select_policy(replace(c,private_media_products=frozenset()),a.owner,'musical_card','child').mode=='private'
        with pytest.raises(pm.MediaProviderBlocked):pm.provider_input('https://synthetic.invalid/input.png','kling')
    changed=replace(c,private_media_products=frozenset())
    monkeypatch.setattr(Config,'from_env',classmethod(lambda cls:changed))
    assert (await policy.restore_operation('musical_card',1,'music_synthetic')).mode=='private'


@pytest.mark.parametrize('grantee',['unknown-account','*','synthetic-owner-not-approved'])
def test_bucket_acl_requires_exact_allowlisted_principal(private_env,monkeypatch,grantee):
    c,s,a,store=private_env
    monkeypatch.setattr(s,'get_bucket_acl',lambda **kwargs:{'Owner':{'ID':'synthetic-owner'},'Grants':[{'Grantee':{'Type':'CanonicalUser','ID':grantee},'Permission':'FULL_CONTROL'}]})
    with pytest.raises(pm.MediaUnavailable):store.preflight()
    assert not s.objects


@pytest.mark.parametrize('rule',[
 {'Principal':{'CanonicalUser':'unknown'}},
 {'Principal':{'AWS':'synthetic-owner'}},
 {'Action':'s3:*'},
 {'NotPrincipal':{'CanonicalUser':'synthetic-owner'}},
 {'Resource':'arn:aws:s3:::other/*'},
 {'Condition':{'StringEquals':{'unknown':'value'}}},
])
def test_unknown_bucket_policy_rules_are_rejected(private_env,rule):
    c,s,a,store=private_env
    base={'Effect':'Allow','Principal':{'CanonicalUser':'synthetic-owner'},'Action':['s3:GetObject','s3:PutObject'],'Resource':'arn:aws:s3:::synthetic-private/media/v1/*'}
    s.policy={'Statement':[dict(base,**rule)]}
    with pytest.raises(pm.MediaUnavailable):store.preflight()


def test_known_yandex_principal_and_scoped_policy_pass(private_env):
    c,s,a,store=private_env
    s.policy={'Version':'2012-10-17','Statement':[{'Effect':'Allow','Principal':{'CanonicalUser':'synthetic-owner'},'Action':['s3:GetObject','s3:PutObject'],'Resource':'arn:aws:s3:::synthetic-private/media/v1/*','Condition':{'Bool':{'aws:SecureTransport':'true'}}}]}
    store.preflight()
    with pytest.raises(pm.MediaUnavailable):pm.PrivateMedia(replace(c,private_media_principals=frozenset()),client=s).preflight()


@pytest.mark.parametrize('role',['service','user'])
def test_unpaid_song_cannot_be_laundered_through_other_order_or_grant(private_env,role):
    c,s,a,store=private_env
    origin=pm.Access(a.owner,'service','audio-origin')
    ref=store.put(b'synthetic song','audio/mpeg',origin,operation_id='song',owner_access='order')
    attacker=pm.Access(a.owner,role,'another-paid-order' if role=='service' else None,references=frozenset({ref}),entitlements=frozenset({'another-paid-order'}))
    before=s.heads
    with pytest.raises(pm.MediaAccessDenied):store.read(ref,attacker)
    with pytest.raises(pm.MediaAccessDenied):store.presign(ref,attacker)
    with pytest.raises(pm.MediaAccessDenied):store.grant(ref,attacker,pm.Owner('max','2'),expires_at=int(s.now)+30)
    assert s.heads==before


@pytest.mark.parametrize('problem',['missing','corrupt','future-version','insecure-db','insecure-journal','incompatible'])
def test_registry_damage_is_fail_closed_before_s3(private_env,problem):
    c,s,a,store=private_env
    ref=store.put(b'synthetic text','text/plain',a,operation_id='test')
    path=Path(c.private_media_registry)
    if problem=='missing':path.unlink()
    elif problem=='corrupt':path.write_bytes(b'synthetic invalid sqlite')
    elif problem=='future-version':
        with sqlite3.connect(path) as db:db.execute('PRAGMA user_version=9999')
    elif problem=='insecure-db':path.chmod(0o644)
    elif problem=='insecure-journal':
        journal=Path(str(path)+'-journal');journal.write_bytes(b'synthetic');journal.chmod(0o644)
    else:
        with sqlite3.connect(path) as db:db.execute('ALTER TABLE media_objects RENAME TO unrelated')
    before=s.heads
    with pytest.raises(pm.MediaError):store.read(ref,a)
    assert s.heads==before
    if problem=='missing':
        with pytest.raises(pm.MediaUnavailable):policy.PolicyStore(c).bind(policy.Policy('postcards','legacy','max','1','cannot-recreate'))
        assert not path.exists()


async def test_readonly_rollback_delivers_saved_objects_but_cannot_publish(private_env,monkeypatch):
    c,s,a,store=rollout(private_env,monkeypatch,{'postcards'})
    chosen=policy.select_policy(c,a.owner,'postcards','retained-order');policy.PolicyStore(c).bind(chosen)
    with policy.policy_scope(chosen),pm.media_scope(a):ref=pm.store_bytes(c,b'synthetic file','text/plain',purpose='file')
    readonly=replace(c,private_media_products=frozenset(),private_media_read_only=True)
    monkeypatch.setattr(Config,'from_env',classmethod(lambda cls:readonly))
    restored=policy.select_policy(readonly,a.owner,'postcards','retained-order')
    bot=SimpleNamespace(upload_media=AsyncMock(return_value=SimpleNamespace(payload=SimpleNamespace(token='synthetic-upload'))))
    with policy.policy_scope(restored),pm.media_scope(a):
        await pm.upload_for_max(bot,ref,a,kind='file')
        with pytest.raises(pm.MediaError):pm.store_bytes(readonly,b'new','text/plain',purpose='new')
    assert store.read(ref,a)[0]==b'synthetic file'
    assert all(bucket=='synthetic-private' for bucket,key in s.objects)


async def test_flag_off_middleware_blocks_private_group_attachment_and_allows_legacy_events(private_env,credit_db,monkeypatch):
    c,s,a,store=private_env
    ref=store.put(b'synthetic file','text/plain',a,operation_id='group')
    monkeypatch.setenv('PRIVATE_MEDIA_ENABLED','false')
    event=SimpleNamespace(get_ids=lambda:(123,1),bot=SimpleNamespace(send_message=AsyncMock()),message=SimpleNamespace(recipient=SimpleNamespace(chat_type='chat')))
    called=[]
    async def handler(event,data):
        called.append(True)
        with pytest.raises(pm.MediaAccessDenied):await pm.upload_for_max(event.bot,ref,a,kind='file')
        return 'legacy event processed'
    assert await pm.MediaScopeMiddleware()(handler,event,{})=='legacy event processed'
    assert called
    event.bot.send_message.assert_not_awaited()


def test_no_s3_head_get_put_inside_write_transactions(private_env,monkeypatch):
    c,s,a,store=private_env
    for method in ('head_object','put_object','get_object'):
        original=getattr(s,method)
        def checked(*args,_original=original,**kwargs):
            with sqlite3.connect(c.private_media_registry,timeout=0.05) as db:
                db.execute('BEGIN IMMEDIATE');db.execute('PRAGMA user_version=111')
            return _original(*args,**kwargs)
        monkeypatch.setattr(s,method,checked)
    ref=store.put(b'synthetic','text/plain',a,operation_id='short-transactions')
    assert store.read(ref,a)[0]==b'synthetic'


@pytest.mark.parametrize('mime,mb',[('image/png',8),('audio/mpeg',16),('video/mp4',32),('text/plain',8)])
def test_type_limits_reject_oversized_files_before_read_or_put(private_env,tmp_path,mime,mb):
    c,s,a,store=private_env
    path=tmp_path/'large.bin'
    with path.open('wb') as f:f.truncate(mb*1024*1024+1)
    with pm.media_scope(a),pytest.raises(pm.MediaError):pm.store_path(c,path,mime,purpose='limit')
    assert s.puts==0


async def test_stream_and_max_sdk_file_path_bound_memory_and_concurrency(private_env,monkeypatch):
    """Exercise installed MAX upload_file (which reads whole file) with mocked HTTP."""
    import tracemalloc
    from maxapi.connection.base import BaseConnection
    from maxapi.enums.upload_type import UploadType
    c,s,a,store=private_env
    size=32*1024*1024;chunk=b'x'*65536
    digest=hashlib.sha256()
    for _ in range(size//len(chunk)):digest.update(chunk)
    digest=digest.hexdigest();identifier='a'*32;reference=pm.PREFIX+identifier
    with store._db(write=True) as db:
        db.execute('INSERT INTO media_objects VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)',(identifier,c.private_media_bucket,'media/v1/synthetic.mp4','max','1',None,'large',digest,size,'video/mp4','ready',int(s.now),'owner'))
    reads=[]
    class Body:
        remaining=size
        def read(self,n):
            assert 0<n<=65536
            reads.append(n);take=min(n,self.remaining);self.remaining-=take
            return b'x'*take
        def close(self):pass
    monkeypatch.setattr(s,'head_object',lambda **kw:{'ContentLength':size,'Metadata':{'sha256':digest}})
    monkeypatch.setattr(s,'get_object',lambda **kw:{'Body':Body()})
    active=peak=0;paths=[]
    class Response:
        async def __aenter__(self):return self
        async def __aexit__(self,*args):pass
    def post(**kwargs):
        form=kwargs['data'];data=form._fields[0][2]
        assert len(data)==size
        return Response()
    sdk_bot=SimpleNamespace(session=SimpleNamespace(closed=False,post=post))
    connection=SimpleNamespace(_ensure_bot=lambda:sdk_bot,_read_upload_response=AsyncMock(return_value='synthetic'))
    async def upload(media):
        nonlocal active,peak
        assert hasattr(media,'path') and not hasattr(media,'buffer')
        path=Path(media.path);paths.append(path)
        assert path.stat().st_mode&0o777==0o600
        active+=1;peak=max(peak,active)
        try:
            await BaseConnection.upload_file(connection,'https://synthetic.invalid/upload',str(path),UploadType.VIDEO)
            await asyncio.sleep(0.01)
            return SimpleNamespace(payload=SimpleNamespace(token='synthetic-token'))
        finally:active-=1
    bot=SimpleNamespace(upload_media=upload)
    tracemalloc.start()
    try:
        await asyncio.gather(*(pm.upload_for_max(bot,reference,a,kind='video') for _ in range(3)))
        _,maximum=tracemalloc.get_traced_memory()
    finally:tracemalloc.stop()
    assert peak==1 and max(reads)<=65536 and len(reads)>1000
    assert maximum<96*1024*1024
    assert all(not p.exists() for p in paths)
    print(json.dumps({'synthetic_concurrent_transfers':3,'maximum_active':peak,'file_mib':32,'python_tracemalloc_peak_mib':round(maximum/1024/1024,2)}))


def test_paid_song_requires_both_fresh_origin_entitlement_and_saved_service_reference(private_env):
    c,s,a,store=private_env
    origin=pm.Access(a.owner,'service','audio-origin')
    ref=store.put(b'synthetic audio','audio/mpeg',origin,operation_id='reuse',owner_access='order')
    entitled=pm.Access(a.owner,'service','music-target',references=frozenset({ref}),entitlements=frozenset({'audio-origin'}))
    assert store.read(ref,entitled)[0]==b'synthetic audio'
    with pytest.raises(pm.MediaAccessDenied):store.read(ref,replace(entitled,references=frozenset()))
    with pytest.raises(pm.MediaAccessDenied):store.read(ref,replace(entitled,entitlements=frozenset()))


async def test_private_ded_begin_is_blocked_before_creating_admin_job(private_env,monkeypatch):
    import ded_moroz
    c,s,a,store=rollout(private_env,monkeypatch,{'ded_moroz'})
    connect=Mock(side_effect=AssertionError('No new Ded Moroz row'))
    monkeypatch.setattr(ded_moroz.aiosqlite,'connect',connect)
    with pytest.raises(pm.MediaProviderBlocked):await ded_moroz.begin(1,new=True)
    connect.assert_not_called()


async def test_private_ded_regeneration_preserves_attempt_and_paid_status(private_env,monkeypatch):
    import ded_moroz
    c,s,a,store=rollout(private_env,monkeypatch,{'ded_moroz'})
    policy.PolicyStore(c).bind(policy.Policy('ded_moroz','private','max','1','ded_moroz:synthetic-dm'))
    row={'id':'synthetic-dm','user_id':1,'status':'audio_ready','regeneration_attempts':0,'payment_order_id':'dm-existing'}
    monkeypatch.setattr(ded_moroz,'get',AsyncMock(return_value=row))
    connection=Mock(side_effect=AssertionError('Do not consume regeneration right'))
    monkeypatch.setattr(ded_moroz.aiosqlite,'connect',connection)
    with pytest.raises(pm.MediaProviderBlocked):await ded_moroz.regenerate('synthetic-dm',1,1)
    assert row['status']=='audio_ready' and row['regeneration_attempts']==0
    connection.assert_not_called()


async def test_legacy_ded_paid_parent_is_restored_without_applying_new_private_flag(private_env,monkeypatch):
    import ded_moroz
    c,s,a,store=rollout(private_env,monkeypatch,{'ded_moroz'})
    row={'id':'synthetic-dm','user_id':1,'payment_order_id':'dm-paid-before-rollout'}
    monkeypatch.setattr(ded_moroz,'get',AsyncMock(return_value=row))
    selected=await policy.ded_action_policy('synthetic-dm',1)
    assert selected.mode=='legacy'
    assert policy.PolicyStore(c).load(a.owner,'ded_moroz:synthetic-dm')==selected


async def test_unverified_storage_blocks_new_checkout_before_s3_or_business_write(private_env,monkeypatch):
    c,s,a,store=rollout(private_env,monkeypatch,{'postcards'},private_media_storage_verified=False)
    called=AsyncMock()
    @policy.checkout_policy('postcards')
    async def create(user_id,order_id):await called()
    with pytest.raises(pm.MediaUnavailable):await create(1,'synthetic-new-payment')
    called.assert_not_awaited();assert not c.private_media_registry.exists()
    assert s.heads==0 and s.puts==0


async def test_emergency_reader_uses_fresh_owner_and_never_changes_objects(private_env,credit_db,monkeypatch):
    from private_media_emergency import deliver_saved
    c,s,a,store=private_env
    ref=store.put(b'synthetic retained document','text/plain',a,operation_id='emergency')
    readonly=replace(c,private_media_enabled=False,private_media_read_only=True)
    monkeypatch.setattr(Config,'from_env',classmethod(lambda cls:readonly))
    bot=SimpleNamespace(upload_media=AsyncMock(return_value=SimpleNamespace(payload=SimpleNamespace(token='synthetic-token'))),send_message=AsyncMock(return_value=True))
    before=s.puts
    with pm.media_scope(a):await deliver_saved(bot,ref)
    assert s.puts==before and bot.send_message.call_args.kwargs['user_id']==1
    bot.upload_media.reset_mock()
    with pm.media_scope(pm.Access(pm.Owner('max','2'))),pytest.raises(pm.MediaAccessDenied):await deliver_saved(bot,ref)
    bot.upload_media.assert_not_awaited()


async def test_emergency_reader_refuses_service_scope_and_unsigned_caller(private_env,monkeypatch):
    from private_media_emergency import deliver_saved
    c,s,a,store=private_env
    monkeypatch.setattr(Config,'from_env',classmethod(lambda cls:replace(c,private_media_read_only=True)))
    bot=SimpleNamespace(upload_media=AsyncMock(),send_message=AsyncMock())
    with pm.media_scope(pm.Access(a.owner,'service','synthetic-order')),pytest.raises(pm.MediaAccessDenied):await deliver_saved(bot,pm.PREFIX+'0'*32)
    bot.upload_media.assert_not_awaited();bot.send_message.assert_not_awaited()


async def test_private_root_image_product_is_selected_without_an_outer_handler(private_env,monkeypatch):
    import image_service
    from test_private_media_integration import png
    c,s,a,store=rollout(private_env,monkeypatch,{'postcards'})
    c=replace(c,image_api_key='synthetic-openai',image_model='synthetic-model')
    monkeypatch.setattr(Config,'from_env',classmethod(lambda cls:c))
    legacy=SimpleNamespace(close=Mock(),put_object=Mock(side_effect=AssertionError('No public output')))
    monkeypatch.setattr(image_service,'_storage_client',lambda c:legacy)
    generator=AsyncMock(return_value=[png()]);monkeypatch.setattr(image_service,'_generate_image_bytes',generator)
    monkeypatch.setattr(image_service,'_postcard_font',lambda *args:None)
    monkeypatch.setattr(image_service,'add_text_to_image',lambda data,*args,**kwargs:data)
    with pm.media_scope(a):result=await image_service.generate_postcards('https://synthetic.invalid/input.png','mode_mother','С праздником!')
    assert len(result)==1 and pm.is_private(result[0])
    assert store.read(result[0],a)[0]==png()
    generator.assert_awaited_once();legacy.put_object.assert_not_called()


async def test_unsupported_private_root_cannot_call_paid_image_generator(private_env,monkeypatch):
    import image_service
    c,s,a,store=rollout(private_env,monkeypatch,{'cartoon'})
    generator=AsyncMock(side_effect=AssertionError('No paid image request'))
    monkeypatch.setattr(image_service,'_generate_and_store',generator)
    with pm.media_scope(a),pytest.raises(pm.MediaProviderBlocked):await image_service.generate_cartoon_avatar('https://synthetic.invalid/portrait.png','mode_mother')
    generator.assert_not_awaited()


async def test_musical_private_paid_flow_retains_and_delivers_without_public_upload(private_env,credit_db,monkeypatch):
    import io,wave
    import media_service
    import musical_orders
    from test_private_media_integration import png
    c,s,a,store=rollout(private_env,monkeypatch,{'musical_card'})
    output=io.BytesIO()
    with wave.open(output,'wb') as w:w.setnchannels(1);w.setsampwidth(2);w.setframerate(8000);w.writeframes(b'\0\0'*1600)
    with pm.media_scope(a):
        chosen=await policy.bind_operation('musical_card',1,'music_synthetic_e2e')
        with policy.policy_scope(chosen):
            image=pm.store_bytes(c,png(),'image/png',purpose='image')
            audio=pm.store_bytes(c,output.getvalue(),'audio/wav',purpose='audio')
            await credit_db.create_musical_order('music_synthetic_e2e',1,image,audio,'direct')
    assert await credit_db.mark_musical_paid('music_synthetic_e2e','synthetic-payment')
    legacy=SimpleNamespace(close=Mock(),put_object=Mock(side_effect=AssertionError('No public upload')))
    monkeypatch.setattr(media_service,'_storage_client',lambda c:legacy)
    bot=SimpleNamespace(upload_media=AsyncMock(return_value=SimpleNamespace(payload=SimpleNamespace(token='synthetic-media-token'))),send_message=AsyncMock(return_value=SimpleNamespace(body=SimpleNamespace(mid='synthetic'))))
    worker=musical_orders.PaidMusicalWorker(bot,renderer=media_service.create_musical_video)
    await worker.process('music_synthetic_e2e')
    row=await credit_db.get_musical_order('music_synthetic_e2e')
    assert row['status']=='paid' and row['delivered']==1 and pm.is_private(row['result_url'])
    assert policy.PolicyStore(c).load(a.owner,'music_synthetic_e2e').mode=='private'
    before=s.puts
    await worker.process('music_synthetic_e2e')
    assert s.puts==before
    legacy.put_object.assert_not_called()


async def test_retained_download_bypasses_product_policy_but_keeps_authenticated_scope(private_env,credit_db,monkeypatch):
    c,s,a,store=private_env
    store.put(b'synthetic','text/plain',a,operation_id='reader')
    context=SimpleNamespace(get_data=AsyncMock(side_effect=AssertionError('Reader must not resume generation policy')))
    event=SimpleNamespace(get_ids=lambda:(123,1),bot=SimpleNamespace(send_message=AsyncMock()),callback=SimpleNamespace(payload='private_media_download:'+'a'*32),message=SimpleNamespace(recipient=SimpleNamespace(chat_type='dialog')))
    async def handler(event,data):return pm.current_access().owner
    assert await pm.MediaScopeMiddleware()(handler,event,{'_memory_context':context})==a.owner
    context.get_data.assert_not_awaited()

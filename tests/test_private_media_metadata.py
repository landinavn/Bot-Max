"""Metadata casing, strict digests and read-only diagnostics on invented data."""
from copy import deepcopy
import hashlib
import io
import json
from pathlib import Path
from unittest.mock import Mock

import pytest

import private_media as pm
from private_media_integrity import metadata_sha256,verify_s3_integrity,S3IntegrityError
from test_private_media import private_env
from test_private_media_migration import legacy
from test_yandex_media_profile import yandex_env,SmokeS3,SmokeHTTP
import private_media_yandex_smoke as smoke

DIGEST=hashlib.sha256(b'synthetic bytes').hexdigest()


@pytest.mark.parametrize('key',['sha256','Sha256','SHA256','sHa256','shA256'])
def test_metadata_key_case_only_is_normalized(key):
    assert metadata_sha256({key:DIGEST,'other':'synthetic'})==DIGEST
    verify_s3_integrity({'ContentLength':15,'Metadata':{key:DIGEST}},15,DIGEST)


def test_digest_value_is_returned_without_lowercase_or_strip():
    assert metadata_sha256({'Sha256':DIGEST.upper()})==DIGEST.upper()
    with pytest.raises(S3IntegrityError):verify_s3_integrity({'ContentLength':1,'Metadata':{'Sha256':DIGEST.upper()}},1,DIGEST)


@pytest.mark.parametrize('metadata',[
    None,{},[],{'ETag':DIGEST},{'sha256':None},{'sha256':123},{'sha256':b'a'*64},
    {'sha256':'a'*63},{'sha256':'g'*64},{'sha256':'a'*64+' '},{'sha256':' '+'a'*64},
    {'sha256':'\n'+'a'*64},{'sha256':'a'*64+'\n'},{'sha256':DIGEST,'other':None},
    {1:'synthetic','Sha256':DIGEST},{'Sha256':DIGEST,'sha256':DIGEST},
    {'SHA256':DIGEST,'sHa256':'0'*64},
])
def test_missing_malformed_and_duplicate_metadata_fails_closed(metadata):
    with pytest.raises(S3IntegrityError):metadata_sha256(metadata)


@pytest.mark.parametrize('head',[
    {},None,{'ContentLength':16,'Metadata':{'Sha256':DIGEST}},
    {'ContentLength':True,'Metadata':{'Sha256':DIGEST}},
    {'ContentLength':15,'Metadata':{'Sha256':'0'*64}},
    {'ContentLength':15,'ETag':DIGEST},
])
def test_length_digest_and_no_etag_fallback(head):
    with pytest.raises(S3IntegrityError):verify_s3_integrity(head,15,DIGEST)


def upper_head(backend,monkeypatch):
    original=backend.head_object
    def head(*args,**kw):
        r=original(*args,**kw);r['Metadata']={k.capitalize():v for k,v in r['Metadata'].items()};return r
    monkeypatch.setattr(backend,'head_object',head)


def test_put_read_stat_and_presign_accept_actual_sdk_casing(private_env,monkeypatch):
    c,s,a,store=private_env;upper_head(s,monkeypatch)
    reference=store.put(b'synthetic bytes','text/plain',a,operation_id='metadata-case')
    assert store.read(reference,a)[0]==b'synthetic bytes'
    assert store.stat(reference,a)['size']==15
    assert store.presign(reference,a).url.startswith('https:')
    assert s.puts==1


def test_unknown_put_recovers_with_uppercase_metadata_without_second_write(private_env,monkeypatch):
    c,s,a,store=private_env;upper_head(s,monkeypatch);s.accept_then_timeout=True
    with pytest.raises(pm.MediaWriteUncertain):store.put(b'synthetic bytes','text/plain',a,operation_id='recover-case')
    s.accept_then_timeout=False
    reference=store.put(b'synthetic bytes','text/plain',a,operation_id='recover-case')
    assert s.puts==1 and store.read(reference,a)[0]==b'synthetic bytes'


def test_unknown_put_with_ambiguous_metadata_never_retries(private_env,monkeypatch):
    c,s,a,store=private_env;s.accept_then_timeout=True
    with pytest.raises(pm.MediaWriteUncertain):store.put(b'synthetic bytes','text/plain',a,operation_id='ambiguous')
    original=s.head_object
    def head(**kw):
        result=original(**kw);result['Metadata']={'Sha256':DIGEST,'sha256':DIGEST};return result
    monkeypatch.setattr(s,'head_object',head)
    with pytest.raises(pm.MediaUnavailable):store.put(b'synthetic bytes','text/plain',a,operation_id='ambiguous')
    assert s.puts==1


@pytest.mark.parametrize('problem',['missing','duplicate','wrong-size','bad-content','bad-digest'])
def test_get_integrity_checks_metadata_size_and_actual_bytes(private_env,monkeypatch,tmp_path,problem):
    c,s,a,store=private_env
    reference=store.put(b'synthetic bytes','text/plain',a,operation_id='get-case')
    body=io.BytesIO(b'corrupted bytes' if problem=='bad-content' else b'synthetic bytes')
    response={'Body':body,'ContentLength':15,'Metadata':{'Sha256':DIGEST}}
    if problem=='missing':response.pop('Metadata')
    if problem=='duplicate':response['Metadata']['sha256']=DIGEST
    if problem=='wrong-size':response['ContentLength']=14
    if problem=='bad-digest':response['Metadata']['Sha256']='0'*64
    monkeypatch.setattr(s,'get_object',lambda **kw:response)
    path=tmp_path/'new-target'
    with pytest.raises(pm.MediaUnavailable):store.download_file(reference,a,path)
    assert not path.exists() and body.closed and s.puts==1


def test_migration_private_copy_uses_shared_metadata_verification(legacy,monkeypatch):
    import private_media_migration as migration
    c,s,a,store,db,url=legacy;upper_head(s,monkeypatch)
    item=migration.inventory(db,c)[0];before=db.read_bytes()
    reference=migration.copy_one(db,c,item,s,store)
    assert migration.copy_one(db,c,item,s,store)==reference
    assert store.read(reference,a)[0]==b'synthetic image'
    assert s.puts==1 and db.read_bytes()==before


def seeded(yandex_env):
    c,s,a,store=yandex_env;runtime=SmokeS3(s.policy)
    key='media/v1/smoke/'+'a'*32+'.txt'
    runtime.data[key]={'Body':smoke.PAYLOAD,'Metadata':{'Sha256':hashlib.sha256(smoke.PAYLOAD).hexdigest()}}
    source={'bucket':c.private_media_bucket,'objects':[{'key':key,'state':'accepted'}],'result':'incomplete'}
    return c,runtime,SmokeHTTP(runtime),source


def test_readonly_resume_of_112_record_never_puts_or_modifies_objects(yandex_env):
    c,runtime,http,source=seeded(yandex_env);before=deepcopy(runtime.data);records=[]
    legacy=SmokeS3(runtime.profile,legacy=True)
    result=smoke.resume_smoke(c,runtime,http,source,save=lambda r:records.append(deepcopy(r)),
        sleep=lambda n:setattr(http,'expired',True),legacy_clients={'bot-storage-user':legacy},
        legacy_unavailable={'speechkit-user':'no_static_credentials'})
    assert result=='partial-read-only' and runtime.puts==[] and legacy.puts==[] and runtime.data==before
    assert records[-1]['stages']['put']['status']=='skipped-read-only'
    assert records[-1]['stages']['out_of_prefix_put']['status']=='skipped-read-only'
    assert records[-1]['legacy']['bot-storage-user']=={'status':'passed','scope':'head-get'}
    assert records[-1]['legacy']['speechkit-user']['reason']=='no_static_credentials'
    assert source['result']=='incomplete' and 'stages' not in source


@pytest.mark.parametrize('key',['media/v1/customer.txt','media/v1/smoke/../../customer.txt',
    'https://untrusted.invalid/file','media/v1/smoke/'+'b'*32+'.png'])
def test_resume_rejects_arbitrary_object_sources_before_cloud(yandex_env,key):
    c,runtime,http,source=seeded(yandex_env);source['objects'][0]['key']=key
    client=Mock(side_effect=AssertionError('No cloud'));client.get_bucket_acl=Mock(side_effect=AssertionError('No cloud'))
    with pytest.raises(smoke.SmokeFailure):smoke.resume_smoke(c,client,http,source,save=Mock(),sleep=Mock())
    client.get_bucket_acl.assert_not_called()


@pytest.mark.parametrize('problem',['public','symlink','foreign-bucket','oversized','duplicate-json','wrong-version'])
def test_resume_record_file_validation(yandex_env,tmp_path,problem):
    c,runtime,http,source=seeded(yandex_env);path=tmp_path/'source.json'
    if problem=='foreign-bucket':source['bucket']='synthetic-other'
    if problem=='wrong-version':source['version']=999
    path.write_text(json.dumps(source));path.chmod(0o600)
    if problem=='public':path.chmod(0o644)
    if problem=='oversized':path.write_bytes(b' '*65537)
    if problem=='duplicate-json':path.write_text('{"bucket":"a","bucket":"b"}')
    if problem=='symlink':
        original=tmp_path/'original';path.rename(original);path.symlink_to(original)
    with pytest.raises(smoke.SmokeFailure):smoke.load_record(path,c.private_media_bucket)


def test_112_record_is_loaded_readonly_unchanged(yandex_env,tmp_path):
    c,runtime,http,source=seeded(yandex_env);path=tmp_path/'record.json'
    path.write_text(json.dumps(source));path.chmod(0o600);before=path.read_bytes()
    assert smoke.load_record(path,c.private_media_bucket)==source and path.read_bytes()==before


@pytest.mark.parametrize('mode',['partial','first-failed','second-only'])
def test_legacy_accounts_are_independent_and_partial_is_never_passed(yandex_env,mode):
    c,s,a,store=yandex_env;runtime=SmokeS3(s.policy);http=SmokeHTTP(runtime);records=[]
    first=SmokeS3(s.policy,legacy=True);second=SmokeS3(s.policy,legacy=True)
    clients={'bot-storage-user':first}
    if mode=='first-failed':first.head_object=lambda **kw:{};clients['speechkit-user']=second
    if mode=='second-only':clients={'speechkit-user':second}
    options=dict(save=lambda r:records.append(deepcopy(r)),sleep=lambda n:setattr(http,'expired',True),
        legacy_clients=clients,legacy_unavailable={'speechkit-user':'no_static_credentials'} if mode=='partial' else {})
    if mode=='first-failed':
        with pytest.raises(smoke.SmokeFailure):smoke.run_smoke(c,runtime,http,**options)
        assert records[-1]['legacy']['bot-storage-user']['status']=='failed'
        assert records[-1]['legacy']['speechkit-user']['status']=='passed'
    else:
        assert smoke.run_smoke(c,runtime,http,**options)=='partial-legacy-not-tested'
        assert records[-1]['stages']['legacy_probes']['status']=='partial'
        if mode=='partial':assert records[-1]['legacy']['speechkit-user']['reason']=='no_static_credentials'


def test_head_diagnostics_record_safe_metadata_error_and_preserve_unknown_put(yandex_env):
    c,s,a,store=yandex_env;runtime=SmokeS3(s.policy);http=SmokeHTTP(runtime);records=[];events=[]
    original=runtime.head_object
    def head(**kw):r=original(**kw);r['Metadata']={};return r
    runtime.head_object=head
    with pytest.raises(S3IntegrityError):smoke.run_smoke(c,runtime,http,save=lambda r:records.append(deepcopy(r)),report=events.append,sleep=Mock())
    assert records[-1]['stage']=='head_integrity'
    assert records[-1]['stages']['head_integrity']=={'status':'failed','error_code':'metadata_missing'}
    assert runtime.puts and records[-1]['objects'][0]['state']=='accepted'
    assert 'https:' not in json.dumps(events) and 'Metadata' not in json.dumps(events)


def test_cli_reports_stage_and_code_without_exception_response(yandex_env,tmp_path,monkeypatch,capsys):
    c,s,a,store=yandex_env;runtime=SmokeS3(s.policy)
    runtime.put_object=Mock(side_effect=TimeoutError('https://secret.invalid/signed?token=SECRET'))
    monkeypatch.setattr(smoke,'configuration',lambda:c)
    monkeypatch.setattr(pm,'storage_client',lambda config:runtime)
    import httpx
    monkeypatch.setattr(httpx,'Client',lambda **kw:SmokeHTTP(runtime))
    path=tmp_path/'result.json'
    assert smoke.main(['--execute','--confirm-bucket',c.private_media_bucket,'--record',str(path)])==1
    output=capsys.readouterr()
    assert 'SECRET' not in output.out+output.err and 'https:' not in output.out+output.err and 'Traceback' not in output.out+output.err
    last=json.loads(output.out.splitlines()[-1]);assert last['stage']=='put' and last['error_code']=='network_timeout'
    assert json.loads(path.read_text())['objects'][0]['state']=='submitting'
    runtime.put_object.assert_called_once()


def test_legacy_configuration_accepts_one_existing_key_without_creating_other(yandex_env,monkeypatch):
    c,s,a,store=yandex_env
    for key in list(smoke.os.environ):
        if key.startswith('PRIVATE_MEDIA_SMOKE_LEGACY_'):monkeypatch.delenv(key)
    for name,value in {'1_PRINCIPAL':'synthetic-old-a','1_ACCESS_KEY_ID':'synthetic-existing',
        '1_SECRET_ACCESS_KEY':'synthetic-secret','2_PRINCIPAL':'synthetic-old-b','2_NO_STATIC_CREDENTIALS':'true'}.items():
        monkeypatch.setenv('PRIVATE_MEDIA_SMOKE_LEGACY_'+name,value)
    configs,unavailable=smoke.legacy_configuration(c)
    assert set(configs)=={'bot-storage-user'} and unavailable=={'speechkit-user':'no_static_credentials'}


def test_cli_resume_keeps_existing_record_and_never_puts(yandex_env,tmp_path,monkeypatch,capsys):
    import httpx
    c,runtime,http,source=seeded(yandex_env);path=tmp_path/'old.json';output=tmp_path/'diagnostic.json'
    path.write_text(json.dumps(source));path.chmod(0o600);before=path.read_bytes()
    monkeypatch.setattr(smoke,'configuration',lambda:c)
    monkeypatch.setattr(pm,'storage_client',lambda config:runtime)
    monkeypatch.setattr(httpx,'Client',lambda **kw:http)
    original=smoke.resume_smoke
    monkeypatch.setattr(smoke,'resume_smoke',lambda *a,**kw:original(*a,**kw,sleep=lambda n:setattr(http,'expired',True)))
    assert smoke.main(['--confirm-bucket',c.private_media_bucket,'--record',str(output),'--resume-record',str(path)])==2
    assert runtime.puts==[] and path.read_bytes()==before and output.stat().st_mode&0o777==0o600
    record=json.loads(output.read_text());assert record['mode']=='read-only' and record['result']=='partial-read-only'
    assert record['stages']['put']['status']=='skipped-read-only'
    assert 'https:' not in capsys.readouterr().out


def test_resume_rejects_execute_flag_before_sdk(yandex_env,tmp_path,monkeypatch):
    c,runtime,http,source=seeded(yandex_env)
    monkeypatch.setattr(smoke,'configuration',lambda:c)
    factory=Mock(side_effect=AssertionError('No cloud'));monkeypatch.setattr(pm,'storage_client',factory)
    assert smoke.main(['--execute','--confirm-bucket',c.private_media_bucket,'--record',str(tmp_path/'out'),
        '--resume-record',str(tmp_path/'input')])==1
    factory.assert_not_called()

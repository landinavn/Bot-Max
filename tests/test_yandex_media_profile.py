"""Only invented identities, fake S3/HTTP, no external services or credentials."""
from copy import deepcopy
from dataclasses import replace
import io
import json
from types import SimpleNamespace
from unittest.mock import Mock

import pytest
from botocore.exceptions import ClientError

import private_media as pm
from test_private_media import private_env
from yandex_media_profile import validate_policy


def policy_document():
    bucket='arn:aws:s3:::synthetic-private'
    return {'Version':'2012-10-17','Statement':[
        {'Sid':'PrivateObjectIO','Effect':'Allow','Principal':{'CanonicalUser':'synthetic-runtime'},
         'Action':['s3:GetObject','s3:PutObject'],'Resource':bucket+'/media/v1/*',
         'Condition':{'Bool':{'aws:securetransport':'true'}}},
        {'Sid':'ReadBucketMetadata','Effect':'Allow','Principal':{'CanonicalUser':['synthetic-runtime','synthetic-operator']},
         'Action':['s3:GetBucketAcl','s3:ListBucket'],'Resource':bucket,
         'Condition':{'Bool':{'aws:securetransport':'true'}}},
        {'Sid':'DenyInsecureTransport','Effect':'Deny','Principal':'*','Action':'*',
         'Resource':[bucket,bucket+'/*'],'Condition':{'Bool':{'aws:securetransport':'false'}}},
        {'Sid':'DenyLegacyStorageAccess','Effect':'Deny','Principal':{'CanonicalUser':['synthetic-old-a','synthetic-old-b']},
         'Action':'*','Resource':[bucket,bucket+'/*'],'Condition':None}]}


@pytest.fixture
def yandex_env(private_env):
    c,s,a,old=private_env
    c=replace(c,private_media_profile='yandex-v1',private_media_yandex_folder_id='synthetic-folder',
              private_media_runtime_principal='synthetic-runtime',
              private_media_principals=frozenset({'synthetic-runtime','synthetic-operator'}),
              private_media_legacy_principals=frozenset({'synthetic-old-a','synthetic-old-b'}),
              private_media_yandex_controls_verified=True)
    s.get_bucket_acl=Mock(return_value={'Owner':{'ID':''},'Grants':[]})
    s.policy=policy_document()
    return c,s,a,pm.PrivateMedia(c,client=s)


@pytest.mark.parametrize('folder',['','synthetic-folder'])
@pytest.mark.parametrize('condition',[None,{},'absent'])
@pytest.mark.parametrize('action',['*','s3:*'])
def test_documented_yandex_acl_and_actual_policy_shape(yandex_env,folder,condition,action):
    c,s,a,store=yandex_env
    s.get_bucket_acl.return_value['Owner']['ID']=folder
    s.policy['Statement'][3]['Action']=action
    if condition=='absent':s.policy['Statement'][3].pop('Condition')
    else:s.policy['Statement'][3]['Condition']=condition
    store.preflight()
    assert not s.objects and s.puts==0


@pytest.mark.parametrize('key',['aws:SecureTransport','aws:securetransport','AWS:SECURETRANSPORT'])
@pytest.mark.parametrize('boolean',[False,True])
def test_tls_condition_case_preserves_boolean_semantics(yandex_env,key,boolean):
    c,s,a,store=yandex_env
    for rule in s.policy['Statement'][:3]:
        value=rule['Condition']['Bool']['aws:securetransport']
        rule['Condition']['Bool']={key:value=='true' if boolean else value}
    store.preflight()


@pytest.mark.parametrize('acl',[
    None,{},[],{'Owner':{},'Grants':[]},{'Owner':None,'Grants':[]},
    {'Owner':{'ID':None},'Grants':[]},{'Owner':{'ID':''}},
    {'Owner':{'ID':''},'Grants':None},{'Owner':{'ID':''},'Grants':{}},
    {'Owner':{'ID':'synthetic-unexpected-folder'},'Grants':[]},
    {'Owner':{'ID':''},'Grants':[],'Unexpected':True},
    {'Owner':{'ID':''},'Grants':[],'ResponseMetadata':{'HTTPStatusCode':500}},
    {'Owner':{'ID':''},'Grants':[{'Grantee':{'Type':'CanonicalUser','ID':'synthetic-unknown'},'Permission':'READ'}]},
    {'Owner':{'ID':''},'Grants':[{'Grantee':{'Type':'CanonicalUser','ID':'synthetic-runtime'},'Permission':'READ'}]},
    {'Owner':{'ID':''},'Grants':[{'Grantee':{'Type':'Group','URI':'http://acs.amazonaws.com/groups/global/AllUsers'},'Permission':'READ'}]},
    {'Owner':{'ID':''},'Grants':[{'Grantee':{'Type':'Group','URI':'http://acs.amazonaws.com/groups/global/AuthenticatedUsers'},'Permission':'READ'}]},
])
def test_missing_corrupt_or_nonempty_acl_is_not_private(yandex_env,acl):
    c,s,a,store=yandex_env;s.get_bucket_acl.return_value=acl
    with pytest.raises(pm.MediaUnavailable):store.preflight()
    assert s.puts==0


@pytest.mark.parametrize('change',[
    (0,'Principal','*'),(0,'Principal',{'CanonicalUser':'synthetic-other'}),
    (0,'Principal',{'AWS':'synthetic-runtime'}),(0,'Action','*'),
    (0,'Action',['s3:GetObject','s3:PutObject','s3:DeleteObject']),
    (0,'Resource','arn:aws:s3:::synthetic-private/*'),
    (0,'Resource','arn:aws:s3:::synthetic-other/media/v1/*'),
    (0,'Condition',None),(0,'Condition',{'Bool':{'aws:securetransport':'false'}}),
    (0,'Condition',{'Bool':{'aws:securetransport':'true','aws:SecureTransport':'false'}}),
    (0,'Condition',{'Bool':{'aws:securetransport':'true','aws:SecureTransport':'true'}}),
    (0,'Condition',{'Bool':{'aws:securetransport':['true','false']}}),
    (0,'Condition',{'Bool':{'aws:securetransport':'true'},'BoolIfExists':{'aws:securetransport':'true'}}),
    (0,'NotPrincipal',{'CanonicalUser':'synthetic-old-a'}),
    (1,'Principal',{'CanonicalUser':['synthetic-runtime','synthetic-unapproved']}),
    (1,'Action',['s3:GetBucketAcl','s3:ListBucket','s3:PutBucketPolicy']),
    (1,'Resource','arn:aws:s3:::synthetic-private/media/v1/*'),
    (2,'Condition',{'Bool':{'aws:securetransport':'true'}}),
    (2,'Principal',{'CanonicalUser':'synthetic-runtime'}),
    (2,'Action',['s3:GetObject']),(2,'Resource','arn:aws:s3:::synthetic-private/*'),
    (3,'Principal',{'CanonicalUser':['synthetic-old-a','synthetic-wrong-old']}),
    (3,'Principal',{'CanonicalUser':['synthetic-old-a']}),
    (3,'Condition',{'Bool':{'aws:securetransport':'false'}}),
    (3,'Resource','arn:aws:s3:::synthetic-private'),
])
def test_empty_acl_never_overrides_unsafe_policy(yandex_env,change):
    c,s,a,store=yandex_env;index,key,value=change;s.policy['Statement'][index][key]=deepcopy(value)
    with pytest.raises(pm.MediaUnavailable):store.preflight()
    assert s.puts==0


@pytest.mark.parametrize('index',range(4))
def test_each_mandatory_rule_is_required(yandex_env,index):
    c,s,a,store=yandex_env;s.policy['Statement'].pop(index)
    with pytest.raises(pm.MediaUnavailable):store.preflight()


@pytest.mark.parametrize('raw',[None,{}, {'Policy':'{}'}, {'Policy':'not-json'},
    {'Policy':'{"Version":"2012-10-17","Statement":[],"Statement":[]}'},
    {'Policy':'{"Version":"2012-10-17","Statement":null}'}])
def test_missing_corrupt_policy_is_rejected(yandex_env,raw):
    c,s,a,store=yandex_env;s.get_bucket_policy=Mock(return_value=raw)
    with pytest.raises(pm.MediaUnavailable):store.preflight()


def test_duplicate_json_condition_is_rejected_before_normalization(yandex_env):
    c,s,a,store=yandex_env
    raw=json.dumps(s.policy).replace('"aws:securetransport": "true"',
        '"aws:securetransport": "true", "aws:securetransport": "true"',1)
    s.get_bucket_policy=Mock(return_value={'Policy':raw})
    with pytest.raises(pm.MediaUnavailable):store.preflight()


@pytest.mark.parametrize('fields',[
    {'private_media_yandex_controls_verified':False},
    {'private_media_yandex_folder_id':None},
    {'private_media_runtime_principal':'synthetic-unknown'},
    {'private_media_legacy_principals':frozenset()},
    {'private_media_legacy_principals':frozenset({'synthetic-old-a','synthetic-runtime'})},
    {'private_media_principals':frozenset({'synthetic-runtime'})},
    {'storage_endpoint_url':'http://storage.yandexcloud.net'},
    {'storage_endpoint_url':'https://untrusted.invalid'},
    {'storage_endpoint_url':'https://storage.yandexcloud.net/path'},
    {'private_media_yandex_folder_id':'synthetic-runtime'},
])
def test_operator_identity_and_controls_confirmation_required_before_sdk(yandex_env,fields):
    c,s,a,store=yandex_env;store=pm.PrivateMedia(replace(c,**fields),client=s)
    with pytest.raises(pm.MediaUnavailable):store.preflight()
    s.get_bucket_acl.assert_not_called()


def test_yandex_empty_acl_is_not_a_global_bypass(yandex_env):
    c,s,a,store=yandex_env
    with pytest.raises(pm.MediaUnavailable):pm.PrivateMedia(replace(c,private_media_profile='strict'),client=s).preflight()


class SmokeS3:
    def __init__(self,profile,*,legacy=False):self.profile=profile;self.legacy=legacy;self.data={};self.puts=[];self.signatures=0
    def get_bucket_acl(self,**kw):return {'Owner':{'ID':''},'Grants':[]}
    def get_bucket_policy(self,**kw):return {'Policy':json.dumps(self.profile)}
    def deny(self):raise ClientError({'Error':{'Code':'AccessDenied'},'ResponseMetadata':{'HTTPStatusCode':403}},'Synthetic')
    def put_object(self,**kw):
        self.puts.append(kw['Key'])
        if self.legacy or not kw['Key'].startswith('media/v1/'):self.deny()
        self.data[kw['Key']]=kw
    def head_object(self,**kw):
        if self.legacy:self.deny()
        d=self.data[kw['Key']];return {'ContentLength':len(d['Body']),'Metadata':d['Metadata']}
    def get_object(self,**kw):
        if self.legacy:self.deny()
        return {**self.head_object(**kw),'Body':io.BytesIO(self.data[kw['Key']]['Body'])}
    def generate_presigned_url(self,*args,**kw):
        self.signatures+=1
        return 'https://storage.yandexcloud.net/synthetic-private/'+kw['Params']['Key']+'?synthetic-signature='+str(self.signatures)
    def close(self):pass


class SmokeHTTP:
    def __init__(self,s3):self.s3=s3;self.expired=False;self.calls=[]
    def request(self,method,url):
        self.calls.append((method,url))
        signed='?' in url
        payload=next(iter(self.s3.data.values()))['Body']
        fresh=signed and int(url.rsplit('=',1)[-1])>2
        return SimpleNamespace(status_code=200 if signed and (not self.expired or fresh) else 403,content=payload)
    def __enter__(self):return self
    def __exit__(self,*args):pass


def test_owner_smoke_all_checks_are_synthetic_and_do_not_touch_registry(yandex_env):
    from private_media_yandex_smoke import run_smoke
    c,s,a,store=yandex_env;runtime=SmokeS3(s.policy);http=SmokeHTTP(runtime);records=[]
    legacy=[SmokeS3(s.policy,legacy=True),SmokeS3(s.policy,legacy=True)]
    result=run_smoke(c,runtime,http,save=lambda r:records.append(deepcopy(r)),
        sleep=lambda n:setattr(http,'expired',True),legacy_clients=legacy)
    assert result=='passed' and not c.private_media_registry.exists()
    assert len(runtime.puts)==2 and all(len(x.puts)==1 for x in legacy)
    assert len(http.calls)==8
    assert all(url.startswith('https://storage.yandexcloud.net/') for _,url in http.calls)
    assert 'synthetic-signature' not in json.dumps(records)
    assert records[-1]['objects'][0]['state']=='accepted'


def test_smoke_unknown_put_is_never_retried(yandex_env):
    from private_media_yandex_smoke import run_smoke
    c,s,a,store=yandex_env;runtime=SmokeS3(s.policy);records=[]
    runtime.put_object=Mock(side_effect=TimeoutError('synthetic secret response'))
    with pytest.raises(TimeoutError):run_smoke(c,runtime,Mock(),save=lambda r:records.append(deepcopy(r)),sleep=Mock())
    runtime.put_object.assert_called_once()
    assert records[-1]['objects'][0]['state']=='submitting'


@pytest.mark.parametrize('failure',['anonymous-public','anonymous-404','expiry-200','redirect','legacy-permitted','outside-permitted','fresh-denied'])
def test_smoke_negative_access_checks_fail_closed(yandex_env,failure):
    from private_media_yandex_smoke import run_smoke
    c,s,a,store=yandex_env;runtime=SmokeS3(s.policy);http=SmokeHTTP(runtime);original=http.request
    def request(method,url):
        if failure.startswith('anonymous') and '?' not in url:return SimpleNamespace(status_code=200 if failure.endswith('public') else 404)
        if failure=='redirect':return SimpleNamespace(status_code=302)
        if failure=='expiry-200' and http.expired:return SimpleNamespace(status_code=200)
        if failure=='fresh-denied' and http.expired and '?' in url and int(url.rsplit('=',1)[-1])>2:return SimpleNamespace(status_code=403)
        return original(method,url)
    http.request=request
    legacy=[SmokeS3(s.policy,legacy=True),SmokeS3(s.policy,legacy=True)]
    if failure=='legacy-permitted':legacy[0].head_object=lambda **kw:{}
    if failure=='outside-permitted':
        put=runtime.put_object
        runtime.put_object=lambda **kw:put(**kw) if kw['Key'].startswith('media/v1/') else None
    with pytest.raises(RuntimeError):run_smoke(c,runtime,http,save=lambda r:None,
        sleep=lambda n:setattr(http,'expired',True),legacy_clients=legacy)


def test_profile_and_private_flags_are_disabled_by_default(tmp_path,monkeypatch):
    from config import Config
    c=Config(token='synthetic',photo_path=tmp_path/'unused')
    assert c.private_media_profile=='strict' and not c.private_media_yandex_controls_verified
    assert not c.private_media_enabled and not c.private_media_products and not c.private_media_storage_verified


@pytest.mark.parametrize('name,value',[
    ('PRIVATE_MEDIA_PROFILE','unknown'),('PRIVATE_MEDIA_YANDEX_CONTROLS_VERIFIED','yes'),
    ('PRIVATE_MEDIA_YANDEX_FOLDER_ID','invalid folder'),
    ('PRIVATE_MEDIA_RUNTIME_PRINCIPAL','invalid/runtime'),
    ('PRIVATE_MEDIA_LEGACY_PRINCIPALS','synthetic-old,invalid subject')])
def test_invalid_profile_environment_is_rejected_without_dotenv(name,value,monkeypatch):
    import config
    monkeypatch.setattr(config,'load_dotenv',Mock())
    monkeypatch.setenv('MAX_BOT_TOKEN','synthetic')
    monkeypatch.setenv(name,value)
    with pytest.raises(ValueError):config.Config.from_env()


def test_smoke_configuration_never_loads_bot_dotenv(yandex_env,monkeypatch):
    import private_media_yandex_smoke as smoke
    from config import Config
    c,s,a,store=yandex_env
    monkeypatch.setattr(Config,'from_env',Mock(side_effect=AssertionError('No bot configuration')))
    for name,value in {'PRIVATE_MEDIA_PROFILE':'yandex-v1','PRIVATE_MEDIA_BUCKET':'synthetic-private',
        'PRIVATE_MEDIA_YANDEX_FOLDER_ID':'synthetic-folder','PRIVATE_MEDIA_RUNTIME_PRINCIPAL':'synthetic-runtime',
        'PRIVATE_MEDIA_PRINCIPALS':'synthetic-runtime,synthetic-operator',
        'PRIVATE_MEDIA_LEGACY_PRINCIPALS':'synthetic-old-a,synthetic-old-b',
        'PRIVATE_MEDIA_YANDEX_CONTROLS_VERIFIED':'true'}.items():monkeypatch.setenv(name,value)
    selected=smoke.configuration()
    assert selected.private_media_profile=='yandex-v1' and not selected.private_media_enabled
    assert selected.private_media_products==frozenset() and not selected.private_media_storage_verified
    Config.from_env.assert_not_called()


def test_smoke_cli_has_strict_tls_private_record_and_no_secret_output(yandex_env,tmp_path,monkeypatch,capsys):
    import private_media_yandex_smoke as smoke
    import httpx,logging
    c,s,a,store=yandex_env;runtime=SmokeS3(s.policy);http=SmokeHTTP(runtime)
    monkeypatch.setattr(smoke,'configuration',lambda:c)
    monkeypatch.setattr(pm,'storage_client',lambda config:runtime)
    factory=Mock(return_value=http);monkeypatch.setattr(httpx,'Client',factory)
    original=smoke.run_smoke
    monkeypatch.setattr(smoke,'run_smoke',lambda *a,**kw:original(*a,**kw,sleep=lambda n:setattr(http,'expired',True)))
    record=tmp_path/'smoke-record.json';before=logging.root.manager.disable
    assert smoke.main(['--execute','--confirm-bucket','synthetic-private','--record',str(record)])==2
    assert record.stat().st_mode&0o777==0o600
    assert json.loads(record.read_text())['result']=='partial-legacy-not-tested'
    factory.assert_called_once_with(verify=True,trust_env=False,follow_redirects=False,timeout=10)
    assert logging.root.manager.disable==before
    output=capsys.readouterr().out
    assert 'https:' not in output and 'synthetic-signature' not in output and 'synthetic-secret' not in output


@pytest.mark.parametrize('execute,confirmation,exists',[(False,'synthetic-private',False),(True,'wrong-bucket',False),(True,'synthetic-private',True)])
def test_cli_requires_explicit_confirmation_and_exclusive_record(yandex_env,tmp_path,monkeypatch,execute,confirmation,exists):
    import private_media_yandex_smoke as smoke
    c,s,a,store=yandex_env;monkeypatch.setattr(smoke,'configuration',lambda:c)
    factory=Mock(side_effect=AssertionError('No network'));monkeypatch.setattr(pm,'storage_client',factory)
    record=tmp_path/'record.json'
    if exists:record.write_text('keep')
    args=['--confirm-bucket',confirmation,'--record',str(record)]
    if execute:args.append('--execute')
    assert smoke.main(args)==1
    factory.assert_not_called()
    if exists:assert record.read_text()=='keep'

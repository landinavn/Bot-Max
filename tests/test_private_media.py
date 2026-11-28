"""Synthetic private S3, actual offline SigV4 signer; no cloud/paid APIs."""
import hashlib
import hmac
import io
import json
import os
import sqlite3
import time
from dataclasses import replace
from datetime import datetime, timezone
from types import SimpleNamespace
from urllib.parse import urlsplit, parse_qsl, quote
from unittest.mock import AsyncMock

import boto3
from botocore.config import Config as BotoConfig
from botocore.exceptions import ClientError
import pytest

from config import Config
import private_media as pm


class SyntheticS3:
    """Private-only mock backend. Anonymous GET is denied, SigV4 checked locally."""
    secret = 'synthetic-secret-not-production'
    def __init__(self):
        self.objects = {}; self.puts = 0; self.heads = 0; self.gets = 0
        self.unavailable = False; self.accept_then_timeout = False
        self.public_acl = False; self.policy = None
        self.now = int(time.time())
        self.signer = boto3.client('s3', endpoint_url='https://storage.invalid',
            region_name='test-region', aws_access_key_id='synthetic-key',
            aws_secret_access_key=self.secret,
            config=BotoConfig(signature_version='s3v4',s3={'addressing_style':'path'}))

    def head_object(self, Bucket, Key):
        self.heads += 1
        if self.unavailable: raise OSError('synthetic-private-response')
        if (Bucket,Key) not in self.objects:
            raise ClientError({'Error':{'Code':'404'}},'HeadObject')
        data, mime, metadata = self.objects[Bucket,Key]
        return {'ContentLength':len(data),'ContentType':mime,'Metadata':metadata}

    def get_bucket_acl(self, Bucket):
        assert Bucket=='synthetic-private'
        grantee={'Type':'Group','URI':'http://acs.amazonaws.com/groups/global/AllUsers'} if self.public_acl else {'Type':'CanonicalUser','ID':'synthetic-owner'}
        return {'Owner':{'ID':'synthetic-owner'},'Grants':[{'Grantee':grantee,'Permission':'FULL_CONTROL'}]}

    def get_bucket_policy(self, Bucket):
        assert Bucket=='synthetic-private'
        if self.policy is None:raise ClientError({'Error':{'Code':'NoSuchBucketPolicy'}},'GetBucketPolicy')
        return {'Policy':json.dumps(self.policy)}

    def put_object(self, **kw):
        assert kw['Bucket'] == 'synthetic-private'
        assert 'ACL' not in kw  # Never public-read; private bucket is a deployment prerequisite.
        self.puts += 1
        self.objects[kw['Bucket'],kw['Key']] = (kw['Body'],kw['ContentType'],kw['Metadata'])
        if self.accept_then_timeout: raise TimeoutError('synthetic signed URL in response')

    def get_object(self, Bucket, Key):
        self.gets += 1
        self.head_object(Bucket,Key)
        return {'Body':io.BytesIO(self.objects[Bucket,Key][0])}

    def generate_presigned_url(self,*args,**kwargs):
        return self.signer.generate_presigned_url(*args,**kwargs)

    def anonymous_or_signed_get(self,url):
        parsed=urlsplit(url); q=dict(parse_qsl(parsed.query))
        if 'X-Amz-Signature' not in q: raise PermissionError('anonymous denied')
        stamp=datetime.strptime(q['X-Amz-Date'],'%Y%m%dT%H%M%SZ').replace(tzinfo=timezone.utc).timestamp()
        if self.now >= stamp+int(q['X-Amz-Expires']): raise PermissionError('expired')
        signature=q.pop('X-Amz-Signature')
        canonical='&'.join(quote(k,safe='-_.~')+'='+quote(v,safe='-_.~') for k,v in sorted(q.items()))
        request='GET\n'+parsed.path+'\n'+canonical+'\nhost:'+parsed.netloc+'\n\nhost\nUNSIGNED-PAYLOAD'
        scope=q['X-Amz-Credential'].split('/',1)[1]
        text='AWS4-HMAC-SHA256\n'+q['X-Amz-Date']+'\n'+scope+'\n'+hashlib.sha256(request.encode()).hexdigest()
        key=('AWS4'+self.secret).encode()
        for part in scope.split('/'):key=hmac.new(key,part.encode(),hashlib.sha256).digest()
        expected=hmac.new(key,text.encode(),hashlib.sha256).hexdigest()
        if not hmac.compare_digest(signature,expected):raise PermissionError('invalid signature')
        bucket,object_key=parsed.path.lstrip('/').split('/',1)
        return self.objects[bucket,object_key][0]

    def close(self): pass


@pytest.fixture
def private_env(tmp_path, monkeypatch):
    c=Config(token='synthetic-max',photo_path=tmp_path/'unused',storage_bucket='synthetic-legacy',
        private_media_enabled=True,private_media_bucket='synthetic-private',
        private_media_storage_verified=True,
        private_media_registry=tmp_path/'private.sqlite3',
        private_media_access_key_id='synthetic-key',private_media_secret_access_key='synthetic-secret',
        private_media_principals=frozenset({'synthetic-owner'}),
        admin_user_id=99)
    backend=SyntheticS3()
    monkeypatch.setattr(Config,'from_env',classmethod(lambda cls:c))
    monkeypatch.setenv('PRIVATE_MEDIA_REGISTRY',str(c.private_media_registry))
    monkeypatch.setattr(pm,'storage_client',lambda config:backend)
    actor=pm.Access(pm.Owner('max','1'))
    return c,backend,actor,pm.PrivateMedia(c,client=backend,clock=lambda:backend.now)


def test_private_roundtrip_signed_get_and_expiry(private_env):
    c,s,a,store=private_env
    ref=store.put(b'synthetic photo','image/png',a,operation_id='one')
    assert pm.is_private(ref) and not ref.startswith('https:')
    bucket,key=next(iter(s.objects))
    assert store.read(ref,a)==(b'synthetic photo','image/png')
    with pytest.raises(PermissionError):s.anonymous_or_signed_get('https://storage.invalid/'+bucket+'/'+key)
    signed=store.presign(ref,a,ttl=5)
    assert s.anonymous_or_signed_get(signed.url)==b'synthetic photo'
    assert 'X-Amz-Signature' not in repr(signed) and 'X-Amz' not in str(signed)
    with pytest.raises(PermissionError):s.anonymous_or_signed_get(signed.url.replace('X-Amz-Signature=','X-Amz-Signature=bad'))
    s.now+=6
    with pytest.raises(PermissionError):s.anonymous_or_signed_get(signed.url)
    assert os.stat(c.private_media_registry).st_mode & 0o777 == 0o600


@pytest.mark.parametrize('access',[
    pm.Access(pm.Owner('max','2')),pm.Access(pm.Owner('account','1')),
    pm.Access(None,'admin',admin_id=98,reason='support'),
    pm.Access(None,'admin',admin_id=99,reason='arbitrary'),
    pm.Access(pm.Owner('max','1'),'service','different-order'),
])
def test_foreign_or_wrong_scope_is_rejected_before_s3(private_env,access):
    c,s,a,store=private_env
    ref=store.put(b'data','image/png',a,operation_id='one')
    heads=s.heads
    with pytest.raises(pm.MediaAccessDenied):store.read(ref,access)
    with pytest.raises(pm.MediaAccessDenied):store.presign(ref,access)
    assert s.heads==heads and s.gets==0


def test_owner_order_admin_and_explicit_service_reference(private_env):
    c,s,a,store=private_env
    service=pm.Access(a.owner,'service','order-1')
    ref=store.put(b'data','text/plain',service,operation_id='one')
    assert store.read(ref,service)[0]==b'data'
    assert store.read(ref,a)[0]==b'data'
    assert store.read(ref,pm.Access(None,'admin',admin_id=99,reason='recovery'))[0]==b'data'
    allowed=pm.Access(a.owner,'service','order-2',references=frozenset([ref]))
    assert store.read(ref,allowed)[0]==b'data'
    with pytest.raises(pm.MediaAccessDenied):store.put(b'bad','text/plain',pm.Access(None,'admin',admin_id=99,reason='support'),operation_id='two')


def test_restart_reuses_stable_key_without_put_and_write_flag_can_be_off(private_env):
    c,s,a,store=private_env
    ref=store.put(b'data','audio/mpeg',a,operation_id='one')
    restarted=pm.PrivateMedia(c,client=s)
    assert restarted.put(b'data','audio/mpeg',a,operation_id='one')==ref
    assert s.puts==1
    with pytest.raises(pm.MediaError):restarted.put(b'different','audio/mpeg',a,operation_id='one')
    disabled=pm.PrivateMedia(replace(c,private_media_enabled=False),client=s)
    assert disabled.read(ref,a)[0]==b'data'
    with pytest.raises(pm.MediaError):disabled.put(b'data','audio/mpeg',a,operation_id='two')


def test_unknown_put_is_not_repeated_and_accepted_result_recovers(private_env):
    c,s,a,store=private_env
    s.accept_then_timeout=True
    with pytest.raises(pm.MediaWriteUncertain):store.put(b'data','video/mp4',a,operation_id='one')
    s.accept_then_timeout=False
    ref=pm.PrivateMedia(c,client=s).put(b'data','video/mp4',a,operation_id='one')
    assert store.read(ref,a)[0]==b'data' and s.puts==1


def test_missing_uncertain_object_is_not_recreated(private_env):
    c,s,a,store=private_env
    s.accept_then_timeout=True
    with pytest.raises(pm.MediaWriteUncertain):store.put(b'data','video/mp4',a,operation_id='one')
    s.objects.clear()
    with pytest.raises(pm.MediaWriteUncertain):pm.PrivateMedia(c,client=s).put(b'data','video/mp4',a,operation_id='one')
    assert s.puts==1


def test_failure_integrity_limits_and_no_signed_urls_in_registry(private_env,caplog):
    c,s,a,store=private_env
    ref=store.put(b'data','text/plain',a,operation_id='one')
    signed=store.presign(ref,a)
    import logging
    with caplog.at_level('ERROR'):
        logging.getLogger('unknown').error('unsafe %s',signed.url,extra={'payload':signed.url},exc_info=True)
    assert signed.url not in caplog.text and 'X-Amz-Signature' not in caplog.text
    assert 'X-Amz' not in c.private_media_registry.read_bytes().decode('latin1')
    s.unavailable=True
    with pytest.raises(pm.MediaUnavailable):store.read(ref,a)
    s.unavailable=False
    with pytest.raises(pm.MediaUnavailable):store.read(ref,a,limit=1)
    bucket,key=next(iter(s.objects));data,mime,metadata=s.objects[bucket,key]
    s.objects[bucket,key]=(b'evil',mime,metadata)
    with pytest.raises(pm.MediaUnavailable):store.read(ref,a)


def test_voluntary_recipient_grant_is_explicit_bounded_and_revocable(private_env):
    c,s,a,store=private_env
    ref=store.put(b'data','image/png',a,operation_id='one')
    recipient=pm.Owner('max','2');other=pm.Access(recipient)
    with pytest.raises(pm.MediaAccessDenied):store.read(ref,other)
    store.grant(ref,a,recipient,expires_at=s.now+5)
    assert store.read(ref,other)[0]==b'data'
    assert store.presign(ref,other,ttl=300).expires_at <= s.now+5
    store.revoke(ref,a,recipient)
    with pytest.raises(pm.MediaAccessDenied):store.read(ref,other)
    store.grant(ref,a,recipient,expires_at=s.now+5);s.now+=6
    with pytest.raises(pm.MediaAccessDenied):store.read(ref,other)


@pytest.mark.parametrize('kind,mime,data',[
    ('image','image/png',None),('audio','audio/mpeg',None),
    ('video','video/mp4',None),('file','text/plain',b'synthetic document')])
async def test_max_uses_authenticated_bytes_not_public_url(private_env,tmp_path,kind,mime,data):
    c,s,a,store=private_env
    if data is None and kind=='image':
        from PIL import Image
        b=io.BytesIO();Image.new('RGB',(8,8),'red').save(b,format='PNG');data=b.getvalue()
    elif data is None:
        import subprocess
        path=tmp_path/('synthetic.mp3' if kind=='audio' else 'synthetic.mp4')
        source=['-f','lavfi','-i','sine=frequency=440:sample_rate=44100'] if kind=='audio' else ['-f','lavfi','-i','color=c=blue:s=16x16:r=10']
        subprocess.run(['ffmpeg','-nostdin','-v','error','-y',*source,'-t','0.1','-threads','1',str(path)],check=True,capture_output=True)
        data=path.read_bytes()
    ref=store.put(data,mime,a,operation_id='one')
    bot=SimpleNamespace(upload_media=AsyncMock(return_value=SimpleNamespace(payload=SimpleNamespace(token='synthetic-token'))))
    await pm.upload_for_max(bot,ref,a,kind=kind)
    media=bot.upload_media.call_args.args[0]
    assert isinstance(media.buffer,bytes) and not str(media.buffer).startswith('https:')
    assert s.gets==1


@pytest.mark.parametrize('provider',['openai-url','piapi-kling','heygen','unverified'])
def test_unproven_provider_input_is_blocked(private_env,provider):
    c,s,a,store=private_env
    ref=store.put(b'data','image/png',a,operation_id='one')
    with pytest.raises(pm.MediaProviderBlocked):pm.provider_input(ref,provider)
    assert pm.provider_input('https://legacy.invalid/image.png',provider)=='https://legacy.invalid/image.png'


@pytest.mark.parametrize('ttl',[0,-1,901,True,1.5])
def test_invalid_ttl_is_rejected(private_env,ttl):
    c,s,a,store=private_env
    ref=store.put(b'data','text/plain',a,operation_id='one')
    with pytest.raises(pm.MediaError):store.presign(ref,a,ttl=ttl)


def test_feature_defaults_off_and_credentials_not_in_repr(tmp_path,monkeypatch):
    monkeypatch.setenv('MAX_BOT_TOKEN','synthetic-max')
    monkeypatch.delenv('PRIVATE_MEDIA_ENABLED',raising=False)
    cfg=Config(token='synthetic',photo_path=tmp_path/'unused',private_media_secret_access_key='synthetic-secret')
    assert not cfg.private_media_enabled and 'synthetic-secret' not in repr(cfg)


def test_same_legacy_bucket_is_forbidden(private_env):
    c,s,a,store=private_env
    with pytest.raises(pm.MediaError):pm.PrivateMedia(replace(c,private_media_bucket=c.storage_bucket),client=s)


def test_recipient_expiry_is_rechecked_after_storage_head(private_env,monkeypatch):
    c,s,a,store=private_env
    ref=store.put(b'private synthetic','text/plain',a,operation_id='slow-head')
    recipient=pm.Owner('max','2')
    store.grant(ref,a,recipient,expires_at=int(s.now)+5)
    head=store._head
    def slow(row):
        result=head(row)
        s.now+=10
        return result
    monkeypatch.setattr(store,'_head',slow)
    with pytest.raises(pm.MediaAccessDenied):store.presign(ref,pm.Access(recipient))


@pytest.mark.parametrize('value,expected',[(None,False),('false',False),(' TRUE ',True)])
def test_environment_flag_is_strict_and_defaults_off(monkeypatch,value,expected):
    import config
    monkeypatch.setattr(config,'load_dotenv',lambda *a,**k:None)
    monkeypatch.setenv('MAX_BOT_TOKEN','synthetic-token')
    if value is None:monkeypatch.delenv('PRIVATE_MEDIA_ENABLED',raising=False)
    else:monkeypatch.setenv('PRIVATE_MEDIA_ENABLED',value)
    assert config.Config.from_env().private_media_enabled is expected


@pytest.mark.parametrize('value',['1','yes',''])
def test_ambiguous_flag_is_rejected(monkeypatch,value):
    import config
    monkeypatch.setattr(config,'load_dotenv',lambda *a,**k:None)
    monkeypatch.setenv('MAX_BOT_TOKEN','synthetic-token')
    monkeypatch.setenv('PRIVATE_MEDIA_ENABLED',value)
    with pytest.raises(ValueError):config.Config.from_env()


def test_private_sdk_uses_explicit_credentials_and_strict_tls(private_env,monkeypatch):
    from unittest.mock import Mock
    c,s,a,store=private_env
    captured={}
    session=Mock()
    monkeypatch.setattr(boto3,'Session',lambda **kwargs:(captured.update(kwargs) or session))
    # private_env replaces the factory; exercise the original factory from source
    import runpy
    factory=runpy.run_path(str(pm.Path(pm.__file__)))['storage_client']
    factory(c)
    assert captured['aws_access_key_id']=='synthetic-key'
    kwargs=session.client.call_args.kwargs
    assert kwargs['verify'] is True
    assert kwargs['config'].signature_version=='s3v4'
    assert kwargs['config'].retries['max_attempts']==0

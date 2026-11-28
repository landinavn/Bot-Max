"""Owned, durable media references. No HTTP fetch or AI submit lives here.

Callers construct Access only from authenticated framework/DB identities, never
from a callback payload. The private registry is separate from business tables.
The write flag never disables reads of already-created private media.
"""
from __future__ import annotations

import hashlib
import re
import sqlite3
import time
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass, field
from pathlib import Path
from urllib.parse import urlsplit
from uuid import uuid4

from safe_logging import get_logger
from media_policy import private_enabled,current_policy,policy_scope

logger = get_logger(__name__)
PREFIX = 'private-media:v1:'
_REF = re.compile(r'^private-media:v1:([0-9a-f]{32})$')
_ID = re.compile(r'^[A-Za-z0-9_.:@-]{1,200}$')
_SCOPE = ContextVar('private_media_access', default=None)
_PRIVATE_DEPENDENCY = ContextVar('private_media_dependency', default=False)
_GROUP_EVENT = ContextVar('private_media_group',default=False)
CONTENT_TYPES = {'image/png': 'png', 'image/jpeg': 'jpg', 'image/webp': 'webp',
                 'audio/mpeg': 'mp3', 'audio/wav': 'wav', 'video/mp4': 'mp4',
                 'text/plain': 'txt', 'application/pdf': 'pdf',
                 'application/octet-stream': 'bin'}


def media_limit(mime):
    return {'image':8,'audio':16,'video':32}.get(mime.split('/')[0],8)*1024*1024


from contextlib import asynccontextmanager
import weakref
_TRANSFER_LOCKS=weakref.WeakKeyDictionary()


@asynccontextmanager
async def transfer_slot():
    import asyncio
    loop=asyncio.get_running_loop()
    lock=_TRANSFER_LOCKS.setdefault(loop,asyncio.Semaphore(1))
    async with lock:yield


class MediaError(RuntimeError): pass
class MediaAccessDenied(MediaError): pass
class MediaUnavailable(MediaError): pass
class MediaWriteUncertain(MediaError): pass
class MediaProviderBlocked(MediaError): pass


def is_private(value):
    return isinstance(value, str) and _REF.fullmatch(value) is not None


def is_signed_url(value):
    from urllib.parse import parse_qsl
    if not isinstance(value,str): return False
    keys={k.lower() for k,_ in parse_qsl(urlsplit(value).query)}
    return bool(keys & {'x-amz-signature','awsaccesskeyid','signature'})


@dataclass(frozen=True)
class Owner:
    namespace: str
    identifier: str = field(repr=False)

    def __post_init__(self):
        if self.namespace not in ('max', 'account') or not isinstance(self.identifier, str) or not _ID.fullmatch(self.identifier):
            raise MediaAccessDenied('Invalid media owner')
        if self.namespace == 'max' and (not self.identifier.isdecimal() or int(self.identifier) <= 0):
            raise MediaAccessDenied('Invalid MAX media owner')


@dataclass(frozen=True)
class Access:
    owner: Owner | None
    role: str = 'user'
    order_id: str | None = field(default=None, repr=False)
    admin_id: int | None = field(default=None, repr=False)
    reason: str | None = None
    references: frozenset[str] = field(default_factory=frozenset, repr=False)
    entitlements: frozenset[str] = field(default_factory=frozenset, repr=False)
    private_required: bool = False

    def __post_init__(self):
        if self.role not in ('user', 'service', 'admin'):
            raise MediaAccessDenied('Invalid media role')
        if self.owner is not None and not isinstance(self.owner, Owner):
            raise MediaAccessDenied('Invalid media owner')
        if self.order_id is not None and (not isinstance(self.order_id, str) or not _ID.fullmatch(self.order_id)):
            raise MediaAccessDenied('Invalid media order scope')
        if self.role != 'admin' and self.owner is None:
            raise MediaAccessDenied('Media owner required')
        if self.role == 'service' and not self.order_id:
            raise MediaAccessDenied('Service access requires an order scope')


@contextmanager
def media_scope(access):
    if not isinstance(access, Access): raise MediaAccessDenied('Media scope required')
    token = _SCOPE.set(access)
    dependency = _PRIVATE_DEPENDENCY.set(access.private_required or any(is_private(r) for r in access.references))
    try: yield access
    finally:
        _PRIVATE_DEPENDENCY.reset(dependency)
        _SCOPE.reset(token)


def current_access():
    access = _SCOPE.get()
    if access is None: raise MediaAccessDenied('Authenticated media scope required')
    return access


def require_song_scope(config):
    access=current_access()
    admin_owner=(Owner('max',str(config.admin_user_id)) if config.admin_user_id is not None else None)
    if access.role=='service' or (access.role=='user' and access.owner==admin_owner):return access
    raise MediaAccessDenied('A persisted order scope is required before private song generation')


def generation_policy(config,*sources):
    """Readonly rollback must never turn private input/derivatives public."""
    private = _PRIVATE_DEPENDENCY.get() or any(is_private(s) for s in sources)
    if (not private_enabled(config) or getattr(config,'private_media_read_only',False)) and private:
        raise MediaError('Private media processing is paused; retained files remain readable')
    if private_enabled(config):
        from media_policy import SUPPORTED
        policy=current_policy()
        if getattr(config,'private_media_read_only',False) or (policy and policy.product not in SUPPORTED):
            raise MediaProviderBlocked('Создание временно недоступно. Оплата не требуется; сохранённый заказ доступен поддержке.')
        if not getattr(config,'private_media_storage_verified',False):raise MediaUnavailable('Private media storage has not passed the owner smoke test')
        actor=current_access()
        if actor.role=='service':
            service=PrivateMedia(config)
            try:service.mark_private_order(actor)
            finally:service.close()


def mutation_policy():
    """Guard direct text/ASR POSTs without changing the legacy configuration path."""
    import os
    if _PRIVATE_DEPENDENCY.get() or current_policy() is not None or os.environ.get('PRIVATE_MEDIA_ENABLED','false').strip().lower()=='true':
        from config import Config
        generation_policy(Config.from_env())


def song_context(function):
    """Bind the existing paid FSM order for the song/file stage only."""
    import functools
    import os
    @functools.wraps(function)
    async def wrapped(data,*args,**kwargs):
        def contains_private(value,depth=0):
            if depth>32:raise MediaError('Media snapshot nesting rejected')
            if is_private(value):return True
            if isinstance(value,dict):return any(contains_private(v,depth+1) for v in value.values())
            if isinstance(value,list):return any(contains_private(v,depth+1) for v in value)
            return False
        if contains_private(data):_PRIVATE_DEPENDENCY.set(True)
        from config import Config
        if not private_enabled(Config.from_env()):
            actor=_SCOPE.get()
            from config import Config, BASE_DIR
            registry=Path(os.environ.get('PRIVATE_MEDIA_REGISTRY',str(BASE_DIR/'private_media.sqlite3')))
            if actor is not None and actor.owner is not None and registry.exists():
                service=PrivateMedia(Config.from_env())
                try:
                    for field in ('music_prepaid_id','cartoon_order_id','audio_checkout_id'):
                        if data.get(field) and service.private_order(actor.owner,data[field]):
                            _PRIVATE_DEPENDENCY.set(True)
                finally:service.close()
            return await function(data,*args,**kwargs)
        from config import Config
        import database
        actor=current_access()
        config=Config.from_env()
        if actor.role=='service' or (config.admin_user_id is not None and actor.owner==Owner('max',str(config.admin_user_id))):
            return await function(data,*args,**kwargs)
        for field,getter in (('music_prepaid_id',database.get_musical_order),('cartoon_order_id',database.get_cartoon_order),('audio_checkout_id',database.get_audio_order)):
            if not data.get(field):continue
            order=await getter(data[field])
            if (not order or actor.owner!=Owner('max',str(order['user_id']))
                    or not (order['status']=='paid' or order.get('free_access'))):
                raise MediaAccessDenied('Owned paid order required')
            refs=set()
            def collect(value,depth=0):
                if depth>32:raise MediaError('Media snapshot nesting rejected')
                if is_private(value):refs.add(value)
                elif isinstance(value,dict):
                    for v in value.values():collect(v,depth+1)
                elif isinstance(value,list):
                    for v in value:collect(v,depth+1)
            collect(data)
            with media_scope(Access(actor.owner,'service',order['order_id'],references=frozenset(refs),entitlements=actor.entitlements)):
                return await function(data,*args,**kwargs)
        raise MediaAccessDenied('Restore the saved order before private song processing')
    return wrapped


@dataclass(frozen=True)
class SignedGet:
    url: str = field(repr=False)
    expires_at: int

    def __str__(self): return '<ephemeral media URL>'


def storage_client(config):
    """Explicit dedicated credentials, SigV4 and TLS; no anonymous HEAD."""
    import boto3
    from botocore.config import Config as S3Config
    from botocore.session import Session
    endpoint = urlsplit(config.storage_endpoint_url)
    if endpoint.scheme != 'https' or not endpoint.hostname or endpoint.username or endpoint.password or endpoint.query or endpoint.fragment:
        raise MediaError('Private storage requires HTTPS')
    if (not config.private_media_bucket or config.private_media_bucket == config.storage_bucket
            or not config.private_media_access_key_id or not config.private_media_secret_access_key):
        raise MediaError('Dedicated private bucket and credentials required')
    bc = Session()
    bc.get_component('config_store').set_config_variable('profile', None)
    session = boto3.Session(aws_access_key_id=config.private_media_access_key_id,
        aws_secret_access_key=config.private_media_secret_access_key,
        region_name=config.storage_region, botocore_session=bc)
    return session.client('s3', endpoint_url=config.storage_endpoint_url, verify=True,
        config=S3Config(signature_version='s3v4', retries={'max_attempts': 0},
                        connect_timeout=15, read_timeout=60, s3={'addressing_style': 'path'}))


SCHEMA = '''
CREATE TABLE IF NOT EXISTS media_objects (
 id TEXT PRIMARY KEY, bucket TEXT NOT NULL, object_key TEXT NOT NULL UNIQUE,
 owner_namespace TEXT NOT NULL, owner_id TEXT NOT NULL, order_id TEXT,
 operation_id TEXT NOT NULL, sha256 TEXT NOT NULL, size INTEGER NOT NULL,
 content_type TEXT NOT NULL, state TEXT NOT NULL CHECK(state IN ('prepared','submitting','ready')),
 created_at INTEGER NOT NULL, owner_access TEXT NOT NULL CHECK(owner_access IN ('owner','order')),
 UNIQUE(owner_namespace,owner_id,operation_id));
CREATE TABLE IF NOT EXISTS media_grants (
 asset_id TEXT NOT NULL REFERENCES media_objects(id), recipient_namespace TEXT NOT NULL,
 recipient_id TEXT NOT NULL, expires_at INTEGER NOT NULL, created_at INTEGER NOT NULL,
 PRIMARY KEY(asset_id,recipient_namespace,recipient_id));
CREATE TABLE IF NOT EXISTS media_orders (
 owner_namespace TEXT NOT NULL, owner_id TEXT NOT NULL, order_id TEXT NOT NULL,
 created_at INTEGER NOT NULL, PRIMARY KEY(owner_namespace,owner_id,order_id));
CREATE TABLE IF NOT EXISTS media_policies (
 owner_namespace TEXT NOT NULL, owner_id TEXT NOT NULL, operation_id TEXT NOT NULL,
 product TEXT NOT NULL, mode TEXT NOT NULL CHECK(mode IN ('legacy','private')), created_at INTEGER NOT NULL,
 PRIMARY KEY(owner_namespace,owner_id,operation_id));
'''


class PrivateMedia:
    def __init__(self, config, *, client=None, clock=None):
        self.config = config
        self.client = client  # Inject only trusted SDK / synthetic test client.
        self._policy_checked = False
        self.clock = clock or time.time
        if Path(config.private_media_registry).expanduser().is_symlink():raise MediaUnavailable('Registry symlink refused')
        self.path = Path(config.private_media_registry).expanduser().resolve()
        from config import BASE_DIR
        if self.path == (BASE_DIR/'database.sqlite3').resolve():
            raise MediaError('Media registry must be separate from business database')
        if self.path.is_dir() or not self.path.parent.is_dir():
            raise MediaError('Private registry directory must already exist')
        if not config.private_media_bucket or config.private_media_bucket == config.storage_bucket:
            raise MediaError('A separate private bucket is required')
        if client is None and (not config.private_media_access_key_id or not config.private_media_secret_access_key):
            raise MediaError('Dedicated private credentials required')

    @contextmanager
    def _db(self, write=False):
        from media_registry import registry_connection
        with registry_connection(self.path,write=write) as db:yield db

    def _sdk(self):
        if self.client is None: self.client = storage_client(self.config)
        if not self._policy_checked:
            self._check_bucket_policy()
            self._policy_checked = True
        return self.client

    def preflight(self):
        """Read bucket access configuration before any paid generation; no PUT."""
        self._sdk()

    def mark_private_order(self,access):
        if not private_enabled(self.config) or access.role!='service':raise MediaAccessDenied('Scoped private write required')
        with self._db(write=True) as db:
            db.execute('INSERT OR IGNORE INTO media_orders VALUES(?,?,?,?)',
                       (access.owner.namespace,access.owner.identifier,access.order_id,int(self.clock())))

    def private_order(self,owner,order_id):
        if not self.path.exists():return False
        with self._db() as db:
            return db.execute('SELECT 1 FROM media_orders WHERE owner_namespace=? AND owner_id=? AND order_id=?',
                              (owner.namespace,owner.identifier,order_id)).fetchone() is not None

    def _check_bucket_policy(self):
        """Read-only, conservative check. Never set bucket ACL/policy here.

        Provider-specific public-access switches and external CDNs still need
        the owner's console review + anonymous synthetic-file smoke test.
        """
        import json
        from botocore.exceptions import ClientError
        try:
            profile=getattr(self.config,'private_media_profile','strict')
            if profile=='yandex-v1':
                from yandex_media_profile import check_bucket
                check_bucket(self.config,self.client)
                return
            if profile!='strict':raise MediaUnavailable('Unknown private storage profile')
            allowed=getattr(self.config,'private_media_principals',frozenset())
            if not allowed:raise MediaUnavailable('An explicit approved principal allowlist is required')
            acl=self.client.get_bucket_acl(Bucket=self.config.private_media_bucket)
            if acl.get('Owner',{}).get('ID') not in allowed or not isinstance(acl.get('Grants'),list) or not acl['Grants']:
                raise MediaUnavailable('Private bucket ACL could not be verified')
            for grant in acl.get('Grants',[]):
                if grant.get('Grantee',{}).get('Type')!='CanonicalUser' or grant.get('Grantee',{}).get('ID') not in allowed:
                    raise MediaUnavailable('Private bucket ACL is not approved')
            try:
                raw=self.client.get_bucket_policy(Bucket=self.config.private_media_bucket)
            except ClientError as error:
                if error.response.get('Error',{}).get('Code') not in ('NoSuchBucketPolicy','NoSuchPolicy'):raise
                raw={'Policy':'{"Statement":[]}'}
            def unique(pairs):
                value={}
                for key,item in pairs:
                    if key in value:raise ValueError('Duplicate policy key')
                    value[key]=item
                return value
            policy=json.loads(raw['Policy'],object_pairs_hook=unique)
            if not isinstance(policy,dict) or 'Statement' not in policy:
                raise MediaUnavailable('Private bucket policy could not be verified')
            statements=policy['Statement']
            if isinstance(statements,dict):statements=[statements]
            for statement in statements:
                if statement.get('Effect')=='Deny':continue
                if statement.get('Effect')!='Allow':raise MediaUnavailable('Unknown private bucket policy rule')
                principal=statement.get('Principal')
                if (set(statement)-{'Sid','Effect','Principal','Action','Resource','Condition'}
                        or not isinstance(principal,dict) or set(principal)!={'CanonicalUser'}):
                    raise MediaUnavailable('Private bucket policy is not approved')
                for values in principal.values():
                    values=values if isinstance(values,list) else [values]
                    if not values or any(not isinstance(v,str) or v not in allowed for v in values):
                        raise MediaUnavailable('Private bucket policy is not approved')
                actions=statement.get('Action',[])
                actions=actions if isinstance(actions,list) else [actions]
                resources=statement.get('Resource',[])
                resources=resources if isinstance(resources,list) else [resources]
                bucket='arn:aws:s3:::'+self.config.private_media_bucket
                if (not actions or set(actions)-{'s3:GetObject','s3:PutObject','s3:GetBucketAcl','s3:ListBucket'}
                        or not resources or any(r not in (bucket,bucket+'/media/v1/*') for r in resources)):
                    raise MediaUnavailable('Unknown private bucket access rule')
                condition=statement.get('Condition',{})
                if not isinstance(condition,dict) or set(condition)-{'Bool','StringLike'}:
                    raise MediaUnavailable('Unknown private bucket condition')
                if 'Bool' in condition and condition['Bool']!={'aws:SecureTransport':'true'}:
                    raise MediaUnavailable('Unknown transport condition')
                if 'StringLike' in condition and condition['StringLike'] not in ({'s3:prefix':'media/v1/*'},{'s3:prefix':['media/v1/*']}):
                    raise MediaUnavailable('Unknown prefix condition')
        except MediaError:raise
        except Exception:raise MediaUnavailable('Private bucket policy could not be verified') from None

    def close(self):
        if self.client is not None:
            try: self.client.close()
            except Exception: logger.warning('Private storage cleanup failed')
            self.client = None
            self._policy_checked = False

    def _authorize(self, db, row, access, *, owner_only=False):
        if not isinstance(access, Access): raise MediaAccessDenied('Media access denied')
        if access.role == 'admin':
            if (type(access.admin_id) is not int or access.admin_id != self.config.admin_user_id
                    or access.reason not in ('support', 'recovery', 'export')):
                raise MediaAccessDenied('Media administrator access denied')
            if owner_only: raise MediaAccessDenied('Owner confirmation required')
            return
        owned = access.owner == Owner(row['owner_namespace'], row['owner_id'])
        if row['owner_access']=='order' and access.role=='user' and not owned:
            raise MediaAccessDenied('Restricted media cannot use recipient grants')
        if owned and access.role == 'user' and row['owner_access'] == 'order' and row['order_id'] not in access.entitlements:
            raise MediaAccessDenied('Paid media entitlement required')
        if (row['owner_access']=='order' and access.role=='service' and row['order_id']!=access.order_id
                and row['order_id'] not in access.entitlements):
            raise MediaAccessDenied('Restricted song cannot cross order scopes')
        if owned and (access.role != 'service' or row['order_id'] == access.order_id or PREFIX+row['id'] in access.references): return
        if not owner_only and access.role == 'user':
            grant = db.execute('SELECT expires_at FROM media_grants WHERE asset_id=? AND recipient_namespace=? AND recipient_id=?',
                (row['id'], access.owner.namespace, access.owner.identifier)).fetchone()
            if grant and grant[0] > int(self.clock()): return
        raise MediaAccessDenied('Media access denied')

    def _row(self, db, reference, access, *, owner_only=False):
        if not is_private(reference): raise MediaAccessDenied('Invalid private media reference')
        row = db.execute('SELECT * FROM media_objects WHERE id=?', (_REF.fullmatch(reference)[1],)).fetchone()
        if row is None: raise MediaAccessDenied('Media access denied')
        self._authorize(db, row, access, owner_only=owner_only)
        if row['bucket'] != self.config.private_media_bucket:
            raise MediaUnavailable('Private bucket configuration mismatch')
        return row

    def _head(self, row):
        try:
            return self._sdk().head_object(Bucket=row['bucket'], Key=row['object_key'])
        except Exception as error:
            from botocore.exceptions import ClientError
            if isinstance(error, ClientError) and str(error.response.get('Error', {}).get('Code')) in ('404','NoSuchKey','NotFound'):
                return None
            raise MediaUnavailable('Authorized media metadata unavailable') from None

    @staticmethod
    def _verify_head(row, head):
        if (not head or head.get('ContentLength') != row['size']
                or head.get('Metadata', {}).get('sha256') != row['sha256']):
            raise MediaUnavailable('Media integrity check failed')

    def put(self, data, content_type, access, *, operation_id, owner_access='owner',mark_order=True):
        if not private_enabled(self.config) or getattr(self.config,'private_media_read_only',False):raise MediaError('Private writes disabled')
        if not isinstance(access,Access) or access.owner is None or access.role=='admin':raise MediaAccessDenied('Explicit media owner required')
        content_type={'audio/x-wav':'audio/wav','audio/mp3':'audio/mpeg','image/jpg':'image/jpeg'}.get(content_type,content_type)
        if not isinstance(data,bytes) or content_type not in CONTENT_TYPES or not 0<len(data)<=media_limit(content_type):raise MediaError('Media type or size refused')
        if not isinstance(operation_id,str) or not _ID.fullmatch(operation_id):raise MediaError('Media operation ID required')
        if owner_access not in ('owner','order') or (owner_access=='order' and not access.order_id):raise MediaError('Restricted media requires an order')
        if access.role=='service' and mark_order:self.mark_private_order(access)
        digest=hashlib.sha256(data).hexdigest()
        # Only metadata operations inside write transactions. No S3 call here.
        with self._db(write=True) as db:
            db.execute('BEGIN IMMEDIATE')
            row=db.execute('SELECT * FROM media_objects WHERE owner_namespace=? AND owner_id=? AND operation_id=?',
                (access.owner.namespace,access.owner.identifier,operation_id)).fetchone()
            if row is None:
                identifier=uuid4().hex
                db.execute('INSERT INTO media_objects VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)',
                    (identifier,self.config.private_media_bucket,f'media/v1/{identifier}.{CONTENT_TYPES[content_type]}',
                     access.owner.namespace,access.owner.identifier,access.order_id,operation_id,digest,len(data),content_type,'prepared',int(self.clock()),owner_access))
                row=db.execute('SELECT * FROM media_objects WHERE id=?',(identifier,)).fetchone()
            row=dict(row)
            if (row['sha256'],row['content_type'],row['order_id'],row['owner_access'])!=(digest,content_type,access.order_id,owner_access):
                raise MediaError('Media operation already has different content or order')
        reference=PREFIX+row['id']
        head=self._head(row)
        if head:
            self._verify_head(row,head)
            self._ready(row['id'])
            return reference
        if row['state']!='prepared':raise MediaWriteUncertain('Previous storage submission requires verification')
        with self._db(write=True) as db:
            claim=db.execute("UPDATE media_objects SET state='submitting' WHERE id=? AND state='prepared'",(row['id'],))
            if claim.rowcount!=1:raise MediaWriteUncertain('Storage operation is already claimed')
        try:
            self._sdk().put_object(Bucket=row['bucket'],Key=row['object_key'],Body=data,ContentType=content_type,
                Metadata={'sha256':digest},CacheControl='private, no-store',ContentDisposition='attachment; filename="media.'+CONTENT_TYPES[content_type]+'"')
        except Exception:raise MediaWriteUncertain('Storage submission requires verification') from None
        self._verify_head(row,self._head(row))
        self._ready(row['id'])
        return reference

    def _ready(self,identifier):
        with self._db(write=True) as db:db.execute("UPDATE media_objects SET state='ready' WHERE id=?",(identifier,))

    def _readable(self,reference,access,limit):
        with self._db() as db:
            row=dict(self._row(db,reference,access))
        if row['content_type'] not in CONTENT_TYPES or row['state']!='ready' or not 0<row['size']<=min(limit,media_limit(row['content_type'])):raise MediaUnavailable('Media not ready or exceeds type limit')
        self._verify_head(row,self._head(row))
        with self._db() as db:self._authorize(db,row,access)
        return row

    def download_file(self,reference,access,path,*,limit=32*1024*1024):
        """Bounded 64 KiB read, no network within a SQLite transaction."""
        import os
        row=self._readable(reference,access,limit)
        target=Path(path);body=None;created=False
        try:
            fd=os.open(target,os.O_WRONLY|os.O_CREAT|os.O_EXCL,0o600)
            created=True
            with os.fdopen(fd,'wb') as output:
                body=self._sdk().get_object(Bucket=row['bucket'],Key=row['object_key'])['Body']
                total=0;digest=hashlib.sha256()
                while True:
                    chunk=body.read(min(65536,row['size']-total+1))
                    if not chunk:break
                    total+=len(chunk)
                    if total>row['size']:raise MediaUnavailable('Media exceeds declared size')
                    output.write(chunk);digest.update(chunk)
                if total!=row['size'] or digest.hexdigest()!=row['sha256']:raise MediaUnavailable('Media integrity check failed')
            return row['content_type']
        except MediaError:
            if created:target.unlink(missing_ok=True)
            raise
        except Exception:
            if created:target.unlink(missing_ok=True)
            raise MediaUnavailable('Authorized media stream failed') from None
        finally:
            if body is not None:
                try:body.close()
                except Exception:logger.warning('Private media stream cleanup failed')

    def read(self,reference,access,*,limit=32*1024*1024):
        import tempfile
        with tempfile.TemporaryDirectory(prefix='private-read-') as directory:
            path=Path(directory)/'media'
            mime=self.download_file(reference,access,path,limit=limit)
            return path.read_bytes(),mime

    def stat(self,reference,access):
        row=self._readable(reference,access,32*1024*1024)
        return {'size':row['size'],'content_type':row['content_type']}

    def presign(self,reference,access,*,ttl=300):
        if type(ttl) is not int or not 1<=ttl<=900:raise MediaError('Invalid signed GET lifetime')
        row=self._readable(reference,access,32*1024*1024)
        with self._db() as db:
            self._authorize(db,row,access)
            if access.role=='user' and access.owner!=Owner(row['owner_namespace'],row['owner_id']):
                grant=db.execute('SELECT expires_at FROM media_grants WHERE asset_id=? AND recipient_namespace=? AND recipient_id=?',
                    (row['id'],access.owner.namespace,access.owner.identifier)).fetchone()
                ttl=min(ttl,grant[0]-int(self.clock()))
                if ttl<1:raise MediaAccessDenied('Media grant expired')
        try:url=self._sdk().generate_presigned_url('get_object',Params={'Bucket':row['bucket'],'Key':row['object_key']},ExpiresIn=ttl,HttpMethod='GET')
        except Exception:raise MediaUnavailable('Media signing unavailable') from None
        if urlsplit(url).scheme!='https':raise MediaUnavailable('HTTPS signing required')
        return SignedGet(url,int(self.clock())+ttl)

    def grant(self, reference, access, recipient, *, expires_at):
        if not isinstance(recipient, Owner) or type(expires_at) is not int or not int(self.clock()) < expires_at <= int(self.clock())+86400:
            raise MediaError('Explicit recipient and bounded access period required')
        with self._db(write=True) as db:
            row = self._row(db, reference, access, owner_only=True)
            if row['owner_access']=='order':raise MediaAccessDenied('Restricted songs cannot use recipient grants')
            if access.role != 'user': raise MediaAccessDenied('Owner confirmation required')
            db.execute('INSERT INTO media_grants VALUES(?,?,?,?,?) ON CONFLICT(asset_id,recipient_namespace,recipient_id) DO UPDATE SET expires_at=excluded.expires_at',
                (row['id'], recipient.namespace, recipient.identifier, expires_at, int(self.clock())))

    def revoke(self, reference, access, recipient):
        with self._db(write=True) as db:
            row = self._row(db, reference, access, owner_only=True)
            db.execute('DELETE FROM media_grants WHERE asset_id=? AND recipient_namespace=? AND recipient_id=?',
                (row['id'],recipient.namespace,recipient.identifier))


def provider_input(reference, provider):
    """URL-only vendor fetch/expiry contracts are not proven by this repository."""
    import os
    policy=current_policy()
    if _PRIVATE_DEPENDENCY.get() or is_private(reference) or (policy.mode=='private' if policy else os.environ.get('PRIVATE_MEDIA_ENABLED','false').strip().lower() == 'true'):
        raise MediaProviderBlocked('Private URL input is not approved for this provider')
    return reference


def client_text(text):
    if not isinstance(text,str): return text
    return re.sub(r'private-media:v1:[0-9a-f]{32}', 'файл доступен кнопкой ниже', text)


def download_button(*, text, url):
    from maxapi.types import LinkButton, CallbackButton
    if is_private(url):
        return CallbackButton(text=text,payload='private_media_download:'+url[len(PREFIX):])
    return LinkButton(text=text,url=url)


def store_bytes(config, data, content_type, *, purpose):
    generation_policy(config)
    service = PrivateMedia(config)
    try:
        access = current_access()
        operation = purpose + ':' + hashlib.sha256((access.order_id or '').encode()+b'\0'+data).hexdigest()
        return service.put(data, content_type, access, operation_id=operation)
    finally: service.close()


def store_path(config,path,content_type,*,purpose):
    if not 0<Path(path).stat().st_size<=media_limit(content_type):raise MediaError('Media file size refused')
    return store_bytes(config,Path(path).read_bytes(),content_type,purpose=purpose)


async def read_bytes(reference, *, limit=32*1024*1024):
    import asyncio
    from config import Config
    access = current_access()
    _PRIVATE_DEPENDENCY.set(True)
    service = PrivateMedia(Config.from_env())
    try:
        async with transfer_slot():return await asyncio.to_thread(service.read, reference, access, limit=limit)
    finally: await asyncio.to_thread(service.close)


async def stream_to_path(reference,path,*,limit=32*1024*1024):
    import asyncio
    from config import Config
    _PRIVATE_DEPENDENCY.set(True)
    service=PrivateMedia(Config.from_env());access=current_access()
    try:
        async with transfer_slot():return await asyncio.to_thread(service.download_file,reference,access,path,limit=limit)
    finally:await asyncio.to_thread(service.close)


async def retain_image_url(url):
    """Archive only an image actually accepted by a handler, not unsolicited files.

    MAX keeps its original copy independently. Our persisted photo payload uses
    an owned reference, not the platform's transient URL/token.
    """
    import os
    from config import Config
    if not private_enabled(Config.from_env()):
        if is_private(url):return url
        return url
    import asyncio
    import httpx
    import tempfile
    from config import Config
    from image_service import _photo_format
    from media_service import _download
    access=current_access()
    if is_private(url):
        service=PrivateMedia(Config.from_env())
        try:await asyncio.to_thread(service.stat,url,access)
        finally:await asyncio.to_thread(service.close)
        return url
    config=Config.from_env()
    generation_policy(config)
    # Validate ownership scope/config before downloading any customer content.
    service=PrivateMedia(config)
    try:
        with tempfile.TemporaryDirectory(prefix='private-source-') as directory:
            path=Path(directory)/'source'
            async with httpx.AsyncClient(timeout=60,trust_env=False,follow_redirects=False) as http:
                try:await _download(http,url,path,media_limit('image/png'))
                except httpx.HTTPError:raise MediaUnavailable('Source photo download failed') from None
            data=path.read_bytes()
            _,mime=_photo_format(data)
            operation='source-image:'+hashlib.sha256((access.order_id or '').encode()+b'\0'+data).hexdigest()
            return await asyncio.to_thread(service.put,data,mime,access,operation_id=operation)
    finally:await asyncio.to_thread(service.close)


async def retain_photo_payload(payload):
    import os
    from config import Config
    if not private_enabled(Config.from_env()):
        return payload.model_dump()
    return {'url':await retain_image_url(payload.url)}


class MediaScopeMiddleware:
    """MAX SDK outer middleware: identity from authenticated update, not payload."""
    async def __call__(self, handler, event_object, data):
        user_id = event_object.get_ids()[1]
        if user_id is None: return await handler(event_object, data)
        import os
        message=getattr(event_object,'message',None)
        recipient=getattr(message,'recipient',None)
        chat_type=getattr(recipient,'chat_type',None)
        from media_policy import configured,session_policy
        group=chat_type in ('chat','channel')
        if (os.environ.get('PRIVATE_MEDIA_ENABLED','false').strip().lower()=='true' and group):
            await event_object.bot.send_message(user_id=user_id,text='Для работы с личными материалами откройте личный диалог с ботом.')
            return
        access = await user_access(user_id)
        token=_GROUP_EVENT.set(group)
        try:
            with media_scope(access):
                context=data.get('_memory_context')
                payload=getattr(getattr(event_object,'callback',None),'payload','') or ''
                retained_download=payload.startswith('private_media_download:')
                policy=await session_policy(context,user_id,payload) if configured() and context and not retained_download else None
                if group and policy and policy.mode=='private':
                    await event_object.bot.send_message(user_id=user_id,text='Для личных материалов откройте личный диалог с ботом.')
                    return
                with policy_scope(policy):return await handler(event_object,data)
        except MediaError:
            from audio_orders import SUPPORT_URL
            from maxapi.types import LinkButton
            from maxapi.utils.inline_keyboard import InlineKeyboardBuilder
            keyboard=InlineKeyboardBuilder().row(LinkButton(text='Поддержка',url=SUPPORT_URL)).as_markup()
            await event_object.bot.send_message(user_id=user_id,text='Создание временно недоступно. Если заказ уже оплачен, оплата и материалы сохранены. Для продолжения обратитесь в поддержку.',attachments=[keyboard])
        finally:_GROUP_EVENT.reset(token)


async def user_access(user_id):
    """Fresh readonly entitlement assertion using the existing paid/free rules.

    In particular, owning an unpaid full-song reference does NOT unlock it.
    Future account adapters must issue their own authenticated assertions.
    """
    from config import Config, BASE_DIR
    import os
    registry = Path(os.environ.get('PRIVATE_MEDIA_REGISTRY',str(BASE_DIR/'private_media.sqlite3')))
    actor=Access(Owner('max',str(user_id)))
    if not registry.exists():return actor
    import aiosqlite
    import database
    try:
        async with aiosqlite.connect(Path(database.DATABASE_PATH).resolve().as_uri()+'?mode=ro',uri=True) as db:
            orders=set()
            for table in ('audio_orders','musical_orders','cartoon_orders'):
                columns={r[1] for r in await (await db.execute('PRAGMA table_info('+table+')')).fetchall()}
                if not columns:continue
                predicate="status='paid'"
                if table=='audio_orders' and 'free_access' in columns:predicate+=' OR free_access=1'
                if table=='cartoon_orders':predicate+=" OR status='completed'"
                rows=await (await db.execute('SELECT order_id FROM '+table+' WHERE user_id=? AND ('+predicate+')',(user_id,))).fetchall()
                orders.update(r[0] for r in rows)
    except sqlite3.Error:
        return actor  # No paid assertions when the source cannot be checked.
    return Access(actor.owner,entitlements=frozenset(orders))


async def check_for_user(reference,user_id,*,kind=None):
    """Media-only validation before binding a private reference to an order.

    Payment amounts, state transitions and transactions are left to existing code.
    A valid opaque UUID alone never permits attaching another customer's asset.
    """
    if not is_private(reference):return
    import asyncio
    from config import Config
    access=current_access()
    owner=Owner('max',str(user_id))
    if access.owner!=owner:raise MediaAccessDenied('Media owner does not match order user')
    service=PrivateMedia(Config.from_env())
    try:
        with service._db() as db:
            row=service._row(db,reference,access)
            if Owner(row['owner_namespace'],row['owner_id'])!=owner:
                raise MediaAccessDenied('Order inputs require their verified owner')
        meta=await asyncio.to_thread(service.stat,reference,access)
        if kind and meta['content_type'].split('/')[0]!=kind:raise MediaError('Wrong media type for order')
    finally:await asyncio.to_thread(service.close)


def order_scope(getter_name):
    """Worker scope from its trusted persisted order, not an incoming payload.

    No extra DB lookup when the feature is off and no private registry exists.
    Only exact private references retained in this order are added to its scope.
    """
    import functools
    import json
    def decorate(function):
        @functools.wraps(function)
        async def wrapped(*args, **kwargs):
            from config import Config
            import database
            import os
            from config import BASE_DIR
            registry = Path(os.environ.get('PRIVATE_MEDIA_REGISTRY', str(BASE_DIR/'private_media.sqlite3')))
            from media_policy import configured,restore_operation
            if not configured():
                return await function(*args, **kwargs)
            config = Config.from_env()
            # Worker process(self, order_id) / deliver_photo_result(bot, order_id,...).
            identifier = args[1] if len(args) > 1 else kwargs.get('order_id', kwargs.get('job_id'))
            row = await getattr(database, getter_name)(identifier)
            if row is None: return await function(*args, **kwargs)
            refs = set()
            def collect(value):
                if is_private(value): refs.add(value)
                elif isinstance(value, dict):
                    for v in value.values(): collect(v)
                elif isinstance(value, list):
                    for v in value: collect(v)
                elif isinstance(value, str) and value[:1] in ('{','['):
                    try: collect(json.loads(value))
                    except (ValueError,TypeError): pass
            collect(dict(row))
            if getter_name=='get_photo_order':
                # Results are separate rows, not part of photo_orders' snapshot.
                collect(await database.get_photo_results(identifier))
            order = row.get('order_id') or str(identifier)
            product={'get_audio_order':'song','get_musical_order':'musical_card','get_photo_order':'photosession','get_animation_job':'animation'}[getter_name]
            policy=await restore_operation(product,row['user_id'],order,refs,parent=row.get('order_id'))
            assertions=await user_access(row['user_id'])
            access=Access(Owner('max',str(row['user_id'])), 'service', order,references=frozenset(refs),private_required=policy.mode=='private',entitlements=assertions.entitlements)
            with policy_scope(policy),media_scope(access):
                return await function(*args, **kwargs)
        return wrapped
    return decorate


def job_scope(kind):
    """Resolve owner from the job/project tables; no owner from job JSON/payload."""
    import functools
    import json
    import os
    def decorate(function):
        @functools.wraps(function)
        async def wrapped(*args, **kwargs):
            from config import Config, BASE_DIR
            registry = Path(os.environ.get('PRIVATE_MEDIA_REGISTRY', str(BASE_DIR/'private_media.sqlite3')))
            from media_policy import configured,restore_operation
            if not configured():
                return await function(*args, **kwargs)
            value = args[1]
            identifier = value['id'] if isinstance(value,dict) else value
            if kind == 'studio':
                from multstudio import repository as repo
                async with repo.connect() as db:
                    row = await repo.one(db, 'SELECT j.*,p.owner_id AS media_owner FROM cartoon_jobs j JOIN cartoon_projects p ON p.id=j.project_id WHERE j.id=?', (identifier,))
                owner = row['media_owner'] if row else None
            elif kind == 'ded_moroz':
                from ded_moroz import get
                row = await get(identifier)
                owner = row['user_id'] if row else None
            else: raise MediaAccessDenied('Unknown media job scope')
            if row is None: raise MediaAccessDenied('Media job not found')
            refs = set()
            def collect(v):
                if is_private(v): refs.add(v)
                elif isinstance(v,dict):
                    for item in v.values(): collect(item)
                elif isinstance(v,list):
                    for item in v: collect(item)
                elif isinstance(v,str) and v[:1] in ('{','['):
                    try: collect(json.loads(v))
                    except (ValueError,TypeError): pass
            collect(dict(row))
            operation=kind+':'+str(identifier)
            policy=await restore_operation('multstudio' if kind=='studio' else 'ded_moroz',owner,operation,refs,parent=row.get('payment_order_id'))
            assertions=await user_access(owner)
            access=Access(Owner('max',str(owner)), 'service', operation, references=frozenset(refs),private_required=policy.mode=='private',entitlements=assertions.entitlements)
            with policy_scope(policy),media_scope(access):
                return await function(*args, **kwargs)
        return wrapped
    return decorate


async def upload_for_max(bot,reference,access,*,kind,destination_user=None):
    import asyncio
    import tempfile
    from config import Config
    from maxapi.types import InputMediaBuffer,InputMedia
    from maxapi.enums.upload_type import UploadType
    kinds={'image':UploadType.IMAGE,'audio':UploadType.AUDIO,'video':UploadType.VIDEO,'file':UploadType.FILE}
    if kind not in kinds:raise MediaError('Unsupported MAX media type')
    if _GROUP_EVENT.get():
        permitted=(access.owner is not None and access.owner.namespace=='max' and str(destination_user)==access.owner.identifier)
        if access.role=='admin':permitted=destination_user==access.admin_id
        if destination_user is None or not permitted:raise MediaAccessDenied('Private attachments require a personal destination')
    service=PrivateMedia(Config.from_env())
    try:
        async with transfer_slot():
            with tempfile.TemporaryDirectory(prefix='private-max-') as directory:
                path=Path(directory)/'media'
                mime=await asyncio.to_thread(service.download_file,reference,access,path)
                if kind!='file' and mime.split('/')[0]!=kind:raise MediaError('MAX attachment type mismatch')
                filename='media.'+CONTENT_TYPES[mime]
                # Small buffers retain SDK compatibility; large transfers use its file path.
                if kind=='image' or path.stat().st_size<=1024*1024:
                    data=await asyncio.to_thread(path.read_bytes)
                    if kind=='image':
                        from image_service import _prepare_max_image
                        data,filename=await asyncio.to_thread(_prepare_max_image,data)
                    upload=InputMediaBuffer(buffer=data,filename=filename,type=kinds[kind])
                else:
                    renamed=path.with_name(filename);path.rename(renamed)
                    upload=InputMedia(path=str(renamed),type=kinds[kind])
                attachment=await bot.upload_media(upload)
                if not isinstance(attachment.payload.token,str) or not attachment.payload.token.strip():raise MediaUnavailable('MAX did not confirm media upload')
                return attachment
    finally:await asyncio.to_thread(service.close)

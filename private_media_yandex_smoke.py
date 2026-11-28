"""OWNER-ONLY synthetic smoke. Not imported/run by the bot; no automatic cleanup.

Requires --execute, exact bucket confirmation and a new private local record.
No feature flags, business DB, registry, IAM, policy or ACL are changed.
Unknown PUT outcomes are recorded and never retried. No URLs/keys in stdout.
"""
import argparse
from dataclasses import replace
import hashlib
import json
import logging
import os
from pathlib import Path
import time
from urllib.parse import urlsplit,urlunsplit
from uuid import uuid4


def denied(call):
    from botocore.exceptions import ClientError
    try:
        response=call()
    except ClientError as error:
        if (error.response.get('ResponseMetadata',{}).get('HTTPStatusCode')==403
                and error.response.get('Error',{}).get('Code') in ('AccessDenied','Forbidden','403')):return
        if error.response.get('Error',{}).get('Code') in ('SignatureDoesNotMatch','InvalidAccessKeyId','InvalidAccessKeyID','ExpiredToken','InvalidToken'):
            raise SmokeFailure('authentication_unverified') from None
        raise SmokeFailure('access_not_denied') from None
    if isinstance(response,dict) and response.get('Body') is not None:response['Body'].close()
    raise SmokeFailure('access_not_denied')


def configuration():
    """Read ONLY explicit private environment variables, never the bot's .env."""
    from config import Config
    def csv(name):return frozenset(filter(None,(x.strip() for x in os.environ.get(name,'').split(','))))
    verified=os.environ.get('PRIVATE_MEDIA_YANDEX_CONTROLS_VERIFIED','false').strip().lower()
    if verified not in ('true','false'):raise ValueError('Invalid controls confirmation')
    return Config(token='synthetic-storage-smoke',photo_path=Path('/tmp/unused-smoke-photo'),
        storage_bucket=os.environ.get('STORAGE_BUCKET'),
        storage_endpoint_url=os.environ.get('STORAGE_ENDPOINT_URL','https://storage.yandexcloud.net'),
        storage_region=os.environ.get('STORAGE_REGION','ru-central1'),
        private_media_bucket=os.environ.get('PRIVATE_MEDIA_BUCKET'),
        private_media_access_key_id=os.environ.get('PRIVATE_MEDIA_ACCESS_KEY_ID'),
        private_media_secret_access_key=os.environ.get('PRIVATE_MEDIA_SECRET_ACCESS_KEY'),
        private_media_profile=os.environ.get('PRIVATE_MEDIA_PROFILE','strict'),
        private_media_yandex_folder_id=os.environ.get('PRIVATE_MEDIA_YANDEX_FOLDER_ID'),
        private_media_runtime_principal=os.environ.get('PRIVATE_MEDIA_RUNTIME_PRINCIPAL'),
        private_media_principals=csv('PRIVATE_MEDIA_PRINCIPALS'),
        private_media_legacy_principals=csv('PRIVATE_MEDIA_LEGACY_PRINCIPALS'),
        private_media_yandex_controls_verified=verified=='true')


PAYLOAD=b'Bot-Max synthetic private storage smoke\n'
LEGACY_LABELS=('bot-storage-user','speechkit-user')
STAGES=('preflight','put','head_integrity','get_integrity','anonymous_get_head',
        'signed_get_head','signed_url_expiry','fresh_signed_access','out_of_prefix_put','legacy_probes')
CODES=frozenset({'metadata_missing','metadata_invalid','metadata_duplicate_sha256','metadata_invalid_sha256',
    'size_mismatch','sha256_mismatch','content_size_mismatch','content_sha256_mismatch',
    'preflight_failed','sdk_access_denied','network_timeout','probe_failed','access_not_denied',
    'signed_access_failed','expiry_not_established','fresh_access_failed','unexpected_endpoint',
    'legacy_probe_failed','invalid_record','invalid_configuration','record_write_failed','interrupted','authentication_unverified'})


class SmokeFailure(RuntimeError):
    def __init__(self,code):self.code=code;super().__init__(code)


def error_code(error,stage):
    from private_media_integrity import S3IntegrityError
    from botocore.exceptions import ClientError
    import httpx
    if isinstance(error,(S3IntegrityError,SmokeFailure)) and error.code in CODES:return error.code
    if isinstance(error,KeyboardInterrupt):return 'interrupted'
    if isinstance(error,(TimeoutError,httpx.TimeoutException)):return 'network_timeout'
    if isinstance(error,ClientError) and error.response.get('ResponseMetadata',{}).get('HTTPStatusCode')==403:
        return 'sdk_access_denied'
    return 'preflight_failed' if stage=='preflight' else 'probe_failed'


class Progress:
    def __init__(self,bucket,save,report=None,*,readonly=False):
        self.save=save;self.report=report
        self.record={'version':2,'bucket':bucket,'mode':'read-only' if readonly else 'synthetic-write',
                     'stage':'configuration','stages':{},'objects':[],
                     'legacy':{label:{'status':'not tested','reason':'not_reached'} for label in LEGACY_LABELS},'result':'incomplete'}
    def emit(self,stage,status,code=None):
        self.record['stage']=stage
        self.record['stages'][stage]={'status':status}
        if code:self.record['stages'][stage]['error_code']=code
        if status=='failed':self.record['result']='failed'
        self.commit()
        if self.report:self.report({'stage':stage,'status':status,**({'error_code':code} if code else {})})
    def commit(self):
        try:self.save(self.record)
        except Exception:raise SmokeFailure('record_write_failed') from None
    def phase(self,stage):
        from contextlib import contextmanager
        @contextmanager
        def context():
            self.emit(stage,'running')
            try:yield
            except BaseException as error:
                self.emit(stage,'failed',error_code(error,stage));raise
            else:self.emit(stage,'passed')
        return context()


def _endpoint(url):
    parts=urlsplit(url)
    if (parts.scheme!='https' or parts.netloc!='storage.yandexcloud.net'
            or parts.username or parts.password or parts.fragment):raise SmokeFailure('unexpected_endpoint')
    return parts


def _attempt(progress,client,bucket,key,*,negative=False):
    entry={'key':key,'state':'submitting'}
    progress.record['objects'].append(entry);progress.commit()
    call=lambda:client.put_object(Bucket=bucket,Key=key,Body=PAYLOAD,
        ContentType='text/plain',Metadata={'sha256':hashlib.sha256(PAYLOAD).hexdigest()})
    if negative:denied(call);entry['state']='denied'
    else:call();entry['state']='accepted'
    progress.commit()


def _read_checks(progress,config,client,http,key,*,sleep,ttl):
    from private_media_integrity import verify_s3_integrity
    bucket=config.private_media_bucket;digest=hashlib.sha256(PAYLOAD).hexdigest()
    with progress.phase('head_integrity'):
        verify_s3_integrity(client.head_object(Bucket=bucket,Key=key),len(PAYLOAD),digest)
    with progress.phase('get_integrity'):
        response=client.get_object(Bucket=bucket,Key=key);body=response.get('Body')
        try:
            verify_s3_integrity(response,len(PAYLOAD),digest)
            data=body.read(len(PAYLOAD)+1)
        finally:
            if body is not None:body.close()
        if len(data)!=len(PAYLOAD):raise SmokeFailure('content_size_mismatch')
        if hashlib.sha256(data).hexdigest()!=digest:raise SmokeFailure('content_sha256_mismatch')
        progress.record['objects'][0]['state']='accepted';progress.commit()
    signed={}
    with progress.phase('anonymous_get_head'):
        for method,operation in (('GET','get_object'),('HEAD','head_object')):
            url=client.generate_presigned_url(operation,Params={'Bucket':bucket,'Key':key},ExpiresIn=ttl,HttpMethod=method)
            parts=_endpoint(url)
            anonymous=urlunsplit((parts.scheme,parts.netloc,parts.path,'',''))
            if http.request(method,anonymous).status_code!=403:raise SmokeFailure('access_not_denied')
            signed[method]=url
    with progress.phase('signed_get_head'):
        for method,url in signed.items():
            response=http.request(method,url)
            if response.status_code!=200 or (method=='GET' and response.content!=PAYLOAD):raise SmokeFailure('signed_access_failed')
    with progress.phase('signed_url_expiry'):
        sleep(ttl+3)
        for method,url in signed.items():
            if http.request(method,url).status_code!=403:raise SmokeFailure('expiry_not_established')
    with progress.phase('fresh_signed_access'):
        for method,operation in (('GET','get_object'),('HEAD','head_object')):
            fresh=client.generate_presigned_url(operation,Params={'Bucket':bucket,'Key':key},ExpiresIn=ttl,HttpMethod=method)
            _endpoint(fresh);response=http.request(method,fresh)
            if response.status_code!=200 or (method=='GET' and response.content!=PAYLOAD):raise SmokeFailure('fresh_access_failed')


def _legacy_checks(progress,config,key,clients,unavailable,*,readonly):
    if isinstance(clients,(list,tuple)):
        if len(clients)>2:raise SmokeFailure('invalid_configuration')
        clients=dict(zip(LEGACY_LABELS,clients))
    if not isinstance(clients,dict) or set(clients)-set(LEGACY_LABELS):raise SmokeFailure('invalid_configuration')
    if set(unavailable)-set(LEGACY_LABELS) or any(x not in ('no_static_credentials','credentials_not_provided') for x in unavailable.values()):
        raise SmokeFailure('invalid_configuration')
    failed=False
    try:
        progress.emit('legacy_probes','running')
        for label in LEGACY_LABELS:
            client=clients.get(label)
            if client is None:
                progress.record['legacy'][label]={'status':'not tested','reason':unavailable.get(label,'credentials_not_provided'),
                    'absence_is_not_authorization_proof':True}
                progress.commit();continue
            try:
                denied(lambda:client.head_object(Bucket=config.private_media_bucket,Key=key))
                denied(lambda:client.get_object(Bucket=config.private_media_bucket,Key=key))
                if not readonly:_attempt(progress,client,config.private_media_bucket,'media/v1/smoke/legacy-'+uuid4().hex+'.txt',negative=True)
            except Exception as error:
                failed=True
                progress.record['legacy'][label]={'status':'failed','error_code':error_code(error,'legacy_probes')}
            else:
                progress.record['legacy'][label]={'status':'passed','scope':'head-get' if readonly else 'head-get-put'}
            progress.commit()
        if failed:raise SmokeFailure('legacy_probe_failed')
    except BaseException as error:
        progress.emit('legacy_probes','failed',error_code(error,'legacy_probes'));raise
    complete=all(progress.record['legacy'][label]['status']=='passed' for label in LEGACY_LABELS)
    progress.emit('legacy_probes','passed' if complete else 'partial')
    return complete


def _run(config,client,http,*,save,sleep,ttl,legacy_clients,legacy_unavailable,report,existing=None):
    from yandex_media_profile import check_bucket
    if not 5<=ttl<=30:raise SmokeFailure('invalid_configuration')
    progress=Progress(config.private_media_bucket,save,report,readonly=existing is not None)
    with progress.phase('preflight'):check_bucket(config,client)
    if existing is None:
        key='media/v1/smoke/'+uuid4().hex+'.txt'
        with progress.phase('put'):_attempt(progress,client,config.private_media_bucket,key)
    else:
        key=validate_record(existing,config.private_media_bucket)
        progress.record['objects']=[{'key':key,'state':'submitting'}]
        progress.emit('put','skipped-read-only')
    _read_checks(progress,config,client,http,key,sleep=sleep,ttl=ttl)
    if existing is None:
        with progress.phase('out_of_prefix_put'):
            _attempt(progress,client,config.private_media_bucket,'security-smoke-outside/'+uuid4().hex+'.txt',negative=True)
    else:progress.emit('out_of_prefix_put','skipped-read-only')
    full_legacy=_legacy_checks(progress,config,key,legacy_clients,legacy_unavailable or {},readonly=existing is not None)
    progress.record['result']=('partial-read-only' if existing is not None else
                              'passed' if full_legacy else 'partial-legacy-not-tested')
    progress.commit()
    return progress.record['result']


def run_smoke(config,client,http,*,save,sleep=time.sleep,ttl=10,legacy_clients=(),legacy_unavailable=None,report=None):
    return _run(config,client,http,save=save,sleep=sleep,ttl=ttl,legacy_clients=legacy_clients,
                legacy_unavailable=legacy_unavailable,report=report)


def resume_smoke(config,client,http,record,*,save,sleep=time.sleep,ttl=10,legacy_clients=(),legacy_unavailable=None,report=None):
    validate_record(record,config.private_media_bucket)
    return _run(config,client,http,save=save,sleep=sleep,ttl=ttl,legacy_clients=legacy_clients,
                legacy_unavailable=legacy_unavailable,report=report,existing=record)


def validate_record(record,bucket):
    import re
    if (not isinstance(record,dict) or record.get('bucket')!=bucket
            or set(record)-{'version','bucket','mode','stage','stages','objects','legacy','result'}):raise SmokeFailure('invalid_record')
    objects=record.get('objects')
    if not isinstance(objects,list) or not 1<=len(objects)<=4:raise SmokeFailure('invalid_record')
    keys=[]
    for entry in objects:
        if (not isinstance(entry,dict) or set(entry)!={'key','state'}
                or entry['state'] not in ('submitting','accepted','denied')
                or not isinstance(entry['key'],str)
                or not re.fullmatch(r'(?:media/v1/smoke/(?:legacy-)?|security-smoke-outside/)[0-9a-f]{32}\.txt',entry['key'])):
            raise SmokeFailure('invalid_record')
        keys.append(entry['key'])
    main=[k for k in keys if re.fullmatch(r'media/v1/smoke/[0-9a-f]{32}\.txt',k)]
    if len(main)!=1 or keys[0]!=main[0] or len(keys)!=len(set(keys)):raise SmokeFailure('invalid_record')
    if objects[0]['state'] not in ('accepted','submitting'):raise SmokeFailure('invalid_record')
    if 'version' in record and (type(record['version']) is not int or record['version']!=2):raise SmokeFailure('invalid_record')
    return main[0]


def load_record(path,bucket):
    import stat
    from yandex_media_profile import unique
    try:fd=os.open(path,os.O_RDONLY|getattr(os,'O_NOFOLLOW',0))
    except OSError:raise SmokeFailure('invalid_record') from None
    try:
        info=os.fstat(fd)
        if not stat.S_ISREG(info.st_mode) or info.st_uid!=os.getuid() or info.st_mode&0o077 or not 0<info.st_size<=65536:
            raise SmokeFailure('invalid_record')
        data=os.read(fd,65537)
        if len(data)!=info.st_size:raise SmokeFailure('invalid_record')
        try:record=json.loads(data,object_pairs_hook=unique)
        except Exception:raise SmokeFailure('invalid_record') from None
        validate_record(record,bucket)
        return record
    finally:os.close(fd)


def legacy_configuration(config):
    configs={};unavailable={};identities=set();access_keys=set()
    for number,label in enumerate(LEGACY_LABELS,1):
        prefix='PRIVATE_MEDIA_SMOKE_LEGACY_'+str(number)+'_'
        identity=os.environ.get(prefix+'PRINCIPAL')
        key=os.environ.get(prefix+'ACCESS_KEY_ID');secret=os.environ.get(prefix+'SECRET_ACCESS_KEY')
        absent=os.environ.get(prefix+'NO_STATIC_CREDENTIALS','false').strip().lower()
        if absent not in ('true','false') or bool(key)!=bool(secret):raise SmokeFailure('invalid_configuration')
        if identity:
            if identity not in config.private_media_legacy_principals or identity in identities:raise SmokeFailure('invalid_configuration')
            identities.add(identity)
        if absent=='true':
            if not identity or key or secret:raise SmokeFailure('invalid_configuration')
            unavailable[label]='no_static_credentials';continue
        if not key:unavailable[label]='credentials_not_provided';continue
        if not identity or key in access_keys:raise SmokeFailure('invalid_configuration')
        access_keys.add(key)
        configs[label]=replace(config,private_media_access_key_id=key,private_media_secret_access_key=secret)
    return configs,unavailable


def main(argv=None):
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--execute',action='store_true')
    parser.add_argument('--confirm-bucket',required=True)
    parser.add_argument('--record',type=Path,required=True,help='NEW protected output record; never overwritten')
    parser.add_argument('--resume-record',type=Path,help='Existing protected synthetic record; READ ONLY, no PUT')
    parser.add_argument('--ttl',type=int,choices=range(5,31),default=10)
    parser.add_argument('--legacy-probes',action='store_true')
    args=parser.parse_args(argv)
    previous_logging_disable=logging.root.manager.disable;logging.disable(logging.CRITICAL)
    clients=[];current={'stage':'configuration'}
    try:
        from private_media import storage_client
        from yandex_media_profile import validate_configuration
        import httpx
        config=configuration();validate_configuration(config)
        if args.confirm_bucket!=config.private_media_bucket or (args.resume_record and args.execute) or (not args.resume_record and not args.execute):
            raise SmokeFailure('invalid_configuration')
        legacy_configs,unavailable=legacy_configuration(config) if args.legacy_probes else ({},{})
        current['stage']='record'
        source=load_record(args.resume_record,config.private_media_bucket) if args.resume_record else None
        fd=os.open(args.record,os.O_WRONLY|os.O_CREAT|os.O_EXCL,0o600)
        with os.fdopen(fd,'w') as output:
            def save(record):
                current['stage']=record['stage']
                current['legacy']=record['legacy']
                output.seek(0);json.dump(record,output);output.truncate();output.flush();os.fsync(output.fileno())
            def report(event):print(json.dumps(event))
            current['stage']='configuration'
            client=storage_client(config);clients.append(client);legacy={}
            for label,legacy_config in legacy_configs.items():
                probe=storage_client(legacy_config);legacy[label]=probe;clients.append(probe)
            with httpx.Client(verify=True,trust_env=False,follow_redirects=False,timeout=10) as http:
                options=dict(save=save,ttl=args.ttl,legacy_clients=legacy,legacy_unavailable=unavailable,report=report)
                result=resume_smoke(config,client,http,source,**options) if source is not None else run_smoke(config,client,http,**options)
        print(json.dumps({'synthetic_smoke':result,'legacy':current.get('legacy',{})}))
        return 0 if result=='passed' else 2
    except (Exception,KeyboardInterrupt) as error:
        code=error_code(error,current['stage'])
        if current['stage']=='configuration' and code=='probe_failed':code='invalid_configuration'
        if current['stage']=='record' and code=='probe_failed':code='invalid_record'
        print(json.dumps({'synthetic_smoke':'failed','stage':current['stage'],'error_code':code,'legacy':current.get('legacy',{})}))
        return 1
    finally:
        for client in clients:
            try:client.close()
            except Exception:pass
        logging.disable(previous_logging_disable)


if __name__=='__main__':raise SystemExit(main())

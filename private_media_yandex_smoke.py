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
        if error.response.get('ResponseMetadata',{}).get('HTTPStatusCode')==403:return
        raise RuntimeError('Negative SDK probe did not establish access denial') from None
    if isinstance(response,dict) and response.get('Body') is not None:response['Body'].close()
    raise RuntimeError('Negative SDK probe unexpectedly succeeded')


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


def run_smoke(config,client,http,*,save,sleep=time.sleep,ttl=10,legacy_clients=()):
    from yandex_media_profile import check_bucket
    if not 5<=ttl<=30:raise ValueError('Unsupported smoke TTL')
    if legacy_clients and len(legacy_clients)!=2:raise ValueError('Both legacy accounts must be tested')
    check_bucket(config,client)
    bucket=config.private_media_bucket
    record={'bucket':bucket,'objects':[],'result':'incomplete'}
    payload=b'Bot-Max synthetic private storage smoke\n'
    digest=hashlib.sha256(payload).hexdigest()

    def attempt(key,producer,expect_denied=False):
        entry={'key':key,'state':'submitting'}
        record['objects'].append(entry);save(record)
        call=lambda:producer.put_object(Bucket=bucket,Key=key,Body=payload,
                  ContentType='text/plain',Metadata={'sha256':digest})
        if expect_denied:denied(call);entry['state']='denied'
        else:call();entry['state']='accepted'
        save(record)

    key='media/v1/smoke/'+uuid4().hex+'.txt'
    attempt(key,client)
    head=client.head_object(Bucket=bucket,Key=key)
    if head.get('ContentLength')!=len(payload) or head.get('Metadata',{}).get('sha256')!=digest:
        raise RuntimeError('Synthetic HEAD integrity failed')
    body=client.get_object(Bucket=bucket,Key=key)['Body']
    try:data=body.read(len(payload)+1)
    finally:body.close()
    if data!=payload:raise RuntimeError('Synthetic GET integrity failed')

    signed={}
    for method,operation in (('GET','get_object'),('HEAD','head_object')):
        url=client.generate_presigned_url(operation,Params={'Bucket':bucket,'Key':key},ExpiresIn=ttl,HttpMethod=method)
        parts=urlsplit(url)
        if (parts.scheme!='https' or parts.hostname!='storage.yandexcloud.net'
                or parts.port is not None or parts.username or parts.password or parts.fragment):
            raise RuntimeError('Unexpected signed URL endpoint')
        anonymous=urlunsplit((parts.scheme,parts.netloc,parts.path,'',''))
        # Missing objects/redirects/network errors are not evidence of privacy.
        if http.request(method,anonymous).status_code!=403:raise RuntimeError('Anonymous access was not denied')
        response=http.request(method,url)
        if response.status_code!=200 or (method=='GET' and response.content!=payload):
            raise RuntimeError('Active signed access failed')
        signed[method]=url
    # Neither signed URL nor credentials are retained in the record or output.
    sleep(ttl+3)
    for method,url in signed.items():
        if http.request(method,url).status_code!=403:raise RuntimeError('Signed URL expiry was not established')
    # Distinguish expiry from revoked credentials, deleted objects or a broad
    # access outage: the same credentials must still sign fresh working URLs.
    for method,operation in (('GET','get_object'),('HEAD','head_object')):
        fresh=client.generate_presigned_url(operation,Params={'Bucket':bucket,'Key':key},ExpiresIn=ttl,HttpMethod=method)
        parts=urlsplit(fresh)
        if parts.scheme!='https' or parts.netloc!='storage.yandexcloud.net':raise RuntimeError('Unexpected fresh signed endpoint')
        response=http.request(method,fresh)
        if response.status_code!=200 or (method=='GET' and response.content!=payload):
            raise RuntimeError('Fresh signed access after expiry failed')

    # This can create only a NEW synthetic object if the policy is wrong.
    # Save its exact key BEFORE attempting PUT; never overwrite/delete anything.
    attempt('security-smoke-outside/'+uuid4().hex+'.txt',client,expect_denied=True)
    for legacy in legacy_clients:
        denied(lambda:legacy.head_object(Bucket=bucket,Key=key))
        denied(lambda:legacy.get_object(Bucket=bucket,Key=key))
        attempt('media/v1/smoke/legacy-'+uuid4().hex+'.txt',legacy,expect_denied=True)
    record['result']='passed' if len(legacy_clients)==2 else 'partial-legacy-not-tested'
    save(record)
    return record['result']


def main(argv=None):
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--execute',action='store_true')
    parser.add_argument('--confirm-bucket',required=True)
    parser.add_argument('--record',type=Path,required=True)
    parser.add_argument('--ttl',type=int,choices=range(5,31),default=10)
    parser.add_argument('--legacy-probes',action='store_true')
    args=parser.parse_args(argv)
    # Suppress third-party request/exception logs even under inherited logging.
    previous_logging_disable=logging.root.manager.disable
    logging.disable(logging.CRITICAL)
    clients=[]
    try:
        from private_media import storage_client
        from yandex_media_profile import validate_configuration
        import httpx
        config=configuration()
        validate_configuration(config)
        if not args.execute or args.confirm_bucket!=config.private_media_bucket:
            raise ValueError('Explicit synthetic bucket confirmation is required')
        # Check both identities before making ANY network request or PUT.
        legacy_configs=[];identities=set()
        if args.legacy_probes:
            for number in (1,2):
                prefix='PRIVATE_MEDIA_SMOKE_LEGACY_'+str(number)+'_'
                identity=os.environ.get(prefix+'PRINCIPAL')
                key=os.environ.get(prefix+'ACCESS_KEY_ID');secret=os.environ.get(prefix+'SECRET_ACCESS_KEY')
                if identity not in config.private_media_legacy_principals or identity in identities or not key or not secret:
                    raise ValueError('Verified distinct legacy probe credentials required')
                identities.add(identity)
                legacy_configs.append(replace(config,private_media_access_key_id=key,private_media_secret_access_key=secret))
        # Exclusive 0600, no overwrite/symlink target; keep outside public/Git dirs.
        fd=os.open(args.record,os.O_WRONLY|os.O_CREAT|os.O_EXCL,0o600)
        with os.fdopen(fd,'w') as output:
            def save(record):
                output.seek(0);json.dump(record,output);output.truncate();output.flush();os.fsync(output.fileno())
            save({'result':'not-started','objects':[]})
            client=storage_client(config);clients.append(client)
            legacy=[]
            for legacy_config in legacy_configs:
                probe=storage_client(legacy_config);legacy.append(probe);clients.append(probe)
            with httpx.Client(verify=True,trust_env=False,follow_redirects=False,timeout=10) as http:
                result=run_smoke(config,client,http,save=save,ttl=args.ttl,legacy_clients=legacy)
        print(json.dumps({'synthetic_smoke':result}))
        return 0 if result=='passed' else 2
    except Exception:
        print(json.dumps({'synthetic_smoke':'failed','action':'stop; inspect the private record; do not retry an unknown PUT'}))
        return 1
    finally:
        for client in clients:
            try:client.close()
            except Exception:pass
        logging.disable(previous_logging_disable)


if __name__=='__main__':raise SystemExit(main())

"""Read-only legacy inventory, idempotent private copies, narrow reviewed cutover.

No list-bucket, anonymous HEAD, ACL mutation, delete, provider call or generation.
Inventory plans are sensitive metadata: stdout is counts only, files mode 0600.
"""
from __future__ import annotations

import argparse
from dataclasses import asdict, dataclass
import hashlib
import json
import os
from pathlib import Path
import sqlite3
from urllib.parse import quote, unquote, urlsplit

from private_media import Access, Owner, PrivateMedia, MediaError, MediaAccessDenied, media_scope


@dataclass(frozen=True)
class LegacyItem:
    table: str
    column: str
    rowid: int
    path: tuple
    object_key: str
    value_hash: str
    owner_id: str | None
    order_id: str | None
    proof: str


def _identifier(name):
    return '"'+name.replace('"','""')+'"'


def legacy_key(config, value):
    if not isinstance(value,str): return None
    base = config.storage_public_base_url or 'https://storage.yandexcloud.net/'+quote(config.storage_bucket or '',safe='')
    p=urlsplit(value)
    if not value.startswith(base.rstrip('/')+'/') or p.scheme!='https' or p.query or p.fragment or p.username or p.password:
        return None
    suffix=value[len(base.rstrip('/'))+1:]
    key=unquote(suffix)
    if (not key or len(key.encode())>1024 or '\\' in key or '\x00' in key
            or any(s in ('','.', '..') for s in key.split('/')) or quote(key,safe='/')!=suffix):
        return None
    return key


def _owner(db, table, row):
    owner=row.get('user_id')
    order=row.get('order_id')
    if table=='photo_results':
        match=db.execute('SELECT user_id FROM photo_orders WHERE order_id=?',(order,)).fetchone()
        owner=match[0] if match else None
    elif table=='cartoon_assets':
        match=db.execute('SELECT owner_id FROM cartoon_projects WHERE id=?',(row.get('project_id'),)).fetchone()
        owner=match[0] if match else None
    return str(owner) if type(owner) is int and owner>0 else None, str(order) if order else None


def _proof(table, column, path, key):
    allowed={('photo_results','image_url'):'vision-boards/',
             ('cartoon_orders','result_url'):'videos/',
             ('musical_orders','result_url'):'videos/',
             ('audio_orders','preview_url'):'audio-previews/',
             ('cartoon_assets','public_url'):'studio/'}
    prefix=allowed.get((table,column))
    if not path and prefix and (key.startswith(prefix) or (table=='cartoon_assets' and key.startswith('multstudio/'))):
        return 'generated-output'
    return 'ownership-unverified'


def _walk(value, path=()):
    if isinstance(value,str):yield path,value
    elif isinstance(value,dict):
        for k,v in value.items():yield from _walk(v,path+(k,))
    elif isinstance(value,list):
        for i,v in enumerate(value):yield from _walk(v,path+(i,))


def inventory(source, config):
    """Does not open storage or create either SQLite file."""
    result=[]
    with sqlite3.connect(Path(source).resolve().as_uri()+'?mode=ro',uri=True) as db:
        db.row_factory=sqlite3.Row
        db.execute('BEGIN')
        tables=[r[0] for r in db.execute("SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'")]
        for table in tables:
            try: rows=db.execute('SELECT rowid AS _media_rowid,* FROM '+_identifier(table))
            except sqlite3.OperationalError: continue  # WITHOUT ROWID: report unsupported, never guess a locator.
            for raw in rows:
                row=dict(raw)
                for column,text in row.items():
                    if not isinstance(text,str):continue
                    value=text
                    if text[:1] in ('{','['):
                        try: value=json.loads(text)
                        except (ValueError,TypeError):continue
                    for path,leaf in _walk(value):
                        key=legacy_key(config,leaf)
                        if key is None:continue
                        owner,order=_owner(db,table,row)
                        proof=_proof(table,column,path,key) if owner else 'ownership-unverified'
                        result.append(LegacyItem(table,column,row['_media_rowid'],path,key,
                            hashlib.sha256(leaf.encode()).hexdigest(),owner,order,proof))
    return result


def _review_current(source,config,item):
    matches=[x for x in inventory(source,config) if (x.table,x.column,x.rowid,x.path)==(item.table,item.column,item.rowid,item.path)]
    if len(matches)!=1 or matches[0]!=item:
        raise MediaError('Migration source changed or ownership cannot be verified')
    if item.proof!='generated-output' or item.owner_id is None:
        raise MediaAccessDenied('Legacy ownership requires separate manual review')


def copy_one(source,config,item,legacy_sdk,private_store):
    """Only a new private copy; existing refs, ACL, objects and orders untouched."""
    _review_current(source,config,item)
    try:
        meta=legacy_sdk.head_object(Bucket=config.storage_bucket,Key=item.object_key)
        if not 0<meta['ContentLength']<=200*1024*1024: raise MediaError('Migration source size rejected')
        response=legacy_sdk.get_object(Bucket=config.storage_bucket,Key=item.object_key)
        body=response['Body']
        try:data=body.read(200*1024*1024+1)
        finally:body.close()
        if len(data)!=meta['ContentLength']:raise MediaError('Migration source incomplete')
        mime=meta.get('ContentType','application/octet-stream').split(';')[0].strip()
    except MediaError:raise
    except Exception:raise MediaError('Authorized legacy media read failed') from None
    operation='migration:'+hashlib.sha256(json.dumps([config.storage_bucket,item.object_key,item.owner_id,item.order_id]).encode()).hexdigest()
    access=Access(Owner('max',item.owner_id),'service',item.order_id or 'migration:'+item.value_hash)
    return private_store.put(data,mime,access,operation_id=operation)


def activity(source):
    """Conservative DB warning, not proof that provider/RAM work has drained."""
    reasons=set()
    with sqlite3.connect(Path(source).resolve().as_uri()+'?mode=ro',uri=True) as db:
        db.row_factory=sqlite3.Row
        tables={r[0] for r in db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        for table in ('audio_orders','musical_orders','cartoon_orders','photo_orders','animation_jobs','cartoon_jobs','ded_moroz_orders','pending_videos'):
            if table not in tables:continue
            for row in db.execute('SELECT * FROM '+_identifier(table)):
                r=dict(row)
                # Unpaid invoices and unapproved drafts alone are not provider jobs.
                if r.get('status') in ('pending','draft','quoted','stale','cancelled') and not any(
                    r.get(k) for k in ('running','job_id','task_id','provider_id','submit_started')):
                    continue
                done=(r.get('status') in ('completed','done','delivered','cancelled')
                      or r.get('delivered')==1 or r.get('generation_status')=='completed')
                if not done:reasons.add(table+':requires-review')
                if r.get('running') or r.get('lease_until',0)>__import__('time').time():reasons.add(table+':inflight')
    return sorted(reasons)


def cutover_completed_photo(source,config,item,reference,private_store,*,service_stopped=False,providers_checked=False,memory_drained=False):
    """Narrow CAS, no finance/schema/status updates. All other cutovers blocked.

    Acknowledgements are operator attestations, not fabricated provider proofs.
    The runbook requires verifying actual service/processes/provider GET statuses.
    """
    if not (service_stopped and providers_checked and memory_drained):raise MediaError('Maintenance verification required')
    if (item.table,item.column,item.path)!=('photo_results','image_url',()):raise MediaError('This scenario needs a separately approved cutover')
    with sqlite3.connect(Path(source).resolve().as_uri()+'?mode=ro',uri=True) as db:
        current=db.execute('SELECT r.image_url,p.user_id,p.order_id FROM photo_results r JOIN photo_orders p ON p.order_id=r.order_id WHERE r.rowid=?',(item.rowid,)).fetchone()
    if not current or str(current[1])!=item.owner_id or current[2]!=item.order_id:
        raise MediaAccessDenied('Migration owner or order changed')
    if current[0]==reference:
        private_store.stat(reference,Access(Owner('max',item.owner_id),'service',item.order_id))
        return 'already-private'
    _review_current(source,config,item)
    if activity(source):raise MediaError('Unresolved active or pending jobs block cutover')
    access=Access(Owner('max',item.owner_id),'service',item.order_id)
    private_store.stat(reference,access)
    # Verify private copy is the exact original, not another owned image.
    with private_store._db() as registry:
        row=private_store._row(registry,reference,access)
        expected='migration:'+hashlib.sha256(json.dumps([config.storage_bucket,item.object_key,item.owner_id,item.order_id]).encode()).hexdigest()
        if row['operation_id']!=expected:raise MediaError('Wrong private copy for migration source')
    with sqlite3.connect(Path(source).resolve().as_uri()+'?mode=rw',uri=True) as db:
        db.execute('BEGIN IMMEDIATE')
        r=db.execute('SELECT image_url,delivered FROM photo_results WHERE rowid=?',(item.rowid,)).fetchone()
        if not r or r[1]!=1:raise MediaError('Only delivered photo results may cut over')
        if r[0]==reference:return 'already-private'
        if hashlib.sha256(r[0].encode()).hexdigest()!=item.value_hash:raise MediaError('Migration source changed')
        db.execute('UPDATE photo_results SET image_url=? WHERE rowid=?',(reference,item.rowid))
    return 'private-reference-saved'


def main():
    parser=argparse.ArgumentParser(description='Read-only legacy-media inventory; no bucket/ACL changes')
    parser.add_argument('--database',required=True)
    parser.add_argument('--plan',help='Optional sensitive metadata file, created exclusively with mode 0600')
    args=parser.parse_args()
    from config import Config
    items=inventory(args.database,Config.from_env())
    if args.plan:
        fd=os.open(args.plan,os.O_WRONLY|os.O_CREAT|os.O_EXCL,0o600)
        with os.fdopen(fd,'w') as f:json.dump([asdict(x) for x in items],f,ensure_ascii=False,indent=2)
    print(json.dumps({'references':len(items),'generated_output_candidates':sum(x.proof=='generated-output' for x in items),
                      'ownership_unverified':sum(x.proof!='generated-output' for x in items),'dry_run':True}))


if __name__=='__main__':
    try:main()
    except Exception:
        import sys
        print('private_media_inventory_failed',file=sys.stderr)
        raise SystemExit(1) from None

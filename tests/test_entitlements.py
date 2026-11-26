"""Offline synthetic financial invariants; no MAX, payments, email or providers."""
import asyncio
from dataclasses import asdict, replace
import hashlib
import json
from pathlib import Path
import sqlite3
import subprocess
import sys
from uuid import uuid4

import pytest
from accounts import AccountRepository
from accounts.errors import AccountStoreBusy, AccountStorageError, SchemaCompatibilityError
from accounts.migrations import migrate, versions, inspect_schema
from entitlements import EntitlementService, GrantSpec, VerifiedGrantEvent, VerifiedOutcome
from entitlements.catalog import PRODUCTS
from entitlements.errors import Conflict, InvalidOperation, InvalidTransition, InsufficientRights, NotFound


@pytest.fixture
async def rights_env(tmp_path):
    path=tmp_path/'synthetic.sqlite3'; path.touch()
    await migrate(path)
    repo=AccountRepository(path)
    a,b=await repo.create_account(),await repo.create_account()
    return path,EntitlementService(path,clock=lambda:1000),a.id,b.id


def event(owner, n=5, **kw):
    return VerifiedGrantEvent(owner,'payment.prodamus','receipt-test',900,
        (GrantSpec('image_credit',n,('postcard','vision_board'),**kw),))


def query(path, sql, args=()):
    with sqlite3.connect(path) as d:return d.execute(sql,args).fetchall()


def dbdump(path):
    with sqlite3.connect('file:'+str(path)+'?mode=ro',uri=True) as d:return '\n'.join(d.iterdump())


async def reserve(s,a, *, key='reserve-1', job='job-1', n=1):
    return await s.reserve(a,'image_credit','postcard',n,generation_ref=job,idempotency_key=key)


async def test_grant_event_retry_global_identity_and_payload(rights_env):
    p,s,a,b=rights_env;e=event(a)
    first=await s.grant(e,idempotency_key='grant')
    assert first==await s.grant(e,idempotency_key='grant')==await s.grant(e,idempotency_key='another-key')
    assert await s.get_available(a,'image_credit','postcard')==5
    for e2,key in [(event(a,6),'grant'),(event(a,6),'new-key'),(event(b),'grant'),(event(b),'new-key')]:
        with pytest.raises(Conflict):await s.grant(e2,idempotency_key=key)
    assert query(p,'SELECT COUNT(*) FROM entitlement_events')==[(1,)]
    assert query(p,'SELECT operation,quantity FROM entitlement_ledger')==[('grant',5)]
    assert await s.get_available(b,'image_credit','postcard')==0


async def test_atomic_multi_right_payment_and_duplicate_package(rights_env):
    p,s,a,b=rights_env
    specs=(GrantSpec('image_credit',15,('postcard','vision_board'),package_ref='pack-799'),
           GrantSpec('animation',1,('animation',),'bonus',package_ref='pack-799'))
    e=replace(event(a),grants=specs)
    assert len(await s.grant(e,idempotency_key='multi'))==2
    assert await s.get_available(a,'animation','animation')==1
    assert {v.source for v in await s.list_packages(a)}=={'paid','bonus'}
    with pytest.raises(Conflict):await s.grant(replace(e,external_id='second-receipt'),idempotency_key='multi-2')
    assert query(p,'SELECT COUNT(*) FROM entitlement_events')==[(1,)]


async def test_partial_grant_rolls_back_everything(rights_env):
    p,s,a,b=rights_env
    specs=(GrantSpec('image_credit',2,('postcard',),package_ref='same-pack'),)*2
    before=dbdump(p)
    with pytest.raises(Conflict):await s.grant(replace(event(a),grants=specs),idempotency_key='rollback')
    assert dbdump(p)==before


@pytest.mark.parametrize('kind', list(PRODUCTS))
async def test_product_specific_units_and_scoped_orders(rights_env,kind):
    p,s,a,b=rights_env
    product=sorted(PRODUCTS[kind])[0]
    order=None if kind in ('image_credit','animation') else 'order-1'
    spec=GrantSpec(kind,1,(product,),order_ref=order)
    await s.grant(replace(event(a),grants=(spec,)),idempotency_key='grant')
    assert await s.get_available(a,kind,product,order_ref=order)==1
    r=await s.reserve(a,kind,product,1,order_ref=order,generation_ref='job',idempotency_key='reserve')
    assert r.kind==kind and r.quantity==1
    if order:
        assert await s.get_available(a,kind,product,order_ref='other-order')==0
        with pytest.raises(Conflict):
            await s.grant(replace(event(a),external_id='new',grants=(spec,)),idempotency_key='other-grant')
    for other in PRODUCTS:
        if other!=kind:
            target=sorted(PRODUCTS[other])[0]
            scope=None if other in ('image_credit','animation') else 'order-1'
            assert await s.get_available(a,other,target,order_ref=scope)==0


async def test_multiple_lots_expiry_and_split_reservation(rights_env):
    p,s,a,b=rights_env
    await s.grant(event(a,2,expires_at=1050),idempotency_key='grant')
    await s.grant(replace(event(a,3,bucket='bonus'),external_id='bonus',source='campaign'),idempotency_key='bonus')
    r=await reserve(s,a,n=4)
    assert sorted(x[0] for x in query(p,'SELECT quantity FROM entitlement_allocations'))==[2,2]
    assert await s.get_available(a,'image_credit','vision_board')==1
    s.clock=lambda:2000
    # Expired allocation remains held and can be settled, never automatically returned.
    await s.commit(a,r.id,verification=VerifiedOutcome('success','result'),idempotency_key='commit')
    views=await s.list_packages(a)
    assert sum(v.consumed for v in views)==4 and sum(v.available for v in views)==1
    assert all(v.available>=0 for v in views)


async def test_expiry_boundary_and_release_after_expiry(rights_env):
    p,s,a,b=rights_env
    await s.grant(event(a,1,expires_at=1001),idempotency_key='grant')
    r=await reserve(s,a)
    s.clock=lambda:1001
    await s.release(a,r.id,verification=VerifiedOutcome('not_submitted','local-cancel'),idempotency_key='release')
    assert await s.get_available(a,'image_credit','postcard')==0
    assert (await s.list_packages(a))[0].expired==1
    with pytest.raises(InsufficientRights):await reserve(s,a,key='r2',job='j2')


async def test_shared_credit_scope_and_restricted_campaign(rights_env):
    p,s,a,b=rights_env
    await s.grant(event(a,1,bucket='promo'),idempotency_key='grant')
    await s.reserve(a,'image_credit','vision_board',1,generation_ref='board',idempotency_key='board')
    assert await s.get_available(a,'image_credit','postcard')==0
    with pytest.raises(InvalidOperation):await s.get_available(a,'image_credit','animation')


async def test_reservation_retry_same_job_and_isolation(rights_env):
    p,s,a,b=rights_env
    await s.grant(event(a),idempotency_key='grant')
    r=await reserve(s,a)
    assert r==await reserve(s,a)==await reserve(s,a,key='new-key')
    for key,n,job in [('reserve-1',2,'job-1'),('new-key-2',2,'job-1'),('reserve-1',1,'other')]:
        with pytest.raises(Conflict):await reserve(s,a,key=key,n=n,job=job)
    with pytest.raises(NotFound):await s.get_reservation(b,r.id)
    with pytest.raises(NotFound):await s.commit(b,r.id,verification=VerifiedOutcome('success','evidence'),idempotency_key='foreign')
    assert await s.get_available(a,'image_credit','postcard')==4


@pytest.mark.parametrize('ending',['commit','release'])
async def test_terminal_settlement_idempotency_and_opposite_forbidden(rights_env,ending):
    p,s,a,b=rights_env
    await s.grant(event(a,1),idempotency_key='grant')
    r=await reserve(s,a)
    proof=VerifiedOutcome('success' if ending=='commit' else 'failed','confirmed-provider-status')
    fn=getattr(s,ending)
    done=await fn(a,r.id,verification=proof,idempotency_key=ending)
    assert done==await fn(a,r.id,verification=proof,idempotency_key=ending)
    assert done==await fn(a,r.id,verification=proof,idempotency_key=ending+'-retry')
    with pytest.raises(Conflict):await fn(a,r.id,verification=replace(proof,evidence_ref='changed'),idempotency_key=ending)
    opposite='release' if ending=='commit' else 'commit'
    proof2=VerifiedOutcome('failed' if opposite=='release' else 'success','other')
    with pytest.raises(InvalidTransition):await getattr(s,opposite)(a,r.id,verification=proof2,idempotency_key='opposite')
    assert query(p,'SELECT COUNT(*) FROM entitlement_ledger WHERE operation=?',(ending,))==[(1,)]
    assert await s.get_available(a,'image_credit','postcard')==(0 if ending=='commit' else 1)


async def test_unknown_provider_holds_reservation_forever_and_blocks_resubmit(rights_env):
    p,s,a,b=rights_env
    await s.grant(event(a,1),idempotency_key='grant')
    r=await reserve(s,a)
    await s.mark_submitting(a,r.id,idempotency_key='barrier')
    await s.mark_uncertain(a,r.id,idempotency_key='timeout')
    s.clock=lambda:10**10
    assert (await reserve(s,a,key='retry')).state=='uncertain'
    assert await s.get_available(a,'image_credit','postcard')==0
    with pytest.raises(InvalidTransition):await s.mark_submitting(a,r.id,idempotency_key='resubmit')
    for proof in (None,VerifiedOutcome('unknown','timeout'),VerifiedOutcome('not_submitted','timeout')):
        with pytest.raises((InvalidOperation,InvalidTransition)):
            await s.release(a,r.id,verification=proof,idempotency_key='unsafe-release')
    assert (await s.get_reservation(a,r.id)).state=='uncertain'
    await s.release(a,r.id,verification=VerifiedOutcome('failed','provider-final-failed'),idempotency_key='final')
    assert await s.get_available(a,'image_credit','postcard')==1


async def test_compensation_append_only_bounded_and_deduplicated(rights_env):
    p,s,a,b=rights_env
    await s.grant(event(a,2),idempotency_key='grant')
    r=await reserve(s,a,n=2)
    await s.commit(a,r.id,verification=VerifiedOutcome('success','delivered'),idempotency_key='commit')
    op=query(p,"SELECT operation_id FROM entitlement_ledger WHERE operation='commit'")[0][0]
    kwargs=dict(evidence_ref='support-ticket',expires_at=None,idempotency_key='correction')
    gid=await s.compensate(a,op,1,**kwargs)
    assert gid==await s.compensate(a,op,1,**kwargs)
    assert gid==await s.compensate(a,op,1,**{**kwargs,'idempotency_key':'correction-again'})
    with pytest.raises(Conflict):await s.compensate(a,op,2,evidence_ref='another',expires_at=None,idempotency_key='too-much')
    with pytest.raises(NotFound):await s.compensate(b,op,1,evidence_ref='foreign',expires_at=None,idempotency_key='foreign')
    assert await s.get_available(a,'image_credit','postcard')==1
    assert (await s.get_reservation(a,r.id)).state=='committed'
    assert query(p,"SELECT quantity FROM entitlement_ledger WHERE operation='commit'")==[(2,)]
    assert query(p,"SELECT parent_operation_id FROM entitlement_ledger WHERE operation='compensate'")==[(op,)]


@pytest.mark.parametrize('value',[True,False,0,-1,1.0,'1',None,10**9+1])
async def test_noninteger_or_unbounded_grants_and_reserves_rejected(rights_env,value):
    p,s,a,b=rights_env
    with pytest.raises(InvalidOperation):await s.grant(event(a,value),idempotency_key='bad')
    with pytest.raises(InvalidOperation):await reserve(s,a,n=value)
    assert query(p,'SELECT COUNT(*) FROM entitlement_ledger')==[(0,)]


@pytest.mark.parametrize('change',[
    {'kind':'universal_money'}, {'bucket':'unknown'}, {'products':('animation',)},
    {'products':()}, {'products':('postcard','postcard')}, {'products':['postcard']},
    {'expires_at':1.5}, {'expires_at':True}, {'order_ref':'https://private.example/a'},
    {'quantity':-2}, {'package_ref':'private person name'},
])
async def test_invalid_grant_restrictions(rights_env,change):
    p,s,a,b=rights_env
    e=replace(event(a),grants=(replace(event(a).grants[0],**change),))
    with pytest.raises(InvalidOperation):await s.grant(e,idempotency_key='bad')


@pytest.mark.parametrize('metadata',[(('prompt','secret'),),(('email','a@example.test'),),(('reason_code','https://x'),),
                                     (('reason_code','a'),('reason_code','b')),{'reason_code':'safe'}])
async def test_metadata_allowlist_excludes_private_payloads(rights_env,metadata):
    p,s,a,b=rights_env
    with pytest.raises(InvalidOperation):await s.grant(replace(event(a),metadata=metadata),idempotency_key='bad')


async def test_untrusted_events_and_outcomes_are_not_paid_proof(rights_env):
    p,s,a,b=rights_env
    for e in ({'paid':True,'quantity':100},None,'paid'):
        with pytest.raises(InvalidOperation):await s.grant(e,idempotency_key='bad')
    await s.grant(event(a),idempotency_key='grant');r=await reserve(s,a)
    with pytest.raises(InvalidOperation):await s.commit(a,r.id,verification={'success':True},idempotency_key='bad')
    assert (await s.get_reservation(a,r.id)).state=='reserved'


async def test_last_unit_concurrent_connections_and_same_key(rights_env):
    p,s,a,b=rights_env
    await s.grant(event(a,1),idempotency_key='grant')
    results=await asyncio.gather(*(reserve(EntitlementService(p),a,key=f'key-{i}',job=f'job-{i}') for i in range(12)),return_exceptions=True)
    assert sum(not isinstance(x,Exception) for x in results)==1
    assert sum(isinstance(x,InsufficientRights) for x in results)==11
    assert await s.get_available(a,'image_credit','postcard')==0
    r=next(x for x in results if not isinstance(x,Exception))
    results=await asyncio.gather(*(s.commit(a,r.id,verification=VerifiedOutcome('success','proof'),idempotency_key='settle') for _ in range(8)))
    assert all(x.state=='committed' for x in results)
    assert query(p,"SELECT COUNT(*) FROM entitlement_ledger WHERE operation='commit'")==[(1,)]


async def test_sqlite_busy_no_partial_event(rights_env):
    p,s,a,b=rights_env
    locked=sqlite3.connect(p);locked.execute('BEGIN IMMEDIATE')
    try:
        with pytest.raises(AccountStoreBusy):await EntitlementService(p,timeout=.02).grant(event(a),idempotency_key='busy')
    finally:locked.rollback();locked.close()
    assert query(p,'SELECT COUNT(*) FROM entitlement_events')==[(0,)]
    await s.grant(event(a),idempotency_key='busy')


@pytest.mark.parametrize('table',['entitlement_events','entitlement_grants','entitlement_allocations','entitlement_commands','entitlement_ledger'])
@pytest.mark.parametrize('verb',['UPDATE','DELETE'])
async def test_sql_append_only_guards(rights_env,table,verb):
    p,s,a,b=rights_env
    await s.grant(event(a),idempotency_key='grant');await reserve(s,a)
    with sqlite3.connect(p) as d:
        column=d.execute('PRAGMA table_info('+table+')').fetchone()[1]
        sql=('UPDATE '+table+' SET '+column+'='+column) if verb=='UPDATE' else 'DELETE FROM '+table
        with pytest.raises(sqlite3.IntegrityError):d.execute(sql)


async def test_direct_sql_negative_and_overallocation_guards(rights_env):
    p,s,a,b=rights_env
    await s.grant(event(a,1),idempotency_key='grant');r=await reserve(s,a)
    gid=query(p,'SELECT id FROM entitlement_grants')[0][0]
    with sqlite3.connect(p) as d:
        with pytest.raises(sqlite3.IntegrityError):d.execute('UPDATE entitlement_reservations SET quantity=-1 WHERE id=?',(r.id,))
        other=str(uuid4())
        d.execute('INSERT INTO entitlement_reservations VALUES(?,?,?,?,?,?,?,?,?)',(other,a,'image_credit','postcard',1,None,'another','reserved',1000))
        with pytest.raises(sqlite3.IntegrityError):d.execute('INSERT INTO entitlement_allocations VALUES(?,?,?)',(other,gid,1))


async def test_reserve_failure_rolls_back_allocations_reservation_and_ledger(rights_env,monkeypatch):
    p,s,a,b=rights_env
    await s.grant(event(a),idempotency_key='grant');before=dbdump(p)
    async def fail(*args,**kwargs):raise RuntimeError('synthetic disk interruption')
    monkeypatch.setattr(s,'_remember',fail)
    with pytest.raises(RuntimeError):await reserve(s,a)
    assert dbdump(p)==before


async def test_sanitized_future_response_excludes_internal_references(rights_env):
    p,s,a,b=rights_env
    await s.grant(event(a,package_ref='payment-private-reference'),idempotency_key='grant')
    value=json.dumps([asdict(v) for v in await s.list_packages(a)])
    for secret in ('payment-private-reference','receipt-test','account_id','external_id','metadata','provider'):
        assert secret not in value
    assert 'available' in value and 'expires_at' in value


async def test_v1_to_v5_upgrade_preserves_old_ddl_checksums_and_legacy_rows(tmp_path):
    p=tmp_path/'synthetic.sqlite3';p.touch()
    with sqlite3.connect(p) as d:d.executescript('CREATE TABLE users(user_id INTEGER PRIMARY KEY,balance INTEGER); INSERT INTO users VALUES(7,9)')
    await migrate(p,target=4)
    prior=query(p,'SELECT version,name,checksum FROM account_schema_migrations ORDER BY version')
    assert len(prior)==4
    old={k:v[:2] for k,v in versions().items() if k<=4}
    assert await migrate(p) and not await migrate(p) and await inspect_schema(p)=='applied'
    assert query(p,'SELECT version,name,checksum FROM account_schema_migrations WHERE version<=4 ORDER BY version')==prior
    assert {k:v[:2] for k,v in versions().items() if k<=4}==old
    assert query(p,'SELECT * FROM users')==[(7,9)]
    with pytest.raises(SchemaCompatibilityError):await migrate(p,target=4)


async def test_import_and_construction_no_io_or_configuration(tmp_path):
    script='''import socket,sqlite3,os
socket.socket.connect=lambda *a: (_ for _ in ()).throw(AssertionError("network"))
sqlite3.connect=lambda *a,**k: (_ for _ in ()).throw(AssertionError("database"))
import entitlements
from entitlements.legacy_reader import LegacyReader
from entitlements.reconciliation import reconcile
s=entitlements.EntitlementService("/does-not-exist/synthetic.sqlite3")
r=LegacyReader("/does-not-exist/synthetic.sqlite3")
assert "config" not in __import__("sys").modules
'''
    run=subprocess.run([sys.executable,'-c',script],capture_output=True,text=True)
    assert run.returncode==0,run.stderr


async def test_multi_process_last_unit_reservation_and_duplicate_settlement(rights_env,tmp_path):
    p,s,a,b=rights_env
    await s.grant(event(a,1),idempotency_key='grant')
    # Each child has its own interpreter, event loop and SQLite connection. The pipe
    # fallback is for sandboxed hosts which prohibit asyncio's AF_UNIX self-pipe.
    script=tmp_path/'synthetic_worker.py'
    script.write_text('''import asyncio,sys,os,json
sys.path.insert(0,sys.argv[1])
class Pipe:
 def __init__(self,fd):self.fd=fd
 def fileno(self):return self.fd
 def recv(self,n):return os.read(self.fd,n)
 def send(self,data):return os.write(self.fd,data)
 def close(self):os.close(self.fd)
def make_pipe(self):
 r,w=os.pipe2(os.O_NONBLOCK|os.O_CLOEXEC)
 self._ssock,self._csock=Pipe(r),Pipe(w)
 self._internal_fds+=1
 self._add_reader(r,self._read_from_self)
asyncio.SelectorEventLoop._make_self_pipe=make_pipe
from entitlements import EntitlementService,VerifiedOutcome,VerifiedGrantEvent,GrantSpec
from entitlements.errors import InsufficientRights,InvalidTransition
async def main():
 s=EntitlementService(sys.argv[2]);owner=sys.argv[3];mode=sys.argv[4];key=sys.argv[5]
 if mode=='reserve':
  try:
   r=await s.reserve(owner,'image_credit','postcard',1,generation_ref=key,idempotency_key=key)
   print('won:'+r.id)
  except InsufficientRights:print('insufficient')
 elif mode=='grant':
  e=VerifiedGrantEvent(owner,'payment.prodamus','parallel-payment',900,(GrantSpec('image_credit',1,('postcard',)),))
  print((await s.grant(e,idempotency_key=key))[0])
 else:
  rid=sys.argv[6]
  try:
   method=getattr(s,mode)
   proof=VerifiedOutcome('success' if mode=='commit' else 'failed','provider-confirmed')
   print((await method(owner,rid,verification=proof,idempotency_key=key)).state)
  except InvalidTransition:print('conflict')
asyncio.run(main())
''')
    def spawn(mode,key,rid=None):
        args=[sys.executable,str(script),str(Path.cwd()),str(p),a,mode,key]
        if rid:args.append(rid)
        return subprocess.Popen(args,stdout=subprocess.PIPE,stderr=subprocess.PIPE,text=True)
    def collect(children):
        result=[]
        for child in children:
            out,err=child.communicate(timeout=30)
            assert child.returncode==0,err
            result.append(out.strip())
        return result
    out=collect([spawn('reserve',f'process-{i}') for i in range(8)])
    assert sum(x.startswith('won:') for x in out)==1 and out.count('insufficient')==7
    rid=next(x.split(':',1)[1] for x in out if x.startswith('won:'))
    settled=collect([spawn(mode,mode+str(i),rid) for i,mode in enumerate(['commit','release']*3)])
    winner=(await s.get_reservation(a,rid)).state
    assert set(settled)<={winner,'conflict'}
    assert query(p,"SELECT COUNT(*) FROM entitlement_ledger WHERE operation IN ('commit','release')")==[(1,)]
    # Receipt-level idempotency works even with different command keys/processes.
    granted=collect([spawn('grant',f'grant-process-{i}') for i in range(5)])
    assert len(set(granted))==1
    assert query(p,"SELECT COUNT(*) FROM entitlement_events WHERE external_id='parallel-payment'")==[(1,)]


@pytest.mark.parametrize('sql',[
    'DROP TRIGGER entitlement_ledger_no_update',
    'DROP INDEX entitlement_unique_order',
    "UPDATE account_schema_migrations SET checksum='wrong' WHERE version=5",
    "CREATE TRIGGER unexpected_financial_trigger AFTER INSERT ON entitlement_grants BEGIN SELECT 1; END",
])
async def test_migration_validator_detects_financial_schema_tampering(rights_env,sql):
    p,s,a,b=rights_env
    with sqlite3.connect(p) as d:d.execute(sql)
    with pytest.raises(SchemaCompatibilityError):await inspect_schema(p)
    with pytest.raises(SchemaCompatibilityError):await migrate(p)


async def test_direct_sql_cross_account_allocation_rejected(rights_env):
    p,s,a,b=rights_env
    await s.grant(event(a),idempotency_key='grant')
    gid=query(p,'SELECT id FROM entitlement_grants')[0][0]
    with sqlite3.connect(p) as d:
        rid=str(uuid4())
        d.execute('INSERT INTO entitlement_reservations VALUES(?,?,?,?,?,?,?,?,?)',(rid,b,'image_credit','postcard',1,None,'foreign','reserved',1000))
        with pytest.raises(sqlite3.IntegrityError):d.execute('INSERT INTO entitlement_allocations VALUES(?,?,?)',(rid,gid,1))


async def test_cancellation_rolls_back_a_partially_created_grant(rights_env,monkeypatch):
    p,s,a,b=rights_env
    before=dbdump(p)
    async def interrupted(*args,**kwargs):raise asyncio.CancelledError()
    monkeypatch.setattr(s,'_ledger',interrupted)
    with pytest.raises(asyncio.CancelledError):await s.grant(event(a),idempotency_key='cancel')
    assert dbdump(p)==before


async def test_same_external_package_cannot_be_reassigned_by_a_second_receipt(rights_env):
    p,s,a,b=rights_env
    await s.grant(event(a,package_ref='globally-unique-purchase'),idempotency_key='grant')
    with pytest.raises(Conflict):
        await s.grant(replace(event(b,package_ref='globally-unique-purchase'),external_id='another-receipt'),idempotency_key='foreign-purchase')
    assert await s.get_available(b,'image_credit','postcard')==0


async def test_compensation_cannot_be_disguised_as_an_unlinked_grant(rights_env):
    p,s,a,b=rights_env
    with pytest.raises(InvalidOperation):await s.grant(event(a,bucket='compensation'),idempotency_key='unlinked')
    assert query(p,'SELECT COUNT(*) FROM entitlement_grants')==[(0,)]


@pytest.mark.parametrize('operation',['commit','release'])
async def test_settlement_rollback_preserves_original_reserve(rights_env,monkeypatch,operation):
    p,s,a,b=rights_env
    await s.grant(event(a),idempotency_key='grant');r=await reserve(s,a)
    before=dbdump(p)
    async def interrupted(*args,**kwargs):raise RuntimeError('synthetic interrupted commit')
    monkeypatch.setattr(s,'_remember',interrupted)
    proof=VerifiedOutcome('success' if operation=='commit' else 'failed','proof')
    with pytest.raises(RuntimeError):await getattr(s,operation)(a,r.id,verification=proof,idempotency_key='settle')
    assert dbdump(p)==before and (await s.get_reservation(a,r.id)).state=='reserved'


@pytest.mark.parametrize('change',[{'kind':[]},{'bucket':{}},{'products':([],)}])
async def test_malformed_typed_assertions_fail_with_safe_domain_error(rights_env,change):
    p,s,a,b=rights_env
    with pytest.raises(InvalidOperation):
        await s.grant(replace(event(a),grants=(replace(event(a).grants[0],**change),)),idempotency_key='bad')

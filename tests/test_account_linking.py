"""Temporary synthetic SQLite, fake mail, offline MAX events. No production services."""
import asyncio
from dataclasses import replace
import hashlib
import re
import sqlite3
from types import SimpleNamespace
from unittest.mock import AsyncMock
from uuid import uuid4

import pytest
from accounts import AccountRepository
from accounts.migrations import migrate, versions, CHECKSUM
from auth import schema as auth_schema, browser_schema
from auth.email_sender import FakeEmailSender
from auth.errors import InvalidCode, InvalidSession, RateLimited
from auth.service import AuthService
from auth.settings import AuthSettings
from account_linking import LinkingService, LinkingSettings
from account_linking.errors import InvalidLink, FreshAuthRequired, LinkingLimited, LinkingUnavailable
from account_linking.security import code_mac, ALPHABET


class Clock:
    now=1700000000
    def __call__(self):return self.now


def read(db,sql,args=()):
    with sqlite3.connect(db) as d:
        d.row_factory=sqlite3.Row
        return [dict(r) for r in d.execute(sql,args)]


@pytest.fixture
async def link_env(tmp_path):
    path=tmp_path/'only-synthetic.sqlite3'
    with sqlite3.connect(path) as d:
        d.executescript('''CREATE TABLE users(user_id INTEGER PRIMARY KEY,balance INTEGER,free_trial_used INTEGER);
        INSERT INTO users VALUES(1001,12,1),(1002,7,1);
        CREATE TABLE payment_orders(order_id TEXT PRIMARY KEY,user_id INTEGER,amount INTEGER,status TEXT);
        INSERT INTO payment_orders VALUES('fake-paid',1001,59000,'paid');''')
    await migrate(path)
    clock=Clock();sender=FakeEmailSender()
    auth=AuthService(path,AuthSettings(secret=b'fake-email-secret-never-production-00000',environment='test'),sender,clock=clock)
    settings=LinkingSettings(secret=b'fake-distinct-linking-key-for-tests-only')
    service=LinkingService(path,settings,clock=clock)
    return SimpleNamespace(path=path,clock=clock,sender=sender,auth=auth,settings=settings,service=service)


async def login(e,email='alice@example.test',context=None):
    request=await e.auth.request_email_code(email,source='203.0.113.1',browser_context=context)
    code=re.search(r'Ваш код для входа: ([0-9]+)',e.sender.messages[-1].body)[1]
    result=await e.auth.verify_email_code(request.challenge_id,code,source='203.0.113.1',browser_context=context)
    return result


async def started(e,**kwargs):
    session=await login(e,**kwargs)
    start=await e.service.start(session.token,source='203.0.113.1')
    return session,start


async def max_ready(e,actor=1001):
    session,start=await started(e)
    await e.service.claim_from_max(actor,start.code)
    await e.service.confirm_from_max(actor,start.operation.operation_id)
    return session,start


def row(e):return read(e.path,'SELECT * FROM account_link_operations ORDER BY rowid DESC LIMIT 1')[0]


async def test_two_sided_success_has_no_financial_or_registration_side_effects(link_env):
    e=link_env;before=read(e.path,'SELECT * FROM users');payments=read(e.path,'SELECT * FROM payment_orders')
    session,start=await started(e)
    assert start.operation.expires_at==e.clock.now+600
    assert len(start.code.replace('-',''))==16 and len(ALPHABET)==32
    assert start.code not in repr(start) and start.code not in str(row(e))
    assert row(e)['code_mac']==code_mac(e.settings.secret,start.code)
    with pytest.raises(InvalidLink):await e.service.confirm(session.token,start.operation.operation_id,source='a')
    claim=await e.service.claim_from_max(1001,start.code.lower())
    assert claim.web_hint=='a***@e***' and 'alice@example.test' not in repr(claim)
    assert claim.status=='pending'
    with pytest.raises(InvalidLink):await e.service.confirm(session.token,start.operation.operation_id,source='a')
    result=await e.service.confirm_from_max(1001,start.operation.operation_id)
    assert result.status=='max_confirmed'
    assert not read(e.path,"SELECT * FROM account_identities WHERE provider='max'")
    assert (await e.service.status(session.token)).max_hint=='…1001'
    done=await e.service.confirm(session.token,start.operation.operation_id,source='a')
    assert done.status=='completed' and row(e)['completed_at']==e.clock.now
    assert (await e.service.identities(session.token))==[{'provider':'email','verified':True},{'provider':'max','verified':True}]
    assert len(read(e.path,'SELECT * FROM accounts'))==1
    assert read(e.path,'SELECT * FROM users')==before and read(e.path,'SELECT * FROM payment_orders')==payments


@pytest.mark.parametrize('mode',['bad','expired','unknown_max','cancel_web','cancel_max','reused','wrong_actor'])
async def test_rejected_codes_and_cancellation(link_env,mode):
    e=link_env;s,start=await started(e);key=start.operation.operation_id
    if mode=='expired':e.clock.now+=600
    elif mode=='cancel_web':await e.service.cancel(s.token,key,source='a')
    elif mode=='cancel_max':await e.service.claim_from_max(1001,start.code);await e.service.cancel_from_max(1001,key)
    elif mode=='reused':
        await e.service.claim_from_max(1001,start.code);await e.service.confirm_from_max(1001,key);await e.service.confirm(s.token,key,source='a')
    elif mode=='wrong_actor':await e.service.claim_from_max(1002,start.code)
    with pytest.raises(InvalidLink):await e.service.claim_from_max(999 if mode=='unknown_max' else 1001,'bad' if mode=='bad' else start.code)
    if mode=='expired':assert row(e)['state']=='expired'
    if mode.startswith('cancel'):assert row(e)['state']=='cancelled'


async def test_max_cannot_substitute_actor_or_web_account(link_env):
    e=link_env;s,start=await started(e);key=start.operation.operation_id
    await e.service.claim_from_max(1001,start.code)
    with pytest.raises(InvalidLink):await e.service.confirm_from_max(1002,key)
    assert row(e)['max_user_id']==1001 and row(e)['state']=='pending'
    for actor in [True,'1001',-1,None]:
        with pytest.raises(InvalidLink):await e.service.claim_from_max(actor,start.code)
    await e.service.confirm_from_max(1001,key)
    other=await login(e,'other@example.test')
    with pytest.raises(InvalidLink):await e.service.confirm(other.token,key,source='a')
    assert (await e.service.status(other.token)) is None


@pytest.mark.parametrize('kind',['revoked','logout_all','expired','idle','stale','other_session','invalid_token'])
async def test_session_guards_at_finalization(link_env,kind):
    e=link_env;session,start=await max_ready(e);token=session.token
    if kind=='revoked':await e.auth.logout(token)
    elif kind=='logout_all':await e.auth.logout_all(token)
    elif kind=='expired':
        with sqlite3.connect(e.path) as d:d.execute('UPDATE auth_sessions SET expires_at=?',(e.clock.now,))
    elif kind=='idle':
        with sqlite3.connect(e.path) as d:d.execute('UPDATE auth_sessions SET idle_expires_at=?',(e.clock.now,))
    elif kind=='stale':e.clock.now+=300
    elif kind=='invalid_token':token='invalid'
    else:
        e.clock.now+=60;token=(await login(e)).token
    error=FreshAuthRequired if kind=='stale' else InvalidLink if kind=='other_session' else InvalidSession
    with pytest.raises(error):await e.service.confirm(token,start.operation.operation_id,source='a')
    assert not read(e.path,"SELECT * FROM account_identities WHERE provider='max'")


async def test_start_requires_fresh_auth_and_supersedes_old_operations(link_env):
    e=link_env;s,first=await started(e)
    await e.service.claim_from_max(1001,first.code);await e.service.confirm_from_max(1001,first.operation.operation_id)
    second=await e.service.start(s.token,source='a')
    assert second.operation.operation_id!=first.operation.operation_id
    assert read(e.path,'SELECT state,reason FROM account_link_operations WHERE operation_id=?',(first.operation.operation_id,))==[{'state':'cancelled','reason':'superseded'}]
    with pytest.raises(InvalidLink):await e.service.confirm(s.token,first.operation.operation_id,source='a')
    e.clock.now+=300
    with pytest.raises(FreshAuthRequired):await e.service.start(s.token,source='a')


@pytest.mark.parametrize('kind',['unbound_to_account','web_has_other','same_account'])
async def test_identity_conflicts_do_not_merge_or_replace(link_env,kind):
    e=link_env;s,start=await started(e);repo=AccountRepository(e.path)
    from accounts import VerifiedIdentity
    from datetime import datetime,timezone
    if kind=='unbound_to_account':await repo.get_or_create_max_account(1001)
    else:await repo._add_verified_identity(s.account.id,verification=VerifiedIdentity('max','1002' if kind=='web_has_other' else '1001',datetime.fromtimestamp(e.clock.now,timezone.utc)))
    before=read(e.path,'SELECT * FROM account_identities');count=len(read(e.path,'SELECT * FROM accounts'))
    await e.service.claim_from_max(1001,start.code)
    result=await e.service.confirm_from_max(1001,start.operation.operation_id)
    if kind=='same_account':
        assert result.status=='max_confirmed';assert (await e.service.confirm(s.token,start.operation.operation_id,source='a')).status=='completed'
    else:assert result.status=='conflict'
    assert read(e.path,'SELECT * FROM account_identities')==before
    assert len(read(e.path,'SELECT * FROM accounts'))==count


async def test_conflict_is_rechecked_inside_final_write_transaction(link_env):
    e=link_env;s,start=await max_ready(e)
    await AccountRepository(e.path).get_or_create_max_account(1001)
    done=await e.service.confirm(s.token,start.operation.operation_id,source='a')
    assert done.status=='conflict'
    assert await AccountRepository(e.path).get_account_by_identity('max','1001')!=s.account


async def test_concurrent_max_claim_and_repeated_finalization(link_env):
    e=link_env;s,start=await started(e);key=start.operation.operation_id
    results=await asyncio.gather(*(e.service.claim_from_max(actor,start.code) for actor in (1001,1002)),return_exceptions=True)
    assert sum(not isinstance(x,Exception) for x in results)==1
    actor=row(e)['max_user_id'];await e.service.confirm_from_max(actor,key)
    done=await asyncio.gather(*(e.service.confirm(s.token,key,source='a') for _ in range(5)))
    assert all(x.status=='completed' for x in done)
    assert len(read(e.path,"SELECT * FROM account_identities WHERE provider='max'"))==1


async def test_two_accounts_cannot_concurrently_acquire_same_max(link_env):
    e=link_env;s,a=await started(e);t,b=await started(e,email='b@example.test')
    for start in (a,b):await e.service.claim_from_max(1001,start.code);await e.service.confirm_from_max(1001,start.operation.operation_id)
    results=await asyncio.gather(e.service.confirm(s.token,a.operation.operation_id,source='a'),e.service.confirm(t.token,b.operation.operation_id,source='b'))
    assert sorted(x.status for x in results)==['completed','conflict']


@pytest.mark.parametrize('action',['revoke','expire'])
async def test_waiting_for_write_lock_rechecks_session_and_server_time(link_env,action):
    e=link_env;s,start=await max_ready(e)
    lock=sqlite3.connect(e.path);lock.execute('BEGIN IMMEDIATE')
    if action=='revoke':lock.execute('UPDATE auth_sessions SET revoked_at=?',(e.clock.now,))
    task=asyncio.create_task(e.service.confirm(s.token,start.operation.operation_id,source='a'))
    await asyncio.sleep(.06)
    if action=='expire':e.clock.now+=600
    lock.commit();lock.close()
    with pytest.raises(InvalidSession if action=='revoke' else FreshAuthRequired):await task
    assert not read(e.path,"SELECT * FROM account_identities WHERE provider='max'")


async def test_insert_and_completion_rollback_together(link_env,monkeypatch):
    e=link_env;s,start=await max_ready(e);insert=e.service.repository.accounts._insert_identity
    async def broken(*args,**kwargs):
        await insert(*args,**kwargs)
        raise RuntimeError('synthetic interruption after identity insert')
    monkeypatch.setattr(e.service.repository.accounts,'_insert_identity',broken)
    with pytest.raises(RuntimeError):await e.service.confirm(s.token,start.operation.operation_id,source='a')
    assert row(e)['state']=='max_confirmed' and row(e)['completed_at'] is None
    assert not read(e.path,"SELECT * FROM account_identities WHERE provider='max'")
    monkeypatch.setattr(e.service.repository.accounts,'_insert_identity',insert)
    assert (await e.service.confirm(s.token,start.operation.operation_id,source='a')).status=='completed'


@pytest.mark.parametrize('scope',['max_attempts','global_attempts','account_starts','source_starts','global_starts','account_actions','source_actions','global_actions'])
async def test_persistent_rate_limits_survive_new_service(link_env,scope):
    e=link_env;s=await login(e);settings=replace(e.settings,**{scope:1});service=LinkingService(e.path,settings,clock=e.clock)
    start=await service.start(s.token,source='a')
    if scope.endswith('attempts'):
        with pytest.raises(InvalidLink):await service.claim_from_max(1001,'wrong')
        call=lambda fresh:fresh.claim_from_max(1002 if scope=='global_attempts' else 1001,start.code)
    elif scope.endswith('starts'):
        call=lambda fresh:fresh.start(s.token,source='a')
    else:
        await service.cancel(s.token,start.operation.operation_id,source='a')
        call=lambda fresh:fresh.cancel(s.token,start.operation.operation_id,source='a')
    with pytest.raises(LinkingLimited):await call(LinkingService(e.path,settings,clock=e.clock))


async def test_operation_attempt_limit_and_collision_protection(link_env,monkeypatch):
    e=link_env;s,start=await started(e)
    for _ in range(e.settings.operation_attempts):await e.service.claim_from_max(1001,start.code)
    with pytest.raises(InvalidLink):await e.service.claim_from_max(1001,start.code)
    assert row(e)['state']=='cancelled'
    monkeypatch.setattr('account_linking.security.new_code',lambda:start.code)
    with pytest.raises(LinkingUnavailable):await e.service.start(s.token,source='a')
    assert len(read(e.path,'SELECT * FROM account_link_operations'))==1


async def test_immutable_versions_and_explicit_migration_v1_to_v4(tmp_path):
    path=tmp_path/'synthetic.sqlite3';path.touch();known=versions()
    assert set(known)=={1,2,3,4} and known[1][1]==CHECKSUM
    for v,ddl in [(2,auth_schema.DDL),(3,browser_schema.DDL)]:
        assert known[v][1]==hashlib.sha256('\n'.join(' '.join(x.split()).rstrip(';') for x in ddl.values()).encode()).hexdigest()
    for v in range(1,5):
        await migrate(path,target=v)
        assert len(read(path,'SELECT * FROM account_schema_migrations'))==v
    assert not await migrate(path)


async def test_no_logging_or_plaintext_persistence(link_env,caplog):
    e=link_env;s,start=await max_ready(e)
    await e.service.confirm(s.token,start.operation.operation_id,source='a')
    for secret in (start.code,start.code.replace('-',''),s.token,'alice@example.test'):
        assert secret not in caplog.text
    assert start.code.encode() not in e.path.read_bytes() and s.token.encode() not in e.path.read_bytes()


async def test_link_code_is_not_a_web_session_or_email_code(link_env):
    e=link_env;session,start=await started(e)
    with pytest.raises(InvalidSession):await e.auth.get_current_account(start.code)
    assert len(read(e.path,'SELECT * FROM auth_sessions'))==1


async def test_cancel_and_confirm_race_is_linearizable(link_env):
    e=link_env;session,start=await max_ready(e);key=start.operation.operation_id
    results=await asyncio.gather(e.service.cancel(session.token,key,source='a'),e.service.confirm(session.token,key,source='a'),return_exceptions=True)
    current=row(e)
    identities=read(e.path,"SELECT * FROM account_identities WHERE provider='max'")
    assert (current['state']=='completed' and len(identities)==1) or (current['state']=='cancelled' and not identities)
    assert all(isinstance(x,InvalidLink) or x.status in ('completed','cancelled') for x in results)

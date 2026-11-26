"""Synthetic DB, fake mail, deterministic server clock; no real email/network/provider."""
import asyncio
from dataclasses import replace
import hashlib
import re
import sqlite3
from unittest.mock import AsyncMock
from uuid import uuid4

import pytest

from accounts.errors import AccountStoreBusy, SchemaCompatibilityError
from accounts.migrations import CHECKSUM, DDL, migrate
from auth.email_sender import FakeEmailSender
from auth.errors import DeliveryFailed, InvalidCode, InvalidSession, RateLimited
from auth.service import AuthService
from auth.settings import AuthSettings


class Clock:
    def __init__(self):self.now=1700000000
    def __call__(self):return self.now
    def add(self,seconds):self.now+=seconds


@pytest.fixture
async def auth_env(tmp_path):
    path=tmp_path/'synthetic-auth.sqlite3'
    with sqlite3.connect(path) as db:
        db.executescript('CREATE TABLE users(user_id INTEGER PRIMARY KEY,balance INTEGER); INSERT INTO users VALUES(123,9);')
    await migrate(path)
    settings=AuthSettings(secret=b'synthetic-auth-only-not-a-production-secret',environment='test')
    clock=Clock();sender=FakeEmailSender()
    return path,settings,clock,sender,AuthService(path,settings,sender,clock=clock)


def rows(path,table):
    with sqlite3.connect(path) as db:
        db.row_factory=sqlite3.Row
        return [dict(x) for x in db.execute('SELECT * FROM '+table)]


def code(sender,index=-1):return re.search(r'Ваш код для входа: ([0-9]+)',sender.messages[index].body)[1]


async def login(env,email='Alice@example.test',source='client-a'):
    _,_,_,sender,service=env
    request=await service.request_email_code(email,source=source)
    return await service.verify_email_code(request.challenge_id,code(sender),source=source)


async def test_first_and_repeat_login_uses_same_account_and_never_legacy_users(auth_env):
    path,_,clock,sender,service=auth_env
    request=await service.request_email_code(' Alice@EXAMPLE.TEST ',source='client-a')
    assert rows(path,'accounts')==[] and len(rows(path,'users'))==1
    assert request.message=='Если адрес указан верно, на него отправлен код для входа.'
    first=await service.verify_email_code(request.challenge_id,code(sender),source='client-a')
    assert (await service.get_current_account(first.token)).id==first.account.id
    clock.add(60);second=await login(auth_env)
    assert first.account.id==second.account.id and first.token!=second.token
    assert len(rows(path,'accounts'))==len(rows(path,'account_identities'))==1
    assert rows(path,'users')==[{'user_id':123,'balance':9}]
    assert len(rows(path,'auth_sessions'))==2


async def test_no_registered_account_flag_in_code_responses(auth_env):
    await login(auth_env)
    _,_,clock,_,s=auth_env;clock.add(60)
    a=await s.request_email_code('Alice@example.test',source='a')
    b=await s.request_email_code('new@example.test',source='b')
    assert a.message==b.message and a.expires_in==b.expires_in
    assert set(a.__dataclass_fields__)=={'challenge_id','expires_in','message'}


@pytest.mark.parametrize('bad',['12345','abcdef','１２３４５６','\ud800',None,'x'*129])
async def test_invalid_codes_do_not_create_accounts(auth_env,bad):
    path,_,_,_,s=auth_env
    req=await s.request_email_code('a@example.test',source='a')
    with pytest.raises(InvalidCode):await s.verify_email_code(req.challenge_id,bad,source='a')
    assert rows(path,'accounts')==[]


async def test_expired_and_used_codes_rejected(auth_env):
    path,settings,clock,sender,s=auth_env
    req=await s.request_email_code('a@example.test',source='a');value=code(sender)
    clock.add(settings.code_ttl)
    with pytest.raises(InvalidCode):await s.verify_email_code(req.challenge_id,value,source='a')
    clock.add(1);req=await s.request_email_code('a@example.test',source='a')
    await s.verify_email_code(req.challenge_id,code(sender),source='a')
    with pytest.raises(InvalidCode):await s.verify_email_code(req.challenge_id,code(sender),source='a')
    assert len(rows(path,'accounts'))==len(rows(path,'auth_sessions'))==1


async def test_resend_invalidates_previous_code_and_has_cooldown(auth_env):
    path,_,clock,sender,s=auth_env
    old=await s.request_email_code('a@example.test',source='a');oldcode=code(sender)
    with pytest.raises(RateLimited):await s.request_email_code('a@example.test',source='a')
    assert len(sender.messages)==1
    clock.add(60);new=await s.request_email_code('a@example.test',source='a')
    with pytest.raises(InvalidCode):await s.verify_email_code(old.challenge_id,oldcode,source='a')
    assert await s.verify_email_code(new.challenge_id,code(sender),source='a')
    assert rows(path,'auth_challenges')[0]['invalidated_at'] is not None


async def test_five_wrong_attempts_lock_challenge(auth_env):
    path,settings,_,sender,s=auth_env
    req=await s.request_email_code('a@example.test',source='a');right=code(sender)
    wrong='0'*settings.code_digits if right!='0'*settings.code_digits else '1'*settings.code_digits
    for _ in range(5):
        with pytest.raises(InvalidCode):await s.verify_email_code(req.challenge_id,wrong,source='a')
    assert rows(path,'auth_challenges')[0]['attempts']==5
    assert rows(path,'auth_challenges')[0]['locked_at'] is not None
    with pytest.raises(InvalidCode):await s.verify_email_code(req.challenge_id,right,source='a')


async def test_new_challenge_cannot_reset_attempt_budget(auth_env):
    path,settings,clock,sender,_=auth_env
    s=AuthService(path,replace(settings,email_attempts=3),sender,clock=clock)
    req=await s.request_email_code('a@example.test',source='a')
    for _ in range(2):
        with pytest.raises(InvalidCode):await s.verify_email_code(req.challenge_id,'wrong',source='a')
    clock.add(60);req=await s.request_email_code('a@example.test',source='b')
    with pytest.raises(InvalidCode):await s.verify_email_code(req.challenge_id,'wrong',source='b')
    restarted=AuthService(path,s.settings,sender,clock=clock)
    with pytest.raises(RateLimited):await restarted.verify_email_code(req.challenge_id,code(sender),source='c')
    assert rows(path,'accounts')==[]


async def test_email_hour_limit_is_rolling_and_persistent(auth_env):
    path,settings,clock,sender,s=auth_env
    for _ in range(5):
        await s.request_email_code('a@example.test',source='a');clock.add(60)
    with pytest.raises(RateLimited):await AuthService(path,settings,sender,clock=clock).request_email_code('a@example.test',source='b')
    clock.add(3600)
    await s.request_email_code('a@example.test',source='a')


@pytest.mark.parametrize('limit',['source','global'])
async def test_source_and_global_delivery_limits(auth_env,limit):
    path,settings,clock,sender,_=auth_env
    settings=replace(settings,**{limit+'_requests':1})
    s=AuthService(path,settings,sender,clock=clock)
    await s.request_email_code('a@example.test',source='a')
    with pytest.raises(RateLimited):await s.request_email_code('b@example.test',source='a' if limit=='source' else 'b')
    assert len(sender.messages)==1


@pytest.mark.parametrize('limit',['source','global'])
async def test_unknown_challenges_still_consume_persistent_attempt_budget(auth_env,limit):
    path,settings,clock,sender,_=auth_env
    s=AuthService(path,replace(settings,**{limit+'_attempts':1}),sender,clock=clock)
    with pytest.raises(InvalidCode):await s.verify_email_code(str(uuid4()),'123456',source='a')
    with pytest.raises(RateLimited):await s.verify_email_code(str(uuid4()),'123456',source='a' if limit=='source' else 'b')


async def test_concurrent_confirmation_and_requests_use_one_account(auth_env):
    path,settings,clock,sender,_=auth_env
    services=[AuthService(path,settings,sender,clock=clock) for _ in range(8)]
    results=await asyncio.gather(*(s.request_email_code('a@example.test',source='a') for s in services),return_exceptions=True)
    assert sum(isinstance(r,RateLimited) for r in results)==7 and len(sender.messages)==1
    req=next(r for r in results if not isinstance(r,Exception))
    results=await asyncio.gather(*(s.verify_email_code(req.challenge_id,code(sender),source='a') for s in services),return_exceptions=True)
    assert sum(isinstance(r,InvalidCode) for r in results)==7
    assert len(rows(path,'accounts'))==len(rows(path,'auth_sessions'))==1


@pytest.mark.parametrize('failure',['definite','unknown','timeout'])
async def test_delivery_failures_have_explicit_state_no_automatic_resend(auth_env,failure,caplog):
    path,settings,clock,_,_=auth_env
    class Sender(FakeEmailSender):
        async def send(self,message):
            self.messages.append(message)
            if failure=='definite':raise DeliveryFailed(message.body)
            if failure=='unknown':raise RuntimeError(message.body)
            await asyncio.sleep(10)
    sender=Sender();s=AuthService(path,replace(settings,delivery_timeout=.01),sender,clock=clock)
    req=await s.request_email_code('a@example.test',source='a')
    assert len(sender.messages)==1
    assert rows(path,'auth_challenges')[0]['delivery_status']==('failed' if failure=='definite' else 'unknown')
    if failure=='definite':
        with pytest.raises(InvalidCode):await s.verify_email_code(req.challenge_id,code(sender),source='a')
    else:assert await s.verify_email_code(req.challenge_id,code(sender),source='a')
    assert code(sender) not in caplog.text


async def test_email_send_does_not_hold_sqlite_write_transaction(auth_env):
    path,settings,clock,_,_=auth_env
    class Sender(FakeEmailSender):
        async def send(self,message):
            lock=sqlite3.connect(path,timeout=.01)
            try:lock.execute('BEGIN IMMEDIATE');lock.rollback()
            finally:lock.close()
            self.messages.append(message)
    sender=Sender();s=AuthService(path,settings,sender,clock=clock)
    await s.request_email_code('a@example.test',source='a')
    assert rows(path,'auth_challenges')[0]['delivery_status']=='sent'


async def test_cancelled_delivery_becomes_unknown(auth_env):
    path,settings,clock,_,_=auth_env;started=asyncio.Event()
    class Sender(FakeEmailSender):
        async def send(self,message):
            self.messages.append(message);started.set();await asyncio.Event().wait()
    sender=Sender();s=AuthService(path,settings,sender,clock=clock)
    task=asyncio.create_task(s.request_email_code('a@example.test',source='a'))
    await started.wait();task.cancel()
    with pytest.raises(asyncio.CancelledError):await task
    assert rows(path,'auth_challenges')[0]['delivery_status']=='unknown'


async def test_abandoned_pending_delivery_recovers_without_new_email(auth_env):
    path,settings,clock,sender,s=auth_env
    req=await s.request_email_code('a@example.test',source='a')
    with sqlite3.connect(path) as db:db.execute("UPDATE auth_challenges SET delivery_status='pending'")
    with pytest.raises(InvalidCode):await s.verify_email_code(req.challenge_id,code(sender),source='a')
    clock.add(settings.delivery_timeout)
    assert await s.verify_email_code(req.challenge_id,code(sender),source='a')
    assert len(sender.messages)==1 and rows(path,'auth_challenges')[0]['delivery_status']=='unknown'


async def test_secrets_not_stored_plain_or_logged(auth_env,caplog):
    path,settings,_,sender,s=auth_env
    req=await s.request_email_code('a@example.test',source='192.0.2.1');value=code(sender)
    grant=await s.verify_email_code(req.challenge_id,value,source='192.0.2.1')
    stored=rows(path,'auth_challenges')[0]
    assert len(stored['code_mac'])==64 and stored['code_mac']!=value
    session=rows(path,'auth_sessions')[0]
    assert session['token_hash']==hashlib.sha256(grant.token.encode()).hexdigest()
    assert grant.token not in repr(session) and grant.token not in repr(grant)
    assert value not in caplog.text and grant.token not in caplog.text
    assert settings.secret.decode() not in repr(settings)
    assert '192.0.2.1' not in repr(rows(path,'auth_rate_limits'))
    with pytest.raises(InvalidSession):await s.get_current_account(session['token_hash'])


async def test_hmac_bound_to_challenge(auth_env):
    path,_,clock,sender,s=auth_env
    first=await s.request_email_code('a@example.test',source='a');old=rows(path,'auth_challenges')[0]['code_mac']
    clock.add(60);second=await s.request_email_code('a@example.test',source='a')
    with sqlite3.connect(path) as db:db.execute('UPDATE auth_challenges SET code_mac=? WHERE challenge_id=?',(old,second.challenge_id))
    with pytest.raises(InvalidCode):await s.verify_email_code(second.challenge_id,code(sender),source='a')


async def test_session_idle_absolute_and_no_resurrection(auth_env):
    path,settings,clock,sender,_=auth_env
    settings=replace(settings,session_lifetime=100,session_idle=200,session_touch=10)
    service=AuthService(path,settings,sender,clock=clock);env=(path,settings,clock,sender,service)
    grant=await login(env);clock.add(100)
    with pytest.raises(InvalidSession):await service.get_current_account(grant.token)
    assert rows(path,'auth_sessions')[0]['last_seen_at']==1700000000


async def test_idle_expiry_and_touch_throttle(auth_env):
    path,settings,clock,_,s=auth_env
    grant=await login(auth_env);before=path.read_bytes();clock.add(1)
    await s.get_current_account(grant.token)
    assert path.read_bytes()==before
    clock.add(settings.session_touch)
    await s.get_current_account(grant.token)
    assert rows(path,'auth_sessions')[0]['last_seen_at']==clock.now
    clock.add(settings.session_idle)
    with pytest.raises(InvalidSession):await s.get_current_account(grant.token)


async def test_logout_one_all_and_other_accounts(auth_env):
    path,_,clock,_,s=auth_env
    first=await login(auth_env);clock.add(60);second=await login(auth_env)
    other=await login(auth_env,'other@example.test','b')
    await s.logout(first.token);await s.logout(first.token)
    with pytest.raises(InvalidSession):await s.get_current_account(first.token)
    assert await s.get_current_account(second.token)
    await s.logout_all(second.token)
    with pytest.raises(InvalidSession):await s.get_current_account(second.token)
    assert (await s.get_current_account(other.token)).id==other.account.id
    with pytest.raises(InvalidSession):await s.logout_all(first.token)


async def test_concurrent_session_touch_and_logout_never_resurrect(auth_env):
    path,settings,clock,_,s=auth_env;grant=await login(auth_env);clock.add(settings.session_touch)
    await asyncio.gather(s.get_current_account(grant.token),s.logout(grant.token),return_exceptions=True)
    with pytest.raises(InvalidSession):await s.get_current_account(grant.token)
    assert rows(path,'auth_sessions')[0]['revoked_at'] is not None


@pytest.mark.parametrize('fault',['account','session','cancel'])
async def test_verification_rollback_allows_safe_retry(auth_env,monkeypatch,fault):
    path,_,_,sender,s=auth_env;req=await s.request_email_code('a@example.test',source='a');value=code(sender)
    if fault=='account':
        target=s.repository.accounts;name='_get_or_create_verified_email_in_transaction'
    else:target=s.repository;name='_insert_session'
    original=getattr(target,name)
    if fault=='cancel':
        async def fail(*args):await original(*args);raise asyncio.CancelledError()
    else:
        async def fail(*args):await original(*args);raise RuntimeError('synthetic failure')
    monkeypatch.setattr(target,name,fail)
    with pytest.raises(asyncio.CancelledError if fault=='cancel' else RuntimeError):await s.verify_email_code(req.challenge_id,value,source='a')
    assert rows(path,'accounts')==rows(path,'account_identities')==rows(path,'auth_sessions')==[]
    assert rows(path,'auth_challenges')[0]['consumed_at'] is None
    monkeypatch.setattr(target,name,original)
    assert await s.verify_email_code(req.challenge_id,value,source='a')
    # A committed verification whose response was lost must NOT be replayed into another session.
    with pytest.raises(InvalidCode):await s.verify_email_code(req.challenge_id,value,source='a')


async def test_lock_timeout_no_partial_account_or_consumption(auth_env):
    path,settings,clock,sender,s=auth_env;req=await s.request_email_code('a@example.test',source='a')
    locked=sqlite3.connect(path);locked.execute('BEGIN IMMEDIATE')
    try:
        other=AuthService(path,settings,sender,clock=clock,timeout=.01)
        with pytest.raises(AccountStoreBusy):await other.verify_email_code(req.challenge_id,code(sender),source='a')
    finally:locked.rollback();locked.close()
    assert rows(path,'auth_challenges')[0]['consumed_at'] is None
    assert await s.verify_email_code(req.challenge_id,code(sender),source='a')


async def test_cleanup_keeps_current_rate_budgets(auth_env):
    path,settings,clock,_,s=auth_env
    await login(auth_env);clock.add(settings.retention+settings.code_ttl+1)
    await s.request_email_code('b@example.test',source='b')
    assert await s.cleanup_expired()==1
    assert len(rows(path,'auth_challenges'))==1
    assert rows(path,'auth_rate_limits')


@pytest.mark.parametrize('kwargs',[{'secret':b'short'},{'environment':'unknown'},{'code_digits':5},
    {'session_touch':604800},{'delivery_timeout':float('inf')},{'email_requests':0},{'resend_interval':4000}])
def test_invalid_settings(kwargs):
    values={'secret':b'x'*32};values.update(kwargs)
    with pytest.raises(ValueError):AuthSettings(**values)


def test_fake_email_never_production_mode(tmp_path):
    with pytest.raises(ValueError):AuthService(tmp_path/'not-created',AuthSettings(secret=b'x'*32),FakeEmailSender())
    assert list(tmp_path.iterdir())==[]


async def test_v1_checksum_and_upgrade_preserve_v1_and_legacy(tmp_path):
    assert CHECKSUM=='b42298a6271e1efd250cdb735fc1af3cc1da161555db2299b428801fb66c9981'
    path=tmp_path/'v1.sqlite3'
    with sqlite3.connect(path) as db:db.execute('CREATE TABLE users(user_id INTEGER PRIMARY KEY,balance INTEGER)');db.execute('INSERT INTO users VALUES(1,99)')
    await migrate(path,target=1)
    with sqlite3.connect(path) as db:
        before={name:db.execute('SELECT sql FROM sqlite_master WHERE name=?',(name,)).fetchone()[0] for name in DDL}
        ledger=db.execute('SELECT * FROM account_schema_migrations').fetchone()
        journal=db.execute('PRAGMA journal_mode').fetchone()[0]
    assert await migrate(path) and not await migrate(path)
    with sqlite3.connect(path) as db:
        assert db.execute('SELECT * FROM account_schema_migrations WHERE version=1').fetchone()==ledger
        assert db.execute('SELECT * FROM users').fetchall()==[(1,99)]
        assert db.execute('PRAGMA journal_mode').fetchone()[0]==journal
        for name in DDL:assert db.execute('SELECT sql FROM sqlite_master WHERE name=?',(name,)).fetchone()[0]==before[name]


@pytest.mark.parametrize('change',["UPDATE account_schema_migrations SET version=3 WHERE version=2",
    "DELETE FROM account_schema_migrations WHERE version=1",'DROP INDEX auth_session_owner',
    'ALTER TABLE auth_sessions ADD COLUMN incompatible TEXT'])
async def test_migration_unknown_gaps_and_drift(auth_env,change):
    path,*_=auth_env
    with sqlite3.connect(path) as db:db.execute(change)
    before=path.read_bytes()
    with pytest.raises(SchemaCompatibilityError):await migrate(path)
    assert before==path.read_bytes()


async def test_partial_v2_and_concurrent_upgrade(tmp_path):
    path=tmp_path/'v1.sqlite3';path.touch();await migrate(path,target=1)
    result=await asyncio.gather(*(migrate(path) for _ in range(6)))
    assert result.count(True)==1
    second=tmp_path/'partial.sqlite3';second.touch();await migrate(second,target=1)
    with sqlite3.connect(second) as db:db.execute('CREATE TABLE auth_challenges(id TEXT)')
    with pytest.raises(SchemaCompatibilityError):await migrate(second)


async def test_session_tokens_random_and_never_client_selected(auth_env):
    _,_,clock,_,s=auth_env
    a=await login(auth_env);clock.add(60);b=await login(auth_env)
    assert len(a.token)==len(b.token)==43 and a.token!=b.token
    assert (await s.get_current_account(a.token)).id==(await s.get_current_account(b.token)).id
    with pytest.raises(TypeError):await s.verify_email_code('x','123456',source='a',account_id=a.account.id)
    with pytest.raises(TypeError):await s.verify_email_code('x','123456',source='a',token=a.token)


@pytest.mark.parametrize('value',['',None,'x'*500,'123','a'*64])
async def test_invalid_session_identifiers(auth_env,value):
    with pytest.raises(InvalidSession):await auth_env[-1].get_current_account(value)


@pytest.mark.parametrize('value',[None,'','x'*129,'a\n','\ud800'])
async def test_invalid_source_has_no_side_effects(auth_env,value):
    path,*_,s=auth_env
    with pytest.raises(ValueError):await s.request_email_code('a@example.test',source=value)
    assert rows(path,'auth_challenges')==rows(path,'auth_rate_limits')==[]


@pytest.mark.parametrize('version',[0,1])
async def test_v2_failure_rolls_back_ddl_and_keeps_existing_v1(tmp_path,monkeypatch,version):
    import auth.schema as schema
    from accounts.errors import AccountStorageError
    path=tmp_path/'rollback.sqlite3';path.touch()
    if version:await migrate(path,target=1)
    before=path.read_bytes()
    monkeypatch.setitem(schema.DDL,'auth_rate_window','INVALID SQL')
    with pytest.raises(AccountStorageError):await migrate(path)
    assert path.read_bytes()==before


async def test_duplicate_source_and_email_limits_do_not_depend_on_service_memory(auth_env):
    path,settings,clock,sender,s=auth_env
    await s.request_email_code('a@example.test',source='shared')
    other=AuthService(path,settings,FakeEmailSender(),clock=clock)
    with pytest.raises(RateLimited):await other.request_email_code('a@EXAMPLE.TEST',source='other')
    assert len(rows(path,'auth_challenges'))==1


def test_import_auth_has_no_configuration_database_network_or_mail_side_effects(tmp_path):
    import os,subprocess,sys
    from pathlib import Path
    script="import sys,socket; socket.socket.connect=lambda *a,**k: (_ for _ in ()).throw(AssertionError('network')); import auth.service,auth.settings,auth.email_sender; assert not {'config','database','maxapi','dotenv'} & sys.modules.keys()"
    env={'PATH':os.environ['PATH'],'PYTHONPATH':str(Path(__file__).resolve().parents[1]),'PYTHONDONTWRITEBYTECODE':'1'}
    result=subprocess.run([sys.executable,'-B','-c',script],cwd=tmp_path,env=env,capture_output=True,text=True)
    assert result.returncode==0,result.stderr
    assert list(tmp_path.iterdir())==[]


async def test_session_expiring_while_waiting_for_write_lock_cannot_be_revived(auth_env):
    path,settings,clock,sender,_=auth_env
    settings=replace(settings,session_idle=2,session_touch=1)
    s=AuthService(path,settings,sender,clock=clock)
    grant=await login((path,settings,clock,sender,s));clock.add(1)
    lock=sqlite3.connect(path);lock.execute('BEGIN IMMEDIATE')
    try:
        task=asyncio.create_task(s.get_current_account(grant.token))
        await asyncio.sleep(.02);clock.add(3)
    finally:lock.rollback();lock.close()
    with pytest.raises(InvalidSession):await task
    assert rows(path,'auth_sessions')[0]['last_seen_at']==1700000000


async def test_code_expiring_while_waiting_for_lock_is_not_accepted(auth_env):
    path,settings,clock,sender,s=auth_env
    req=await s.request_email_code('a@example.test',source='a');clock.add(settings.code_ttl-1)
    lock=sqlite3.connect(path);lock.execute('BEGIN IMMEDIATE')
    try:
        task=asyncio.create_task(s.verify_email_code(req.challenge_id,code(sender),source='a'))
        await asyncio.sleep(.02);clock.add(2)
    finally:lock.rollback();lock.close()
    with pytest.raises(InvalidCode):await task
    assert rows(path,'accounts')==[]


async def test_sql_metacharacters_never_change_query_meaning(auth_env):
    path,_,_,sender,s=auth_env
    address="test'OR1=1--@example.test"
    req=await s.request_email_code(address,source="client' OR 1=1 --")
    with pytest.raises(InvalidCode):await s.verify_email_code("' OR 1=1 --",'123456',source='a')
    assert rows(path,'accounts')==[]
    grant=await s.verify_email_code(req.challenge_id,code(sender),source='a')
    assert rows(path,'account_identities')[0]['provider_subject']==address
    assert rows(path,'users')==[{'user_id':123,'balance':9}]
    assert (await s.get_current_account(grant.token)).id==grant.account.id

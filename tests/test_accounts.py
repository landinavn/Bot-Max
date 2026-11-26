"""Stage 1.1: synthetic SQLite only; no transport, real credentials or provider requests."""
import asyncio
from datetime import datetime, timedelta, timezone
import hashlib
import json
import os
from pathlib import Path
import sqlite3
import subprocess
import sys
from uuid import UUID, uuid4

import pytest

from accounts import AccountRepository, VerifiedIdentity
from accounts.errors import (AccountNotFound, AccountStorageError, AccountStoreBusy, IdentityConflict,
                             InvalidIdentity, LegacyMaxUserNotFound, LegacySchemaError, SchemaCompatibilityError)
from accounts.models import normalize_email
from accounts.migrations import migrate, inspect_schema


def verification(provider='email', subject='Alice@example.test'):
    return VerifiedIdentity(provider, subject, datetime.now(timezone.utc))


def snapshot(path):
    with sqlite3.connect('file:'+str(path)+'?mode=ro', uri=True) as db:
        names=[r[0] for r in db.execute("SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%' ORDER BY name")]
        return {name:(db.execute('SELECT sql FROM sqlite_master WHERE name=?',(name,)).fetchone()[0],
                      db.execute('SELECT * FROM "'+name.replace('"','""')+'" ORDER BY rowid').fetchall()) for name in names}


@pytest.fixture
async def accounts_db(tmp_path):
    path=tmp_path/'synthetic.sqlite3'
    with sqlite3.connect(path) as db:
        db.executescript('''CREATE TABLE users(user_id INTEGER PRIMARY KEY,balance INTEGER NOT NULL,free_trial_used INTEGER NOT NULL);
            CREATE TABLE payment_orders(order_id TEXT PRIMARY KEY,user_id INTEGER NOT NULL,amount_kopecks INTEGER,status TEXT);
            CREATE TABLE photo_orders(order_id TEXT PRIMARY KEY,user_id INTEGER NOT NULL,status TEXT);
            INSERT INTO users VALUES(123,7,1);
            INSERT INTO payment_orders VALUES('synthetic-paid',123,9900,'paid');
            INSERT INTO photo_orders VALUES('synthetic-photo',123,'paid');''')
    await migrate(path)
    return path


def counts(path):
    with sqlite3.connect(path) as db:
        return tuple(db.execute('SELECT COUNT(*) FROM '+t).fetchone()[0] for t in ('accounts','account_identities'))


async def test_independent_account_no_max_or_legacy_user(accounts_db,monkeypatch):
    monkeypatch.delenv('MAX_BOT_TOKEN',raising=False)
    before=snapshot(accounts_db)
    repo=AccountRepository(accounts_db)
    result=await repo.create_account()
    assert UUID(result.id).version==4 and result.id!='123'
    assert result.created_at and result.updated_at
    assert await repo.get_account(result.id)==result
    assert await repo.get_account(str(uuid4())) is None
    assert await repo.list_account_identities(result.id)==[]
    after=snapshot(accounts_db)
    for name in ('users','payment_orders','photo_orders'):assert before[name]==after[name]


async def test_verified_email_needs_matching_trusted_assertion(accounts_db):
    repo=AccountRepository(accounts_db)
    for proof in (None,{},'verified',verification(subject='Other@example.test'),verification('max','123')):
        with pytest.raises(InvalidIdentity):
            await repo.create_account_with_verified_email('Alice@example.test',verification=proof)
    assert counts(accounts_db)==(0,0)
    a=await repo.create_account_with_verified_email(' Alice@EXAMPLE.TEST ',verification=verification())
    assert await repo.get_account_by_identity('email','Alice@EXAMPLE.test')==a
    assert await repo.get_account_by_identity('email','alice@example.test') is None
    identities=await repo.list_account_identities(a.id)
    assert len(identities)==1 and identities[0].verified_at.endswith('Z')
    assert identities[0].provider_subject=='Alice@example.test'
    assert counts(accounts_db)==(1,1)
    assert len(snapshot(accounts_db)['users'][1])==1


async def test_missing_assertion_is_not_an_email_verifier(accounts_db):
    with pytest.raises(TypeError):await AccountRepository(accounts_db).create_account_with_verified_email('Alice@example.test')
    assert counts(accounts_db)==(0,0)


@pytest.mark.parametrize(('original','expected'),[
    (' Alice+news@EXAMPLE.TEST ','Alice+news@example.test'),
    ('a.b@example.test','a.b@example.test'), ('ab@example.test','ab@example.test'),
    ('alice+one@example.test','alice+one@example.test'), ('alice+two@example.test','alice+two@example.test'),
    ('Alice@example.test','Alice@example.test'), ('alice@example.test','alice@example.test'),
    ('a@xn--e1afmkfd.test','a@xn--e1afmkfd.test'),
])
def test_conservative_email_normalization(original,expected):
    assert normalize_email(original)==expected


@pytest.mark.parametrize('value',[None,True,123,'','a','a@@b.test','Name <a@b.test>','a..b@b.test',
    '.a@b.test','a.@b.test','a@-b.test','a@b-.test','a@b..test','a@b','a@b.test\n',
    'a b@b.test','a@b_test.test','мама@example.test','a@пример.рф','a'*65+'@example.test'])
def test_invalid_emails_rejected(value):
    with pytest.raises(InvalidIdentity):normalize_email(value)


async def test_max_mapping_idempotent_and_preserves_legacy(accounts_db):
    before=snapshot(accounts_db);repo=AccountRepository(accounts_db)
    a=await repo.get_or_create_max_account(123)
    assert await repo.get_or_create_max_account(123)==a
    assert await repo.get_account_by_identity('max','123')==a
    assert counts(accounts_db)==(1,1)
    after=snapshot(accounts_db)
    for table in before:
        if table not in ('accounts','account_identities'):assert before[table]==after[table]


async def test_concurrent_independent_max_connections_return_one_account(accounts_db):
    results=await asyncio.gather(*(AccountRepository(accounts_db).get_or_create_max_account(123) for _ in range(24)))
    assert len({r.id for r in results})==1
    assert counts(accounts_db)==(1,1)


async def test_concurrent_email_creation_conflicts_without_orphan_accounts(accounts_db):
    results=await asyncio.gather(*(AccountRepository(accounts_db).create_account_with_verified_email(
        'Alice@example.test',verification=verification()) for _ in range(12)),return_exceptions=True)
    assert sum(not isinstance(x,Exception) for x in results)==1
    assert sum(isinstance(x,IdentityConflict) for x in results)==11
    assert counts(accounts_db)==(1,1)


async def test_missing_legacy_max_user_never_created(accounts_db):
    with pytest.raises(LegacyMaxUserNotFound):await AccountRepository(accounts_db).get_or_create_max_account(456)
    assert counts(accounts_db)==(0,0)
    assert len(snapshot(accounts_db)['users'][1])==1


async def test_web_store_without_legacy_users_is_supported(tmp_path):
    path=tmp_path/'web-only.sqlite3';path.touch();await migrate(path)
    repo=AccountRepository(path)
    assert await repo.create_account()
    with pytest.raises(LegacySchemaError):await repo.get_or_create_max_account(123)
    assert 'users' not in snapshot(path)


async def test_conflicts_never_merge_or_transfer_identities(accounts_db):
    repo=AccountRepository(accounts_db)
    a=await repo.create_account_with_verified_email('Alice@example.test',verification=verification())
    b=await repo.create_account()
    with pytest.raises(IdentityConflict):await repo._add_verified_identity(b.id,verification=verification())
    assert (await repo.get_account_by_identity('email','Alice@example.test')).id==a.id
    assert await repo.list_account_identities(b.id)==[]
    assert counts(accounts_db)==(2,1)
    first=await repo._add_verified_identity(a.id,verification=verification())
    assert await repo._add_verified_identity(a.id,verification=verification())==first
    with pytest.raises(AccountNotFound):await repo._add_verified_identity(str(uuid4()),verification=verification())


async def test_identity_constraints_are_enforced_by_database(accounts_db):
    a=await AccountRepository(accounts_db).get_or_create_max_account(123)
    with sqlite3.connect(accounts_db) as db:
        db.execute('PRAGMA foreign_keys=ON')
        with pytest.raises(sqlite3.IntegrityError):
            db.execute('INSERT INTO account_identities VALUES(?,?,?,?,?,?)',(str(uuid4()),a.id,'max','123','now','now'))
        with pytest.raises(sqlite3.IntegrityError):
            db.execute('INSERT INTO account_identities VALUES(?,?,?,?,?,?)',(str(uuid4()),str(uuid4()),'email','a@example.test','now','now'))
        with pytest.raises(sqlite3.IntegrityError):
            db.execute('INSERT INTO account_identities VALUES(?,?,?,?,?,?)',(str(uuid4()),a.id,'max','456','now','now'))


@pytest.mark.parametrize('value',[None,True,False,0,-1,1.5,'123',2**63])
async def test_invalid_max_ids_rejected_without_writes(accounts_db,value):
    with pytest.raises(InvalidIdentity):await AccountRepository(accounts_db).get_or_create_max_account(value)
    assert counts(accounts_db)==(0,0)


@pytest.mark.parametrize(('provider','subject'),[('MAX','123'),('max','00123'),('max','0'),('max','1 OR 1=1'),
    ('email','bad'),('', 'x'),('future', ''),('future',' x'),('future','x\n'),('future', 'x'*256)])
async def test_invalid_identity_lookup(accounts_db,provider,subject):
    with pytest.raises(InvalidIdentity):await AccountRepository(accounts_db).get_account_by_identity(provider,subject)


async def test_additional_provider_and_distinct_email_aliases(accounts_db):
    repo=AccountRepository(accounts_db);a=await repo.create_account()
    for provider,subject in [('telegram','synthetic-subject'),('email','a.b@example.test'),('email','ab@example.test'),('email','a+one@example.test')]:
        await repo._add_verified_identity(a.id,verification=verification(provider,subject))
    assert len(await repo.list_account_identities(a.id))==4


@pytest.mark.parametrize('value',[None,123,'','123','not-a-uuid',str(uuid4()).upper()])
async def test_invalid_internal_ids(accounts_db,value):
    with pytest.raises(InvalidIdentity):await AccountRepository(accounts_db).get_account(value)


async def test_identity_insert_failure_rolls_back_account(accounts_db):
    with sqlite3.connect(accounts_db) as db:
        db.execute("CREATE TRIGGER reject_identity BEFORE INSERT ON account_identities BEGIN SELECT RAISE(ABORT,'synthetic failure'); END")
    with pytest.raises(AccountStorageError):await AccountRepository(accounts_db).create_account_with_verified_email(
        'Alice@example.test',verification=verification())
    assert counts(accounts_db)==(0,0)


async def test_cancellation_rolls_back_new_account(accounts_db,monkeypatch):
    from unittest.mock import AsyncMock
    repo=AccountRepository(accounts_db)
    monkeypatch.setattr(repo,'_insert_identity',AsyncMock(side_effect=asyncio.CancelledError()))
    with pytest.raises(asyncio.CancelledError):await repo.get_or_create_max_account(123)
    assert counts(accounts_db)==(0,0)


async def test_lock_timeout_is_controlled_and_never_leaves_account(accounts_db):
    lock=sqlite3.connect(accounts_db);lock.execute('BEGIN IMMEDIATE')
    try:
        with pytest.raises(AccountStoreBusy):await AccountRepository(accounts_db,timeout=.02).get_or_create_max_account(123)
    finally:lock.rollback();lock.close()
    assert counts(accounts_db)==(0,0)
    assert await AccountRepository(accounts_db).get_or_create_max_account(123)


async def test_migration_idempotent_and_keeps_old_schema_rows_and_journal(tmp_path):
    path=tmp_path/'legacy.sqlite3'
    with sqlite3.connect(path) as db:
        db.executescript("CREATE TABLE users(user_id INTEGER PRIMARY KEY,balance INTEGER); INSERT INTO users VALUES(1,9);")
        journal=db.execute('PRAGMA journal_mode').fetchone()[0]
    before=snapshot(path)
    assert await inspect_schema(path)=='pending'
    assert snapshot(path)==before
    assert await migrate(path) is True
    assert await migrate(path) is False
    assert await inspect_schema(path)=='applied'
    assert snapshot(path)['users']==before['users']
    with sqlite3.connect(path) as db:assert db.execute('PRAGMA journal_mode').fetchone()[0]==journal


@pytest.mark.parametrize('sql',[
    'CREATE TABLE accounts(id INTEGER PRIMARY KEY)',
    'CREATE TABLE account_identities(id TEXT PRIMARY KEY)',
    'CREATE VIEW accounts AS SELECT 1 AS id',
    'CREATE TABLE account_schema_migrations(version INTEGER PRIMARY KEY)',
])
async def test_incompatible_partial_schema_fails_without_writes(tmp_path,sql):
    path=tmp_path/'partial.sqlite3'
    with sqlite3.connect(path) as db:db.execute(sql)
    before=path.read_bytes()
    with pytest.raises(SchemaCompatibilityError):await migrate(path)
    assert path.read_bytes()==before


async def test_migration_failure_rolls_back_all_ddl(tmp_path,monkeypatch):
    from unittest.mock import AsyncMock
    path=tmp_path/'failure.sqlite3';path.touch()
    monkeypatch.setattr('accounts.migrations._validate',AsyncMock(side_effect=SchemaCompatibilityError('synthetic')))
    with pytest.raises(SchemaCompatibilityError):await migrate(path)
    assert snapshot(path)=={}


@pytest.mark.parametrize('change',[
    "UPDATE account_schema_migrations SET checksum='modified'",
    "DELETE FROM account_schema_migrations",
    "ALTER TABLE accounts ADD COLUMN incompatible TEXT",
    "DROP INDEX account_identity_subject",
    "CREATE TRIGGER unsupported AFTER INSERT ON accounts BEGIN SELECT 1; END",
])
async def test_managed_schema_drift_is_detected(accounts_db,change):
    with sqlite3.connect(accounts_db) as db:db.execute(change)
    before=accounts_db.read_bytes()
    with pytest.raises(SchemaCompatibilityError):await migrate(accounts_db)
    assert accounts_db.read_bytes()==before


async def test_real_legacy_initialization_is_unchanged(credit_db):
    path=credit_db.DATABASE_PATH
    with sqlite3.connect(path) as db:
        db.execute('INSERT INTO users(user_id,balance,free_trial_used) VALUES(123,11,1)')
        db.execute("INSERT INTO payment_orders(order_id,user_id,credits,amount_kopecks,product_name,status,provider_payment_id) VALUES('synthetic-paid',123,1,9900,'synthetic','paid','synthetic-provider')")
        db.execute("INSERT INTO photo_orders(order_id,user_id,tariff,amount_kopecks,product_name,status) VALUES('synthetic-photo',123,590,49000,'synthetic','paid')")
    before=snapshot(path)
    await migrate(path);await AccountRepository(path).get_or_create_max_account(123)
    after=snapshot(path)
    for name in before:assert after[name]==before[name]


def test_import_has_no_db_config_or_max_side_effects(tmp_path):
    path=tmp_path/'untouched.sqlite3';path.touch();before=hashlib.sha256(path.read_bytes()).hexdigest()
    env={'PATH':os.environ['PATH'],'PYTHONPATH':str(Path(__file__).resolve().parents[1]),'PYTHONDONTWRITEBYTECODE':'1'}
    result=subprocess.run([sys.executable,'-B','-c',
        "import sys,accounts,accounts.migrations; assert not {'database','config','maxapi'} & sys.modules.keys()"],
        cwd=tmp_path,env=env,capture_output=True,text=True)
    assert result.returncode==0,result.stderr
    assert hashlib.sha256(path.read_bytes()).hexdigest()==before
    assert list(tmp_path.iterdir())==[path]


async def test_missing_database_file_is_never_created(tmp_path):
    path=tmp_path/'missing.sqlite3'
    with pytest.raises(AccountStorageError):await migrate(path)
    with pytest.raises(AccountStorageError):await AccountRepository(path).create_account()
    assert not path.exists()


@pytest.mark.parametrize('stamp',[datetime(2020,1,1),datetime.now(timezone.utc)+timedelta(days=1),None])
def test_unusable_verification_timestamp_rejected(stamp):
    with pytest.raises(InvalidIdentity):VerifiedIdentity('email','a@example.test',stamp)


async def test_concurrent_migrations_apply_exactly_once(tmp_path):
    path=tmp_path/'concurrent.sqlite3';path.touch()
    results=await asyncio.gather(*(migrate(path) for _ in range(8)))
    assert results.count(True)==1 and results.count(False)==7
    assert await inspect_schema(path)=='applied'


async def test_case_insensitive_partial_schema_is_not_silently_adopted(tmp_path):
    path=tmp_path/'partial.sqlite3'
    with sqlite3.connect(path) as db:db.execute('CREATE TABLE Accounts(id INTEGER PRIMARY KEY)')
    with pytest.raises(SchemaCompatibilityError):await migrate(path)
    assert list(snapshot(path))==['Accounts']


async def test_legacy_indexes_and_triggers_preserved(accounts_db):
    with sqlite3.connect(accounts_db) as db:
        db.execute('CREATE INDEX legacy_status ON payment_orders(status)')
        db.execute('CREATE TRIGGER legacy_noop AFTER INSERT ON users BEGIN SELECT 1; END')
        before=db.execute("SELECT type,name,tbl_name,sql FROM sqlite_master WHERE tbl_name IN ('users','payment_orders','photo_orders') ORDER BY name").fetchall()
    await migrate(accounts_db);await AccountRepository(accounts_db).get_or_create_max_account(123)
    with sqlite3.connect(accounts_db) as db:
        assert db.execute("SELECT type,name,tbl_name,sql FROM sqlite_master WHERE tbl_name IN ('users','payment_orders','photo_orders') ORDER BY name").fetchall()==before


async def test_internal_attach_checks_legacy_and_rejects_second_max(accounts_db):
    repo=AccountRepository(accounts_db);a=await repo.create_account()
    with pytest.raises(LegacyMaxUserNotFound):await repo._add_verified_identity(a.id,verification=verification('max','456'))
    assert await repo.list_account_identities(a.id)==[]
    await repo._add_verified_identity(a.id,verification=verification('max','123'))
    with sqlite3.connect(accounts_db) as db:db.execute('INSERT INTO users VALUES(456,2,1)')
    with pytest.raises(IdentityConflict):await repo._add_verified_identity(a.id,verification=verification('max','456'))
    assert await repo.get_account_by_identity('max','456') is None


async def test_invalid_legacy_primary_key_is_rejected(tmp_path):
    path=tmp_path/'invalid-legacy.sqlite3'
    with sqlite3.connect(path) as db:db.execute('CREATE TABLE users(user_id INTEGER,other INTEGER,PRIMARY KEY(user_id,other))')
    await migrate(path)
    with pytest.raises(LegacySchemaError):await AccountRepository(path).get_or_create_max_account(123)
    assert counts(path)==(0,0)


def test_cli_is_readonly_until_explicit_apply(tmp_path,monkeypatch,capsys):
    import accounts.migrate as cli
    path=tmp_path/'cli.sqlite3';path.touch();before=path.read_bytes()
    monkeypatch.setattr(sys,'argv',['accounts.migrate','--database',str(path)])
    cli.main()
    assert 'pending' in capsys.readouterr().out and path.read_bytes()==before
    monkeypatch.setattr(sys,'argv',['accounts.migrate','--database',str(path),'--apply'])
    cli.main()
    assert 'применена' in capsys.readouterr().out
    applied=path.read_bytes();cli.main()
    assert 'изменений нет' in capsys.readouterr().out and path.read_bytes()==applied


def test_cli_never_creates_missing_database(tmp_path,monkeypatch):
    import accounts.migrate as cli
    path=tmp_path/'missing.sqlite3'
    monkeypatch.setattr(sys,'argv',['accounts.migrate','--database',str(path),'--apply'])
    with pytest.raises(SystemExit) as exc:cli.main()
    assert exc.value.code==1 and not path.exists()

"""Known technical columns only, fixtures are invented and never copied from users."""
from dataclasses import asdict, replace
import json
import sqlite3
from uuid import uuid4

import pytest
from accounts import AccountRepository
from accounts.migrations import migrate
from entitlements import EntitlementService, GrantSpec, VerifiedGrantEvent
from entitlements.errors import LegacySchemaError, UnverifiedLink
from entitlements.legacy_reader import LegacyReader, PAYMENTS
from entitlements.reconciliation import reconcile


EXTRAS={
    'payment_orders':'credits INTEGER NOT NULL DEFAULT 3',
    'photo_orders':"tariff INTEGER NOT NULL DEFAULT 590,generation_status TEXT DEFAULT 'queued'",
    'audio_orders':"audio_format TEXT DEFAULT 'gift',submit_started INTEGER DEFAULT 0,job_id TEXT,running INTEGER DEFAULT 0,delivered INTEGER DEFAULT 0,failed INTEGER DEFAULT 0,free_access INTEGER DEFAULT 0,regeneration_used INTEGER DEFAULT 0",
    'musical_orders':"tariff TEXT DEFAULT 'direct',delivered INTEGER DEFAULT 0,running INTEGER DEFAULT 0,failed INTEGER DEFAULT 0,input_ready INTEGER DEFAULT 1,prepaid INTEGER DEFAULT 0",
    'cartoon_orders':"tariff TEXT DEFAULT 'mother',theme TEXT DEFAULT 'theme_mother'",
    'animation_orders':"tariff TEXT DEFAULT 'three',quantity INTEGER DEFAULT 3,is_admin INTEGER DEFAULT 0,promo_bonus INTEGER DEFAULT 0",
    'ded_moroz_payments':"claimed_id TEXT,variant TEXT DEFAULT 'single',children_count INTEGER",
}


def execute(p,sql,args=()):
    with sqlite3.connect(p) as d:return d.execute(sql,args).fetchall()


def insert(p,table,**data):
    with sqlite3.connect(p) as d:
        cols=','.join(data);qs=','.join('?' for _ in data)
        d.execute('INSERT INTO '+table+'('+cols+') VALUES('+qs+')',tuple(data.values()))


def payment(p,table,**changes):
    data=dict(order_id='order-'+table,user_id=101,status='paid',amount_kopecks=10000,
              provider_payment_id='receipt-'+table,paid_at='2026-11-25 10:00:00')
    data.update(changes);insert(p,table,**data)
    return data['order_id']


def dump(p):
    with sqlite3.connect(p) as d:return '\n'.join(d.iterdump())


@pytest.fixture
async def legacy_env(tmp_path):
    p=tmp_path/'synthetic-legacy.sqlite3';p.touch()
    with sqlite3.connect(p) as d:
        d.executescript('''CREATE TABLE users(user_id INTEGER PRIMARY KEY,balance INTEGER,free_trial_used INTEGER,promo_image_balance INTEGER,free_animation_count INTEGER);
        INSERT INTO users VALUES(101,7,1,3,2); INSERT INTO users VALUES(202,99,0,0,0);
        CREATE TABLE photo_results(order_id TEXT,slot INTEGER,delivered INTEGER,image_url TEXT,PRIMARY KEY(order_id,slot));
        CREATE TABLE animation_jobs(order_id TEXT,user_id INTEGER,slot INTEGER,status TEXT,task_id TEXT);
        CREATE TABLE ded_moroz_orders(id TEXT PRIMARY KEY,user_id INTEGER,status TEXT,payment_order_id TEXT);
        CREATE TABLE pending_videos(user_id INTEGER,avatar_url TEXT,song_url TEXT);
        CREATE TABLE payment_recoveries(user_id INTEGER,bonus_credits INTEGER);
        INSERT INTO payment_recoveries VALUES(101,1);
        CREATE TABLE payment_incidents(user_id INTEGER,reason_code TEXT);
        CREATE TABLE promo_offers(user_id INTEGER,code TEXT);
        CREATE TABLE active_promos(user_id INTEGER,code TEXT);
        CREATE TABLE promo_redemptions(user_id INTEGER,code TEXT);''')
        for table,extra in EXTRAS.items():
            d.execute('CREATE TABLE '+table+'''(order_id TEXT PRIMARY KEY,user_id INTEGER,status TEXT,
                amount_kopecks INTEGER,provider_payment_id TEXT,paid_at TEXT,'''+extra+''',
                details TEXT DEFAULT 'PRIVATE_LYRICS',payment_link TEXT DEFAULT 'https://private.test',
                fsm_data TEXT DEFAULT 'PRIVATE_MEDIA')''')
    await migrate(p)
    repo=AccountRepository(p)
    linked=await repo.get_or_create_max_account(101)
    other=await repo.get_or_create_max_account(202)
    web=await repo.create_account()
    return p,LegacyReader(p),linked.id,other.id,web.id


def find(snapshot,table):return next(x for x in snapshot.rights if x.table==table)


async def test_wallets_current_state_not_historical_payments_and_no_trial_on_link(legacy_env):
    p,r,a,b,w=legacy_env
    payment(p,'payment_orders',credits=500)
    before=dump(p);snapshot=await r.read(a)
    assert snapshot.trial_granted
    wallet=[x for x in snapshot.rights if x.table=='users']
    assert [x.available for x in wallet]==[7,3,2]
    assert wallet[0].bucket=='mixed' and wallet[1].bucket=='promo'
    assert find(snapshot,'payment_orders').available is None
    assert dump(p)==before
    assert [x.available for x in (await r.read(b)).rights if x.table=='users']==[99,0,0]
    assert not (await r.read(b)).trial_granted


async def test_confirmed_link_only_no_arbitrary_max_id_or_automatic_accounts(legacy_env):
    p,r,a,b,w=legacy_env
    before=dump(p)
    for owner in (w,str(uuid4())):
        with pytest.raises(UnverifiedLink):await r.read(owner)
    assert dump(p)==before
    execute(p,"UPDATE account_identities SET verified_at='not-a-verified-date' WHERE account_id=?",(a,))
    with pytest.raises(UnverifiedLink):await r.read(a)


async def test_deleted_legacy_user_is_not_recreated(legacy_env):
    p,r,a,b,w=legacy_env
    execute(p,'DELETE FROM users WHERE user_id=101');before=dump(p)
    with pytest.raises(UnverifiedLink):await r.read(a)
    assert dump(p)==before


@pytest.mark.parametrize('subject',['00101','-1','not-an-id',str(2**63)])
async def test_corrupt_identity_not_used_as_ownership(legacy_env,subject):
    p,r,a,b,w=legacy_env
    execute(p,'UPDATE account_identities SET provider_subject=? WHERE account_id=?',(subject,a))
    with pytest.raises(UnverifiedLink):await r.read(a)


@pytest.mark.parametrize('table',PAYMENTS)
@pytest.mark.parametrize('state',['pending','cancelled','refunded','unrecognized'])
async def test_unconfirmed_unknown_payments_never_available(legacy_env,table,state):
    p,r,a,b,w=legacy_env
    payment(p,table,status=state)
    right=find(await r.read(a),table)
    assert right.available is None and right.consumed is None
    assert ('unconfirmed_payment' if state=='pending' else 'unknown_status') in right.issues


@pytest.mark.parametrize('table',PAYMENTS)
@pytest.mark.parametrize('change',[{'provider_payment_id':None},{'paid_at':None},{'amount_kopecks':0}])
async def test_paid_without_evidence_is_not_invented_paid_right(legacy_env,table,change):
    p,r,a,b,w=legacy_env
    payment(p,table,**change)
    right=find(await r.read(a),table)
    assert right.kind=='evidence' and right.available is None
    assert 'missing_payment_evidence_or_non_cash_grant' in right.issues


@pytest.mark.parametrize('tariff,size',[(590,3),(990,5),(1490,7)])
async def test_photo_saved_frames_consumed_even_not_delivered_and_price_not_tariff(legacy_env,tariff,size):
    p,r,a,b,w=legacy_env
    oid=payment(p,'photo_orders',tariff=tariff,amount_kopecks=1)
    for slot,delivered in [(1,1),(2,0)]:insert(p,'photo_results',order_id=oid,slot=slot,delivered=delivered,image_url='PRIVATE_MEDIA')
    right=find(await r.read(a),'photo_orders')
    assert right.available==size-2 and right.consumed==2
    assert 'remaining_slots_may_be_inflight_no_durable_reservation' in right.issues


@pytest.mark.parametrize('state',['awaiting_payment','running','manual','new-status'])
async def test_photo_unknown_generation_state_blocks_remaining(legacy_env,state):
    p,r,a,b,w=legacy_env
    payment(p,'photo_orders',generation_status=state)
    right=find(await r.read(a),'photo_orders')
    assert right.available is None and 'unknown_generation_status' in right.issues


async def test_inconsistent_completed_photo_detected(legacy_env):
    p,r,a,b,w=legacy_env
    payment(p,'photo_orders',generation_status='completed')
    report=await reconcile(p,a)
    assert any('completed_with_missing_results' in x for x in report.discrepancies)
    assert not report.execution_allowed


@pytest.mark.parametrize('table',['audio_orders','musical_orders'])
@pytest.mark.parametrize('delivered',[0,1])
async def test_single_order_delivery_does_not_mint_unused_credit(legacy_env,table,delivered):
    p,r,a,b,w=legacy_env
    payment(p,table,delivered=delivered)
    right=find(await r.read(a),table)
    assert right.available==(0 if delivered else None)
    assert right.reserved==(0 if delivered else 1)
    assert right.consumed==(1 if delivered else None)


async def test_audio_unknown_post_and_included_regeneration(legacy_env):
    p,r,a,b,w=legacy_env
    oid=payment(p,'audio_orders',submit_started=1)
    assert 'unknown_provider_outcome' in find(await r.read(a),'audio_orders').issues
    execute(p,'UPDATE audio_orders SET delivered=1 WHERE order_id=?',(oid,))
    right=find(await r.read(a),'audio_orders')
    assert right.consumed==1 and 'included_regeneration_policy_not_transferable' in right.issues


@pytest.mark.parametrize('state,consumed,reserved',[('paid',None,1),('manual',None,1),('completed',1,0)])
async def test_cartoon_states_and_archived_assets_not_new_rights(legacy_env,state,consumed,reserved):
    p,r,a,b,w=legacy_env
    payment(p,'cartoon_orders',status=state)
    right=find(await r.read(a),'cartoon_orders')
    assert right.consumed==consumed and right.reserved==reserved
    assert right.available==(0 if consumed else None)


@pytest.mark.parametrize('state',['queued','creating','processing','ready','delivered','manual','unknown'])
async def test_animation_enqueued_slot_is_held_not_returned_on_error(legacy_env,state):
    p,r,a,b,w=legacy_env
    oid=payment(p,'animation_orders')
    insert(p,'animation_jobs',order_id=oid,user_id=101,slot=1,status=state)
    right=find(await r.read(a),'animation_orders')
    assert right.reserved==(0 if state=='delivered' else 1)
    assert right.consumed==(1 if state=='delivered' else 0)
    assert right.available==(None if state in ('creating','unknown') else 2)
    if state=='creating':assert 'unknown_provider_outcome' in right.issues


async def test_animation_materialized_bonus_and_counter_are_not_two_grants(legacy_env):
    p,r,a,b,w=legacy_env
    payment(p,'animation_orders',amount_kopecks=0,provider_payment_id=None,promo_bonus=1,quantity=1,tariff='promo1111_bonus')
    snapshot=await r.read(a)
    right=find(snapshot,'animation_orders')
    assert right.bucket=='bonus' and right.available==1
    assert [x.available for x in snapshot.rights if x.reference=='animation-bonus-wallet']==[2]
    assert not hasattr(snapshot,'total_available')


@pytest.mark.parametrize('state',['input','audio_queued','audio_publish','audio_ready','editing','video_queued','polling','download','render','done','manual','cancelled','unknown'])
async def test_ded_moroz_claim_is_one_service_even_family(legacy_env,state):
    p,r,a,b,w=legacy_env
    oid=payment(p,'ded_moroz_payments',claimed_id='claim',variant='family',children_count=4)
    insert(p,'ded_moroz_orders',id='claim',user_id=101,status=state,payment_order_id=oid)
    right=find(await r.read(a),'ded_moroz_payments')
    assert right.available==(0 if state=='done' else None)
    assert right.consumed==(1 if state=='done' else None)
    assert right.reserved==(1 if state in ('input','audio_queued','audio_publish','audio_ready','editing','video_queued','polling','download','render') else (0 if state=='done' else None))


async def test_ded_claim_owner_mismatch_refused(legacy_env):
    p,r,a,b,w=legacy_env
    oid=payment(p,'ded_moroz_payments',claimed_id='claim')
    insert(p,'ded_moroz_orders',id='claim',user_id=202,status='done',payment_order_id=oid)
    with pytest.raises(LegacySchemaError):await r.read(a)


async def test_readonly_dry_run_detects_cross_product_and_new_event_duplicates(legacy_env):
    p,r,a,b,w=legacy_env
    payment(p,'payment_orders',provider_payment_id='duplicate')
    payment(p,'photo_orders',provider_payment_id='duplicate')
    svc=EntitlementService(p,clock=lambda:1000)
    await svc.grant(VerifiedGrantEvent(a,'payment.prodamus','duplicate',900,(GrantSpec('image_credit',3,('postcard','vision_board')),)),idempotency_key='grant')
    before=dump(p);raw=p.read_bytes()
    report=await reconcile(p,a,clock=lambda:1000)
    assert len(report.possible_duplicates)==3
    assert report.new_packages[0].available==3
    assert report.legacy.rights[0].available==7  # NEVER 10
    assert report.transfer_plan and all(x.startswith('BLOCKED_MANUAL_REVIEW:') for x in report.transfer_plan)
    assert not report.execution_allowed and dump(p)==before and p.read_bytes()==raw
    value=json.dumps(asdict(report))
    for private in ('PRIVATE_LYRICS','PRIVATE_MEDIA','https://private.test'):
        assert private not in value
    assert any('payment_recoveries:not_an_additional_grant'==x for x in report.legacy.observations)


@pytest.mark.parametrize('table',PAYMENTS)
async def test_missing_payment_table_refuses_incomplete_schema(legacy_env,table):
    p,r,a,b,w=legacy_env
    execute(p,'DROP TABLE '+table)
    with pytest.raises(LegacySchemaError):await r.read(a)


@pytest.mark.parametrize('change',[
    'UPDATE users SET balance=-1 WHERE user_id=101',
    'UPDATE users SET free_trial_used=2 WHERE user_id=101',
    'UPDATE users SET promo_image_balance=1.5 WHERE user_id=101',
    'UPDATE users SET free_animation_count=NULL WHERE user_id=101',
    'ALTER TABLE audio_orders RENAME COLUMN submit_started TO unknown_column',
    'DROP TABLE musical_orders',
])
async def test_unknown_or_inconsistent_legacy_schema_safe_refusal(legacy_env,change):
    p,r,a,b,w=legacy_env
    execute(p,change)
    with pytest.raises(LegacySchemaError):await r.read(a)


async def test_migration_and_read_never_mutate_product_tables(legacy_env):
    p,r,a,b,w=legacy_env
    payment(p,'photo_orders');payment(p,'payment_orders')
    def legacy_dump():
        with sqlite3.connect(p) as d:
            names=['users',*PAYMENTS,'photo_results','animation_jobs','payment_recoveries']
            return {t:(d.execute('SELECT sql FROM sqlite_master WHERE name=?',(t,)).fetchone(),
                       d.execute('SELECT * FROM '+t).fetchall()) for t in names}
    before=legacy_dump()
    await migrate(p);await r.read(a);await reconcile(p,a)
    assert legacy_dump()==before


async def test_actual_legacy_schema_initializers_and_bonus_order(credit_db):
    """Compatibility with real DDL/functions, exclusively the synthetic tmp_path DB."""
    import aiosqlite
    import ded_moroz
    import ded_moroz_sales
    import promo1111
    p=credit_db.DATABASE_PATH
    async with aiosqlite.connect(p) as d:
        await ded_moroz.init_tables(d)
        await ded_moroz_sales.init_tables(d)
        await d.execute('INSERT INTO users(user_id,balance,free_trial_used,free_animation_count) VALUES(909,4,1,1)')
        await d.commit()
    await migrate(p)
    owner=(await AccountRepository(p).get_or_create_max_account(909)).id
    bonus=await promo1111.claim_animation(909)
    before=dump(p)
    snapshot=await LegacyReader(p).read(owner)
    right=find(snapshot,'animation_orders')
    assert right.reference==bonus['order_id'] and right.available==1 and right.bucket=='bonus'
    assert [x.available for x in snapshot.rights if x.reference=='animation-bonus-wallet']==[0]
    assert dump(p)==before


async def test_all_queries_are_readonly_and_exclude_sensitive_column_names(legacy_env,monkeypatch):
    p,r,a,b,w=legacy_env
    import aiosqlite
    original=aiosqlite.Connection.execute
    statements=[]
    def tracked(db,sql,parameters=None):
        statements.append(sql)
        return original(db,sql,parameters or ())
    monkeypatch.setattr(aiosqlite.Connection,'execute',tracked)
    await reconcile(p,a)
    for sql in statements:
        assert sql.strip().split()[0].upper() in ('SELECT','PRAGMA','BEGIN')
        assert 'SELECT *' not in sql.upper()
        if sql.strip().upper().startswith('SELECT'):
            assert all(name not in sql for name in ('details','fsm_data','payment_link','image_url','tracks_json','lyrics','prompt'))


async def test_foreign_orders_with_same_table_are_not_leaked(legacy_env):
    p,r,a,b,w=legacy_env
    for t in PAYMENTS:payment(p,t,user_id=202)
    snapshot=await r.read(a)
    assert all(x.table=='users' for x in snapshot.rights)
    assert 'receipt-' not in json.dumps(asdict(snapshot))


async def test_malformed_paid_timestamp_is_not_confirmed_payment(legacy_env):
    p,r,a,b,w=legacy_env
    payment(p,'photo_orders',paid_at='not-a-date')
    right=find(await r.read(a),'photo_orders')
    assert right.available is None and 'invalid_payment_timestamp' in right.issues

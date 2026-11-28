import hashlib
import sqlite3
from dataclasses import replace

import pytest

from test_private_media import private_env
from private_media import MediaError, MediaAccessDenied
import private_media_migration as migration


@pytest.fixture
def legacy(private_env,tmp_path):
    c,s,a,store=private_env
    db=tmp_path/'business.sqlite3'
    url='https://storage.yandexcloud.net/synthetic-legacy/vision-boards/synthetic.png'
    with sqlite3.connect(db) as d:
        d.executescript('''CREATE TABLE photo_orders(order_id TEXT PRIMARY KEY,user_id INTEGER,status TEXT,generation_status TEXT);
        CREATE TABLE photo_results(order_id TEXT,slot INTEGER,image_url TEXT,delivered INTEGER);
        CREATE TABLE shared_images(token TEXT,image_url TEXT);
        CREATE TABLE users(user_id INTEGER,balance INTEGER);
        INSERT INTO photo_orders VALUES('order-1',1,'paid','completed');
        INSERT INTO users VALUES(1,7);''')
        d.execute('INSERT INTO photo_results VALUES(?,?,?,?)',('order-1',1,url,1))
        d.execute('INSERT INTO shared_images VALUES(?,?)',('synthetic-share',url))
    s.objects[c.storage_bucket,'vision-boards/synthetic.png']=(b'synthetic image','image/png',{})
    return c,s,a,store,db,url


def test_inventory_is_readonly_no_bucket_calls_no_raw_urls_in_plan(legacy):
    c,s,a,store,db,url=legacy
    before=db.read_bytes();items=migration.inventory(db,c)
    assert db.read_bytes()==before and s.heads==0 and s.gets==0 and s.puts==0
    assert len(items)==2
    assert items[0].proof=='generated-output'
    assert items[1].proof=='ownership-unverified'
    assert all('https:' not in repr(i) for i in items)
    assert not c.private_media_registry.exists()


def test_copy_is_idempotent_private_only_and_leaves_all_originals(legacy):
    c,s,a,store,db,url=legacy
    item=migration.inventory(db,c)[0];before=db.read_bytes()
    ref=migration.copy_one(db,c,item,s,store)
    assert migration.copy_one(db,c,item,s,store)==ref and s.puts==1
    assert db.read_bytes()==before
    assert (c.storage_bucket,item.object_key) in s.objects
    assert store.read(ref,a)[0]==b'synthetic image'
    assert len(migration.inventory(db,c))==2  # Source still public until a separate cutover/ACL decision.


def test_ambiguous_owner_and_changed_source_cannot_be_copied(legacy):
    c,s,a,store,db,url=legacy
    items=migration.inventory(db,c)
    with pytest.raises(MediaAccessDenied):migration.copy_one(db,c,items[1],s,store)
    with pytest.raises(MediaError):migration.copy_one(db,c,replace(items[0],owner_id='2'),s,store)
    with sqlite3.connect(db) as d:d.execute('UPDATE photo_results SET image_url=?',(url+'?signed=synthetic',))
    with pytest.raises(MediaError):migration.copy_one(db,c,items[0],s,store)
    assert s.puts==0 and s.gets==0


def test_cutover_requires_stopped_service_reviewed_providers_and_drained_memory(legacy):
    c,s,a,store,db,url=legacy
    item=migration.inventory(db,c)[0];ref=migration.copy_one(db,c,item,s,store)
    with pytest.raises(MediaError):migration.cutover_completed_photo(db,c,item,ref,store)
    with sqlite3.connect(db) as d:
        d.executescript("CREATE TABLE cartoon_jobs(id TEXT,status TEXT,provider_id TEXT); INSERT INTO cartoon_jobs VALUES('synthetic-job','polling','synthetic-provider-id');")
    with pytest.raises(MediaError):migration.cutover_completed_photo(db,c,item,ref,store,service_stopped=True,providers_checked=True,memory_drained=True)
    with sqlite3.connect(db) as d:assert d.execute('SELECT image_url FROM photo_results').fetchone()[0]==url


def test_reviewed_cutover_is_narrow_idempotent_and_preserves_finances(legacy):
    c,s,a,store,db,url=legacy
    item=migration.inventory(db,c)[0];ref=migration.copy_one(db,c,item,s,store)
    ack=dict(service_stopped=True,providers_checked=True,memory_drained=True)
    assert migration.cutover_completed_photo(db,c,item,ref,store,**ack)=='private-reference-saved'
    assert migration.cutover_completed_photo(db,c,item,ref,store,**ack)=='already-private'
    with sqlite3.connect(db) as d:
        assert d.execute('SELECT image_url FROM photo_results').fetchone()[0]==ref
        assert d.execute('SELECT balance FROM users').fetchone()[0]==7
        assert d.execute('SELECT status,generation_status FROM photo_orders').fetchone()==('paid','completed')
        assert d.execute('SELECT image_url FROM shared_images').fetchone()[0]==url
    assert (c.storage_bucket,item.object_key) in s.objects  # No ACL/delete API exists here.


@pytest.mark.parametrize('suffix',['../other','one.png?signature=synthetic','one.png#fragment','%2e%2e/other','%2Fother','one%00.png'])
def test_unsafe_or_signed_legacy_location_is_never_guessed(private_env,suffix):
    c,s,a,store=private_env
    assert migration.legacy_key(c,'https://storage.yandexcloud.net/synthetic-legacy/'+suffix) is None


def test_copy_does_not_change_legacy_order_policy_or_private_intent(legacy):
    from media_policy import Policy,PolicyStore
    c,s,a,store,db,url=legacy
    PolicyStore(c).bind(Policy('photosession','legacy','max','1','order-1'))
    item=migration.inventory(db,c)[0]
    reference=migration.copy_one(db,c,item,s,store)
    assert not store.private_order(a.owner,'order-1')
    assert PolicyStore(c).load(a.owner,'order-1').mode=='legacy'
    with pytest.raises(MediaError):migration.cutover_completed_photo(db,c,item,reference,store,service_stopped=True,providers_checked=True,memory_drained=True)
    with sqlite3.connect(db) as d:assert d.execute('SELECT image_url FROM photo_results').fetchone()[0]==url

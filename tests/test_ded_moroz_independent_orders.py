"""Independent greetings; exact order routing and idempotent explicit creation."""
import asyncio
import json
from uuid import uuid4
from unittest.mock import AsyncMock

import aiosqlite
import pytest

import database
import ded_moroz as dm
import ded_moroz_family as family
import ded_moroz_sales as sales
from handlers import create_router,PosterStates
from test_handlers import callback_event,message_event,ready_context,dispatch_callback,call_handler,event_replies
from test_ded_moroz_sales import campaign_clock


async def family_payload(router,context):
    event=callback_event('cat_ded_moroz_family')
    await dispatch_callback(router,event,context)
    return event.send.await_args.kwargs['attachments'][0].payload.buttons[0][0].payload


@pytest.mark.parametrize('status',['input','audio_ready','manual','polling','deliver','done'])
async def test_any_old_single_status_does_not_block_family_or_get_rewritten(credit_db,status):
    single=await dm.begin(456)
    await dm.update(single['id'],status=status,details=json.dumps({'name':'Марк'}),output_url='https://storage.example/received.mp4')
    before=await dm.get(single['id'])
    router=create_router(admin_user_id=456);context=await ready_context()
    payload=await family_payload(router,context)
    await dispatch_callback(router,callback_event(payload),context)
    key=(await context.get_data())['dm_order']
    assert key!=single['id'] and family.is_family((await dm.get(key))['details'])
    assert await context.get_state()==PosterStates.ded_moroz_input
    assert await dm.get(single['id'])==before
    assert (await dm.get(key))['status']=='input' and not (await dm.get(key))['audio_path']


async def test_two_families_and_single_can_be_opened_by_their_own_keys(credit_db):
    one=await dm.begin(456)
    await dm.update(one['id'],details=json.dumps({'name':'Саша'}))
    router=create_router(admin_user_id=456);context=await ready_context()
    keys=[]
    for _ in range(2):
        payload=await family_payload(router,context)
        await dispatch_callback(router,callback_event(payload),context)
        keys.append((await context.get_data())['dm_order'])
    assert len(set(keys))==2
    for key in (one['id'],*keys):
        await dispatch_callback(router,callback_event('dm_open:'+key),context)
        assert (await context.get_data())['dm_order']==key
    assert len(await dm.read('SELECT * FROM ded_moroz_orders',all_rows=True))==3


async def test_same_creation_button_is_idempotent_even_under_concurrent_clicks(credit_db):
    token='family:'+uuid4().hex
    rows=await asyncio.gather(*(dm.begin(456,details={'variant':'family','children_count':2},new=True,creation_key=token) for _ in range(5)))
    assert len({r['id'] for r in rows})==1
    assert (await database.get_all_counters())['dm_family_started']==1
    await dm.update(rows[0]['id'],status='done')
    repeated=await dm.begin(456,details={'variant':'family','children_count':3},new=True,creation_key=token)
    assert repeated['id']==rows[0]['id'] and repeated['status']=='done'
    assert json.loads(repeated['details'])['children_count']==2


async def test_explicit_single_new_is_independent_and_its_button_cannot_duplicate_order(credit_db):
    old=await dm.begin(456)
    router=create_router(admin_user_id=456);context=await ready_context()
    payload='dm_single_new:'+uuid4().hex
    for _ in range(2):await dispatch_callback(router,callback_event(payload),context)
    key=(await context.get_data())['dm_order']
    assert key!=old['id'] and not family.is_family((await dm.get(key))['details'])
    assert len(await dm.read('SELECT * FROM ded_moroz_orders',all_rows=True))==2


async def test_customer_cannot_create_free_single_or_open_other_persons_order(credit_db):
    row=await dm.begin(789)
    router=create_router(admin_user_id=999);context=await ready_context()
    await dispatch_callback(router,callback_event('dm_single_new:'+uuid4().hex),context)
    await dispatch_callback(router,callback_event('dm_open:'+row['id']),context)
    assert not (await context.get_data()).get('dm_order')
    assert len(await dm.read('SELECT * FROM ded_moroz_orders',all_rows=True))==1


async def test_payment_return_selects_exact_invoice_despite_other_open_orders(credit_db):
    invoices=[]
    for i in range(2):
        row=await sales.checkout(456);await sales.mark_paid(row['order_id'],'parallel-'+str(i));invoices.append(await sales.payment(row['order_id']))
    router=create_router(admin_user_id=999);context=await ready_context()
    for pay in invoices:
        event=message_event('/start dm_paid_'+pay['order_id'])
        await call_handler(router,'start',event,context)
        assert (await context.get_data())['dm_order']==pay['claimed_id']
    assert invoices[0]['claimed_id']!=invoices[1]['claimed_id']
    assert len(await dm.read('SELECT * FROM ded_moroz_orders',all_rows=True))==2
    assert await sales.claim(789,999,payment_key=invoices[0]['order_id']) is None


async def test_legacy_unique_index_is_removed_without_resetting_existing_rows(credit_db):
    row=await dm.begin(456);await dm.update(row['id'],status='manual',error='audio_submitting:old_error')
    before=await dm.get(row['id'])
    async with aiosqlite.connect(database.DATABASE_PATH) as db:
        await db.execute("CREATE UNIQUE INDEX dm_active_user ON ded_moroz_orders(user_id) WHERE status NOT IN ('done','cancelled')")
        await db.commit()
    await database.init_db()
    new=await dm.begin(456,new=True,creation_key='single:'+uuid4().hex)
    assert new['id']!=row['id'] and await dm.get(row['id'])==before


async def test_completed_order_button_sends_exact_saved_result_without_generating(credit_db,monkeypatch):
    row=await dm.begin(456);await dm.update(row['id'],status='done')
    import ded_moroz_customer
    send=AsyncMock();monkeypatch.setattr(ded_moroz_customer,'last_result',send)
    monkeypatch.setattr(dm.Config,'from_env',lambda:object())
    await dispatch_callback(create_router(admin_user_id=456),callback_event('dm_open:'+row['id']),await ready_context())
    assert send.await_args.kwargs['order_id']==row['id']
    assert (await dm.get(row['id']))['status']=='done'

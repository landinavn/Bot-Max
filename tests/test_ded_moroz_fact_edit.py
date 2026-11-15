"""Personal fields remain editable without exposing or losing the tagged template."""
import json
import re

import pytest

import ded_moroz as dm
from handlers import create_router, PosterStates
from test_ded_moroz import setup_dm, audio_order, step
from test_handlers import callback_event, message_event, ready_context, dispatch_callback, call_handler, event_replies


async def test_all_fields_edit_as_draft_and_only_confirm_queues_tagged_speech(credit_db,setup_dm):
    _,_,worker=setup_dm;row=await audio_order(worker);key=row['id']
    for _ in range(4):await step(worker,key)
    original=await dm.get(key)
    router=create_router(admin_user_id=456);context=await ready_context()
    await dispatch_callback(router,callback_event(f'dm_audio_edit:{key}:0'),context)
    # The old edit button no longer accepts a replacement script.
    await call_handler(router,'ded_moroz_input',message_event('Произвольное поздравление'),context)
    assert (await dm.get(key))['text']==original['text']
    changes=dict(name='Машенька',fact1='любишь рисовать',fact2='научилась плавать',fact3='заботливая',task='читать перед сном')
    for field,value in changes.items():
        await dispatch_callback(router,callback_event(f'dm_fact_edit:{key}:0:{field}'),context)
        await call_handler(router,'ded_moroz_input',message_event(value),context)
        draft=await dm.get(key)
        assert draft['version']==0 and draft['status']=='editing'
        assert draft['audio_path']==original['audio_path'] and draft['text']==original['text']
    assert worker.tts.generate_speech.await_count==1
    await dispatch_callback(router,callback_event(f'dm_facts_apply:{key}:0'),context)
    saved=await dm.get(key)
    assert saved['text']==dm.TEMPLATE.format(**changes)
    assert json.loads(saved['details'])==changes and saved['version']==1 and saved['status']=='audio_queued'
    assert re.findall(r'\[[^\]]+\]',saved['text'])==re.findall(r'\[[^\]]+\]',dm.TEMPLATE)
    assert worker.assets.path(original['audio_path']).is_file()
    assert worker.tts.generate_speech.await_count==1
    await dispatch_callback(router,callback_event(f'dm_facts_apply:{key}:0'),context)
    assert (await dm.get(key))['version']==1
    assert not await dm.select_fact(key,456,0,'fact1')


async def test_draft_and_selected_field_survive_restart_and_cancel_preserves_audio(credit_db,setup_dm):
    _,_,worker=setup_dm;row=await audio_order(worker);key=row['id']
    for _ in range(4):await step(worker,key)
    original=await dm.get(key)
    assert await dm.request_edit(key,456,0,admin=456)
    assert await dm.select_fact(key,456,0,'fact1')
    assert await dm.revised(key,456,'обожаешь конструкторы',admin=456)
    assert await dm.select_fact(key,456,0,'name')
    await credit_db.init_db()
    context=await ready_context()
    await dm.enter(callback_event('cat_ded_moroz'),context,456)
    assert await context.get_state()==PosterStates.ded_moroz_edit
    saved=await dm.get(key)
    assert saved['edit_field']=='name' and json.loads(saved['edit_details'])['fact1']=='обожаешь конструкторы'
    assert await dm.revised(key,456,'Сашенька',admin=456)
    assert not await dm.cancel_facts(key,999,0)
    assert await dm.cancel_facts(key,456,0)
    saved=await dm.get(key)
    for field in ('text','details','audio_path','audio_url','version','regeneration_attempts','edit_used'):
        assert saved[field]==original[field]
    assert saved['status']=='audio_ready' and saved['edit_details'] is None
    assert saved['preview_sent']==0
    assert worker.tts.generate_speech.await_count==1


@pytest.mark.parametrize('value',['[raspy] Маша','{name}','', 'x'*301])
async def test_fact_input_rejects_tags_and_invalid_values_without_queueing(credit_db,setup_dm,value):
    _,_,worker=setup_dm;row=await audio_order(worker);key=row['id']
    await dm.update(key,status='audio_ready')
    assert await dm.request_edit(key,456,0,admin=456)
    assert not await dm.select_fact(key,999,0,'name')
    assert not await dm.select_fact(key,456,99,'name')
    assert await dm.select_fact(key,456,0,'name')
    with pytest.raises(ValueError):await dm.revised(key,456,value,admin=456)
    saved=await dm.get(key)
    assert saved['version']==0 and saved['status']=='editing' and saved['edit_field']=='name'
    assert not await dm.apply_facts(key,456,0,admin=456)
    worker.tts.generate_speech.assert_not_awaited()

"""Plain-text character editing must never parse input as JSON or replace other fields."""
import json
import logging
from types import SimpleNamespace

import pytest

from multstudio import repository as repo
from multstudio.router import create_router, StudioStates, DESCRIPTION_PROMPT
from test_multstudio import studio
from test_multstudio_project_menu import event
from test_handlers import dispatch_callback, ready_context, message_event, call_handler


@pytest.fixture
def replies(monkeypatch):
    async def reply(ev,text,**kwargs):
        sender=ev.send if hasattr(ev,'send') else ev.message.answer
        return await sender(text,**kwargs)
    monkeypatch.setattr('multstudio.router._reply',reply)


async def character(studio):
    return (await repo.children('character',studio[0]))[0]


async def open_description(studio,context):
    char=await character(studio)
    router=create_router(456,studio[1],studio[2])
    pressed=event('studio:edit:character:'+char['id'])
    await dispatch_callback(router,pressed,context)
    return router,char,pressed


@pytest.mark.parametrize('text',[
    '38 лет, темные волосы, голубые глаза, кремовый свитер.',
    '38 лет, волосы «тёмный каштан», глаза голубые. Без изменения лица.',
    'Возраст: 38 лет\nВолосы: тёмные\nОдежда: свитер и джинсы',
    '38 лет, "темные волосы", естественный вид.',
])
async def test_plain_description_is_saved_and_logged_without_touching_other_fields(studio,replies,caplog,text):
    context=await ready_context()
    router,char,pressed=await open_description(studio,context)
    assert await context.get_state()==StudioStates.input
    assert DESCRIPTION_PROMPT in pressed.bot.send_message.await_args.kwargs['text']
    before=(await repo.get('character',char['id']))['spec']
    submitted=message_event(text)
    with caplog.at_level(logging.INFO,logger='multstudio.router'):
        await call_handler(router,'studio_input',submitted,context)
    updated=await repo.get('character',char['id'])
    assert updated['spec']==dict(before,description=text)
    assert updated['revision']==char['revision']+1
    assert 'Обновлено описание персонажа '+char['id']+': '+text in caplog.text
    assert 'сохранено' in submitted.message.answer.await_args.args[0]
    assert await context.get_state()!=StudioStates.input
    assert await repo.claim() is None


async def test_submit_uses_current_spec_not_form_snapshot(studio,replies):
    context=await ready_context()
    router,char,_=await open_description(studio,context)
    updated_spec=dict(char['spec'],voice='alloy',voice_direction='Новая интонация',reference='new-reference-id',
                      extra_settings={'keep':'all nested fields'})
    await repo.edit('character',char['id'],updated_spec)
    await call_handler(router,'studio_input',message_event('38 лет, темные волосы'),context)
    saved=await repo.get('character',char['id'])
    assert saved['spec']==dict(updated_spec,description='38 лет, темные волосы')


async def test_legacy_open_json_edit_state_now_accepts_plain_description(studio,replies):
    char=await character(studio)
    context=await ready_context()
    await context.update_data(studio_input={'action':'edit','entity':'character','key':char['id']})
    await context.set_state(StudioStates.input)
    await call_handler(create_router(456,studio[1],studio[2]),'studio_input',message_event('38 лет, темные волосы'),context)
    assert (await repo.get('character',char['id']))['spec']['description']=='38 лет, темные волосы'


@pytest.mark.parametrize('text',['','   ','x'*10001])
async def test_empty_or_overlong_description_keeps_form_and_database(studio,replies,text):
    context=await ready_context()
    router,char,_=await open_description(studio,context)
    submitted=message_event(text)
    await call_handler(router,'studio_input',submitted,context)
    assert DESCRIPTION_PROMPT in submitted.message.answer.await_args.args[0]
    assert await context.get_state()==StudioStates.input
    assert (await repo.get('character',char['id']))['spec']==char['spec']


async def test_description_never_replaces_spec_from_json_looking_text(studio,replies):
    context=await ready_context()
    router,char,_=await open_description(studio,context)
    literal='{"description":"test","voice":"onyx","reference":"overwrite"}'
    await call_handler(router,'studio_input',message_event(literal),context)
    updated=await repo.get('character',char['id'])
    assert updated['spec']==dict(char['spec'],description=literal)


async def test_voice_settings_remain_separate_explicit_json_editor(studio,replies):
    char=await character(studio)
    router=create_router(456,studio[1],studio[2]); context=await ready_context()
    pressed=event('studio:character_settings:'+char['id'])
    await dispatch_callback(router,pressed,context)
    assert 'пришлите JSON' in pressed.bot.send_message.await_args.kwargs['text']
    profile=dict(voice='jane',emotion='good',speed=1.0)
    await call_handler(router,'studio_input',message_event(json.dumps(profile,ensure_ascii=False)),context)
    assert await repo.get('character',char['id'])==char
    assert await repo.character_speech(char['id'])==profile


async def test_foreign_project_and_non_admin_cannot_update_description(studio,replies):
    char=await character(studio)
    with pytest.raises(ValueError,match='доступ'):
        await repo.update_character_description(char['id'],999,'Чужое описание')
    context=await ready_context()
    await context.update_data(studio_input={'action':'description','entity':'character','key':char['id']})
    submitted=message_event('Не сохранять')
    await call_handler(create_router(999,studio[1],studio[2]),'studio_input',submitted,context)
    submitted.message.answer.assert_not_awaited()
    assert (await repo.get('character',char['id']))['spec']==char['spec']


async def test_description_versions_preserve_previous_spec_and_identical_save_is_idempotent(studio):
    char=await character(studio)
    await repo.update_character_description(char['id'],456,'38 лет, темные волосы')
    await repo.update_character_description(char['id'],456,'38 лет, темные волосы')
    async with repo.connect() as db:
        versions=await repo.rows(db,'SELECT * FROM cartoon_versions WHERE entity_id=? ORDER BY revision',(char['id'],))
    assert len(versions)==2
    assert json.loads(versions[-1]['snapshot'])==dict(char['spec'],description='38 лет, темные волосы')
    assert (await repo.get('character',char['id']))['revision']==2


@pytest.mark.parametrize('broken',['{not-json','[]'])
async def test_invalid_stored_spec_is_reported_without_overwriting_it(studio,broken):
    char=await character(studio)
    async with repo.connect() as db:
        await db.execute('UPDATE cartoon_characters SET spec=? WHERE id=?',(broken,char['id'])); await db.commit()
    with pytest.raises(ValueError,match='повреждены'):
        await repo.update_character_description(char['id'],456,'Новый текст')
    async with repo.connect() as db:
        stored=await repo.one(db,'SELECT spec FROM cartoon_characters WHERE id=?',(char['id'],))
    assert stored['spec']==broken

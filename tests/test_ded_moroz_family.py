"""Family pilot: ownership, numbered fields, preflight edits, tags and optional photo."""
import json
import re
import subprocess
from unittest.mock import AsyncMock

import pytest

import database
import ded_moroz as dm
import ded_moroz_family as family
import ded_moroz_sales as sales
from handlers import create_router,PosterStates
from heygen_tts import display_text
from test_ded_moroz import setup_dm,step
from test_ded_moroz_customer_flow import media
from test_handlers import callback_event,message_event,ready_context,dispatch_callback,call_handler,event_replies


def details(n=2):
    data=dict(variant='family',children_count=n,common='помогаете маме')
    for i in range(1,n+1):
        data.update({f'child_{i}_name':['Машенька','Саша','Петя','Катя','Даша','Ваня'][i-1],
                     f'child_{i}_age':'7',f'child_{i}_gender':'девочка' if i%2 else 'мальчик',
                     f'child_{i}_fact':'любишь рисовать',f'child_{i}_wish':'интересных книг'})
    return data


async def filled(n=2):
    data=details(n)
    row=await dm.begin(456,details=data)
    return row


@pytest.mark.parametrize('n',range(2,7))
def test_tagged_script_keeps_order_every_child_and_individual_wishes(n):
    data=details(n);text=family.tagged_text(data);plain=display_text(text)
    assert len(plain.split())<=130
    assert '[deep voice] Охо-хо-хо!' in text
    for tag in ('[raspy]','[drawls]','[warm]','[proud]','[reassuring]','[pause]','[slightly excited]','[serious]'):
        assert tag in text
    positions=[plain.index(data[f'child_{i}_name']) for i in range(1,n+1)]
    assert positions==sorted(positions)
    for i in range(1,n+1):
        assert plain.count(data[f'child_{i}_name'])==3
        assert data[f'child_{i}_fact'] in text and data[f'child_{i}_wish'] in text
    assert 'Умница!' in text and 'Здорово!' in text
    assert plain.startswith('Охо-хо-хо! Здравствуйте, мои дорогие '+family.joined([data[f'child_{i}_name'] for i in range(1,n+1)])+'! Это я, Дедушка Мороз!')
    assert 'ты ты' not in dm.prepare_speech_text(text)
    assert family.tagged_text(data)==dm.tagged_text(data)


@pytest.mark.parametrize('admin',[None,999])
@pytest.mark.parametrize('payload',['cat_ded_moroz_family','dm_family_count:2','dm_family_count:more','dm_family_no_photo:fake','dm_family_confirm:fake','dm_family_field:fake:child_1_name'])
async def test_public_menu_and_forged_callbacks_never_create_free_orders(credit_db,admin,payload):
    e=callback_event(payload);context=await ready_context()
    await dispatch_callback(create_router(admin_user_id=admin),e,context)
    text=str(e.send.await_args_list)
    assert family.STUB not in text
    if payload=='cat_ded_moroz_family':assert 'Одно поздравление для всей семьи!' in text
    e.ack.assert_awaited_once()
    assert not await dm.read('SELECT * FROM ded_moroz_orders')
    assert not await dm.read('SELECT * FROM ded_moroz_payments')


async def test_numbered_collection_review_edit_restart_and_confirm_before_audio(credit_db,setup_dm,monkeypatch):
    config,settings,worker=setup_dm;router=create_router(admin_user_id=456);context=await ready_context()
    monkeypatch.setattr(dm.Config,'from_env',lambda:config)
    monkeypatch.setattr(dm.Settings,'from_config',lambda _:settings)
    await dispatch_callback(router,callback_event('cat_ded_moroz_family'),context)
    assert not await dm.read('SELECT * FROM ded_moroz_orders')
    await dispatch_callback(router,callback_event('dm_family_count:2'),context)
    key=(await context.get_data())['dm_order']
    data=details()
    for field in family.labels(data):
        row=await dm.get(key)
        assert row['status']=='input' and not row['text']
        e=message_event(data[field]);await call_handler(router,'ded_moroz_input',e,context)
        if field=='child_1_name':assert 'Машенька' in str(e.message.answer.await_args_list)
    row=await dm.get(key)
    assert 'Проверьте данные' in str(e.message.answer.await_args_list)
    assert 'Машенька' in str(e.message.answer.await_args_list) and 'Саша' in str(e.message.answer.await_args_list)
    assert row['status']=='input' and not row['audio_path']
    worker.tts.generate_speech.assert_not_awaited()
    await dispatch_callback(router,callback_event('dm_family_field:'+key+':child_2_fact'),context)
    await credit_db.init_db()  # Persisted selected field; empty in-memory FSM after restart.
    context=await ready_context()
    await dispatch_callback(router,callback_event('cat_ded_moroz_family'),context)
    await dispatch_callback(router,callback_event('dm_open:'+key),context)
    assert await context.get_state()==PosterStates.ded_moroz_input
    e=message_event('занимаешься футболом');await call_handler(router,'ded_moroz_input',e,context)
    changed=await dm.get(key);saved=json.loads(changed['details'])
    assert saved['child_2_fact']=='занимаешься футболом'
    assert saved['child_1_fact']==data['child_1_fact']
    assert not saved.get('family_confirmed') and not changed['text']
    worker.tts.generate_speech.assert_not_awaited()
    await dispatch_callback(router,callback_event('dm_family_confirm:'+key),context)
    assert await context.get_state()==PosterStates.ded_moroz_photo
    row=await dm.get(key);assert json.loads(row['details'])['family_confirmed']
    await dispatch_callback(router,callback_event('dm_family_no_photo:'+key),context)
    row=await dm.get(key)
    assert row['status']=='audio_queued' and row['photo_path'] is None
    assert 'занимаешься футболом' in row['text'] and '[drawls]' in row['text']
    # A duplicate button never queues another attempt or changes the version.
    await dispatch_callback(router,callback_event('dm_family_no_photo:'+key),context)
    assert (await dm.get(key))['version']==0
    worker.tts.generate_speech.assert_not_awaited()


async def test_no_photo_requires_confirmed_fields_and_cannot_skip_selected_edit(credit_db):
    row=await filled();assert not json.loads(row['details']).get('family_confirmed')
    with pytest.raises(ValueError,match='подтвердите'):await family.queue_audio(row)
    d=json.loads(row['details']);d['family_confirmed']=True
    await dm.update(row['id'],details=json.dumps(d))
    import aiosqlite
    async with aiosqlite.connect(database.DATABASE_PATH) as db:
        await db.execute('UPDATE ded_moroz_orders SET edit_field=? WHERE id=?',('child_1_fact',row['id']));await db.commit()
    assert not await family.queue_audio(await dm.get(row['id']))
    assert (await dm.get(row['id']))['status']=='input'


async def test_single_order_is_preserved_when_family_count_starts_an_independent_order(credit_db):
    single=await dm.begin(456);await dm.update(single['id'],details=json.dumps({'name':'Марк'}))
    context=await ready_context();e=callback_event('dm_family_count:3')
    await dispatch_callback(create_router(admin_user_id=456),e,context)
    assert await context.get_state()==PosterStates.ded_moroz_input
    assert (await context.get_data())['dm_order']!=single['id']
    assert json.loads((await dm.get(single['id']))['details'])=={'name':'Марк'}
    assert len(await dm.read('SELECT * FROM ded_moroz_orders',all_rows=True))==2


async def test_unpaid_family_never_authorized_for_customer_even_with_paid_single_receipt(credit_db):
    row=await filled()
    assert not await sales.allowed(row,999)
    assert await sales.allowed(row,456)
    assert not await dm.regenerate(row['id'],456,0,admin=999)


@pytest.mark.parametrize('field,text',[
    ('child_1_name','Маша [raspy]'),('child_1_fact','{fact}'),('child_1_gender','неизвестно'),
    ('child_1_age','18'),('child_1_age','-1'),('child_1_age','семь'),('child_1_wish','длинное '*20),
])
def test_inputs_are_bounded_without_accepting_voice_tags(field,text):
    with pytest.raises(ValueError):family.value(field,text,details())


def test_questions_explain_exact_insertion_child_identity_and_examples():
    data=details(4)
    for field in family.labels(data):
        text=family.question(field,data)
        assert 'Например' in text or 'например' in text or field.endswith('_gender')
        if field!='common':assert f'Ребёнок {field.split("_")[1]} из 4' in text
        if field.endswith('_fact'):assert 'после' not in text or '«ты»' in text
        if field.endswith('_wish'):assert 'Желаю тебе' in text and 'Не начинайте' in text


async def test_worker_whole_pipeline_without_photo_keeps_approved_audio_and_counts_once(credit_db,setup_dm,media,monkeypatch):
    config,settings,worker=setup_dm;video,speech,image=media
    row=await filled();key=row['id'];d=json.loads(row['details']);d['family_confirmed']=True
    await dm.update(key,details=json.dumps(d))
    assert await family.queue_audio(await dm.get(key))
    async def generate(text,*,job_id,version):
        return (await worker.assets.write(speech.read_bytes(),'mp3',job_id=job_id,name=f'speech-v{version}.mp3'))[0]
    worker.tts.generate_speech.side_effect=generate
    for _ in range(3):await step(worker,key)
    row=await dm.get(key);assert row['status']=='audio_ready'
    assert '[raspy]' in worker.tts.generate_speech.await_args.args[0]
    assert '[drawls]' in worker.tts.generate_speech.await_args.args[0]
    assert not row['photo_path'] and row['audio_url']
    take=await dm.read('SELECT * FROM ded_moroz_audio_versions WHERE order_id=?',(key,))
    assert take['duration']<sales.MAX_SPEECH_SECONDS
    await step(worker,key);assert worker.tts.generate_speech.await_count==1
    assert await dm.approve(key,456,0)
    async def render(source,photo,audio,examples,target,name,**kwargs):
        assert photo is None
        assert audio==worker.assets.path(take['path'])
        assert name=='Машенька и Саша' and kwargs['name_genitive']=='Машеньки и Саши'
        target.parent.mkdir(parents=True,exist_ok=True);target.write_bytes(video.read_bytes())
    import video_service
    monkeypatch.setattr(video_service,'render_ded_moroz',AsyncMock(side_effect=render))
    for _ in range(6):await step(worker,key)
    row=await dm.get(key);assert row['status']=='done' and row['output_url']
    worker.heygen.create_video.assert_awaited_once()
    assert worker.heygen.create_video.await_args.args[1]==take['public_url']
    assert worker.tts.generate_speech.await_count==1
    assert worker.bot.upload_media.await_args.args[0].type.value=='video'
    counters=await database.get_all_counters()
    assert counters['dm_family_started']==1 and counters['dm_family_video_created']==1
    assert counters.get('dm_video_created',0)==0
    await worker.tick();assert (await database.get_all_counters())['dm_family_video_created']==1
    assert (await database.get_all_counters(admin_id=456)).get('dm_family_video_created',0)==0


async def test_regenerate_and_edit_rebuild_tags_without_losing_other_children(credit_db,setup_dm):
    row=await filled();key=row['id'];d=json.loads(row['details']);d['family_confirmed']=True
    await dm.update(key,details=json.dumps(d));await family.queue_audio(await dm.get(key))
    await dm.update(key,status='audio_ready')
    assert await dm.request_edit(key,456,0,admin=456)
    assert await dm.select_fact(key,456,0,'child_2_wish')
    assert await dm.revised(key,456,'новых друзей',admin=456)
    assert await dm.apply_facts(key,456,0,admin=456)
    row=await dm.get(key);new=json.loads(row['details'])
    assert new['child_1_wish']==d['child_1_wish'] and new['child_2_wish']=='новых друзей'
    assert row['text']==family.tagged_text(new)
    await dm.update(key,status='audio_ready')
    assert await dm.regenerate(key,456,1,admin=456)
    assert (await dm.get(key))['text']==family.tagged_text(new)


async def test_too_long_recording_cannot_launch_paid_video(credit_db):
    import aiosqlite
    row=await filled();key=row['id']
    await dm.update(key,status='audio_ready',audio_path=key+'/speech.mp3',audio_url='https://storage.example/speech.mp3')
    async with aiosqlite.connect(database.DATABASE_PATH) as db:
        await db.execute('INSERT INTO ded_moroz_audio_versions(order_id,version,text,provider_url,duration) VALUES (?,0,?,?,?)',(key,'text','https://storage.example/speech.mp3',80));await db.commit()
    with pytest.raises(ValueError,match='длинная'):await dm.approve(key,456,0)
    assert (await dm.get(key))['status']=='audio_ready'


async def test_real_ffmpeg_without_photo_has_no_pip_or_frame(tmp_path,media):
    from video_service import render_ded_moroz
    from media_service import audio_duration
    from ded_moroz_intro import INTRO_SECONDS
    from PIL import Image
    source,speech,_=media;examples=tmp_path/'examples';(examples/'audio').mkdir(parents=True)
    for filename in ('jingle_bells.mp3','ny_music.mp3'):(examples/'audio'/filename).write_bytes(speech.read_bytes())
    out=tmp_path/'family.mp4'
    name,options=family.title_options(details())
    await render_ded_moroz(source,None,speech,examples,out,name,**options)
    info=json.loads(subprocess.check_output(['ffprobe','-v','error','-show_streams','-of','json',str(out)]))
    assert {s['codec_name'] for s in info['streams']}=={'h264','aac'}
    assert abs(await audio_duration(out)-(await audio_duration(speech)+INTRO_SECONDS))<.15
    assert not out.with_name('family-frame.png').exists()
    assert 'для Машеньки и Саши' in out.with_name('family-intro.txt').read_text()
    frame=tmp_path/'check.png'
    subprocess.run(['ffmpeg','-v','error','-ss',str(INTRO_SECONDS+.4),'-i',str(out),'-frames:v','1',str(frame)],check=True)
    with Image.open(frame) as image:
        assert max(image.getpixel((1650,850)))<15  # No photo or decorative frame over original black background.


async def test_family_photo_is_validated_published_and_attached_to_same_order(credit_db,setup_dm,media,monkeypatch):
    from types import SimpleNamespace
    config,settings,worker=setup_dm;_,_,image=media
    row=await filled();data=json.loads(row['details']);data['family_confirmed']=True
    await dm.update(row['id'],details=json.dumps(data))
    async def download(assets,url,suffix,**kwargs):
        return await assets.write(image,suffix,job_id=kwargs['job_id'],name=kwargs['name'])
    monkeypatch.setattr(dm.Assets,'download',download)
    monkeypatch.setattr(dm.Assets,'publish',AsyncMock(return_value='https://storage.example/family.png'))
    context=await ready_context();await context.update_data(dm_order=row['id']);await context.set_state(PosterStates.ded_moroz_photo)
    event=message_event(None);event.message.body.attachments=[SimpleNamespace(type='image',payload=SimpleNamespace(url='https://max.example/photo'))]
    await dm.collect(event,context,456,config,settings)
    saved=await dm.get(row['id'])
    assert saved['status']=='audio_queued' and saved['photo_url']=='https://storage.example/family.png'
    assert worker.assets.path(saved['photo_path']).is_file()
    assert saved['text']==family.tagged_text(data)
    worker.tts.generate_speech.assert_not_awaited()


async def test_family_stats_separate_customer_interest_from_admin_tests(credit_db):
    await database.increment_counter('click_ded_moroz_family',user_id=123)
    await database.increment_counter('click_ded_moroz_family',user_id=456)
    row=await filled(3)
    await database.increment_counter('dm_family_video_created',user_id=456)
    event=message_event('/stats')
    await call_handler(create_router(admin_user_id=456),'stats',event)
    text=event.message.answer.await_args.args[0]
    assert 'Нажатия пользователей: 1 | Начато: 0 | Готово: 0' in text
    assert 'Администратор — нажатия: 1, начато: 1, готово: 1' in text
    assert '3: 1' in text


async def test_six_child_review_fits_keyboard_and_all_fields_remain_editable(credit_db):
    row=await filled(6);event=callback_event('test')
    await family.review(event,await ready_context(),row)
    markup=event.send.await_args.kwargs['attachments'][0]
    rows=markup.payload.buttons
    assert len(rows)<=30
    assert {b.payload.split(':')[-1] for buttons in rows for b in buttons if b.payload.startswith('dm_family_field:')}==set(family.labels(details(6)))


async def test_completed_family_without_photo_can_rebuild_from_saved_media(credit_db,setup_dm):
    from ded_moroz_rebuild import queue_rebuild
    _,_,worker=setup_dm;row=await filled();key=row['id']
    for name in ('heygen.mp4','speech.mp3','done.mp4'):
        await worker.assets.write(b'saved',name.split('.')[-1],job_id=key,name=name)
    await dm.update(key,status='done',raw_path=key+'/heygen.mp4',approved_audio_path=key+'/speech.mp3',output_path=key+'/done.mp4')
    result=queue_rebuild(worker.config,worker.settings,key)
    assert result['paid_requests']==0 and (await dm.get(key))['status']=='render'
    assert worker.assets.path(key+'/done.mp4').read_bytes()==b'saved'
    worker.tts.generate_speech.assert_not_awaited();worker.heygen.create_video.assert_not_awaited()


def test_missing_fact_fallback_remains_grammatical_and_tagged():
    data=details()
    data["child_1_fact"]=family.value("child_1_fact","пропустить",data)
    assert data["child_1_fact"]=="очень стараешься"
    assert "ты очень стараешься" in display_text(family.tagged_text(data))
    assert "[drawls]" in family.tagged_text(data)

import io
import json
import subprocess
import asyncio
from dataclasses import replace
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from PIL import Image

import database
import ded_moroz as dm
import ded_moroz_sales as sales
from handlers import create_router,PosterStates
from test_ded_moroz import setup_dm,step
from test_handlers import callback_event,message_event,ready_context,dispatch_callback,call_handler,event_replies
from test_prodamus import payments,payment_config,client,post_notification
from test_heygen_tts import mp3_bytes
from test_ded_moroz_sales import receipt,campaign_clock


@pytest.fixture(scope='module')
def media(tmp_path_factory):
    folder=tmp_path_factory.mktemp('ded-public-media')
    video=folder/'source.mp4';speech=folder/'speech.mp3'
    subprocess.run(['ffmpeg','-nostdin','-v','error','-y','-f','lavfi','-i','color=c=black:s=320x180:r=30:d=1.2',
        '-f','lavfi','-i','sine=frequency=200:duration=1.2','-c:v','libx264','-pix_fmt','yuv420p',
        '-c:a','aac','-shortest',str(video)],check=True)
    subprocess.run(['ffmpeg','-nostdin','-v','error','-y','-f','lavfi','-i','sine=frequency=400:duration=0.8',
        '-c:a','libmp3lame',str(speech)],check=True)
    image=io.BytesIO();Image.new('RGB',(941,1672),'blue').save(image,format='PNG')
    return video,speech,image.getvalue()


@pytest.fixture
def customer_setup(setup_dm,payments,media,monkeypatch):
    config,settings,worker=setup_dm;video,speech,image=media
    config=replace(payments.config,admin_user_id=999,storage_bucket=config.storage_bucket,
                   storage_public_base_url=config.storage_public_base_url)
    worker.admin=999;worker.config=config;payments.config=config
    for name in ('ny_music.mp3','jingle_bells.mp3'):(settings.examples/'audio'/name).write_bytes(speech.read_bytes())
    monkeypatch.setattr(dm.Config,'from_env',lambda:config)
    monkeypatch.setattr(dm.Settings,'from_config',lambda _:settings)
    monkeypatch.setattr(sales,'EXAMPLE',video)
    async def download(assets,url,suffix,**kwargs):
        data=image if 'child' in url else video.read_bytes()
        return await assets.write(data,suffix,job_id=kwargs['job_id'],name=kwargs['name'])
    monkeypatch.setattr(dm.Assets,'download',download)
    async def worker_download(url,suffix,**kwargs):return await download(worker.assets,url,suffix,**kwargs)
    worker.assets.download=AsyncMock(side_effect=worker_download)
    monkeypatch.setattr(dm.Assets,'publish',AsyncMock(side_effect=lambda path:'https://storage.example/'+path))
    async def generate(text,*,job_id,version):
        path,_=await worker.assets.write(speech.read_bytes(),'mp3',job_id=job_id,name=f'speech-v{version}.mp3')
        return path
    worker.tts.generate_speech.side_effect=generate
    return worker,create_router(payments=payments,admin_user_id=999)


async def test_customer_complete_flow_two_audios_choose_first_and_next_invoice(credit_db,payments,client,customer_setup):
    worker,router=customer_setup;context=await ready_context()
    await dispatch_callback(router,callback_event('dm_continue'),context)
    assert await context.get_state()!=PosterStates.ded_moroz_input
    assert not await dm.read('SELECT * FROM ded_moroz_orders')
    start=callback_event('dm_start');await dispatch_callback(router,start,context)
    pending=await dm.read('SELECT * FROM ded_moroz_payments WHERE user_id=456')
    assert pending['status']=='pending' and pending['amount_kopecks']==99000
    data=await receipt(payments,user=456)
    assert (await post_notification(client,data)).status==200
    await dispatch_callback(router,callback_event('dm_continue'),context)
    assert await context.get_state()==PosterStates.ded_moroz_input
    values=['Марк','любишь футбол','научился плавать','добрый','помогать маме']
    for text in values:await call_handler(router,'ded_moroz_input',message_event(text),context)
    key=(await context.get_data())['dm_order']
    photo=message_event('');photo.message.body.attachments=[SimpleNamespace(type='image',payload=SimpleNamespace(url='https://image.example/child.png'))]
    await call_handler(router,'ded_moroz_input',photo,context)
    assert (await dm.get(key))['status']=='audio_queued'
    worker.bot.send_message.return_value=SimpleNamespace(body=SimpleNamespace(mid='audio-1'))
    for _ in range(3):await step(worker,key)
    first=await dm.read('SELECT * FROM ded_moroz_audio_versions WHERE order_id=? AND version=0',(key,))
    assert first['duration']<sales.MAX_SPEECH_SECONDS and first['public_url']
    await dispatch_callback(router,callback_event(f'dm_audio_edit:{key}:0'),context)
    await dispatch_callback(router,callback_event(f'dm_fact_edit:{key}:0:name'),context)
    await call_handler(router,'ded_moroz_input',message_event('Дима'),context)
    await dispatch_callback(router,callback_event(f'dm_facts_apply:{key}:0'),context)
    worker.bot.send_message.return_value=SimpleNamespace(body=SimpleNamespace(mid='audio-2'))
    for _ in range(3):await step(worker,key)
    current=await dm.get(key)
    assert current['version']==1 and current['regeneration_attempts']==1
    assert not await dm.request_edit(key,456,1,admin=999)
    assert not await dm.regenerate(key,456,1,admin=999)
    assert worker.tts.generate_speech.await_count==2
    await credit_db.init_db()  # A restart must retain both paid variants and their fact snapshots.
    context=await ready_context()
    pick=callback_event(f'dm_audio_choose:{key}:1:0')
    await dispatch_callback(router,pick,context)
    assert 'Дедушка Мороз записывает' in pick.send.await_args.args[0]
    selected=await dm.get(key)
    assert selected['approved_audio_path']==first['path'] and json.loads(selected['details'])['name']=='Марк'
    for _ in range(7):
        if (await dm.get(key))['status']=='done':break
        await step(worker,key)
    final=await dm.get(key)
    assert final['status']=='done'
    worker.heygen.create_video.assert_awaited_once_with('look_santa',first['public_url'],'dm-video-'+key)
    assert worker.bot.upload_media.await_args.args[0].type.value=='video'
    customer_text=[c.kwargs.get('text','') for c in worker.bot.send_message.await_args_list if c.kwargs.get('user_id')==456]
    assert not any(brand.lower() in t.lower() for t in customer_text for brand in ('HeyGen','Avatar IV','OpenAI','SpeechKit'))
    assert any('фотографию' in t for t in customer_text)
    await dispatch_callback(router,callback_event('dm_another'),context)
    new=await dm.read("SELECT * FROM ded_moroz_payments WHERE user_id=456 AND status='pending'")
    assert new and new['order_id']!=data['order_num']
    assert worker.tts.generate_speech.await_count==2
    assert worker.assets.path(final['output_path']).is_file()


async def test_unpaid_user_cannot_use_another_customers_inputs(credit_db,customer_setup):
    worker,router=customer_setup
    invoice=await sales.checkout(789);await sales.mark_paid(invoice['order_id'],'someone-else')
    row=await sales.claim(789,999)
    context=await ready_context();await context.update_data(dm_order=row['id']);await context.set_state(PosterStates.ded_moroz_input)
    await call_handler(router,'ded_moroz_input',message_event('Украденное имя'),context)
    assert json.loads((await dm.get(row['id']))['details'])=={}
    await dispatch_callback(router,callback_event(f"dm_audio_choose:{row['id']}:0:0"),context)
    worker.tts.generate_speech.assert_not_awaited();worker.heygen.create_video.assert_not_awaited()


@pytest.mark.parametrize('field,value',[('name','А'*25),('fact1','факт '*7),('fact2','д'*41),('fact3','добрый и смелый'),('task','x'*46)])
def test_customer_field_limits(field,value):
    with pytest.raises(ValueError,match='сократите'):sales.validate_field(field,value)


async def test_actual_audio_duration_blocks_paid_video_even_if_metadata_is_wrong(credit_db,customer_setup,monkeypatch):
    import media_service
    worker,_=customer_setup
    pay=await sales.checkout(456);await sales.mark_paid(pay['order_id'],'paid-duration')
    row=await sales.claim(456,999);key=row['id']
    await worker.assets.write(b'photo','png',job_id=key,name='child.png')
    await worker.assets.write(b'audio','mp3',job_id=key,name='speech.mp3')
    await dm.update(key,status='video_queued',photo_path=key+'/child.png',audio_path=key+'/speech.mp3',audio_url='https://storage.example/speech.mp3')
    monkeypatch.setattr(media_service,'audio_duration',AsyncMock(return_value=67.0))
    await worker.tick()
    assert (await dm.get(key))['status']=='manual'
    worker.heygen.create_video.assert_not_awaited();worker.heygen.create_avatar.assert_not_awaited()


async def test_oversized_audio_cannot_be_approved(credit_db,customer_setup):
    import aiosqlite
    worker,_=customer_setup
    pay=await sales.checkout(456);await sales.mark_paid(pay['order_id'],'long-audio')
    row=await sales.claim(456,999);key=row['id']
    await dm.update(key,status='audio_ready',audio_path=key+'/long.mp3',audio_url='https://storage.example/long.mp3')
    async with aiosqlite.connect(database.DATABASE_PATH) as db:
        await db.execute("INSERT INTO ded_moroz_audio_versions(order_id,version,text,provider_url,path,public_url,details,duration) VALUES (?,0,'текст','',?,?,?,67)",
            (key,key+'/long.mp3','https://storage.example/long.mp3','{}'));await db.commit()
    with pytest.raises(ValueError):await dm.approve(key,456,0)
    with pytest.raises(ValueError):await dm.choose_audio(key,456,0,0,999)
    assert (await dm.get(key))['status']=='audio_ready'
    worker.heygen.create_video.assert_not_awaited()


async def test_payment_return_payload_opens_name_question_without_second_invoice(credit_db,customer_setup):
    worker,router=customer_setup
    invoice=await sales.checkout(456);await sales.mark_paid(invoice['order_id'],'returned-payment')
    context=await ready_context();event=message_event('/start dm_paid_'+invoice['order_id'])
    await call_handler(router,'start',event,context)
    assert await context.get_state()==PosterStates.ded_moroz_input
    assert any(c.args and c.args[0].startswith(dm.GREETING) for c in event.message.answer.await_args_list)
    assert len(await dm.read('SELECT * FROM ded_moroz_payments',all_rows=True))==1
    worker.tts.generate_speech.assert_not_awaited()


async def test_double_start_reuses_one_invoice_and_provider_link(credit_db,customer_setup,payments):
    _,router=customer_setup
    contexts=[await ready_context() for _ in range(3)]
    await asyncio.gather(*(dispatch_callback(router,callback_event('dm_start'),context) for context in contexts))
    orders=await dm.read('SELECT * FROM ded_moroz_payments',all_rows=True)
    assert len(orders)==1 and orders[0]['payment_link']
    payments._request_payment_link.assert_awaited_once()


async def test_paid_customer_audio_recovery_reuses_verified_file_without_posting_again(credit_db,customer_setup,mp3_bytes):
    from test_heygen_tts import make_adapter
    worker,_=customer_setup
    invoice=await sales.checkout(456);await sales.mark_paid(invoice['order_id'],'recovery-payment')
    row=await sales.claim(456,999);key=row['id']
    facts=dict(name='Марк',fact1='любишь футбол',fact2='научился плавать',fact3='добрый',task='читать')
    await dm.update(key,text=dm.TEMPLATE.format(**facts),details=json.dumps(facts,ensure_ascii=False),status='audio_submitting')
    calls=[];worker.tts=make_adapter(worker.assets,mp3_bytes,calls)
    path=await worker.tts.generate_speech((await dm.get(key))['text'],job_id=key,version=0)
    await worker.recover_interrupted()
    saved=await dm.get(key)
    assert saved['status']=='audio_publish' and saved['audio_path']==path
    assert len(calls)==2  # One GET and one original synthesis POST; recovery made no new request.
    archive=await dm.read('SELECT * FROM ded_moroz_audio_versions WHERE order_id=?',(key,))
    assert json.loads(archive['details'])==facts and archive['duration']>0

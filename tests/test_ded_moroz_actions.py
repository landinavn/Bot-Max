import asyncio
import json
import struct
import subprocess
import zlib
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from PIL import Image

import database
import ded_moroz as dm
import ded_moroz_sales as sales
import ded_moroz_broadcast as broadcast
import ded_moroz_customer as customer
from ded_moroz_media import child_png
from handlers import create_router
from test_ded_moroz_customer_flow import customer_setup,media
from test_ded_moroz import setup_dm
from test_ded_moroz_sales import campaign_clock,stamp
from test_prodamus import payments,payment_config
from test_handlers import callback_event,message_event,ready_context,dispatch_callback,call_handler,event_replies


async def completed(worker):
    invoice=await sales.checkout(456);await sales.mark_paid(invoice['order_id'],'actions-paid')
    row=await sales.claim(456,999)
    target=worker.assets.path(row['id']+'/result.mp4');target.parent.mkdir(parents=True,exist_ok=True);target.write_bytes(b'mp4')
    await dm.update(row['id'],status='done',output_path=target.relative_to(worker.assets.root).as_posix(),output_url='https://storage.example/result.mp4')
    return await dm.get(row['id'])


async def test_review_is_forwarded_to_admin_once_and_share_never_generates(credit_db,customer_setup):
    worker,router=customer_setup;row=await completed(worker);context=await ready_context()
    await dispatch_callback(router,callback_event('dm_review:'+row['id']),context)
    await call_handler(router,'ded_moroz_review',message_event('Ребёнку понравилось, спасибо!'),context)
    await customer.process_auxiliary(worker);await customer.process_auxiliary(worker)
    reviews=[c for c in worker.bot.send_message.await_args_list if 'Ребёнку понравилось' in c.kwargs.get('text','')]
    assert len(reviews)==1 and reviews[0].kwargs['user_id']==999
    assert (await database.get_all_counters())['dm_review_received']==1
    event=callback_event('dm_share:'+row['id']);await dispatch_callback(router,event,context)
    assert 'Переслать' in event.send.await_args.args[0]
    assert (await database.get_all_counters())['dm_share_click']==1
    worker.tts.generate_speech.assert_not_awaited();worker.heygen.create_video.assert_not_awaited()


async def test_mobile_job_is_cached_and_sent_as_video(credit_db,customer_setup,monkeypatch):
    worker,router=customer_setup;row=await completed(worker);context=await ready_context()
    async def compact(source,target,**kwargs):target.write_bytes(b'mobile');return target
    convert=AsyncMock(side_effect=compact);monkeypatch.setattr(customer,'compact_video',convert)
    for _ in range(2):
        await dispatch_callback(router,callback_event('dm_mobile:'+row['id']),context)
        await customer.process_auxiliary(worker)
    convert.assert_awaited_once()
    assert worker.bot.upload_media.await_args.args[0].type.value=='video'
    assert (await dm.get(row['id']))['status']=='done'
    assert worker.assets.path(row['output_path']).read_bytes()==b'mp4'
    worker.tts.generate_speech.assert_not_awaited();worker.heygen.create_video.assert_not_awaited()


async def test_compact_mp4_is_main_profile_720p_aac_and_faststart(tmp_path,media):
    source,_,_=media;out=tmp_path/'mobile.mp4'
    await customer.compact_video(source,out)
    info=json.loads(subprocess.check_output(['ffprobe','-v','error','-show_streams','-of','json',str(out)]))
    video=next(s for s in info['streams'] if s['codec_type']=='video')
    audio=next(s for s in info['streams'] if s['codec_type']=='audio')
    assert (video['codec_name'],video['profile'],video['codec_tag_string'],video['pix_fmt'])==('h264','Main','avc1','yuv420p')
    assert (video['width'],video['height'])==(1280,720) and audio['codec_name']=='aac'
    data=out.read_bytes();assert data.find(b'moov')<data.find(b'mdat') and len(data)<25*1024*1024


async def test_preview_rejects_full_length_source_before_upload(monkeypatch,tmp_path):
    path=tmp_path/'fake-full.mp4';path.write_bytes(b'full')
    monkeypatch.setattr(sales,'EXAMPLE',path)
    import media_service
    monkeypatch.setattr(media_service,'audio_duration',AsyncMock(return_value=65.5))
    bot=SimpleNamespace(upload_media=AsyncMock())
    with pytest.raises(ValueError,match='15 секунд'):await broadcast.messages(bot)
    bot.upload_media.assert_not_awaited()


async def test_broadcast_requires_tomorrows_window_and_resumes_per_message(credit_db,monkeypatch):
    for user in (456,789):await database.increment_counter('active',user_id=user)
    # increment_counter does not create a user row: register explicit active subscribers.
    import aiosqlite
    async with aiosqlite.connect(database.DATABASE_PATH) as db:
        await db.executemany('INSERT OR IGNORE INTO users(user_id) VALUES (?)',[(456,),(789,)]);await db.commit()
    monkeypatch.setattr(broadcast,'messages',AsyncMock(return_value=[('video',[]),('offer',[])]))
    monkeypatch.setattr(broadcast.asyncio,'sleep',AsyncMock())
    event=callback_event('dm_broadcast_confirm');event.bot.send_message=AsyncMock(return_value=object())
    monkeypatch.setattr(database,'_now_ts',lambda:stamp(11,15))
    await broadcast.send(event,asyncio.Lock())
    event.bot.send_message.assert_not_awaited()
    monkeypatch.setattr(database,'_now_ts',lambda:stamp(11,16))
    failed=False
    async def send(**kwargs):
        nonlocal failed
        if kwargs['user_id']==789 and kwargs['text']=='offer' and not failed:
            failed=True;raise RuntimeError('temporary MAX error')
        return object()
    event.bot.send_message.side_effect=send
    await broadcast.send(event,asyncio.Lock())
    state=await dm.read('SELECT * FROM ded_moroz_broadcast WHERE user_id=789')
    assert state['next_message']==1 and not state['completed']
    event.bot.send_message.reset_mock();event.bot.send_message.side_effect=None
    await broadcast.send(event,asyncio.Lock())
    event.bot.send_message.assert_awaited_once_with(user_id=789,text='offer',attachments=[])
    event.bot.send_message.reset_mock()
    await broadcast.send(event,asyncio.Lock())
    event.bot.send_message.assert_not_awaited()


async def test_regular_user_cannot_confirm_or_prepare_broadcast(credit_db):
    router=create_router(admin_user_id=999);context=await ready_context()
    event=callback_event('dm_broadcast_confirm');await dispatch_callback(router,event,context)
    event.bot.send_message.assert_not_awaited()
    command=message_event('/broadcast_ded_moroz')
    await call_handler(router,'ded_moroz_broadcast_preview',command)
    command.message.answer.assert_not_awaited()


@pytest.mark.parametrize('fmt',['JPEG','PNG','WEBP'])
def test_max_photo_format_is_detected_by_content_and_normalized(tmp_path,fmt):
    file=tmp_path/'wrong-extension.jpg';Image.new('RGB',(941,1672),'blue').save(file,format=fmt)
    import io
    result=child_png(file)
    with Image.open(io.BytesIO(result)) as image:assert image.format=='PNG' and image.size==(941,1672)


def test_photo_pixel_limit_is_checked_before_decoding(tmp_path):
    file=tmp_path/'oversized.png';Image.new('RGB',(2,2),'blue').save(file)
    data=bytearray(file.read_bytes());data[16:24]=struct.pack('>II',10001,4000)
    data[29:33]=struct.pack('>I',zlib.crc32(data[12:29])&0xffffffff);file.write_bytes(data)
    with pytest.raises(ValueError,match='40 000 000'):child_png(file)


def test_animated_photo_is_not_accepted(tmp_path):
    file=tmp_path/'animated.webp'
    Image.new('RGB',(10,10),'blue').save(file,format='WEBP',save_all=True,append_images=[Image.new('RGB',(10,10),'red')],duration=100,loop=0)
    with pytest.raises(ValueError,match='без анимации'):child_png(file)

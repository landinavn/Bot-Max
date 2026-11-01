"""Rabbit trial uses one mother's portrait, one son's portrait, then a shared family frame."""
from email.parser import BytesParser
from email.policy import default
from io import BytesIO
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from PIL import Image

import handlers
import image_service as images
from handlers import create_router,PosterStates
from test_handlers import (callback_event,ready_context,dispatch_callback,call_handler,photo_event,message_event,
    event_replies,max_image_upload,isolated_example_directory, example_photos,text_correction)
from test_sales_stats import sales_payments,deliver_webhook
from test_image_service import config,storage,api,PHOTO_URL,IMAGE_EDIT_URL,PNG,ENCODED_PNG,PUBLIC_URL,post_requests


async def select_rabbit(router,context):
    await dispatch_callback(router,callback_event('photo_style_ny'),context)
    await dispatch_callback(router,callback_event('photo_tariff_590'),context)
    event=callback_event('photo_temp_rabbit')
    await dispatch_callback(router,event,context)
    return event


async def test_admin_free_rabbit_three_frames_use_previous_frames_for_outfits(credit_db):
    frames=AsyncMock(side_effect=['https://cdn.example/mother.jpg','https://cdn.example/son.jpg','https://cdn.example/together.jpg'])
    payments=SimpleNamespace(create_payment_link=AsyncMock())
    router=create_router(admin_user_id=456,photosession_generator=frames,payments=payments)
    context=await ready_context()
    event=await select_rabbit(router,context)
    assert 'Оплата пропущена' in event.send.await_args.args[0]
    assert 'мамы и сына (2 человека)' in event.send.await_args.args[0]
    assert 'Кролик на исходном фото не нужен' in event.send.await_args.args[0]
    assert await context.get_state()==PosterStates.waiting_for_photosession_photo
    await call_handler(router,'collect_photosession_photo',photo_event(),context)
    assert frames.await_count==3
    assert [c.args[1] for c in frames.await_args_list]==list(images.NY_RABBIT_PROMPTS)
    assert frames.await_args_list[0].kwargs=={}
    assert frames.await_args_list[1].kwargs=={'style_reference_urls':('https://cdn.example/mother.jpg',)}
    assert frames.await_args_list[2].kwargs=={'style_reference_urls':('https://cdn.example/mother.jpg','https://cdn.example/son.jpg')}
    assert all(c.args[0]=='https://photos.example.com/source.png' for c in frames.await_args_list)
    payments.create_payment_link.assert_not_awaited()
    assert (await credit_db.get_photo_template_stats('rabbit',admin_id=456))['paid_packages']==0
    assert await credit_db.get_active_promo(456,'photo') is None


@pytest.mark.parametrize('promo',[False,True])
async def test_rabbit_paid_checkout_webhook_and_result_stats(credit_db,sales_payments,promo):
    await credit_db.record_first_start(456,'youtube')
    if promo: await credit_db.set_promo(456,'photo')
    frames=AsyncMock(side_effect=[f'https://cdn.example/rabbit-{i}.jpg' for i in range(3)])
    router=create_router(admin_user_id=999,photosession_generator=frames,payments=sales_payments)
    context=await ready_context()
    await select_rabbit(router,context)
    order=await credit_db.get_photo_order((await context.get_data())['photo_order_id'])
    assert order['amount_kopecks']==(47200 if promo else 59000)
    assert order['template']=='rabbit' and order['style']=='photo_style_ny'
    frames.assert_not_awaited()
    assert (await credit_db.get_photo_template_stats('rabbit'))['paid_packages']==0
    assert (await deliver_webhook(sales_payments,order)).status==200
    assert (await deliver_webhook(sales_payments,order)).status==200
    await context.clear()
    resume=callback_event('photo_resume:'+order['order_id']);await dispatch_callback(router,resume,context)
    assert 'мамы и сына (2 человека)' in resume.send.await_args.args[0]
    await call_handler(router,'collect_photosession_photo',photo_event(),context)
    stats=await credit_db.get_photo_template_stats('rabbit',admin_id=999)
    assert stats=={'paid_packages':1,'revenue_kopecks':order['amount_kopecks'],'generated_photos':3,'completed_packages':1}
    assert (await credit_db.get_all_counters())['buy_photo_590']==1
    assert (await credit_db.get_photo_package_balance(order['order_id']))==0
    assert await credit_db.get_active_promo(456,'photo') is not None
    source=next(r for r in await credit_db.get_source_stats(admin_id=999) if r['source']=='youtube')
    assert source['buyers']==1 and source['revenue_kopecks']==order['amount_kopecks']
    await dispatch_callback(router,resume,context)
    assert frames.await_count==3


async def test_rabbit_third_frame_resume_keeps_both_previous_style_images(credit_db,sales_payments):
    key=await credit_db.create_photo_order('photo_rabbit_retry',456,590)
    await credit_db.update_photo_order(key,style='photo_style_ny',template='rabbit')
    await deliver_webhook(sales_payments,await credit_db.get_photo_order(key))
    frames=AsyncMock(side_effect=['https://cdn.example/mother.jpg','https://cdn.example/son.jpg',RuntimeError('third failed')])
    router=create_router(admin_user_id=999,photosession_generator=frames,payments=sales_payments)
    context=await ready_context()
    await dispatch_callback(router,callback_event('photo_resume:'+key),context)
    await call_handler(router,'collect_photosession_photo',photo_event(),context)
    assert (await credit_db.get_photo_template_stats('rabbit'))['generated_photos']==2
    assert await credit_db.get_active_promo(456,'photo') is None
    await context.clear()
    frames.side_effect=None;frames.return_value='https://cdn.example/family.jpg'
    await dispatch_callback(router,callback_event('photo_resume:'+key),context)
    assert frames.await_count==4
    assert frames.await_args.kwargs=={'style_reference_urls':('https://cdn.example/mother.jpg','https://cdn.example/son.jpg')}
    assert (await credit_db.get_photo_template_stats('rabbit'))['completed_packages']==1


@pytest.mark.parametrize('owner,paid',[(456,False),(101,True)])
async def test_rabbit_cannot_use_unpaid_or_someone_elses_package(credit_db,sales_payments,owner,paid):
    key=await credit_db.create_photo_order('photo_rabbit_access',owner,590)
    await credit_db.update_photo_order(key,style='photo_style_ny',template='rabbit')
    if paid: await deliver_webhook(sales_payments,await credit_db.get_photo_order(key))
    frames=AsyncMock();router=create_router(admin_user_id=999,payments=sales_payments,photosession_generator=frames)
    context=await ready_context()
    await context.update_data(photo_style='photo_style_ny',photo_tariff='photo_tariff_590',template='rabbit',photo_order_id=key)
    await context.set_state(PosterStates.waiting_for_photosession_photo)
    await call_handler(router,'collect_photosession_photo',photo_event(),context)
    frames.assert_not_awaited()


@pytest.mark.parametrize('style',['photo_style_studio','photo_style_walk'])
async def test_rabbit_stays_in_new_year_style_only(credit_db,style):
    context=await ready_context();await context.update_data(photo_style=style,photo_tariff='photo_tariff_590')
    frames=AsyncMock();payments=SimpleNamespace(create_payment_link=AsyncMock())
    await dispatch_callback(create_router(admin_user_id=456,photosession_generator=frames,payments=payments),callback_event('photo_temp_rabbit'),context)
    frames.assert_not_awaited();payments.create_payment_link.assert_not_awaited()
    assert await context.get_state()!=PosterStates.waiting_for_photosession_photo


async def test_rabbit_can_be_chosen_after_package_payment_and_rejected_after_other_plot_started(credit_db,sales_payments):
    key=await credit_db.create_photo_order('photo_rabbit_late',456,590)
    await credit_db.update_photo_order(key,style='photo_style_ny')
    await deliver_webhook(sales_payments,await credit_db.get_photo_order(key))
    router=create_router(admin_user_id=999,payments=sales_payments);context=await ready_context()
    await context.update_data(photo_style='photo_style_ny',photo_tariff='photo_tariff_590',photo_order_id=key)
    await dispatch_callback(router,callback_event('photo_temp_rabbit'),context)
    assert (await credit_db.get_photo_template_stats('rabbit'))['paid_packages']==1
    await credit_db.save_photo_result(key,1,'https://cdn.example/rabbit-first.jpg')
    event=callback_event('photo_temp_mom_daughter');await dispatch_callback(router,event,context)
    assert 'Продолжите исходную фотосессию' in event.send.await_args.args[0]
    assert (await credit_db.get_photo_order(key))['template']=='rabbit'


async def test_three_image_edit_sends_client_then_mother_then_son(config,storage,api,monkeypatch):
    monkeypatch.setattr(images.Config,'from_env',lambda:config)
    blobs=[]
    for color in ('ivory','lightblue'):
        out=BytesIO();Image.new('RGB',(2,2),color).save(out,format='PNG');blobs.append(out.getvalue())
    urls=('https://cdn.example/mother.png','https://cdn.example/son.png')
    api.get(PHOTO_URL,body=PNG,content_type='image/png')
    for url,data in zip(urls,blobs): api.get(url,body=data,content_type='image/png')
    api.post(IMAGE_EDIT_URL,payload={'data':[{'b64_json':ENCODED_PNG}]})
    api.head(PUBLIC_URL,status=200,repeat=True)
    result=await images.generate_photosession_frame(PHOTO_URL,images.NY_RABBIT_PROMPTS[2],style_reference_urls=urls)
    assert PUBLIC_URL.fullmatch(result)
    request=post_requests(api)[0];body=await request.aread()
    message=BytesParser(policy=default).parsebytes(
        f"Content-Type: {request.headers['Content-Type']}\r\nMIME-Version: 1.0\r\n\r\n".encode()+body)
    parts=list(message.iter_parts())
    assert [p.get_payload(decode=True) for p in parts if p.get_filename()]==[PNG,*blobs]
    prompt=next(p.get_payload(decode=True).decode() for p in parts if p.get_param('name',header='content-disposition')=='prompt')
    assert 'FIRST image is the original client photo' in prompt and 'SECOND and THIRD images' in prompt
    assert 'EXACTLY TWO humans' in prompt and 'EXACTLY ONE calm' in prompt


@pytest.mark.parametrize('kwargs',[
    {'style_reference_urls':[]},{'style_reference_urls':['https://x.example/a']*3},
    {'style_reference_urls':'https://x.example/a'},{'style_reference_urls':['http://invalid.example/a']},
    {'style_reference_url':'https://x.example/a','style_reference_urls':['https://x.example/b']},
])
async def test_invalid_style_inputs_stop_before_paid_api(monkeypatch,kwargs):
    generate=AsyncMock();monkeypatch.setattr(images,'_generate_and_store',generate)
    with pytest.raises(ValueError): await images.generate_photosession_frame(PHOTO_URL,'portrait',**kwargs)
    generate.assert_not_awaited()


async def test_rabbit_stats_exclude_admin_and_count_payments_once(credit_db,sales_payments):
    for actor in (456,999):
        await credit_db.increment_counter('select_photo_rabbit',user_id=actor)
        key=await credit_db.create_photo_order(f'photo_rabbit_{actor}',actor,590)
        await credit_db.update_photo_order(key,style='photo_style_ny',template='rabbit')
        await deliver_webhook(sales_payments,await credit_db.get_photo_order(key),payment_id=f'p-{actor}')
    event=message_event('/stats');event.get_ids=lambda:(123,999)
    await call_handler(create_router(admin_user_id=999),'stats',event)
    block=event.message.answer.await_args.args[0].split('🐰 Фотосессия «С кроликом»:',1)[1].split('👑 Эксклюзив',1)[0]
    assert '- Выбор сюжета (клики): 1' in block and '- Оплачено пакетов: 1' in block
    assert '- Выручка: 590,00 ₽' in block and '- Сгенерировано фото: 0' in block


@pytest.fixture(autouse=True)
def photosession_examples(example_photos):
    return example_photos

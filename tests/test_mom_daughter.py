"""Mother/daughter NY trial: style consistency, paid/resumed generation and YouTube attribution."""
from email.parser import BytesParser
from email.policy import default
from io import BytesIO
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from PIL import Image

import handlers
import image_service as images
from handlers import PosterStates,create_router
from test_handlers import (callback_event,ready_context,dispatch_callback,call_handler,photo_event,message_event,
                           event_replies,max_image_upload,isolated_example_directory, example_photos,text_correction)
from test_sales_stats import sales_payments,deliver_webhook
from test_image_service import config,storage,api,PHOTO_URL,IMAGE_EDIT_URL,PNG,ENCODED_PNG,PUBLIC_URL,post_requests


async def choose_trial(router,context):
    await dispatch_callback(router,callback_event('photo_style_ny'),context)
    await dispatch_callback(router,callback_event('photo_tariff_590'),context)
    event=callback_event('photo_temp_mom_daughter')
    await dispatch_callback(router,event,context)
    return event


async def test_admin_trial_generates_three_frames_with_same_style_reference(credit_db):
    frames=AsyncMock(side_effect=[f'https://cdn.example/mom-{i}.jpg' for i in range(1,4)])
    payments=SimpleNamespace(create_payment_link=AsyncMock())
    router=create_router(admin_user_id=456,photosession_generator=frames,payments=payments)
    context=await ready_context()
    event=await choose_trial(router,context)
    assert 'Оплата пропущена' in event.send.await_args.args[0]
    assert 'мамы и дочки (2 человека)' in event.send.await_args.args[0]
    assert await context.get_state()==PosterStates.waiting_for_photosession_photo
    photo=photo_event()
    await call_handler(router,'collect_photosession_photo',photo,context)
    assert frames.await_count==3
    assert [c.args[1] for c in frames.await_args_list]==list(images.NY_MOM_DAUGHTER_PROMPTS)
    assert all(c.args[0]=='https://photos.example.com/source.png' for c in frames.await_args_list)
    assert frames.await_args_list[0].kwargs=={}
    assert all(c.kwargs=={'style_reference_url':'https://cdn.example/mom-1.jpg'} for c in frames.await_args_list[1:])
    payments.create_payment_link.assert_not_awaited()
    assert (await credit_db.get_photo_template_stats('mom_daughter',admin_id=456))['paid_packages']==0


async def test_customer_payment_unlocks_three_mom_frames_and_attributes_youtube_sale(credit_db,sales_payments):
    await credit_db.record_first_start(456,'youtube')
    frames=AsyncMock(side_effect=[f'https://cdn.example/paid-{i}.jpg' for i in range(1,4)])
    router=create_router(admin_user_id=999,photosession_generator=frames,payments=sales_payments)
    context=await ready_context()
    await choose_trial(router,context)
    data=await context.get_data()
    order=await credit_db.get_photo_order(data['photo_order_id'])
    assert order['amount_kopecks']==59000 and order['template']=='mom_daughter' and order['style']=='photo_style_ny'
    frames.assert_not_awaited()
    assert (await credit_db.get_photo_template_stats('mom_daughter'))['paid_packages']==0
    assert (await deliver_webhook(sales_payments,order,payment_id='mom-paid')).status==200
    assert (await deliver_webhook(sales_payments,order,payment_id='mom-paid')).status==200
    # Resume after losing in-memory FSM, using persisted paid order and selected template.
    await context.clear()
    event=callback_event('photo_resume:'+order['order_id'])
    await dispatch_callback(router,event,context)
    assert 'мамы и дочки (2 человека)' in event.send.await_args.args[0]
    await call_handler(router,'collect_photosession_photo',photo_event(),context)
    assert frames.await_count==3
    stats=await credit_db.get_photo_template_stats('mom_daughter',admin_id=999)
    assert stats=={'paid_packages':1,'revenue_kopecks':59000,'generated_photos':3,'completed_packages':1}
    assert (await credit_db.get_all_counters())['buy_photo_590']==1
    source=next(r for r in await credit_db.get_source_stats(admin_id=999) if r['source']=='youtube')
    assert source['total_users']==1 and source['buyers']==1 and source['revenue_kopecks']==59000
    assert await credit_db.get_active_promo(456,'photo') is not None
    # An already delivered paid package cannot generate extra frames.
    await dispatch_callback(router,callback_event('photo_resume:'+order['order_id']),context)
    assert frames.await_count==3


async def test_resume_failed_second_frame_uses_persisted_first_outfit_image(credit_db,sales_payments):
    order_id=await credit_db.create_photo_order('photo_mom_resume',456,590)
    await credit_db.update_photo_order(order_id,style='photo_style_ny',template='mom_daughter')
    order=await credit_db.get_photo_order(order_id)
    await deliver_webhook(sales_payments,order)
    frames=AsyncMock(side_effect=['https://cdn.example/style-first.jpg',RuntimeError('second failed')])
    router=create_router(admin_user_id=999,payments=sales_payments,photosession_generator=frames)
    context=await ready_context()
    await dispatch_callback(router,callback_event('photo_resume:'+order_id),context)
    await call_handler(router,'collect_photosession_photo',photo_event(),context)
    assert len(await credit_db.get_photo_results(order_id))==1
    await context.clear()
    frames.side_effect=['https://cdn.example/frame2.jpg','https://cdn.example/frame3.jpg']
    await dispatch_callback(router,callback_event('photo_resume:'+order_id),context)
    assert frames.await_count==4
    assert all(c.kwargs.get('style_reference_url')=='https://cdn.example/style-first.jpg' for c in frames.await_args_list[1:])
    stats=await credit_db.get_photo_template_stats('mom_daughter')
    assert stats['generated_photos']==3 and stats['completed_packages']==1


async def test_mom_trial_promo_price_and_selection_after_payment_are_counted(credit_db,sales_payments):
    await credit_db.set_promo(456,'photo')
    router=create_router(admin_user_id=999,payments=sales_payments)
    context=await ready_context()
    await dispatch_callback(router,callback_event('promo_photo'),context)
    await dispatch_callback(router,callback_event('photo_style_ny'),context)
    await dispatch_callback(router,callback_event('photo_tariff_590'),context)
    assert 'photo_order_id' not in await context.get_data()
    key=await credit_db.create_photo_order('photo_legacy_mom',456,590)
    await credit_db.update_photo_order(key,style='photo_style_ny')
    order=await credit_db.get_photo_order(key)
    assert order['amount_kopecks']==47200
    await deliver_webhook(sales_payments,order)
    await context.update_data(photo_order_id=key)
    assert (await credit_db.get_photo_template_stats('mom_daughter'))['paid_packages']==0
    await dispatch_callback(router,callback_event('photo_temp_mom_daughter'),context)
    stats=await credit_db.get_photo_template_stats('mom_daughter')
    assert stats['paid_packages']==1 and stats['revenue_kopecks']==47200
    await dispatch_callback(router,callback_event('photo_temp_mom_daughter'),context)
    assert (await credit_db.get_photo_template_stats('mom_daughter'))['paid_packages']==1


@pytest.mark.parametrize('owner,paid',[(456,False),(101,True)])
async def test_unpaid_or_foreign_customer_cannot_generate_mom_trial(credit_db,sales_payments,owner,paid):
    order=await credit_db.create_photo_order('photo_other_mom',owner,590)
    await credit_db.update_photo_order(order,style='photo_style_ny',template='mom_daughter')
    if paid:
        await deliver_webhook(sales_payments,await credit_db.get_photo_order(order))
    frames=AsyncMock()
    router=create_router(admin_user_id=999,payments=sales_payments,photosession_generator=frames)
    context=await ready_context()
    await context.update_data(photo_order_id=order,photo_style='photo_style_ny',photo_tariff='photo_tariff_590',template='mom_daughter')
    await context.set_state(PosterStates.waiting_for_photosession_photo)
    await call_handler(router,'collect_photosession_photo',photo_event(),context)
    frames.assert_not_awaited()


@pytest.mark.parametrize('style',['photo_style_studio','photo_style_walk'])
async def test_new_mom_template_does_not_start_other_photo_styles(credit_db,style):
    frames=AsyncMock();context=await ready_context()
    await context.update_data(photo_style=style,photo_tariff='photo_tariff_590')
    event=callback_event('photo_temp_mom_daughter')
    await dispatch_callback(create_router(admin_user_id=456,photosession_generator=frames),event,context)
    frames.assert_not_awaited()
    assert await context.get_state()!=PosterStates.waiting_for_photosession_photo


async def test_two_image_edit_keeps_client_identity_first_and_first_frame_as_style(config,storage,api,monkeypatch):
    monkeypatch.setattr(images.Config,'from_env',lambda:config)
    style_url='https://cdn.example/first-frame.png'
    buffer=BytesIO();Image.new('RGB',(2,2),'green').save(buffer,format='PNG');style_bytes=buffer.getvalue()
    api.get(PHOTO_URL,body=PNG,content_type='image/png')
    api.get(style_url,body=style_bytes,content_type='image/png')
    api.post(IMAGE_EDIT_URL,payload={'data':[{'b64_json':ENCODED_PNG}]})
    api.head(PUBLIC_URL,status=200,repeat=True)
    result=await images.generate_photosession_frame(PHOTO_URL,images.NY_MOM_DAUGHTER_PROMPTS[1],style_reference_url=style_url)
    assert PUBLIC_URL.fullmatch(result)
    request=post_requests(api)[0]
    body=await request.aread()
    message=BytesParser(policy=default).parsebytes(
        f"Content-Type: {request.headers['Content-Type']}\r\nMIME-Version: 1.0\r\n\r\n".encode()+body)
    parts=list(message.iter_parts())
    files=[p.get_payload(decode=True) for p in parts if p.get_filename()]
    assert files==[PNG,style_bytes]
    prompt=next(p.get_payload(decode=True).decode() for p in parts if p.get_param('name',header='content-disposition')=='prompt')
    assert 'FIRST image is the original client photo' in prompt and 'SECOND image is the first generated frame' in prompt
    assert 'EXACTLY TWO people' in prompt and 'ONLY the mother' not in prompt


async def test_invalid_style_url_fails_before_paid_generation(monkeypatch):
    backend=AsyncMock();monkeypatch.setattr(images,'_generate_and_store',backend)
    with pytest.raises(ValueError):
        await images.generate_photosession_frame(PHOTO_URL,'portrait',style_reference_url='http://invalid.example/style.jpg')
    backend.assert_not_awaited()


@pytest.mark.parametrize('entry',['start','bot_started'])
async def test_youtube_deep_link_entry_and_repeat_source(credit_db,entry):
    from test_handlers import bot_started_event
    router=create_router();context=await ready_context()
    event=message_event('/start youtube') if entry=='start' else bot_started_event('youtube')
    await call_handler(router,entry,event,context)
    assert (await credit_db.get_user(456))['first_start_source']=='youtube'
    await credit_db.record_first_start(456,'vk_posts')
    assert (await credit_db.get_user(456))['first_start_source']=='youtube'


async def test_mom_stats_and_youtube_link_exclude_admin(credit_db,sales_payments):
    for actor in (456,999):
        await credit_db.record_first_start(actor,'youtube')
        await credit_db.increment_counter('select_photo_mom_daughter',user_id=actor)
        key=await credit_db.create_photo_order(f'photo_mom_{actor}',actor,590)
        await credit_db.update_photo_order(key,template='mom_daughter')
        await deliver_webhook(sales_payments,await credit_db.get_photo_order(key),payment_id=f'pay-{actor}')
    event=message_event('/stats');event.get_ids=lambda:(123,999)
    await call_handler(create_router(admin_user_id=999),'stats',event)
    text=event.message.answer.await_args.args[0]
    assert '- Выбор сюжета (клики): 1' in text and '- Оплачено пакетов: 1' in text
    assert '- Выручка: 590,00 ₽' in text
    assert '- YouTube: 1 польз. / 1 покуп. / 590,00 ₽' in text
    assert 'https://max.ru/se14596751_1_bot?start=youtube' in text


@pytest.mark.parametrize('payload',['photo_temp_rabbit','photo_temp_fam3'])
async def test_started_mom_package_cannot_be_relabelled_by_old_other_template_buttons(credit_db,sales_payments,payload):
    key=await credit_db.create_photo_order('photo_mom_locked',456,590)
    await credit_db.update_photo_order(key,style='photo_style_ny',template='mom_daughter')
    await deliver_webhook(sales_payments,await credit_db.get_photo_order(key))
    await credit_db.save_photo_result(key,1,'https://cdn.example/mom-first.jpg')
    router=create_router(admin_user_id=999,payments=sales_payments)
    context=await ready_context()
    await context.update_data(photo_order_id=key,photo_style='photo_style_ny',photo_tariff='photo_tariff_590',template='mom_daughter')
    event=callback_event(payload);await dispatch_callback(router,event,context)
    assert 'Продолжите исходную фотосессию' in event.send.await_args.args[0]
    assert (await credit_db.get_photo_order(key))['template']=='mom_daughter'
    assert (await credit_db.get_photo_template_stats('mom_daughter'))['paid_packages']==1


@pytest.fixture(autouse=True)
def photosession_examples(example_photos):
    return example_photos

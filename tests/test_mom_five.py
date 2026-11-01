"""Five-frame NY mother/daughter standard package: price, quotas, costume groups and recovery."""
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
import handlers
import image_service as images
from handlers import create_router,PosterStates
from photo_orders import PaidPhotoWorker
from test_handlers import (callback_event,ready_context,dispatch_callback,call_handler,message_event,photo_event,
    event_replies,max_image_upload,isolated_example_directory,text_correction)
from test_sales_stats import sales_payments,deliver_webhook
from test_image_service import config,storage,api,PHOTO_URL,PNG,IMAGE_EDIT_URL,ENCODED_PNG,PUBLIC_URL,post_requests,multipart_fields


async def choose_five(router,context):
    await dispatch_callback(router,callback_event('photo_style_ny'),context)
    tariff=callback_event('photo_tariff_990');await dispatch_callback(router,tariff,context)
    choices=tariff.send.await_args.kwargs['attachments'][0].payload.buttons
    assert choices[0][0].payload=='photo_temp_mom_daughter_5'
    event=callback_event('photo_temp_mom_daughter_5');await dispatch_callback(router,event,context)
    return event


def assert_frame_calls(frames):
    calls=frames.await_args_list
    assert len(calls)==5
    assert [c.args[1] for c in calls]==list(images.NY_MOM_DAUGHTER_FIVE_PROMPTS)
    assert calls[0].kwargs=={'image_size':'1536x1024'}
    assert calls[1].kwargs=={}
    assert calls[2].kwargs=={'style_reference_url':'https://cdn.example/frame-1.jpg'}
    assert all(c.kwargs=={'style_reference_url':'https://cdn.example/frame-2.jpg'} for c in calls[3:])
    assert all(c.args[0]=='https://photos.example.com/source.png' for c in calls)


async def test_admin_generates_five_with_two_separate_costume_groups(credit_db):
    await credit_db.grant_free_trial(456)
    frames=AsyncMock(side_effect=[f'https://cdn.example/frame-{i}.jpg' for i in range(1,6)])
    payments=SimpleNamespace(create_payment_link=AsyncMock())
    router=create_router(admin_user_id=456,photosession_generator=frames,payments=payments)
    context=await ready_context();choice=await choose_five(router,context)
    assert 'Оплата пропущена' in choice.send.await_args.args[0]
    assert 'пять кадров' in choice.send.await_args.args[0]
    assert (await context.get_data())['photo_tariff']=='photo_tariff_990'
    event=photo_event();await call_handler(router,'collect_photosession_photo',event,context)
    assert_frame_calls(frames)
    assert sum('Генерирую кадр' in str(c) for c in event.message.answer.await_args_list)==5
    assert 'из 5' in str(event.message.answer.await_args_list)
    assert 'Все 5 фотографий отправлены' in event.message.answer.await_args.args[0]
    assert event.bot.send_message.await_count==5
    payments.create_payment_link.assert_not_awaited()
    assert (await credit_db.get_user(456))['balance']==1
    assert await credit_db.get_active_promo(456,'photo') is None


@pytest.mark.parametrize('promo,amount',[(False,99000),(True,79200)])
async def test_paid_five_package_links_webhook_five_slots_and_promo_after_last(credit_db,sales_payments,promo,amount):
    if promo: await credit_db.set_promo(456,'photo')
    frames=AsyncMock(side_effect=[f'https://cdn.example/frame-{i}.jpg' for i in range(1,6)])
    router=create_router(admin_user_id=999,photosession_generator=frames,payments=sales_payments)
    context=await ready_context();await choose_five(router,context)
    key=(await context.get_data())['photo_order_id'];order=await credit_db.get_photo_order(key)
    assert order['tariff']==990 and order['amount_kopecks']==amount and order['template']=='mom_daughter_5'
    frames.assert_not_awaited()
    assert (await deliver_webhook(sales_payments,order)).status==200
    assert (await deliver_webhook(sales_payments,order)).status==200
    await context.clear()
    resume=callback_event('photo_resume:'+key);await dispatch_callback(router,resume,context)
    assert '5 кадров' in resume.send.await_args.args[0]
    event=photo_event()
    async def send(**kwargs):
        if kwargs.get('text','').startswith('Фотография'):
            assert await credit_db.get_active_promo(456,'photo') is None
        return object()
    event.bot.send_message=AsyncMock(side_effect=send)
    await call_handler(router,'collect_photosession_photo',event,context)
    assert_frame_calls(frames)
    assert await credit_db.get_photo_package_balance(key)==0
    assert len(await credit_db.get_photo_results(key))==5
    stats=await credit_db.get_photo_template_stats('mom_daughter_5',admin_id=999)
    assert stats=={'paid_packages':1,'revenue_kopecks':amount,'generated_photos':5,'completed_packages':1}
    assert (await credit_db.get_all_counters())['buy_photo_990']==1
    assert await credit_db.get_active_promo(456,'photo') is not None
    await dispatch_callback(router,resume,context)
    assert frames.await_count==5
    with pytest.raises(ValueError): await credit_db.save_photo_result(key,6,'https://cdn.example/extra.jpg')


async def test_payment_before_plot_selection_has_working_notification_and_selector(credit_db,sales_payments):
    router=create_router(admin_user_id=999,payments=sales_payments);context=await ready_context()
    await dispatch_callback(router,callback_event('photo_style_ny'),context)
    await dispatch_callback(router,callback_event('photo_tariff_990'),context)
    key=(await context.get_data())['photo_order_id'];order=await credit_db.get_photo_order(key)
    assert order['template'] is None
    await deliver_webhook(sales_payments,order)
    bot=SimpleNamespace(send_message=AsyncMock(return_value=object()))
    await PaidPhotoWorker(bot,admin_user_id=999).process()
    notice=next(c.kwargs for c in bot.send_message.await_args_list if c.kwargs['user_id']==456)
    assert notice['attachments'][0].payload.buttons[0][0].payload=='photo_resume:'+key
    await context.clear();event=callback_event('photo_resume:'+key)
    await dispatch_callback(router,event,context)
    assert event.send.await_args.kwargs['attachments'][0].payload.buttons[0][0].payload=='photo_temp_mom_daughter_5'
    await dispatch_callback(router,callback_event('photo_temp_mom_daughter_5'),context)
    assert (await credit_db.get_photo_template_stats('mom_daughter_5'))['paid_packages']==1
    assert await context.get_state()==PosterStates.waiting_for_photosession_photo


async def test_fifth_frame_recovery_uses_saved_second_frame_as_daughter_outfit(credit_db,sales_payments):
    key=await credit_db.create_photo_order('photo_five_resume',456,990)
    await credit_db.update_photo_order(key,style='photo_style_ny',template='mom_daughter_5')
    await deliver_webhook(sales_payments,await credit_db.get_photo_order(key))
    frames=AsyncMock(side_effect=[*[f'https://cdn.example/frame-{i}.jpg' for i in range(1,5)],RuntimeError('fifth failed')])
    router=create_router(admin_user_id=999,payments=sales_payments,photosession_generator=frames)
    context=await ready_context()
    await dispatch_callback(router,callback_event('photo_resume:'+key),context)
    await call_handler(router,'collect_photosession_photo',photo_event(),context)
    assert len(await credit_db.get_photo_results(key))==4
    assert await credit_db.get_photo_package_balance(key)==1
    assert await credit_db.get_active_promo(456,'photo') is None
    await context.clear();frames.side_effect=None;frames.return_value='https://cdn.example/frame-5.jpg'
    await dispatch_callback(router,callback_event('photo_resume:'+key),context)
    assert frames.await_count==6 and frames.await_args.kwargs=={'style_reference_url':'https://cdn.example/frame-2.jpg'}
    assert (await credit_db.get_photo_template_stats('mom_daughter_5'))['completed_packages']==1


@pytest.mark.parametrize('owner,tariff,paid',[(456,990,False),(101,990,True),(456,590,True)])
async def test_five_requires_owned_paid_990_package(credit_db,sales_payments,owner,tariff,paid):
    key=await credit_db.create_photo_order('photo_five_access',owner,tariff)
    await credit_db.update_photo_order(key,style='photo_style_ny',template='mom_daughter_5')
    if paid: await deliver_webhook(sales_payments,await credit_db.get_photo_order(key))
    frames=AsyncMock();router=create_router(admin_user_id=999,payments=sales_payments,photosession_generator=frames)
    context=await ready_context()
    await context.update_data(photo_order_id=key,photo_style='photo_style_ny',photo_tariff='photo_tariff_990',template='mom_daughter_5')
    await context.set_state(PosterStates.waiting_for_photosession_photo)
    await call_handler(router,'collect_photosession_photo',photo_event(),context)
    frames.assert_not_awaited()


async def test_new_invoice_drops_previous_package_fsm_images(credit_db,sales_payments):
    router=create_router(admin_user_id=999,payments=sales_payments);context=await ready_context()
    await context.update_data(photo_promo_flow=True,photo_style='photo_style_ny',photo_tariff='photo_tariff_590',
        template='rabbit',photosession_urls=['https://cdn.example/old.jpg']*3,photosession_complete=True,photosession_sent_count=3)
    await dispatch_callback(router,callback_event('photo_tariff_990'),context)
    data=await context.get_data()
    assert 'photosession_urls' not in data and 'photosession_complete' not in data and 'photo' not in data
    assert (await credit_db.get_photo_order(data['photo_order_id']))['template'] is None


async def test_590_callback_and_other_styles_cannot_start_five(credit_db):
    frames=AsyncMock();router=create_router(admin_user_id=456,photosession_generator=frames)
    for style,tariff in [('photo_style_ny','photo_tariff_590'),('photo_style_studio','photo_tariff_990')]:
        context=await ready_context();await context.update_data(photo_style=style,photo_tariff=tariff)
        await dispatch_callback(router,callback_event('photo_temp_mom_daughter_5'),context)
        assert await context.get_state()!=PosterStates.waiting_for_photosession_photo
    frames.assert_not_awaited()


async def test_990_mom_stats_exclude_admin_and_do_not_merge_with_590(credit_db,sales_payments):
    for user,tariff,template in [(456,990,'mom_daughter_5'),(999,990,'mom_daughter_5'),(456,590,'mom_daughter')]:
        await credit_db.increment_counter('select_photo_'+template,user_id=user)
        key=await credit_db.create_photo_order(f'photo_stats_{user}_{tariff}',user,tariff)
        await credit_db.update_photo_order(key,style='photo_style_ny',template=template)
        await deliver_webhook(sales_payments,await credit_db.get_photo_order(key),payment_id=f'p-{user}-{tariff}')
    event=message_event('/stats');event.get_ids=lambda:(123,999)
    await call_handler(create_router(admin_user_id=999),'stats',event)
    block=event.message.answer.await_args.args[0].split('👩‍👧 Мама + дочка — 990 ₽ / 5 фото:',1)[1].split('👑 Эксклюзив',1)[0]
    assert '- Оплачено пакетов: 1' in block and '- Выбор сюжета (клики): 1' in block and '- Выручка: 990,00 ₽' in block


async def test_first_frame_real_sdk_request_is_landscape(config,storage,api,monkeypatch):
    monkeypatch.setattr(images.Config,'from_env',lambda:config)
    api.get(PHOTO_URL,body=PNG,content_type='image/png')
    api.post(IMAGE_EDIT_URL,payload={'data':[{'b64_json':ENCODED_PNG}]})
    api.head(PUBLIC_URL,status=200,repeat=True)
    result=await images.generate_photosession_frame(PHOTO_URL,images.NY_MOM_DAUGHTER_FIVE_PROMPTS[0],image_size='1536x1024')
    assert PUBLIC_URL.fullmatch(result)
    fields=await multipart_fields(post_requests(api)[0])
    assert fields['size'].get_content()=='1536x1024'
    assert fields['image'].get_payload(decode=True)==PNG
    assert 'look directly at the CAMERA and smile' in fields['prompt'].get_content()


async def test_invalid_size_does_not_call_paid_pipeline(monkeypatch):
    backend=AsyncMock();monkeypatch.setattr(images,'_generate_and_store',backend)
    with pytest.raises(ValueError): await images.generate_photosession_frame(PHOTO_URL,'photo',image_size='invalid')
    backend.assert_not_awaited()

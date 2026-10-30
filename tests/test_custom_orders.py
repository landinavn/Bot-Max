"""Exclusive-order questionnaire and retirement of the public long cartoon funnel."""
import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from maxapi.context import MemoryContext
from maxapi.types import PhotoAttachmentPayload

import handlers
from handlers import create_router, PosterStates
from test_handlers import (callback_event, message_event, ready_context, dispatch_callback,
                           call_handler, event_replies, isolated_example_directory,
                           max_image_upload, text_correction)


def photos_event(count=1):
    event = message_event(None)
    event.message.body.attachments = [SimpleNamespace(type='image', payload=PhotoAttachmentPayload(
        photo_id=i, token=f'material-{i}', url=f'https://cdn.example/photo-{i}.jpg')) for i in range(count)]
    return event


async def complete_form(router, context, count=1):
    await dispatch_callback(router, callback_event('custom_start_form'), context)
    for text in ('Юбилей', '@client_max', 'Ирина', 'Поздравление от всей семьи'):
        await call_handler(router, 'echo', message_event(text), context)
    await call_handler(router, 'echo', photos_event(count), context)
    assert await context.get_state() == PosterStates.waiting_for_custom_deadline


def buttons(event):
    return [(b.text, b.payload) for row in event.send.await_args.kwargs['attachments'][0].payload.buttons for b in row]


async def test_custom_order_menu_position_and_retired_button(credit_db):
    entries = [(b.text,b.payload) for row in handlers.main_keyboard().payload.buttons for b in row]
    assert entries[1:4] == [('🎬 Мульт-открытка — Новинка 🔥','cat_cartoon'),
        ('👑 Эксклюзив (ручная работа) — от 2 000 ₽','custom_order'),('📸 Нейрофотосессия','cat_photo')]
    assert 'cat_cartoon_long' not in [payload for _,payload in entries]
    context = await ready_context()
    event = callback_event('custom_order')
    await dispatch_callback(create_router(admin_user_id=999),event,context)
    assert event.send.await_args.args[0] == handlers.CUSTOM_ORDER_TEXT
    assert buttons(event)[0] == ('📝 Заполнить заявку','custom_start_form')
    assert (await credit_db.get_all_counters())['click_custom_order'] == 1
    assert await context.get_state() is None


@pytest.mark.parametrize('user_id',[456,999])
@pytest.mark.parametrize('payload',['cat_cartoon_long','promo_cartoon_tariff:long'])
async def test_old_long_buttons_open_manual_order_without_payment(credit_db,user_id,payload):
    payments = SimpleNamespace(create_payment_link=AsyncMock())
    context=await ready_context()
    await context.update_data(cartoon_flow='long',cartoon_theme='theme_ny',cartoon_promo_choose_tariff=True)
    await context.set_state(PosterStates.waiting_for_cartoon_style)
    event=callback_event(payload);event.get_ids=lambda:(123,user_id)
    await dispatch_callback(create_router(admin_user_id=999,payments=payments),event,context)
    assert event.send.await_args.args[0]==handlers.CUSTOM_ORDER_TEXT
    assert await context.get_data()=={}
    payments.create_payment_link.assert_not_awaited()


@pytest.mark.parametrize('deadline',list(handlers.CUSTOM_DEADLINES))
async def test_six_steps_submit_photo_and_contact_to_admin_once(credit_db,deadline):
    router=create_router(admin_user_id=999);context=await ready_context()
    await complete_form(router,context,2)
    data=await context.get_data()
    assert data['custom_contact']=='@client_max' and data['custom_names']=='Ирина'
    event=callback_event(deadline)
    await dispatch_callback(router,event,context)
    event.ack.assert_awaited_once()
    sent=event.bot.send_message.await_args.kwargs
    assert sent['user_id']==999
    for value in ('456','@client_max','Юбилей','Ирина','Поздравление от всей семьи',handlers.CUSTOM_DEADLINES[deadline]):
        assert value in sent['text']
    assert [a.payload.token for a in sent['attachments']]==['material-0','material-1']
    assert all(a.type.value=='image' for a in sent['attachments'])
    assert '✅ Спасибо! Ваша заявка принята.' in event.send.await_args.args[0]
    assert await context.get_data()=={} and await context.get_state() is None
    assert (await credit_db.get_all_counters())['submit_custom_order']==1
    await dispatch_callback(router,event,context)
    assert event.bot.send_message.await_count==1
    assert (await credit_db.get_all_counters())['submit_custom_order']==1


@pytest.mark.parametrize('failure',['exception','unconfirmed'])
async def test_failed_admin_delivery_keeps_answers_for_retry(credit_db,failure):
    router=create_router(admin_user_id=999);context=await ready_context()
    await complete_form(router,context)
    event=callback_event('custom_deadline_1')
    event.bot.send_message=AsyncMock(side_effect=RuntimeError('MAX unavailable')) if failure=='exception' else AsyncMock(return_value=None)
    await dispatch_callback(router,event,context)
    assert 'Ваши ответы сохранены' in event.send.await_args.args[0]
    assert await context.get_state()==PosterStates.waiting_for_custom_deadline
    assert (await context.get_data())['custom_materials'][0]['token']=='material-0'
    assert (await credit_db.get_all_counters()).get('submit_custom_order',0)==0
    event.bot.send_message=AsyncMock(return_value=object())
    await dispatch_callback(router,event,context)
    assert (await credit_db.get_all_counters())['submit_custom_order']==1


async def test_partial_material_delivery_resumes_without_duplicate_admin_summary(credit_db):
    router=create_router(admin_user_id=999);context=await ready_context()
    await complete_form(router,context,4)
    event=callback_event('custom_deadline_7')
    event.bot.send_message=AsyncMock(side_effect=[object(),RuntimeError('second batch failed')])
    await dispatch_callback(router,event,context)
    assert (await context.get_data())['custom_delivery_index']==1
    event.bot.send_message=AsyncMock(return_value=object())
    await dispatch_callback(router,event,context)
    event.bot.send_message.assert_awaited_once()
    assert [x.payload.token for x in event.bot.send_message.await_args.kwargs['attachments']]==['material-3']
    assert 'Материалы к заявке' in event.bot.send_message.await_args.kwargs['text']


async def test_duplicate_deadline_clicks_are_serialized(credit_db):
    router=create_router(admin_user_id=999);context=await ready_context()
    await complete_form(router,context)
    event=callback_event('custom_deadline_3')
    await asyncio.gather(dispatch_callback(router,event,context),dispatch_callback(router,event,context))
    event.bot.send_message.assert_awaited_once()
    assert (await credit_db.get_all_counters())['submit_custom_order']==1


async def test_text_and_material_validation_and_bot_messages(credit_db):
    router=create_router(admin_user_id=999);context=await ready_context()
    await dispatch_callback(router,callback_event('custom_start_form'),context)
    for invalid in ('',' ', 'x'*301):
        await call_handler(router,'echo',message_event(invalid),context)
        assert await context.get_state()==PosterStates.waiting_for_custom_occasion
    event=message_event('ignored');event.message.sender.is_bot=True
    await call_handler(router,'collect_custom_form',event,context)
    assert 'custom_occasion' not in await context.get_data()
    await complete_form(router,context)
    await context.set_state(PosterStates.waiting_for_custom_materials)
    event=message_event('not a photo')
    await call_handler(router,'echo',event,context)
    assert 'Отправьте фото как изображение' in event.message.answer.await_args.args[0]
    assert await context.get_state()==PosterStates.waiting_for_custom_materials


async def test_unconfigured_admin_does_not_collect_contacts(credit_db):
    context=MemoryContext(chat_id=123,user_id=456);event=callback_event('custom_start_form')
    await dispatch_callback(create_router(),event,context)
    assert 'временно недоступен' in event.send.await_args.args[0]
    assert await context.get_data()=={} and await context.get_state() is None


@pytest.mark.parametrize('payload',['custom_order','custom_start_form'])
async def test_custom_navigation_does_not_cancel_running_generation(credit_db,payload):
    context=await ready_context();await context.set_state(PosterStates.cartoon_generating)
    await context.update_data(chosen_avatar='keep')
    event=callback_event(payload)
    await dispatch_callback(create_router(admin_user_id=999),event,context)
    assert await context.get_state()==PosterStates.cartoon_generating
    assert (await context.get_data())['chosen_avatar']=='keep'


async def test_stats_custom_counters_exclude_admin(credit_db):
    for actor in (456,999):
        await credit_db.increment_counter('click_custom_order',user_id=actor)
        await credit_db.increment_counter('submit_custom_order',user_id=actor)
    event=message_event('/stats');event.get_ids=lambda:(123,999)
    await call_handler(create_router(admin_user_id=999),'stats',event)
    text=event.message.answer.await_args.args[0]
    assert '👑 Эксклюзив (ручная работа):\n- Клики по кнопке: 1\n- Отправлено заявок: 1' in text
    assert text.index('👑 Эксклюзив')<text.index('🛠 Админ')


async def test_real_dispatcher_routes_form_photos_cancel_and_delivery(credit_db,monkeypatch,event_replies):
    from maxapi import Dispatcher
    from maxapi.context.isolation import SimpleEventIsolation
    from maxapi.types import MessageCallback, MessageCreated, User
    monkeypatch.setattr(handlers,'_reply',event_replies)
    bot=SimpleNamespace(me=User(user_id=888,first_name='Bot',is_bot=True),
                        send_message=AsyncMock(return_value=object()),send_callback=AsyncMock())
    dp=Dispatcher(storage=MemoryContext,event_isolation=SimpleEventIsolation())
    dp.include_routers(create_router(admin_user_id=999))
    user={'user_id':456,'first_name':'Client','is_bot':False}
    def message(text=None,attachments=None,sender=None):
        return {'sender':sender or user,'recipient':{'chat_id':123,'chat_type':'dialog'},'timestamp':1,
                'body':{'mid':'custom-form','seq':1,'text':text,'attachments':attachments}}
    async def click(payload):
        await dp.handle(MessageCallback(timestamp=1,bot=bot,message=message(sender=bot.me.model_dump()),
            callback={'timestamp':1,'callback_id':'custom','payload':payload,'user':user}))
    async def say(text=None,attachments=None):
        await dp.handle(MessageCreated(timestamp=1,bot=bot,message=message(text,attachments)))
    try:
        await click('custom_start_form')
        await say('Юбилей')
        context=dp.fsm.get_context(chat_id=123,user_id=456)
        assert await context.get_state()==PosterStates.waiting_for_custom_contact
        await say('/cancel')
        assert await context.get_data()=={}
        await click('custom_start_form')
        for answer in ('Реклама','@contact','Компания','Реклама товара'):
            await say(answer)
        await say(attachments=[{'type':'image','payload':{'photo_id':1,'token':'logo-token','url':'https://cdn.example/logo.jpg'}}])
        assert await context.get_state()==PosterStates.waiting_for_custom_deadline
        await click('custom_deadline_7')
        notifications=[c.kwargs for c in bot.send_message.await_args_list if c.kwargs.get('user_id')==999]
        assert len(notifications)==1
        assert 'Контакт: @contact' in notifications[0]['text']
        assert notifications[0]['attachments'][0].payload.token=='logo-token'
        assert await context.get_data()=={} and await context.get_state() is None
    finally:
        await dp.shutdown()


async def test_unpaid_legacy_long_state_cannot_create_a_new_invoice(credit_db):
    payments=SimpleNamespace(create_payment_link=AsyncMock())
    router=create_router(admin_user_id=999,payments=payments)
    context=await ready_context()
    await context.update_data(cartoon_flow='long',cartoon_amount_kopecks=129000)
    await context.set_state(PosterStates.waiting_for_cartoon_style)
    event=callback_event('theme_ny')
    await dispatch_callback(router,event,context)
    assert event.send.await_args.args[0]==handlers.CUSTOM_ORDER_TEXT
    payments.create_payment_link.assert_not_awaited()

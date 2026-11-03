"""MAX receives one keyboard; OpenAI moderation never consumes a generation credit."""
from unittest.mock import AsyncMock

import pytest
from maxapi.types import AttachmentUpload,CallbackButton,LinkButton
from maxapi.utils.inline_keyboard import InlineKeyboardBuilder

import handlers
from handlers import create_router,PosterStates
from image_service import ImageModerationError
from test_handlers import (
    message_event,callback_event,ready_context,call_handler,event_replies,
    text_correction,max_image_upload,isolated_example_directory,
)


def keyboard_count(attachments):
    return sum(getattr(a.type,'value',a.type)=='inline_keyboard' for a in attachments)


@pytest.mark.parametrize('callback',[False,True])
@pytest.mark.parametrize('as_dict',[False,True])
async def test_reply_merges_keyboards_without_losing_media_or_buttons(credit_db,monkeypatch,event_replies,callback,as_dict):
    monkeypatch.setattr(handlers,'_reply',event_replies)
    image=AttachmentUpload(type='image',payload={'token':'generated-image'})
    video=AttachmentUpload(type='video',payload={'token':'example-video'})
    first=InlineKeyboardBuilder().row(LinkButton(text='Скачать',url='https://cdn.example/image.png')).as_markup()
    second=InlineKeyboardBuilder().row(CallbackButton(text='Ещё',payload='more')).as_markup()
    third=handlers.navigation_keyboard((('Главное меню','open_main_menu'),))
    attachments=[image,first,video,second,third]
    if as_dict:
        attachments=[a.model_dump(mode='json') if a.type=='inline_keyboard' else a for a in attachments]
    incoming=callback_event('show_help') if callback else message_event('Привет')
    await handlers._reply(incoming,'Готово',attachments=attachments)
    sent=incoming.bot.send_message.await_args.kwargs['attachments']
    assert keyboard_count(sent)==1
    assert sent[0] is image and sent[2] is video
    rows=sent[1].payload.buttons
    assert [[b.text for b in row] for row in rows]==[['Скачать'],['Ещё'],['Главное меню']]
    assert rows[0][0].url=='https://cdn.example/image.png'
    assert rows[1][0].payload=='more' and rows[2][0].payload=='open_main_menu'
    assert len(attachments)==5  # Caller-owned attachments and keyboard rows were not mutated.
    assert len(first.payload.buttons)==1


async def test_reply_preserves_single_keyboard_and_plain_messages(credit_db,monkeypatch,event_replies):
    monkeypatch.setattr(handlers,'_reply',event_replies)
    incoming=message_event('Привет')
    keyboard=handlers.result_keyboard('https://cdn.example/image.png')
    await handlers._reply(incoming,'Готово',attachments=[keyboard])
    assert incoming.bot.send_message.await_args.kwargs['attachments']==[keyboard]
    await handlers._reply(incoming,'Обычный текст')
    assert 'attachments' not in incoming.bot.send_message.await_args.kwargs


async def test_zero_credit_postcard_sends_purchase_and_retry_as_one_keyboard(credit_db,monkeypatch,event_replies):
    monkeypatch.setattr(handlers,'_reply',event_replies)
    generator=AsyncMock()
    incoming=message_event('С Новым годом!')
    async def strict_max(**kwargs):
        assert keyboard_count(kwargs.get('attachments',[]))<=1
        return object()
    incoming.bot.send_message.side_effect=strict_max
    context=await ready_context();await context.update_data(mode='mode_ny')
    await call_handler(create_router(postcard_generator=generator),'collect_card_text',incoming,context)
    sent=incoming.bot.send_message.await_args.kwargs
    assert sent['text']==handlers.NO_CREDITS_TEXT
    assert len(sent['attachments'])==1
    payloads=[b.payload for row in sent['attachments'][0].payload.buttons for b in row if hasattr(b,'payload')]
    assert 'buy_1' in payloads and 'postcard_retry' in payloads
    generator.assert_not_awaited()


@pytest.mark.parametrize('mode',['mode_vision','mode_ny'])
async def test_generated_image_is_sent_with_one_keyboard(credit_db,monkeypatch,event_replies,mode):
    monkeypatch.setattr(handlers,'_reply',event_replies)
    await credit_db.add_credits(456,2)
    model=AsyncMock(return_value=['https://cdn.example/generated.png'])
    context=await ready_context();await context.update_data(mode=mode)
    incoming=message_event('Счастья и радости!')
    async def strict_max(**kwargs):
        assert keyboard_count(kwargs.get('attachments',[]))<=1
        return object()
    incoming.bot.send_message.side_effect=strict_max
    router=create_router(model,postcard_generator=model)
    await call_handler(router,'collect_sphere' if mode=='mode_vision' else 'collect_card_text',incoming,context)
    images=[c.kwargs for c in incoming.bot.send_message.await_args_list if any(
        getattr(a.type,'value',a.type)=='image' for a in c.kwargs.get('attachments',[]))]
    assert len(images)==1
    assert [getattr(a.type,'value',a.type) for a in images[0]['attachments']]==['image','inline_keyboard']
    buttons=images[0]['attachments'][1].payload.buttons[0]
    assert [b.text for b in buttons]==['Скачать','Ещё']
    assert (await credit_db.get_user(456))['balance']==1


@pytest.mark.parametrize('mode',['mode_vision','mode_ny','mode_teacher','mode_coach'])
@pytest.mark.parametrize('admin',[False,True])
async def test_moderation_rejection_keeps_balance_and_requests_new_photo(credit_db,mode,admin):
    await credit_db.add_credits(456,2)
    before=await credit_db.get_user(456)
    model=AsyncMock(side_effect=ImageModerationError('moderation_blocked'))
    router=create_router(model,postcard_generator=model,admin_user_id=456 if admin else 999)
    context=await ready_context();await context.update_data(mode=mode)
    if mode in handlers.ROLE_CARD_FIELDS:
        await context.update_data(**{handlers.ROLE_CARD_FIELDS[mode]:'Имя, с праздником!'})
    incoming=message_event('Счастья и радости!')
    await call_handler(router,'collect_sphere' if mode=='mode_vision' else 'collect_card_text',incoming,context)
    model.assert_awaited_once()
    after=await credit_db.get_user(456)
    assert after['balance']==before['balance']
    assert after['total_generations']==before['total_generations']
    assert incoming.message.answer.await_args.args[0]==(
        '❌ К сожалению, ваше фото или описание не прошли проверку безопасности OpenAI. '
        'Пожалуйста, загрузите другое фото (без откровенной одежды) и измените текст. Кредит не списан!'
    )
    assert 'attachments' not in incoming.message.answer.await_args.kwargs
    assert await context.get_state()==PosterStates.waiting_for_photo
    data=await context.get_data()
    assert data['mode']==mode and data['photo'] is None and data['card_text'] is None
    assert not data['variants'] and not data['new_batch_requested']
    assert not (await credit_db.get_all_counters()).get(handlers.GENERATION_EVENTS[mode],0)
    handlers.send_offline_conversion.assert_not_awaited()
    handlers.upload_image_for_max.assert_not_awaited()


@pytest.mark.parametrize('mode',['mode_vision','mode_ny'])
async def test_new_photo_after_moderation_uses_only_one_credit(credit_db,mode):
    from test_handlers import photo_event
    await credit_db.grant_free_trial(456)
    model=AsyncMock(side_effect=[ImageModerationError('safety_violations'),['https://cdn.example/accepted.png']])
    router=create_router(model,postcard_generator=model)
    context=await ready_context();await context.update_data(mode=mode)
    entry='collect_sphere' if mode=='mode_vision' else 'collect_card_text'
    await call_handler(router,entry,message_event('Первый текст'),context)
    assert (await credit_db.get_user(456))['balance']==1
    assert await context.get_state()==PosterStates.waiting_for_photo
    await call_handler(router,'collect_photo',photo_event(),context)
    await call_handler(router,entry,message_event('Новый текст'),context)
    user=await credit_db.get_user(456)
    assert user['balance']==0 and user['total_generations']==1
    assert model.await_count==2


async def test_repeated_zero_balance_retries_never_deduct_or_generate(credit_db,monkeypatch,event_replies):
    monkeypatch.setattr(handlers,'_reply',event_replies)
    await credit_db.grant_free_trial(456)
    assert await credit_db.deduct_credit(456,count_generation=True,generation_event='gen_ny')
    before=await credit_db.get_user(456)
    model=AsyncMock()
    router=create_router(postcard_generator=model)
    context=await ready_context();await context.update_data(mode='mode_ny')
    first=message_event('С праздником!')
    await call_handler(router,'collect_card_text',first,context)
    for _ in range(12):
        retry=callback_event('postcard_retry')
        await call_handler(router,'retry_postcard',retry,context)
        sent=retry.bot.send_message.await_args.kwargs
        assert sent['text']==handlers.NO_CREDITS_TEXT
        assert keyboard_count(sent['attachments'])==1
    after=await credit_db.get_user(456)
    assert after['balance']==before['balance']==0
    assert after['total_generations']==before['total_generations']==1
    model.assert_not_awaited()

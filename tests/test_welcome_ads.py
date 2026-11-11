"""New welcome, ad attribution, and campaign navigation without external requests."""
from unittest.mock import AsyncMock
import asyncio

import aiosqlite
import pytest
from maxapi.enums.update import UpdateType

import handlers
from handlers import create_router, PosterStates
from test_handlers import (message_event, callback_event, bot_started_event, ready_context,
    call_handler, dispatch_callback, isolated_example_directory, event_replies)


EXPECTED_BUTTONS = [
    ('🔮 Карты желаний','cat_vision'), ('🎁 Открытки','cat_cards'),
    ('🎵 Музыкальная открытка','cat_video'), ('🎬 Мульт-открытка','cat_cartoon'),
    ('✨ Оживление фотографий','cat_animate'), ('🎅 Поздравление Деда Мороза','cat_ded_moroz'),
    ('👑 Ручная работа','custom_order'), ('🎵 Аудиопесни','cat_audio'),
    ('📸 Нейрофотосессии','cat_photo'),
]


@pytest.fixture
def welcome_image(isolated_example_directory):
    p=handlers.EXAMPLES_DIR/handlers.MAIN_SCREEN_IMAGE
    p.write_bytes(b'fixture image')
    return p


def test_exact_main_buttons_and_welcome_copy():
    assert [(b.text,b.payload) for row in handlers.main_keyboard().payload.buttons for b in row]==EXPECTED_BUTTONS
    assert handlers.WELCOME_TEXT==(
        'Привет! Я создаю волшебные открытки и карты желаний с твоим лицом или лицом твоего близкого человека.\n\n'
        '✍️ На открытках ты можешь добавить любое пожелание и поздравление.\n\n'
        '🎨 Первая открытка с вашим фото — бесплатно!\n\n'
        '👇 Выбери, что хочешь создать:')


@pytest.mark.parametrize('entry',['start','bot_started','back_to_main','open_promo_menu'])
async def test_welcome_uses_one_named_image(credit_db,welcome_image,entry,monkeypatch,event_replies):
    router=create_router();context=await ready_context()
    if entry=='start':
        e=message_event('/start');await call_handler(router,entry,e,context);replies=e.message.answer
    elif entry=='bot_started':
        monkeypatch.setattr(handlers,'_reply',event_replies)
        e=bot_started_event();handler=next(h for h in router.event_handlers if h.update_type==UpdateType.BOT_STARTED)
        await handler.func_event(e,context);replies=e.bot.send_message
    else:
        e=callback_event(entry);await dispatch_callback(router,e,context);replies=e.send
    e.bot.upload_media.assert_awaited_once()
    media=e.bot.upload_media.await_args.args[0]
    assert media.path==str(welcome_image) and media.type.value=='image'
    assert len(replies.await_args_list[0].kwargs['attachments'])==1
    assert replies.await_args_list[1].kwargs['text']==handlers.WELCOME_TEXT
    assert (await credit_db.get_user(456))['balance']==1


@pytest.mark.parametrize('failure',['missing','upload','empty_token','send'])
async def test_welcome_failure_keeps_menu_and_credit(credit_db,welcome_image,failure,caplog):
    e=message_event('/start')
    if failure=='missing':welcome_image.unlink()
    elif failure=='upload':e.bot.upload_media.side_effect=RuntimeError('upload')
    elif failure=='empty_token':
        from maxapi.types import AttachmentUpload
        e.bot.upload_media.return_value=AttachmentUpload(type='image',payload={'token':''})
    else:e.message.answer.side_effect=[RuntimeError('send'),object(),object()]
    await call_handler(create_router(),'start',e,await ready_context())
    greetings=[c for c in e.message.answer.await_args_list if c.kwargs.get('text')==handlers.WELCOME_TEXT]
    assert len(greetings)==1 and len(greetings[0].kwargs['attachments'])==1
    assert (await credit_db.get_user(456))['balance']==1
    assert 'NY_card.png' in caplog.text


async def test_promo_click_count_and_legacy_navigation_do_not_duplicate_trial(credit_db,welcome_image):
    await credit_db.grant_free_trial(456);await credit_db.deduct_credit(456)
    router=create_router();context=await ready_context()
    for payload in ('open_promo_menu','back_to_main','open_main_menu','open_promo_menu'):
        await dispatch_callback(router,callback_event(payload),context)
    assert (await credit_db.get_all_counters())['promo_1111_click']==2
    assert (await credit_db.get_user(456))['balance']==0


async def test_ded_moroz_placeholder_and_back(credit_db,welcome_image):
    e=callback_event('cat_ded_moroz');router=create_router();context=await ready_context()
    await dispatch_callback(router,e,context)
    e.ack.assert_awaited_once()
    assert e.send.await_args.args[0]==handlers.DED_MOROZ_TEXT
    button=e.send.await_args.kwargs['attachments'][0].payload.buttons[0][0]
    assert (button.text,button.payload)==('⬅️ Назад','back_to_main')
    assert (await credit_db.get_all_counters())['click_ded_moroz']==1
    await dispatch_callback(router,callback_event(button.payload),context)
    assert await context.get_state()==PosterStates.waiting_for_mode


@pytest.mark.parametrize('payload',['cat_animation','cat_animate'])
async def test_animation_old_and_new_buttons(credit_db,payload):
    e=callback_event(payload)
    await dispatch_callback(create_router(),e,await ready_context())
    assert any('Оживление фотографий' in str(c) for c in e.send.await_args_list)
    assert (await credit_db.get_all_counters())['click_animation']==1


@pytest.mark.parametrize('entry',['start','bot_started'])
@pytest.mark.parametrize('tag',list(handlers.AD_SOURCE_LABELS)+['vk_extra','ya_extra'])
async def test_new_user_exact_ad_tag_saved_through_both_entries(credit_db,entry,tag):
    router=create_router();context=await ready_context()
    # Activity middleware may insert the user before the /start handler runs.
    await credit_db.update_user_activity(456)
    if entry=='start':await call_handler(router,'start',message_event('/start '+tag),context)
    else:
        handler=next(h for h in router.event_handlers if h.update_type==UpdateType.BOT_STARTED)
        await handler.func_event(bot_started_event(tag),context)
    assert (await credit_db.get_user(456))['source']==tag
    await credit_db.record_first_start(456,'ya_changed')
    await credit_db.init_db()
    assert (await credit_db.get_user(456))['source']==tag


@pytest.mark.parametrize('initial',[None,'share_hello','unmarked','vk_ads','vk_music'])
async def test_later_ad_click_never_rewrites_first_attribution(credit_db,initial):
    await credit_db.record_first_start(101,initial)
    before=dict(await credit_db.get_user(101))
    assert not await credit_db.record_first_start(101,'ya_cartoon')
    after=dict(await credit_db.get_user(101))
    assert before==after


async def test_ad_source_migration_does_not_invent_historical_tags(credit_db):
    await credit_db.record_first_start(101,'vk_music')
    async with aiosqlite.connect(credit_db.DATABASE_PATH) as db:
        await db.execute('ALTER TABLE users DROP COLUMN source');await db.commit()
    await credit_db.init_db();await credit_db.init_db()
    assert (await credit_db.get_user(101))['source'] is None
    assert not await credit_db.record_first_start(101,'ya_cartoon')
    assert await credit_db.get_ad_source_counts()=={}


async def test_concurrent_first_starts_and_query_counts(credit_db):
    r=await asyncio.gather(*(credit_db.record_first_start(101,t) for t in ('vk_music','ya_music')*5))
    assert sum(r)==1
    tag=(await credit_db.get_user(101))['source']
    await credit_db.record_first_start(456,tag)
    assert await credit_db.get_ad_source_counts()=={tag:2}
    assert await credit_db.get_ad_source_counts(admin_id=456)=={tag:1}


async def test_stats_shows_eight_labels_and_clicks(credit_db):
    for uid,tag in enumerate(handlers.AD_SOURCE_LABELS,101):await credit_db.record_first_start(uid,tag)
    await credit_db.record_first_start(456,'vk_music')
    await credit_db.increment_counter('promo_1111_click',user_id=101)
    await credit_db.increment_counter('click_ded_moroz',user_id=101)
    e=message_event('/stats')
    await call_handler(create_router(admin_user_id=456),'stats',e)
    text='\n'.join(c.args[0] for c in e.message.answer.await_args_list)
    assert '🔥 Клики по кнопке акции 11.11: 1' in text
    assert '📍 Рекламные метки:' in text and '- Дед Мороз: 1' in text
    for label in handlers.AD_SOURCE_LABELS.values():assert '- '+label+': 1' in text
    assert len(text)<4000

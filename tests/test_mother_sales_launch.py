import asyncio
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock
from urllib.parse import urlsplit,parse_qsl

import pytest
from maxapi.types import AttachmentUpload
from maxapi.exceptions import MaxApiError

import database
import handlers
import mother_broadcast as campaign
import mothers_day
import song_review
from prodamus import parse_form_fields
from test_prodamus import payments,payment_config
from test_handlers import message_event,callback_event,call_handler,dispatch_callback,ready_context,event_replies


@pytest.fixture
def media(tmp_path,monkeypatch):
    example=tmp_path/'den_mama.mp4';example.write_bytes(b'video')
    monkeypatch.setattr(campaign,'EXAMPLE',example)
    monkeypatch.setattr(campaign.asyncio,'sleep',AsyncMock())
    def event(text='/broadcast_mother'):
        e=message_event(text)
        e.bot.upload_media=AsyncMock(return_value=AttachmentUpload(type='video',payload={'token':'mother-video'}))
        return e
    return event


@pytest.mark.parametrize('tariff,theme,price',[('mother','theme_mother',590),('short','theme_ny',390),('long','theme_ny',1290)])
async def test_current_prices_create_signed_links_and_validate_payments(credit_db,payments,tariff,theme,price):
    key=await database.create_cartoon_order('cartoon_price_'+tariff,456,theme,tariff)
    order=await database.get_cartoon_order(key)
    assert order['amount_kopecks']==price*100
    link=await payments.create_payment_link(456,order_id=key,amount=price,description=order['product_name'])
    fields=parse_form_fields(parse_qsl(urlsplit(link).query,keep_blank_values=True))
    assert fields['products'][0]['price']==str(price)+'.00'
    data={'order_num':key,'order_id':'new-'+tariff,'sum':str(price),'products':fields['products'],
          'customer_extra':fields['customer_extra'],'_param_package_id':fields['_param_package_id']}
    validated,pid=await payments.validate_payment(data)
    assert validated['tariff']==tariff
    await database.mark_cartoon_paid(key,pid)
    counters=await database.get_all_counters()
    expected={'mother':'buy_cartoon_mother_590','short':'buy_cartoon_390','long':'buy_cartoon_1290'}[tariff]
    assert counters[expected]==1


@pytest.mark.parametrize('tariff,theme,price',[('mother','theme_mother',790),('short','theme_ny',590)])
async def test_previously_issued_invoices_keep_old_price(credit_db,payments,monkeypatch,tariff,theme,price):
    current=database.CARTOON_TARIFFS
    with monkeypatch.context() as patch:
        patch.setattr(database,'CARTOON_TARIFFS',{**current,tariff:(price*100,current[tariff][1])})
        key=await database.create_cartoon_order('cartoon_old_'+tariff,456,theme,tariff)
    order=await database.get_cartoon_order(key)
    link=await payments.create_payment_link(456,order_id=key,amount=price,description=order['product_name'])
    fields=parse_form_fields(parse_qsl(urlsplit(link).query,keep_blank_values=True))
    validated,_=await payments.validate_payment({'order_num':key,'order_id':'old-'+tariff,'sum':str(price),
                                               'products':fields['products'],'customer_extra':fields['customer_extra']})
    assert validated['amount_kopecks']==price*100
    assert database.CARTOON_TARIFFS[tariff][0]!=price*100


@pytest.mark.parametrize('admin,flow,excerpt',[(False,'cartoon',True),(True,'cartoon',False),(False,'gift',False),(False,'video',False)])
async def test_only_minute_excerpt_is_shown_to_cartoon_clients_without_changing_approved_lyrics(credit_db,admin,flow,excerpt):
    full=mothers_day.render_approved_song('Ольги',[])
    ctx=await ready_context()
    await ctx.update_data(song_review_lyrics=full,song_review_token='token',song_review_flow=flow,
        song_review_label=mothers_day.TITLE,song_review_admin=admin,song_review_voice='female')
    e=message_event('');await song_review.show_text(e,ctx)
    text='\n'.join(c.args[0] for c in e.message.answer.await_args_list)
    assert 'Для Ольги эта песня — благодарность' in text and 'Для Елены' not in text
    assert ('Мы выросли' not in text)==excerpt
    assert text.count('С Днём матери, мама родная!')==(1 if excerpt else 3)
    assert (await ctx.get_data())['song_review_lyrics']==full
    handlers.generate_song.assert_not_awaited()


async def test_example_is_before_description_and_campaign_start_opens_mother_directly(credit_db,media):
    ctx=await ready_context();e=media('');e.callback=SimpleNamespace(payload='mother_card_start');e.ack=AsyncMock();e.send=AsyncMock(return_value=object())
    router=handlers.create_router(admin_user_id=999)
    await call_handler(router,'mother_card_start',e,ctx)
    messages=e.send.await_args_list
    assert messages[0].kwargs['attachments'][0].type.value=='video'
    assert '590 ₽' in messages[1].args[0]
    assert (await ctx.get_data())['card_song_target']=='theme_mother'
    assert await ctx.get_state()==handlers.PosterStates.waiting_for_song_setup
    assert next(iter(handlers.CARTOON_THEMES))=='theme_mother'
    assert list(handlers.CARTOON_THEMES)[1]=='theme_ny'


async def test_catalog_entry_and_broadcast_use_the_same_example(credit_db,media):
    e=media();await campaign.preview(e)
    source=e.bot.upload_media.await_args.args[0].path
    ctx=await ready_context();await ctx.update_data(cartoon_flow='short')
    router=handlers.create_router(admin_user_id=456)
    click=callback_event('theme_mother');click.bot=e.bot
    await dispatch_callback(router,click,ctx)
    assert e.bot.upload_media.await_args.args[0].path==source


async def subscribers():
    for user in (101,102,103):await database.add_credits(user,1)
    await database.mark_user_inactive(102)


async def test_campaign_preview_never_sends_to_users_and_has_exact_buttons(credit_db,media):
    await subscribers();e=media('/broadcast_mother_preview')
    await call_handler(handlers.create_router(admin_user_id=456),'broadcast_mother_preview',e)
    e.bot.send_message.assert_not_awaited()
    assert e.message.answer.await_args_list[1].args[0]==campaign.TEXT
    buttons=e.message.answer.await_args_list[1].kwargs['attachments'][0].payload.buttons
    assert [row[0].payload for row in buttons]==['mother_card_start','back_to_main']
    assert '29 ноября' in campaign.TEXT


async def test_unauthorized_campaign_cannot_upload_or_send(credit_db,media):
    e=media();await call_handler(handlers.create_router(admin_user_id=999),'broadcast_mother_preview',e)
    e.bot.upload_media.assert_not_awaited();e.bot.send_message.assert_not_awaited()


async def test_campaign_is_durable_skips_inactive_and_counts_completed_once(credit_db,media):
    await subscribers();e=media()
    await campaign.send(e,asyncio.Lock())
    assert [c.kwargs['user_id'] for c in e.bot.send_message.await_args_list]==[101,101,103,103]
    assert e.bot.upload_media.await_count==1
    assert (await database.get_all_counters())['mother_broadcast_delivered']==2
    e.bot.send_message.reset_mock();await campaign.send(e,asyncio.Lock())
    e.bot.send_message.assert_not_awaited()
    assert (await database.get_all_counters())['mother_broadcast_delivered']==2


async def test_campaign_resumes_partial_recipient_and_stops_on_rate_limit(credit_db,media):
    await database.add_credits(101,1);e=media()
    e.bot.send_message.side_effect=[object(),MaxApiError(code=429,raw='rate limit')]
    await campaign.send(e,asyncio.Lock())
    assert 'остановлена' in str(e.message.answer.await_args_list)
    e.bot.send_message=AsyncMock(return_value=object())
    await campaign.send(e,asyncio.Lock())
    assert e.bot.send_message.await_count==1
    assert e.bot.send_message.await_args.kwargs['text']==campaign.TEXT
    assert (await database.get_all_counters())['mother_broadcast_delivered']==1


async def test_missing_video_aborts_before_contacting_subscribers(credit_db,media):
    await subscribers();e=media();campaign.EXAMPLE.unlink()
    with pytest.raises(ValueError):await campaign.send(e,asyncio.Lock())
    e.bot.send_message.assert_not_awaited()


async def test_campaign_start_cannot_interrupt_active_render(credit_db):
    ctx=await ready_context();await ctx.update_data(cartoon_session_id='active',chosen_avatar='https://cdn.example/saved.png')
    await ctx.set_state(handlers.PosterStates.cartoon_generating)
    before=await ctx.get_data();e=callback_event('mother_card_start')
    await dispatch_callback(handlers.create_router(admin_user_id=999),e,ctx)
    assert await ctx.get_data()==before and await ctx.get_state()==handlers.PosterStates.cartoon_generating
    e.bot.upload_media.assert_not_awaited()

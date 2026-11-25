"""Both holidays: retained approved lyrics, paid gates, birthday continuity and replay."""
import json
import re
from pathlib import Path
from unittest.mock import AsyncMock
from urllib.parse import urlsplit,parse_qsl

import httpx
import pytest
import respx

import audio_service as audio
import database
import handlers
import mothers_day as mother
import mother_story as story
import mother_storyboard as board
import mother_stats
from config import Config
from prodamus import parse_form_fields
from test_handlers import callback_event,message_event,ready_context,dispatch_callback,call_handler,event_replies
from test_prodamus import payments,payment_config
from test_mothers_day import renderer


@respx.mock
async def test_birthday_full_song_preserves_approved_verses_personal_name_and_holiday(monkeypatch):
    monkeypatch.setattr(audio.Config,'from_env',lambda:Config(token='test',photo_path=Path('unused'),image_api_key='test-key'))
    notes,tags,title=audio.build_song_request('Ольга; спасибо за заботу',mother.BIRTHDAY_TITLE,'gift')
    route=respx.post('https://api.openai.com/v1/chat/completions').mock(return_value=httpx.Response(200,json={
        'choices':[{'finish_reason':'stop','message':{'content':json.dumps({'name_genitive':'Ольги','personal_lines':[]})}}]}))
    words=await audio.write_song_lyrics(notes,tags,title)
    assert words==mother.render_approved_song('Ольги',[],birthday=True)
    assert words.count('С днём рождения, мама родная!')==3 and 'Днём матери' not in words
    assert 'Для Ольги эта песня — благодарность' in words and 'Мы выросли' in words
    assert 'бабуш' not in words and 'внук' not in words
    assert "mother's birthday" in tags and "Mother's Day" not in tags
    assert 'рождения мамы' in json.loads(route.calls[0].request.content)['messages'][0]['content']


@pytest.mark.parametrize('occasion',['mother','mother_bday'])
@pytest.mark.parametrize('entry',['audio','music','cartoon'])
async def test_product_review_invoice_and_paid_resume(credit_db,payments,monkeypatch,occasion,entry):
    monkeypatch.setattr('mother_broadcast.example_attachment',AsyncMock(return_value=object()))
    words=mother.render_approved_song('Ольги',[],birthday=occasion=='mother_bday')
    writer=AsyncMock(return_value=words);picture=AsyncMock()
    router=handlers.create_router(admin_user_id=999,payments=payments,lyric_writer=writer,avatar_generator=picture)
    ctx=await ready_context()
    if entry=='audio':
        await dispatch_callback(router,callback_event('audio_fun_'+occasion),ctx)
        d=await ctx.get_data();token=d['song_setup_token']
        await dispatch_callback(router,callback_event('song_setup:genre:pop:'+token),ctx)
    else:
        await ctx.update_data(cartoon_flow='short' if entry=='cartoon' else None,video_flow=entry=='music',video_product='direct',video_amount_kopecks=29900)
        await dispatch_callback(router,callback_event(('theme_' if entry=='cartoon' else 'video_style:mode_')+occasion),ctx)
        token=(await ctx.get_data())['song_setup_token']
    await dispatch_callback(router,callback_event('song_setup:voice:female:'+token),ctx)
    e=message_event('Ольга; спасибо за заботу')
    await call_handler(router,'collect_song_details' if entry=='audio' else 'card_song_details',e,ctx)
    d=await ctx.get_data()
    assert d['song_review_format']=='gift' and d['song_review_lyrics']==words
    shown=str(e.message.answer.await_args_list)
    assert ('Мы выросли' in shown)==(entry!='cartoon')
    assert ('Переход' in shown)==(entry!='cartoon')
    assert 'Для Ольги' in shown
    e=callback_event('song_review:approve:'+d['song_review_token'])
    await dispatch_callback(router,e,ctx)
    d=await ctx.get_data();picture.assert_not_awaited();handlers.generate_song.assert_not_awaited()
    if entry=='audio':
        order=await database.get_audio_order(d['audio_checkout_id'])
        assert order['amount_kopecks']==29000 and order['scenario']==occasion and order['lyrics']==words
        assert not order['job_id'] and not order['submit_started']
    elif entry=='music':
        order=await database.get_musical_order(d['music_prepaid_id'])
        assert order['amount_kopecks']==29900 and order['mode']=='mode_'+occasion
        seed=json.loads(order['draft']);assert seed['song_review_lyrics']==words
        await database.mark_musical_paid(order['order_id'],'paid-'+occasion)
        await dispatch_callback(router,callback_event('music_resume:'+order['order_id']),ctx)
        assert await ctx.get_state()==handlers.PosterStates.waiting_for_photo
    else:
        order=await database.get_cartoon_order(d['cartoon_order_id'])
        assert order['amount_kopecks']==59000 and order['tariff']==occasion and order['theme']=='theme_'+occasion
        seed=json.loads(order['fsm_data']);assert seed['song_review_lyrics']==words
        await database.mark_cartoon_paid(order['order_id'],'paid-'+occasion)
        await dispatch_callback(router,callback_event('cartoon_resume:'+order['order_id']),ctx)
        assert await ctx.get_state()==handlers.PosterStates.waiting_for_avatar_photo
    writer.assert_awaited_once()


async def test_birthday_invoice_cannot_be_relabelled_or_underpaid(credit_db,payments):
    key=await database.create_cartoon_order('cartoon_birthday_invoice',456,'theme_mother_bday','mother_bday')
    order=await database.get_cartoon_order(key)
    link=await payments.create_payment_link(456,order_id=key,amount=590,description=order['product_name'])
    fields=parse_form_fields(parse_qsl(urlsplit(link).query,keep_blank_values=True))
    payload={'order_num':key,'order_id':'birthday-invoice','sum':'590','products':fields['products'],'customer_extra':fields['customer_extra']}
    valid,_=await payments.validate_payment(payload)
    assert valid['amount_kopecks']==59000
    with pytest.raises(ValueError):await payments.validate_payment({**payload,'sum':'390'})
    for theme,tariff in [('theme_mother_bday','short'),('theme_mother_bday','mother'),('theme_mother','mother_bday'),('theme_ny','mother_bday')]:
        with pytest.raises(ValueError):await database.create_cartoon_order('cartoon_bad_'+theme+tariff,456,theme,tariff)
    assert order['status']=='pending'


async def test_birthday_six_scene_prompts_and_cached_result_reused(renderer):
    images,submit,poll=renderer;data={'cartoon_theme':'theme_mother_bday'}
    url=await story.generate(data,'https://cdn.example/mom.png','https://cdn.example/song.mp3',checkpoint=AsyncMock(),progress=AsyncMock())
    assert data['cartoon_kling_story']['source'][0]==board.BIRTHDAY_POLICY
    assert images.await_count==submit.await_count==6
    prompts=[call.args[1] for call in images.await_args_list]
    assert all('balloons' in p and 'fairy-light' in p and 'SILENT' in p for p in prompts)
    assert all(not re.search(r'\bpie\b',p,re.I) for p in prompts)
    assert 'birthday cake with exactly three lit candles' in prompts[2]
    assert 'no candles' not in prompts[2] and 'birthday decorations or' not in prompts[2]
    assert 'FULLY SET' in prompts[3] and 'cake' in prompts[3]
    assert 'NOT at the dining table' in prompts[4]
    assert all('No speech, singing' in c.args[2] for c in submit.await_args_list)
    assert await story.generate(data,'https://cdn.example/mom.png','https://cdn.example/song.mp3',checkpoint=AsyncMock(),progress=AsyncMock())==url
    assert images.await_count==submit.await_count==6
    # Mother's Day storyboard remains distinct, with pie and no birthday decoration.
    assert 'apple pie' in board.image_prompt(2) and 'no candles' in board.image_prompt(2)
    assert 'no candles' not in board.image_prompt(2,birthday=True)


async def test_birthday_statistics_are_separate_and_idempotent(credit_db):
    await mother_stats.record(456,'mother_bday_cartoon_ready','same-order')
    await mother_stats.record(456,'mother_bday_cartoon_ready','same-order')
    await mother_stats.record(999,'mother_bday_cartoon_ready','admin-test')
    counts=await database.get_all_counters(admin_id=999)
    assert counts['mother_bday_cartoon_ready']==1 and not counts.get('mother_cartoon_ready')
    paid=await mother_stats.sales(999)
    report=mother_stats.text(lambda k:counts.get(k,0),await database.get_all_counters(),paid)
    assert 'День рождения мамы (клиенты)' in report and 'Администратор (отдельно)' in report

import json
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import httpx
import pytest
import respx

import audio_service as audio
import personal_song as personal
import song_review
import database
import handlers
import mothers_day
from config import Config
from test_handlers import callback_event,message_event,ready_context,dispatch_callback,call_handler,event_replies
from test_prodamus import payments,payment_config
from test_song_review import draft

CHORUS='''С днём рождения, дочка родная,
Пусть удача встречает в пути.
Мы с тобой, и всегда помогаем
Верить в силы свои и идти.
Машенька, смело мечтай и учись,
Рядом с тобой наша нежность на жизнь.'''
WORDS='''[Verse 1]
Солнце касается школьных тетрадей,
Утро рисует на стёклах узор.
Машенька, сколько открытий нас ради
Ты принесла в наш семейный простор.
Ты стала студенткой, и мы понимаем:
Дальше тебя ждёт счастливый маршрут.
[Chorus]
'''+CHORUS+'''
[Verse 2]
Краски и кисти лежат у окошка,
В новых картинах — твои города.
Каждая линия, каждая крошка
Света останется с нами всегда.
Пусть на занятиях всё удаётся,
Мы поддержать тебя рядом всегда.
[Chorus]
'''+CHORUS+'''
[Bridge]
Можно прийти, если трудно бывает,
Можно о самом заветном сказать.
Здесь тебя любят и здесь понимают,
Здесь не придётся поддержку искать.
[Chorus]
'''+CHORUS
DETAILS='Машенька, дочь. От мамы и папы. Стала студенткой, любит рисовать. Гордимся и всегда будем рядом.'


@respx.mock
async def test_real_lyric_request_uses_story_and_full_structure_without_copying_questionnaire(monkeypatch):
    monkeypatch.setattr(audio.Config,'from_env',lambda:Config(token='test',photo_path=Path('unused'),image_api_key='test-key'))
    route=respx.post('https://api.openai.com/v1/chat/completions').mock(return_value=httpx.Response(200,json={
        'choices':[{'finish_reason':'stop','message':{'content':WORDS}}]}))
    notes,tags,title=personal.build_request(DETAILS,'🎂 День рождения','gift','warm pop')
    assert await audio.write_song_lyrics(notes,tags,title)==WORDS
    request=json.loads(route.calls[0].request.content)
    brief=json.loads(request['messages'][1]['content']);system=request['messages'][0]['content']
    assert brief['details']==DETAILS and brief['required_chorus_phrase'] is None
    assert brief['occasion']=='🎂 День рождения' and brief['format']=='gift'
    assert personal.SYSTEM_PROMPT in system and personal.FULL_FORMAT in system
    assert 'Дословно сохраняй только цитаты' in system and '6–8 строк' in system
    assert len(route.calls)==1


@pytest.mark.parametrize('fault',[lambda t:t.replace('[Bridge]','[Wrong]'),lambda t:t.replace(CHORUS,'С праздником!',1),lambda t:t.replace(CHORUS,CHORUS+'\nЕщё строка',1)])
def test_rejects_incomplete_or_inconsistent_model_text_before_paid_music(fault):
    with pytest.raises(ValueError):personal.validate_lyrics(fault(WORDS),'gift')


def test_short_and_full_profiles_and_existing_mother_template():
    short='[Verse 1]\nТы любишь рисунки и краски,\nВ них мир оживает опять.\n[Chorus]\nС днём рождения, дочка родная,\nПусть удача встречает в пути.\nМы с тобой, и всегда помогаем\nВерить в силы свои и идти.'
    assert personal.validate_lyrics(short,'fun')==short
    with pytest.raises(ValueError):personal.validate_lyrics(short,'gift')
    notes,_,_=personal.build_request('а'*1000,'🎂 День рождения','gift')
    assert personal.parse_request(notes)['details']=='а'*1000
    mother=personal.build_request('Ольга; спасибо за заботу',mothers_day.TITLE,'gift')
    assert mother==audio.build_song_request('Ольга; спасибо за заботу',mothers_day.TITLE,'gift')
    assert personal.parse_request(mother[0]) is None


async def music_draft(payments):
    writer=AsyncMock(return_value=WORDS)
    router=handlers.create_router(admin_user_id=999,payments=payments,lyric_writer=writer)
    ctx=await ready_context();await ctx.update_data(video_flow=True,video_product='direct')
    e=callback_event('video_style:mode_ny');await dispatch_callback(router,e,ctx)
    d=await ctx.get_data();assert 'Кто дарит' in d['song_setup_prompt'] and 'любимая фраза' in d['song_setup_prompt']
    await dispatch_callback(router,callback_event('song_setup:voice:female:'+d['song_setup_token']),ctx)
    await call_handler(router,'card_song_details',message_event(DETAILS),ctx)
    return router,ctx,writer


async def test_music_full_text_can_be_edited_by_client_before_bill_and_no_listening(credit_db,payments):
    router,ctx,writer=await music_draft(payments)
    d=await ctx.get_data();assert d['song_review_format']=='gift' and d['song_review_no_preview']
    assert personal.parse_request(writer.await_args.args[0])['details']==DETAILS
    assert not d.get('music_prepaid_id')
    await dispatch_callback(router,callback_event('song_review:text_edit:'+d['song_review_token']),ctx)
    edited=WORDS.replace('Машенька','Машуля')
    await call_handler(router,'song_review_input',message_event(edited),ctx)
    d=await ctx.get_data();assert d['song_review_lyrics']==edited and not d['song_review_approved']
    assert writer.await_count==1
    await dispatch_callback(router,callback_event('song_review:approve:'+d['song_review_token']),ctx)
    order=await database.get_musical_order((await ctx.get_data())['music_prepaid_id'])
    assert order['status']=='pending' and order['amount_kopecks']==29900
    assert json.loads(order['draft'])['song_review_lyrics']==edited
    assert not order['input_ready']
    handlers.generate_song.assert_not_awaited();handlers.make_protected_preview.assert_not_awaited()


@pytest.mark.parametrize('product',['audio','cartoon'])
async def test_music_draft_can_become_song_or_cartoon_without_paying_for_wrong_product(credit_db,payments,product):
    router,ctx,writer=await music_draft(payments)
    token=(await ctx.get_data())['song_review_token']
    await dispatch_callback(router,callback_event('song_review:'+product+':'+token),ctx)
    if product=='audio':
        order=await database.get_audio_order((await ctx.get_data())['audio_checkout_id'])
        assert order['lyrics']==WORDS and order['amount_kopecks']==49000 and not order['preview_requested']
        assert not order['job_id'] and order['status']=='pending'
    else:
        assert (await ctx.get_data())['song_product_kind']=='cartoon'
        await dispatch_callback(router,callback_event('song_product_theme:theme_ny'),ctx)
        order=await database.get_cartoon_order((await ctx.get_data())['cartoon_order_id'])
        assert order['amount_kopecks']==39000 and json.loads(order['fsm_data'])['song_review_lyrics']==WORDS
    import aiosqlite
    async with aiosqlite.connect(database.DATABASE_PATH) as db:
        assert (await (await db.execute('SELECT COUNT(*) FROM musical_orders')).fetchone())[0]==0
    writer.assert_awaited_once();handlers.generate_song.assert_not_awaited()


@pytest.mark.parametrize('occasion',['mother','mother_bday'])
@pytest.mark.parametrize('interrupted',[False,True])
async def test_new_paid_cartoon_starts_video_automatically_without_preview_or_second_music_charge(credit_db,payments,monkeypatch,occasion,interrupted):
    from maxapi.types import AttachmentUpload
    from test_handlers import cartoon_photo
    import mother_story
    router,ctx,writer=await draft(payments,scenario='gift_scen_'+occasion)
    await dispatch_callback(router,callback_event('song_review:cartoon:'+(await ctx.get_data())['song_review_token']),ctx)
    key=(await ctx.get_data())['cartoon_order_id'];await database.mark_cartoon_paid(key,'new-no-preview')
    song=AsyncMock(return_value='one-retained-job');preview=AsyncMock(side_effect=AssertionError('No audio previews'))
    import asyncio
    calls=0
    async def render_scene(data,reference,audio,*,checkpoint,progress):
        nonlocal calls
        calls+=1
        if interrupted and calls==1:
            data['cartoon_kling_story']={'source':['minute',reference],'shots':[{'task_id':'retained-video-id'}]}
            await checkpoint();raise asyncio.CancelledError()
        if interrupted:assert data['cartoon_kling_story']['shots'][0]['task_id']=='retained-video-id'
        return 'https://cdn.example/final.mp4'
    render=AsyncMock(side_effect=render_scene)
    monkeypatch.setattr(mother_story,'generate',render)
    monkeypatch.setattr(handlers,'create_audio_preview',preview)
    monkeypatch.setattr(handlers,'count_people_in_photo',AsyncMock(return_value=1))
    monkeypatch.setattr(handlers,'create_cartoon_avatar_preview',AsyncMock(return_value='https://cdn.example/image-preview.png'))
    for name,kind in [('upload_image_for_max','image'),('upload_video_for_max','video')]:
        monkeypatch.setattr(handlers,name,AsyncMock(return_value=AttachmentUpload(type=kind,payload={'token':'media'})))
    router=handlers.create_router(admin_user_id=999,payments=payments,lyric_writer=writer,
        avatar_generator=AsyncMock(return_value='https://cdn.example/mom.png'),song_generator=song,
        song_waiter=AsyncMock(return_value={'data':{'musics':[{'audioUrl':'https://cdn.example/full.mp3'}]}}))
    await dispatch_callback(router,callback_event('cartoon_resume:'+key),ctx)
    await call_handler(router,'collect_cartoon_photo',cartoon_photo(),ctx)
    if interrupted:
        with pytest.raises(asyncio.CancelledError):await dispatch_callback(router,callback_event('avatar_ok_1'),ctx)
        await ctx.clear();await dispatch_callback(router,callback_event('cartoon_resume:'+key),ctx)
    else:await dispatch_callback(router,callback_event('avatar_ok_1'),ctx)
    assert (await database.get_cartoon_order(key))['status']=='completed'
    song.assert_awaited_once();assert render.await_count==(2 if interrupted else 1);preview.assert_not_awaited()
    assert not (await ctx.get_data()).get('mother_song_variants')


async def test_admin_receives_full_audio_without_protected_preview(credit_db,monkeypatch):
    writer=AsyncMock(return_value=WORDS);song=AsyncMock(return_value='admin-one-job')
    preview=AsyncMock(side_effect=AssertionError('No preview'))
    monkeypatch.setattr(handlers,'make_protected_preview',preview)
    router=handlers.create_router(admin_user_id=456,lyric_writer=writer,song_generator=song,
        song_waiter=AsyncMock(return_value={'data':{'musics':[{'audioUrl':'https://cdn.example/full.mp3','title':'Дочке'}]}}))
    ctx=await ready_context();await dispatch_callback(router,callback_event('gift_scen_bday'),ctx)
    token=(await ctx.get_data())['song_setup_token']
    await dispatch_callback(router,callback_event('song_setup:genre:pop:'+token),ctx)
    await dispatch_callback(router,callback_event('song_setup:voice:female:'+token),ctx)
    await call_handler(router,'collect_gift_details',message_event(DETAILS),ctx)
    token=(await ctx.get_data())['song_review_token'];event=callback_event('song_review:approve:'+token)
    await dispatch_callback(router,event,ctx)
    order=await database.get_audio_order((await ctx.get_data())['audio_checkout_id'])
    assert order['delivered'] and order['free_access'] and not order['preview_url']
    song.assert_awaited_once();preview.assert_not_awaited()


async def test_client_mother_excerpt_edit_keeps_unshown_verses_and_reconfirms(credit_db):
    full=mothers_day.render_approved_song('Ольги',[])
    ctx=await ready_context()
    await ctx.update_data(song_review_lyrics=full,song_review_label=mothers_day.TITLE,
        song_review_flow='cartoon',song_review_no_preview=True,song_review_admin=False,
        song_review_token='current',song_review_step='text',song_review_voice='female',
        song_review_tags='warm pop',song_review_title='Маме',song_review_details='Ольга')
    await ctx.set_state(handlers.PosterStates.waiting_for_song_review)
    writer=AsyncMock();router=handlers.create_router(admin_user_id=999,lyric_writer=writer)
    await dispatch_callback(router,callback_event('song_review:text_edit:current'),ctx)
    await call_handler(router,'song_review_input',message_event(mothers_day.minute_excerpt(full).replace('Для Ольги','Для Марии')),ctx)
    d=await ctx.get_data()
    assert 'Для Марии' in d['song_review_lyrics'] and 'Мы выросли' in d['song_review_lyrics']
    assert d['song_review_lyrics'].count(mothers_day.APPROVED_CHORUS)==3
    assert not d['song_review_approved'] and d['song_review_token']!='current'
    writer.assert_not_awaited();handlers.generate_song.assert_not_awaited()

"""Real new interface: occasion -> genre -> voice -> details -> words -> product -> pay."""
import json
from unittest.mock import AsyncMock

import pytest

import handlers
import database
import song_review
from audio_orders import PaidAudioWorker
from test_handlers import callback_event,message_event,ready_context,dispatch_callback,call_handler,event_replies
from test_prodamus import payments,payment_config

WORDS='[Verse 1]\nДом согревает свет\n[Chorus]\nС Днём матери, Людмила, спасибо за заботу!\n[Verse 2]\nТы даришь нам тепло\n[Chorus]\nС Днём матери, Людмила, спасибо за заботу!\n[Bridge]\nПусть счастье будет рядом\n[Chorus]\nС Днём матери, Людмила, спасибо за заботу!'


async def draft(payments,*,scenario='gift_scen_mother',voice='mixed'):
    words=WORDS.replace('С Днём матери,','С днём рождения,') if scenario.endswith('mother_bday') else WORDS
    writer=AsyncMock(return_value=words)
    router=handlers.create_router(admin_user_id=999,payments=payments,lyric_writer=writer)
    ctx=await ready_context()
    await dispatch_callback(router,callback_event(scenario),ctx)
    d=await ctx.get_data();assert await ctx.get_state()==handlers.PosterStates.waiting_for_song_setup
    writer.assert_not_awaited()
    token=d['song_setup_token']
    await dispatch_callback(router,callback_event('song_setup:genre:pop:'+token),ctx)
    await dispatch_callback(router,callback_event('song_setup:voice:'+voice+':'+token),ctx)
    assert await ctx.get_state()==handlers.GIFT_SCENARIO_STATES[scenario]
    writer.assert_not_awaited()
    await call_handler(router,'collect_gift_details',message_event('Людмила; спасибо за заботу'),ctx)
    d=await ctx.get_data()
    writer.assert_awaited_once()
    assert d['song_review_lyrics']==words and d['song_review_voice']==voice
    assert await ctx.get_state()==handlers.PosterStates.waiting_for_song_review
    return router,ctx,writer


async def test_public_catalog_and_all_three_products_before_any_music_charge(credit_db,payments):
    router,ctx,writer=await draft(payments)
    assert not await database.get_audio_order('not-created')
    async with __import__('aiosqlite').connect(database.DATABASE_PATH) as db:
        assert (await (await db.execute('SELECT COUNT(*) FROM audio_orders')).fetchone())[0]==0
    d=await ctx.get_data();event=callback_event('song_review:voice_menu:'+d['song_review_token'])
    await dispatch_callback(router,event,ctx)
    event=callback_event('song_review:voice:female:'+d['song_review_token'])
    await dispatch_callback(router,event,ctx)
    assert writer.await_count==1  # Changing the vocal style reuses reviewed words.
    text=str(event.send.await_args_list)
    assert 'Сделать песню' in text and 'Сделать мульт-открытку' in text and 'Сделать музыкальную открытку' in text
    assert (await ctx.get_data())['song_review_tags'].endswith('female solo vocals')


async def test_change_personal_words_then_confirm_keeps_final_lyrics_for_paid_worker(credit_db,payments):
    router,ctx,writer=await draft(payments)
    token=(await ctx.get_data())['song_review_token']
    await dispatch_callback(router,callback_event('song_review:edit:'+token),ctx)
    newer=WORDS.replace('заботу','поддержку')
    writer.return_value=newer
    await call_handler(router,'song_review_input',message_event('Людмила; спасибо за поддержку'),ctx)
    assert writer.await_count==2
    token=(await ctx.get_data())['song_review_token']
    e=callback_event('song_review:approve:'+token)
    await dispatch_callback(router,e,ctx)
    data=await ctx.get_data();order=await database.get_audio_order(data['audio_checkout_id'])
    assert order['status']=='pending' and order['amount_kopecks']==49000
    assert order['lyrics']==newer and 'male and female vocals' in order['tags']
    assert not order['job_id'] and not order['submit_started'] and not order['preview_requested']
    assert '490 ₽' in str(e.send.await_args_list)
    assert not await database.pending_audio_orders()
    await database.mark_audio_paid(order['order_id'],'song-review-paid')
    generator=AsyncMock(return_value='known-job')
    worker=PaidAudioWorker(e.bot,generator=generator,waiter=AsyncMock(return_value={'data':{'musics':[{'audioUrl':'https://cdn.example/song.mp3','title':'Маме'}]}}),lyric_writer=AsyncMock(side_effect=AssertionError('Do not rewrite approved text')))
    await worker.process(order['order_id'])
    generator.assert_awaited_once()
    assert generator.await_args.args[0]==newer and 'male and female vocals' in generator.await_args.args[1]
    assert (await database.get_audio_order(order['order_id']))['delivered']==1
    await dispatch_callback(router,callback_event('song_review:approve:'+token),ctx)
    assert generator.await_count==1


@pytest.mark.parametrize('product,table,price',[('cartoon','cartoon_orders',59000),('music','musical_orders',29900)])
async def test_conversion_preserves_review_and_voice_in_durable_unpaid_draft(credit_db,payments,product,table,price):
    router,ctx,writer=await draft(payments)
    token=(await ctx.get_data())['song_review_token'];e=callback_event('song_review:'+product+':'+token)
    await dispatch_callback(router,e,ctx)
    async with __import__('aiosqlite').connect(database.DATABASE_PATH) as db:
        db.row_factory=__import__('aiosqlite').Row
        row=dict(await (await db.execute('SELECT * FROM '+table)).fetchone())
    assert row['status']=='pending' and row['amount_kopecks']==price
    seed=json.loads(row['fsm_data'] if product=='cartoon' else row['draft'])
    assert seed['song_review_lyrics']==WORDS and seed['song_review_voice']=='mixed' and seed['_song_seed']
    if product=='cartoon':
        assert row['tariff']=='mother' and row['theme']=='theme_mother'
        await database.mark_cartoon_paid(row['order_id'],'cartoon-from-song')
        await dispatch_callback(router,callback_event('cartoon_resume:'+row['order_id']),ctx)
        assert await ctx.get_state()==handlers.PosterStates.waiting_for_avatar_photo
    else:
        await database.mark_musical_paid(row['order_id'],'music-from-song')
        await dispatch_callback(router,callback_event('music_resume:'+row['order_id']),ctx)
        assert await ctx.get_state()==handlers.PosterStates.waiting_for_photo
    assert (await ctx.get_data())['song_review_lyrics']==WORDS
    writer.assert_awaited_once()


async def test_other_occasions_also_use_genre_voice_and_review(credit_db,payments):
    router,ctx,_=await draft(payments,scenario='gift_scen_bday',voice='male')
    d=await ctx.get_data()
    assert 'male solo vocals' in d['song_review_tags']
    await dispatch_callback(router,callback_event('song_review:cartoon:'+d['song_review_token']),ctx)
    assert (await ctx.get_data())['song_product_seed']['song_review_lyrics']==WORDS
    e=callback_event('song_product_theme:theme_ny')
    await dispatch_callback(router,e,ctx)
    order=await database.get_cartoon_order((await ctx.get_data())['cartoon_order_id'])
    assert order['amount_kopecks']==39000 and order['status']=='pending'


async def test_stale_review_or_setup_callbacks_cannot_start_music_or_create_invoices(credit_db,payments):
    router,ctx,writer=await draft(payments)
    e=callback_event('song_review:approve:'+'a'*32)
    await dispatch_callback(router,e,ctx)
    assert not (await ctx.get_data()).get('audio_checkout_id')
    await ctx.clear()
    await dispatch_callback(router,callback_event('song_setup:voice:male:'+'b'*32),ctx)
    writer.assert_awaited_once()


async def test_mother_cartoon_preview_and_heygen_story_use_approved_song_without_kling(credit_db,monkeypatch):
    from maxapi.types import AttachmentUpload
    from test_handlers import cartoon_photo
    import mother_story
    import mothers_day
    writer=AsyncMock(return_value=WORDS)
    generator=AsyncMock(return_value='approved-song-job')
    router=handlers.create_router(admin_user_id=456,lyric_writer=writer,avatar_generator=AsyncMock(return_value='https://cdn.example/mom-avatar.png'),
        song_generator=generator,song_waiter=AsyncMock(return_value={'data':{'musics':[{'audioUrl':'https://cdn.example/approved.mp3'}]}}))
    monkeypatch.setattr(handlers,'upload_image_for_max',AsyncMock(return_value=AttachmentUpload(type='image',payload={'token':'image'})))
    monkeypatch.setattr(handlers,'upload_audio_for_max',AsyncMock(return_value=AttachmentUpload(type='audio',payload={'token':'audio'})))
    monkeypatch.setattr(handlers,'upload_video_for_max',AsyncMock(return_value=AttachmentUpload(type='video',payload={'token':'video'})))
    monkeypatch.setattr(handlers,'create_audio_preview',AsyncMock(return_value='https://cdn.example/preview.mp3'))
    animation=AsyncMock(return_value='https://cdn.example/mother-final.mp4');monkeypatch.setattr(mother_story,'generate',animation)
    ctx=await ready_context()
    await dispatch_callback(router,callback_event('cat_cartoon'),ctx)
    await dispatch_callback(router,callback_event('theme_mother'),ctx)
    token=(await ctx.get_data())['song_setup_token']
    await dispatch_callback(router,callback_event('song_setup:voice:female:'+token),ctx)
    await call_handler(router,'card_song_details',message_event('Людмила; спасибо за заботу'),ctx)
    generator.assert_not_awaited()
    token=(await ctx.get_data())['song_review_token']
    await dispatch_callback(router,callback_event('song_review:approve:'+token),ctx)
    assert await ctx.get_state()==handlers.PosterStates.waiting_for_avatar_photo
    await call_handler(router,'collect_cartoon_photo',cartoon_photo(),ctx)
    await dispatch_callback(router,callback_event('avatar_ok_1'),ctx)
    assert generator.await_count==2
    assert generator.await_args.args[0]==WORDS and 'female solo vocals' in generator.await_args.args[1]
    token=(await ctx.get_data())['cartoon_preview_token']
    await dispatch_callback(router,callback_event('cartoon_song_pick:2:'+token),ctx)
    await dispatch_callback(router,callback_event('confirm_video_2'),ctx)
    animation.assert_awaited_once()
    assert animation.await_args.args[1:] == ('https://cdn.example/mom-avatar.png','https://cdn.example/approved.mp3')
    handlers.create_video_task.assert_not_awaited()
    writer.assert_awaited_once()
    assert await ctx.get_state()==handlers.PosterStates.waiting_for_mode


async def test_short_song_keeps_199_price_with_approved_text_and_female_voice(credit_db,payments):
    writer=AsyncMock(return_value=WORDS)
    router=handlers.create_router(admin_user_id=999,payments=payments,lyric_writer=writer)
    ctx=await ready_context()
    await dispatch_callback(router,callback_event('audio_fun_universal'),ctx)
    token=(await ctx.get_data())['song_setup_token']
    await dispatch_callback(router,callback_event('song_setup:genre:jazz:'+token),ctx)
    await dispatch_callback(router,callback_event('song_setup:voice:female:'+token),ctx)
    await call_handler(router,'collect_song_details',message_event('Людмила; спасибо за заботу'),ctx)
    token=(await ctx.get_data())['song_review_token']
    await dispatch_callback(router,callback_event('song_review:approve:'+token),ctx)
    order=await database.get_audio_order((await ctx.get_data())['audio_checkout_id'])
    assert order['amount_kopecks']==19900 and order['lyrics']==WORDS
    assert 'female solo vocals' in order['tags'] and 'jazz' in order['tags']
    assert order['status']=='pending' and not order['job_id']
    assert not await database.pending_audio_orders()


async def test_old_payment_button_cannot_bypass_new_text_approval(credit_db,payments):
    router,ctx,writer=await draft(payments)
    event=callback_event('gift_payment_soon')
    await dispatch_callback(router,event,ctx)
    assert 'подтвердите текст' in event.ack.await_args.kwargs['notification']
    assert not (await ctx.get_data()).get('audio_checkout_id')
    assert not await database.pending_audio_orders()
    writer.assert_awaited_once()


async def test_paid_music_conversion_completes_inputs_without_rewriting_chosen_song(credit_db,payments,monkeypatch):
    from test_handlers import photo_event
    router,ctx,writer=await draft(payments)
    token=(await ctx.get_data())['song_review_token']
    await dispatch_callback(router,callback_event('song_review:music:'+token),ctx)
    key=(await ctx.get_data())['music_prepaid_id']
    await database.mark_musical_paid(key,'seeded-music-complete')
    generator=AsyncMock(return_value='music-from-reviewed-text')
    card=AsyncMock(return_value=['https://cdn.example/card.png'])
    monkeypatch.setattr(handlers,'fix_text_with_ai',AsyncMock(side_effect=lambda text:text))
    router=handlers.create_router(admin_user_id=999,payments=payments,lyric_writer=writer,postcard_generator=card,
        song_generator=generator,song_waiter=AsyncMock(return_value={'data':{'musics':[{'audioUrl':'https://cdn.example/approved-song.mp3'}]}}))
    await dispatch_callback(router,callback_event('music_resume:'+key),ctx)
    await call_handler(router,'collect_photo',photo_event(),ctx)
    await call_handler(router,'collect_card_text',message_event('Мама, с Днём матери!'),ctx)
    order=await database.get_musical_order(key)
    assert order['input_ready']==1 and order['audio_url']=='https://cdn.example/approved-song.mp3'
    assert order['image_url']=='https://cdn.example/card.png'
    writer.assert_awaited_once();generator.assert_awaited_once()
    assert generator.await_args.args[0]==WORDS and 'male and female vocals' in generator.await_args.args[1]


@pytest.mark.parametrize('kind,target,table,price',[
    ('cartoon','theme_mother','cartoon_orders',59000),
    ('music','mode_mother','musical_orders',29900),
    ('cartoon','theme_ny','cartoon_orders',39000),
    ('music','mode_ny','musical_orders',29900),
])
async def test_direct_card_voice_text_checkout_then_photo(credit_db,payments,kind,target,table,price):
    writer=AsyncMock(return_value=WORDS);images=AsyncMock();songs=AsyncMock()
    router=handlers.create_router(admin_user_id=999,payments=payments,lyric_writer=writer,
        avatar_generator=images,postcard_generator=images,song_generator=songs)
    ctx=await ready_context()
    await ctx.update_data(cartoon_flow='short') if kind=='cartoon' else await ctx.update_data(video_flow=True,video_product='direct')
    e=callback_event(target if kind=='cartoon' else 'video_style:'+target)
    await dispatch_callback(router,e,ctx)
    assert await ctx.get_state()==handlers.PosterStates.waiting_for_song_setup
    token=(await ctx.get_data())['song_setup_token']
    await dispatch_callback(router,callback_event('song_setup:voice:female:'+token),ctx)
    assert await ctx.get_state()==handlers.PosterStates.waiting_for_card_song_details
    await call_handler(router,'card_song_details',message_event('Ольга; спасибо за заботу'),ctx)
    writer.assert_awaited_once();images.assert_not_awaited();songs.assert_not_awaited()
    async with __import__('aiosqlite').connect(database.DATABASE_PATH) as db:
        assert (await (await db.execute('SELECT COUNT(*) FROM '+table)).fetchone())[0]==0
    token=(await ctx.get_data())['song_review_token']
    await dispatch_callback(router,callback_event('song_review:approve:'+token),ctx)
    key=(await ctx.get_data())['cartoon_order_id' if kind=='cartoon' else 'music_prepaid_id']
    order=await (database.get_cartoon_order(key) if kind=='cartoon' else database.get_musical_order(key))
    assert order['status']=='pending' and order['amount_kopecks']==price
    assert await ctx.get_state()==(handlers.PosterStates.waiting_for_cartoon_payment if kind=='cartoon' else handlers.PosterStates.waiting_for_video_payment)
    images.assert_not_awaited();songs.assert_not_awaited()
    # A checkout resume button alone must never bypass payment.
    callback=('cartoon_resume:' if kind=='cartoon' else 'music_resume:')+key
    await dispatch_callback(router,callback_event(callback),ctx)
    assert 'payment' in str(await ctx.get_state())
    await (database.mark_cartoon_paid(key,'direct-card-paid') if kind=='cartoon' else database.mark_musical_paid(key,'direct-card-paid'))
    await ctx.clear()  # Restore from durable checkout draft, as after a bot restart.
    await dispatch_callback(router,callback_event(callback),ctx)
    assert await ctx.get_state()==(handlers.PosterStates.waiting_for_avatar_photo if kind=='cartoon' else handlers.PosterStates.waiting_for_photo)
    assert (await ctx.get_data())['song_review_lyrics']==WORDS
    assert (await ctx.get_data())['song_review_voice']=='female'
    writer.assert_awaited_once();images.assert_not_awaited();songs.assert_not_awaited()


async def test_admin_rewrite_preserves_picture_story_and_defers_music_until_approval(credit_db,monkeypatch):
    from maxapi.types import AttachmentUpload
    writer=AsyncMock(return_value=WORDS.replace('заботу','поддержку'))
    songs=AsyncMock(return_value='new-music-job');avatar=AsyncMock()
    router=handlers.create_router(admin_user_id=456,lyric_writer=writer,avatar_generator=avatar,song_generator=songs,
        song_waiter=AsyncMock(return_value={'data':{'musics':[{'audioUrl':'https://cdn.example/new.mp3'}]}}))
    monkeypatch.setattr(handlers,'upload_audio_for_max',AsyncMock(return_value=AttachmentUpload(type='audio',payload={'token':'audio'})))
    monkeypatch.setattr(handlers,'create_audio_preview',AsyncMock(return_value='https://cdn.example/new-preview.mp3'))
    ctx=await ready_context()
    story={'policy':'saved','shots':[{'task_id':'already-paid-id','video_url':'https://cdn.example/clip.mp4'}]}
    await ctx.update_data(cartoon_flow='mother',cartoon_theme='theme_mother',cartoon_session_id='session',
        chosen_avatar='https://cdn.example/mom.png',original_image_url='https://cdn.example/photo.png',
        chosen_song='audio_fun_mother',chosen_song_phrase='Ольга; спасибо за заботу',chosen_song_url='https://cdn.example/old.mp3',
        song_1_url='https://cdn.example/old.mp3',song_attempt=1,cartoon_preview_token='old-token',
        cartoon_kling_story=story,cartoon_kling_task_id='old-animation-id',song_review_voice='female')
    await ctx.set_state(handlers.PosterStates.waiting_for_cartoon_music_review)
    e=callback_event('admin_song_rewrite:old-token');await dispatch_callback(router,e,ctx)
    writer.assert_awaited_once();songs.assert_not_awaited();avatar.assert_not_awaited()
    d=await ctx.get_data()
    assert d['chosen_avatar']=='https://cdn.example/mom.png' and d['cartoon_kling_story']==story
    assert 'Вставить свой текст' in str(e.send.await_args_list)
    await dispatch_callback(router,callback_event('song_review:approve:'+d['song_review_token']),ctx)
    songs.assert_awaited_once();avatar.assert_not_awaited()
    assert songs.await_args.args[0]==writer.return_value and 'female solo vocals' in songs.await_args.args[1]
    d=await ctx.get_data()
    assert d['song_1_url']=='https://cdn.example/new.mp3'
    assert d['song_rewrite_archive'][0]['song_1_url']=='https://cdn.example/old.mp3'
    assert d['chosen_avatar']=='https://cdn.example/mom.png' and d['cartoon_kling_story']==story
    assert d['cartoon_kling_task_id']=='old-animation-id'


async def test_admin_manual_words_and_public_guard(credit_db,payments):
    router,ctx,writer=await draft(payments)
    token=(await ctx.get_data())['song_review_token']
    # Forging the saved UI flag cannot grant an ordinary user admin access.
    await ctx.update_data(song_review_admin=True)
    await dispatch_callback(router,callback_event('song_review:manual:'+token),ctx)
    assert (await ctx.get_data())['song_review_step']=='text'
    admin_router=handlers.create_router(admin_user_id=456,lyric_writer=writer)
    await dispatch_callback(admin_router,callback_event('song_review:manual:'+token),ctx)
    assert (await ctx.get_data())['song_review_step']=='manual'
    text=WORDS.replace('заботу','терпение')
    await ctx.update_data(chosen_avatar='https://cdn.example/saved.png')
    await call_handler(admin_router,'song_review_input',message_event(text),ctx)
    d=await ctx.get_data()
    assert d['song_review_lyrics']==text and not d['song_review_approved']
    assert d['chosen_avatar']=='https://cdn.example/saved.png'
    writer.assert_awaited_once()


@pytest.mark.parametrize('kind,target',[('cartoon','theme_mother'),('music','mode_mother')])
async def test_failed_payment_link_retry_keeps_same_reviewed_order(credit_db,kind,target):
    from types import SimpleNamespace
    link=AsyncMock(side_effect=[TimeoutError('payment provider timeout'),'https://shop.payform.ru/retry'])
    payments=SimpleNamespace(create_payment_link=link,create_musical_payment_link=link)
    writer=AsyncMock(return_value=WORDS)
    router=handlers.create_router(admin_user_id=999,payments=payments,lyric_writer=writer)
    ctx=await ready_context()
    await ctx.update_data(cartoon_flow='short') if kind=='cartoon' else await ctx.update_data(video_flow=True,video_product='direct')
    payload=target if kind=='cartoon' else 'video_style:'+target
    await dispatch_callback(router,callback_event(payload),ctx)
    token=(await ctx.get_data())['song_setup_token']
    await dispatch_callback(router,callback_event('song_setup:voice:female:'+token),ctx)
    await call_handler(router,'card_song_details',message_event('Ольга; спасибо за заботу'),ctx)
    token=(await ctx.get_data())['song_review_token']
    await dispatch_callback(router,callback_event('song_review:approve:'+token),ctx)
    key=(await ctx.get_data())['cartoon_order_id' if kind=='cartoon' else 'music_prepaid_id']
    await dispatch_callback(router,callback_event(payload),ctx)
    assert link.await_count==2 and link.await_args_list[0]==link.await_args_list[1]
    assert (await ctx.get_data())['cartoon_order_id' if kind=='cartoon' else 'music_prepaid_id']==key
    assert (await ctx.get_data())['song_review_lyrics']==WORDS
    writer.assert_awaited_once()


async def test_public_review_does_not_expose_admin_rewrite_button(credit_db,payments):
    router,ctx,_=await draft(payments)
    e=callback_event('song_review:voice_menu:'+(await ctx.get_data())['song_review_token'])
    await dispatch_callback(router,e,ctx)
    await dispatch_callback(router,callback_event('song_review:voice:female:'+(await ctx.get_data())['song_review_token']),ctx)
    show=callback_event('unused');await song_review.show_text(show,ctx)
    assert 'Переписать текст песни' not in str(show.send.await_args_list)
    assert 'Вставить свой текст' not in str(show.send.await_args_list)
    assert 'Изменить мои слова' in str(show.send.await_args_list)


@pytest.mark.parametrize('kind,target',[('cartoon','theme_mother'),('music','mode_mother')])
async def test_mother_card_reviews_full_song_without_technical_sequence_message(credit_db,payments,kind,target):
    writer=AsyncMock(return_value=WORDS)
    router=handlers.create_router(admin_user_id=999,payments=payments,lyric_writer=writer)
    ctx=await ready_context()
    await ctx.update_data(cartoon_flow='short') if kind=='cartoon' else await ctx.update_data(video_flow=True,video_product='direct')
    event=callback_event(target if kind=='cartoon' else 'video_style:'+target)
    await dispatch_callback(router,event,ctx)
    assert 'Сначала выберем голос' not in str(event.send.await_args_list)
    token=(await ctx.get_data())['song_setup_token']
    await dispatch_callback(router,callback_event('song_setup:voice:female:'+token),ctx)
    await call_handler(router,'card_song_details',message_event('Ольга; спасибо за заботу'),ctx)
    assert (await ctx.get_data())['song_review_format']=='gift'
    assert '2-3 minutes' in writer.await_args.args[1] and '35 seconds' not in writer.await_args.args[1]


@pytest.mark.parametrize('interrupted',[False,True])
@pytest.mark.parametrize('occasion',['mother','mother_bday'])
async def test_paid_customer_selects_performance_before_video_and_resumes_without_new_music(credit_db,payments,monkeypatch,interrupted,occasion):
    import asyncio
    import mother_story
    from maxapi.types import AttachmentUpload
    from test_handlers import cartoon_photo
    router,ctx,writer=await draft(payments,scenario='gift_scen_'+occasion)
    await dispatch_callback(router,callback_event('song_review:cartoon:'+(await ctx.get_data())['song_review_token']),ctx)
    key=(await ctx.get_data())['cartoon_order_id']
    await database.mark_cartoon_paid(key,'auto-render-paid')
    picture=AsyncMock(return_value='https://cdn.example/mom.png')
    song=AsyncMock(return_value='known-song-id')
    preview=AsyncMock(return_value='https://cdn.example/song-preview.mp3');audio_upload=AsyncMock(return_value=AttachmentUpload(type='audio',payload={'token':'audio'}))
    monkeypatch.setattr(handlers,'create_audio_preview',preview)
    monkeypatch.setattr(handlers,'upload_audio_for_max',audio_upload)
    monkeypatch.setattr(handlers,'count_people_in_photo',AsyncMock(return_value=1))
    monkeypatch.setattr(handlers,'create_cartoon_avatar_preview',AsyncMock(return_value='https://cdn.example/preview.png'))
    monkeypatch.setattr(handlers,'upload_image_for_max',AsyncMock(return_value=AttachmentUpload(type='image',payload={'token':'image'})))
    monkeypatch.setattr(handlers,'upload_video_for_max',AsyncMock(return_value=AttachmentUpload(type='video',payload={'token':'video'})))
    calls=0
    async def render(data,reference,audio,*,checkpoint,progress):
        nonlocal calls
        calls+=1
        if interrupted and calls==1:
            data['cartoon_kling_story']={'source':['minute','https://cdn.example/mom.png'],'shots':[{'task_id':'already-paid-heygen-id'}]}
            await checkpoint()
            raise asyncio.CancelledError()
        if interrupted:assert data['cartoon_kling_story']['shots'][0]['task_id']=='already-paid-heygen-id'
        return 'https://cdn.example/minute.mp4'
    render_mock=AsyncMock(side_effect=render);monkeypatch.setattr(mother_story,'generate',render_mock)
    router=handlers.create_router(admin_user_id=999,payments=payments,lyric_writer=writer,avatar_generator=picture,
        song_generator=song,song_waiter=AsyncMock(return_value={'data':{'musics':[{'audioUrl':'https://cdn.example/full-song.mp3'}]}}))
    await dispatch_callback(router,callback_event('cartoon_resume:'+key),ctx)
    await call_handler(router,'collect_cartoon_photo',cartoon_photo(),ctx)
    await dispatch_callback(router,callback_event('avatar_ok_1'),ctx)
    render_mock.assert_not_awaited()
    async def choose():
        token=(await ctx.get_data())['cartoon_preview_token']
        await dispatch_callback(router,callback_event('cartoon_song_pick:2:'+token),ctx)
        event=callback_event('confirm_video_2')
        await dispatch_callback(router,event,ctx)
        return event
    if interrupted:
        with pytest.raises(asyncio.CancelledError):await choose()
        assert (await database.get_cartoon_order(key))['status']=='paid'
        await ctx.clear()
        await dispatch_callback(router,callback_event('cartoon_resume:'+key),ctx)
    event=await choose()
    assert (await database.get_cartoon_order(key))['status']=='completed'
    assert 'video' in str(event.send.await_args_list)
    assert preview.await_count==2 and audio_upload.await_count>=2
    picture.assert_awaited_once();assert song.await_count==2;writer.assert_awaited_once()


async def test_mother_song_from_short_menu_is_full_before_minute_cartoon_invoice(credit_db,payments):
    writer=AsyncMock(return_value=WORDS)
    router=handlers.create_router(admin_user_id=999,payments=payments,lyric_writer=writer)
    ctx=await ready_context()
    await dispatch_callback(router,callback_event('audio_fun_mother'),ctx)
    token=(await ctx.get_data())['song_setup_token']
    await dispatch_callback(router,callback_event('song_setup:genre:pop:'+token),ctx)
    await dispatch_callback(router,callback_event('song_setup:voice:female:'+token),ctx)
    await call_handler(router,'collect_song_details',message_event('Ольга; спасибо за заботу'),ctx)
    d=await ctx.get_data()
    assert d['song_review_format']=='gift' and '2-3 minutes' in d['song_review_tags']
    assert d['song_review_voice']=='female' and not d.get('cartoon_order_id')
    await dispatch_callback(router,callback_event('song_review:cartoon:'+d['song_review_token']),ctx)
    order=await database.get_cartoon_order((await ctx.get_data())['cartoon_order_id'])
    assert order['status']=='pending' and order['amount_kopecks']==59000
    writer.assert_awaited_once()


async def test_admin_music_and_scene_checkpoints_survive_bot_restart(credit_db,monkeypatch):
    import asyncio
    import mother_story
    import mother_admin_sessions
    from maxapi.types import AttachmentUpload
    song=AsyncMock(return_value='retained-song-id');writer=AsyncMock()
    monkeypatch.setattr(handlers,'create_audio_preview',AsyncMock(return_value='https://cdn.example/preview.mp3'))
    monkeypatch.setattr(handlers,'upload_audio_for_max',AsyncMock(return_value=AttachmentUpload(type='audio',payload={'token':'audio'})))
    async def interrupted(data,reference,audio,*,checkpoint,progress):
        data['cartoon_kling_story']={'source':['minute',reference],'shots':[{'task_id':'retained-video-id'}]}
        await checkpoint();raise asyncio.CancelledError()
    monkeypatch.setattr(mother_story,'generate',AsyncMock(side_effect=interrupted))
    router=handlers.create_router(admin_user_id=456,lyric_writer=writer,song_generator=song,
        song_waiter=AsyncMock(return_value={'data':{'musics':[{'audioUrl':'https://cdn.example/full.mp3'}]}}))
    ctx=await ready_context()
    await ctx.update_data(cartoon_session_id='persistent-admin-test',cartoon_flow='mother',cartoon_theme='theme_mother',
        chosen_avatar='https://cdn.example/mom.png',chosen_song='audio_fun_mother',
        chosen_song_phrase='Ольга; спасибо',video_song_phrase='Ольга; спасибо',song_attempt=1,
        song_review_user=456,song_review_approved=True,song_review_details='Ольга; спасибо',song_review_lyrics=WORDS,
        song_review_voice='female',song_review_tags='female vocals',song_review_title='Маме',song_review_notes='notes')
    await ctx.set_state(handlers.PosterStates.waiting_for_cartoon_song_phrase)
    await mother_admin_sessions.save(456,await ctx.get_data(),await ctx.get_state())
    await call_handler(router,'mother_resume',message_event('/mother_resume'),ctx)
    token=(await ctx.get_data())['cartoon_preview_token']
    await dispatch_callback(router,callback_event('cartoon_song_pick:1:'+token),ctx)
    with pytest.raises(asyncio.CancelledError):await dispatch_callback(router,callback_event('confirm_video_2'),ctx)
    snapshot,_=await mother_admin_sessions.latest(456)
    assert snapshot['mother_song_variants']['items']['1']['job_id']=='retained-song-id'
    assert snapshot['cartoon_kling_story']['shots'][0]['task_id']=='retained-video-id'
    assert await mother_admin_sessions.latest(999) is None
    await ctx.clear()
    await call_handler(router,'mother_resume',message_event('/mother_resume'),ctx)
    assert (await ctx.get_data())['cartoon_kling_story']['shots'][0]['task_id']=='retained-video-id'
    assert song.await_count==2;writer.assert_not_awaited()


async def test_admin_explicit_minute_upgrade_archives_old_clips_and_keeps_picture_and_music(credit_db,monkeypatch):
    from maxapi.types import AttachmentUpload
    monkeypatch.setattr(handlers,'create_audio_preview',AsyncMock(return_value='https://cdn.example/preview.mp3'))
    monkeypatch.setattr(handlers,'upload_audio_for_max',AsyncMock(return_value=AttachmentUpload(type='audio',payload={'token':'audio'})))
    songs=AsyncMock();pictures=AsyncMock()
    router=handlers.create_router(admin_user_id=456,song_generator=songs,avatar_generator=pictures)
    ctx=await ready_context();old={'source':['mother-one-character-heygen-seven-shots-v1','https://cdn.example/mom.png'],
        'shots':[{'image_url':'https://cdn.example/old.png','video_submitting':True,'video_url':'https://cdn.example/old.mp4'}]}
    await ctx.update_data(cartoon_theme='theme_mother',cartoon_flow='mother',cartoon_session_id='upgrade-test',
        chosen_avatar='https://cdn.example/mom.png',chosen_song='audio_fun_mother',chosen_song_phrase='Ольга; спасибо',
        song_1_url='https://cdn.example/full.mp3',cartoon_kling_story=old,video_merged_url='https://cdn.example/old-final.mp4',
        song_review_user=456)
    await ctx.set_state(handlers.PosterStates.waiting_for_mode)
    await call_handler(router,'mother_minute',message_event('/mother_minute'),ctx)
    data=await ctx.get_data()
    assert data['chosen_avatar']=='https://cdn.example/mom.png' and data['song_1_url']=='https://cdn.example/full.mp3'
    assert data['cartoon_kling_story'] is None and data['video_merged_url'] is None
    assert data['cartoon_kling_story_archive'][0]['cartoon_kling_story']==old
    assert data['cartoon_kling_story_archive'][0]['video_merged_url']=='https://cdn.example/old-final.mp4'
    assert await ctx.get_state()==handlers.PosterStates.waiting_for_cartoon_music_review
    songs.assert_not_awaited();pictures.assert_not_awaited()

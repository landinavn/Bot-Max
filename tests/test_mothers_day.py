"""Personal lyrics, one-character story, retained HeyGen IDs and real local assembly."""
import json
import subprocess
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock,Mock

import httpx
import pytest
import respx

import handlers
import database
import audio_service as audio
import image_service
import mother_story as story
import mothers_day as mother
import media_service as media
from config import Config
from test_prodamus import payments,payment_config
from test_handlers import callback_event,ready_context,dispatch_callback,event_replies,call_handler,message_event


@pytest.mark.parametrize('fmt',['fun','gift'])
def test_song_shared_request_preserves_personal_source_and_day(fmt):
    notes,tags,title=audio.build_song_request('Людмила; спасибо за заботу',mother.TITLE,fmt)
    assert 'Людмила; спасибо за заботу' in notes and title.startswith(mother.TITLE)
    assert '35 seconds' in tags if fmt=='fun' else '2-3 minutes' in tags
    assert 'днём рождения' not in notes


@respx.mock
@pytest.mark.parametrize('grandchildren',[False,True])
async def test_personalizes_only_name_and_two_lines_of_approved_song(monkeypatch,grandchildren):
    monkeypatch.setattr(audio.Config,'from_env',lambda:Config(token='test',photo_path=Path('unused'),image_api_key='test-key'))
    details='Людмила; спасибо за заботу'+('; от внуков' if grandchildren else '')
    notes,tags,title=audio.build_song_request(details,mother.TITLE,'fun')
    lines=['От внуков эта песня благодарность,','Любимой бабушке за каждый добрый час.'] if grandchildren else []
    route=respx.post('https://api.openai.com/v1/chat/completions').mock(return_value=httpx.Response(200,json={
        'choices':[{'finish_reason':'stop','message':{'content':json.dumps({'name_genitive':'Людмилы','personal_lines':lines})}}]}))
    text=await audio.write_song_lyrics(notes,tags,title)
    assert text==mother.render_approved_song('Людмилы',lines,short=True)
    assert mother.APPROVED_CHORUS in text
    body=json.loads(route.calls[0].request.content)
    assert body['response_format']=={'type':'json_object'}
    assert 'НЕ пиши новую песню' in body['messages'][0]['content']
    payload=json.loads(body['messages'][1]['content'])
    assert payload['required_chorus_phrase'] is None and payload['details']==details


@respx.mock
@pytest.mark.parametrize('wrong',['С днём рождения, Людмила!','С Днём матери, бабушка!'])
async def test_rejects_birthday_or_invented_grandchildren_before_paid_music(monkeypatch,wrong):
    monkeypatch.setattr(audio.Config,'from_env',lambda:Config(token='test',photo_path=Path('unused'),image_api_key='test-key'))
    notes,tags,title=audio.build_song_request('Людмила; спасибо за заботу',mother.TITLE,'fun')
    respx.post('https://api.openai.com/v1/chat/completions').mock(return_value=httpx.Response(200,json={
        'choices':[{'finish_reason':'stop','message':{'content':json.dumps({'name_genitive':'Людмилы','personal_lines':[wrong,'Спасибо за твою заботу.']})}}]}))
    with pytest.raises(audio.AudioServiceError):await audio.write_song_lyrics(notes,tags,title)


async def test_ordinary_and_music_menu_and_song_options_are_wired(credit_db):
    assert (mother.TITLE,'mode_mother') in handlers.CATEGORY_MENUS['cat_cards'][2]
    assert 'mode_mother' in image_service.POSTCARD_PROMPTS
    assert handlers.AUDIO_FUN_SCENARIOS['audio_fun_mother'][0]==mother.TITLE
    assert handlers.AUDIO_GIFT_SCENARIOS['gift_scen_mother'][0]==mother.TITLE
    assert handlers.GENERATION_EVENTS['mode_mother']=='gen_mother'
    e=callback_event('video_style:cat_cards')
    c=await ready_context();await c.update_data(video_flow=True,video_product='direct',video_amount_kopecks=29900)
    await dispatch_callback(handlers.create_router(admin_user_id=456),e,c)
    buttons=e.send.await_args.kwargs['attachments'][0].payload.buttons
    payloads=[b.payload for row in buttons for b in row]
    assert 'video_style:mode_mother' not in payloads
    assert 'video_style:mode_mother_bday' in payloads


async def test_non_admin_mother_cartoon_still_requires_payment(credit_db):
    e=callback_event('theme_mother');c=await ready_context()
    await c.update_data(cartoon_flow='short',cartoon_amount_kopecks=59000)
    await dispatch_callback(handlers.create_router(admin_user_id=999),e,c)
    assert not (await c.get_data()).get('chosen_avatar')


@pytest.fixture
def renderer(monkeypatch):
    config=SimpleNamespace(storage_bucket='bucket')
    monkeypatch.setenv('HEYGEN_API_KEY','test-key')
    monkeypatch.setattr(Config,'from_env',lambda:config)
    storage=Mock();monkeypatch.setattr(media,'_storage_client',lambda _:storage)
    monkeypatch.setattr(media,'_upload_video',Mock(return_value='https://cdn.example/mother.mp4'))
    async def download(http,url,path,limit):path.write_bytes(b'fixture')
    monkeypatch.setattr(media,'_download',download)
    async def duration(path):
        return 120. if path.suffix=='.mp3' else 10. if path.name.startswith('scene-') else 60.
    monkeypatch.setattr(media,'audio_duration',AsyncMock(side_effect=duration))
    monkeypatch.setattr(story,'video_duration',AsyncMock(side_effect=duration))
    monkeypatch.setattr('song_alignment.detect_song_start',AsyncMock(return_value=2.))
    monkeypatch.setattr(image_service,'_store_image',Mock(side_effect=lambda client,config,path:'https://cdn.example/'+path.name))
    async def end(video,target):target.write_bytes(b'png')
    monkeypatch.setattr(story,'extract_last_frame',AsyncMock(side_effect=end))
    async def render(clips,song,target,duration,**kwargs):
        assert len(clips)==6 and duration==60. and kwargs['audio_start'] in (2.,13.);target.write_bytes(b'mp4')
    monkeypatch.setattr(story,'render_local',AsyncMock(side_effect=render))
    images=AsyncMock(side_effect=['https://cdn.example/image-'+str(i)+'.png' for i in range(6)])
    monkeypatch.setattr(image_service,'generate_photosession_frame',images)
    submit=AsyncMock(side_effect=['heygen-scene-'+str(i) for i in range(6)])
    monkeypatch.setattr(story,'submit_animation',submit)
    poll=AsyncMock(side_effect=['https://cdn.example/video-'+str(i)+'.mp4' for i in range(6)])
    monkeypatch.setattr(story,'poll_animation',poll)
    return images,submit,poll


async def test_six_different_ten_second_mother_shots_are_retained_and_reused(renderer):
    images,submit,poll=renderer;data={};snapshots=[]
    async def checkpoint():snapshots.append(json.loads(json.dumps(data)))
    result=await story.generate(data,'https://cdn.example/mom.png','https://cdn.example/song.mp3',checkpoint=checkpoint,progress=AsyncMock())
    assert result=='https://cdn.example/mother.mp4'
    assert images.await_count==submit.await_count==poll.await_count==6
    assert len({c.args[1] for c in images.await_args_list})==6
    assert all('Exactly ONE' in c.args[1] and '16:9' in c.args[1] for c in images.await_args_list)
    assert all(c.args[0]=='https://cdn.example/mom.png' for c in images.await_args_list)
    assert all('style_reference_urls' not in c.kwargs and 'style_reference_url' not in c.kwargs for c in images.await_args_list)
    assert all(c.kwargs['duration']==10 for c in submit.await_args_list)
    assert all(x['task_id'].startswith('heygen-') and x['request_key'] for x in data['cartoon_kling_story']['shots'])
    assert any(s['cartoon_kling_story']['shots'][0].get('video_submitting') and not s['cartoon_kling_story']['shots'][0].get('task_id') for s in snapshots)
    assert await story.generate(data,'https://cdn.example/mom.png','https://cdn.example/song.mp3',checkpoint=checkpoint,progress=AsyncMock())==result
    assert submit.await_count==images.await_count==6


async def test_unknown_paid_submission_is_not_repeated(renderer):
    images,submit,poll=renderer;data={}
    submit.side_effect=TimeoutError('unknown result')
    with pytest.raises(TimeoutError):await story.generate(data,'https://cdn.example/mom.png','https://cdn.example/song.mp3',checkpoint=AsyncMock(),progress=AsyncMock())
    with pytest.raises(ValueError,match='неизвестен'):await story.generate(data,'https://cdn.example/mom.png','https://cdn.example/song.mp3',checkpoint=AsyncMock(),progress=AsyncMock())
    submit.assert_awaited_once();images.assert_awaited_once();poll.assert_not_awaited()


async def test_known_heygen_id_resumes_poll_without_second_submission(renderer):
    images,submit,poll=renderer;data={}
    poll.side_effect=TimeoutError('still rendering')
    with pytest.raises(TimeoutError):await story.generate(data,'https://cdn.example/mom.png','https://cdn.example/song.mp3',checkpoint=AsyncMock(),progress=AsyncMock())
    assert data['cartoon_kling_story']['shots'][0]['task_id']=='heygen-scene-0'
    poll.side_effect=['https://cdn.example/v-'+str(i)+'.mp4' for i in range(6)]
    await story.generate(data,'https://cdn.example/mom.png','https://cdn.example/song.mp3',checkpoint=AsyncMock(),progress=AsyncMock())
    assert images.await_count==submit.await_count==6 and poll.await_count==7


async def test_too_short_song_stops_before_paid_visuals(renderer,monkeypatch):
    monkeypatch.setattr(media,'audio_duration',AsyncMock(return_value=12.))
    with pytest.raises(ValueError,match='короче'):await story.generate({},'https://cdn.example/mom.png','https://cdn.example/song.mp3',checkpoint=AsyncMock(),progress=AsyncMock())
    renderer[0].assert_not_awaited();renderer[1].assert_not_awaited()


async def test_heygen_uses_existing_silent_image_api_not_avatar_or_kling():
    from heygen import HeyGenAdapter
    adapter=HeyGenAdapter('test')
    adapter.request=AsyncMock(return_value={'video_id':'existing-contract-id'})
    assert await story.submit_animation(adapter,'https://cdn.example/mom.png','Mother arranges flowers','unique-key')=='existing-contract-id'
    c=adapter.request.await_args
    assert c.args==('POST','/v3/models/videos')
    assert c.kwargs['payload']['model']=='heygen-video-1' and c.kwargs['payload']['mode']=='image_to_video'
    assert c.kwargs['payload']['duration']==10 and c.kwargs['idempotency']=='unique-key'
    assert 'audio_url' not in c.kwargs['payload'] and 'avatar_id' not in c.kwargs['payload']


async def test_real_ffmpeg_legacy_35_second_story_still_renders(tmp_path):
    clip=tmp_path/'clip.mp4';song=tmp_path/'song.mp3';target=tmp_path/'output.mp4'
    subprocess.run(['ffmpeg','-v','error','-y','-f','lavfi','-i','color=c=blue:s=160x90:r=25:d=10','-c:v','libx264','-pix_fmt','yuv420p',str(clip)],check=True)
    subprocess.run(['ffmpeg','-v','error','-y','-f','lavfi','-i','sine=frequency=440:duration=70','-c:a','libmp3lame',str(song)],check=True)
    await story.render_local([clip]*7,song,target,35,shot_seconds=5)
    probe=json.loads(subprocess.check_output(['ffprobe','-v','error','-show_entries','format=duration:stream=codec_name,width,height,start_time,pix_fmt','-of','json',str(target)],text=True))
    assert abs(float(probe['format']['duration'])-35)<.15
    video,audio=probe['streams']
    assert (video['width'],video['height'],video['pix_fmt'])==(1280,720,'yuv420p')
    assert audio['codec_name']=='aac' and float(audio['start_time'])<.05


async def test_new_mother_price_receipt_and_statistics_survive_restart(credit_db,payments):
    from mother_stats import record,sales
    from prodamus import parse_form_fields
    from urllib.parse import parse_qsl,urlsplit
    key=await database.create_cartoon_order('cartoon_mother_price',456,'theme_mother','mother')
    order=await database.get_cartoon_order(key)
    assert order['amount_kopecks']==59000
    link=await payments.create_payment_link(456,order_id=key,amount=590,description=order['product_name'])
    form=parse_form_fields(parse_qsl(urlsplit(link).query,keep_blank_values=True))
    receipt={'order_num':key,'order_id':'paid-mother-590','sum':'590.00','currency':'rub','payment_status':'success',
        'products':form['products'],'_param_user_id':'456','_param_credits':'0','_param_package_id':order['package_id']}
    validated,_=await payments.validate_payment(receipt)
    assert validated['tariff']=='mother'
    assert await database.mark_cartoon_paid(key,'paid-mother-590')
    assert not await database.mark_cartoon_paid(key,'paid-mother-590')
    assert await record(456,'mother_cartoon_ready',key)
    assert not await record(456,'mother_cartoon_ready',key)
    await record(999,'mother_cartoon_ready','admin-test')
    await database.init_db()
    counters=await database.get_all_counters(admin_id=999)
    assert counters['buy_cartoon_mother_590']==1 and counters['mother_cartoon_ready']==1
    paid=await sales(999)
    assert paid['cartoon_paid']==1 and paid['cartoon_revenue']==59000
    e=message_event('/stats')
    e.get_ids=lambda:(123,999)
    await call_handler(handlers.create_router(admin_user_id=999),'stats',e)
    assert 'День матери' in str(e.message.answer.await_args_list)
    assert '590' in str(e.message.answer.await_args_list)
    assert all(len(c.args[0])<4000 for c in e.message.answer.await_args_list)


async def test_cannot_buy_new_mother_story_at_old_short_price(credit_db):
    with pytest.raises(ValueError):await database.create_cartoon_order('cartoon_wrong_price',456,'theme_mother','short')
    with pytest.raises(ValueError):await database.create_cartoon_order('cartoon_wrong_theme',456,'theme_ny','mother')
    key=await database.create_cartoon_order('cartoon_other_theme',456,'theme_ny','short')
    assert (await database.get_cartoon_order(key))['amount_kopecks']==39000


@respx.mock
async def test_full_song_is_the_exact_approved_text_not_free_model_output(monkeypatch):
    monkeypatch.setattr(audio.Config,'from_env',lambda:Config(token='test',photo_path=Path('unused'),image_api_key='test-key'))
    notes,tags,title=audio.build_song_request('Ольга; спасибо за заботу',mother.TITLE,'gift')
    route=respx.post('https://api.openai.com/v1/chat/completions').mock(return_value=httpx.Response(200,json={
        'choices':[{'finish_reason':'stop','message':{'content':'{"name_genitive":"Ольги","personal_lines":[]}'}}]}))
    text=await audio.write_song_lyrics(notes,tags,title)
    assert text==mother.render_approved_song('Ольги',[])
    assert text.count(mother.APPROVED_CHORUS)==3
    assert mother.APPROVED_VERSE_2 in text and mother.APPROVED_BRIDGE in text
    assert 'Для Ольги эта песня — благодарность' in text
    # Free-form lyrics and unexpected fields cannot silently replace the agreed song.
    for bad in ['[Verse 1]\nДругая песня','{"name_genitive":"Ольги","personal_lines":[],"lyrics":"Другая песня"}']:
        route.mock(return_value=httpx.Response(200,json={'choices':[{'finish_reason':'stop','message':{'content':bad}}]}))
        with pytest.raises(audio.AudioServiceError,match='персонализировать'):await audio.write_song_lyrics(notes,tags,title)


@pytest.mark.parametrize('lines',[['Одна строка'],['Новая\nстрока','Вторая'],['[Chorus]','Подмена'],['x'*141,'Текст']])
def test_personal_lines_cannot_replace_stanzas_or_insert_tags(lines):
    with pytest.raises(ValueError):mother.render_approved_song('Ольги',lines)


def test_personalization_keeps_all_other_lines_and_never_adds_grandchildren():
    base=mother.render_approved_song('Ольги',[])
    lines=['За то, что ты любила наши встречи,','За тёплый чай и добрые слова.']
    updated=mother.render_approved_song('Ольги',lines)
    assert updated==base.replace('Но в каждом добром шаге, в каждом взлёте —\nТвоя любовь, что силы нам дала.','\n'.join(lines))
    assert 'внук' not in base.lower() and 'бабуш' not in base.lower()


async def test_new_audio_reuses_all_six_paid_scenes_and_refreshes_only_alignment(renderer):
    import song_alignment
    images,submit,poll=renderer;data={}
    for audio in ('https://cdn.example/first.mp3','https://cdn.example/second.mp3'):
        await story.generate(data,'https://cdn.example/mom.png',audio,checkpoint=AsyncMock(),progress=AsyncMock())
    assert images.await_count==submit.await_count==poll.await_count==6
    assert song_alignment.detect_song_start.await_count==2


async def test_completed_legacy_story_is_kept_without_submitting_new_minute(renderer):
    import mother_story_legacy
    images,submit,poll=renderer
    data={'cartoon_kling_story':{'source':[mother_story_legacy.POLICY,'https://cdn.example/mom.png'],
        'shots':[{} for _ in range(7)],'result_audio':'https://cdn.example/old.mp3','result_url':'https://cdn.example/old-35.mp4'}}
    assert await story.generate(data,'https://cdn.example/mom.png','https://cdn.example/old.mp3',checkpoint=AsyncMock(),progress=AsyncMock())=='https://cdn.example/old-35.mp4'
    images.assert_not_awaited();submit.assert_not_awaited();poll.assert_not_awaited()


async def test_issued_35_second_receipt_still_valid_after_minute_tariff(credit_db,payments):
    from urllib.parse import parse_qsl,urlsplit
    from prodamus import parse_form_fields
    import aiosqlite
    key=await database.create_cartoon_order('cartoon_legacy_mother_invoice',456,'theme_mother','mother')
    old='Мульт-открытка «День матери» (35 сек)'
    async with aiosqlite.connect(database.DATABASE_PATH) as db:
        await db.execute('UPDATE cartoon_orders SET product_name=?,amount_kopecks=79000 WHERE order_id=?',(old,key));await db.commit()
    link=await payments.create_payment_link(456,order_id=key,amount=790,description=old)
    form=parse_form_fields(parse_qsl(urlsplit(link).query,keep_blank_values=True))
    receipt={'order_num':key,'order_id':'old-35-paid','sum':'790.00','currency':'rub','payment_status':'success',
        'products':form['products'],'_param_user_id':'456','_param_credits':'0','_param_package_id':'cartoon_mother'}
    order,_=await payments.validate_payment(receipt)
    assert order['product_name']==old and order['amount_kopecks']==79000


async def test_alignment_timeout_has_two_bounded_attempts_and_never_starts_visuals(renderer):
    import song_alignment
    song_alignment.detect_song_start.side_effect=TimeoutError('unknown transcription response')
    data={}
    with pytest.raises(song_alignment.SongAlignmentError):
        await story.generate(data,'https://cdn.example/mom.png','https://cdn.example/song.mp3',checkpoint=AsyncMock(),progress=AsyncMock())
    with pytest.raises(song_alignment.SongAlignmentError,match='Две попытки'):
        await story.generate(data,'https://cdn.example/mom.png','https://cdn.example/song.mp3',checkpoint=AsyncMock(),progress=AsyncMock())
    assert song_alignment.detect_song_start.await_count==2
    assert data['cartoon_kling_story']['alignment_submitting'] is None
    renderer[0].assert_not_awaited();renderer[1].assert_not_awaited()


async def test_short_video_is_not_looped_or_frozen_to_fill_ten_seconds(renderer,monkeypatch):
    async def duration(path):return 120. if path.suffix=='.mp3' else 8.
    monkeypatch.setattr(story,'video_duration',AsyncMock(return_value=8.))
    data={}
    with pytest.raises(ValueError,match='короче 10'):await story.generate(data,'https://cdn.example/mom.png','https://cdn.example/song.mp3',checkpoint=AsyncMock(),progress=AsyncMock())
    assert data['cartoon_kling_story']['shots'][0]['task_id']=='heygen-scene-0'
    renderer[0].assert_awaited_once();renderer[1].assert_awaited_once()

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


def lyrics(chorus='С Днём матери, Людмила! Спасибо за заботу.'):
    return '[Verse 1]\nВ окне сияет свет\n[Chorus]\n'+chorus+'\n[Verse 2]\nТы даришь нам тепло\n[Chorus]\n'+chorus+'\n[Bridge]\nПусть счастье будет рядом\n[Chorus]\n'+chorus


@pytest.mark.parametrize('fmt',['fun','gift'])
def test_song_shared_request_preserves_personal_source_and_day(fmt):
    notes,tags,title=audio.build_song_request('Людмила; спасибо за заботу',mother.TITLE,fmt)
    assert 'Людмила; спасибо за заботу' in notes and title.startswith(mother.TITLE)
    assert '35 seconds' in tags if fmt=='fun' else '2-3 minutes' in tags
    assert 'днём рождения' not in notes


@respx.mock
@pytest.mark.parametrize('grandchildren',[False,True])
async def test_lyrics_allow_inflection_and_only_requested_grandchildren(monkeypatch,grandchildren):
    config=Config(token='test',photo_path=Path('unused'),image_api_key='test-text-key')
    monkeypatch.setattr(audio.Config,'from_env',lambda:config)
    details='Людмила; благодарность за заботу'+('; от внуков' if grandchildren else '')
    notes,tags,title=audio.build_song_request(details,mother.TITLE,'fun')
    text=lyrics('С Днём матери! Для Людмилы наши тёплые слова.'+(' Любимая бабушка, от внуков!' if grandchildren else ''))
    route=respx.post('https://api.openai.com/v1/chat/completions').mock(return_value=httpx.Response(200,json={
        'id':'lyrics','object':'chat.completion','created':1,'model':'gpt-4o-mini',
        'choices':[{'index':0,'finish_reason':'stop','message':{'role':'assistant','content':text}}]}))
    assert await audio.write_song_lyrics(notes,tags,title)==text
    body=json.loads(route.calls[0].request.content)
    instruction=body['messages'][0]['content']
    assert 'Разрешено грамматически' in instruction and 'НЕ ко дню рождения' in instruction
    assert '35' in tags and '45–60' in instruction
    assert json.loads(body['messages'][1]['content'])['required_chorus_phrase'] is None


@respx.mock
@pytest.mark.parametrize('wrong',['С днём рождения, Людмила!','С Днём матери, бабушка!'])
async def test_rejects_birthday_or_invented_grandchildren_before_paid_music(monkeypatch,wrong):
    monkeypatch.setattr(audio.Config,'from_env',lambda:Config(token='test',photo_path=Path('unused'),image_api_key='test-key'))
    notes,tags,title=audio.build_song_request('Людмила; спасибо за заботу',mother.TITLE,'fun')
    respx.post('https://api.openai.com/v1/chat/completions').mock(return_value=httpx.Response(200,json={
        'choices':[{'finish_reason':'stop','message':{'content':lyrics(wrong)}}]}))
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
    assert 'video_style:mode_mother' in str(e.send.await_args_list)


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
    monkeypatch.setattr(media,'audio_duration',AsyncMock(return_value=35.))
    async def render(clips,song,target,duration):
        assert len(clips)==7 and duration==35.;target.write_bytes(b'mp4')
    monkeypatch.setattr(story,'render_local',AsyncMock(side_effect=render))
    images=AsyncMock(side_effect=['https://cdn.example/image-'+str(i)+'.png' for i in range(7)])
    monkeypatch.setattr(image_service,'generate_photosession_frame',images)
    submit=AsyncMock(side_effect=['heygen-scene-'+str(i) for i in range(7)])
    monkeypatch.setattr(story,'submit_animation',submit)
    poll=AsyncMock(side_effect=['https://cdn.example/video-'+str(i)+'.mp4' for i in range(7)])
    monkeypatch.setattr(story,'poll_animation',poll)
    return images,submit,poll


async def test_seven_different_single_mother_shots_are_retained_and_reused(renderer):
    images,submit,poll=renderer;data={};snapshots=[]
    async def checkpoint():snapshots.append(json.loads(json.dumps(data)))
    result=await story.generate(data,'https://cdn.example/mom.png','https://cdn.example/song.mp3',checkpoint=checkpoint,progress=AsyncMock())
    assert result=='https://cdn.example/mother.mp4'
    assert images.await_count==submit.await_count==poll.await_count==7
    assert len({c.args[1] for c in images.await_args_list})==7
    assert all('EXACTLY ONE' in c.args[1] and '16:9' in c.args[1] for c in images.await_args_list)
    assert images.await_args_list[1].kwargs['style_reference_url']=='https://cdn.example/image-0.png'
    assert all(x['task_id'].startswith('heygen-') and x['request_key'] for x in data['cartoon_kling_story']['shots'])
    assert any(s['cartoon_kling_story']['shots'][0].get('video_submitting') and not s['cartoon_kling_story']['shots'][0].get('task_id') for s in snapshots)
    assert await story.generate(data,'https://cdn.example/mom.png','https://cdn.example/song.mp3',checkpoint=checkpoint,progress=AsyncMock())==result
    assert submit.await_count==images.await_count==7


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
    poll.side_effect=['https://cdn.example/v-'+str(i)+'.mp4' for i in range(7)]
    await story.generate(data,'https://cdn.example/mom.png','https://cdn.example/song.mp3',checkpoint=AsyncMock(),progress=AsyncMock())
    assert images.await_count==submit.await_count==7 and poll.await_count==8


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
    assert c.kwargs['payload']['duration']==5 and c.kwargs['idempotency']=='unique-key'
    assert 'audio_url' not in c.kwargs['payload'] and 'avatar_id' not in c.kwargs['payload']


async def test_real_ffmpeg_35_seconds_widescreen_song_from_zero(tmp_path):
    clip=tmp_path/'clip.mp4';song=tmp_path/'song.mp3';target=tmp_path/'output.mp4'
    subprocess.run(['ffmpeg','-v','error','-y','-f','lavfi','-i','color=c=blue:s=160x90:r=25:d=5','-c:v','libx264','-pix_fmt','yuv420p',str(clip)],check=True)
    subprocess.run(['ffmpeg','-v','error','-y','-f','lavfi','-i','sine=frequency=440:duration=40','-c:a','libmp3lame',str(song)],check=True)
    await story.render_local([clip]*7,song,target,35)
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
    assert order['amount_kopecks']==79000
    link=await payments.create_payment_link(456,order_id=key,amount=790,description=order['product_name'])
    form=parse_form_fields(parse_qsl(urlsplit(link).query,keep_blank_values=True))
    receipt={'order_num':key,'order_id':'paid-mother-790','sum':'790.00','currency':'rub','payment_status':'success',
        'products':form['products'],'_param_user_id':'456','_param_credits':'0','_param_package_id':order['package_id']}
    validated,_=await payments.validate_payment(receipt)
    assert validated['tariff']=='mother'
    assert await database.mark_cartoon_paid(key,'paid-mother-790')
    assert not await database.mark_cartoon_paid(key,'paid-mother-790')
    assert await record(456,'mother_cartoon_ready',key)
    assert not await record(456,'mother_cartoon_ready',key)
    await record(999,'mother_cartoon_ready','admin-test')
    await database.init_db()
    counters=await database.get_all_counters(admin_id=999)
    assert counters['buy_cartoon_790']==1 and counters['mother_cartoon_ready']==1
    paid=await sales(999)
    assert paid['cartoon_paid']==1 and paid['cartoon_revenue']==79000
    e=message_event('/stats')
    e.get_ids=lambda:(123,999)
    await call_handler(handlers.create_router(admin_user_id=999),'stats',e)
    assert 'День матери' in str(e.message.answer.await_args_list)
    assert '790' in str(e.message.answer.await_args_list)
    assert all(len(c.args[0])<4000 for c in e.message.answer.await_args_list)


async def test_cannot_buy_new_mother_story_at_old_short_price(credit_db):
    with pytest.raises(ValueError):await database.create_cartoon_order('cartoon_wrong_price',456,'theme_mother','short')
    with pytest.raises(ValueError):await database.create_cartoon_order('cartoon_wrong_theme',456,'theme_ny','mother')
    key=await database.create_cartoon_order('cartoon_other_theme',456,'theme_ny','short')
    assert (await database.get_cartoon_order(key))['amount_kopecks']==59000

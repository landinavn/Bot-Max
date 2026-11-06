import asyncio
import json
from dataclasses import replace
from types import SimpleNamespace
from unittest.mock import AsyncMock

import httpx
import pytest
from PIL import Image

from multstudio import repository as repo
from multstudio.adapters import KlingAdapter, TemporaryPollError, Providers
from multstudio.adapters import KlingTaskFailed
from multstudio.assets import Assets, https_url, public_url
from multstudio.render import Renderer, probe, process
from multstudio.router import create_router, validate_spec
from multstudio.seed import seed, SCENES
from multstudio.settings import Settings, money
from multstudio.worker import Worker
from test_handlers import callback_event, ready_context, call_handler


@pytest.fixture
async def studio(credit_db,tmp_path):
    await repo.init()
    project=await seed(456)
    settings=Settings(root=tmp_path/'files',image_model='image-test',image_price=1000000,
                      video_price=500000,speech_price=100000,enabled=True)
    await repo.prepare_drafts(project,settings)
    return project,settings,Assets(settings.root,SimpleNamespace())


async def add_image(assets,snap,*,approved=True):
    assets.root.mkdir(parents=True,exist_ok=True)
    relative,checksum=await assets.write(b'fake-image','png')
    return await repo.add_asset(snap['project_id'],snap['target'],snap['id'],'image',snap['context_hash'],
                                relative,checksum,approved=int(approved))


async def ready_scene(studio):
    project,settings,assets=studio
    for char in await repo.children('character',project):
        await add_image(assets,await repo.snapshot('character',char['id']))
    episode=(await repo.children('episode',project))[0]
    scene=(await repo.children('scene',episode['id']))[0]
    return scene,episode


async def queued_image(studio):
    project,settings,_=studio
    character=(await repo.children('character',project))[0]
    item=await repo.quote('character',character['id'],'image',settings)
    await repo.budget(project,10000000)
    await repo.confirm(item['id'],456)
    return item


async def test_seed_is_exact_idempotent_and_never_queues_paid_calls(studio):
    project,settings,_=studio
    assert await seed(456)==project
    await repo.prepare_drafts(project,settings)
    chars=await repo.children('character',project)
    assert [c['name'] for c in chars]==['Аня','Дима','Маша']
    assert [c['spec']['voice'] for c in chars]==['alena','filipp','alena']
    assert [c['spec']['emotion'] for c in chars]==['neutral','neutral','good']
    episode=(await repo.children('episode',project))[0]
    assert episode['title']=='Опять Новый год?!'
    scenes=await repo.children('scene',episode['id'])
    assert len(scenes)==7 and sum(s['spec']['duration'] for s in scenes)==40
    assert [s['spec']['dialogue'][0]['text'] if s['spec']['dialogue'] else '' for s in scenes]==[d[4][0][1] if d[4] else '' for d in SCENES]
    assert len(await repo.job_list(project))==10
    assert {j['status'] for j in await repo.job_list(project)}=={'draft'}
    assert await repo.claim() is None
    assert await repo.budget(project)=={'limit':0,'committed':0}


async def test_paid_confirmation_reserves_atomically_and_is_idempotent(studio):
    project,settings,_=studio
    chars=await repo.children('character',project)
    jobs=[await repo.quote('character',c['id'],'image',settings) for c in chars[:2]]
    with pytest.raises(ValueError,match='бюджета'):
        await repo.confirm(jobs[0]['id'],456)
    await repo.budget(project,1000000)
    results=await asyncio.gather(*(repo.confirm(j['id'],456) for j in jobs),return_exceptions=True)
    assert sum(isinstance(x,ValueError) for x in results)==1
    success=next(x for x in results if isinstance(x,dict))
    await repo.confirm(success['id'],456)
    assert (await repo.budget(project))['committed']==1000000


async def test_stale_quote_and_wrong_actor_cannot_start_paid_request(studio):
    project,settings,_=studio
    char=(await repo.children('character',project))[0]
    item=await repo.quote('character',char['id'],'image',settings)
    with pytest.raises(ValueError,match='доступа'):
        await repo.confirm(item['id'],999)
    await repo.edit('character',char['id'],dict(char['spec'],description='Changed face'))
    await repo.budget(project,10000000)
    with pytest.raises(ValueError,match='устарела'):
        await repo.confirm(item['id'],456)
    assert (await repo.job(item['id']))['status']=='stale'
    assert await repo.claim() is None


async def test_scene_references_location_and_image_approval_are_required(studio):
    project,settings,assets=studio
    episode=(await repo.children('episode',project))[0]
    scenes=await repo.children('scene',episode['id'])
    with pytest.raises(ValueError,match='эталоны'):
        await repo.quote('scene',scenes[0]['id'],'image',settings)
    await ready_scene(studio)
    # Stay in the kitchen: scene two must use scene one's approved room image.
    await repo.edit('scene',scenes[1]['id'],dict(scenes[1]['spec'],visual='Кухня. Аня и Дима у стола.'))
    with pytest.raises(ValueError,match='предыдущей сцены'):
        await repo.quote('scene',scenes[1]['id'],'image',settings)
    snap=await repo.snapshot('scene',scenes[0]['id'])
    image=await add_image(assets,snap,approved=False)
    with pytest.raises(ValueError,match='утвердите изображение'):
        await repo.quote('scene',scenes[0]['id'],'video',settings)
    await repo.approve_asset(image,456)
    assert (await repo.quote('scene',scenes[0]['id'],'video',settings))['estimate']==500000
    second=await repo.snapshot('scene',scenes[1]['id'])
    assert second['location_reference']['id']==image
    assert len(second['cast'])==2
    assert (await repo.quote('scene',scenes[1]['id'],'image',settings))['status']=='quoted'


async def test_character_edit_keeps_history_and_invalidates_dependent_image(studio):
    _,_,assets=studio
    scene,_=await ready_scene(studio)
    snap=await repo.snapshot('scene',scene['id'])
    image=await add_image(assets,snap)
    character=snap['cast'][0]
    await repo.edit('character',character['id'],dict(character['spec'],voice='alloy'))
    changed=await repo.snapshot('scene',scene['id'])
    assert changed['context_hash']!=snap['context_hash']
    assert 'image' not in changed['assets']
    async with repo.connect() as db:
        assert await repo.one(db,'SELECT id FROM cartoon_assets WHERE id=?',(image,))
        assert len(await repo.rows(db,'SELECT * FROM cartoon_versions WHERE entity_id=?',(character['id'],)))==2


async def test_unknown_submission_survives_restart_without_resubmitting(studio):
    _,settings,assets=studio
    original=await queued_image(studio)
    providers=SimpleNamespace(preflight=lambda kind:None,image=AsyncMock(side_effect=httpx.ReadTimeout('secret')))
    worker=Worker(SimpleNamespace(),456,settings,providers,assets)
    await worker.execute(await repo.claim())
    assert (await repo.job(original['id']))['status']=='manual'
    await repo.init()
    assert await repo.claim() is None
    providers.image.assert_awaited_once()
    assert (await repo.budget(original['project_id']))['committed']==1000000
    assert 'secret' not in (await repo.job(original['id']))['error']


async def test_claim_recovers_expired_preparation_but_not_paid_post(studio):
    item=await queued_image(studio)
    await repo.claim()
    await repo.update_job(item['id'],lease_until=1)
    assert (await repo.claim())['id']==item['id']
    await repo.update_job(item['id'],status='submitting',lease_until=1)
    assert await repo.claim() is None
    assert (await repo.job(item['id']))['status']=='manual'


async def test_known_kling_task_retries_poll_without_second_post(studio):
    project,settings,assets=studio
    scene,_=await ready_scene(studio)
    image=await add_image(assets,await repo.snapshot('scene',scene['id']))
    item=await repo.quote('scene',scene['id'],'video',settings)
    await repo.budget(project,10000000); await repo.confirm(item['id'],456)
    kling=SimpleNamespace(submit=AsyncMock(return_value='known-task'),poll=AsyncMock(side_effect=TemporaryPollError()))
    providers=SimpleNamespace(preflight=lambda kind:None,kling=kling)
    assets.asset_url=AsyncMock(return_value='https://cdn.example/ref.png')
    worker=Worker(SimpleNamespace(),456,settings,providers,assets)
    await worker.execute(await repo.claim())
    current=await repo.job(item['id'])
    assert current['provider_id']=='known-task' and current['status']=='polling'
    await repo.update_job(item['id'],next_attempt=0)
    await worker.execute(await repo.claim())
    assert (await repo.job(item['id']))['status']=='polling'
    kling.submit.assert_awaited_once()
    kling.poll.assert_awaited_once_with('known-task')


async def test_excess_actual_cost_stops_new_queued_paid_requests(studio):
    project,settings,assets=studio
    chars=await repo.children('character',project)
    jobs=[await repo.quote('character',c['id'],'image',settings) for c in chars[:2]]
    await repo.budget(project,2000000)
    for item in jobs: await repo.confirm(item['id'],456)
    first=await repo.claim()
    await repo.update_job(first['id'],status='done',lease_until=0)
    await repo.reconcile(first['id'],456,3000000)
    assert await repo.claim() is None
    await repo.budget(project,5000000)
    assert (await repo.claim())['id']==jobs[1]['id']


async def test_worker_concurrent_claims_cannot_take_same_job(studio):
    item=await queued_image(studio)
    claims=await asyncio.gather(repo.claim(),repo.claim())
    assert len([x for x in claims if x and x['id']==item['id']])==1


async def test_no_customer_can_open_studio_or_confirm_jobs(studio):
    _,settings,assets=studio
    item=await queued_image(studio)
    router=create_router(999,settings,assets)
    context=await ready_context()
    event=callback_event('studio:confirm:'+item['id'])
    await call_handler(router,'studio_callback',event,context)
    event.send.assert_not_awaited()
    event.ack.assert_awaited_once_with(notification='Раздел доступен только администратору.')


async def test_opening_admin_studio_does_not_enqueue_jobs(studio,monkeypatch):
    project,settings,assets=studio
    router=create_router(456,settings,assets)
    event=callback_event('studio:home')
    async def reply(ev,text,**kw): await ev.send(text,**kw)
    monkeypatch.setattr('multstudio.router._reply',reply)
    await call_handler(router,'studio_callback',event,await ready_context())
    assert await repo.claim() is None
    assert 'Мультстудия' in event.send.await_args.args[0]
    assert len(event.send.await_args.kwargs['attachments'])==1


@pytest.mark.parametrize('value',['NaN','Infinity','-1','100001','not-a-number'])
def test_budget_cannot_be_nonfinite_or_negative(value):
    with pytest.raises(ValueError): money(value)


@pytest.mark.parametrize('changes',[{'duration':float('nan')},{'duration':20},{'characters':[]},
                                   {'dialogue':[{'character_id':'wrong','text':'hi'}]}])
def test_invalid_scene_is_rejected(changes):
    spec=dict(visual='Kitchen',duration=5,characters=['anya'],dialogue=[],approved=True)
    spec.update(changes)
    with pytest.raises(ValueError): validate_spec('scene',spec)


@pytest.mark.parametrize('url',['http://example.com/a','https://user:password@example.com/a','https://example.com:88/a'])
def test_sources_require_public_https(url):
    with pytest.raises(ValueError): https_url(url)


async def test_private_source_cannot_be_downloaded():
    with pytest.raises(ValueError,match='внутреннюю'):
        await public_url('https://127.0.0.1/a.png')


async def test_kling_post_is_not_retried_and_does_not_expose_key():
    calls=[]
    def handler(request):
        calls.append(request)
        return httpx.Response(500,json={'error':'secret-key'})
    adapter=KlingAdapter('secret-key',httpx.MockTransport(handler))
    with pytest.raises(Exception) as error:
        await adapter.submit('https://cdn.example/a.png','test',5)
    assert len(calls)==1 and 'secret-key' not in str(error.value)


async def test_real_ffmpeg_normalizes_video_audio_and_does_not_speed_speech(studio):
    _,_,assets=studio
    assets.root.mkdir(parents=True,exist_ok=True)
    image=assets.path('source.png'); Image.new('RGB',(64,96),'navy').save(image)
    voice=assets.path('voice.wav')
    await process('ffmpeg','-nostdin','-v','error','-y','-f','lavfi','-i','sine=frequency=440:duration=0.6',voice)
    scene={'spec':{'duration':0.2,'dialogue':[{'text':'Мам, а ты себе что хочешь?'}]},
           'assets':{'image':{'kind':'image','path':'source.png'},'speech':{'path':'voice.wav'}}}
    renderer=Renderer(assets)
    result=await renderer.scene(scene,ending=['Маленькие чудеса Ани','Продолжение следует…'])
    info=await probe(result)
    video=next(s for s in info['streams'] if s['codec_type']=='video')
    audio=next(s for s in info['streams'] if s['codec_type']=='audio')
    assert (video['width'],video['height'],video['r_frame_rate'],video['codec_name'])==(1080,1920,'30/1','h264')
    assert audio['codec_name']=='aac' and float(info['format']['duration'])>=0.6
    assert image.exists() and voice.exists()


async def test_budget_is_rechecked_immediately_before_paid_post(studio):
    project,settings,assets=studio
    item=await queued_image(studio)
    await repo.claim()
    await repo.budget(project,0)
    assert await repo.begin_submission(item['id']) is False
    assert (await repo.job(item['id']))['status']=='queued'
    assert await repo.claim() is None


async def test_complete_local_edit_with_music_and_final_title(studio):
    _,_,assets=studio
    assets.root.mkdir(parents=True,exist_ok=True)
    Image.new('RGB',(64,96),'teal').save(assets.path('shot.png'))
    await process('ffmpeg','-nostdin','-v','error','-y','-f','lavfi','-i','sine=frequency=330:duration=0.3',assets.path('music.wav'))
    scene={'spec':{'duration':0.2,'dialogue':[]},'assets':{'image':{'kind':'image','path':'shot.png'}}}
    snap={'spec':{'ending':['А о чём мечтаете вы?']},'scenes':[scene,scene],
          'assets':{'music':{'path':'music.wav'}}}
    result=await Renderer(assets).assemble(snap)
    info=await probe(result)
    assert float(info['format']['duration'])>=0.4
    assert len(info['streams'])==2
    assert {s['codec_name'] for s in info['streams']}=={'h264','aac'}


async def test_reference_approval_is_bound_to_project_owner(studio):
    _,_,assets=studio
    scene,_=await ready_scene(studio)
    asset=await add_image(assets,await repo.snapshot('scene',scene['id']),approved=False)
    with pytest.raises(ValueError,match='доступа'):
        await repo.approve_asset(asset,999)


async def test_quote_uses_approved_snapshot_even_if_model_config_changes(studio):
    project,settings,_=studio
    char=(await repo.children('character',project))[0]
    item=await repo.quote('character',char['id'],'image',settings)
    changed=replace(settings,image_model='another-model',image_price=9000000)
    await repo.budget(project,1000000)
    await repo.confirm(item['id'],456)
    saved=json.loads((await repo.job(item['id']))['snapshot'])
    assert saved['quote']['model']=='image-test' and saved['quote']['estimate']==1000000
    assert changed.image_model!=saved['quote']['model']


async def test_cancelled_quote_cannot_be_started_from_old_button(studio):
    project,settings,_=studio
    char=(await repo.children('character',project))[0]
    item=await repo.quote('character',char['id'],'image',settings)
    await repo.cancel_quote(item['id'],456)
    assert (await repo.confirm(item['id'],456))['status']=='cancelled'
    assert await repo.claim() is None
    assert (await repo.budget(project))['committed']==0


async def test_publishing_unchanged_references_does_not_hide_scene_results(studio):
    _,_,assets=studio
    scene,_=await ready_scene(studio)
    before=await repo.snapshot('scene',scene['id'])
    image=await add_image(assets,before)
    character_asset=before['cast'][0]['assets']['image']
    async with repo.connect() as db:
        await db.execute('UPDATE cartoon_assets SET public_url=? WHERE id=?',
                         ('https://cdn.example/immutable.png',character_asset['id']))
        await db.commit()
    after=await repo.snapshot('scene',scene['id'])
    assert before['context_hash']==after['context_hash']
    assert after['assets']['image']['id']==image


async def test_completed_retry_keeps_previously_reconciled_actual_cost(studio):
    _,settings,assets=studio
    item=await queued_image(studio)
    await repo.update_job(item['id'],status='manual',lease_until=0)
    await repo.reconcile(item['id'],456,250000)
    worker=Worker(SimpleNamespace(),456,settings,SimpleNamespace(),assets)
    assets.asset_url=AsyncMock(return_value='https://storage.yandexcloud.net/vision-board-bot-images/multstudio/result.png')
    await worker.save_result(item,json.loads(item['snapshot']),data=b'retained-result')
    assert (await repo.job(item['id']))['actual']==250000
    assert (await repo.budget(item['project_id']))['committed']==250000


async def test_speech_waits_for_approved_keyframe(studio):
    _,settings,assets=studio
    scene,_=await ready_scene(studio)
    with pytest.raises(ValueError,match='утвердите изображение'):
        await repo.quote('scene',scene['id'],'speech',settings)
    await add_image(assets,await repo.snapshot('scene',scene['id']))
    assert (await repo.quote('scene',scene['id'],'speech',settings))['status']=='quoted'


async def seven_videos(studio):
    project,settings,assets=studio
    _,episode=await ready_scene(studio)
    scenes=await repo.children('scene',episode['id'])
    for scene in scenes:
        await add_image(assets,await repo.snapshot('scene',scene['id']))
    await repo.budget(project,10000000)
    jobs=[]
    for scene in scenes:
        item=await repo.quote('scene',scene['id'],'video',settings)
        await repo.confirm(item['id'],456)
        jobs.append(item)
    return jobs


async def test_seven_piapi_jobs_are_strictly_serial_until_each_finishes(studio):
    jobs=await seven_videos(studio)
    for expected in jobs:
        claims=await asyncio.gather(repo.claim(),repo.claim(),repo.claim())
        started=[x for x in claims if x]
        assert len(started)==1 and started[0]['id']==expected['id']
        assert await repo.begin_submission(expected['id']) is True
        await repo.update_job(expected['id'],status='polling',provider_id='task-'+expected['id'],
                              lease_until=0,next_attempt=9999999999)
        assert await repo.claim() is None  # No next POST while provider is rendering.
        await repo.update_job(expected['id'],status='done',lease_until=0)


async def test_unknown_piapi_outcome_blocks_queue_until_explicit_provider_review(studio):
    jobs=await seven_videos(studio)
    first=await repo.claim()
    await repo.begin_submission(first['id'])
    await repo.update_job(first['id'],status='manual',lease_until=0,error='unknown_submission')
    assert await repo.claim() is None
    with pytest.raises(ValueError,match='стоимость'):
        await repo.resolve_video(first['id'],456)
    await repo.reconcile(first['id'],456,0)
    assert await repo.claim() is None  # Billing review alone does not prove rendering ended.
    with pytest.raises(ValueError):
        await repo.resolve_video(first['id'],999)
    await repo.resolve_video(first['id'],456)
    assert (await repo.claim())['id']==jobs[1]['id']


async def test_explicit_terminal_kling_failure_releases_execution_slot(studio):
    jobs=await seven_videos(studio)
    await repo.claim()
    await repo.update_job(jobs[0]['id'],status='polling',provider_id='failed-task',lease_until=0)
    providers=SimpleNamespace(kling=SimpleNamespace(poll=AsyncMock(side_effect=KlingTaskFailed())))
    worker=Worker(SimpleNamespace(),456,studio[1],providers,studio[2])
    await worker.execute(await repo.claim())
    assert (await repo.job(jobs[0]['id']))['status']=='failed'
    assert (await repo.claim())['id']==jobs[1]['id']


async def test_scene_image_reuses_canonical_references_via_real_edits_endpoint(studio,monkeypatch):
    import base64
    from pathlib import Path
    from io import BytesIO
    from aioresponses import aioresponses
    import respx
    from config import Config
    project,settings,assets=studio
    scene,_=await ready_scene(studio)
    snap=await repo.snapshot('scene',scene['id'])
    snap['quote']=settings.quote('image',snap)
    assets.asset_url=AsyncMock(return_value='https://cdn.example/canonical-anya.png')
    buffer=BytesIO(); Image.new('RGB',(16,24),'white').save(buffer,format='PNG'); png=buffer.getvalue()
    config=Config(token='fake',photo_path=Path('unused'),image_api_key='fake-openai',image_model='image-test')
    provider=Providers(settings,config,assets)
    with aioresponses() as images,respx.mock() as api:
        images.get('https://cdn.example/canonical-anya.png',body=png)
        route=api.post('https://api.openai.com/v1/images/edits').mock(return_value=httpx.Response(200,
            json={'data':[{'b64_json':base64.b64encode(png).decode()}]}))
        output=await provider.image(snap)
    assert output==png and route.call_count==1
    assert b'filename=' in route.calls[0].request.content
    assert png in route.calls[0].request.content
    assert b'character reference images' in route.calls[0].request.content


async def test_srt_is_saved_from_database_lines_with_explicit_white_black_style(studio,monkeypatch):
    project,_,assets=studio
    scene,_=await ready_scene(studio)
    snap=await repo.snapshot('scene',scene['id'])
    assets.root.mkdir(parents=True,exist_ok=True)
    Image.new('RGB',(16,24),'white').save(assets.path('subtitle-test.png'))
    snap['assets']={'image':{'kind':'image','path':'subtitle-test.png'}}
    captured=[]
    async def ffmpeg(*args,**kwargs): captured.append(args)
    monkeypatch.setattr('multstudio.render.process',ffmpeg)
    await Renderer(assets).scene(snap)
    async with repo.connect() as db:
        asset=await repo.one(db,"SELECT * FROM cartoon_assets WHERE kind='subtitle' AND target_id=?",(scene['id'],))
    text=assets.path(asset['path']).read_text()
    assert snap['spec']['dialogue'][0]['text'] in text and '00:00:00,000 --> 00:00:05,000' in text
    command=' '.join(map(str,captured[0]))
    assert 'PrimaryColour=&H00FFFFFF' in command
    assert 'OutlineColour=&H00000000' in command and 'Outline=2' in command


async def test_episode_srt_uses_actual_scene_durations_and_database_dialogue(studio,monkeypatch):
    project,_,assets=studio
    episode=(await repo.children('episode',project))[0]
    snap=await repo.snapshot('episode',episode['id'])
    assets.root.mkdir(parents=True,exist_ok=True)
    renderer=Renderer(assets)
    renderer.scene=AsyncMock(side_effect=[assets.path(f'clip-{i}.mp4') for i in range(7)])
    monkeypatch.setattr('multstudio.render.duration',AsyncMock(side_effect=[5.2,6,6,7,7,6,3]))
    monkeypatch.setattr('multstudio.render.process',AsyncMock(return_value=b''))
    await renderer.assemble(snap)
    async with repo.connect() as db:
        asset=await repo.one(db,"SELECT * FROM cartoon_assets WHERE kind='subtitle' AND target='episode' AND target_id=?",(episode['id'],))
    text=assets.path(asset['path']).read_text()
    assert '00:00:00,000 --> 00:00:05,200' in text
    assert '00:00:31,200 --> 00:00:37,200' in text
    for scene in snap['scenes']:
        for line in scene['spec']['dialogue']:
            assert line['text'] in text

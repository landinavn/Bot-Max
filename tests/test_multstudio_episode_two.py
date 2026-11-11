"""Episode-two preparation is idempotent, reference-safe and never pays providers."""
import asyncio
import json
from dataclasses import replace
from unittest.mock import AsyncMock

import pytest

from multstudio import repository as repo
from multstudio.episode_two import prepare, prepare_jobs, plan, TITLE, SCENES
from multstudio.seed import seed
from multstudio.prompts import validate_scene_image_references
from test_multstudio import studio, add_image


async def episode(studio):
    project,settings,assets=studio
    key=await prepare(project,456)
    return key,await repo.children('scene',key)


async def test_seven_scenes_nine_utterances_and_immutable_first_episode(studio):
    project,settings,assets=studio
    async with repo.connect() as db:
        before={t:await repo.rows(db,'SELECT * FROM '+t+' ORDER BY rowid') for t in (
            'cartoon_projects','cartoon_characters','cartoon_assets','cartoon_versions','cartoon_character_speech_profiles')}
    first=(await repo.children('episode',project))[0]
    first_snapshot=await repo.snapshot('episode',first['id'])
    key,scenes=await episode(studio)
    assert (await repo.get('episode',key))['title']==TITLE
    assert [s['number'] for s in scenes]==list(range(1,8))
    assert sum(s['spec']['duration'] for s in scenes)==44
    assert sum(len(s['spec']['dialogue']) for s in scenes)==9
    for s,source in zip(scenes,SCENES):
        assert [l['text'] for l in s['spec']['dialogue']]==[l[1] for l in source[4]]
        assert all(l['character_id'] in s['spec']['characters'] for l in s['spec']['dialogue'])
    await prepare_jobs(key,settings)
    assert await repo.snapshot('episode',first['id'])==first_snapshot
    async with repo.connect() as db:
        for table,old in before.items():
            rows=await repo.rows(db,'SELECT * FROM '+table+' ORDER BY rowid')
            assert rows[:len(old)]==old
            if table!='cartoon_versions':assert rows==old
        jobs=await repo.rows(db,'SELECT * FROM cartoon_jobs WHERE target_id IN (SELECT id FROM cartoon_scenes WHERE episode_id=?) OR target_id=?',(key,key))
    assert len(jobs)==22 and all(j['status']=='draft' and j['approved_by'] is None for j in jobs)
    assert await repo.claim() is None
    await seed(456)
    assert await repo.snapshot('episode',first['id'])==first_snapshot


async def test_restart_and_concurrent_preparation_preserve_manual_changes(studio):
    project,settings,_=studio
    ids=await asyncio.gather(*(prepare(project,456) for _ in range(4)))
    assert len(set(ids))==1
    key=ids[0];scenes=await repo.children('scene',key)
    await repo.update_scene_visual(scenes[0]['id'],456,'Правка администратора')
    await prepare_jobs(key,settings)
    async with repo.connect() as db:
        before=await repo.rows(db,'SELECT * FROM cartoon_jobs ORDER BY rowid')
    await prepare(project,456);await prepare_jobs(key,settings);await repo.init()
    assert (await repo.get('scene',scenes[0]['id']))['spec']['visual']=='Правка администратора'
    async with repo.connect() as db:assert await repo.rows(db,'SELECT * FROM cartoon_jobs ORDER BY rowid')==before


async def test_last_scene_of_first_episode_is_first_scene_location(studio):
    project,settings,assets=studio
    chars=await repo.children('character',project)
    for c in chars:await add_image(assets,await repo.snapshot('character',c['id']))
    first=(await repo.children('episode',project))[0]
    last=(await repo.children('scene',first['id']))[-1]
    last_image=await add_image(assets,await repo.snapshot('scene',last['id']))
    key,scenes=await episode(studio)
    snap=await repo.snapshot('scene',scenes[0]['id'])
    assert scenes[0]['spec']['location_reference_scene_id']==last['id']
    assert snap['location_reference']['id']==last_image
    assert snap['location_reference_required'] and len(snap['cast'])==1
    validate_scene_image_references(snap)
    quoted=await repo.quote('scene',scenes[0]['id'],'image',settings)
    assert quoted['status']=='quoted' and await repo.claim() is None
    beach=await repo.snapshot('scene',scenes[4]['id'])
    assert beach['location_reference_required'] is False and not beach.get('location_reference')
    restored=await repo.snapshot('scene',scenes[5]['id'])
    assert restored['location_reference']['id']==last_image
    assert set(c['id'] for c in restored['cast'])==set(scenes[5]['spec']['characters'])


async def test_missing_or_unapproved_anchor_blocks_before_payment(studio):
    project,settings,assets=studio
    for c in await repo.children('character',project):await add_image(assets,await repo.snapshot('character',c['id']))
    _,scenes=await episode(studio)
    snap=await repo.snapshot('scene',scenes[0]['id'])
    with pytest.raises(ValueError):validate_scene_image_references(snap)
    with pytest.raises(ValueError):await repo.quote('scene',scenes[0]['id'],'image',settings)
    last_id=scenes[0]['spec']['location_reference_scene_id']
    await add_image(assets,await repo.snapshot('scene',last_id),approved=False)
    with pytest.raises(ValueError):await repo.quote('scene',scenes[0]['id'],'image',settings)
    assert await repo.claim() is None


async def test_exact_current_voices_with_episode_specific_speeds(studio):
    project,settings,assets=studio
    chars=await repo.children('character',project)
    before={c['id']:await repo.character_speech(c['id']) for c in chars}
    _,scenes=await episode(studio)
    for s in scenes:
        await add_image(assets,await repo.snapshot('scene',s['id']))
        item=await repo.quote('scene',s['id'],'speech',settings)
        profiles=json.loads(item['snapshot'])['quote']['speech_profiles']
        for cid,profile in profiles.items():
            assert profile['voice']==before[cid]['voice']
            assert profile['speed']=={'Аня':1.05,'Дима':0.95,'Маша':1.1}[profile['name']]
    assert {c['id']:await repo.character_speech(c['id']) for c in chars}==before


async def test_video_waits_for_speech(studio):
    _,settings,assets=studio
    _,scenes=await episode(studio);s=scenes[0]
    snap=await repo.snapshot('scene',s['id']);await add_image(assets,snap)
    with pytest.raises(ValueError,match='сначала создайте озвучку'):
        await repo.quote('scene',s['id'],'video',settings)
    assert await repo.claim() is None


async def test_plan_includes_all_services_and_reports_missing_prices(studio):
    _,settings,_=studio
    key,_=await episode(studio)
    result=await plan(key,settings)
    assert (result['images'],result['videos'],result['utterances'])==(7,7,9)
    assert set(result['services'])=={'image','video','speech'}
    assert result['total']==sum(s['estimate'] for s in result['services'].values())>0
    assert result['services']['video']['duration']==70  # Seven provider clips of 10s; final cuts target 44s.
    assert not result['errors']
    missing=await plan(key,replace(settings,image_price=0,speech_v3_price=0))
    assert missing['errors'] and missing['total']<result['total']
    assert await repo.claim() is None


async def test_no_replacement_cast_or_overwriting_occupied_episode(studio):
    project,_,_=studio
    with pytest.raises(ValueError):await prepare(project,999)
    occupied=await repo.create('episode',{},project_id=project,title='Существующая серия',number=2)
    with pytest.raises(ValueError,match='уже занят'):await prepare(project,456)
    assert (await repo.get('episode',occupied))['title']=='Существующая серия'
    assert len(await repo.children('character',project))==3


@pytest.mark.parametrize('trim,expected',[(True,'6.000000000'),(False,'10.000000000')])
async def test_new_series_cuts_provider_padding_without_changing_old_renders(studio,monkeypatch,trim,expected):
    from multstudio import render
    _,_,assets=studio;assets.root.mkdir(parents=True,exist_ok=True)
    renderer=render.Renderer(assets)
    monkeypatch.setattr(renderer,'ready',lambda:None)
    monkeypatch.setattr(render,'duration',AsyncMock(return_value=10))
    runner=AsyncMock();monkeypatch.setattr(render,'process',runner)
    scene=dict(spec=dict(duration=6,dialogue=[],trim_video_to_script=trim),assets={'video':dict(kind='video',path='video.mp4')})
    await renderer.scene(scene)
    assert 'trim=duration='+expected in ' '.join(map(str,runner.await_args.args))


async def test_two_dialogue_files_are_separate_measured_and_retained(studio,monkeypatch):
    from types import SimpleNamespace
    from multstudio.worker import Worker
    from multstudio import render
    project,settings,assets=studio
    _,scenes=await episode(studio);s=scenes[3]
    await add_image(assets,await repo.snapshot('scene',s['id']))
    item=await repo.quote('scene',s['id'],'speech',settings)
    await repo.budget(project,10000000);await repo.confirm(item['id'],456)
    item=await repo.claim()
    provider=SimpleNamespace(preflight=lambda kind:None,speech_line=AsyncMock(return_value=b'mock-mp3'))
    worker=Worker(SimpleNamespace(),456,settings,provider,assets)
    measure=AsyncMock(side_effect=[1.2,1.8]);monkeypatch.setattr(render,'duration',measure)
    combined=assets.path('test-combined.mp3');combined.write_bytes(b'combined')
    worker.renderer.speech=AsyncMock(return_value=combined)
    worker.save_result=AsyncMock()
    await worker.execute(item)
    assert provider.speech_line.await_count==2 and measure.await_count==2
    async with repo.connect() as db:
        lines=await repo.rows(db,"SELECT * FROM cartoon_assets WHERE job_id=? AND kind='speech_line' ORDER BY rowid",(item['id'],))
    assert len(lines)==2 and lines[0]['path']!=lines[1]['path']
    assert [json.loads(l['metadata'])['duration_seconds'] for l in lines]==[1.2,1.8]
    assert [json.loads(l['metadata'])['text'] for l in lines]==[x['text'] for x in s['spec']['dialogue']]
    worker.save_result.assert_awaited_once()


async def test_second_episode_card_and_plan_preserve_first_episode_ui(studio):
    from multstudio.router import create_router
    from test_multstudio_project_menu import event
    from test_handlers import dispatch_callback, ready_context
    project,settings,assets=studio
    first=(await repo.children('episode',project))[0]
    second,_=await episode(studio)
    router=create_router(456,settings,assets);context=await ready_context()
    for key,is_second in [(first['id'],False),(second,True)]:
        e=event('studio:e:'+key)
        await dispatch_callback(router,e,context)
        sent=e.bot.send_message.await_args.kwargs
        buttons=[b.payload for row in sent['attachments'][0].payload.buttons for b in row]
        assert ('studio:episode_plan:'+key in buttons)==is_second
        assert ('44 секунд' if is_second else '40 секунд, допустимо 42–45') in sent['text']
    e=event('studio:episode_plan:'+second)
    await dispatch_callback(router,e,context)
    text=e.bot.send_message.await_args.kwargs['text']
    assert 'Изображений: 7; видео: 7; отдельных реплик: 9' in text
    assert 'Всего ориентировочно:' in text
    assert await repo.claim() is None


async def test_first_frame_generation_receives_last_episode_frame_after_character(studio,monkeypatch):
    from config import Config
    from multstudio import adapters
    project,settings,assets=studio
    for char in await repo.children('character',project):await add_image(assets,await repo.snapshot('character',char['id']))
    first=(await repo.children('episode',project))[0]
    last=(await repo.children('scene',first['id']))[-1]
    anchor=await add_image(assets,await repo.snapshot('scene',last['id']))
    _,scenes=await episode(studio)
    item=await repo.quote('scene',scenes[0]['id'],'image',settings)
    snap=json.loads(item['snapshot'])
    assets.asset_url=AsyncMock(side_effect=lambda a:'https://cdn.example/'+a['id'])
    assets.retain=AsyncMock()
    generate=AsyncMock(return_value=[b'generated']);monkeypatch.setattr(adapters,'_generate_image_bytes',generate)
    provider=adapters.Providers(settings,Config(token='test',photo_path=assets.root/'unused'),assets)
    assert await provider.image(snap)==b'generated'
    request=generate.await_args
    assert request.args[0]=='https://cdn.example/'+snap['cast'][0]['assets']['image']['id']
    assert request.kwargs['style_reference_urls']==('https://cdn.example/'+anchor,)
    assert 'LOCATION REFERENCE URL: https://cdn.example/'+anchor in request.kwargs['prompt_override']

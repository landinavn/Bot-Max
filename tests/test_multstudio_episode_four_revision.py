"""Draft-only rewrite, authentic dream assets, provider separation and real FFmpeg output."""
import json
from unittest.mock import AsyncMock
from pathlib import Path

import pytest
from PIL import Image

from multstudio import repository as repo,rules
from multstudio.episode_two import prepare as second,prepare_jobs,speech_profiles,plan
from multstudio.episode_three import prepare as third
from multstudio.episode_three_revision import apply as revise_three
from multstudio.episode_three_trim import apply as trim_three
from multstudio.episode_four import prepare as old_four
from multstudio.episode_four_revision import apply,TITLE,SCENES,OPENAI_BLOCK,MIGRATION
from multstudio.episode_four_media import card_bytes,clip,splice_tail
from multstudio.render import Renderer,duration,probe,process
from multstudio.worker import Worker
from multstudio.prompts import scene_image_prompt
from test_multstudio import studio,add_image
from test_multstudio_episode_three import make_clip,attach_audio


async def setup(studio):
    project,settings,assets=studio
    await second(project,456);await third(project,456)
    await revise_three(project,456);await trim_three(project,456)
    key=await old_four(project,456)
    return key


async def image(assets,snap,color='blue',approved=True):
    assets.root.mkdir(parents=True,exist_ok=True)
    import io
    data=io.BytesIO();Image.new('RGB',(120,180),color).save(data,format='PNG')
    path,sha=await assets.write(data.getvalue(),'png')
    return await repo.add_asset(snap['project_id'],'scene',snap['id'],'image',snap['context_hash'],path,sha,approved=int(approved))


async def materials(studio,key):
    episode=await repo.get('episode',key)
    for n,k in enumerate(episode['spec']['dream_scene_ids']):await image(studio[2],await repo.snapshot('scene',k),('red','green','blue')[n])
    shots=await repo.children('scene',key)
    board=await repo.snapshot('scene',shots[3]['id'])
    data=card_bytes(board,studio[2]);path,sha=await studio[2].write(data,'png')
    await repo.add_asset(board['project_id'],'scene',board['id'],'image',board['context_hash'],path,sha,approved=1)
    return shots


async def test_replace_only_draft_preserves_old_episodes_cast_profiles_and_archives(studio):
    key=await setup(studio);project,settings,assets=studio
    await prepare_jobs(key,settings)
    old=await repo.children('scene',key);old_spec=[s['spec'] for s in old]
    before=[await repo.snapshot('episode',e['id']) for e in await repo.children('episode',project) if e['number']<4]
    cast=await repo.children('character',project)
    voices={c['id']:await repo.character_speech(c['id']) for c in cast}
    assert await apply(project,456)==key
    await prepare_jobs(key,settings)
    revised=await repo.get('episode',key);shots=await repo.children('scene',key)
    assert revised['title']==TITLE and len(shots)==7 and revised['spec']['target_seconds']==35
    assert [s['id'] for s in shots]==[s['id'] for s in old[:7]]
    assert [s['spec']['dialogue'][0]['text'] if s['spec']['dialogue'] else '' for s in shots]==[x[2] for x in SCENES]
    assert sum(s['spec']['duration'] for s in shots)==35
    assert [await repo.snapshot('episode',e['id']) for e in before]==before
    assert await repo.children('character',project)==cast
    assert {c['id']:await repo.character_speech(c['id']) for c in cast}==voices
    async with repo.connect() as db:
        archived=await repo.one(db,'SELECT snapshot FROM cartoon_versions WHERE entity=? AND entity_id=? AND revision=1',('scene',shots[0]['id']))
        assert json.loads(archived['snapshot'])['spec']==old_spec[0]
        assert await repo.one(db,"SELECT id FROM cartoon_jobs WHERE status='cancelled' AND error='episode_four_draft_replaced'")
    snap=await repo.snapshot('episode',key)
    assert await old_four(project,456)==key and await apply(project,456)==key
    assert await repo.snapshot('episode',key)==snap
    assert await repo.claim() is None


@pytest.mark.parametrize('busy',['published','submitting','queued','manual'])
async def test_started_or_assembled_episode_not_replaced(studio,busy):
    key=await setup(studio);before=await repo.snapshot('episode',key)
    if busy=='published':
        await repo.add_asset(studio[0],'episode',key,'assemble',before['context_hash'],'retained.mp4','old',approved=1)
    else:
        await prepare_jobs(key,studio[1])
        async with repo.connect() as db:
            row=await repo.one(db,'SELECT id FROM cartoon_jobs WHERE target_id=?',(key,))
            await db.execute('UPDATE cartoon_jobs SET status=?,provider_id=? WHERE id=?',(busy,'existing-provider-id',row['id']));await db.commit()
    with pytest.raises(ValueError,match='сохранены'):await apply(studio[0],456)
    assert (await repo.get('episode',key))['title']!=TITLE
    assert (await repo.get('episode',key))['spec']==before['spec']


async def test_provider_and_speed_assignment_and_isolated_masha_stop(studio):
    key=await setup(studio);await apply(studio[0],456)
    shots=await repo.children('scene',key)
    assert [s['spec']['video_provider'] for s in shots]==['heygen','openai','heygen','heygen_video','heygen','heygen','heygen_video']
    assert [s['spec']['framing'] for s in shots]==['medium_wide','medium','medium_side','montage','medium','medium_wide','object_topdown']
    for s in shots:
        spec=s['spec'];rules.validate(spec)
        if spec['dialogue']:
            profiles=await speech_profiles(await repo.snapshot('scene',s['id']))
            assert profiles[spec['speaker_id']]['speed']==({'Аня':1.05,'Дима':.95,'Маша':1.10}[profiles[spec['speaker_id']]['name']])
    snap=await repo.snapshot('scene',shots[1]['id'])
    assert 'child speaker' in scene_image_prompt(snap) and 'Only the adult speaker' not in scene_image_prompt(snap)
    with pytest.raises(ValueError,match='Сцена Маши'):await repo.quote('scene',snap['id'],'video',studio[1])
    with pytest.raises(ValueError,match='OpenAI'):studio[1].quote('video',snap)
    result=await plan(key,studio[1]);assert any('Маши' in x for x in result['errors'])
    assert result['utterances']==5 and result['silent']==2


async def test_missing_map_prevents_paid_adult_requests_and_no_placeholders(studio):
    key=await setup(studio);await apply(studio[0],456);shots=await repo.children('scene',key)
    with pytest.raises(ValueError,match='три изображения'):await repo.quote('scene',shots[0]['id'],'image',studio[1])
    with pytest.raises(ValueError,match='три изображения'):card_bytes(await repo.snapshot('scene',shots[3]['id']),studio[2])
    for c in await repo.children('character',studio[0]):await add_image(studio[2],await repo.snapshot('character',c['id']))
    anchor=shots[0]['spec']['location_reference_scene_id'];await image(studio[2],await repo.snapshot('scene',anchor))
    await materials(studio,key)
    assert (await repo.quote('scene',shots[0]['id'],'image',studio[1]))['status']=='quoted'
    assert (await repo.quote('scene',shots[3]['id'],'video',studio[1]))['estimate']==0


async def test_approved_beach_and_old_studio_are_reused_without_changing_originals(studio):
    key=await setup(studio);old=await repo.children('scene',key)
    beach=old[0]['spec']['prop_reference_scene_ids'][0]
    first=await image(studio[2],await repo.snapshot('scene',beach),'red')
    second_image=await image(studio[2],await repo.snapshot('scene',old[6]['id']),'green')
    before=await repo.snapshot('scene',beach)
    await apply(studio[0],456)
    ep=await repo.get('episode',key)
    for k,source in zip(ep['spec']['dream_scene_ids'][:2],[first,second_image]):
        snap=await repo.snapshot('scene',k)
        assert snap['assets']['image']['approved']
        assert json.loads(snap['assets']['image']['metadata'])['reused_approved_asset_id']==source
    assert await repo.snapshot('scene',beach)==before


async def test_auth_only_and_materials_cannot_be_animated(studio):
    key=await setup(studio)
    with pytest.raises(ValueError):await apply(studio[0],999)
    await apply(studio[0],456);ep=await repo.get('episode',key)
    with pytest.raises(ValueError,match='исходное изображение'):await repo.quote('scene',ep['spec']['dream_scene_ids'][0],'video',studio[1])


async def test_actual_local_montage_is_five_seconds_1080p30_and_h264_aac(studio):
    key=await setup(studio);await apply(studio[0],456);shots=await materials(studio,key)
    snap=await repo.snapshot('scene',shots[3]['id'])
    path=await clip(Renderer(studio[2]),snap)
    assert abs(await duration(path)-5)<.035
    info=await probe(path);video=next(s for s in info['streams'] if s['codec_type']=='video')
    assert (video['width'],video['height'],video['r_frame_rate'],video['codec_name'],video['pix_fmt'])==(1080,1920,'30/1','h264','yuv420p')
    assert any(s['codec_name']=='aac' for s in info['streams'])


async def test_actual_speech_tail_is_replaced_and_final_assembly_35_seconds(studio):
    key=await setup(studio);await apply(studio[0],456);shots=await materials(studio,key)
    snap=await repo.snapshot('scene',shots[0]['id'])
    raw=await make_clip(studio[2],5,audio_seconds=3)
    voice=studio[2].path('approved-voice.mp3')
    await process('ffmpeg','-v','error','-y','-f','lavfi','-i','sine=frequency=440:duration=3',voice)
    renderer=Renderer(studio[2])
    result=await splice_tail(renderer,snap,raw,voice,3.0)
    assert abs(await duration(result)-5)<.035
    assert 3<=snap['normalization_metadata']['cutaway_start']<3.2
    assert snap['normalization_metadata']['tail_policy']=='approved-dream-card-no-freeze'
    # Offline concat verifies the 35-second contract; no provider scene is claimed as generated.
    renderer.scene=AsyncMock(return_value=result)
    episode=await repo.snapshot('episode',key)
    episode['assets']['music']={'path':voice.name}
    output=await renderer.assemble(episode)
    assert abs(await duration(output)-35)<.035


async def test_provider_worker_never_falls_back_for_masha(studio,monkeypatch):
    from multstudio.adapters import Providers
    from config import Config
    key=await setup(studio);await apply(studio[0],456);await prepare_jobs(key,studio[1])
    shots=await repo.children('scene',key)
    async with repo.connect() as db:
        row=await repo.one(db,"SELECT * FROM cartoon_jobs WHERE target_id=? AND kind='video' AND status='draft'",(shots[1]['id'],))
        await db.execute("UPDATE cartoon_jobs SET status='preparing' WHERE id=?",(row['id'],));await db.commit()
    row=await repo.job(row['id'])
    providers=Providers(studio[1],Config(token='test',photo_path=Path('unused')),studio[2])
    monkeypatch.setattr(providers.kling,'submit',AsyncMock())
    monkeypatch.setattr(providers.studio_heygen,'scene_video',AsyncMock())
    monkeypatch.setattr(providers.studio_heygen,'silent_video',AsyncMock())
    worker=Worker(None,456,studio[1],providers,studio[2]);await worker.execute(row)
    result=await repo.job(row['id'])
    assert result['status']=='preflight_failed' and 'Сцена Маши' in result['error']
    providers.kling.submit.assert_not_awaited();providers.studio_heygen.scene_video.assert_not_awaited();providers.studio_heygen.silent_video.assert_not_awaited()


async def test_production_report_marks_missing_costs_and_counts_materials(studio):
    from multstudio.episode_four_media import production_report
    key=await setup(studio);await apply(studio[0],456)
    text='\n'.join(await production_report(key,studio[1]))
    assert '4; OpenAI: 1' in text and 'немых локальных сцен: 2' in text
    assert '0 из 3' in text and 'пока не определена' in text


async def test_cutaway_metadata_is_required_for_approval_to_assembly(studio):
    key=await setup(studio);await apply(studio[0],456);shots=await materials(studio,key)
    snap=await repo.snapshot('scene',shots[0]['id'])
    snap['assets']['video']={'approved':1,'metadata':repo.encoded(dict(duration_seconds=5,image_id=None,speech_id=None))}
    with pytest.raises(ValueError,match='немой план'):rules.validate_video_asset(snap)


async def test_local_final_shot_keeps_real_card_and_separate_russian_titles(studio):
    from multstudio.episode_four_media import end_titles
    key=await setup(studio);await apply(studio[0],456);shots=await materials(studio,key)
    await image(studio[2],await repo.snapshot('scene',shots[6]['id']))
    snap=await repo.snapshot('scene',shots[6]['id'])
    output=await clip(Renderer(studio[2]),snap)
    assert abs(await duration(output)-5)<.035
    destination=studio[2].path('titles.png');end_titles(['А вы уже создали свою карту желаний?','Продолжение следует…'],destination)
    with Image.open(destination) as im:
        assert im.getchannel('A').getbbox()[1]>=1488


async def test_materials_ui_and_cost_plan_are_available_only_to_owner(studio):
    from multstudio.router import create_router
    from test_multstudio_project_menu import event
    from test_handlers import dispatch_callback,ready_context
    key=await setup(studio);await apply(studio[0],456)
    router=create_router(456,studio[1],studio[2]);ctx=await ready_context()
    e=event('studio:e:'+key);await dispatch_callback(router,e,ctx)
    buttons=e.bot.send_message.await_args.kwargs['attachments'][0].payload.buttons
    assert any(b.payload=='studio:dream_materials:'+key for row in buttons for b in row)
    assert sum(b.payload.startswith('studio:s:') for row in buttons for b in row)==7
    e=event('studio:dream_materials:'+key);await dispatch_callback(router,e,ctx)
    assert 'настоящих изображения' in str(e.bot.send_message.await_args_list)
    e=event('studio:episode_plan:'+key);await dispatch_callback(router,e,ctx)
    assert 'Общая стоимость' in str(e.bot.send_message.await_args_list) and 'Маши' in str(e.bot.send_message.await_args_list)
    e=event('studio:dream_materials:'+key,actor=999);await dispatch_callback(router,e,ctx)
    e.bot.send_message.assert_not_awaited()


async def test_old_offsets_are_archived_without_inheriting_tail_trims(studio):
    key=await setup(studio);old=await repo.children('scene',key)
    await repo.set_scene_speech_start(old[0]['id'],456,1.5)
    await repo.set_scene_video_start(old[0]['id'],456,2)
    ep=await repo.get('episode',key)
    await repo.edit('episode',key,dict(ep['spec'],heygen_end_trim_seconds=2))
    await apply(studio[0],456)
    ep=await repo.get('episode',key)
    assert ep['spec']['archived_scene_editing'][old[0]['id']]==dict(speech_start=1.5,video_start=2)
    assert await repo.scene_editing(old[0]['id'])==dict(speech_start=0,video_start=0)
    assert not ep['spec'].get('heygen_end_trim_seconds')


async def test_local_card_worker_does_not_require_a_paid_image_api(studio,monkeypatch):
    from unittest.mock import Mock
    from multstudio.adapters import Providers
    from config import Config
    key=await setup(studio);await apply(studio[0],456);shots=await materials(studio,key)
    q=await repo.quote('scene',shots[3]['id'],'image',studio[1]);await repo.confirm(q['id'],456)
    job=await repo.claim()
    providers=Providers(studio[1],Config(token='test',photo_path=Path('unused')),studio[2])
    monkeypatch.setattr(providers,'preflight',Mock(side_effect=AssertionError('No paid API needed')))
    worker=Worker(None,456,studio[1],providers,studio[2]);worker.save_result=AsyncMock()
    await worker.execute(job)
    worker.save_result.assert_awaited_once()
    assert worker.save_result.await_args.kwargs['data'].startswith(b'\x89PNG')
    providers.preflight.assert_not_called()


async def test_revised_episode_cannot_assemble_without_background_music(studio):
    key=await setup(studio);await apply(studio[0],456)
    with pytest.raises(ValueError,match='фоновую музыку'):await repo.quote('episode',key,'assemble',studio[1])

"""Approved fourth episode: ten strict shots, adult speakers and reusable artwork."""
import copy
from dataclasses import replace
from unittest.mock import AsyncMock

import pytest

from config import Config
from multstudio import repository as repo,rules
from multstudio.episode_four import prepare,TITLE,SCENES
from multstudio.episode_two import prepare as second,prepare_jobs,plan,speech_profiles
from multstudio.episode_three import prepare as third
from multstudio.episode_three_revision import apply as revise
from multstudio.episode_three_trim import apply as trim
from multstudio.episode_three_delivery import apply as delivery
from multstudio.prompts import scene_image_prompt,validate_scene_image_references
from multstudio.adapters import Providers
from multstudio.render import Renderer,duration
from test_multstudio import studio,add_image
from test_multstudio_episode_three import make_clip


async def setup_episode(studio):
    project,_,_=studio
    await second(project,456);previous=await third(project,456)
    await revise(project,456);await trim(project,456)
    await delivery(project,456,'https://max.ru/se14596751_1_bot')
    return previous,await prepare(project,456)


async def test_ten_original_shots_50_seconds_exact_text_and_no_old_media_changes(studio):
    project,settings,assets=studio
    await second(project,456);await third(project,456)
    await revise(project,456);await trim(project,456);await delivery(project,456,'https://max.ru/se14596751_1_bot')
    before=[await repo.snapshot('episode',e['id']) for e in await repo.children('episode',project)]
    cast=await repo.children('character',project)
    voices={c['id']:await repo.character_speech(c['id']) for c in cast}
    key=await prepare(project,456);await prepare_jobs(key,settings)
    current=await repo.children('scene',key)
    assert len(current)==10 and sum(s['spec']['duration'] for s in current)==50
    assert [s['spec']['title'] for s in current]==[s[0] for s in SCENES]
    assert [s['spec'].get('dialogue',[{}])[0]['text'] for s in current if s['spec']['dialogue']]==[
        'Море — это здорово… А чего ещё я хочу?','Так… отпуск, новая кухня… Что ещё?',
        'Ань, почему у тебя одни покупки?','А разве путешествия и покупки — не мечты?',
        'А кем ты сама всегда хотела стать?','Я ведь давно мечтала о своём деле…',
        'Главное, чтобы начальник выходные давал!','Соберу-ка я все мечты в одну карту!']
    ids={c['name']:c['id'] for c in cast}
    for row in current:
        spec=row['spec'];rules.validate(spec)
        assert spec['camera']==spec['camera_direction']
        if spec['dialogue']:
            assert spec['characters']==[spec['speaker_id']] and spec['speaker_id']!=ids['Маша']
            assert spec['video_provider']=='heygen'
            assert spec['speech_speeds'][spec['speaker_id']]==(1.05 if spec['speaker_id']==ids['Аня'] else .95)
        else:assert spec['video_provider']=='heygen_video' and spec['speaker_id'] is None
    snap=await repo.snapshot('episode',key)
    assert snap['spec']['ending']==['Мечтать можно по-крупному!','Продолжение следует…']
    assert not snap['spec'].get('heygen_end_trim_seconds') and not snap['spec'].get('closing_card')
    assert [await repo.snapshot('episode',e['id']) for e in await repo.children('episode',project) if e['number']<4]==before
    assert await repo.children('character',project)==cast
    assert {c['id']:await repo.character_speech(c['id']) for c in cast}==voices
    async with repo.connect() as db:
        jobs=await repo.rows(db,"SELECT kind,status FROM cartoon_jobs WHERE target_id IN (SELECT id FROM cartoon_scenes WHERE episode_id=?) OR (target='episode' AND target_id=?)",(key,key))
    assert len(jobs)==29 and all(j['status']=='draft' for j in jobs)
    summary=await plan(key,settings)
    assert (summary['images'],summary['videos'],summary['utterances'],summary['silent'])==(10,10,8,2)
    assert await repo.claim() is None


async def test_correct_last_scene_anchor_new_studio_and_return_to_old_apartment(studio):
    previous,key=await setup_episode(studio)
    old=await repo.children('scene',previous);shots=await repo.children('scene',key)
    assert old[-1]['number']==9 and max(s['number'] for s in old)==11
    for row in shots:
        if row['number']==7:assert row['spec']['new_location'] and not row['spec'].get('location_reference_scene_id')
        else:assert row['spec']['location_reference_scene_id']==old[-1]['id']
    assert not (await repo.snapshot('scene',shots[6]['id']))['location_reference_required']
    assert shots[7]['spec']['location_reference_scene_id']!=shots[6]['id']


async def test_repeated_setup_preserves_manual_edits_ids_and_completed_jobs(studio):
    _,key=await setup_episode(studio)
    row=(await repo.children('scene',key))[0]
    await repo.edit('scene',row['id'],dict(row['spec'],visual='Ручная правка Ани у окна.'))
    before=await repo.snapshot('episode',key)
    assert await prepare(studio[0],456)==key
    assert await repo.snapshot('episode',key)==before
    with pytest.raises(ValueError):await prepare(studio[0],999)


async def test_framing_is_varied_and_face_stays_visible_without_closeups(studio):
    _,key=await setup_episode(studio)
    shots=await repo.children('scene',key)
    assert [s['spec']['framing'] for s in shots]==['establishing','medium_side','medium_wide','medium_side','knee_up','medium_wide','establishing','medium','over_shoulder','object_topdown']
    for row in shots:
        snap=await repo.snapshot('scene',row['id']);prompt=scene_image_prompt(snap)
        assert row['spec']['camera_direction'] in prompt
        assert 'Do not substitute a facial close-up' in prompt and 'mouth fully closed' in prompt
        assert 'Never create a collage' in prompt
        if row['spec']['dialogue']:assert 'adult speaker' in prompt and 'large enough for later lip-sync' in prompt


async def test_prop_images_are_required_and_sent_before_last_location_reference(studio,monkeypatch):
    previous,key=await setup_episode(studio);project,settings,assets=studio
    chars=await repo.children('character',project)
    for c in chars:await add_image(assets,await repo.snapshot('character',c['id']))
    old=await repo.children('scene',previous);anchor=old[-1]
    await add_image(assets,await repo.snapshot('scene',anchor['id']))
    shots=await repo.children('scene',key);ninth=shots[8]
    with pytest.raises(ValueError,match='изображения мечтаний'):await repo.quote('scene',ninth['id'],'image',settings)
    for ref in ninth['spec']['prop_reference_scene_ids']:
        await add_image(assets,await repo.snapshot('scene',ref))
    snap=await repo.snapshot('scene',ninth['id']);validate_scene_image_references(snap)
    assert len(snap['prop_references'])==2 and snap['location_reference']['target_id']==anchor['id']
    generate=AsyncMock(return_value=[b'png']);monkeypatch.setattr('multstudio.adapters._generate_image_bytes',generate)
    monkeypatch.setattr(assets,'asset_url',AsyncMock(side_effect=lambda a:'https://storage.example/'+a['id']))
    config=Config(token='test',photo_path=assets.root/'unused')
    snap['quote']={'model':'test-model','image_quality':'high'}
    await Providers(settings,config,assets).image(snap)
    args=generate.await_args
    assert args.args[0]=='https://storage.example/'+snap['cast'][0]['assets']['image']['id']
    refs=args.kwargs['style_reference_urls']
    assert refs==tuple('https://storage.example/'+r['image']['id'] for r in snap['prop_references'])+('https://storage.example/'+snap['location_reference']['id'],)
    assert 'PROP IMAGE REFERENCES' in args.kwargs['prompt_override']


async def test_prop_references_reject_later_scene_cycles(studio):
    _,key=await setup_episode(studio);shots=await repo.children('scene',key)
    first=shots[0]
    await repo.edit('scene',first['id'],dict(first['spec'],prop_reference_scene_ids=[shots[9]['id']]))
    with pytest.raises(ValueError,match='более ранней'):await repo.snapshot('scene',first['id'])


async def test_actual_assembly_stays_50_seconds_and_uses_own_final_titles(studio):
    _,key=await setup_episode(studio)
    snap=await repo.snapshot('episode',key)
    clip=await make_clip(studio[2],5,audio_seconds=5)
    renderer=Renderer(studio[2]);renderer.scene=AsyncMock(return_value=clip)
    output=await renderer.assemble(snap)
    assert abs(await duration(output)-50)<.035
    calls=renderer.scene.await_args_list
    assert [c.args[0]['number'] for c in calls]==list(range(1,11))
    assert all(c.args[1] is None for c in calls[:-1])
    assert calls[-1].args[1]==['Мечтать можно по-крупному!','Продолжение следует…']


async def test_occupied_episode_number_is_never_replaced(studio):
    _,key=await setup_episode(studio)
    async with repo.connect() as db:
        await db.execute("UPDATE cartoon_episodes SET title='Пользовательская серия' WHERE id=?",(key,));await db.commit()
    before=await repo.snapshot('episode',key)
    with pytest.raises(ValueError,match='занят'):await prepare(studio[0],456)
    assert await repo.snapshot('episode',key)==before


def test_explicit_camera_takes_priority_even_on_a_legacy_seed_moment():
    from multstudio.seed import SCENES as original
    spec=rules.normalize(dict(title=original[0][0],visual=original[0][3],characters=['adult'],
        dialogue=[dict(character_id='adult',text='Короткая реплика.')],
        framing='medium_wide',camera_direction='Общий план с видимым лицом и интерьером.'))
    prompt=scene_image_prompt(dict(spec=spec,world={},cast=[{'spec':{'description':'Взрослая Аня, 38 лет.'}}]))
    assert spec['camera_direction'] in prompt and original[0][3] in prompt

"""Removing a shot changes the episode montage, never archived media hashes."""
import pytest
from uuid import uuid4

from multstudio import repository as repo
from multstudio.episode_three_revision import apply as revise,PAIR_TITLE
from multstudio.episode_three_trim import apply,MIGRATION
from multstudio.episode_two import prepare_jobs
from multstudio.timeline import ordered,label
from test_multstudio import studio,add_image
from test_multstudio_anya_broll import old_series


async def test_remove_2a_keeps_all_scene_snapshots_files_and_other_episodes(studio):
    project,settings,assets=studio
    episode,_,_=await old_series(studio)
    await revise(project,456)
    scenes=await repo.children('scene',episode)
    for s in scenes:
        snap=await repo.snapshot('scene',s['id'])
        await add_image(assets,snap)
        path,_=await assets.write(b'saved video','mp4',job_id=s['id'],name='saved.mp4')
        await repo.add_asset(project,'scene',s['id'],'video',snap['context_hash'],path,assets.checksum(assets.path(path)),approved=1)
    before={s['id']:await repo.snapshot('scene',s['id']) for s in scenes}
    earlier=[await repo.snapshot('episode',e['id']) for e in await repo.children('episode',project) if e['number']<3]
    await apply(project,456)
    current=await repo.children('scene',episode)
    assert [label(s) for s in current]==['1','2','3','4','5','6','6а','7','8','9']
    assert sum(s['spec']['duration'] for s in current)==50
    snapshot=await repo.snapshot('episode',episode)
    assert snapshot['spec']['target_seconds']==50
    assert [s['id'] for s in snapshot['scenes']]==[s['id'] for s in current]
    assert snapshot['scenes'][-1]['number']==9
    pair=next(s for s in scenes if s['spec'].get('display_number')=='2а')
    assert snapshot['spec']['excluded_scene_ids']==[pair['id']]
    for s in scenes:
        saved=await repo.snapshot('scene',s['id'])
        assert saved==before[s['id']]
        assert assets.path(saved['assets']['video']['path']).read_bytes()==b'saved video'
    assert [await repo.snapshot('episode',e['id']) for e in await repo.children('episode',project) if e['number']<3]==earlier
    async with repo.connect() as db:
        assert len(await repo.rows(db,'SELECT * FROM cartoon_scenes WHERE episode_id=?',(episode,)))==11
    assert await repo.claim() is None


async def test_idempotent_and_following_bootstrap_preserve_archive_and_manual_edits(studio):
    project,settings,_=studio;episode,_,_=await old_series(studio)
    await revise(project,456);await apply(project,456)
    snap=await repo.get('episode',episode)
    spec=dict(snap['spec'],ending=['Новый финал'])
    await repo.edit('episode',episode,spec)
    before=await repo.get('episode',episode)
    await revise(project,456);await apply(project,456);await prepare_jobs(episode,settings)
    assert await repo.get('episode',episode)==before
    assert len(await repo.children('scene',episode))==10
    with pytest.raises(ValueError,match='Нет доступа'):await apply(project,999)


async def test_cancel_only_unsubmitted_removed_scene_and_obsolete_assembly(studio):
    project,_,_=studio;episode,_,_=await old_series(studio)
    await revise(project,456)
    scenes=await repo.children('scene',episode)
    pair=next(s for s in scenes if s['spec'].get('display_number')=='2а')
    other=next(s for s in scenes if s['number']==2)
    jobs=[]
    async with repo.connect() as db:
        for target,key,kind,status in [('scene',pair['id'],'video','queued'),('scene',pair['id'],'image','submitting'),
                ('scene',pair['id'],'video','done'),('scene',other['id'],'video','queued'),
                ('episode',episode,'assemble','queued'),('episode',episode,'assemble','done')]:
            job=uuid4().hex;jobs.append(job)
            await db.execute("INSERT INTO cartoon_jobs(id,project_id,target,target_id,kind,snapshot,fingerprint,status,estimate,expires) VALUES (?,?,?,?,?,'{}',?,?,0,0)",
                             (job,project,target,key,kind,job,status))
        await db.commit()
    await apply(project,456)
    assert [(await repo.job(j))['status'] for j in jobs]==['cancelled','submitting','done','queued','cancelled','done']


def test_excluded_scene_cannot_reappear_when_unspecified_order_appends_new_scenes():
    scenes=[{'id':'a','number':1},{'id':'b','number':2},{'id':'c','number':3}]
    assert [s['id'] for s in ordered(scenes,{'scene_order':['a'],'excluded_scene_ids':['b']})]==['a','c']
    assert [s['id'] for s in ordered(scenes,{'excluded_scene_ids':['b']})]==['a','c']
    for spec in ({'excluded_scene_ids':['unknown']},{'excluded_scene_ids':['b','b']},
                 {'excluded_scene_ids':'b'},{'excluded_scene_ids':['b'],'scene_order':['a','b']}):
        with pytest.raises(ValueError):ordered(scenes,spec)


async def test_missing_expected_insert_rolls_back_instead_of_hiding_other_shot(studio):
    project,_,_=studio;episode,_,_=await old_series(studio)
    before=await repo.snapshot('episode',episode)
    with pytest.raises(ValueError,match='2а'):await apply(project,456)
    assert await repo.snapshot('episode',episode)==before
    async with repo.connect() as db:
        assert not await repo.one(db,'SELECT name FROM cartoon_editing_migrations WHERE name=?',(MIGRATION,))


async def test_new_assembly_is_50_seconds_and_ending_stays_on_scene9(studio):
    from unittest.mock import AsyncMock
    from multstudio.render import Renderer,duration
    from test_multstudio_episode_three import make_clip
    project,_,assets=studio;episode,_,_=await old_series(studio)
    await revise(project,456);await apply(project,456)
    snap=await repo.snapshot('episode',episode)
    clip=await make_clip(assets,5,audio_seconds=5)
    renderer=Renderer(assets);renderer.scene=AsyncMock(return_value=clip)
    output=await renderer.assemble(snap)
    assert abs(await duration(output)-50)<.035
    calls=renderer.scene.await_args_list
    assert [c.args[0]['number'] for c in calls]==[1,2,3,4,5,6,11,7,8,9]
    assert all(c.args[1] is None for c in calls[:-1])
    assert calls[-1].args[1]==snap['spec']['ending']

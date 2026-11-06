import json
from types import SimpleNamespace
from unittest.mock import AsyncMock

import httpx
import pytest
import respx

from multstudio import repository as repo
from multstudio.router import create_router
from test_multstudio import studio, add_image, ready_scene
from test_multstudio_job_results import result_assets
from test_multstudio_project_menu import event
from test_handlers import dispatch_callback, ready_context


async def ready_archive(studio):
    project,settings,assets=studio
    _,episode=await ready_scene(studio)
    saved=[]
    for scene in await repo.children('scene',episode['id']):
        image_job=await repo.quote('scene',scene['id'],'image',settings)
        image=await assets.retain(json.loads(image_job['snapshot']),'image',
            data=('saved-image-'+str(scene['number'])).encode(),suffix='png',job_id=image_job['id'])
        await repo.approve_asset(image,456)
        await repo.update_job(image_job['id'],status='done',actual=30000,notified=1)
        video_job=await repo.quote('scene',scene['id'],'video',settings)
        data=('saved-video-'+str(scene['number'])).encode()
        video=await assets.retain(json.loads(video_job['snapshot']),'video',data=data,suffix='mp4',job_id=video_job['id'])
        await repo.update_job(video_job['id'],status='done',actual=27000,notified=1,
                              result_url='https://cdn.example/'+video_job['id']+'/video.mp4')
        before=await repo.snapshot('scene',scene['id'])
        saved.append((scene,await repo.job(image_job['id']),await repo.job(video_job['id']),video,data,before))
    masha=next(c for c in await repo.children('character',project) if c['name']=='Маша')
    await repo.edit('character',masha['id'],dict(masha['spec'],voice='jane'))
    scene,image_job,video_job,video,data,before=saved[4]
    assert not (await repo.snapshot('scene',scene['id']))['assets'].get('image')
    assert not (await repo.snapshot('scene',scene['id']))['assets'].get('video')
    return scene,image_job,video_job,video,data,before,saved


async def test_voice_change_restores_scene5_and_does_not_break_following_rooms(studio,result_assets):
    scene,image_job,video_job,video,data,before,saved=await ready_archive(studio)
    original_image=await repo.job_result(image_job['id'])
    original_video=await repo.job_result(video_job['id'])
    result=await repo.restore_scene_assets(scene['id'],actor=456,assets=result_assets)
    assert result['success'] and result['missing']==[]
    restored=await repo.snapshot('scene',scene['id'])
    assert restored['assets']['image']['id']==original_image['id']
    assert restored['assets']['video']['id']==original_video['id']==video
    assert restored['assets']['image']['context_hash']==original_image['context_hash']
    assert restored['assets']['video']['context_hash']==original_video['context_hash']
    assert result_assets.path(restored['assets']['video']['path']).read_bytes()==data
    for next_scene,_,_,_,_,prior in saved[5:]:
        after=await repo.snapshot('scene',next_scene['id'])
        assert after['context_hash']==prior['context_hash']
        assert after['assets']['image']['id']==prior['assets']['image']['id']
        assert after['assets']['video']['id']==prior['assets']['video']['id']
    for job in (image_job,video_job):
        after=await repo.job(job['id'])
        assert after['status']=='done' and after['actual']==job['actual'] and after['notified']==job['notified']
    assert await repo.claim() is None


async def test_restore_is_idempotent_and_keeps_original_archive_records(studio,result_assets):
    scene,image_job,video_job,video,data,before,_=await ready_archive(studio)
    first=await repo.restore_scene_assets(scene['id'],actor=456,assets=result_assets)
    await repo.init()  # A normal restart must retain explicit archive selections.
    async with repo.connect() as db:
        counts=await repo.one(db,'SELECT COUNT(*) AS n FROM cartoon_assets')
        events=await repo.one(db,"SELECT COUNT(*) AS n FROM cartoon_job_events WHERE event='scene_assets_restored'")
    second=await repo.restore_scene_assets(scene['id'],actor=456,assets=result_assets)
    assert first==second
    async with repo.connect() as db:
        assert await repo.one(db,'SELECT COUNT(*) AS n FROM cartoon_assets')==counts
        assert await repo.one(db,"SELECT COUNT(*) AS n FROM cartoon_job_events WHERE event='scene_assets_restored'")==events


async def test_missing_asset_rows_are_rebuilt_from_video_snapshot_and_job_files(studio,result_assets):
    scene,image_job,video_job,video,data,before,_=await ready_archive(studio)
    image=await repo.job_result(image_job['id'])
    # Put output at the canonical job path, then remove both registry rows.
    await result_assets.write(result_assets.path(image['path']).read_bytes(),'png',job_id=image_job['id'],name='image.png')
    async with repo.connect() as db:
        await db.execute('DELETE FROM cartoon_scene_image_selections WHERE scene_id=?',(scene['id'],))
        await db.execute('DELETE FROM cartoon_assets WHERE id IN (?,?)',(image['id'],video))
        await db.commit()
    result=await repo.restore_scene_assets(scene['id'],actor=456,assets=result_assets)
    snap=await repo.snapshot('scene',scene['id'])
    assert result['missing']==[] and snap['assets']['image']['id']==image['id']
    assert snap['assets']['image']['approved']==1
    assert result_assets.path(snap['assets']['video']['path']).read_bytes()==data


async def test_wrong_paths_and_scene_binding_are_recovered_by_job_and_sha(studio,result_assets):
    scene,image_job,video_job,video,data,before,_=await ready_archive(studio)
    image=await repo.job_result(image_job['id'])
    other=(await repo.children('scene',scene['episode_id']))[0]
    async with repo.connect() as db:
        await db.execute('UPDATE cartoon_assets SET path=?,target_id=? WHERE id=?',('wrong/path.png',other['id'],image['id']))
        await db.execute('UPDATE cartoon_assets SET path=? WHERE id=?',('wrong/path.mp4',video))
        await db.commit()
    result=await repo.restore_scene_assets(scene['id'],actor=456,assets=result_assets)
    snap=await repo.snapshot('scene',scene['id'])
    assert result['missing']==[] and snap['assets']['image']['target_id']==scene['id']
    assert result_assets.path(snap['assets']['video']['path']).read_bytes()==data


async def test_no_done_jobs_has_clear_error_and_never_uploads(studio,result_assets):
    episode=(await repo.children('episode',studio[0]))[0]
    scene=(await repo.children('scene',episode['id']))[0]
    with pytest.raises(ValueError,match=repo.RESTORE_NOT_FOUND):
        await repo.restore_scene_assets(scene['id'],actor=456,assets=result_assets)
    result_assets.publish.assert_not_awaited()


async def test_missing_files_ignores_non_done_and_other_scene_jobs(studio,result_assets,monkeypatch):
    scene,image_job,video_job,video,data,before,_=await ready_archive(studio)
    async with repo.connect() as db:
        await db.execute("UPDATE cartoon_jobs SET status='manual' WHERE id IN (?,?)",(image_job['id'],video_job['id']))
        await db.commit()
    with pytest.raises(ValueError,match=repo.RESTORE_NOT_FOUND):
        await repo.restore_scene_assets(scene['id'],actor=456,assets=result_assets)


async def test_fresh_generation_overrides_restored_results_normally(studio,result_assets):
    scene,image_job,video_job,video,data,before,_=await ready_archive(studio)
    await repo.restore_scene_assets(scene['id'],actor=456,assets=result_assets)
    current=await repo.snapshot('scene',scene['id'])
    new=await add_image(result_assets,current,approved=True)
    assert (await repo.snapshot('scene',scene['id']))['assets']['image']['id']==new


async def test_non_admin_cannot_restore_or_publish(studio,result_assets):
    scene,_,_,_,_,_,_=await ready_archive(studio)
    with pytest.raises(ValueError,match='доступ'):
        await repo.restore_scene_assets(scene['id'],actor=999,assets=result_assets)
    result_assets.publish.assert_not_awaited()
    pressed=event('studio:restore_assets:'+scene['id'],actor=999)
    await dispatch_callback(create_router(456,studio[1],result_assets),pressed,await ready_context())
    result_assets.publish.assert_not_awaited()


async def test_button_restores_and_edits_original_card_with_both_links(studio,result_assets):
    scene,_,_,_,_,_,_=await ready_archive(studio)
    router=create_router(456,studio[1],result_assets)
    opened=event('studio:s:'+scene['id'])
    await dispatch_callback(router,opened,await ready_context())
    buttons=[b for row in opened.bot.send_message.await_args.kwargs['attachments'][0].payload.buttons for b in row]
    assert any(getattr(b,'payload',None)=='studio:restore_assets:'+scene['id'] for b in buttons)
    bot=SimpleNamespace(send_message=AsyncMock(return_value=object()),send_callback=AsyncMock(return_value=object()),
                        edit_message=AsyncMock(return_value=object()))
    pressed=event('studio:restore_assets:'+scene['id'],bot=bot)
    await dispatch_callback(router,pressed,await ready_context())
    bot.edit_message.assert_awaited_once()
    sent=bot.edit_message.await_args.kwargs
    assert sent['message_id']=='mid-studio' and 'Восстановлено из архива' in sent['text']
    assert len(sent['attachments'])==1
    links=[b for row in sent['attachments'][0].payload.buttons for b in row if getattr(b,'url',None)]
    assert len(links)==2 and any('картинку' in b.text for b in links) and any('видео' in b.text for b in links)
    bot.send_message.assert_not_awaited()


async def test_missing_files_router_shows_requested_error(studio,result_assets):
    episode=(await repo.children('episode',studio[0]))[0]
    scene=(await repo.children('scene',episode['id']))[0]
    pressed=event('studio:restore_assets:'+scene['id'])
    await dispatch_callback(create_router(456,studio[1],result_assets),pressed,await ready_context())
    assert repo.RESTORE_NOT_FOUND in pressed.bot.send_message.await_args.kwargs['text']


@respx.mock
async def test_physical_files_and_cloud_missing_return_clear_error(studio,result_assets,monkeypatch):
    scene,image_job,video_job,video,data,before,_=await ready_archive(studio)
    for job in (image_job,video_job):
        asset=await repo.job_result(job['id'])
        result_assets.path(asset['path']).unlink()
    monkeypatch.setattr('multstudio.assets.public_url',AsyncMock())
    respx.get(video_job['result_url']).mock(return_value=httpx.Response(404))
    with pytest.raises(ValueError,match=repo.RESTORE_NOT_FOUND):
        await repo.restore_scene_assets(scene['id'],actor=456,assets=result_assets)
    result_assets.publish.assert_not_awaited()


@respx.mock
async def test_missing_video_is_downloaded_from_saved_public_url(studio,result_assets,monkeypatch):
    scene,image_job,video_job,video,data,before,_=await ready_archive(studio)
    asset=await repo.job_result(video_job['id'])
    result_assets.path(asset['path']).unlink()
    monkeypatch.setattr('multstudio.assets.public_url',AsyncMock())
    route=respx.get(video_job['result_url']).mock(return_value=httpx.Response(200,content=data))
    result=await repo.restore_scene_assets(scene['id'],actor=456,assets=result_assets)
    assert result['missing']==[] and route.call_count==1
    assert result_assets.path(asset['path']).read_bytes()==data


@respx.mock
async def test_corrupt_video_is_not_silently_accepted_or_overwritten(studio,result_assets,monkeypatch):
    scene,image_job,video_job,video,data,before,_=await ready_archive(studio)
    asset=await repo.job_result(video_job['id'])
    result_assets.path(asset['path']).write_bytes(b'wrong-content')
    monkeypatch.setattr('multstudio.assets.public_url',AsyncMock())
    respx.get(video_job['result_url']).mock(return_value=httpx.Response(404))
    result=await repo.restore_scene_assets(scene['id'],actor=456,assets=result_assets)
    assert result['missing']==['video']
    assert result_assets.path(asset['path']).read_bytes()==b'wrong-content'


async def test_last_done_image_is_selected_and_scene_upload_kind_is_supported(studio,result_assets):
    scene,image_job,video_job,video,data,before,_=await ready_archive(studio)
    from uuid import uuid4
    latest=uuid4().hex
    async with repo.connect() as db:
        await db.execute('''INSERT INTO cartoon_jobs
            (id,project_id,target,target_id,kind,snapshot,fingerprint,status,estimate,actual,expires,notified)
            SELECT ?,project_id,target,target_id,'scene_image',snapshot,fingerprint,'done',0,0,0,1
            FROM cartoon_jobs WHERE id=?''',(latest,image_job['id']))
        await db.commit()
    image=await result_assets.retain(before,'image',data=b'latest-custom-image',suffix='png',job_id=latest)
    await repo.approve_asset(image,456)
    result=await repo.restore_scene_assets(scene['id'],actor=456,assets=result_assets)
    assert result['restored']['image']==image


async def test_s3_failure_does_not_claim_files_missing_or_start_generation(studio,result_assets):
    scene,image_job,video_job,video,data,before,_=await ready_archive(studio)
    result_assets.publish=AsyncMock(side_effect=RuntimeError('storage unavailable'))
    with pytest.raises(ValueError,match='файлы найдены'):
        await repo.restore_scene_assets(scene['id'],actor=456,assets=result_assets)
    assert (await repo.job(video_job['id']))['status']=='done'
    assert await repo.claim() is None


async def test_default_function_uses_configured_files_directory(studio,result_assets,monkeypatch):
    scene,image_job,video_job,video,data,before,_=await ready_archive(studio)
    monkeypatch.setenv('STUDIO_FILES_DIR',str(result_assets.root))
    result=await repo.restore_scene_assets(scene['id'])
    assert result['missing']==[]
    result_assets.publish.assert_not_awaited()


async def test_failed_card_edit_falls_back_to_new_card_with_one_keyboard(studio,result_assets):
    scene,_,_,_,_,_,_=await ready_archive(studio)
    bot=SimpleNamespace(send_message=AsyncMock(return_value=object()),send_callback=AsyncMock(return_value=object()),
                        edit_message=AsyncMock(side_effect=RuntimeError('MAX unavailable')))
    await dispatch_callback(create_router(456,studio[1],result_assets),event('studio:restore_assets:'+scene['id'],bot=bot),
                            await ready_context())
    assert 'Восстановлено из архива' in bot.send_message.await_args.kwargs['text']
    assert len(bot.send_message.await_args.kwargs['attachments'])==1

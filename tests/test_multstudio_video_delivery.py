import json
from types import SimpleNamespace
from unittest.mock import AsyncMock

import httpx
import pytest
import respx
from maxapi.types import AttachmentUpload

from multstudio import repository as repo
from multstudio.router import create_router
from multstudio.worker import Worker
from test_handlers import dispatch_callback, ready_context
from test_multstudio import studio, ready_scene, add_image
from test_multstudio_job_results import result_assets, done_image
from test_multstudio_project_menu import event


async def video_job(studio,assets,*,done=True):
    _,settings,_=studio
    _,episode=await ready_scene(studio)
    scene=(await repo.children('scene',episode['id']))[1]
    await add_image(assets,await repo.snapshot('scene',scene['id']))
    job=await repo.quote('scene',scene['id'],'video',settings)
    await repo.update_job(job['id'],actual=27000)
    if done:
        await assets.retain(json.loads(job['snapshot']),'video',data=b'mp4-result',suffix='mp4',job_id=job['id'])
        await repo.update_job(job['id'],status='done',notified=0)
    return await repo.job(job['id']),scene


def bot():
    return SimpleNamespace(send_message=AsyncMock(return_value=object()))


def attachment():
    return AttachmentUpload(type='video',payload={'token':'max-video-result'})


async def test_done_video_is_sent_inline_once_with_one_keyboard(studio,result_assets,monkeypatch):
    job,scene=await video_job(studio,result_assets)
    admin=bot()
    upload=AsyncMock(return_value=attachment())
    monkeypatch.setattr('media_service.upload_video_for_max',upload)
    worker=Worker(admin,456,studio[1],SimpleNamespace(),result_assets)
    await worker.notices()
    item=await repo.job(job['id'])
    asset=await repo.job_result(job['id'])
    upload.assert_awaited_once_with(admin,item['result_url'])
    message=admin.send_message.await_args.kwargs
    assert message['user_id']==456
    assert message['text']=='🎬 Сцена 2 оживлена! Посмотрите результат.'
    media,keyboard=message['attachments']
    assert media.type=='video' and keyboard.type=='inline_keyboard'
    buttons=[b for row in keyboard.payload.buttons for b in row]
    assert [(b.text,b.payload) for b in buttons]==[
        ('✅ Утвердить','studio:approve_video:'+asset['id']),
        ('🔄 Перегенерировать','studio:make:video:scene:'+scene['id'])]
    assert item['notified']==1 and item['status']=='done' and item['actual']==27000
    await worker.notices()
    admin.send_message.assert_awaited_once()
    assert worker._media_uploads=={}
    assert await repo.claim() is None


async def test_poll_completion_immediately_saves_and_sends_video(studio,result_assets,monkeypatch):
    job,_=await video_job(studio,result_assets,done=False)
    await repo.update_job(job['id'],status='polling',provider_id='known-kling-task')
    async def download(url,suffix,**kwargs):
        assert url=='https://cdn.example/kling-result.mp4'
        return await result_assets.write(b'kling-mp4',suffix,**kwargs)
    monkeypatch.setattr(result_assets,'download',download)
    upload=AsyncMock(return_value=attachment())
    monkeypatch.setattr('media_service.upload_video_for_max',upload)
    providers=SimpleNamespace(kling=SimpleNamespace(
        poll=AsyncMock(return_value={'url':'https://cdn.example/kling-result.mp4'}),submit=AsyncMock()))
    admin=bot()
    await Worker(admin,456,studio[1],providers,result_assets).execute(await repo.job(job['id']))
    saved=await repo.job(job['id'])
    assert saved['status']=='done' and saved['notified']==1
    assert saved['actual']==27000 and saved['provider_id']=='known-kling-task'
    assert result_assets.path(job['id']+'/video.mp4').read_bytes()==b'kling-mp4'
    providers.kling.poll.assert_awaited_once_with('known-kling-task')
    providers.kling.submit.assert_not_awaited()
    admin.send_message.assert_awaited_once()


@pytest.mark.parametrize('send_none',[False,True])
async def test_failed_send_keeps_done_and_retries_same_max_token_without_generation(studio,result_assets,monkeypatch,send_none):
    job,_=await video_job(studio,result_assets)
    admin=bot()
    admin.send_message.side_effect=[None if send_none else RuntimeError('attachment.not.ready'),object()]
    upload=AsyncMock(return_value=attachment())
    monkeypatch.setattr('media_service.upload_video_for_max',upload)
    worker=Worker(admin,456,studio[1],SimpleNamespace(),result_assets)
    await worker.notices()
    failed=await repo.job(job['id'])
    assert failed['notified']==0 and failed['status']=='done' and failed['actual']==27000
    await worker.notices()
    upload.assert_awaited_once()
    assert admin.send_message.await_count==2
    assert (await repo.job(job['id']))['notified']==1
    assert await repo.claim() is None


async def test_failed_upload_can_retry_after_worker_restart(studio,result_assets,monkeypatch):
    job,_=await video_job(studio,result_assets)
    upload=AsyncMock(side_effect=[RuntimeError('MAX temporary'),attachment()])
    monkeypatch.setattr('media_service.upload_video_for_max',upload)
    admin=bot()
    await Worker(admin,456,studio[1],SimpleNamespace(),result_assets).notices()
    admin.send_message.assert_not_awaited()
    assert (await repo.job(job['id']))['notified']==0
    await Worker(admin,456,studio[1],SimpleNamespace(),result_assets).notices()
    assert upload.await_count==2
    assert (await repo.job(job['id']))['notified']==1


async def test_historical_text_notification_can_be_redelivered_from_job_card(studio,result_assets,monkeypatch):
    job,_=await video_job(studio,result_assets)
    await repo.update_job(job['id'],notified=1)
    router=create_router(456,studio[1],result_assets)
    pressed=event('studio:job:'+job['id'])
    await dispatch_callback(router,pressed,await ready_context())
    message=pressed.bot.send_message.await_args.kwargs
    buttons=[b for row in message['attachments'][0].payload.buttons for b in row]
    assert any(getattr(b,'payload',None)=='studio:resend_result:'+job['id'] for b in buttons)
    result_url=(await repo.job(job['id']))['result_url']
    assert any(getattr(b,'url',None)==result_url for b in buttons)
    await dispatch_callback(router,event('studio:resend_result:'+job['id']),await ready_context())
    assert (await repo.job(job['id']))['notified']==0
    monkeypatch.setattr('media_service.upload_video_for_max',AsyncMock(return_value=attachment()))
    admin=bot()
    await Worker(admin,456,studio[1],SimpleNamespace(),result_assets).notices()
    assert admin.send_message.await_args.kwargs['attachments'][0].type=='video'
    assert await repo.claim() is None


async def test_video_approval_is_authorized_and_does_not_start_generation(studio,result_assets):
    job,_=await video_job(studio,result_assets)
    asset=await repo.job_result(job['id'])
    router=create_router(456,studio[1],result_assets)
    denied=event('studio:approve_video:'+asset['id'],actor=999)
    await dispatch_callback(router,denied,await ready_context())
    assert (await repo.job_result(job['id']))['approved']==0
    await dispatch_callback(router,event('studio:approve_video:'+asset['id']),await ready_context())
    assert (await repo.job_result(job['id']))['approved']==1
    assert await repo.claim() is None


async def test_video_approval_rejects_nonvideo_asset(studio,result_assets):
    job,asset=await done_image(studio,result_assets)
    with pytest.raises(ValueError):
        await repo.approve_asset(asset,456,expected_kind='video')
    assert (await repo.job_result(job['id']))['approved']==0


@pytest.mark.parametrize('legacy',[False,True])
async def test_images_and_legacy_character_reference_still_arrive_inline(studio,result_assets,monkeypatch,legacy):
    job,_=await done_image(studio,result_assets,legacy_kind=legacy)
    await repo.update_job(job['id'],notified=0)
    image=AttachmentUpload(type='image',payload={'token':'image-token'})
    monkeypatch.setattr('image_service.upload_image_for_max',AsyncMock(return_value=image))
    admin=bot()
    await Worker(admin,456,studio[1],SimpleNamespace(),result_assets).notices()
    assert [a.type for a in admin.send_message.await_args.kwargs['attachments']]==['image','inline_keyboard']
    assert (await repo.job(job['id']))['notified']==1


async def test_existing_max_uploader_really_uploads_video_type(studio,result_assets):
    job,_=await video_job(studio,result_assets)
    asset,url=await result_assets.result_for_job(job)
    admin=bot()
    from pathlib import Path
    async def upload(media):
        assert media.type=='video'
        assert Path(media.path).read_bytes()==b'mp4-result'
        return attachment()
    admin.upload_media=AsyncMock(side_effect=upload)
    with respx.mock() as api:
        api.get(url).mock(return_value=httpx.Response(200,content=b'mp4-result'))
        await Worker(admin,456,studio[1],SimpleNamespace(),result_assets).notices()
    admin.upload_media.assert_awaited_once()
    admin.send_message.assert_awaited_once()
    assert (await repo.job(job['id']))['notified']==1


async def done_media(studio,assets,kind):
    item,scene=await video_job(studio,assets,done=False)
    snap=json.loads(item['snapshot'])
    target,target_id='scene',scene['id']
    if kind in ('assemble','episode_assembly'):
        target,target_id='episode',scene['episode_id']
        snap=await repo.snapshot(target,target_id)
    actual=0 if target=='episode' else 27000
    async with repo.connect() as db:
        await db.execute('UPDATE cartoon_jobs SET kind=?,target=?,target_id=?,snapshot=?,status=\'done\',actual=?,notified=0 WHERE id=?',
                         (kind,target,target_id,repo.encoded(snap),actual,item['id']))
        await db.commit()
    asset_kind='assemble' if target=='episode' else 'speech'
    await assets.retain(snap,asset_kind,data=b'ID3-audio' if kind=='speech' else b'final-mp4',
                         suffix='mp3' if kind=='speech' else 'mp4',job_id=item['id'])
    return await repo.job(item['id'])


@pytest.mark.parametrize('kind',['speech','assemble','episode_assembly'])
async def test_audio_and_final_episode_are_sent_with_correct_types_and_text(studio,result_assets,monkeypatch,kind):
    job=await done_media(studio,result_assets,kind)
    expected='audio' if kind=='speech' else 'video'
    media=AttachmentUpload(type=expected,payload={'token':'result-token'})
    upload=AsyncMock(return_value=media)
    monkeypatch.setattr('media_service.upload_audio_for_max' if expected=='audio' else 'media_service.upload_video_for_max',upload)
    admin=bot()
    worker=Worker(admin,456,studio[1],SimpleNamespace(),result_assets)
    await worker.notices()
    sent=admin.send_message.await_args.kwargs
    assert sent['user_id']==456 and sent['attachments'][0].type==expected
    if kind=='speech':
        assert sent['text']=='🎧 Озвучка готова' and len(sent['attachments'])==1
    else:
        assert sent['text']=='🎉 Мультфильм собран! Скачайте или отправьте в соцсети.'
        assert len(sent['attachments'])==2 and sent['attachments'][1].type=='inline_keyboard'
        button=sent['attachments'][1].payload.buttons[0][0]
        assert button.text=='📥 Скачать MP4' and button.url==(await repo.job(job['id']))['result_url']
    assert (await repo.job(job['id']))['notified']==1
    await worker.notices()
    upload.assert_awaited_once()
    assert await repo.claim() is None


async def test_speech_completion_sends_audio_immediately_without_second_tts_call(studio,result_assets,monkeypatch):
    project,settings,_=studio
    scene,_=await ready_scene(studio)
    await add_image(result_assets,await repo.snapshot('scene',scene['id']))
    job=await repo.quote('scene',scene['id'],'speech',settings)
    await repo.budget(project,10000000)
    await repo.confirm(job['id'],456)
    result_assets.root.mkdir(parents=True,exist_ok=True)
    merged=result_assets.path('merged.mp3')
    merged.write_bytes(b'ID3-merged-speech')
    providers=SimpleNamespace(preflight=lambda kind:None,speech_line=AsyncMock(return_value=b'ID3-line'))
    uploader=AsyncMock(return_value=AttachmentUpload(type='audio',payload={'token':'tts-audio'}))
    monkeypatch.setattr('media_service.upload_audio_for_max',uploader)
    admin=bot()
    worker=Worker(admin,456,settings,providers,result_assets)
    worker.renderer.speech=AsyncMock(return_value=merged)
    await worker.execute(await repo.claim())
    assert (await repo.job(job['id']))['status']=='done'
    assert (await repo.job(job['id']))['notified']==1
    assert admin.send_message.await_args.kwargs['attachments'][0].type=='audio'
    providers.speech_line.assert_awaited_once()
    await worker.notices()
    providers.speech_line.assert_awaited_once()


async def test_real_audio_uploader_uses_audio_type(studio,result_assets):
    job=await done_media(studio,result_assets,'speech')
    _,url=await result_assets.result_for_job(job)
    admin=bot()
    from pathlib import Path
    async def upload(media):
        assert media.type=='audio' and Path(media.path).read_bytes()==b'ID3-audio'
        return AttachmentUpload(type='audio',payload={'token':'max-tts-audio'})
    admin.upload_media=AsyncMock(side_effect=upload)
    with respx.mock() as api:
        api.get(url).mock(return_value=httpx.Response(200,content=b'ID3-audio'))
        await Worker(admin,456,studio[1],SimpleNamespace(),result_assets).notices()
    admin.upload_media.assert_awaited_once()
    assert (await repo.job(job['id']))['notified']==1


@pytest.mark.parametrize('kind',['speech','assemble','episode_assembly'])
async def test_older_audio_and_episode_jobs_offer_free_redelivery(studio,result_assets,kind):
    job=await done_media(studio,result_assets,kind)
    await repo.update_job(job['id'],notified=1)
    router=create_router(456,studio[1],result_assets)
    pressed=event('studio:job:'+job['id'])
    await dispatch_callback(router,pressed,await ready_context())
    buttons=[b for row in pressed.bot.send_message.await_args.kwargs['attachments'][0].payload.buttons for b in row]
    assert any(getattr(b,'payload',None)=='studio:resend_result:'+job['id'] for b in buttons)
    await dispatch_callback(router,event('studio:resend_result:'+job['id']),await ready_context())
    item=await repo.job(job['id'])
    assert item['notified']==0 and item['status']=='done' and item['actual']==job['actual']
    assert await repo.claim() is None


async def test_failed_audio_send_reuses_audio_token_and_preserves_cost(studio,result_assets,monkeypatch):
    job=await done_media(studio,result_assets,'speech')
    upload=AsyncMock(return_value=AttachmentUpload(type='audio',payload={'token':'same-audio'}))
    monkeypatch.setattr('media_service.upload_audio_for_max',upload)
    admin=bot()
    admin.send_message.side_effect=[RuntimeError('MAX temporary'),object()]
    worker=Worker(admin,456,studio[1],SimpleNamespace(),result_assets)
    await worker.notices()
    assert (await repo.job(job['id']))['notified']==0
    await worker.notices()
    upload.assert_awaited_once()
    assert (await repo.job(job['id']))['actual']==job['actual']


async def test_resend_denied_to_non_admin(studio,result_assets):
    job=await done_media(studio,result_assets,'speech')
    await repo.update_job(job['id'],notified=1)
    await dispatch_callback(create_router(456,studio[1],result_assets),
                            event('studio:resend_result:'+job['id'],actor=999),await ready_context())
    assert (await repo.job(job['id']))['notified']==1

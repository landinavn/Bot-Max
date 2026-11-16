"""No saved avatar is created for new scenes; old paid job states stay resumable."""
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock

import httpx
import pytest

from multstudio import repository as repo
from multstudio.worker import Worker
from multstudio.heygen_video import StudioHeyGen
from test_multstudio import studio
from test_multstudio_episode_three import ready,attach_audio


async def new_video(studio):
    snap=await attach_audio(studio,await ready(studio))
    job=await repo.quote('scene',snap['id'],'video',studio[1])
    await repo.confirm(job['id'],456)
    return job,snap


async def test_new_job_submits_once_with_selected_image_and_saved_speech_then_only_polls(studio):
    job,snap=await new_video(studio)
    requests=[]
    def handle(request):
        requests.append(request)
        assert request.url.path=='/v3/videos'
        return httpx.Response(200,json={'data':{'video_id':'new-video'}})
    adapter=StudioHeyGen('test-key',httpx.MockTransport(handle))
    providers=SimpleNamespace(studio_heygen=adapter,preflight=lambda kind:None)
    assets=studio[2]
    assets.asset_url=AsyncMock(side_effect=['https://example.org/approved-image.png','https://example.org/exact-speech.mp3'])
    worker=Worker(SimpleNamespace(),456,studio[1],providers,assets)
    await worker.execute(await repo.claim())
    saved=await repo.job(job['id'])
    assert saved['status']=='polling' and saved['provider_id']=='new-video'
    assert json.loads(saved['usage'])['input_type']=='image'
    assert len(requests)==1 and json.loads(requests[0].content)['audio_url']=='https://example.org/exact-speech.mp3'
    assert json.loads(requests[0].content)['image']['url']=='https://example.org/approved-image.png'
    assert assets.asset_url.await_args_list[0].args[0]['id']==snap['assets']['image']['id']
    assert assets.asset_url.await_args_list[1].args[0]['id']==snap['assets']['speech']['id']
    adapter.poll=AsyncMock(return_value=None)
    await repo.update_job(job['id'],next_attempt=0)
    worker=Worker(SimpleNamespace(),456,studio[1],providers,assets)
    await worker.execute(await repo.claim())
    adapter.poll.assert_awaited_once_with('new-video')
    assert len(requests)==1


async def test_unknown_direct_video_post_is_manual_and_never_retried(studio):
    job,_=await new_video(studio)
    requests=[]
    def handle(request):
        requests.append(request)
        raise httpx.ReadTimeout('unknown result',request=request)
    adapter=StudioHeyGen('test-key',httpx.MockTransport(handle))
    assets=studio[2];assets.asset_url=AsyncMock(return_value='https://example.org/media')
    worker=Worker(SimpleNamespace(),456,studio[1],SimpleNamespace(studio_heygen=adapter,preflight=lambda k:None),assets)
    await worker.execute(await repo.claim())
    assert (await repo.job(job['id']))['status']=='manual'
    assert await repo.claim() is None and len(requests)==1
    assert requests[0].url.path=='/v3/videos'


async def test_failed_media_publication_cannot_start_paid_post(studio):
    job,_=await new_video(studio)
    adapter=SimpleNamespace(ready=lambda:None,scene_video=AsyncMock())
    assets=studio[2];assets.asset_url=AsyncMock(side_effect=[ 'https://example.org/image.png',ValueError('Публикация MP3 недоступна')])
    worker=Worker(SimpleNamespace(),456,studio[1],SimpleNamespace(studio_heygen=adapter,preflight=lambda k:None),assets)
    await worker.execute(await repo.claim())
    assert (await repo.job(job['id']))['status']=='preflight_failed'
    adapter.scene_video.assert_not_awaited()


@pytest.mark.parametrize('status',[400,401,402,429,500])
async def test_rejection_never_falls_back_to_paid_avatar_creation(status):
    from heygen import HeyGenError
    requests=[]
    def handle(request):
        requests.append(request)
        return httpx.Response(status,json={'error':'rejected'})
    adapter=StudioHeyGen('test-key',httpx.MockTransport(handle))
    with pytest.raises(HeyGenError):
        await adapter.scene_video('https://example.org/image.png','https://example.org/voice.mp3','job-video')
    assert len(requests)==1 and requests[0].url.path=='/v3/videos'

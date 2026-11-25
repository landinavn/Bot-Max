"""Documented PiAPI Kling Avatar contract; no live/paid requests."""
import json
from dataclasses import replace

import httpx
import pytest

from multstudio.adapters import KlingAdapter,ProviderFailed
from multstudio.settings import Settings
from pathlib import Path


async def test_avatar_posts_exact_image_and_original_mp3_once():
    requests=[]
    def respond(request):
        requests.append(request)
        return httpx.Response(200,json={'data':{'task_id':'known-avatar-task'}})
    client=KlingAdapter('private-test-key',transport=httpx.MockTransport(respond))
    assert await client.avatar('https://cdn.example/masha.png','https://cdn.example/approved.mp3','Она помогает маме.')=='known-avatar-task'
    assert len(requests)==1
    assert json.loads(requests[0].content)=={'model':'kling','task_type':'avatar',
        'input':{'image_url':'https://cdn.example/masha.png','local_dubbing_url':'https://cdn.example/approved.mp3',
                 'prompt':'Она помогает маме.','mode':'std','batch_size':1},'config':{'service_mode':'public'}}
    assert requests[0].headers['X-API-Key']=='private-test-key'


@pytest.mark.parametrize('failure',[400,500,'missing_task','timeout'])
async def test_avatar_uncertain_submission_has_no_automatic_retries(failure):
    requests=[]
    def respond(request):
        requests.append(request)
        if failure=='timeout':raise httpx.ReadTimeout('timeout')
        return httpx.Response(failure if isinstance(failure,int) else 200,json={'data':{}})
    client=KlingAdapter('private-test-key',transport=httpx.MockTransport(respond))
    with pytest.raises((ProviderFailed,httpx.RequestError)):
        await client.avatar('https://cdn.example/masha.png','https://cdn.example/approved.mp3','Текст')
    assert len(requests)==1


def test_avatar_estimate_uses_actual_audio_duration_and_configurable_rate():
    settings=Settings(root=Path('/tmp'),image_model='test')
    snap={'spec':{'duration':5,'video_provider':'kling_avatar'},'assets':{'speech':{'metadata':json.dumps({'audio_duration_seconds':3.528})}}}
    q=settings.quote('video',snap)
    assert q['estimate']==183456 and q['billing_seconds']==3.528 and q['duration']==5
    assert q['model']=='kling/avatar/std'
    assert replace(settings,kling_avatar_second_price=100000).quote('video',snap)['estimate']==352800

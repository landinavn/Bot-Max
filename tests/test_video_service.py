import asyncio
import json
import logging
from types import SimpleNamespace
from unittest.mock import AsyncMock

import httpx
import pytest
import respx

import video_service as video


@pytest.fixture(autouse=True)
def settings(monkeypatch):
    monkeypatch.setattr(video.Config, 'from_env', lambda: SimpleNamespace(piapi_key='test-piapi-secret'))


@pytest.fixture
def fast_poll(monkeypatch):
    sleep = AsyncMock()
    monkeypatch.setattr(video.asyncio, 'sleep', sleep)
    return sleep


@respx.mock
async def test_create_exact_kling_task_and_log(caplog):
    route = respx.post(video.TASK_URL).mock(return_value=httpx.Response(200, json={'code': 200, 'data': {'task_id': 'task-123'}}))
    with caplog.at_level(logging.INFO, logger=video.__name__):
        assert await video.create_video_task('https://images.example/avatar.png', 'Snow and glowing lights') == 'task-123'
    request = route.calls[0].request
    assert request.headers['X-API-Key'] == 'test-piapi-secret'
    assert request.headers['Content-Type'] == 'application/json'
    assert json.loads(request.content) == {
        'model': 'kling', 'task_type': 'video_generation',
        'input': {'image_url': 'https://images.example/avatar.png', 'prompt': 'Snow and glowing lights',
                  'version': '3.0-turbo', 'mode': 'std', 'duration': 5},
        'config': {'service_mode': 'public'},
    }
    assert route.call_count == 1
    assert 'task-123' in caplog.text and 'создаю' in caplog.text
    assert 'test-piapi-secret' not in caplog.text


@respx.mock
async def test_poll_processing_then_completed(fast_poll, caplog):
    route = respx.get(video.TASK_URL + '/task-123').mock(side_effect=[
        httpx.Response(200, json={'data': {'status': 'processing'}}),
        httpx.Response(200, json={'data': {'status': 'completed', 'output': {'video': 'https://video.example/result.mp4'}}}),
    ])
    with caplog.at_level(logging.INFO, logger=video.__name__):
        assert await video.poll_video_task('task-123') == 'https://video.example/result.mp4'
    assert route.call_count == 2
    assert [call.args for call in fast_poll.await_args_list] == [(10,), (10,)]
    assert all(call.request.headers['X-API-Key'] == 'test-piapi-secret' for call in route.calls)
    assert 'processing' in caplog.text and 'completed' in caplog.text and 'видео готово' in caplog.text


@respx.mock
async def test_failed_task_keeps_provider_reason_and_hides_key(fast_poll):
    respx.get(video.TASK_URL + '/failed').respond(200, json={'data': {'status': 'failed', 'error': {'message': 'moderation failed test-piapi-secret'}}})
    with pytest.raises(video.VideoServiceError, match='moderation failed') as error:
        await video.poll_video_task('failed')
    assert 'test-piapi-secret' not in str(error.value)


@respx.mock
@pytest.mark.parametrize('response,match', [
    (httpx.Response(403, text='invalid key test-piapi-secret'), 'HTTP 403'),
    (httpx.Response(200, text='not JSON'), 'JSON'),
    (httpx.Response(200, json=[]), 'объект'),
    (httpx.Response(200, json={'code': 500, 'message': 'unavailable'}), 'unavailable'),
    (httpx.Response(200, json={'data': {}}), 'task_id'),
])
async def test_creation_errors_do_not_retry_or_expose_key(response, match):
    route = respx.post(video.TASK_URL).mock(return_value=response)
    with pytest.raises(video.VideoServiceError, match=match) as error:
        await video.create_video_task('https://image.example/a.png', 'Animate')
    assert route.call_count == 1
    assert 'test-piapi-secret' not in str(error.value)


@respx.mock
@pytest.mark.parametrize('response,match', [
    (httpx.Response(503, text='unavailable'), 'HTTP 503'),
    (httpx.Response(200, json={}), 'Нет data'),
    (httpx.Response(200, json={'data': {'status': 'completed'}}), 'data.output.video'),
    (httpx.Response(200, json={'data': {'status': 'completed', 'output': {'video': 'javascript:bad'}}}), 'data.output.video'),
])
async def test_poll_rejects_bad_result(fast_poll, response, match):
    respx.get(video.TASK_URL + '/task').mock(return_value=response)
    with pytest.raises(video.VideoServiceError, match=match):
        await video.poll_video_task('task')


@respx.mock
@pytest.mark.parametrize('error_type', [httpx.ReadTimeout, httpx.ConnectError])
async def test_request_timeout_and_connection_error(error_type):
    route = respx.post(video.TASK_URL).mock(side_effect=error_type('test-piapi-secret'))
    with pytest.raises(video.VideoServiceError) as error:
        await video.create_video_task('https://image.example/a.png', 'Animate')
    assert 'test-piapi-secret' not in str(error.value)
    assert route.call_count == 1


@respx.mock
async def test_total_timeout_also_cancels_inflight_http(monkeypatch):
    monkeypatch.setattr(video, 'POLL_INTERVAL', 0)
    entered = asyncio.Event()
    async def slow(request):
        entered.set()
        await asyncio.Event().wait()
    respx.get(video.TASK_URL + '/slow').mock(side_effect=slow)
    with pytest.raises(TimeoutError, match='Таймаут ожидания PiAPI'):
        await video.poll_video_task('slow', timeout=0.1)
    assert entered.is_set()


async def test_timeout_while_sleeping():
    with pytest.raises(TimeoutError):
        await video.poll_video_task('queued', timeout=0.01)


async def test_cancellation_is_not_converted_to_provider_error(monkeypatch):
    monkeypatch.setattr(video.asyncio, 'sleep', AsyncMock(side_effect=asyncio.CancelledError))
    with pytest.raises(asyncio.CancelledError):
        await video.poll_video_task('task')


@pytest.mark.parametrize('image,prompt', [('http://image.example/a.png', 'Animate'), ('https://image.example/a.png', ' '), ('bad', 'Animate')])
async def test_invalid_input_does_not_submit_paid_task(image, prompt):
    with pytest.raises(ValueError):
        await video.create_video_task(image, prompt)


async def test_missing_key_is_clear(monkeypatch):
    monkeypatch.setattr(video.Config, 'from_env', lambda: SimpleNamespace(piapi_key=None))
    with pytest.raises(video.VideoServiceError, match='PIAPI_KEY'):
        await video.create_video_task('https://image.example/a.png', 'Animate')


@respx.mock
async def test_generate_avatar_video_end_to_end(fast_poll):
    post = respx.post(video.TASK_URL).respond(200, json={'data': {'task_id': 'new-task'}})
    get = respx.get(video.TASK_URL + '/new-task').respond(200, json={'data': {'status': 'completed', 'output': {'video': 'https://video.example/a.mp4'}}})
    assert await video.generate_video_from_avatar('https://image.example/avatar.png', 'Animate') == 'https://video.example/a.mp4'
    assert post.call_count == get.call_count == 1


@respx.mock
async def test_default_avatar_prompt_matches_smiling_and_blinking_request(fast_poll):
    post = respx.post(video.TASK_URL).respond(200, json={'data': {'task_id': 'avatar'}})
    respx.get(video.TASK_URL + '/avatar').respond(200, json={'data': {'status': 'completed', 'output': {'video': 'https://video.example/avatar.mp4'}}})
    assert await video.generate_video_from_avatar('https://images.example/avatar.png') == 'https://video.example/avatar.mp4'
    assert json.loads(post.calls[0].request.content)['input']['prompt'] == 'The character is smiling and blinking naturally'

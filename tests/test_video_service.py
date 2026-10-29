import asyncio
import json
import logging
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

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
                  'version': '3.0', 'mode': 'std', 'duration': 5, 'enable_audio': False},
        'config': {'service_mode': 'public'},
    }
    assert route.call_count == 1
    assert 'task-123' in caplog.text and 'создаю' in caplog.text
    assert 'test-piapi-secret' not in caplog.text


@respx.mock
async def test_poll_processing_then_completed(fast_poll, caplog, monkeypatch):
    budget = Mock(wraps=asyncio.timeout)
    monkeypatch.setattr(video.asyncio, 'timeout', budget)
    monkeypatch.setattr(video, 'monotonic', Mock(side_effect=[1000, 1315, 1900]))
    route = respx.get(video.TASK_URL + '/task-123').mock(side_effect=[
        httpx.Response(200, json={'data': {'status': 'processing'}}),
        httpx.Response(200, json={'data': {'status': 'completed', 'output': {'video': 'https://video.example/result.mp4'}}}),
    ])
    with caplog.at_level(logging.INFO, logger=video.__name__):
        assert await video.poll_video_task('task-123') == 'https://video.example/result.mp4'
    assert route.call_count == 2
    assert [call.args for call in fast_poll.await_args_list] == [(15,), (15,)]
    budget.assert_called_once_with(1200)
    assert all(call.request.headers['X-API-Key'] == 'test-piapi-secret' for call in route.calls)
    assert 'processing' in caplog.text and 'completed' in caplog.text and 'видео готово' in caplog.text
    assert 'прошло 315 сек, осталось 885 сек' in caplog.text
    assert 'прошло 900 сек, осталось 300 сек' in caplog.text


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
    assert json.loads(post.calls[0].request.content)['input']['prompt'] == video.DEFAULT_ANIMATION_PROMPT


@pytest.mark.parametrize('mode,addition', [
    ('mode_ny', ' Snow is gently falling in the background.'),
    ('mode_ny_family', ' Snow is gently falling in the background.'),
    ('mode_ny_cozy', ' Snow is gently falling in the background.'),
    ('mode_ny_fairytale', ' Snow is gently falling in the background.'),
    ('mode_ny_elegant', ' Snow is gently falling in the background.'),
    ('mode_bday_young', ' If there is a birthday cake, the candles are flickering gently.'),
    ('mode_bday_old', ' If there is a birthday cake, the candles are flickering gently.'),
    ('mode_bday_old_elegant', ' If there is a birthday cake, the candles are flickering gently.'),
    ('mode_goodmorning', ' If there is a coffee cup, warm steam is rising from it.'),
    ('mode_modern_card', ''),
    ('mode_scrapbook_winter', ' Snow is gently falling in the background.'),
    ('mode_modern_card_winter', ' Snow is gently falling in the background.'),
    ('mode_scrapbook_spring', ''),
    ('mode_teacher', ''), ('mode_coach', ''), ('mode_halloween', ''), ('', ''), ('unknown', ''),
])
@respx.mock
async def test_style_specific_prompt_reaches_piapi_and_is_logged(mode, addition, fast_poll, caplog):
    base = 'The character is smiling, blinking naturally, and slightly moving their head from side to side.'
    post = respx.post(video.TASK_URL).respond(200, json={'data': {'task_id': 'styled'}})
    respx.get(video.TASK_URL + '/styled').respond(200, json={'data': {'status': 'completed', 'output': {'video': 'https://video.example/styled.mp4'}}})
    with caplog.at_level(logging.INFO, logger=video.__name__):
        assert await video.generate_video_from_avatar('https://images.example/card.png', mode=mode) == 'https://video.example/styled.mp4'
    payload = json.loads(post.calls[0].request.content)
    assert payload['input']['prompt'] == base + addition
    assert payload['input']['mode'] == 'std' and payload['input']['enable_audio'] is False
    assert f'Промпт для Kling ({mode}): {base + addition}' in caplog.text
    assert post.call_count == 1


@pytest.mark.parametrize('count', [0, 1, 5, 6, 40])
@respx.mock
async def test_crowd_threshold_controls_real_piapi_prompt_and_logs_count(count, fast_poll, monkeypatch, caplog):
    counter = AsyncMock(return_value=count)
    monkeypatch.setattr(video, 'count_people_in_photo', counter)
    post = respx.post(video.TASK_URL).respond(200, json={'data':{'task_id':'crowd'}})
    respx.get(video.TASK_URL + '/crowd').respond(200, json={'data':{'status':'completed','output':{'video':'https://video.example/crowd.mp4'}}})
    with caplog.at_level(logging.INFO, logger=video.__name__):
        await video.generate_video_from_avatar('https://images.example/group.png', mode='mode_ny')
    counter.assert_awaited_once_with('https://images.example/group.png')
    prompt = json.loads(post.calls[0].request.content)['input']['prompt']
    if count > 5:
        assert prompt == ('Cinematic background animation only. The glowing lights shimmer, and there is a subtle camera zoom. '
            'The people remain completely still like in a photograph. DO NOT animate faces or bodies.')
        assert 'Snow' not in prompt and 'smiling' not in prompt
    else:
        assert 'The character is smiling, blinking naturally, and slightly moving their head from side to side.' in prompt and 'Snow is gently falling' in prompt
    assert f'На фото найдено {count} человек. Промпт для Kling: {prompt}' in caplog.text


@respx.mock
async def test_supplied_count_avoids_duplicate_vision_and_custom_prompt_cannot_animate_crowd(fast_poll, monkeypatch):
    counter = AsyncMock()
    monkeypatch.setattr(video, 'count_people_in_photo', counter)
    post = respx.post(video.TASK_URL).respond(200, json={'data':{'task_id':'checked'}})
    respx.get(video.TASK_URL + '/checked').respond(200, json={'data':{'status':'completed','output':{'video':'https://video.example/checked.mp4'}}})
    await video.generate_video_from_avatar('https://images.example/group.png', 'Make everyone dance', mode='mode_bday_young', people_count=6)
    counter.assert_not_awaited()
    assert json.loads(post.calls[0].request.content)['input']['prompt'] == video.CROWD_VIDEO_PROMPT


@pytest.mark.parametrize('theme,details',[
    ('theme_ny','Snow is falling, fairy lights are twinkling.'),
    ('theme_bday','Balloons are floating, confetti is falling.'),
    ('theme_bday_kid','Pastel balloons are bobbing, confetti is floating.'),
    ('theme_valentine','Hearts are pulsing, rose petals are falling.'),
    ('theme_feb23','The St. George ribbon is waving, snow is falling.'),
    ('theme_mar8','Flowers are swaying, sunlight is shimmering.'),
    ('theme_mother','Soft particles are floating.'),
    ('theme_father','Warm light is flickering, dust motes are floating.'),
])
def test_cartoon_kling_prompt_uses_exact_short_themed_animation(theme,details):
    assert video.build_video_prompt(theme)==video.CARTOON_VIDEO_PROMPTS[theme]
    assert video.CARTOON_VIDEO_PROMPTS[theme]=='The character is smiling, blinking naturally, and slightly moving their head from side to side. '+details


@pytest.mark.parametrize('response', [
    httpx.Response(200, json={'code': 10000, 'message': 'Internal error'}),
    httpx.Response(200, json={'code': 500, 'message': 'Server error'}),
    httpx.Response(200, json={'code': '10000', 'message': 'Internal error'}),
    httpx.Response(500, text='Server error test-piapi-secret'),
])
@respx.mock
async def test_internal_creation_error_retries_then_polls_only_accepted_task(response, fast_poll, caplog):
    post = respx.post(video.TASK_URL).mock(side_effect=[
        response, response, httpx.Response(200, json={'data': {'task_id': 'accepted'}}),
    ])
    get = respx.get(video.TASK_URL + '/accepted').respond(
        200, json={'data': {'status': 'completed', 'output': {'video': 'https://video.example/done.mp4'}}})
    with caplog.at_level(logging.WARNING, logger=video.__name__):
        result = await video.generate_video_from_avatar('https://images.example/a.png', mode='theme_ny', people_count=1)
    assert result == 'https://video.example/done.mp4'
    assert post.call_count == 3 and get.call_count == 1
    assert [call.args for call in fast_poll.await_args_list] == [(15,), (15,), (15,)]
    assert 'Попытка 1 провалилась (Internal error). Повтор через 15 секунд...' in caplog.text
    assert 'Попытка 2 провалилась (Internal error). Повтор через 15 секунд...' in caplog.text
    assert 'test-piapi-secret' not in caplog.text
    for call in post.calls:
        payload = json.loads(call.request.content)
        assert payload['input']['version'] == '3.0'
        assert payload['input']['prompt'] == video.CARTOON_VIDEO_PROMPTS['theme_ny']


@respx.mock
async def test_internal_creation_errors_stop_after_three_attempts(fast_poll):
    post = respx.post(video.TASK_URL).respond(200, json={'code': 10000, 'message': 'Internal error'})
    with pytest.raises(video.VideoServiceError, match='^Сервис Kling временно недоступен. Попробуйте позже.$'):
        await video.generate_video_from_avatar('https://images.example/a.png', people_count=1)
    assert post.call_count == 3
    assert [call.args for call in fast_poll.await_args_list] == [(15,), (15,)]


@pytest.mark.parametrize('response', [
    httpx.Response(401, text='invalid key'),
    httpx.Response(403, text='forbidden'),
    httpx.Response(429, text='rate limited'),
    httpx.Response(200, json={'code': 400, 'message': 'invalid prompt'}),
    httpx.Response(200, json={'data': {}}),
    httpx.Response(200, text='not JSON'),
])
@respx.mock
async def test_other_creation_errors_are_not_retried(response, fast_poll):
    post = respx.post(video.TASK_URL).mock(return_value=response)
    with pytest.raises(video.VideoServiceError):
        await video.generate_video_from_avatar('https://images.example/a.png', people_count=1)
    assert post.call_count == 1
    fast_poll.assert_not_awaited()


@pytest.mark.parametrize('error_type', [httpx.ReadTimeout, httpx.ConnectError])
@respx.mock
async def test_ambiguous_transport_failure_never_creates_duplicate_task(error_type, fast_poll):
    post = respx.post(video.TASK_URL).mock(side_effect=error_type('network'))
    with pytest.raises(video.VideoServiceError):
        await video.generate_video_from_avatar('https://images.example/a.png', people_count=1)
    assert post.call_count == 1
    fast_poll.assert_not_awaited()


@respx.mock
async def test_poll_error_never_reposts_accepted_task(fast_poll):
    post = respx.post(video.TASK_URL).respond(200, json={'data': {'task_id': 'accepted'}})
    respx.get(video.TASK_URL + '/accepted').respond(500, text='Internal error')
    with pytest.raises(video.VideoServiceError, match='HTTP 500'):
        await video.generate_video_from_avatar('https://images.example/a.png', people_count=1)
    assert post.call_count == 1


@respx.mock
async def test_cancellation_during_retry_delay_stops_without_another_post(monkeypatch):
    post = respx.post(video.TASK_URL).respond(200, json={'code': 10000})
    monkeypatch.setattr(video.asyncio, 'sleep', AsyncMock(side_effect=asyncio.CancelledError))
    with pytest.raises(asyncio.CancelledError):
        await video.generate_video_from_avatar('https://images.example/a.png', people_count=1)
    assert post.call_count == 1

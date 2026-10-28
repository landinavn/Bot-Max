import asyncio
import json
from pathlib import Path
from unittest.mock import AsyncMock, Mock

import httpx
import pytest
import respx
from aioresponses import aioresponses

import audio_service as service
from config import Config


@pytest.fixture(autouse=True)
def audio_config(monkeypatch):
    monkeypatch.setattr(service.Config, 'from_env', lambda: Config(
        token='test-max', photo_path=Path('unused'), ttapi_key='secret-test-ttapi'))


async def test_submit_exact_custom_music_request_and_fetch():
    with aioresponses() as http:
        http.post(service.BASE_URL + '/suno/v1/music', payload={'status': 'SUCCESS', 'data': {'jobId': 'job-1'}})
        http.get(service.BASE_URL + '/suno/v2/fetch?jobId=job-1', payload={'status': 'SUCCESS', 'data': {'musics': [{'audioUrl': 'https://cdn.example/1.mp3'}]}})
        assert await service.generate_song('Текст песни', 'pop', 'Для Ани') == 'job-1'
        result = await service.fetch_song('job-1')
    calls = list(http.requests.values())
    assert calls[0][0].kwargs['json'] == {'custom': True, 'instrumental': False, 'mv': 'chirp-v6',
        'prompt': 'Текст песни', 'title': 'Для Ани', 'tags': 'pop', 'audio_format': 'mp3'}
    assert calls[0][0].kwargs['headers']['TT-API-KEY'] == 'secret-test-ttapi'
    assert result['status'] == 'SUCCESS'


@pytest.mark.parametrize('status', [400, 401, 429, 500])
async def test_http_failures_are_not_retried_or_exposed(status):
    with aioresponses() as http:
        http.post(service.BASE_URL + '/suno/v1/music', status=status, body='provider rejected secret-test-ttapi')
        with pytest.raises(service.AudioServiceError) as error:
            await service.generate_song('words', 'pop', 'title')
    assert f'TTAPI Error: {status}' in str(error.value)
    assert 'provider rejected' in str(error.value)
    assert 'secret-test-ttapi' not in str(error.value)
    assert len(next(iter(http.requests.values()))) == 1


@pytest.mark.parametrize('body', [{'status': 'FAILED'}, {'status': 'SUCCESS'}, [], 'invalid'])
async def test_bad_submit_responses(body):
    with aioresponses() as http:
        http.post(service.BASE_URL + '/suno/v1/music', payload=body)
        with pytest.raises(service.AudioServiceError):
            await service.generate_song('words', 'pop', 'title')


async def test_missing_key_makes_no_http_request(monkeypatch):
    monkeypatch.setattr(service.Config, 'from_env', lambda: Config(token='test-max', photo_path=Path('unused')))
    with aioresponses() as http:
        with pytest.raises(service.AudioNotConfigured):
            await service.generate_song('words', 'pop', 'title')
    assert not http.requests


async def test_wait_polls_every_five_seconds_until_success(monkeypatch):
    result = {'status': 'SUCCESS', 'data': [{'audio_url': 'https://cdn.example/1.mp3'}]}
    fetch = AsyncMock(side_effect=[{'status': 'ON_QUEUE'}, {'status': 'PROCESSING'}, result])
    sleep = AsyncMock()
    monkeypatch.setattr(service, 'fetch_song', fetch)
    monkeypatch.setattr(service.asyncio, 'sleep', sleep)
    assert await service.wait_for_song('job') == 'https://cdn.example/1.mp3'
    assert fetch.await_count == 3
    assert [c.args for c in sleep.await_args_list] == [(5,), (5,), (5,)]


@pytest.mark.parametrize('status,error', [('FAILED', service.SongFailed), ('WRONG', TimeoutError)])
async def test_failed_and_unknown_poll_status(monkeypatch, status, error):
    monkeypatch.setattr(service.asyncio, 'sleep', AsyncMock())
    monkeypatch.setattr(service, 'fetch_song', AsyncMock(return_value={'status': status}))
    with pytest.raises(error):
        await service.wait_for_song('job')


async def test_timeout_includes_slow_http_request(monkeypatch):
    async def fetch(job):
        await asyncio.Event().wait()
    monkeypatch.setattr(service, 'fetch_song', fetch)
    with pytest.raises(TimeoutError):
        await service.wait_for_song('job', timeout=0.02)


async def test_zero_timeout_does_not_fetch(monkeypatch):
    fetch = AsyncMock()
    monkeypatch.setattr(service, 'fetch_song', fetch)
    with pytest.raises(TimeoutError):
        await service.wait_for_song('job', timeout=0)
    fetch.assert_not_awaited()


@pytest.mark.parametrize('musics', [[], [{'audioUrl': 'javascript:alert(1)'}], [{'audioUrl': 'https://user:secret@cdn.example/1.mp3'}]])
def test_invalid_or_empty_audio_urls_rejected(musics):
    with pytest.raises(service.AudioServiceError):
        service.song_tracks({'data': {'musics': musics}})


def test_tracks_use_audio_url_and_keep_two_unique_valid_links():
    assert service.song_tracks({'data': {'musics': [
        {'audioUrl': 'https://cdn.example/1.mp3', 'title': 'Один'},
        {'audioUrl': 'https://cdn.example/1.mp3'},
        {'audioUrl': 'https://cdn.example/2.mp3', 'title': 'Два'},
        {'audioUrl': 'https://cdn.example/3.mp3'},
    ]}}) == [{'audioUrl': 'https://cdn.example/1.mp3', 'title': 'Один'},
            {'audioUrl': 'https://cdn.example/2.mp3', 'title': 'Два'}]


@respx.mock
async def test_lyrics_model_receives_rules_and_returns_six_sections(monkeypatch):
    settings = Config(token='max', photo_path=Path('unused'), image_api_key='text-key')
    monkeypatch.setattr(service.Config, 'from_env', lambda: settings)
    lyrics = '[Verse 1]\nТы даришь свет\n[Chorus]\nПой со мной\n[Verse 2]\nВ горах Аня встречает рассвет\n[Chorus]\nПой со мной\n[Bridge]\nМечты сбываются\n[Chorus]\nПой со мной'
    route = respx.post('https://api.openai.com/v1/chat/completions').mock(return_value=httpx.Response(200, json={
        'id': 'lyrics', 'object': 'chat.completion', 'created': 1, 'model': 'gpt-4o-mini',
        'choices': [{'index': 0, 'finish_reason': 'stop', 'message': {'role': 'assistant', 'content': lyrics}}],
    }))
    assert await service.write_song_lyrics('Аня, любит горы', 'pop', 'День рождения') == lyrics
    body = json.loads(route.calls[0].request.content)
    instruction = body['messages'][0]['content']
    assert 'НЕ перечисляй факты и привычки в первом куплете' in instruction
    assert '[Куплет 1] -> [Припев] -> [Куплет 2] -> [Припев] -> [Бридж] -> [Припев]' in instruction
    assert 'Не пиши название песни, название сценария' in instruction
    assert body['model'] == 'gpt-4o-mini'


@pytest.mark.parametrize('lyrics', ['Название\n[Verse 1]\nПривет', '[Verse 1]\nПривет\n[Chorus]\nПой'])
@respx.mock
async def test_lyrics_rejects_heading_or_incomplete_structure(monkeypatch, lyrics):
    monkeypatch.setattr(service.Config, 'from_env', lambda: Config(token='max', photo_path=Path('unused'), image_api_key='key'))
    respx.post('https://api.openai.com/v1/chat/completions').mock(return_value=httpx.Response(200, json={
        'id': 'lyrics', 'object': 'chat.completion', 'created': 1, 'model': 'gpt-4o-mini',
        'choices': [{'index': 0, 'finish_reason': 'stop', 'message': {'role': 'assistant', 'content': lyrics}}],
    }))
    with pytest.raises(service.AudioServiceError):
        await service.write_song_lyrics('details', 'pop', 'title')


@respx.mock
async def test_preview_is_reencoded_short_mp3_and_only_clip_is_uploaded(monkeypatch, tmp_path):
    import io
    import math
    import struct
    import subprocess
    import wave
    from unittest.mock import Mock
    import image_service
    if not service.shutil.which('ffmpeg') or not service.shutil.which('ffprobe'):
        pytest.skip('ffmpeg/ffprobe not installed')
    raw = io.BytesIO()
    with wave.open(raw, 'wb') as wav:
        wav.setnchannels(1); wav.setsampwidth(2); wav.setframerate(8000)
        wav.writeframes(b''.join(struct.pack('<h', int(5000*math.sin(2*math.pi*220*i/8000))) for i in range(8000*20)))
    respx.get('https://cdn.example/full.mp3').mock(return_value=httpx.Response(200, content=raw.getvalue()))
    storage = Mock()
    monkeypatch.setattr(image_service, '_storage_client', lambda settings: storage)
    monkeypatch.setattr(service.Config, 'from_env', lambda: Config(token='max', photo_path=tmp_path, storage_bucket='preview-bucket'))
    url = await service.make_protected_preview('https://cdn.example/full.mp3')
    assert '/audio-previews/' in url and url != 'https://cdn.example/full.mp3'
    payload = storage.put_object.call_args.kwargs
    assert payload['ContentType'] == 'audio/mpeg'
    result = tmp_path / 'result.mp3'; result.write_bytes(payload['Body'])
    probe = json.loads(subprocess.check_output(['ffprobe','-v','error','-show_format','-of','json',str(result)]))
    assert 14.9 <= float(probe['format']['duration']) <= 15.2
    assert payload['Body'] != raw.getvalue()
    storage.close.assert_called_once()


async def test_poll_default_timeout_is_100_seconds_and_logs_each_attempt(monkeypatch, caplog):
    timeout = Mock(wraps=asyncio.timeout)
    monkeypatch.setattr(service.asyncio, 'timeout', timeout)
    monkeypatch.setattr(service, 'fetch_song', AsyncMock(side_effect=[{'status': 'PENDING'}, {'status': 'SUCCESS', 'data': [{'audio_url': 'https://cdn.example/1.mp3'}]}]))
    monkeypatch.setattr(service.asyncio, 'sleep', AsyncMock())
    with caplog.at_level('INFO', logger='audio_service'):
        await service.wait_for_song('long-job')
    timeout.assert_called_once_with(100)
    assert 'Попытка 1: статус PENDING (job_id=long-job)' in caplog.text
    assert 'Попытка 2: статус SUCCESS (job_id=long-job)' in caplog.text


@pytest.mark.parametrize('status', ['FAILED', 'ERROR'])
async def test_poll_failure_logs_complete_response(monkeypatch, caplog, status):
    monkeypatch.setattr(service.asyncio, 'sleep', AsyncMock())
    response = {'status': status, 'data': {'reason': 'lyrics too long', 'code': 42}}
    monkeypatch.setattr(service, 'fetch_song', AsyncMock(return_value=response))
    with pytest.raises(service.SongFailed):
        await service.wait_for_song('failed-job')
    assert json.dumps(response, ensure_ascii=False) in caplog.text
    assert 'failed-job' in caplog.text


async def test_progress_delayed_once_and_result_preserved():
    ready = asyncio.Event()
    result = {'status': 'SUCCESS'}
    async def wait(job):
        await ready.wait()
        return result
    async def notify(text):
        assert text == service.LONG_WAIT_MESSAGE
        ready.set()
    notify_mock = AsyncMock(side_effect=notify)
    assert await service.wait_for_song_with_progress('job', notify_mock, waiter=wait, notification_delay=0.001) is result
    notify_mock.assert_awaited_once()


@pytest.mark.parametrize('error', [None, service.SongFailed(), TimeoutError()])
async def test_fast_completion_never_sends_delayed_notification(error):
    notify = AsyncMock()
    waiter = AsyncMock(return_value={'status': 'SUCCESS'}, side_effect=error)
    if error is None:
        assert await service.wait_for_song_with_progress('job', notify, waiter=waiter) == {'status': 'SUCCESS'}
    else:
        with pytest.raises(type(error)):
            await service.wait_for_song_with_progress('job', notify, waiter=waiter)
    notify.assert_not_awaited()


async def test_progress_send_failure_does_not_abort_song():
    ready = asyncio.Event()
    async def wait(job):
        await ready.wait()
        return {'status': 'SUCCESS'}
    async def notify(text):
        ready.set()
        raise RuntimeError('MAX unavailable')
    assert await service.wait_for_song_with_progress('job', notify, waiter=wait, notification_delay=0.001) == {'status': 'SUCCESS'}


async def test_cancel_wait_cleans_up_polling_task():
    started, cancelled = asyncio.Event(), asyncio.Event()
    async def wait(job):
        started.set()
        try:
            await asyncio.Event().wait()
        finally:
            cancelled.set()
    notify = AsyncMock()
    task = asyncio.create_task(service.wait_for_song_with_progress('job', notify, waiter=wait))
    await started.wait()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert cancelled.is_set()
    notify.assert_not_awaited()


@pytest.mark.parametrize('prompt,tags,title,field', [
    (None, 'pop', 'title', 'Текст песни'), (' ', 'pop', 'title', 'Текст песни'),
    ('words', None, 'title', 'Стиль музыки'), ('words', '', 'title', 'Стиль музыки'),
    ('words', 'pop', None, 'Название песни'), ('words', 'pop', ' ', 'Название песни'),
    ('x'*5001, 'pop', 'title', 'Текст песни'), ('words', 'x'*1001, 'title', 'Стиль музыки'),
    ('words', 'pop', 'x'*81, 'Название песни'),
])
@respx.mock
async def test_song_arguments_fail_clearly_before_any_http(prompt, tags, title, field):
    with pytest.raises(service.InvalidSongArguments, match=field):
        await service.generate_song(prompt, tags, title)
    assert not respx.calls


@pytest.mark.parametrize('payload', [
    {'status': 'SUCCESS', 'data': [{'audio_url': 'https://cdn.example/one.mp3'}, {'audio_url': 'https://cdn.example/two.mp3'}]},
    {'status': 'SUCCESS', 'data': {'musics': [{'audioUrl': 'https://cdn.example/one.mp3'}, {'audioUrl': 'https://cdn.example/two.mp3'}]}},
])
async def test_public_wait_returns_first_url_and_full_wait_keeps_two_variants(monkeypatch, payload):
    monkeypatch.setattr(service.asyncio, 'sleep', AsyncMock())
    monkeypatch.setattr(service, 'fetch_song', AsyncMock(return_value=payload))
    assert await service.wait_for_song('job') == 'https://cdn.example/one.mp3'
    assert len(service.song_tracks(await service.wait_for_song_result('job'))) == 2


async def test_twenty_pending_attempts_raise_clear_timeout_without_resubmission(monkeypatch, caplog):
    monkeypatch.setattr(service.asyncio, 'sleep', AsyncMock())
    fetch = AsyncMock(return_value={'status': 'PROCESSING'})
    monkeypatch.setattr(service, 'fetch_song', fetch)
    with caplog.at_level('INFO'), pytest.raises(TimeoutError, match='100 секунд'):
        await service.wait_for_song('job')
    assert fetch.await_count == 20
    assert service.asyncio.sleep.await_args_list == [__import__('unittest.mock', fromlist=['call']).call(5)]*20
    assert 'Попытка 20: статус PROCESSING' in caplog.text


async def test_session_timeout_30_and_missing_job_response_body(monkeypatch):
    session = Mock(wraps=service.aiohttp.ClientSession)
    monkeypatch.setattr(service.aiohttp, 'ClientSession', session)
    with aioresponses() as http:
        http.post(service.BASE_URL + '/suno/v1/music', payload={'status': 'SUCCESS', 'reason': 'no task'})
        with pytest.raises(service.AudioServiceError, match='Нет jobId в ответе:') as error:
            await service.generate_song('words', 'pop', 'title')
    assert 'no task' in str(error.value)
    assert session.call_args.kwargs['timeout'].total == 30


async def test_request_timeout_is_clear_and_is_not_silently_retried():
    with aioresponses() as http:
        http.post(service.BASE_URL + '/suno/v1/music', exception=TimeoutError())
        with pytest.raises(TimeoutError, match='30 секунд'):
            await service.generate_song('words', 'pop', 'title')
    assert len(next(iter(http.requests.values()))) == 1


@pytest.mark.parametrize('kind,duration', [('fun', '30-40 seconds'), ('gift', '2-3 minutes')])
def test_new_year_song_has_required_mood_wishes_and_duration(kind, duration):
    prompt, tags, title = service.build_song_request('Аня, семья, вместе катались на лыжах', '🎄 Новогодняя', kind)
    assert duration in tags and 'New Year' in tags
    assert 'С Новым годом' in prompt and 'Аня' in prompt
    assert title.startswith('🎄 Новогодняя:')
    if kind == 'fun':
        assert 'подарков' in prompt and 'Счастья' in prompt and 'playful' in tags
    else:
        assert 'семейные мгновения' in prompt and 'родные' in prompt and 'family' in tags
    assert prompt.startswith('[Verse 1]')
    # Название сценария остаётся только в title.
    assert 'Новогодняя' not in prompt


def test_new_year_custom_style_keeps_duration_and_festive_theme():
    _, tags, _ = service.build_song_request('Сергей', '🎄 Новогодняя', 'fun', 'rock')
    assert tags.startswith('rock,') and '30-40 seconds' in tags and 'New Year' in tags

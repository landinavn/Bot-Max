import asyncio
import json
from pathlib import Path
from unittest.mock import AsyncMock

import httpx
import pytest
import respx

import audio_service as service
from config import Config


@pytest.fixture(autouse=True)
def audio_config(monkeypatch):
    monkeypatch.setattr(service.Config, 'from_env', lambda: Config(
        token='test-max', photo_path=Path('unused'), ttapi_key='secret-test-ttapi'))


@respx.mock
async def test_submit_exact_custom_music_request_and_fetch():
    submit = respx.post(service.BASE_URL + '/suno/v1/music').mock(return_value=httpx.Response(
        200, json={'status': 'SUCCESS', 'data': {'jobId': 'job-1'}}))
    fetch = respx.get(service.BASE_URL + '/suno/v2/fetch', params={'jobId': 'job-1'}).mock(return_value=httpx.Response(
        200, json={'status': 'SUCCESS', 'data': {'musics': [{'audioUrl': 'https://cdn.example/1.mp3'}]}}))
    assert await service.generate_song('Текст песни', 'pop', 'Для Ани') == 'job-1'
    payload = json.loads(submit.calls[0].request.content)
    assert payload == {'custom': True, 'instrumental': False, 'mv': 'chirp-v6', 'prompt': 'Текст песни',
                       'title': 'Для Ани', 'tags': 'pop', 'audio_format': 'mp3'}
    assert submit.calls[0].request.headers['TT-API-KEY'] == 'secret-test-ttapi'
    result = await service.fetch_song('job-1')
    assert result['status'] == 'SUCCESS'
    assert fetch.calls[0].request.headers['TT-API-KEY'] == 'secret-test-ttapi'


@pytest.mark.parametrize('status', [400, 401, 429, 500])
@respx.mock
async def test_http_failures_are_not_retried_or_exposed(status):
    route = respx.post(service.BASE_URL + '/suno/v1/music').mock(return_value=httpx.Response(
        status, json={'message': 'secret-test-ttapi'}))
    with pytest.raises(service.AudioServiceError) as error:
        await service.generate_song('words', 'pop', 'title')
    assert 'secret-test-ttapi' not in str(error.value)
    assert route.call_count == 1


@pytest.mark.parametrize('body', [{'status': 'FAILED'}, {'status': 'SUCCESS'}, [], 'invalid'])
@respx.mock
async def test_bad_submit_responses(body):
    respx.post(service.BASE_URL + '/suno/v1/music').mock(return_value=httpx.Response(200, json=body))
    with pytest.raises(service.AudioServiceError):
        await service.generate_song('words', 'pop', 'title')


@respx.mock
async def test_missing_key_makes_no_http_request(monkeypatch):
    monkeypatch.setattr(service.Config, 'from_env', lambda: Config(token='test-max', photo_path=Path('unused')))
    with pytest.raises(service.AudioNotConfigured):
        await service.generate_song('words', 'pop', 'title')
    assert not respx.calls


async def test_wait_polls_every_five_seconds_until_success(monkeypatch):
    result = {'status': 'SUCCESS', 'data': {'musics': []}}
    fetch = AsyncMock(side_effect=[{'status': 'ON_QUEUE'}, {'status': 'PROCESSING'}, result])
    sleep = AsyncMock()
    monkeypatch.setattr(service, 'fetch_song', fetch)
    monkeypatch.setattr(service.asyncio, 'sleep', sleep)
    assert await service.wait_for_song('job') is result
    assert fetch.await_count == 3
    assert [c.args for c in sleep.await_args_list] == [(5,), (5,)]


@pytest.mark.parametrize('status,error', [('FAILED', service.SongFailed), ('WRONG', service.AudioServiceError)])
async def test_failed_and_unknown_poll_status(monkeypatch, status, error):
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

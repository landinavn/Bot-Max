import asyncio
import json
import shutil
import subprocess
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import httpx
import pytest
import respx

import media_service as media


@pytest.fixture
def storage(monkeypatch):
    settings = SimpleNamespace(storage_bucket='media-bucket', storage_public_base_url='https://cdn.example')
    monkeypatch.setattr(media.Config, 'from_env', lambda: settings)
    client = Mock()
    client.objects = []
    def put(**kwargs):
        client.objects.append({**kwargs, 'Body': kwargs['Body'].read()})
    client.put_object.side_effect = put
    monkeypatch.setattr(media, '_storage_client', lambda config: client)
    return client


@pytest.fixture
def sample_media(tmp_path):
    if not shutil.which('ffmpeg') or not shutil.which('ffprobe'):
        pytest.skip('ffmpeg/ffprobe not installed')
    video, audio = tmp_path/'original.mp4', tmp_path/'song.wav'
    subprocess.run(['ffmpeg', '-v', 'error', '-f', 'lavfi', '-i', 'color=c=blue:s=64x64:r=10:d=2',
        '-f', 'lavfi', '-i', 'sine=frequency=180:duration=2', '-c:v', 'mpeg4', '-c:a', 'aac', '-shortest', str(video)], check=True)
    subprocess.run(['ffmpeg', '-v', 'error', '-f', 'lavfi', '-i', 'sine=frequency=880:duration=3.5', str(audio)], check=True)
    return video.read_bytes(), audio.read_bytes()


@respx.mock
async def test_merge_replaces_original_audio_copies_video_uploads_and_cleans_temp(storage, sample_media, tmp_path, monkeypatch):
    import array
    respx.get('https://input.example/video.mp4').respond(200, content=sample_media[0])
    respx.get('https://input.example/song.mp3').respond(200, content=sample_media[1])
    paths = []
    download = media._download
    async def record(*args):
        paths.append(args[2])
        await download(*args)
    monkeypatch.setattr(media, '_download', record)
    url = await media.merge_video_and_audio('https://input.example/video.mp4', 'https://input.example/song.mp3')
    assert url.startswith('https://cdn.example/videos/') and url.endswith('.mp4')
    stored = storage.objects[0]
    assert stored['ContentType'] == 'video/mp4' and stored['Bucket'] == 'media-bucket'
    result = tmp_path/'result.mp4'
    result.write_bytes(stored['Body'])
    probe = json.loads(subprocess.check_output(['ffprobe', '-v', 'error', '-show_streams', '-show_format', '-of', 'json', str(result)]))
    assert [s['codec_type'] for s in probe['streams']] == ['video', 'audio']
    assert probe['streams'][0]['codec_name'] == 'mpeg4'  # Видео скопировано, не перекодировано.
    assert probe['streams'][1]['codec_name'] == 'aac'
    assert 1.9 <= float(probe['format']['duration']) <= 2.2
    pcm = array.array('h', subprocess.check_output(['ffmpeg', '-v', 'error', '-i', str(result), '-map', '0:a:0', '-ac', '1', '-ar', '8000', '-f', 's16le', '-']))
    crossings = sum(1 for a, b in zip(pcm, pcm[1:]) if a < 0 <= b)
    frequency = crossings / (len(pcm)/8000)
    assert 850 < frequency < 910  # Взята песня 880 Гц, а не старый звук 180 Гц.
    assert all(not path.exists() and not path.parent.exists() for path in paths)
    storage.close.assert_called_once()


@respx.mock
async def test_merge_download_error_cleans_files_and_never_uploads(storage, monkeypatch):
    paths = []
    original = media._download
    async def record(*args):
        paths.append(args[2])
        await original(*args)
    monkeypatch.setattr(media, '_download', record)
    respx.get('https://input.example/video.mp4').respond(500)
    with pytest.raises(httpx.HTTPStatusError):
        await media.merge_video_and_audio('https://input.example/video.mp4', 'https://input.example/song.mp3')
    storage.put_object.assert_not_called()
    storage.close.assert_called_once()
    assert paths and not paths[0].parent.exists()


async def test_merge_missing_ffmpeg_is_clear(monkeypatch):
    monkeypatch.setattr(media.shutil, 'which', lambda binary: None)
    with pytest.raises(media.MediaServiceError, match='установите ffmpeg'):
        await media.merge_video_and_audio('https://input.example/v.mp4', 'https://input.example/a.mp3')


@pytest.mark.parametrize('failure', [asyncio.CancelledError, TimeoutError])
async def test_media_process_killed_on_cancel_or_timeout(monkeypatch, failure):
    process = SimpleNamespace(communicate=AsyncMock(side_effect=failure), returncode=None, kill=Mock(), wait=AsyncMock())
    monkeypatch.setattr(media.asyncio, 'create_subprocess_exec', AsyncMock(return_value=process))
    with pytest.raises(failure):
        await media.run_media_process('ffmpeg')
    process.kill.assert_called_once()
    process.wait.assert_awaited_once()


@respx.mock
async def test_merge_upload_error_still_cleans_temp_and_closes_storage(storage, sample_media, monkeypatch):
    respx.get('https://input.example/video.mp4').respond(200, content=sample_media[0])
    respx.get('https://input.example/song.mp3').respond(200, content=sample_media[1])
    paths = []
    original = media._upload_video
    def failing(client, settings, path):
        paths.append(path)
        raise RuntimeError('storage unavailable')
    monkeypatch.setattr(media, '_upload_video', failing)
    with pytest.raises(RuntimeError, match='storage unavailable'):
        await media.merge_video_and_audio('https://input.example/video.mp4', 'https://input.example/song.mp3')
    assert paths and not paths[0].parent.exists()
    storage.close.assert_called_once()

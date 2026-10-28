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
    assert 3.4 <= float(probe['format']['duration']) <= 3.7
    assert int(probe['streams'][0]['nb_frames']) >= 34  # Исходный ролик имел только 20 кадров.
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


@pytest.mark.parametrize('duration', [1.25, 67.25])
@respx.mock
async def test_looped_video_matches_full_song_even_when_shorter_or_much_longer(storage, sample_media, tmp_path, duration):
    audio = tmp_path/'duration.wav'
    subprocess.run(['ffmpeg','-v','error','-f','lavfi','-i',f'sine=frequency=660:duration={duration}',str(audio)], check=True)
    respx.get('https://input.example/video.mp4').respond(200, content=sample_media[0])
    respx.get('https://input.example/song.mp3').respond(200, content=audio.read_bytes())
    await media.merge_video_and_audio('https://input.example/video.mp4', 'https://input.example/song.mp3')
    target = tmp_path/'looped.mp4'
    target.write_bytes(storage.objects[0]['Body'])
    result = json.loads(subprocess.check_output(['ffprobe','-v','error','-show_streams','-show_format','-of','json',str(target)]))
    video, sound = result['streams']
    assert abs(float(sound['duration'])-duration) < 0.1
    # Поток копируется без перекодирования: длительность кратна кадру (здесь 0.1 с).
    assert duration-0.11 <= float(video['duration']) <= duration+0.2
    assert abs(float(result['format']['duration'])-duration) < 0.2
    assert video['codec_name'] == 'mpeg4' and sound['codec_name'] == 'aac'
    if duration > 2:
        assert int(video['nb_frames']) > 600  # Больше минуты видео из исходных двух секунд.


@respx.mock
async def test_looped_video_accepts_kling_result_without_any_audio(storage, sample_media, tmp_path):
    original, silent = tmp_path/'original.mp4', tmp_path/'silent.mp4'
    original.write_bytes(sample_media[0])
    subprocess.run(['ffmpeg','-v','error','-i',str(original),'-map','0:v:0','-c:v','copy','-an',str(silent)], check=True)
    respx.get('https://input.example/video.mp4').respond(200, content=silent.read_bytes())
    respx.get('https://input.example/song.mp3').respond(200, content=sample_media[1])
    await media.merge_video_and_audio('https://input.example/video.mp4', 'https://input.example/song.mp3')
    target = tmp_path/'merged-silent-input.mp4'
    target.write_bytes(storage.objects[0]['Body'])
    result = json.loads(subprocess.check_output(['ffprobe','-v','error','-show_streams','-of','json',str(target)]))
    assert [s['codec_type'] for s in result['streams']] == ['video','audio']
    assert float(result['streams'][1]['duration']) >= 3.4


@pytest.mark.parametrize('duration', [3.5, 51.25])
@respx.mock
async def test_musical_card_is_static_h264_with_full_aac_song_and_cleans_files(storage, sample_media, tmp_path, monkeypatch, duration):
    import array
    import io
    from PIL import Image
    image = Image.new('RGB', (65, 67), '#d6326b')
    image.paste('#236ac4', (10, 10, 50, 50))
    source = io.BytesIO(); image.save(source, 'WEBP')
    song = tmp_path/'full.wav'
    subprocess.run(['ffmpeg','-v','error','-f','lavfi','-i',f'sine=frequency=880:duration={duration}',str(song)], check=True)
    respx.get('https://input.example/card.webp').respond(200, content=source.getvalue())
    respx.get('https://input.example/song.mp3').respond(200, content=song.read_bytes())
    paths = []
    download = media._download
    async def record(*args):
        paths.append(args[2])
        return await download(*args)
    monkeypatch.setattr(media, '_download', record)
    url = await media.create_musical_video('https://input.example/card.webp', 'https://input.example/song.mp3')
    assert url.startswith('https://cdn.example/videos/') and url.endswith('.mp4')
    assert storage.objects[0]['ContentType'] == 'video/mp4'
    result = tmp_path/'musical.mp4'; result.write_bytes(storage.objects[0]['Body'])
    probe = json.loads(subprocess.check_output(['ffprobe','-v','error','-show_streams','-show_format','-of','json',str(result)]))
    video, sound = probe['streams']
    assert video['codec_name'] == 'h264' and video['pix_fmt'] == 'yuv420p'
    assert (video['width'],video['height']) == (66,68)
    assert sound['codec_name'] == 'aac'
    assert abs(float(sound['duration'])-duration) < 0.1
    assert abs(float(probe['format']['duration'])-duration) < 0.15
    frames = []
    for seek in (0.2,duration-0.3):
        frames.append(subprocess.check_output(['ffmpeg','-v','error','-ss',str(seek),'-i',str(result),'-vframes','1','-f','rawvideo','-pix_fmt','rgb24','-']))
    assert len(frames[0]) == len(frames[1]) == 66*68*3
    # H.264 может слегка менять квантование одинаковых кадров.
    assert sum(abs(a-b) for a,b in zip(*frames))/len(frames[0]) < 5
    pcm = array.array('h', subprocess.check_output(['ffmpeg','-v','error','-i',str(result),'-ss','1','-t','1','-f','s16le','-ac','1','-ar','8000','-']))
    frequency = sum(a < 0 <= b for a,b in zip(pcm, pcm[1:]))/(len(pcm)/8000)
    assert 850 < frequency < 910
    assert all(not path.exists() and not path.parent.exists() for path in paths)
    storage.close.assert_called_once()


@respx.mock
async def test_musical_card_download_error_cleans_temp_without_upload(storage, monkeypatch):
    paths=[]
    original=media._download
    async def record(*args):
        paths.append(args[2]); await original(*args)
    monkeypatch.setattr(media,'_download',record)
    respx.get('https://input.example/image.jpg').respond(503)
    with pytest.raises(httpx.HTTPStatusError):
        await media.create_musical_video('https://input.example/image.jpg','https://input.example/song.mp3')
    storage.put_object.assert_not_called(); storage.close.assert_called_once()
    assert not paths[0].parent.exists()


async def test_musical_card_missing_ffmpeg_is_clear(monkeypatch):
    monkeypatch.setattr(media.shutil,'which',lambda name:None)
    with pytest.raises(media.MediaServiceError,match='установите ffmpeg'):
        await media.create_musical_video('https://input.example/image.jpg','https://input.example/song.mp3')


@respx.mock
async def test_musical_mp4_is_uploaded_as_video_attachment_to_max_and_temp_removed():
    from maxapi.types import AttachmentUpload
    respx.get('https://cdn.example/musical.mp4').respond(200,content=b'MP4-CONTENT')
    paths=[]
    async def upload(media_file):
        assert media_file.type.value == 'video'
        path=Path(media_file.path); paths.append(path)
        assert path.read_bytes() == b'MP4-CONTENT'
        return AttachmentUpload(type='video',payload={'token':'video-token'})
    bot=SimpleNamespace(upload_media=AsyncMock(side_effect=upload))
    attachment=await media.upload_video_for_max(bot,'https://cdn.example/musical.mp4')
    assert attachment.type.value == 'video' and attachment.payload.token == 'video-token'
    assert not paths[0].parent.exists()

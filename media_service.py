"""Локальная обработка медиа через ffmpeg и публикация результата в Object Storage."""

import asyncio
import json
import logging
import math
import shutil
import tempfile
from pathlib import Path
from urllib.parse import quote, urlsplit
from uuid import uuid4

import httpx

from config import Config
from image_service import _storage_client

logger = logging.getLogger(__name__)
VIDEO_MERGE_POLICY = 'loop-full-song-v1'
MUSICAL_VIDEO_POLICY = 'static-image-full-song-v1'


class MediaServiceError(RuntimeError):
    pass


async def run_media_process(*args, timeout=120) -> bytes:
    process = await asyncio.create_subprocess_exec(*args, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
    try:
        stdout, stderr = await asyncio.wait_for(process.communicate(), timeout=timeout)
    except BaseException:
        if process.returncode is None:
            process.kill()
            await process.wait()
        raise
    if process.returncode:
        # Ответ не содержит секретов HTTP или адресов исходных объектов.
        logger.error('Ошибка обработки медиа: %s', stderr.decode(errors='replace')[-2000:])
        raise MediaServiceError('ffmpeg/ffprobe не смог обработать файл.')
    return stdout


async def audio_duration(source: Path) -> float:
    binary = shutil.which('ffprobe')
    if not binary:
        raise MediaServiceError('Для обработки аудио установите ffprobe (пакет ffmpeg).')
    raw = await run_media_process(binary, '-v', 'error', '-protocol_whitelist', 'file,pipe',
        '-show_entries', 'format=duration', '-of', 'json', str(source), timeout=30)
    try:
        duration = float(json.loads(raw)['format']['duration'])
        if not math.isfinite(duration) or duration <= 0:
            raise ValueError
        return duration
    except (ValueError, TypeError, KeyError):
        raise MediaServiceError('Не удалось определить длительность аудио.') from None


async def _download(http, url: str, path: Path, limit: int):
    parsed = urlsplit(url)
    if parsed.scheme != 'https' or not parsed.hostname or parsed.username or parsed.password:
        raise MediaServiceError('Для склейки нужны HTTPS-ссылки видео и аудио.')
    async with http.stream('GET', url) as response:
        response.raise_for_status()
        size = 0
        with path.open('wb') as stream:
            async for chunk in response.aiter_bytes():
                size += len(chunk)
                if size > limit:
                    raise MediaServiceError('Медиафайл слишком большой.')
                stream.write(chunk)
    if not size:
        raise MediaServiceError('Загружен пустой медиафайл.')


def _upload_video(client, settings, path: Path) -> str:
    key = f'videos/{uuid4().hex}.mp4'
    with path.open('rb') as stream:
        client.put_object(Bucket=settings.storage_bucket, Key=key, Body=stream, ContentType='video/mp4')
    base = settings.storage_public_base_url or f"https://storage.yandexcloud.net/{quote(settings.storage_bucket, safe='')}"
    return base.rstrip('/') + '/' + key


async def merge_video_and_audio(video_url: str, audio_url: str) -> str:
    """Повторять видеоряд до конца песни, заменяя исходную звуковую дорожку."""
    binary = shutil.which('ffmpeg')
    if not binary:
        raise MediaServiceError('Для склейки видео и аудио установите ffmpeg.')
    settings = Config.from_env()
    client = await asyncio.to_thread(_storage_client, settings)
    try:
        with tempfile.TemporaryDirectory(prefix='video-merge-') as directory:
            source_video, source_audio, target = (Path(directory)/name for name in ('video.mp4', 'audio.mp3', 'output.mp4'))
            logger.info('Скачиваю видео и песню для склейки.')
            async with httpx.AsyncClient(timeout=120, trust_env=False, follow_redirects=False) as http:
                await _download(http, video_url, source_video, 128 * 1024 * 1024)
                await _download(http, audio_url, source_audio, 32 * 1024 * 1024)
            duration = await audio_duration(source_audio)
            logger.info('Повторяю видеоряд на всю песню: %.2f секунд.', duration)
            await run_media_process(binary, '-nostdin', '-v', 'error', '-protocol_whitelist', 'file,pipe',
                '-stream_loop', '-1', '-i', str(source_video), '-i', str(source_audio), '-map', '0:v:0', '-map', '1:a:0',
                '-c:v', 'copy', '-c:a', 'aac', '-t', f'{duration:.6f}', '-shortest', '-movflags', '+faststart', '-y', str(target))
            if not target.is_file() or not target.stat().st_size:
                raise MediaServiceError('ffmpeg не создал готовое видео.')
            logger.info('Загружаю склеенное видео в Yandex Object Storage.')
            result = await asyncio.to_thread(_upload_video, client, settings, target)
            logger.info('Видео с песней готово.')
            return result
    finally:
        await asyncio.to_thread(client.close)


async def create_musical_video(image_url: str, audio_url: str) -> str:
    """Статичная картинка и полная песня в совместимом с MAX MP4."""
    binary = shutil.which('ffmpeg')
    if not binary:
        raise MediaServiceError('Для музыкальной открытки установите ffmpeg.')
    settings = Config.from_env()
    client = await asyncio.to_thread(_storage_client, settings)
    try:
        with tempfile.TemporaryDirectory(prefix='musical-card-') as directory:
            image, audio, target = (Path(directory)/name for name in ('image.jpg', 'audio.mp3', 'output.mp4'))
            logger.info('Скачиваю картинку и полную песню для музыкальной открытки.')
            async with httpx.AsyncClient(timeout=120, trust_env=False, follow_redirects=False) as http:
                await _download(http, image_url, image, 32 * 1024 * 1024)
                await _download(http, audio_url, audio, 32 * 1024 * 1024)
            duration = await audio_duration(audio)
            # pad нужен для нечётных размеров; исходное изображение не кадрируется.
            await run_media_process(binary, '-nostdin', '-v', 'error', '-protocol_whitelist', 'file,pipe',
                '-y', '-loop', '1', '-framerate', '25', '-i', str(image), '-i', str(audio),
                '-map', '0:v:0', '-map', '1:a:0', '-c:v', 'libx264', '-tune', 'stillimage',
                '-vf', 'pad=ceil(iw/2)*2:ceil(ih/2)*2', '-c:a', 'aac', '-b:a', '192k',
                '-pix_fmt', 'yuv420p', '-t', f'{duration:.6f}', '-shortest', '-movflags', '+faststart',
                '-map_metadata', '-1', '-map_chapters', '-1', str(target), timeout=600)
            if not target.is_file() or not target.stat().st_size:
                raise MediaServiceError('ffmpeg не создал музыкальную открытку.')
            result = await asyncio.to_thread(_upload_video, client, settings, target)
            logger.info('Музыкальная открытка готова, длительность %.2f секунд.', duration)
            return result
    finally:
        await asyncio.to_thread(client.close)


async def upload_video_for_max(bot, video_url: str):
    """Загрузить MP4 в MAX, чтобы отправить сам видеофайл вместе со ссылкой."""
    from maxapi.types import InputMedia
    from maxapi.enums.upload_type import UploadType
    with tempfile.TemporaryDirectory(prefix='max-musical-video-') as directory:
        path = Path(directory)/'musical-card.mp4'
        async with httpx.AsyncClient(timeout=120, trust_env=False, follow_redirects=False) as http:
            await _download(http, video_url, path, 128 * 1024 * 1024)
        attachment = await bot.upload_media(InputMedia(path=str(path), type=UploadType.VIDEO))
        token = attachment.payload.token
        if not isinstance(token, str) or not token.strip():
            raise MediaServiceError('MAX вернул пустой media token видео.')
        return attachment

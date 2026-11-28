"""Retain a user-supplied MP3 for offline episode assembly; no paid music API."""
from media_policy import private_enabled
import math
from uuid import uuid4

import httpx

from . import repository as repo
from .render import probe, process

MAX_BYTES = 20 * 1024 * 1024
MAX_SECONDS = 600


async def upload_music(assets, snapshot, url):
    try:
        relative, _ = await assets.download(url, 'bin', limit=MAX_BYTES)
    except httpx.HTTPError:
        raise ValueError('Не удалось скачать музыку. Отправьте MP3 файлом ещё раз или пришлите доступную HTTPS-ссылку.') from None
    source = assets.path(relative)
    try:
        info = await probe(source)
        track = next((s for s in info.get('streams', []) if s.get('codec_type') == 'audio'), {})
        seconds = float(info.get('format', {}).get('duration', 0))
        if (info.get('format', {}).get('format_name') != 'mp3' or track.get('codec_name') != 'mp3'
                or not math.isfinite(seconds) or not 0 < seconds <= MAX_SECONDS):
            raise ValueError('Отправьте MP3 длительностью до 10 минут и размером до 20 МБ. Имя файла не заменяет проверку формата.')
        # Decode the whole track before replacing the selected music. Normalize for the existing mixer.
        output = assets.path(uuid4().hex + '.mp3')
        if private_enabled(assets.config):
            import os
            fd=os.open(output,os.O_WRONLY|os.O_CREAT|os.O_EXCL,0o600)
            os.close(fd)
        await process('ffmpeg', '-nostdin', '-v', 'error', '-y', '-i', source,
                      '-map', '0:a:0', '-vn', '-map_metadata', '-1', '-ar', '48000', '-ac', '2',
                      '-c:a', 'libmp3lame', '-b:a', '192k', output, timeout=90)
        fields={}
        if private_enabled(assets.config):
            from private_media import is_private, MediaError
            reference=await assets.publish(output.name)
            if not is_private(reference):raise MediaError('Private audio publication refused a public URL')
            fields['public_url']=reference
        return await repo.add_asset(snapshot['project_id'], snapshot['target'], snapshot['id'], 'music',
                                    snapshot['context_hash'], output.name, assets.checksum(output),
                                    metadata=repo.encoded({'duration_seconds': seconds, 'source': 'uploaded_mp3'}),**fields)
    except (ValueError, TypeError, KeyError, StopIteration, TimeoutError):
        raise ValueError('Не удалось прочитать музыку. Отправьте исправный MP3 до 20 МБ и 10 минут; предыдущая музыка сохранена.') from None

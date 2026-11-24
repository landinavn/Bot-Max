"""Locate the first approved sung words; instrumental music is NOT treated as silence."""
import math
import re
from pathlib import Path

import httpx
from openai import AsyncOpenAI

from config import Config


def words(text):
    return re.findall(r'[a-zа-яё0-9]+',text.casefold().replace('ё','е'))


def first_lyric(text):
    lines=[line.strip() for line in re.sub(r'\[[^\]]*\]','',text).splitlines() if line.strip()]
    if not lines:raise ValueError('Нет утверждённой первой строки песни для синхронизации.')
    expected=words(lines[0])[:4]
    if len(expected)<3:raise ValueError('Для синхронизации нужна первая строка хотя бы из трёх слов.')
    return lines[0],expected


def matched_start(transcribed,expected):
    tokens=[]
    for item in transcribed:
        value=item if isinstance(item,dict) else item.model_dump()
        tokens.extend((token,value.get('start')) for token in words(value.get('word') or ''))
    for i in range(len(tokens)-len(expected)+1):
        if [t[0] for t in tokens[i:i+len(expected)]]!=expected:continue
        start=tokens[i][1]
        if isinstance(start,(int,float)) and math.isfinite(start) and 0<=start<=29:
            return max(0.,float(start)-0.06)  # retain the leading consonant
    raise ValueError('Не удалось найти начало утверждённой строки. Сцены ещё не создавались; нужна проверка песни.')


async def detect_song_start(audio:Path,lyrics:str,workdir:Path):
    from media_service import run_media_process
    import shutil
    line,expected=first_lyric(lyrics)
    settings=Config.from_env()
    if not settings.image_api_key:raise ValueError('Не настроен API для синхронизации начала песни.')
    sample=workdir/'vocal-start-sample.mp3'
    await run_media_process(shutil.which('ffmpeg'),'-nostdin','-v','error','-y','-i',str(audio),
        '-t','30','-ac','1','-ar','16000','-c:a','libmp3lame',str(sample),timeout=60)
    async with httpx.AsyncClient(proxy=settings.openai_proxy_url,timeout=60,trust_env=False) as http:
        async with AsyncOpenAI(api_key=settings.image_api_key,base_url='https://api.openai.com/v1',
                http_client=http,max_retries=0) as client:
            with sample.open('rb') as stream:
                result=await client.audio.transcriptions.create(model='whisper-1',file=stream,
                    language='ru',prompt=line,response_format='verbose_json',timestamp_granularities=['word'])
    return matched_start(result.words or [],expected)


class SongAlignmentError(ValueError):
    """Alignment failed before any visual generation; original media remain intact."""


def alignment_source(audio_url, lyrics):
    import hashlib
    return [audio_url, hashlib.sha256(lyrics.encode()).hexdigest()]


def retryable_alignment_error(exc):
    from openai import APIConnectionError, APIStatusError
    return (isinstance(exc, (TimeoutError, httpx.RequestError, APIConnectionError))
            or isinstance(exc, APIStatusError) and exc.status_code in (408, 429, 500, 502, 503, 504))


async def resolve_song_start(story, audio, lyrics, audio_url, workdir, checkpoint):
    """Two persisted attempts per audio/text pair, including interrupted legacy requests."""
    import asyncio
    import logging
    source = alignment_source(audio_url, lyrics)
    if story.get('alignment_source') == source:
        start = story.get('audio_start')
        if isinstance(start, (int, float)) and not isinstance(start, bool) and math.isfinite(start) and 0 <= start <= 29:
            return float(start)
        raise SongAlignmentError('Сохранённое начало песни некорректно. Нужна проверка поддержки.')
    if story.get('alignment_attempt_source') != source:
        # The old release persisted only this marker, without the attempt number.
        story['alignment_attempts'] = int(story.get('alignment_submitting') == source)
        story['alignment_attempt_source'] = source
    while story['alignment_attempts'] < 2:
        story['alignment_attempts'] += 1
        story['alignment_submitting'] = source
        await checkpoint()
        try:
            start = await detect_song_start(audio, lyrics, workdir)
            if not isinstance(start, (int, float)) or isinstance(start, bool) or not math.isfinite(start) or not 0 <= start <= 29:
                raise ValueError('Некорректная отметка начала песни.')
        except asyncio.CancelledError:
            story['alignment_submitting'] = None
            story['alignment_error'] = {'source': source, 'type': 'interrupted'}
            await asyncio.shield(checkpoint())
            raise
        except Exception as exc:
            story['alignment_submitting'] = None
            story['alignment_error'] = {'source': source, 'type': type(exc).__name__}
            logging.getLogger(__name__).warning(
                'Song alignment failed: attempt=%s error_type=%s status=%s',
                story['alignment_attempts'], type(exc).__name__, getattr(exc, 'status_code', None))
            await asyncio.shield(checkpoint())
            if retryable_alignment_error(exc) and story['alignment_attempts'] < 2:
                await asyncio.sleep(1)
                continue
            raise SongAlignmentError('Не удалось проверить начало песни. Песня и картинка сохранены; '
                                     'сцены ещё не создавались. Нужна проверка поддержки.') from exc
        story.update(alignment_source=source, audio_start=float(start), alignment_submitting=None,
                     alignment_error=None)
        await asyncio.shield(checkpoint())
        return float(start)
    raise SongAlignmentError('Две попытки проверки начала песни уже использованы. Песня и картинка сохранены. '
                             'Повторные запросы отключены; нужна проверка поддержки.')

"""Locate the first approved sung words; instrumental music is NOT treated as silence."""
from safe_logging import get_logger
import math
import re
from pathlib import Path

import httpx
from openai import AsyncOpenAI

from config import Config


def words(text):
    return re.findall(r'[a-zа-яё0-9]+',text.casefold().replace('ё','е'))


def first_lyric(text):
    section = re.compile(r'^(?:куплет|припев|переход|бридж|вступление|verse|chorus|bridge|intro)(?:\s*\d+)?[.:]?$', re.I)
    lines = [line.strip(' \t*#') for line in re.sub(r'\[[^\]]*\]', '', text).splitlines()]
    lines = [line for line in lines if line and not section.fullmatch(line)]
    if not lines:
        raise ValueError('Нет утверждённой первой строки песни для синхронизации.')
    expected = words(lines[0])[:8]
    if len(expected) < 3:
        raise ValueError('Для синхронизации нужна первая строка хотя бы из трёх слов.')
    return lines[0], expected


def _distance(left, right):
    row = list(range(len(right) + 1))
    for i, word in enumerate(left, 1):
        new = [i]
        for j, other in enumerate(right, 1):
            new.append(min(new[-1] + 1, row[j] + 1, row[j-1] + (word != other)))
        row = new
    return row[-1]


def matched_start(transcribed, expected):
    tokens = []
    for item in transcribed:
        value = item if isinstance(item, dict) else item.model_dump()
        tokens.extend((token, value.get('start')) for token in words(value.get('word') or ''))
    minimum = min(4, len(expected))
    for i in range(len(tokens)):
        for length in range(len(expected), minimum - 1, -1):
            anchor = expected[:length]
            for size in range(1, min(length + 1, len(tokens) - i) + 1):
                part = tokens[i:i+size]
                starts = [t[1] for t in part]
                if any(not isinstance(t, (int, float)) or isinstance(t, bool) or not math.isfinite(t)
                       or not 0 <= t <= 30 for t in starts):
                    continue
                if starts[0] > 29 or any(not 0 <= b-a <= 5 for a, b in zip(starts, starts[1:])):
                    continue
                observed = [t[0] for t in part]
                # Exact letters, even when the recognizer splits/joins words differently.
                exact = ''.join(anchor) == ''.join(observed)
                # One internal word error only, supported by at least four matching words.
                tolerant = (length >= 5 and abs(size-length) <= 1
                    and anchor[0] == observed[0] and anchor[-1] == observed[-1]
                    and len([w for w in anchor if len(w) >= 4 and w in observed]) >= 2
                    and _distance(anchor, observed) <= 1)
                if exact or tolerant:
                    return max(0., float(starts[0]) - 0.06)
    raise ValueError('Не удалось найти начало утверждённой строки. Сцены ещё не создавались; нужна проверка песни.')


def transcript_words(items):
    """Only the bounded 30-second word transcript is retained, without API metadata."""
    result = []
    for item in list(items)[:512]:
        value = item if isinstance(item, dict) else item.model_dump()
        start = value.get('start')
        result.append({'word': str(value.get('word') or '')[:200],
                       'start': float(start) if isinstance(start, (int, float)) and not isinstance(start, bool)
                                and math.isfinite(start) else None})
    return result


async def detect_song_start(audio:Path,lyrics:str,workdir:Path, *, on_transcribed=None):
    from private_media import mutation_policy
    mutation_policy()
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
    transcript = transcript_words(result.words or [])
    if on_transcribed is not None:
        import asyncio
        await asyncio.shield(on_transcribed(transcript))
    return matched_start(transcript, expected)


class SongAlignmentError(ValueError):
    """Alignment failed before any visual generation; original media remain intact."""


def alignment_source(audio_url, lyrics):
    import hashlib
    return [audio_url, hashlib.sha256(lyrics.encode()).hexdigest()]


def retryable_alignment_error(exc):
    from openai import APIConnectionError, APIStatusError
    return (isinstance(exc, (TimeoutError, httpx.RequestError, APIConnectionError))
            or isinstance(exc, APIStatusError) and exc.status_code in (408, 429, 500, 502, 503, 504))


def apply_fixed_start(story, lyrics, audio_url, fixed_start):
    """No inference/API call. Keep a deliberate manual mark for this exact audio/text."""
    from mother_song_timing import FIXED_INTRO_METHOD
    if (isinstance(fixed_start, bool) or not isinstance(fixed_start, (int, float))
            or not math.isfinite(fixed_start) or not 0 <= fixed_start <= 29):
        raise SongAlignmentError('Некорректное фиксированное начало песни.')
    source = alignment_source(audio_url, lyrics)
    saved = story.get('audio_start')
    if (story.get('alignment_source') == source and story.get('alignment_method') == 'admin'
            and isinstance(saved, (int, float)) and not isinstance(saved, bool)
            and math.isfinite(saved) and 0 <= saved <= 29):
        story.update(alignment_submitting=None, alignment_error=None)
        return float(saved)
    story.update(alignment_source=source, audio_start=float(fixed_start), alignment_method=FIXED_INTRO_METHOD,
                 alignment_submitting=None, alignment_error=None)
    return float(fixed_start)


async def resolve_song_start(story, audio, lyrics, audio_url, workdir, checkpoint, *, fixed_start=None):
    """Explicit editing offset skips ASR; other flows keep the two-attempt durable budget."""
    import asyncio
    import logging
    source = alignment_source(audio_url, lyrics)
    if fixed_start is not None:
        start = apply_fixed_start(story, lyrics, audio_url, fixed_start)
        await asyncio.shield(checkpoint())
        return start
    if story.get('alignment_source') == source:
        start = story.get('audio_start')
        if isinstance(start, (int, float)) and not isinstance(start, bool) and math.isfinite(start) and 0 <= start <= 29:
            return float(start)
        raise SongAlignmentError('Сохранённое начало песни некорректно. Нужна проверка поддержки.')
    cached = story.get('alignment_transcript') or {}
    if cached.get('audio_url') == audio_url:
        try:
            start = matched_start(cached['words'], first_lyric(lyrics)[1])
        except ValueError as exc:
            story.update(alignment_submitting=None, alignment_error={'source': source, 'type': 'lyrics_mismatch'})
            await asyncio.shield(checkpoint())
            raise SongAlignmentError('Распознанная песня сохранена, но начало строки не найдено. '
                                     'Новое распознавание не запускается. Нужна проверка начала песни.') from exc
        story.update(alignment_source=source, audio_start=start, alignment_method='transcript',
                     alignment_submitting=None, alignment_error=None)
        await asyncio.shield(checkpoint())
        return start
    async def retain_transcript(transcript):
        story['alignment_transcript'] = {'audio_url': audio_url, 'words': transcript}
        await checkpoint()
    if story.get('alignment_attempt_source') != source:
        # The old release persisted only this marker, without the attempt number.
        story['alignment_attempts'] = int(story.get('alignment_submitting') == source)
        story['alignment_attempt_source'] = source
    while story['alignment_attempts'] < 2:
        story['alignment_attempts'] += 1
        story['alignment_submitting'] = source
        await checkpoint()
        try:
            start = await detect_song_start(audio, lyrics, workdir, on_transcribed=retain_transcript)
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
            get_logger(__name__).warning(
                'Song alignment failed: attempt=%s error_type=%s status=%s',
                story['alignment_attempts'], type(exc).__name__, getattr(exc, 'status_code', None))
            await asyncio.shield(checkpoint())
            if retryable_alignment_error(exc) and story['alignment_attempts'] < 2:
                await asyncio.sleep(1)
                continue
            raise SongAlignmentError('Не удалось проверить начало песни. Песня и картинка сохранены; '
                                     'сцены ещё не создавались. Нужна проверка поддержки.') from exc
        story.update(alignment_source=source, audio_start=float(start), alignment_method='transcript', alignment_submitting=None,
                     alignment_error=None)
        await asyncio.shield(checkpoint())
        return float(start)
    raise SongAlignmentError('Две попытки проверки начала песни уже использованы. Песня и картинка сохранены. '
                             'Повторные запросы отключены; нужна проверка поддержки.')

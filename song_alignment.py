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

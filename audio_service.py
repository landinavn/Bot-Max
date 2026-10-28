"""Асинхронная генерация музыки TTAPI/Suno; без автоматического повторения POST."""

import asyncio
import json
import logging
import re
import shutil
import tempfile
from pathlib import Path
from uuid import uuid4
from urllib.parse import urlsplit, quote

import httpx
from openai import AsyncOpenAI

from config import Config

logger = logging.getLogger(__name__)

BASE_URL = "https://api.ttapi.io"
LONG_WAIT_MESSAGE = "⏳ Трек все еще создается, это занимает больше времени, чем обычно. Пожалуйста, подождите, я пришлю его, как только он будет готов."


def build_song_request(details: str, scenario_title: str, audio_format: str, style: str | None = None) -> tuple[str, str, str]:
    if audio_format not in {'fun', 'gift'} or not details.strip():
        raise ValueError('Некорректные детали песни.')
    chorus = "Эта песня для тебя, улыбайся, пой со мной!\nПусть удача каждый день будет рядом с тобой!"
    prompt = (f"[Verse 1]\nС тобою мир становится светлей,\nИ каждый день наполнен добротою.\n[Chorus]\n{chorus}"
              f"\n[Verse 2]\n{details.strip()}\nИз этих встреч слагается история твоя.\n[Chorus]\n{chorus}"
              f"\n[Bridge]\nПусть впереди сияет новый день,\nИ счастье остаётся рядом с нами.\n[Chorus]\n{chorus}")
    tags = str(style or ('upbeat pop, playful, Russian vocals, short song, 30-40 seconds' if audio_format == 'fun'
                        else 'warm emotional pop, Russian vocals, heartfelt, 2-3 minutes'))
    if len(prompt) > 5000 or len(tags) > 1000:
        raise ValueError('Слишком много деталей для TTAPI.')
    return prompt, tags, f"{scenario_title}: {details.split(',')[0]}"[:80]


class AudioServiceError(RuntimeError):
    """Ошибка TTAPI, безопасная для журналирования без ключа и тела ответа."""


class AudioNotConfigured(AudioServiceError):
    pass


class SongFailed(AudioServiceError):
    pass


LYRICS_SYSTEM_PROMPT = (
    'Ты профессиональный автор персональных песен на русском языке. '
    'НЕ перечисляй факты и привычки в первом куплете. Начни песню с эмоционального или поэтического вступления '
    '(например, о том, какой это замечательный человек). Факты и детали аккуратно вплети в повествование второго куплета или бриджа. '
    'Соблюдай классическую структуру: [Куплет 1] -> [Припев] -> [Куплет 2] -> [Припев] -> [Бридж] -> [Припев]. '
    'Для Suno записывай эти шесть секций как [Verse 1], [Chorus], [Verse 2], [Chorus], [Bridge], [Chorus]. '
    'Верни ТОЛЬКО текст песни. Не пиши название песни, название сценария или заголовок. Начинай сразу с [Verse 1]. '
    'Сохрани имена и смысл деталей, не вставляй перечень из анкеты дословно. Не добавляй пояснения или кодовые блоки. '
    'Если нужен короткий формат 30–40 секунд, используй 1–2 строки на секцию, иначе 2–4 строки. До 5000 символов.'
)


async def write_song_lyrics(notes: str, tags: str, title: str) -> str:
    settings = Config.from_env()
    if not settings.image_api_key:
        raise AudioNotConfigured('Не настроена текстовая модель для стихов.')
    try:
        async with httpx.AsyncClient(proxy=settings.openai_proxy_url, timeout=60, trust_env=False) as http_client:
            async with AsyncOpenAI(api_key=settings.image_api_key, base_url='https://api.openai.com/v1',
                                   http_client=http_client, max_retries=0) as client:
                result = await client.chat.completions.create(model='gpt-4o-mini', temperature=0.7,
                    messages=[{'role': 'system', 'content': LYRICS_SYSTEM_PROMPT},
                              {'role': 'user', 'content': json.dumps({'details': notes, 'style': tags, 'title_for_context_only': title}, ensure_ascii=False)}])
    except Exception as exc:
        raise AudioServiceError('Не удалось написать стихи.') from exc
    if not result.choices or result.choices[0].finish_reason != 'stop':
        raise AudioServiceError('Текстовая модель не вернула полную песню.')
    text = result.choices[0].message.content
    if not isinstance(text, str) or len(text) > 5000:
        raise AudioServiceError('Некорректный текст песни.')
    text = text.strip()
    sections = re.findall(r'\[([^\]]+)\]', text)
    if not text.startswith('[Verse 1]') or sections != ['Verse 1','Chorus','Verse 2','Chorus','Bridge','Chorus']:
        raise AudioServiceError('Не соблюдена структура песни.')
    if any(not part.strip() for part in re.split(r'\[[^\]]+\]', text)[1:]):
        raise AudioServiceError('В песне есть пустые секции.')
    return text


async def make_protected_preview(audio_url: str) -> str:
    """Публичным становится только 15-секундный MP3 со звуковой меткой."""
    parsed = urlsplit(audio_url)
    if parsed.scheme != 'https' or not parsed.hostname or parsed.username or parsed.password:
        raise AudioServiceError('Некорректный URL аудио.')
    binary = shutil.which('ffmpeg')
    if not binary:
        raise AudioNotConfigured('Для превью установите ffmpeg.')
    settings = Config.from_env()
    from image_service import _storage_client
    client = await asyncio.to_thread(_storage_client, settings)
    try:
        with tempfile.TemporaryDirectory(prefix='song-preview-') as directory:
            source, target = Path(directory)/'source.mp3', Path(directory)/'preview.mp3'
            async with httpx.AsyncClient(timeout=60, trust_env=False, follow_redirects=False) as http:
                async with http.stream('GET', audio_url) as response:
                    response.raise_for_status()
                    total = 0
                    with source.open('wb') as file:
                        async for chunk in response.aiter_bytes():
                            total += len(chunk)
                            if total > 32 * 1024 * 1024:
                                raise AudioServiceError('Слишком большой аудиофайл.')
                            file.write(chunk)
            # Фильтр добавляет короткую звуковую метку каждые пять секунд.
            process = await asyncio.create_subprocess_exec(binary, '-nostdin', '-v', 'error', '-protocol_whitelist', 'file,pipe',
                '-i', str(source), '-f', 'lavfi', '-i', 'sine=frequency=1100:duration=15',
                '-filter_complex', "[0:a]atrim=0:15,asetpts=PTS-STARTPTS[a];[1:a]volume='if(lt(mod(t,5),0.7),0.5,0)':eval=frame[b];[a][b]amix=inputs=2:duration=first:normalize=0[out]",
                '-map', '[out]', '-map_metadata', '-1', '-map_chapters', '-1', '-t', '15', '-codec:a', 'libmp3lame', '-b:a', '128k', '-y', str(target),
                stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.PIPE)
            try:
                await asyncio.wait_for(process.communicate(), timeout=60)
            except BaseException:
                if process.returncode is None:
                    process.kill()
                    await process.wait()
                raise
            if process.returncode != 0 or not target.is_file() or not target.stat().st_size:
                raise AudioServiceError('Не удалось создать фрагмент песни.')
            key = f'audio-previews/{uuid4().hex}.mp3'
            await asyncio.to_thread(client.put_object, Bucket=settings.storage_bucket, Key=key,
                                    Body=target.read_bytes(), ContentType='audio/mpeg')
            base = settings.storage_public_base_url or f"https://storage.yandexcloud.net/{quote(settings.storage_bucket, safe='')}"
            return base.rstrip('/') + '/' + key
    finally:
        await asyncio.to_thread(client.close)


def _headers() -> dict[str, str]:
    key = Config.from_env().ttapi_key
    if not key:
        raise AudioNotConfigured("Задайте TTAPI_KEY.")
    return {"TT-API-KEY": key, "Content-Type": "application/json"}


async def _request(method: str, path: str, **kwargs) -> dict:
    headers = _headers()
    try:
        async with httpx.AsyncClient(timeout=30.0, trust_env=False, follow_redirects=False) as client:
            response = await client.request(method, BASE_URL + path, headers=headers, **kwargs)
            response.raise_for_status()
            result = response.json()
    except (httpx.HTTPError, ValueError) as exc:
        raise AudioServiceError("Не удалось получить корректный ответ TTAPI.") from exc
    if not isinstance(result, dict):
        raise AudioServiceError("TTAPI вернул неожиданный формат ответа.")
    return result


async def generate_song(prompt: str, tags: str, title: str) -> str:
    if not isinstance(prompt, str) or not prompt.strip() or len(prompt) > 5000:
        raise ValueError("Текст песни должен содержать от 1 до 5000 символов.")
    if not isinstance(tags, str) or len(tags) > 1000 or not isinstance(title, str) or len(title) > 80:
        raise ValueError("Слишком длинные или некорректные tags/title.")
    result = await _request("POST", "/suno/v1/music", json={
        "custom": True, "instrumental": False, "mv": "chirp-v6",
        "prompt": prompt, "title": title, "tags": tags, "audio_format": "mp3",
    })
    if result.get("status") in {"FAILED", "ERROR"}:
        logger.error("TTAPI отклонил запрос: %r", result)
        raise SongFailed("TTAPI отклонил генерацию.")
    data = result.get("data")
    job_id = result.get("jobId") or (data.get("jobId") if isinstance(data, dict) else None)
    if not isinstance(job_id, str) or not job_id.strip():
        raise AudioServiceError("TTAPI не вернул jobId.")
    return job_id


async def fetch_song(job_id: str) -> dict:
    if not isinstance(job_id, str) or not job_id.strip():
        raise ValueError("Не задан jobId.")
    return await _request("GET", "/suno/v2/fetch", params={"jobId": job_id})


async def wait_for_song(job_id: str, timeout: int = 360) -> dict:
    if timeout <= 0:
        raise TimeoutError("Истекло время ожидания песни.")
    async with asyncio.timeout(timeout):
        while True:
            result = await fetch_song(job_id)
            status = result.get("status")
            logger.info("TTAPI статус: %s (job_id=%s)", status, job_id)
            if status == "SUCCESS":
                return result
            if status in {"FAILED", "ERROR"}:
                logger.error("TTAPI ошибка генерации (job_id=%s): %r", job_id, result)
                raise SongFailed("Генерация песни завершилась ошибкой.")
            if status not in {"ON_QUEUE", "PROCESSING", "PENDING", "QUEUED", "IN_PROGRESS"}:
                logger.error("TTAPI неизвестный статус (job_id=%s): %r", job_id, result)
                raise AudioServiceError("TTAPI вернул неизвестный статус задачи.")
            await asyncio.sleep(5)


async def wait_for_song_with_progress(job_id, notify, *, waiter=None, notification_delay=120):
    """Уведомление один раз за ожидание; отмена не оставляет фоновых задач."""
    task = asyncio.create_task((waiter or wait_for_song)(job_id))
    try:
        done, _ = await asyncio.wait({task}, timeout=notification_delay)
        if not done:
            try:
                await notify(LONG_WAIT_MESSAGE)
            except Exception:
                logger.warning("Не удалось отправить уведомление ожидания TTAPI (job_id=%s).", job_id)
        return await task
    finally:
        if not task.done():
            task.cancel()
        await asyncio.gather(task, return_exceptions=True)


def song_tracks(result: dict) -> list[dict[str, str]]:
    """Только реальные HTTPS audioUrl, максимум два варианта, без дубликатов."""
    data = result.get("data")
    musics = data.get("musics") if isinstance(data, dict) else None
    if not isinstance(musics, list):
        raise AudioServiceError("TTAPI не вернул список треков.")
    tracks = []
    for music in musics:
        if not isinstance(music, dict):
            continue
        url = music.get("audioUrl")
        try:
            parsed = urlsplit(url) if isinstance(url, str) else None
            valid = parsed and parsed.scheme == "https" and parsed.hostname and not parsed.username and not parsed.password
        except ValueError:
            valid = False
        if valid and not any(track["audioUrl"] == url for track in tracks):
            tracks.append({"audioUrl": url, "title": str(music.get("title") or "Песня")[:80]})
    if not tracks:
        raise AudioServiceError("В результате TTAPI нет доступного аудио.")
    return tracks[:2]

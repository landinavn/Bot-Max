"""Асинхронная генерация музыки TTAPI/Suno; без автоматического повторения POST."""

import asyncio
from urllib.parse import urlsplit

import httpx

from config import Config

BASE_URL = "https://api.ttapi.io"


class AudioServiceError(RuntimeError):
    """Ошибка TTAPI, безопасная для журналирования без ключа и тела ответа."""


class AudioNotConfigured(AudioServiceError):
    pass


class SongFailed(AudioServiceError):
    pass


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
    if result.get("status") == "FAILED":
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


async def wait_for_song(job_id: str, timeout: int = 300) -> dict:
    if timeout <= 0:
        raise TimeoutError("Истекло время ожидания песни.")
    async with asyncio.timeout(timeout):
        while True:
            result = await fetch_song(job_id)
            status = result.get("status")
            if status == "SUCCESS":
                return result
            if status == "FAILED":
                raise SongFailed("Генерация песни завершилась ошибкой.")
            if status not in {"ON_QUEUE", "PROCESSING", "PENDING", "QUEUED", "IN_PROGRESS"}:
                raise AudioServiceError("TTAPI вернул неизвестный статус задачи.")
            await asyncio.sleep(5)


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

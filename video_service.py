"""Генерация видео PiAPI/Kling: один POST и ограниченное ожидание результата."""

import asyncio
import json
import logging
from time import monotonic
from urllib.parse import quote, urlsplit

import httpx

from config import Config

logger = logging.getLogger(__name__)
TASK_URL = "https://api.piapi.ai/api/v1/task"
POLL_INTERVAL = 15
REQUEST_TIMEOUT = 30
DEFAULT_ANIMATION_PROMPT = "The character is smiling and blinking naturally"


class VideoServiceError(RuntimeError):
    """Ошибка PiAPI или некорректный ответ сервиса."""


def _api_key() -> str:
    key = Config.from_env().piapi_key
    if not key:
        raise VideoServiceError("Задайте PIAPI_KEY для генерации видео.")
    return key


def _safe_detail(value, key: str) -> str:
    text = value if isinstance(value, str) else json.dumps(value, ensure_ascii=False)
    return text.replace(key, "[REDACTED]")[:2000]


def _is_https_url(value) -> bool:
    if not isinstance(value, str):
        return False
    try:
        parsed = urlsplit(value)
        return parsed.scheme == "https" and bool(parsed.hostname) and not (parsed.username or parsed.password)
    except ValueError:
        return False


async def _request(client, method: str, url: str, key: str, **kwargs) -> dict:
    try:
        response = await client.request(method, url, headers={"X-API-Key": key}, **kwargs)
    except httpx.TimeoutException:
        raise VideoServiceError("Таймаут HTTP-запроса PiAPI.") from None
    except httpx.RequestError:
        raise VideoServiceError("Не удалось подключиться к PiAPI.") from None
    if not response.is_success:
        raise VideoServiceError(f"PiAPI HTTP {response.status_code}: {_safe_detail(response.text, key)}")
    try:
        result = response.json()
    except ValueError:
        raise VideoServiceError("PiAPI вернул некорректный JSON.") from None
    if not isinstance(result, dict):
        raise VideoServiceError("PiAPI вернул некорректный объект ответа.")
    if result.get("code") not in (None, 200):
        raise VideoServiceError(f"Ошибка PiAPI: {_safe_detail(result, key)}")
    return result


async def create_video_task(image_url: str, prompt: str) -> str:
    """Создать платную задачу Kling; POST автоматически не повторяется."""
    if not _is_https_url(image_url):
        raise ValueError("Для PiAPI нужна HTTPS-ссылка на изображение.")
    if not isinstance(prompt, str) or not prompt.strip():
        raise ValueError("Промпт видео не должен быть пустым.")
    key = _api_key()
    payload = {
        "model": "kling", "task_type": "video_generation",
        "input": {"image_url": image_url, "prompt": prompt, "version": "3.0-turbo", "mode": "std", "duration": 5,
                  "enable_audio": False},
        "config": {"service_mode": "public"},
    }
    logger.info("PiAPI: создаю задачу Kling (5 секунд, std, 3.0-turbo, enable_audio=false).")
    async with httpx.AsyncClient(timeout=REQUEST_TIMEOUT, trust_env=False) as client:
        result = await _request(client, "POST", TASK_URL, key, json=payload)
    data = result.get("data")
    task_id = data.get("task_id") if isinstance(data, dict) else None
    if not isinstance(task_id, str) or not task_id.strip():
        raise VideoServiceError("Нет data.task_id в ответе PiAPI.")
    logger.info("PiAPI: создана задача %s", task_id)
    return task_id


async def poll_video_task(task_id: str, timeout: int = 1200) -> str:
    """Опрос каждые 15 секунд, до 20 минут; общий таймаут включает ожидание HTTP."""
    if not isinstance(task_id, str) or not task_id.strip():
        raise ValueError("Не задан task_id PiAPI.")
    if timeout <= 0:
        raise ValueError("Таймаут PiAPI должен быть положительным.")
    key = _api_key()
    started_at = monotonic()
    logger.info("PiAPI: начинаю ожидание задачи %s (лимит %s секунд)", task_id, timeout)
    try:
        async with asyncio.timeout(timeout):
            async with httpx.AsyncClient(timeout=REQUEST_TIMEOUT, trust_env=False) as client:
                while True:
                    await asyncio.sleep(POLL_INTERVAL)
                    result = await _request(client, "GET", TASK_URL + "/" + quote(task_id, safe=""), key)
                    data = result.get("data")
                    if not isinstance(data, dict):
                        raise VideoServiceError("Нет data в ответе PiAPI.")
                    status = data.get("status")
                    elapsed = max(0, monotonic() - started_at)
                    remaining = max(0, timeout - elapsed)
                    logger.info("PiAPI: задача %s, статус %s, прошло %.0f сек, осталось %.0f сек",
                                task_id, status, elapsed, remaining)
                    if status == "completed":
                        output = data.get("output")
                        video_url = output.get("video") if isinstance(output, dict) else None
                        if not _is_https_url(video_url):
                            raise VideoServiceError("Нет HTTPS-ссылки data.output.video в ответе PiAPI.")
                        logger.info("PiAPI: видео готово, задача %s", task_id)
                        return video_url
                    if status == "failed":
                        detail = data.get("error") or data.get("message") or "Причина не указана"
                        raise VideoServiceError(f"PiAPI: задача завершилась с ошибкой: {_safe_detail(detail, key)}")
    except TimeoutError:
        logger.error("PiAPI: таймаут ожидания задачи %s (%s секунд)", task_id, timeout)
        raise TimeoutError(f"Таймаут ожидания PiAPI ({timeout} секунд), task_id={task_id}") from None


async def generate_video_from_avatar(avatar_url: str, prompt: str = DEFAULT_ANIMATION_PROMPT) -> str:
    """Создать задачу по аватару и вернуть ссылку на готовое видео."""
    task_id = await create_video_task(avatar_url, prompt)
    return await poll_video_task(task_id)

"""Генерация видео PiAPI/Kling: повторы внутренних ошибок и ограниченное ожидание."""

import asyncio
import json
import logging
from time import monotonic
from collections.abc import Awaitable, Callable
from urllib.parse import quote, urlsplit

import httpx

from config import Config
from image_service import count_people_in_photo

logger = logging.getLogger(__name__)
TASK_URL = "https://api.piapi.ai/api/v1/task"
POLL_INTERVAL = 15
REQUEST_TIMEOUT = 30
DEFAULT_ANIMATION_PROMPT = "The character is smiling, blinking naturally, and slightly moving their head from side to side."
CARTOON_ANIMATION_PROMPT = (DEFAULT_ANIMATION_PROMPT +
    ' The character makes small, natural hand gestures and subtle upper-body movements. '
    'Keep hands anatomically correct; no exaggerated gestures.')
PHOTO_ANIMATION_PROMPT = (
    'Bring this real photograph gently to life. Preserve the exact identities, ages, clothing, and background. '
    'If people are present, add warm smiles. Each person may blink naturally at most ONCE during the entire 5-second video. '
    'Make it one brief, gentle closure and reopening of both eyes, then keep the eyes naturally open for the rest of the video. '
    'No repeated blinking, eyelid fluttering, or winking. In group photos, do not synchronize the blinks. '
    'If a person has a hand already visible in the photo, let them give a small, slow wave with ONLY ONE existing visible hand. '
    'Keep their other hand and body still. If no hand is visible, animate only smiling and the single brief blink described above; do not introduce a hand. '
    'Preserve the original number of arms and hands and their anatomy. No extra, duplicated or newly appearing hands, arms or fingers. '
    'Keep the camera steady. '
    'Photorealistic, no cartoon transformation, no new people, no distorted faces or extra limbs.'
)


CARTOON_VIDEO_PROMPTS = {
    'theme_ny': CARTOON_ANIMATION_PROMPT + ' Snow is falling, fairy lights are twinkling.',
    'theme_bday': CARTOON_ANIMATION_PROMPT + ' Balloons are floating, confetti is falling.',
    'theme_bday_kid': CARTOON_ANIMATION_PROMPT + ' Pastel balloons are bobbing, confetti is floating.',
    'theme_valentine': CARTOON_ANIMATION_PROMPT + ' Hearts are pulsing, rose petals are falling.',
    'theme_feb23': CARTOON_ANIMATION_PROMPT + ' The St. George ribbon is waving, snow is falling.',
    'theme_mar8': CARTOON_ANIMATION_PROMPT + ' Flowers are swaying, sunlight is shimmering.',
    'theme_mother': CARTOON_ANIMATION_PROMPT + ' Soft particles are floating.',
    'theme_father': CARTOON_ANIMATION_PROMPT + ' Warm light is flickering, dust motes are floating.',
}


CROWD_VIDEO_PROMPT = (
    'Cinematic background animation only. The glowing lights shimmer, and there is a subtle camera zoom. '
    'The people remain completely still like in a photograph. DO NOT animate faces or bodies.'
)


def build_video_prompt(mode: str, people_count: int = 0) -> str:
    """Добавить только подходящие стилю эффекты; неизвестный стиль получает нейтральную анимацию."""
    if people_count > 5:
        return CROWD_VIDEO_PROMPT
    normalized = (mode or '').lower()
    if normalized in CARTOON_VIDEO_PROMPTS:
        return CARTOON_VIDEO_PROMPTS[normalized]
    prompt = DEFAULT_ANIMATION_PROMPT
    scrapbook = 'scrapbook' in normalized or 'modern_card' in normalized
    winter_scrapbook = scrapbook and any(season in normalized for season in ('winter', 'christmas', 'snow'))
    if 'ny' in normalized or winter_scrapbook:
        prompt += ' Snow is gently falling in the background.'
    if 'bday' in normalized:
        prompt += ' If there is a birthday cake, the candles are flickering gently.'
    if normalized == 'mode_goodmorning':
        prompt += ' If there is a coffee cup, warm steam is rising from it.'
    return prompt


class VideoServiceError(RuntimeError):
    """Ошибка PiAPI с кодом провайдера или HTTP для точного выбора повторов."""

    def __init__(self, message: str, *, code=None, http_status: int | None = None):
        super().__init__(message)
        self.code = code
        self.http_status = http_status


class VideoTaskFailed(VideoServiceError):
    """PiAPI explicitly marked an accepted task as failed; polling cannot repair it."""


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
        raise VideoServiceError(f"PiAPI HTTP {response.status_code}: {_safe_detail(response.text, key)}",
                                http_status=response.status_code)
    try:
        result = response.json()
    except ValueError:
        raise VideoServiceError("PiAPI вернул некорректный JSON.") from None
    if not isinstance(result, dict):
        raise VideoServiceError("PiAPI вернул некорректный объект ответа.")
    if result.get("code") not in (None, 200):
        raise VideoServiceError(f"Ошибка PiAPI: {_safe_detail(result, key)}", code=result.get("code"))
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
        "input": {"image_url": image_url, "prompt": prompt, "version": "3.0", "mode": "std", "duration": 5,
                  "enable_audio": False},
        "config": {"service_mode": "public"},
    }
    logger.info("PiAPI: создаю задачу Kling (5 секунд, std, 3.0, enable_audio=false).")
    async with httpx.AsyncClient(timeout=REQUEST_TIMEOUT, trust_env=False) as client:
        result = await _request(client, "POST", TASK_URL, key, json=payload)
    data = result.get("data")
    task_id = data.get("task_id") if isinstance(data, dict) else None
    if not isinstance(task_id, str) or not task_id.strip():
        raise VideoServiceError("Нет data.task_id в ответе PiAPI.")
    logger.info("PiAPI: создана задача %s", task_id)
    return task_id


async def create_video_task_with_retry(image_url: str, prompt: str) -> str:
    """До трёх POST только после явного ответа 10000/500 без принятой задачи."""
    for attempt in range(3):
        try:
            return await create_video_task(image_url, prompt)
        except VideoServiceError as exc:
            if exc.code not in (10000, 500, '10000', '500') and exc.http_status != 500:
                raise
            if attempt == 2:
                raise VideoServiceError("Сервис Kling временно недоступен. Попробуйте позже.") from exc
            logger.warning("Попытка %s провалилась (Internal error). Повтор через 15 секунд...", attempt + 1)
            await asyncio.sleep(15)
    raise AssertionError("Недостижимый конец цикла повторов PiAPI.")


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
                        raise VideoTaskFailed(f"PiAPI: задача завершилась с ошибкой: {_safe_detail(detail, key)}")
    except TimeoutError:
        logger.error("PiAPI: таймаут ожидания задачи %s (%s секунд)", task_id, timeout)
        raise TimeoutError(f"Таймаут ожидания PiAPI ({timeout} секунд), task_id={task_id}") from None


async def generate_video_from_avatar(
    avatar_url: str, prompt: str | None = None, *, mode: str = '', people_count: int | None = None,
    task_id: str | None = None, on_task_created: Callable[[str], Awaitable[None]] | None = None,
) -> str:
    """Создать задачу по аватару и вернуть ссылку на готовое видео."""
    if people_count is None:
        people_count = await count_people_in_photo(avatar_url)
    if not isinstance(people_count, int) or isinstance(people_count, bool) or people_count < 0:
        raise ValueError('Некорректное количество людей на фото.')
    video_prompt = build_video_prompt(mode, people_count) if prompt is None or people_count > 5 else prompt
    logger.info('На фото найдено %s человек. Промпт для Kling: %s', people_count, video_prompt)
    logger.info('Промпт для Kling (%s): %s', mode, video_prompt)
    if task_id is None:
        task_id = await create_video_task_with_retry(avatar_url, video_prompt)
        if on_task_created is not None:
            await on_task_created(task_id)
    return await poll_video_task(task_id)


async def render_ded_moroz(video, child, speech, examples, target, name):
    """Offline landscape composition; input speech is the exact approved recording."""
    from pathlib import Path
    from media_service import audio_duration
    from multstudio.render import process as render_process
    video,child,speech,examples,target=map(Path,(video,child,speech,examples,target))
    seconds=await audio_duration(speech)
    if not 0<seconds<=1800:raise ValueError('Недопустимая длительность озвучки.')
    fire=examples/'fire_overlay.mp4'
    has_fire=fire.is_file()
    if not has_fire:logger.warning('Оверлей огня %s не найден; наложение пропущено.',fire)
    for path in (video,child,speech,examples/'audio/ny_music.mp3',examples/'audio/jingle_bells.mp3'):
        if not path.is_file():raise ValueError('Не найден файл монтажа: '+path.name)
    target.parent.mkdir(parents=True,exist_ok=True)
    # Text is data in a separate file; never interpolate child names into FFmpeg expressions.
    title=target.with_name('title.txt');title.write_text('Для '+name+' от Деда Мороза',encoding='utf-8')
    temporary=target.with_name('rendering.mp4')
    offset=int(has_fire)
    child_index,speech_index,bg_index,jingle_index=1+offset,2+offset,3+offset,4+offset
    base_label='base' if has_fire else 'lit'
    filters=(
        f'[0:v]setpts=PTS-STARTPTS,scale=1920:1080:force_original_aspect_ratio=increase,'
        f'crop=1920:1080,setsar=1,fps=30,tpad=stop_mode=clone:stop_duration={seconds:.6f},trim=duration={seconds:.6f}[{base_label}];'
        + ('[1:v]setpts=PTS-STARTPTS,scale=1920:1080,setsar=1,fps=30[fire];'
           '[base][fire]blend=all_mode=screen:shortest=1[lit];' if has_fire else '')
        + f'[{child_index}:v]scale=480:900:force_original_aspect_ratio=decrease,pad=iw+12:ih+12:6:6:color=white[pip];'
        f"[lit][pip]overlay=x=W-w-48:y=H-h-48:enable='between(t,{seconds*.3:.6f},{seconds*.7:.6f})':shortest=1[framed];"
        "[framed]drawtext=textfile=title.txt:expansion=none:fontcolor=white:fontsize=54:"
        "borderw=3:bordercolor=black:x=(w-text_w)/2:y=55:enable='lt(t,5)'[v];"
        f'[{speech_index}:a]asetpts=PTS-STARTPTS,aresample=48000,apad[voice];'
        f'[{bg_index}:a]asetpts=PTS-STARTPTS,aresample=48000,volume=0.2[bg];'
        f'[{jingle_index}:a]atrim=duration=3,asetpts=PTS-STARTPTS,aresample=48000,volume=0.4[jingle];'
        '[voice][bg][jingle]amix=inputs=3:duration=first:normalize=0,alimiter=limit=0.95[a]'
    )
    inputs=['-i',str(video.resolve())]
    if has_fire:inputs+=['-stream_loop','-1','-i',str(fire.resolve())]
    inputs+=['-loop','1','-framerate','30','-i',str(child.resolve()),'-i',str(speech.resolve()),
        '-stream_loop','-1','-i',str((examples/'audio/ny_music.mp3').resolve()),
        '-i',str((examples/'audio/jingle_bells.mp3').resolve())]
    await render_process('ffmpeg','-nostdin','-v','error','-y','-filter_complex_threads','1',
        *inputs,'-filter_complex',filters,
        '-map','[v]','-map','[a]','-t',f'{seconds:.6f}','-r','30','-c:v','libx264','-preset','fast','-crf','20',
        '-pix_fmt','yuv420p','-c:a','aac','-ar','48000','-ac','2','-movflags','+faststart',str(temporary.resolve()),
        timeout=1800,cwd=str(target.parent.resolve()))
    await asyncio.to_thread(temporary.replace,target)
    return target

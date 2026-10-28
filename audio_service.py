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

import aiohttp
import httpx
from openai import AsyncOpenAI

from config import Config
from media_service import audio_duration

logger = logging.getLogger(__name__)

BASE_URL = "https://api.ttapi.io"
LONG_WAIT_MESSAGE = "⏳ Трек все еще создается, это занимает больше времени, чем обычно. Пожалуйста, подождите, я пришлю его, как только он будет готов."

TEACHER_SCENARIO_TITLE = '👨‍🏫 Для тренера / учителя'
TEACHER_LYRICS_INSTRUCTION = (
    'Напиши текст официальной и трогательной песни-благодарности на «Вы» для тренера или учителя. '
    'Включи слова благодарности за вложенные усилия, терпение, поддержку и знания. '
    'Используй имя и детали, которые ввел пользователь. Стиль: элегантный поп или баллада. '
    'Введённая пользователем фраза должна стать смысловой основой каждого припева [Chorus]. '
    'Сохрани имя, вид спорта или предмет и именно ту оценку, которую ввёл пользователь. '
    'Например, «Иван, лучший тренер по футболу» должно звучать в припеве как признание Ивана лучшим тренером по футболу, '
    'а не заменяться общей благодарностью. Не выдумывай другой спорт и не меняй смысл. '
    'В авторских строках обращайся на Вы/Вас/Ваш; пользовательскую фразу в припеве сохраняй дословно.'
)


def build_song_request(details: str, scenario_title: str, audio_format: str, style: str | None = None) -> tuple[str, str, str]:
    """Ответ целиком, включая имя и запятые, становится обязательной фразой припева."""
    if audio_format not in {'fun', 'gift'} or not details.strip():
        raise ValueError('Некорректные детали песни.')
    chorus = details.strip()
    prompt = (f"[Verse 1]\nС тобою мир становится светлей,\nИ каждый день наполнен добротою.\n[Chorus]\n{chorus}"
              f"\n[Verse 2]\n{details.strip()}\nИз этих встреч слагается история твоя.\n[Chorus]\n{chorus}"
              f"\n[Bridge]\nПусть впереди сияет новый день,\nИ счастье остаётся рядом с нами.\n[Chorus]\n{chorus}")
    tags = str(style or ('upbeat pop, playful, Russian vocals, short song, 30-40 seconds' if audio_format == 'fun'
                        else 'warm emotional pop, Russian vocals, heartfelt, 2-3 minutes'))
    if scenario_title == '🎄 Новогодняя':
        opening = ('За окном снежинки, огоньки горят,\nИ улыбки дарит праздник всем подряд.' if audio_format == 'fun' else
                   'За одним столом собралась семья,\nВ свете огоньков рядом ты и я.')
        bridge = ('Пусть подарки ждут у ёлки, смех звучит повсюду!' if audio_format == 'fun' else
                  'Вспомним наши тёплые семейные мгновения,\nПусть подарит Новый год надежды и свершения.')
        wishes = ('С Новым годом! Счастья, радости, подарков!' if audio_format == 'fun' else
                  'С Новым годом, родные, пусть в доме тепло!')
        prompt = (f'[Verse 1]\n{opening}\n[Chorus]\n{chorus}\n[Verse 2]\n{details.strip()}\n'
                  f'{wishes}\nПусть сбываются добрые пожелания.\n[Chorus]\n{chorus}\n[Bridge]\n{bridge}\n[Chorus]\n{chorus}')
        tags = str(style or ('cheerful festive pop' if audio_format == 'fun' else 'warm family acoustic pop'))
        tags += ', New Year, Russian vocals, ' + ('playful, happiness and gifts, short song, 30-40 seconds' if audio_format == 'fun' else
                                                'heartfelt family memories and wishes, 2-3 minutes')

    if scenario_title == TEACHER_SCENARIO_TITLE:
        prompt = (f'[Verse 1]\nВаш труд оставит в сердце добрый свет,\nИ благодарность бережно хранится.\n'
                  f'[Chorus]\n{chorus}\n[Verse 2]\n{details.strip()}\nВаш пример поможет нам стремиться.\n'
                  f'[Chorus]\n{chorus}\n[Bridge]\nСпасибо Вам за знания и труд, за веру в нас, терпение, поддержку.\n[Chorus]\n{chorus}')
        tags = str(style or 'elegant pop ballad') + ', formal respectful Russian vocals, gratitude, '
        tags += 'short song, 30 seconds' if audio_format == 'fun' else 'touching ballad, 2-3 minutes'
    if len(prompt) > 5000 or len(tags) > 1000:
        raise ValueError('Слишком много деталей для TTAPI.')
    return prompt, tags, f"{scenario_title}: {details.split(',')[0]}"[:80]


class AudioServiceError(RuntimeError):
    """Ошибка TTAPI, безопасная для журналирования без ключа и тела ответа."""


class AudioNotConfigured(AudioServiceError):
    pass


class SongFailed(AudioServiceError):
    pass


class InvalidSongArguments(ValueError):
    """Ошибка входных данных до отправки платного POST в TTAPI."""


def validate_song_arguments(prompt, tags, title) -> None:
    for value, name, limit in ((prompt, 'Текст песни', 5000), (tags, 'Стиль музыки', 1000), (title, 'Название песни', 80)):
        if not isinstance(value, str) or not value.strip():
            raise InvalidSongArguments(f'{name} не задан. Заполните данные песни заново.')
        if len(value) > limit:
            raise InvalidSongArguments(f'{name}: не более {limit} символов.')


LYRICS_SYSTEM_PROMPT = (
    'Ты профессиональный автор персональных песен на русском языке. '
    'НЕ перечисляй факты и привычки в первом куплете. Начни песню с эмоционального или поэтического вступления '
    '(например, о том, какой это замечательный человек). Факты и детали аккуратно вплети в повествование второго куплета или бриджа. '
    'Соблюдай классическую структуру: [Куплет 1] -> [Припев] -> [Куплет 2] -> [Припев] -> [Бридж] -> [Припев]. '
    'Для Suno записывай эти шесть секций как [Verse 1], [Chorus], [Verse 2], [Chorus], [Bridge], [Chorus]. '
    'Верни ТОЛЬКО текст песни. Не пиши название песни, название сценария или заголовок. Начинай сразу с [Verse 1]. '
    'Сохрани имена и смысл деталей; кроме обязательной фразы припева, не вставляй перечень из анкеты дословно. Не добавляй пояснения или кодовые блоки. '
    'Если нужен короткий формат 30–40 секунд, используй 1–2 строки на секцию, иначе 2–4 строки. До 5000 символов.'
    ' CRITICAL REQUIREMENT: В припеве обязательно должна прозвучать фраза, которую ввел пользователь. Используй её дословно. '
    'Поле required_chorus_phrase содержит эту фразу целиком, включая имя. Включи её в каждый [Chorus] без изменения слов, '
    'пунктуации и регистра. Не исправляй и не склоняй её, не заменяй синонимами. Остальные строки можно сочинять свободно.'
)


async def write_song_lyrics(notes: str, tags: str, title: str) -> str:
    settings = Config.from_env()
    if not settings.image_api_key:
        raise AudioNotConfigured('Не настроена текстовая модель для стихов.')
    chorus = re.search(r'\[Chorus\]\s*\n(.*?)(?=\n\[|$)', notes, flags=re.S)
    required_phrase = chorus.group(1).strip() if chorus else None
    instruction = LYRICS_SYSTEM_PROMPT
    if title.startswith(TEACHER_SCENARIO_TITLE + ':'):
        instruction += ' ' + TEACHER_LYRICS_INSTRUCTION
        if '30 seconds' in tags:
            instruction += ' Длина около 30 секунд: по 1–2 короткие строки в секции.'
    try:
        async with httpx.AsyncClient(proxy=settings.openai_proxy_url, timeout=60, trust_env=False) as http_client:
            async with AsyncOpenAI(api_key=settings.image_api_key, base_url='https://api.openai.com/v1',
                                   http_client=http_client, max_retries=0) as client:
                result = await client.chat.completions.create(model='gpt-4o-mini', temperature=0.7,
                    messages=[{'role': 'system', 'content': instruction},
                              {'role': 'user', 'content': json.dumps({'details': notes, 'style': tags, 'title_for_context_only': title,
                                  'required_chorus_phrase': required_phrase}, ensure_ascii=False)}])
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
    if required_phrase:
        choruses = re.findall(r'\[Chorus\]\s*\n(.*?)(?=\n\[|$)', text, flags=re.S)
        if len(choruses) != 3 or not all(required_phrase in part for part in choruses):
            raise AudioServiceError('Модель изменила фразу припева. Повторите генерацию.')
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
            duration = await audio_duration(source)
            start = '00:00:20' if duration >= 35 else ('00:00:05' if duration > 5 else '00:00:00')
            logger.info('Превью песни: длительность %.2f с, начало %s.', duration, start)
            # Фильтр добавляет короткую звуковую метку каждые пять секунд.
            process = await asyncio.create_subprocess_exec(binary, '-nostdin', '-v', 'error', '-protocol_whitelist', 'file,pipe',
                '-ss', start, '-i', str(source), '-f', 'lavfi', '-i', 'sine=frequency=1100:duration=15',
                '-filter_complex', "[0:a]atrim=0:15,asetpts=PTS-STARTPTS[a];[1:a]volume='if(lt(mod(t,5),0.7),0.5,0)':eval=frame[b];[a][b]amix=inputs=2:duration=first:normalize=0[out]",
                '-map', '[out]', '-map_metadata', '-1', '-map_chapters', '-1', '-t', '00:00:15', '-codec:a', 'libmp3lame', '-b:a', '128k', '-y', str(target),
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


def _safe_response(value) -> str:
    text = value if isinstance(value, str) else json.dumps(value, ensure_ascii=False)
    key = Config.from_env().ttapi_key
    return text.replace(key, '[redacted]') if key else text


async def _request(method: str, path: str, **kwargs) -> dict:
    headers = _headers()
    try:
        async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=30)) as session:
            async with session.request(method, BASE_URL + path, headers=headers, allow_redirects=False, **kwargs) as response:
                body = await response.text()
                if response.status != 200:
                    raise AudioServiceError(f'TTAPI Error: {response.status} - {_safe_response(body)}')
                try:
                    result = json.loads(body)
                except ValueError as exc:
                    raise AudioServiceError(f'Некорректный JSON TTAPI: {_safe_response(body)}') from exc
    except TimeoutError as exc:
        raise TimeoutError('Таймаут запроса TTAPI (30 секунд)') from exc
    except aiohttp.ClientError as exc:
        raise AudioServiceError(f'Ошибка соединения с TTAPI: {_safe_response(str(exc))}') from exc
    if not isinstance(result, dict):
        raise AudioServiceError(f'Неожиданный ответ TTAPI: {_safe_response(result)}')
    return result


async def generate_song(prompt: str, tags: str, title: str) -> str:
    validate_song_arguments(prompt, tags, title)
    suffix = ', start immediately with vocals, no long intro'
    tags = tags if tags.endswith(suffix) else tags + suffix
    validate_song_arguments(prompt, tags, title)
    result = await _request('POST', '/suno/v1/music', json={
        'custom': True, 'instrumental': False, 'mv': 'chirp-v6',
        'prompt': prompt, 'title': title, 'tags': tags, 'audio_format': 'mp3',
    })
    if result.get('status') in {'FAILED', 'ERROR'}:
        raise SongFailed(f'TTAPI отклонил генерацию: {_safe_response(result)}')
    data = result.get('data')
    job_id = result.get('jobId') or (data.get('jobId') if isinstance(data, dict) else None)
    if not isinstance(job_id, str) or not job_id.strip():
        raise AudioServiceError(f'Нет jobId в ответе: {_safe_response(result)}')
    logger.info('Получен jobId: %s', job_id)
    return job_id


async def fetch_song(job_id: str) -> dict:
    if not isinstance(job_id, str) or not job_id.strip():
        raise ValueError('Не задан jobId.')
    return await _request('GET', '/suno/v2/fetch', params={'jobId': job_id})


async def wait_for_song_result(job_id: str, timeout: int = 100) -> dict:
    """Полный ответ нужен оплаченному формату с двумя вариантами песни."""
    if timeout <= 0:
        raise TimeoutError('Таймаут ожидания TTAPI (100 секунд)')
    budget = asyncio.timeout(timeout)
    try:
        async with budget:
            for attempt in range(20):
                await asyncio.sleep(5)
                data = await fetch_song(job_id)
                status = data.get('status')
                logger.info('Попытка %s: статус %s (job_id=%s)', attempt + 1, status, job_id)
                if status == 'SUCCESS':
                    song_tracks(data)  # Пустой SUCCESS тоже должен дать понятную ошибку.
                    return data
                if status in {'FAILED', 'ERROR'}:
                    logger.error('TTAPI вернул %s (job_id=%s): %s', status, job_id, _safe_response(data))
                    raise SongFailed(f'TTAPI вернул {status}: {_safe_response(data)}')
    except TimeoutError as exc:
        if budget.expired():
            raise TimeoutError('Таймаут ожидания TTAPI (100 секунд)') from exc
        raise
    raise TimeoutError('Таймаут ожидания TTAPI (100 секунд)')


async def wait_for_song(job_id: str, timeout: int = 100) -> str:
    data = await wait_for_song_result(job_id, timeout=timeout)
    return song_audio_url(data)


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


def song_tracks(result: dict | str) -> list[dict[str, str]]:
    """Только реальные HTTPS audioUrl, максимум два варианта, без дубликатов."""
    if isinstance(result, str):
        musics = [{'audioUrl': result}]
    else:
        data = result.get('data')
        musics = data if isinstance(data, list) else data.get('musics') if isinstance(data, dict) else None
    if not isinstance(musics, list):
        raise AudioServiceError("TTAPI не вернул список треков.")
    tracks = []
    for music in musics:
        if not isinstance(music, dict):
            continue
        url = music.get("audioUrl") or music.get("audio_url")
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


def song_audio_url(result: dict | str) -> str:
    """Ссылка на готовое аудио; lyrics/prompt никогда не заменяют отсутствующий MP3."""
    return song_tracks(result)[0]['audioUrl']

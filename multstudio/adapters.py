"""One paid POST per confirmed job; polling is a separate, retryable operation."""
import base64
import json
import logging
import os
from dataclasses import replace
from urllib.parse import quote, quote_plus

import httpx

from image_service import _image_api_client, _generate_image_bytes
from .repository import encoded
from .prompts import scene_image_prompt, validate_scene_image_references
from .tts import voice_settings, validate_speech_snapshot, VOICES, EMOTIONS

logger = logging.getLogger(__name__)


def _tts_error_text(response, api_key, text):
    """Keep the full provider explanation, masking only known echoed secrets."""
    body = response.text
    variants = set()
    for value in (api_key, text):
        if value:
            variants.update((value, json.dumps(value, ensure_ascii=True)[1:-1],
                             json.dumps(value, ensure_ascii=False)[1:-1],
                             quote(value, safe=''), quote_plus(value, safe='')))
    for value in sorted(variants, key=len, reverse=True):
        body = body.replace(value, '[REDACTED]')
    return body


class TemporaryPollError(Exception):
    pass


class ProviderFailed(Exception):
    pass


class KlingTaskFailed(ProviderFailed):
    """Provider explicitly reports a TERMINAL task status, unlike a transport error."""


class YandexTTSAdapter:
    ENDPOINT = 'https://tts.api.cloud.yandex.net/speech/v1/tts:synthesize'

    def __init__(self, key, folder_id=None, transport=None):
        # folder_id is accepted only for compatibility with previously saved settings.
        self.key, self.transport = key, transport

    def ready(self):
        if not self.key:
            raise ValueError('Задайте YANDEX_TTS_API_KEY для мультстудии.')

    async def synthesize(self, text: str, voice: str, emotion: str = 'neutral') -> bytes:
        self.ready()
        if not isinstance(text, str) or not text.strip() or len(text) > 5000:
            raise ValueError('Реплика SpeechKit должна содержать от 1 до 5000 символов.')
        if voice not in VOICES or emotion not in EMOTIONS:
            raise ValueError('Нужен поддерживаемый голос и эмоция SpeechKit v1.')
        try:
            async with httpx.AsyncClient(timeout=60, trust_env=False, follow_redirects=False,
                                         transport=self.transport) as client:
                result = await client.post(self.ENDPOINT, headers={'Authorization': 'Api-Key ' + self.key},
                    data=dict(text=text, lang='ru-RU', voice=voice, format='mp3'))
        except httpx.HTTPError as exc:
            logger.warning('Yandex TTS: ошибка соединения (%s)',type(exc).__name__)
            raise ProviderFailed('Ошибка соединения со SpeechKit. Новый платный запрос автоматически не отправляется.') from None
        if result.status_code != 200:
            # Do not truncate provider details; mask credentials/dialogue if echoed.
            logger.warning('Yandex TTS: запрос отклонён, HTTP %s; ответ Yandex: %s',
                           result.status_code,_tts_error_text(result,self.key,text))
            raise ProviderFailed(f'SpeechKit отклонил запрос: HTTP {result.status_code}.')
        data = result.content
        if not (data.startswith(b'ID3') or (len(data)>1 and data[0]==255 and data[1]&224==224)):
            logger.warning('Yandex TTS: вместо MP3 получен некорректный ответ, bytes=%s',len(data))
            raise ProviderFailed('SpeechKit не вернул непустое MP3-аудио.')
        logger.info('Yandex TTS: озвучено %s символов, голос %s',len(text),voice)
        return data


async def yandex_tts(text: str, voice: str, emotion: str = 'neutral', *,
                     api_key=None, folder_id=None) -> bytes:
    key=os.getenv('YANDEX_TTS_API_KEY','').strip() if api_key is None else api_key
    # API-key auth uses the service account's folder; never send a folder override.
    return await YandexTTSAdapter(key).synthesize(text,voice,emotion)


class KlingAdapter:
    ENDPOINT = 'https://api.piapi.ai/api/v1/task'

    def __init__(self, key, transport=None):
        self.key, self.transport = key, transport

    def ready(self):
        if not self.key:
            raise ValueError('Задайте STUDIO_KLING_API_KEY или PIAPI_KEY.')

    async def submit(self, image_url, prompt, duration):
        self.ready()
        async with httpx.AsyncClient(timeout=60, trust_env=False, transport=self.transport) as client:
            response = await client.post(self.ENDPOINT, headers={'X-API-Key': self.key}, json={
                'model':'kling', 'task_type':'video_generation',
                'input':dict(image_url=image_url, prompt=prompt, version='3.0', mode='std',
                             duration=duration, enable_audio=False, aspect_ratio='9:16'),
                'config':{'service_mode':'public'}})
        # Even a transport error or HTTP 5xx can hide an accepted task: caller must not retry POST.
        if not response.is_success:
            raise ProviderFailed('Kling: запрос требует ручной сверки; HTTP ' + str(response.status_code))
        result = response.json()
        task_id = result.get('data', {}).get('task_id')
        if not isinstance(task_id, str) or not task_id:
            raise ProviderFailed('Kling не вернул task_id; требуется ручная сверка.')
        return task_id

    async def poll(self, task_id):
        self.ready()
        try:
            async with httpx.AsyncClient(timeout=30, trust_env=False, transport=self.transport) as client:
                response = await client.get(self.ENDPOINT + '/' + quote(task_id, safe=''), headers={'X-API-Key':self.key})
        except httpx.RequestError:
            raise TemporaryPollError('Временный сбой опроса Kling.') from None
        if response.status_code == 429 or response.status_code >= 500:
            raise TemporaryPollError('Временный HTTP-сбой опроса Kling.')
        if not response.is_success:
            raise ProviderFailed('Kling: HTTP ' + str(response.status_code))
        data = response.json().get('data', {})
        if data.get('status') == 'failed':
            raise KlingTaskFailed('Kling подтвердил терминальный статус failed.')
        if data.get('status') != 'completed':
            return None
        url = data.get('output', {}).get('video')
        if not isinstance(url, str) or not url.startswith('https://'):
            raise ProviderFailed('Kling не вернул HTTPS-видео.')
        # Do not infer dollars from provider credits/tokens: reconcile against its invoice.
        return dict(url=url, usage=data.get('meta', {}).get('usage', {}))


class Providers:
    def __init__(self, settings, config, assets):
        self.settings, self.config, self.assets = settings, config, assets
        self.kling = KlingAdapter(settings.kling_key)
        self.tts = YandexTTSAdapter(settings.tts_key)

    def preflight(self, kind):
        from image_service import _storage_client
        _storage_client(self.config)  # Validate storage credentials before any paid POST.
        if not self.config.storage_public_base_url:
            raise ValueError('Задайте STORAGE_PUBLIC_BASE_URL для студийных референсов и результатов.')
        if kind=='image':
            self.assets.canonical_image_url('multstudio/preflight.png')
        if kind == 'speech':
            self.tts.ready()
            from .render import Renderer
            Renderer(self.assets).ready()
        if kind == 'video':
            self.kling.ready()
        elif kind == 'image' and not self.config.image_api_key:
            raise ValueError('Задайте IMAGE_API_KEY.')

    async def image(self, snap):
        validate_scene_image_references(snap)
        config = replace(self.config, image_model=snap['quote']['model'], image_quality=snap['quote']['image_quality'])
        if snap['target'] == 'character':
            prompt = ('Create a single canonical character reference portrait, full body and clear face. '
                      'Family-friendly high-quality 3D animation. Keep specified age, outfit and proportions. '
                      'Neutral background, no text. ' + encoded(snap['world']) + '\n' + encoded(snap['spec']))
            refs = [snap['assets']['image']] if snap['assets'].get('image') else []
        else:
            refs = [c['assets']['image'] for c in snap['cast']]
            if snap.get('location_reference'):
                refs.append(snap['location_reference'])
        urls = [await self.assets.asset_url(a) for a in refs]
        if snap['target'] == 'scene':
            prompt = scene_image_prompt(snap, urls[-1] if snap.get('location_reference') else None)
        await self.assets.retain(snap,'image_prompt',data=prompt.encode(),suffix='txt',job_id=snap.get('job_id'))
        if refs:
            images = await _generate_image_bytes(urls[0], 'Мультстудия', config, count=1,
                prompt_override=prompt, image_size='1024x1536',
                **({'style_reference_urls':tuple(urls[1:])} if len(urls)>1 else {}))
            return images[0]
        async with _image_api_client(config) as client:
            result = await client.images.generate(model=config.image_model, prompt=prompt, n=1,
                                                   size='1024x1536', quality=config.image_quality)
        item = result.data[0]
        if not item.b64_json:
            raise ProviderFailed('Генератор не вернул PNG эталона.')
        return base64.b64decode(item.b64_json, validate=True)

    async def speech_line(self, snap, line):
        validate_speech_snapshot(snap)
        character = next((c for c in snap['cast'] if c['id'] == line['character_id']), None)
        if character is None:
            raise ValueError('Говорящий персонаж отсутствует в сцене.')
        voice, emotion = voice_settings(character['spec'])
        return await yandex_tts(line['text'],voice,emotion,api_key=self.settings.tts_key)

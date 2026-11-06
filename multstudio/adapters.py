"""One paid POST per confirmed job; polling is a separate, retryable operation."""
import base64
from dataclasses import replace
from urllib.parse import quote

import httpx

from image_service import _image_api_client, _generate_image_bytes
from .repository import encoded


class TemporaryPollError(Exception):
    pass


class ProviderFailed(Exception):
    pass


class KlingTaskFailed(ProviderFailed):
    """Provider explicitly reports a TERMINAL task status, unlike a transport error."""


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

    def preflight(self, kind):
        from image_service import _storage_client
        _storage_client(self.config)  # Validate storage credentials before any paid POST.
        if not self.config.storage_public_base_url:
            raise ValueError('Задайте STORAGE_PUBLIC_BASE_URL для студийных референсов и результатов.')
        if kind == 'speech':
            from .render import Renderer
            Renderer(self.assets).ready()
        if kind == 'video':
            self.kling.ready()
        elif kind == 'speech' and not self.settings.tts_key:
            raise ValueError('Задайте STUDIO_TTS_API_KEY или IMAGE_API_KEY.')
        elif kind == 'image' and not self.config.image_api_key:
            raise ValueError('Задайте IMAGE_API_KEY.')

    async def image(self, snap):
        config = replace(self.config, image_model=snap['quote']['model'], image_quality=snap['quote']['image_quality'])
        if snap['target'] == 'character':
            prompt = ('Create a single canonical character reference portrait, full body and clear face. '
                      'Family-friendly high-quality 3D animation. Keep specified age, outfit and proportions. '
                      'Neutral background, no text. ' + encoded(snap['world']) + '\n' + encoded(snap['spec']))
            refs = [snap['assets']['image']] if snap['assets'].get('image') else []
        else:
            prompt = ('Create ONE vertical family 3D animated-film keyframe. Use character reference images '
                      'as exact identity/outfit models, NOT as photographic humans. Consistent faces, ages, '
                      'hair, clothing and body proportions. No subtitles, logos or advertising. '
                      'The final reference, if supplied, is the apartment: preserve layout, furniture, props '
                      'and lighting; change only camera and action. Scene and camera:\n' + encoded(snap['spec'])
                      + '\nWORLD:\n' + encoded(snap['world']) + '\nCAST IN IMAGE ORDER:\n'
                      + encoded([c['spec'] for c in snap['cast']]))
            refs = [c['assets']['image'] for c in snap['cast']]
            if snap.get('location_reference'):
                refs.append(snap['location_reference'])
        await self.assets.retain(snap,'image_prompt',data=prompt.encode(),suffix='txt',job_id=snap.get('job_id'))
        if refs:
            urls = [await self.assets.asset_url(a) for a in refs]
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
        character = next((c for c in snap['cast'] if c['id'] == line['character_id']), None)
        if character is None:
            raise ValueError('Говорящий персонаж отсутствует в сцене.')
        config = replace(self.config, image_api_key=self.settings.tts_key)
        async with _image_api_client(config) as client:
            result = await client.audio.speech.create(model='gpt-4o-mini-tts', voice=character['spec']['voice'],
                input=line['text'], response_format='mp3', instructions=character['spec'].get('voice_direction', '')
                + ' ' + line.get('direction', '') + ' Speak naturally in Russian. Never artificially speed up.')
            return result.content

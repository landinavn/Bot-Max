"""Studio Talking Photo: one video POST with an image and existing speech audio."""
import httpx

from heygen import HeyGenAdapter, HeyGenError
from .adapters import TemporaryPollError, ProviderFailed
from .assets import https_url


class StudioHeyGen(HeyGenAdapter):
    def ready(self):
        if not self.key:
            raise ValueError('Задайте HEYGEN_API_KEY для говорящих сцен мультстудии.')

    async def scene_video(self, image_url, audio_url, key):
        """Avatar IV Talking Photo; no avatar, voice creation or TTS request."""
        self.ready(); https_url(image_url); https_url(audio_url)
        data=await self.request('POST','/v3/videos',payload={
            'type':'image','image':{'type':'url','url':image_url},'audio_url':audio_url,
            'aspect_ratio':'9:16','resolution':'1080p','expressiveness':'medium'},idempotency=key)
        return self.identifier(data.get('video_id') or data.get('id'))

    async def avatar_video(self, avatar_id, audio_url, key):
        """Finish only an old job whose Photo Avatar was already created and paid."""
        self.ready(); https_url(audio_url)
        data=await self.request('POST','/v3/videos',payload={
            'type':'avatar','avatar_id':self.identifier(avatar_id),'audio_url':audio_url,
            'aspect_ratio':'9:16','resolution':'1080p','engine':{'type':'avatar_iv'},
            'expressiveness':'medium'},idempotency=key)
        return self.identifier(data.get('video_id') or data.get('id'))

    async def poll(self, provider_id, *, avatar=False):
        try:
            data=await (self.avatar_status(provider_id) if avatar else self.video_status(provider_id))
        except HeyGenError as exc:
            if exc.status_code==429 or (exc.status_code and exc.status_code>=500):
                raise TemporaryPollError('Временный сбой опроса HeyGen.') from None
            raise
        except httpx.RequestError:
            raise TemporaryPollError('Временный сбой соединения при опросе HeyGen.') from None
        if data.get('status') in ('failed','error'):
            raise ProviderFailed('HeyGen сообщил об ошибке сцены. Проверьте статус задачи перед повторной генерацией.')
        if data.get('status')!='completed': return None
        if avatar: return dict(avatar_id=provider_id)
        https_url(data.get('video_url',''))
        return dict(url=data['video_url'])
